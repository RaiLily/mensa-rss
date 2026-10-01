#!/usr/bin/env python3
"""JAPAN MENSA 入会テスト日程ページを監視して RSS (feed.xml) を生成する。"""
import datetime
import hashlib
import html
import json
import re
import sys
import urllib.parse
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
    """ページから日程を抽出。key -> {region, datetime, place, note, status, link}

    「日時」から次の「日時」（または次の見出し）までを1枠として切り出し、
    その中に「満員」画像があれば満員、リンク（申し込むボタン）があれば受付中と判定する。
    """
    def clean(fragment):
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()

    heads = [(m.start(), clean(m.group(1)))
             for m in re.finditer(r"<h3[^>]*>(.*?)</h3>", page, re.S | re.I)]
    starts = [m.start() for m in re.finditer(r"日時", page)]
    slots = {}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page)
        nxt = [h for h, _ in heads if start < h < end]
        end = min([end, start + 4000] + nxt)
        chunk = page[start:end]
        t = clean(chunk)
        m = re.search(r"日時 ?[：:] ?(.+?) 場所 ?[：:] ?(\S+)", t)
        if not m:
            continue
        region = ""
        for h, name in heads:
            if h < start:
                region = name
        note = re.search(r"(このテストには.*?(?:申し込めます|いただけます)。?)", t)
        status, link = "不明", URL
        if re.search(r"quota|満員", chunk):
            status = "満員"
        else:
            a = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", chunk, re.I)
            if a:
                status = "受付中"
                link = urllib.parse.urljoin(URL, html.unescape(a.group(1)))
        key = f"{m.group(1).strip()}|{m.group(2)}"
        slots[key] = {"region": region, "datetime": m.group(1).strip(),
                      "place": m.group(2), "note": note.group(1) if note else "",
                      "status": status, "link": link}
    counts = {}
    for v in slots.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"{len(slots)} 枠:", counts)
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
            elif old[key]["status"] != s["status"] and old[key]["status"] != "不明":
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
