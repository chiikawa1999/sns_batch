# -*- coding: utf-8 -*-
"""
未発売タイトル（coming_soon）のうち、Steam公式検索の「Top Wishlists」順で上位N件を取得し、
ジャンル（genres）と発売元（publishers）を併記して1ツイートにまとめて投稿（またはプレビュー出力）します。

- ランキング取得（近似）:
    https://store.steampowered.com/search/results/?infinite=1&filter=popularwishlist
- 未発売判定:
    appdetails.release_date.coming_soon == True
- 日本向け:
    cc=jp / l=japanese

必要な環境変数:
  X_CLIENT_ID, X_CLIENT_SECRET, X_REDIRECT_URI, （初回のみ）X_REFRESH_TOKEN

依存:
  pip install requests python-dateutil
"""

import os
import sys
import re
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===== 基本設定 =====
COUNTRY = "JP"
JST = timezone(timedelta(hours=9))
POST_TO_X = True  # Falseで投稿せず標準出力

TOP_N = 20                 # 取得ランキング上位N件
SEARCH_PAGE_COUNT = 60     # 無限スクロール1ページあたり件数
SEARCH_PAGES = 6           # 最大ページ数（60*6=360件分を候補に）

STEAM_MIN_INTERVAL = {"appdetails": 1.0, "search": 1.0}
DEBUG = True

# X OAuth2 Confidential Client 情報
X_CLIENT_ID = os.getenv("X_CLIENT_ID") or "YOUR_X_CLIENT_ID"
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET") or "YOUR_X_CLIENT_SECRET"
X_REDIRECT_URI = os.getenv("X_REDIRECT_URI") or "http://localhost/callback"
TOKEN_FILE = "itad_x_tokens.json"             # 既存運用のトークンファイルを流用
GHA_NEW_RT_PATH = os.getenv("GHA_NEW_RT_PATH")  # 例: new_refresh_token.txt

# ===== ユーティリティ =====
_last_hit = {"appdetails": 0.0, "search": 0.0}
def ts(): return datetime.now(JST).strftime("%H:%M:%S")
def log(msg: str): print(f"[{ts()}] {msg}")

def _throttle(kind: str):
    gap = STEAM_MIN_INTERVAL.get(kind, 0)
    now = time.time()
    wait = max(0.0, _last_hit.get(kind, 0) + gap - now)
    if wait > 0: time.sleep(wait)
    _last_hit[kind] = time.time()

def _requests_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, backoff_factor=1.2, status_forcelist=(429, 500, 502, 503, 504)
    )))
    return s

def _get_with_retry(url, params=None, kind="search", timeout=30):
    _throttle(kind)
    s = _requests_session()
    r = s.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r

# ===== Steam: Top Wishlists 無限スクロール =====
def fetch_popular_wishlist_appids(max_pages=SEARCH_PAGES, page_count=SEARCH_PAGE_COUNT, cc="jp", lang="japanese"):
    appids, seen, total_hint = [], set(), None
    for i in range(max_pages):
        start = i * page_count
        params = {
            "start": start, "count": page_count,
            "filter": "popularwishlist", "cc": cc, "l": lang, "infinite": 1,
        }
        r = _get_with_retry("https://store.steampowered.com/search/results/", params=params, kind="search")
        js = r.json()
        html = js.get("results_html", "")
        total_hint = js.get("total_count", total_hint)
        # data-ds-appid="12345" を抽出
        for m in re.finditer(r'data-ds-appid="(\d+)"', html):
            aid = int(m.group(1))
            if aid not in seen:
                seen.add(aid); appids.append(aid)
        log(f"wishlist page {i+1}: collected={len(appids)} (total~{total_hint})")
        if len(appids) >= TOP_N * 4:  # 未発売で間引く分に余裕
            break
    return appids

# ===== Steam: appdetails =====
_details_cache = {}
def steam_appdetails_batch(appids, cc="jp", lang="japanese"):
    result, skipped = {}, []
    log(f"appdetails targets={len(appids)} mode=single")
    for aid in appids:
        if aid in _details_cache:
            result[aid] = _details_cache[aid]; continue
        try:
            params = {"appids": aid, "cc": cc, "l": lang}
            j = _get_with_retry("https://store.steampowered.com/api/appdetails",
                                params=params, kind="appdetails").json() or {}
            obj = j.get(str(aid)) or {}
            if not obj.get("success"):
                skipped.append((aid, "success:false")); continue
            data = obj.get("data") or {}
            _details_cache[aid] = data
            result[aid] = data
        except Exception as e:
            skipped.append((aid, f"error:{type(e).__name__}"))
    if skipped:
        for aid, why in skipped[:8]:
            log(f"appdetails skipped {aid}: {why}")
        if len(skipped) > 8:
            log(f"appdetails skipped more {len(skipped)-8}...")
    return result

# ===== 整形 =====
def fmt_date_jp(date_str: str) -> str:
    # 例: "27 Aug, 2025" / "TBA" / "Q4 2025" など
    return date_str or "TBA"

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n-1)] + "…"

# ===== X投稿 =====
def _token_path(): return os.path.join(os.getcwd(), TOKEN_FILE)

def _load_refresh_token():
    p = _token_path()
    if os.path.exists(p):
        try: return json.load(open(p,"r",encoding="utf-8")).get("refresh_token")
        except Exception: pass
    return os.getenv("X_REFRESH_TOKEN") or ""

def _save_refresh_token(new_rt: str):
    if not new_rt: return
    if GHA_NEW_RT_PATH:
        try:
            with open(GHA_NEW_RT_PATH,"w",encoding="utf-8") as f: f.write(new_rt)
        except Exception:
            pass
    try:
        with open(_token_path(),"w",encoding="utf-8") as f:
            json.dump({"refresh_token": new_rt}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _x_refresh_access_token():
    cid = (X_CLIENT_ID or "").strip()
    sec = (X_CLIENT_SECRET or "").strip()
    red = (X_REDIRECT_URI or "").strip()
    if not (cid and sec and red):
        raise RuntimeError("X CLIENT情報が不足しています")

    rt = _load_refresh_token()
    if not rt: raise RuntimeError("X refresh_token がありません")

    url = "https://api.twitter.com/2/oauth2/token"
    form = {"grant_type":"refresh_token","refresh_token":rt,"client_id":cid,"redirect_uri":red}
    s = _requests_session()
    s.headers.update({"Content-Type":"application/x-www-form-urlencoded"})
    r = s.post(url, data=form, auth=(cid, sec), timeout=30)
    if r.status_code == 200:
        js = r.json()
        access = js["access_token"]
        new_rt = js.get("refresh_token")
        if new_rt and new_rt != rt:
            _save_refresh_token(new_rt)
            if DEBUG: log("[TOKEN] refresh_token rotated")
        return access
    raise RuntimeError(f"X token refresh失敗 ({r.status_code}): {r.text[:200]}")

def _x_create_tweet(text, bearer=None):
    if bearer is None: bearer = _x_refresh_access_token()
    url = "https://api.twitter.com/2/tweets"
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type":"application/json"}
    r = requests.post(url, headers=headers, json={"text": text}, timeout=30)
    if r.status_code == 201:
        return r.json().get("data",{}).get("id")
    raise RuntimeError(f"X tweet失敗 {r.status_code}: {r.text[:200]}")

# ===== メイン =====
def main():
    today = datetime.now(JST).date()

    # 1) 人気ウィッシュ順の候補AppIDを取得
    candidates = fetch_popular_wishlist_appids()
    if not candidates:
        print("[ERROR] 候補が取得できませんでした", file=sys.stderr); sys.exit(1)

    # 2) appdetailsで未発売（coming_soon）のみ抽出 + ジャンル/発売元
    details = steam_appdetails_batch(candidates, cc="jp", lang="japanese")
    rank_index = {aid: idx for idx, aid in enumerate(candidates)}  # 並び順保持
    prelim = []
    for aid, d in details.items():
        rd = d.get("release_date") or {}
        if not rd.get("coming_soon"):
            continue  # 未発売のみ
        name = d.get("name") or f"App {aid}"
        release_str = fmt_date_jp(rd.get("date") or "TBA")
        genres = [g.get("description") for g in (d.get("genres") or []) if g.get("description")]
        devs = [p for p in (d.get("developers") or []) if p]   # ★ publishers→developers に変更
        prelim.append({
            "appid": aid,
            "name": name,
            "release_str": release_str,
            "genres": genres,
            "developers": devs,   # ★ キー名も developers に
            "rank": rank_index.get(aid, 10**9),
        })

    if not prelim:
        print("[INFO] 未発売タイトルが見つかりませんでした"); return

    # 3) 人気順でソート → 上位N
    rows = sorted(prelim, key=lambda x: x["rank"])[:TOP_N]

    # 4) ツイート本文作成
    head1 = f"🔜 未発売 × ウィッシュリスト上位 Top{TOP_N}"
    head2 = f"（{today.strftime('%m/%d')} 現在 / JST）"
    lines = [head1, head2, ""]

    medals = ["🥇", "🥈", "🥉"]

    for i, e in enumerate(rows, 1):
        if i <= 3:
            title_line = f"{medals[i-1]} 🎮 {e['name']}"
        else:
            title_line = f"🎮 {e['name']}"
        lines.append(title_line)
        lines.append(f"🗓 発売予定: {e.get('release_str') or 'TBA'}")
        genres_txt = ", ".join(e.get("genres", [])[:3]) if e.get("genres") else "不明"
        devs_txt = ", ".join(e.get("developers", [])[:2]) if e.get("developers") else "不明"  # ★ developers 表示
        lines.append(f"🏷 ジャンル: {genres_txt}")
        lines.append(f"👨‍💻 開発元: {devs_txt}")  # ★ ラベルも「開発元」に変更
        lines.append(f"🔗 https://store.steampowered.com/app/{e['appid']}/")
        lines.append("")

    lines.append("#Steam")
    lines.append("#ウィッシュリスト")
    text = "\n".join(lines).rstrip()
    
    # 5) 投稿 or プレビュー
    if not POST_TO_X:
        print(text); return
    try:
        log("[POST] Xへ投稿を開始します…")
        tid = _x_create_tweet(text)
        log(f"[POST] 完了: tweet_id={tid}, URL=https://x.com/i/web/status/{tid}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()



