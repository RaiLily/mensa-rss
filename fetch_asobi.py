#!/usr/bin/env python3
"""ASOBI TICKET の一覧ページをヘッドレスブラウザで開き、新しい項目を RSS (asobi-feed.xml) にする。

JavaScript で描画されるページなので Playwright(Chromium) を使う。
基本は「/booths/ を含むリンク」を1件の項目として扱い、
見つからない場合はページ本文の行単位で差分を取る。
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
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", viewport={"width": 1280, "height": 2000})
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(3000)
        for _ in range(6):  # 遅延読み込み対策でスクロール
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))")
        body = page.inner_text("body")
        final_url = page.url
        browser.close()
    return links, body, final_url


def norm(text):
    return re.sub(r"\s+", " ", text).strip()


def extract(links, body):
    """key -> 表示テキスト。モード名も返す。"""
    host = urlparse(URL).netloc
    items = {}
    for a in links:
        u = urlparse(a["href"])
        path = u.path.rstrip("/")
        if u.netloc == host and re.search(r"/booths/[^/]+", path) and norm(a["text"]):
            key = f"{u.netloc}{path}"
            items[key] = (items.get(key, "") + " " + norm(a["text"])).strip()[:200]
    if items:
        return items, "links"
    lines = {}
    for line in body.splitlines():
        line = norm(line)
        if 4 <= len(line) <= 150:
            lines[line] = line
    return lines, "text"


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
    def add(title, key, text):
        link = f"https://{key}" if mode == "links" else URL
        new_items.append({
            "title": title, "link": link, "description": text,
            "pubDate": format_datetime(now),
            "guid": hashlib.sha1((title + key + now.isoformat()).encode()).hexdigest(),
        })
    if not old:
        add(f"【監視開始】一覧の {len(cur)} 件を記録しました", "", f"モード: {mode}")
        new_items[-1]["link"] = URL
        return new_items
    for key, text in cur.items():
        if key not in old:
            add(f"【新着】{text[:80]}", key, text)
        elif NOTIFY_CHANGES and mode == "links" and old[key] != text:
            add(f"【更新】{text[:80]}", key, f"変更前: {old[key]}\n変更後: {text}")
    return new_items


def main():
    now = datetime.datetime.now(JST)
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    try:
        links, body, final_url = render()
    except Exception as e:
        print(f"render error: {e}", file=sys.stderr)
        return 0
    if "login" in final_url.lower():
        print(f"ログインページに転送されました: {final_url}", file=sys.stderr)
        return 0
    cur, mode = extract(links, body)
    print(f"mode={mode}, {len(cur)} 件")
    for k, v in list(cur.items())[:15]:
        print("  ", k, "|", v[:80])
    if not cur:
        print("項目が取れなかったので状態を更新しません", file=sys.stderr)
        return 0

    old = state.get("items_seen", {}) if state.get("mode") == mode else {}
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

