#!/usr/bin/env python3
"""ASOBI TICKET の一覧ページをヘッドレスブラウザで開き、新しい項目を RSS (asobi-feed.xml) にする。

JavaScript で描画されるページなので Playwright(Chromium) を使う。
ページが裏で読み込むJSON → /booths/ リンク → 画像付きカード → 本文の行
の順に、取れた方式でイベント一覧を作る。
"""
import datetime
import hashlib
import json
import re
import sys
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

URL = "https://asobiticket2.asobistore.jp/booths"
STATE = Path("asobi-state.json")
FEED = Path("asobi-feed.xml")
MAX_ITEMS = 50
NOTIFY_CHANGES = True  # 既存項目の表示内容（受付中→終了など）が変わったときも通知する
JST = datetime.timezone(datetime.timedelta(hours=9))


def render():
    from playwright.sync_api import sync_playwright
    jsons = []

    def on_response(res):
        try:
            if "json" in (res.headers.get("content-type") or ""):
                jsons.append((res.url, res.json()))
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", viewport={"width": 1280, "height": 2000})
        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(3000)
        for _ in range(6):  # 遅延読み込み対策でスクロール
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))")
        # 画像付きカードのタイトル（画像から親をたどって最初に文字が出てくる要素）
        cards = page.eval_on_selector_all("img", """imgs => imgs.map(img => {
            let el = img.parentElement;
            for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                const t = (el.innerText || '').trim();
                if (t) return t.length <= 200 ? t : '';
            }
            return '';
        }).filter(t => t)""")
        body = page.inner_text("body")
        final_url = page.url
        browser.close()
    return links, body, final_url, jsons, cards


def norm(text):
    return re.sub(r"\s+", " ", text).strip()


TITLE_KEYS = re.compile(r"^(title|name|.*_?title|.*_?name)$", re.I)


def dict_lists(obj, out):
    if isinstance(obj, list):
        if len(obj) >= 2 and all(isinstance(x, dict) for x in obj):
            out.append(obj)
        for x in obj:
            dict_lists(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            dict_lists(v, out)
    return out


def from_api(jsons, body):
    """ページが裏で読み込んでいるJSONから、本文に表示されているタイトルの一覧を探す。"""
    best, best_hits, best_url = None, 0, ""
    for url, data in jsons:
        for lst in dict_lists(data, []):
            title_key = next((k for k in lst[0] if TITLE_KEYS.match(k)
                              and isinstance(lst[0][k], str)), None)
            if not title_key:
                continue
            hits = sum(1 for x in lst if isinstance(x.get(title_key), str)
                       and x[title_key].strip() and norm(x[title_key])[:20] in norm(body))
            if hits >= 2 and hits >= len(lst) * 0.5 and hits > best_hits:
                best, best_hits, best_url = (lst, title_key), hits, url
    if not best:
        return {}
    lst, title_key = best
    print(f"API: {best_url}  title_key={title_key}  keys={list(lst[0].keys())[:20]}")
    items = {}
    for x in lst:
        title = norm(str(x.get(title_key) or ""))
        if not title:
            continue
        ident = next((str(x[k]) for k in ("id", "code", "slug", "key", "uuid") if x.get(k)), title)
        link = next((v for k, v in x.items() if isinstance(v, str)
                     and v.startswith("http") and "url" in k.lower()
                     and not re.search(r"\.(png|jpe?g|webp|gif)", v, re.I)), URL)
        status = " ".join(norm(str(x[k])) for k in x
                          if re.search(r"status|state|label", k, re.I)
                          and isinstance(x[k], (str, int)) and str(x[k]).strip())
        items[f"api:{ident}"] = {"text": (title + (f"（{status}）" if status else ""))[:200],
                                 "link": link}
    return items


def extract(links, body, jsons=(), cards=()):
    """key -> {text, link}。モード名も返す。"""
    items = from_api(jsons, body)
    if items:
        return items, "api"
    host = urlparse(URL).netloc
    for a in links:
        u = urlparse(a["href"])
        path = u.path.rstrip("/")
        if u.netloc == host and re.search(r"/booths/[^/]+", path) and norm(a["text"]):
            key = f"{u.netloc}{path}"
            text = (items.get(key, {}).get("text", "") + " " + norm(a["text"])).strip()[:200]
            items[key] = {"text": text, "link": f"https://{key}"}
    if items:
        return items, "links"
    for t in cards:
        t = norm(t)
        if 4 <= len(t) <= 200:
            items[t] = {"text": t, "link": URL}
    if len(items) >= 2:
        return items, "cards"
    items = {}
    for line in body.splitlines():
        line = norm(line)
        if 4 <= len(line) <= 150:
            items[line] = {"text": line, "link": URL}
    return items, "text"


def write_feed(entries, now):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>ASOBI TICKET 受付一覧（非公式）</title>",
        f"<link>{URL}</link>",
        "<description>ASOBI TICKET の一覧ページに追加・変更された項目</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for it in entries:
        parts.append(
            "<item>"
            f'<title>{escape(it["title"])}</title>'
            f'<link>{escape(it["link"])}</link>'
            f'<description>{escape(it["description"])}</description>'
            f'<pubDate>{it["pubDate"]}</pubDate>'
            f'<guid isPermaLink="false">{it["guid"]}</guid>'
            "</item>")
    parts.append("</channel></rss>")
    FEED.write_text("\n".join(parts) + "\n", encoding="utf-8")


def diff(old, cur, mode, now):
    new_items = []

    def add(title, link, desc, key=""):
        new_items.append({
            "title": title, "link": link, "description": desc,
            "pubDate": format_datetime(now),
            "guid": hashlib.sha1((title + key + now.isoformat()).encode()).hexdigest(),
        })
    if not old:
        names = " / ".join(v["text"] for v in list(cur.values())[:30])
        add(f"【監視開始】一覧の {len(cur)} 件を記録しました", URL, f"モード: {mode}\n{names}")
        return new_items
    for key, v in cur.items():
        if key not in old:
            add(f"【新着】{v['text'][:80]}", v["link"], v["text"], key)
        elif NOTIFY_CHANGES and mode in ("api", "links") and old[key]["text"] != v["text"]:
            add(f"【更新】{v['text'][:80]}", v["link"],
                f"変更前: {old[key]['text']}\n変更後: {v['text']}", key)
    return new_items


def main():
    now = datetime.datetime.now(JST)
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    try:
        links, body, final_url, jsons, cards = render()
    except Exception as e:
        print(f"render error: {e}", file=sys.stderr)
        return 0
    if "login" in final_url.lower():
        print(f"ログインページに転送されました: {final_url}", file=sys.stderr)
        return 0
    cur, mode = extract(links, body, jsons, cards)
    print(f"mode={mode}, {len(cur)} 件")
    for k, v in list(cur.items())[:15]:
        print("  ", v["text"][:80], "|", v["link"])
    if not cur:
        print("項目が取れなかったので状態を更新しません", file=sys.stderr)
        return 0

    old = state.get("items_seen", {}) if state.get("mode") == mode else {}
    if old and not isinstance(next(iter(old.values())), dict):
        old = {}
    new_items = diff(old, cur, mode, now)
    entries = state.get("entries", [])
    heartbeat = now.strftime("%Y-%m")
    if new_items:
        entries = (new_items + entries)[:MAX_ITEMS]
        write_feed(entries, now)
    elif not FEED.exists():
        write_feed(entries, now)
    if new_items or cur != state.get("items_seen") or state.get("heartbeat") != heartbeat:
        STATE.write_text(json.dumps(
            {"mode": mode, "items_seen": cur, "entries": entries, "heartbeat": heartbeat},
            ensure_ascii=False, indent=1), encoding="utf-8")
    for it in new_items:
        print(it["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
