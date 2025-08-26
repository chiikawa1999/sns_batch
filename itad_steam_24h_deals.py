# -*- coding: utf-8 -*-
"""
Steam: 実行日の朝9:00から翌朝9:00（JST）の24hで終了予定のセールを ITAD→Steam 連携で収集し、
【日本語レビュー10件以上】の作品のみを整形し、必要ならX(旧Twitter)へ1ツイート投稿します。
（対象はソフト単体 = Steam app のみ。JPストア基準）

準備:
  1) pip install -r requirements.txt
  2) ITAD_API_KEY, X_CLIENT_ID, X_CLIENT_SECRET, X_REDIRECT_URI を設定
  3) 初回のみ X_REFRESH_TOKEN を GitHub Secrets へ設定（ローカル運用なら itad_x_tokens.jsonでも可）
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

# ログ
DEBUG = True
def ts(): return datetime.now(JST).strftime("%H:%M:%S")
def log(msg): 
    if DEBUG: print(f"[{ts()}] {msg}")

# ====== バリデーション ======
if not ITAD_API_KEY:
    raise RuntimeError("ITAD_API_KEY が未設定です。")
if not (X_CLIENT_ID and X_CLIENT_SECRET and X_REDIRECT_URI):
    raise RuntimeError("X_CLIENT_ID / X_CLIENT_SECRET / X_REDIRECT_URI を設定してください。")

# ====== パス ======
def _base_dir():
    try: return pathlib.Path(__file__).resolve().parent
    except NameError: return pathlib.Path(os.getcwd())

def _token_path():
    p = pathlib.Path(TOKEN_FILE)
    if not p.is_absolute(): p = _base_dir() / p
    return p.resolve()

# ====== refresh_token 読み/書き ======
def _load_refresh_token():
    """
    refresh_token を取得する。
    優先順: 環境変数 X_REFRESH_TOKEN -> itad_x_tokens.json
    """
    env_rt = (os.getenv("X_REFRESH_TOKEN") or "").strip()
    if env_rt:
        if DEBUG: log("[TOKEN] Loaded refresh_token from ENV (X_REFRESH_TOKEN)")
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
        if DEBUG: log(f"[TOKEN] Loaded refresh_token from {path}")
        return rt
    except Exception as e:
        raise RuntimeError(
            f"refresh_token読み込み失敗: {e}\n"
            "JSONはコメント不可・ダブルクォートのみ・末尾カンマ無しで保存してください。"
        )

def _save_refresh_token(new_rt: str):
    """
    新しい refresh_token を itad_x_tokens.json に保存し、
    もし GHA_NEW_RT_PATH が設定されていればそのファイルにも書き出す（Actions側でSecrets更新に使う）。
    """
    if not new_rt:
        return

    # (1) ローカル保存（ローカル運用時）
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
            if DEBUG: log(f"[TOKEN] Saved refresh_token to {path}")
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    except Exception as e:
        if DEBUG: log(f"[TOKEN] local save skipped: {type(e).__name__}: {e}")

    # (2) GitHub Actions 向けの吐き出し
    if GHA_NEW_RT_PATH:
        try:
            pathlib.Path(GHA_NEW_RT_PATH).write_text(new_rt, encoding="utf-8")
            if DEBUG: log(f"[TOKEN] Emitted new RT to {GHA_NEW_RT_PATH}")
        except Exception as e:
            if DEBUG: log(f"[TOKEN] emit to GHA_NEW_RT_PATH failed: {type(e).__name__}: {e}")

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

        if r.status_code != 429:
            http_err = requests.HTTPError(f"{r.status_code} Error"); http_err.response = r
            raise http_err

        retry_after = getattr(r, "headers", {}).get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else base_wait * (2 ** i)
        except Exception:
            wait = base_wait * (2 ** i)
        extra_backoff = min((STEAM_429_SLEEP_BASE * (i + 1)), STEAM_429_SLEEP_CAP)
        time.sleep(wait + random.uniform(0.3, 0.9) + extra_backoff)
        with _throttle_lock:
            _last_steam_ts[kind] = 0.0

    time.sleep(max(20.0, extra_backoff))
    _throttle_steam(kind)
    r = _steam_session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r

# ====== ITAD呼び出しと処理（あなたのベースどおり） ======
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
    return 61

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
                lst = [d for d in (data.get("list") or []) if (d.get("type") or "").lower() == "game"]

                page_in, page_out = 0, 0
                for d in lst:
                    expiry = (d.get("deal") or {}).get("expiry")
                    if not expiry: continue
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
                    used_sort = sort_key; break

                if not data.get("hasMore"):
                    used_sort = sort_key; break
                offset = data.get("nextOffset", 0)
                time.sleep(ITAD_SLEEP_SEC)
            if used_sort: break
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
            if not ids: continue
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
            if not obj: skipped.append((aid, "no-key-in-json")); continue
            if not obj.get("success"):
                skipped.append((aid, "success:false (likely region/unavailable in JP)")); continue
            data = obj.get("data")
            if not data: skipped.append((aid, "no-data-field")); continue
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
    if appid in _reviews_cache: return appid, _reviews_cache[appid]
    params = {"json": 1, "language": "japanese", "purchase_type": "all"}
    q = _get_with_retry(f"https://store.steampowered.com/appreviews/{appid}",
                        params=params, kind="appreviews").json().get("query_summary", {}) or {}
    n = int(q.get("total_reviews", 0))
    _reviews_cache[appid] = n
    return appid, n

def fetch_jp_reviews_parallel(appids):
    results = {}
    with ThreadPoolExecutor(max_workers=JP_REVIEW_WORKERS) as ex:
        futs = [ex.submit(_fetch_jp_reviews, aid) for aid in appids]
        for f in as_completed(futs):
            aid, n = f.result(); results[aid] = n
    return results

def fmt_yen(y):
    try: return f"{int(y):,}"
    except Exception: return str(y)

def compose_item_lines(entry):
    exp = entry.get("expiry_jst")
    exp_s = exp.strftime("%m/%d %H:%M") if exp else "不明"
    return [
        f"🎮 {entry['name']}",
        f"🛒 ¥{fmt_yen(entry['initial'])} ➡️ ¥{fmt_yen(entry['final'])} （-{entry['off']}%）",
        f"⏳ 終了予定(JST): {exp_s}",
        f"🔗 https://store.steampowered.com/app/{entry['appid']}/",
    ]

# ====== X: refresh_token -> access_token（Confidential/Basic） & 投稿 ======
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
    for i in range(3):  # 5xx リトライ
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
                if DEBUG: log("[TOKEN] refresh_token rotated")
            return access

        if 500 <= r.status_code < 600:
            last = r; time.sleep(1.5 * (2 ** i)); continue

        raise RuntimeError(f"X token refresh失敗 (Basic) ({r.status_code}): {r.text[:300]}")

    if isinstance(last, requests.RequestException):
        raise RuntimeError(f"X token refresh失敗 (Basic): 接続エラー {last}")
    raise RuntimeError(f"X token refresh失敗 (Basic, 5xx継続): {getattr(last,'status_code','N/A')} {getattr(last,'text','')[:300]}")

def _x_create_tweet(text, bearer=None):
    if bearer is None:
        bearer = _x_refresh_access_token()
    url = "https://api.twitter.com/2/tweets"
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json={"text": text}, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X投稿失敗 ({r.status_code}): {r.text[:400]}")
    return r.json()["data"]["id"]

# ====== 実行 ======
def main():
    t0 = time.time()

    # 実行日 9:00 → 翌日 9:00（JST）
    today = datetime.now(JST).date()
    start = datetime(today.year, today.month, today.day, 9, 0, 0, tzinfo=JST)
    end   = start + timedelta(days=1)

    head1 = "⏰ 本日終了のSteamセールまとめ"
    head2 = f"（{start.strftime('%m/%d %H:%M')} → {end.strftime('%m/%d %H:%M')} JST）"

    # 1) deals
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

    # 4) 日本価格のある game のみ
    prelim, seen = [], set()
    for appid in target_appids:
        if appid in seen: continue
        seen.add(appid)
        data = details_map.get(appid)
        if not data: continue
        if (data.get("type") or "").lower() != "game": continue
        po = data.get("price_overview") or {}
        is_free = bool(data.get("is_free", False))
        if is_free:
            initial = final = 0; off = 0
        else:
            if not po: continue
            initial = (po.get("initial") or 0) // 100
            final   = (po.get("final")   or 0) // 100
            off     = po.get("discount_percent") or 0
        prelim.append({"appid": appid, "name": data.get("name", f"App {appid}"),
                       "initial": initial, "final": final, "off": off, "expiry_jst": None})

    # expiry 紐付け（JST）
    itad_expiry_map = {}
    for d in deals:
        expiry = (d.get("deal") or {}).get("expiry")
        if not expiry: continue
        try:
            itad_expiry_map[d["id"]] = dtparser.isoparse(expiry).astimezone(JST)
        except Exception:
            pass
    for d in prelim:
        for itad_id, appid in id2appid.items():
            if appid == d["appid"] and itad_id in itad_expiry_map:
                d["expiry_jst"] = itad_expiry_map[itad_id]; break
    t4 = time.time()

    # 5) 日本語レビュー >= 10
    appids_for_reviews = [p["appid"] for p in prelim]
    jp_map = fetch_jp_reviews_parallel(appids_for_reviews)
    rows = []
    for item in prelim:
        n = jp_map.get(item["appid"], 0)
        if n >= MIN_JP_REVIEWS:
            item["reviews_jp"] = n
            rows.append(item)

    def expiry_key(dt): return (0, dt.timestamp()) if dt else (1, float("inf"))
    rows.sort(key=lambda x: (-x.get("reviews_jp", 0), -x["off"], expiry_key(x["expiry_jst"]), x["final"], x["name"]))
    t5 = time.time()

    log(f"PROFILE deals:{t1-t0:.1f}s map:{t2-t1:.1f}s appdetails:{t3-t2:.1f}s prelim+expiry:{t4-t3:.1f}s jp_reviews:{t5-t4:.1f}s")

    # 6) 1ツイート整形
    lines = [head1, head2, ""]
    if not rows:
        if not deals:
            lines.append("（条件を満たすセールは見つかりませんでした）")
        else:
            lines.append("該当ディールはありましたが、Steam側のappid解決に失敗しました。")
    else:
        for r in rows:
            lines.extend(compose_item_lines(r))
            lines.append("")
    lines.append(HASHTAG)
    text = "\n".join(lines)

    if not POST_TO_X:
        print(text); return

    try:
        print("[POST] Xへ投稿を開始します…")
        tid = _x_create_tweet(text)
        print(f"[POST] 完了: tweet_id={tid}, URL=https://x.com/i/web/status/{tid}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
