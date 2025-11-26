# -*- coding: utf-8 -*-
"""
Steam: スクリプト実行時刻（JST）から24時間以内に終了予定のセールを ITAD→Steam 連携で収集し、
【日本語レビュー10件以上】の作品のみを整形し、投稿は「次に到来するJSTの9:00」から
5分間隔で TOP5 タイトルを個別ツイートします。
（対象はソフト単体 = Steam app のみ。JPストア基準）

仕様（このコード版）:
  - 対象: 「実行した瞬間〜24時間後」(JST) にセール終了予定のSteamゲーム
  - 日本語レビュー数が MIN_JP_REVIEWS 以上のゲームのみ
  - ITAD / Steam APIから以下の情報を使用:
      * 価格（元値・セール価格・割引率）
      * 日本語レビュー件数（日本語のみ）
      * レビュー評価%とラベル（/appreviews の全言語ベース）
      * ジャンル（最大2つ、日本語表記）
      * ITAD の lowest から「今回最安値です」判定
  - TOP5件を抽出し、実行時刻から5分間隔で個別ツイート
  - 本文にはURLを含めず、ストアURLは各ツイートへの自己リプライに記載
  - 文面は絵文字控えめ（👇のみ）かつ情報重視

準備:
  1) pip install -r requirements.txt
  2) ITAD_API_KEY, X_CLIENT_ID, X_CLIENT_SECRET, X_REDIRECT_URI を設定
  3) 初回のみ X_REFRESH_TOKEN を GitHub Secrets へ設定（ローカル運用なら itad_x_tokens.jsonでも可）

オプション:
  - 環境変数 DEFER_OFFSET_SEC: 9:00 からの遅延秒（例: 10 を指定すると 9:00:10 に投稿開始）
"""

import os
import sys
import json
import time
import base64
import random
import tempfile
import pathlib
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ====== 設定 ======
COUNTRY = "JP"
JST = timezone(timedelta(hours=9))
HASHTAG = "#Steamセール"
POST_TO_X = True  # Falseなら投稿せずプレビューのみ

# 認証情報（Confidential/Web App）
ITAD_API_KEY    = os.getenv("ITAD_API_KEY") or "YOUR_ITAD_API_KEY"
X_CLIENT_ID     = os.getenv("X_CLIENT_ID") or "YOUR_X_CLIENT_ID"
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET") or "YOUR_X_CLIENT_SECRET"
X_REDIRECT_URI  = os.getenv("X_REDIRECT_URI") or "http://localhost/callback"

# refresh_token 保存先（ローカル運用時のみ使用）
TOKEN_FILE = "itad_x_tokens.json"

# GitHub Actions 用：新しい refresh_token を出力する先
GHA_NEW_RT_PATH = os.getenv("GHA_NEW_RT_PATH")  # 例: new_refresh_token.txt

# スロットル/閾値
ITAD_SLEEP_SEC = 1.0
STEAM_MIN_INTERVAL = {"appdetails": 1.0, "appreviews": 1.0}
STEAM_429_SLEEP_BASE = 6.0
STEAM_429_SLEEP_CAP = 45.0
MIN_JP_REVIEWS = 10
JP_REVIEW_WORKERS = 2
ITAD_API_BASE = "https://api.isthereanydeal.com"

# 9:00からの遅延秒（微調整用）
DEFER_OFFSET_SEC = int(os.getenv("DEFER_OFFSET_SEC", "0") or "0")

# ログ
DEBUG = True
def ts(): return datetime.now(JST).strftime("%H:%M:%S")
def log(msg):
    if DEBUG:
        print(f"[{ts()}] {msg}")

# ====== バリデーション ======
if not ITAD_API_KEY:
    raise RuntimeError("ITAD_API_KEY が未設定です。")
if not (X_CLIENT_ID and X_CLIENT_SECRET and X_REDIRECT_URI):
    raise RuntimeError("X_CLIENT_ID / X_CLIENT_SECRET / X_REDIRECT_URI を設定してください。")

# ====== パス ======
def _base_dir():
    try:
        return pathlib.Path(__file__).resolve().parent
    except NameError:
        return pathlib.Path(os.getcwd())

def _token_path():
    p = pathlib.Path(TOKEN_FILE)
    if not p.is_absolute():
        p = _base_dir() / p
    return p.resolve()

# ====== refresh_token 読み/書き ======
def _load_refresh_token():
    env_rt = (os.getenv("X_REFRESH_TOKEN") or "").strip()
    if env_rt:
        if DEBUG:
            log("[TOKEN] Loaded refresh_token from ENV (X_REFRESH_TOKEN)")
        return env_rt

    path = _token_path()
    if not path.exists():
        raise RuntimeError(
            f"refresh_tokenファイルが見つかりません: {path}\n"
            "初回は itad_x_tokens.json を {\"refresh_token\":\"...\"} の形で作成するか、"
            "GitHub Actions では Secrets に X_REFRESH_TOKEN を設定してください。"
        )
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw)
        rt = (data.get("refresh_token") or "").strip()
        if not rt:
            raise RuntimeError(f"{path} に refresh_token がありません")
        if DEBUG:
            log(f"[TOKEN] Loaded refresh_token from {path}")
        return rt
    except Exception as e:
        raise RuntimeError(
            f"refresh_token読み込み失敗: {e}\n"
            "JSONはコメント不可・ダブルクォートのみ・末尾カンマ無しで保存してください。"
        )

def _save_refresh_token(new_rt: str):
    if not new_rt:
        return
    # (1) ローカル保存
    try:
        path = _token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_rt_", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"refresh_token": new_rt}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(path))
            try:
                os.chmod(str(path), 0o600)
            except Exception:
                pass
            if DEBUG:
                log(f"[TOKEN] Saved refresh_token to {path}")
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    except Exception as e:
        if DEBUG:
            log(f"[TOKEN] local save skipped: {type(e).__name__}: {e}")
    # (2) GHA用吐き出し
    if GHA_NEW_RT_PATH:
        try:
            pathlib.Path(GHA_NEW_RT_PATH).write_text(new_rt, encoding="utf-8")
            if DEBUG:
                log(f"[TOKEN] Emitted new RT to {GHA_NEW_RT_PATH}")
        except Exception as e:
            if DEBUG:
                log(f"[TOKEN] emit to GHA_NEW_RT_PATH failed: {type(e).__name__}: {e}")

# ====== HTTP セッション ======
_session = requests.Session()
_session_adapter = HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=0.8,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
))
_session.mount("http://", _session_adapter)
_session.mount("https://", _session_adapter)

_steam_session = requests.Session()
_steam_adapter = HTTPAdapter(max_retries=Retry(total=0))
_steam_session.mount("http://", _steam_adapter)
_steam_session.mount("https://", _steam_adapter)
_steam_session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
})

# ====== スロットル ======
_last_steam_ts = {"appdetails": 0.0, "appreviews": 0.0}
_throttle_lock = Lock()

def _throttle_steam(kind: str):
    with _throttle_lock:
        now = time.time()
        last = _last_steam_ts.get(kind, 0.0)
        min_gap = STEAM_MIN_INTERVAL.get(kind, 1.0)
        gap = now - last
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last_steam_ts[kind] = time.time()

def _get_with_retry(url, params, max_retry=6, base_wait=2.0, kind="appdetails"):
    extra_backoff = 0.0
    for i in range(max_retry):
        _throttle_steam(kind)
        try:
            r = _steam_session.get(url, params=params, timeout=30)
        except requests.exceptions.RetryError:
            r = type("Dummy", (), {"status_code": 429, "headers": {}})()

        if r.status_code == 200:
            return r

        if r.status_code == 400:
            err = requests.HTTPError("400 Bad Request"); err.response = r
            raise err

        if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            retry_after = getattr(r, "headers", {}).get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else base_wait * (2 ** i)
            except Exception:
                wait = base_wait * (2 ** i)
            extra_backoff = min((STEAM_429_SLEEP_BASE * (i + 1)), STEAM_429_SLEEP_CAP)
            time.sleep(wait + random.uniform(0.3, 0.9) + extra_backoff)
            with _throttle_lock:
                _last_steam_ts[kind] = 0.0
            continue

        http_err = requests.HTTPError(f"{r.status_code} Error"); http_err.response = r
        raise http_err

    time.sleep(max(20.0, extra_backoff))
    _throttle_steam(kind)
    r = _steam_session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r

# ====== ITAD呼び出し ======
def get_with_key(url, params=None):
    params = dict(params or {}); params["key"] = ITAD_API_KEY
    r = _session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r

def post_with_key(url, json_body=None):
    sep = "&" if "?" in url else "?"
    url2 = f"{url}{sep}key={ITAD_API_KEY}"
    r = _session.post(url2, json=json_body or {}, timeout=30)
    r.raise_for_status()
    return r

def get_steam_shop_id():
    r = get_with_key(f"{ITAD_API_BASE}/service/shops/v1", params={"country": COUNTRY})
    for s in r.json():
        if (s.get("title") or "").lower() == "steam":
            return s.get("id")
    return 61  # フォールバック

def list_steam_deals_expiring_window(start, end):
    steam_shop_id = get_steam_shop_id()
    deals, offset = [], 0
    sort_candidates = ["expiry", "-expiry", "-cut"]
    used_sort = None

    for sort_key in sort_candidates:
        try:
            deals.clear(); offset = 0
            too_far_pages = 0
            while True:
                r = get_with_key(
                    f"{ITAD_API_BASE}/deals/v2",
                    params={
                        "country": COUNTRY,
                        "shops": str(steam_shop_id),
                        "limit": 200,
                        "offset": offset,
                        "sort": sort_key,
                    },
                )
                data = r.json()
                lst = [d for d in (data.get("list") or [])
                       if (d.get("type") or "").lower() == "game"]

                page_in, page_out = 0, 0
                for d in lst:
                    expiry = (d.get("deal") or {}).get("expiry")
                    if not expiry:
                        continue
                    try:
                        exp_dt = dtparser.isoparse(expiry).astimezone(JST)
                    except Exception:
                        continue
                    if start <= exp_dt <= end:
                        deals.append(d); page_in += 1
                    elif exp_dt > end:
                        page_out += 1

                if page_in == 0 and page_out > 0:
                    too_far_pages += 1
                else:
                    too_far_pages = 0
                if too_far_pages >= 3:
                    used_sort = sort_key
                    break

                if not data.get("hasMore"):
                    used_sort = sort_key
                    break
                offset = data.get("nextOffset", 0)
                time.sleep(ITAD_SLEEP_SEC)
            if used_sort:
                break
        except requests.HTTPError:
            continue

    log(f"ITAD deals (game-only, sort={used_sort}): expiring_in_window={len(deals)}")
    return deals

def map_itad_ids_to_appids(itad_ids, steam_shop_id):
    appids = {}
    CHUNK = 200
    for i in range(0, len(itad_ids), CHUNK):
        chunk = itad_ids[i:i+CHUNK]
        r = post_with_key(f"{ITAD_API_BASE}/lookup/shop/{steam_shop_id}/id/v1", json_body=chunk)
        mapping = r.json() or {}
        for itad_id, ids in (mapping.items() if mapping else []):
            if not ids:
                continue
            for sid in ids:
                if isinstance(sid, str) and sid.startswith("app/"):
                    try:
                        appids[itad_id] = int(sid.split("/", 1)[1]); break
                    except Exception:
                        continue
        time.sleep(ITAD_SLEEP_SEC)
    return appids

_details_cache = {}
def steam_appdetails_batch(appids, cc="jp", lang="japanese"):
    ids = [int(a) for a in appids if str(a).isdigit() and int(a) > 0]
    ids = list(dict.fromkeys(ids))
    result, skipped = {}, []
    log(f"appdetails targets={len(ids)} mode=single")
    for aid in ids:
        if aid in _details_cache:
            result[aid] = _details_cache[aid]; continue
        try:
            params = {"appids": aid, "cc": cc, "l": lang}
            j = _get_with_retry("https://store.steampowered.com/api/appdetails",
                                params=params, kind="appdetails").json() or {}
            obj = j.get(str(aid))
            if not obj:
                skipped.append((aid, "no-key-in-json")); continue
            if not obj.get("success"):
                skipped.append((aid, "success:false (likely region/unavailable in JP)")); continue
            data = obj.get("data")
            if not data:
                skipped.append((aid, "no-data-field")); continue
            result[aid] = data
            _details_cache[aid] = data
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            skipped.append((aid, f"http-{code or 'err'}"))
        except Exception as e:
            skipped.append((aid, f"exception:{type(e).__name__}"))

    if skipped:
        head = ", ".join(f"{aid}:{reason}" for aid, reason in skipped[:5])
        more = f" (+{len(skipped)-5} more)" if len(skipped) > 5 else ""
        log(f"appdetails skipped {len(skipped)}: {head}{more}")
    log(f"appdetails collected {len(result)}/{len(ids)} (single)")
    return result

_reviews_cache = {}
def _fetch_jp_reviews(appid):
    if appid in _reviews_cache:
        return appid, _reviews_cache[appid]
    params = {"json": 1, "language": "japanese", "purchase_type": "all"}
    try:
        resp = _get_with_retry(
            f"https://store.steampowered.com/appreviews/{appid}",
            params=params, kind="appreviews"
        )
        try:
            js = resp.json() or {}
        except ValueError:
            js = {}
        q = js.get("query_summary", {}) or {}
        n = int(q.get("total_reviews", 0))
    except requests.RequestException:
        n = 0
    _reviews_cache[appid] = n
    return appid, n

def fetch_jp_reviews_parallel(appids):
    results = {}
    with ThreadPoolExecutor(max_workers=JP_REVIEW_WORKERS) as ex:
        futs = [ex.submit(_fetch_jp_reviews, aid) for aid in appids]
        for f in as_completed(futs):
            try:
                aid, n = f.result()
            except Exception:
                continue
            results[aid] = n
    return results

def fmt_yen(y):
    try:
        return f"{int(y):,}"
    except Exception:
        return str(y)

def compose_item_lines(entry):
    # 旧形式で使っていたが、現在は1ツイート1作品形式に変更済み
    exp = entry.get("expiry_jst")
    exp_s = exp.strftime("%m/%d %H:%M") if exp else "不明"
    return [
        f"🎮 {entry['name']}",
        f"🛒 ¥{fmt_yen(entry['initial'])} ➡️ ¥{fmt_yen(entry['final'])} （-{entry['off']}%）",
        f"⏳ 終了予定(JST): {exp_s}",
        f"🔗 https://store.steampowered.com/app/{entry['appid']}/",
    ]

# ====== ジャンル日本語マップ ======
GENRE_JA_MAP = {
    "Action": "アクション",
    "Adventure": "アドベンチャー",
    "RPG": "RPG",
    "Strategy": "ストラテジー",
    "Simulation": "シミュレーション",
    "Indie": "インディー",
    "Casual": "カジュアル",
    "Racing": "レース",
    "Sports": "スポーツ",
    "Survival": "サバイバル",
    "Roguelike": "ローグライク",
    "Roguelite": "ローグライク",
    "Horror": "ホラー",
    "Puzzle": "パズル",
    "Shooter": "シューティング",
    "FPS": "FPS",
    "TPS": "TPS",
    "Open World": "オープンワールド",
    "Sandbox": "サンドボックス",
    "Platformer": "プラットフォーマー",
    "Fighting": "格闘",
    "Visual Novel": "ビジュアルノベル",
    "Music": "音楽",
    "Turn-Based": "ターン制",
    "JRPG": "JRPG",
    "Tower Defense": "タワーディフェンス",
}

# ====== 評価%とラベルを /appreviews から取得 ======
def _calc_review_score_from_appreviews(appid, language="all"):
    """
    Steamの /appreviews から全体評価%とラベルを取得する。
    language="all" にしておくと全言語ベースの評価になる。
    """
    params = {
        "json": 1,
        "language": language,   # "all" or "japanese"
        "purchase_type": "all",
    }
    try:
        resp = _get_with_retry(
            f"https://store.steampowered.com/appreviews/{appid}",
            params=params,
            kind="appreviews"
        )
        js = resp.json() or {}
        q = js.get("query_summary", {}) or {}
        pos = int(q.get("total_positive", 0) or 0)
        neg = int(q.get("total_negative", 0) or 0)
        total = pos + neg
        if total <= 0:
            return 0, "評価情報なし"

        pct = int(round(pos / total * 100))

        if pct >= 90:
            label = "圧倒的に好評"
        elif pct >= 80:
            label = "非常に好評"
        elif pct >= 70:
            label = "好評"
        else:
            label = "賛否両論"

        return pct, label
    except Exception:
        return 0, "評価情報なし"

# ====== X: token & 投稿 ======
def _x_refresh_access_token():
    cid = (X_CLIENT_ID or "").strip()
    sec = (X_CLIENT_SECRET or "").strip()
    red = (X_REDIRECT_URI or "").strip()
    if not (cid and sec and red):
        raise RuntimeError("X OAuth2不足: X_CLIENT_ID / X_CLIENT_SECRET / X_REDIRECT_URI を設定してください。")

    rt = _load_refresh_token()
    url = "https://api.twitter.com/2/oauth2/token"
    form = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": cid,
        "redirect_uri": red,
    }
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    last = None
    for i in range(3):
        try:
            r = requests.post(url, data=form, headers=headers, timeout=30)
        except requests.RequestException as e:
            last = e; time.sleep(1.5 * (2 ** i)); continue

        if r.status_code == 200:
            js = r.json()
            access = js["access_token"]
            new_rt = js.get("refresh_token")
            if new_rt and new_rt != rt:
                _save_refresh_token(new_rt)
                if DEBUG:
                    log("[TOKEN] refresh_token rotated")
            return access

        if 500 <= r.status_code < 600:
            last = r; time.sleep(1.5 * (2 ** i)); continue

        raise RuntimeError(f"X token refresh失敗 (Basic) ({r.status_code}): {r.text[:300]}")

    if isinstance(last, requests.RequestException):
        raise RuntimeError(f"X token refresh失敗 (Basic): 接続エラー {last}")
    raise RuntimeError(f"X token refresh失敗 (Basic, 5xx継続): {getattr(last,'status_code','N/A')} {getattr(last,'text','')[:300]}")

def _x_create_tweet(text, bearer=None, reply_to=None, media_ids=None):
    if bearer is None:
        bearer = _x_refresh_access_token()
    url = "https://api.twitter.com/2/tweets"
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    payload = {"text": text}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    if media_ids:
        payload["media"] = {"media_ids": [str(m) for m in media_ids]}

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X投稿失敗 ({r.status_code}): {r.text[:400]}")
    return r.json()["data"]["id"]

# ====== 待機ユーティリティ ======
def _next_9am_jst(base_dt: datetime) -> datetime:
    """base_dt（JST）から見て『次に到来する 9:00 JST』を返す."""
    nine_today = base_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    if base_dt < nine_today:
        target = nine_today
    else:
        target = nine_today + timedelta(days=1)
    if DEFER_OFFSET_SEC:
        target += timedelta(seconds=max(0, DEFER_OFFSET_SEC))
    return target

def _sleep_until(target_dt: datetime):
    """target_dt(JST)まで段階的に待機（分刻み→秒刻みの順でログ出力）"""
    while True:
        now = datetime.now(JST)
        remain = (target_dt - now).total_seconds()
        if remain <= 0:
            break
        if remain > 180:
            chunk = 60
        elif remain > 30:
            chunk = 10
        else:
            chunk = 1
        log(f"[DEFER] 投稿まで {int(remain)} 秒")
        time.sleep(chunk)

# ====== 実行 ======
def main():
    t0 = time.time()
    t1 = t2 = t3 = t4 = t5 = None

    # ★ 起動時刻(JST) → 24時間後までの窓
    start = datetime.now(JST)
    end = start + timedelta(hours=24)

    # ★ このrunを識別するラベル（ツイート本文に入れて重複回避）
    run_label = start.strftime("%Y/%m/%d %H:%M")

    # 1) deals 取得
    deals = list_steam_deals_expiring_window(start, end)
    t1 = time.time()

    # 2) ITAD→appid
    steam_shop_id = get_steam_shop_id()
    itad_ids = [d.get("id") for d in deals if d.get("id")]
    itad_ids = list(dict.fromkeys(itad_ids))
    id2appid = map_itad_ids_to_appids(itad_ids, steam_shop_id)
    target_appids = [id2appid[d.get("id")] for d in deals if d.get("id") in id2appid]
    t2 = time.time()
    log(f"mapped_app={len(id2appid)} -> target_appids={len(target_appids)}")

    # 3) appdetails
    details_map = steam_appdetails_batch(target_appids, cc="jp", lang="japanese") if target_appids else {}
    if target_appids:
        t3 = time.time()

    # 4) 日本価格のある game のみ抽出
    prelim, seen = [], set()
    for appid in target_appids:
        if appid in seen:
            continue
        seen.add(appid)
        data = details_map.get(appid)
        if not data:
            continue
        if (data.get("type") or "").lower() != "game":
            continue
        po = data.get("price_overview") or {}
        is_free = bool(data.get("is_free", False))
        if is_free:
            initial = final = 0
            off = 0
        else:
            if not po:
                continue
            initial = (po.get("initial") or 0) // 100
            final   = (po.get("final")   or 0) // 100
            off     = po.get("discount_percent") or 0
        prelim.append({
            "appid": appid,
            "name": data.get("name", f"App {appid}"),
            "initial": initial,
            "final": final,
            "off": off,
            "expiry_jst": None,
        })

    # expiry 紐付け（JST）
    itad_expiry_map = {}
    for d in deals:
        expiry = (d.get("deal") or {}).get("expiry")
        if not expiry:
            continue
        try:
            itad_expiry_map[d["id"]] = dtparser.isoparse(expiry).astimezone(JST)
        except Exception:
            pass

    for d in prelim:
        for itad_id, appid in id2appid.items():
            if appid == d["appid"] and itad_id in itad_expiry_map:
                d["expiry_jst"] = itad_expiry_map[itad_id]
                break
    if target_appids:
        t4 = time.time()

    # 5) 日本語レビュー >= MIN_JP_REVIEWS
    appids_for_reviews = [p["appid"] for p in prelim]
    jp_map = fetch_jp_reviews_parallel(appids_for_reviews) if appids_for_reviews else {}
    rows = []
    for item in prelim:
        n = jp_map.get(item["appid"], 0)
        if n >= MIN_JP_REVIEWS:
            item["reviews_jp"] = n
            rows.append(item)

    def expiry_key(dt): return (0, dt.timestamp()) if dt else (1, float("inf"))

    # 並び替え: レビュー数, 割引率, 終了時刻, 価格, 名前
    rows.sort(
        key=lambda x: (
            -x.get("reviews_jp", 0),
            -x["off"],
            expiry_key(x["expiry_jst"]),
            x["final"],
            x["name"],
        )
    )
    if target_appids:
        t5 = time.time()

    # ===== ここからツイート用の追加情報を付与 =====

    # ★ TOP5件に制限
    TOP_N = 5
    rows = rows[:TOP_N]

    # レビュー評価%とラベル（/appreviews から取得）
    for item in rows:
        pct, label = _calc_review_score_from_appreviews(item["appid"], language="all")
        item["review_percent"] = pct
        item["review_label"] = label

    # ジャンル（日本語, 最大2つ）
    for item in rows:
        data = details_map.get(item["appid"], {}) or {}
        genres = data.get("genres") or []
        names_en = [g.get("description") for g in genres if g.get("description")]
        names_ja = []
        for g in names_en:
            if g in GENRE_JA_MAP:
                names_ja.append(GENRE_JA_MAP[g])
            else:
                names_ja.append(g)
        item["genres"] = " / ".join(names_ja[:2]) if names_ja else "ジャンル情報なし"

    # ITAD lowest から最安値判定
    itad_lowest_map = {}
    for d in deals:
        did = d.get("id")
        deal = d.get("deal") or {}
        low = deal.get("lowest") or {}
        price = low.get("price")
        if did and price is not None:
            try:
                itad_lowest_map[did] = int(round(float(price)))
            except Exception:
                continue

    for item in rows:
        item["lowest"] = None
        item["is_lowest"] = False
        for itad_id, appid in id2appid.items():
            if appid == item["appid"] and itad_id in itad_lowest_map:
                lowest = itad_lowest_map[itad_id]
                item["lowest"] = lowest
                if lowest and item["final"] == lowest:
                    item["is_lowest"] = True
                break

    # プロファイルログ
    profile_parts = []
    if t1 is not None: profile_parts.append(f"deals:{t1 - t0:.1f}s")
    if t2 is not None: profile_parts.append(f"map:{t2 - t1:.1f}s")
    if t3 is not None: profile_parts.append(f"appdetails:{t3 - t2:.1f}s")
    if t4 is not None: profile_parts.append(f"prelim+expiry:{t4 - t3:.1f}s" if t3 is not None else "prelim+expiry:NA")
    if t5 is not None: profile_parts.append(f"jp_reviews:{t5 - t4:.1f}s" if t4 is not None else "jp_reviews:NA")
    if profile_parts:
        log("PROFILE " + " ".join(profile_parts))

    # ===== ツイート本文＆リプテキスト構築 =====

    def build_main_tweet(entry):
        name = entry["name"]
        initial = entry["initial"]
        final = entry["final"]
        off = entry["off"]

        reviews_jp = entry.get("reviews_jp", 0)
        genre_text = entry.get("genres", "ジャンル情報なし")

        pct = entry.get("review_percent", 0)
        label = entry.get("review_label", "評価情報なし")

        lowest_text = "今回最安値です" if entry.get("is_lowest") else ""

        lines = [
            "【24時間以内にセール終了】",
            f"（{run_label} 時点）",
            name,
            f"価格: ¥{fmt_yen(initial)} → ¥{fmt_yen(final)}（-{off}%）",
            f"ジャンル: {genre_text}",
            f"評価: {label}（{pct}%）",
            f"日本語レビュー: {reviews_jp}件",
        ]

        if lowest_text:
            lines.append(lowest_text)

        lines.extend([
            "",
            "ストアページ案内はリプ欄から👇",
            HASHTAG,
        ])

        return "\n".join(lines)

    def build_reply_text(entry):
        url = f"https://store.steampowered.com/app/{entry['appid']}/"
        return f"ストアページ（{run_label} 時点）：\n{url}"

    # tweets = [{ "entry": dict, "main": str, "reply": Optional[str] }, ...]
    tweets = []

    if not rows:
        # 対象がない場合は1ツイートだけ出す
        lines = [
            "【24時間以内にセール終了】",
            f"（{run_label} 時点）",
            "",
        ]
        if not deals:
            lines.append("条件を満たすセールは見つかりませんでした。")
        else:
            lines.append("該当ディールはありましたが、Steam側のappid解決またはレビュー条件を満たしませんでした。")
        lines.append("")
        lines.append(HASHTAG)
        tweets.append({"entry": None, "main": "\n".join(lines), "reply": None})
    else:
        for entry in rows:
            tweets.append({
                "entry": entry,
                "main": build_main_tweet(entry),
                "reply": build_reply_text(entry),
            })

    # ===== 投稿待機ロジック =====
    # 実行時刻をベースに 5分間隔で投下
    if POST_TO_X:
        base_target = datetime.now(JST)
        log(f"[DEFER] ベース投稿ターゲット: {base_target.strftime('%m/%d %H:%M:%S')} JST")
    else:
        base_target = None

    # ===== 投稿（待機後に実施） =====
    if not POST_TO_X:
        # プレビュー出力
        for i, tw in enumerate(tweets, 1):
            print(f"--- Tweet {i} main ---")
            print(tw["main"])
            if tw["reply"]:
                print(f"--- Tweet {i} reply ---")
                print(tw["reply"])
        return

    try:
        print("[POST] Xへ投稿を開始します…")
        bearer = _x_refresh_access_token()  # ※投稿前にアクセストークン取得

        total = len(tweets)
        for idx, tw in enumerate(tweets, 1):
            # 実行時刻 + 5分*(idx-1) をターゲットにする
            scheduled = base_target + timedelta(minutes=5 * (idx - 1))
            log(f"[DEFER] Tweet {idx}/{total} 用ターゲット: {scheduled.strftime('%m/%d %H:%M:%S')} JST")
            _sleep_until(scheduled)

            # 本文ツイート
            main_id = _x_create_tweet(tw["main"], bearer=bearer)
            print(f"[POST] main {idx}/{total} 完了: tweet_id={main_id}, URL=https://x.com/i/web/status/{main_id}")

            # URLリプ（ある場合のみ）
            if tw["reply"]:
                reply_id = _x_create_tweet(tw["reply"], bearer=bearer, reply_to=main_id)
                print(f"[POST] reply {idx}/{total} 完了: tweet_id={reply_id}, URL=https://x.com/i/web/status/{reply_id}")

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
