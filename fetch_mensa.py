#!/usr/bin/env python3
"""JAPAN MENSA 入会テスト日程ページを監視して RSS (feed.xml) を生成する。"""
import datetime
import hashlib
import html
import json
import re
import sys
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

URL = "https://mensa.jp/exam/"
STATE = Path("state.json")
FEED = Path("feed.xml")
MAX_ITEMS = 50
JST = datetime.timezone(datetime.timedelta(hours=9))


def fetch() -> str:
    req = urllib.request.Request(
        URL, headers={"User-Agent": "Mozilla/5.0 (personal mensa-exam-rss)"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(page: str) -> dict:
    """ページから日程を抽出。key -> {region, datetime, place, note, status, link}"""
    # タグを消す前に「申し込む」リンク・「満員」画像・地方見出しを目印に置き換える
    page = re.sub(r'<a[^>]+href="[^"]*/exam/index/notice/id/(\d+)/?"[^>]*>',
                  r" [[OPEN:\1]] ", page)
    page = re.sub(r"<img[^>]+entry_quota[^>]*>", " [[FULL]] ", page)
    page = re.sub(
        r"<h3[^>]*>(.*?)</h3>",
        lambda m: "\n[[REGION:" + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "]]\n",
        page, flags=re.S | re.I)
    page = re.sub(r"<br\s*/?>|</(li|p|div|dd|dt|tr|td)>", "\n", page, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))

    token = re.compile(
        r"\[\[REGION:(?P<region>.*?)\]\]"
        r"|日時\s*[：:]\s*(?P<dt>.+?)\s+場所\s*[：:]\s*(?P<place>\S+)"
        r"(?:\s*(?P<note>このテストには[^\n]*))?"
        r"|\[\[OPEN:(?P<open>\d+)\]\]"
        r"|\[\[FULL\]\]"
    )
    slots, region, pending = {}, "", None
    for m in token.finditer(text):
        if m.group("region") is not None:
            region = m.group("region")
        elif m.group("dt"):
            key = f'{m.group("dt").strip()}|{m.group("place")}'
            slots[key] = {
                "region": region,
                "datetime": m.group("dt").strip(),
                "place": m.group("place"),
                "note": (m.group("note") or "").strip(),
                "status": "不明",
                "link": URL,
            }
            pending = key
        elif pending:
            if m.group("open"):
                slots[pending]["status"] = "受付中"
                slots[pending]["link"] = f'{URL}index/notice/id/{m.group("open")}/'
            else:
                slots[pending]["status"] = "満員"
            pending = None
    return slots


def make_item(title: str, s: dict, now: datetime.datetime) -> dict:
    desc = f'{s["region"]}｜{s["datetime"]}｜{s["place"]}｜{s["status"]}'
    if s["note"]:
        desc += f'<br>{s["note"]}'
    return {
        "title": title,
        "link": s["link"],
        "description": desc,
        "pubDate": format_datetime(now),
        "guid": hashlib.sha1((title + now.isoformat()).encode()).hexdigest(),
    }


def write_feed(items: list, now: datetime.datetime) -> None:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>JAPAN MENSA 入会テスト日程（非公式）</title>",
        f"<link>{URL}</link>",
        "<description>新しい日程の追加と空き状況の変化を通知します</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for it in items:
        parts.append(
            "<item>"
            f'<title>{escape(it["title"])}</title>'
            f'<link>{escape(it["link"])}</link>'
            f'<description>{escape(it["description"])}</description>'
            f'<pubDate>{it["pubDate"]}</pubDate>'
            f'<guid isPermaLink="false">{it["guid"]}</guid>'
            "</item>"
        )
    parts.append("</channel></rss>")
    FEED.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> int:
    now = datetime.datetime.now(JST)
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    old = state.get("slots", {})
    items = state.get("items", [])

    try:
        slots = parse(fetch())
    except Exception as e:  # 取得失敗時は何も変えない
        print(f"fetch/parse error: {e}", file=sys.stderr)
        return 0
    if not slots:
        print("日程が1件も取れなかったので状態を更新しません", file=sys.stderr)
        return 0

    new_items = []
    if not old:
        open_ = [s for s in slots.values() if s["status"] == "受付中"]
        summary = {
            "region": "", "datetime": now.strftime("%Y/%m/%d %H:%M"),
            "place": f"掲載{len(slots)}件", "status": f"受付中{len(open_)}件",
            "note": " / ".join(f'{s["datetime"]} {s["place"]}' for s in open_),
            "link": URL,
        }
        new_items.append(make_item("【監視開始】入会テスト日程の監視を始めました", summary, now))
    else:
        for key, s in slots.items():
            label = f'{s["region"]} {s["datetime"]} {s["place"]}'
            if key not in old:
                new_items.append(make_item(f"【新日程・{s['status']}】{label}", s, now))
            elif old[key]["status"] != s["status"]:
                tag = "空きあり" if s["status"] == "受付中" else s["status"]
                new_items.append(make_item(f"【{tag}】{label}", s, now))

    heartbeat = now.strftime("%Y-%m")  # 月1回はコミットしてActionsの自動停止を防ぐ
    changed = bool(new_items) or slots != old or state.get("heartbeat") != heartbeat
    if new_items:
        items = (new_items + items)[:MAX_ITEMS]
        write_feed(items, now)
    elif not FEED.exists():
        write_feed(items, now)
    if changed:
        STATE.write_text(json.dumps(
            {"slots": slots, "items": items, "heartbeat": heartbeat},
            ensure_ascii=False, indent=1), encoding="utf-8")
    for it in new_items:
        print(it["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
