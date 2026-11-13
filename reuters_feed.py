#!/usr/bin/env python3
"""
Reuters Google News RSS → Direct URL RSS
Decodes Google News redirect URLs, tracks seen GUIDs, outputs a
500-item Inoreader-friendly RSS 2.0 feed.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from googlenewsdecoder import gnewsdecoder  # >=0.2.2 (lexbor backend)

# ── Config ────────────────────────────────────────────────────────────────────
FEED_URL     = "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen"
STATE_FILE   = "state/seen_guids.json"
OUTPUT_FILE  = "feed/reuters_direct.xml"
MAX_ITEMS    = 500
MAX_SEEN     = 2000          # trim seen list beyond this to avoid unbounded growth
DECODE_DELAY = 0.3           # seconds between decode attempts

FEED_TITLE = "Reuters (Direct)"
FEED_DESC  = "Reuters news with direct article URLs, decoded from Google News"
FEED_LINK  = "https://www.reuters.com"


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_rfc822() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


# ── State ─────────────────────────────────────────────────────────────────────
def load_seen(path: str) -> list:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else []


def save_seen(path: str, seen: list):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # keep only the most recent MAX_SEEN GUIDs
    Path(path).write_text(json.dumps(seen[-MAX_SEEN:]))


# ── URL Decoding ──────────────────────────────────────────────────────────────
def decode_url(google_url: str) -> str | None:
    """
    Decode a Google News article URL to the publisher URL.
    Returns None on failure (caller skips the item and retries next run).
    Note: the decoder makes network calls (article page fetch + batchexecute POST).
    """
    try:
        result = gnewsdecoder(google_url, interval=0, timeout=15)
        if isinstance(result, dict) and result.get("success"):
            url = result.get("decoded_url", "")
            if url.startswith("http") and "google.com" not in url:
                return url
        else:
            print(f"    [decoder: {(result or {}).get('message', 'no result')}]")
    except Exception as e:
        print(f"    [decoder failed: {type(e).__name__}: {e}]")
    return None


# ── XML I/O ───────────────────────────────────────────────────────────────────
def load_existing(path: str) -> list:
    if not Path(path).exists():
        return []
    try:
        root = ET.parse(path).getroot()
        ch = root.find("channel")
        if ch is None:
            return []
        return [
            {
                "title":   it.findtext("title", ""),
                "link":    it.findtext("link", ""),
                "desc":    it.findtext("description", ""),
                "pubDate": it.findtext("pubDate", ""),
            }
            for it in ch.findall("item")
        ]
    except Exception as e:
        print(f"[load_existing failed: {e}]")
        return []


def write_rss(items: list, path: str):
    rss = ET.Element("rss", attrib={"version": "2.0"})
    ch  = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text       = FEED_TITLE
    ET.SubElement(ch, "link").text        = FEED_LINK
    ET.SubElement(ch, "description").text = FEED_DESC
    ET.SubElement(ch, "language").text    = "en-US"
    ET.SubElement(ch, "lastBuildDate").text = now_rfc822()

    for d in items:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text       = d["title"]
        ET.SubElement(it, "link").text        = d["link"]
        ET.SubElement(it, "description").text = d["desc"]
        ET.SubElement(it, "pubDate").text     = d["pubDate"]
        g = ET.SubElement(it, "guid")
        g.set("isPermaLink", "true")
        g.text = d["link"]

    ET.indent(rss, space="  ")   # requires Python ≥ 3.9
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode"),
        encoding="utf-8",
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    seen_list = load_seen(STATE_FILE)
    seen_set  = set(seen_list)
    existing  = load_existing(OUTPUT_FILE)

    print(f"Known GUIDs : {len(seen_set)}")
    print(f"Existing    : {len(existing)} items")

    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        print(f"Feed fetch failed: {feed.bozo_exception}")
        return

    entries = feed.entries
    print(f"Feed entries: {len(entries)}")

    new_items = []
    skipped = 0
    for entry in entries:
        guid = entry.get("id", "")
        if not guid or guid in seen_set:
            continue

        title   = entry.get("title", "No title")
        raw_url = entry.get("link", "")
        desc    = strip_html(entry.get("summary", title))
        pub     = entry.get("published", now_rfc822())

        print(f"  → {title[:75]}")
        direct_url = decode_url(raw_url)
        time.sleep(DECODE_DELAY)
        if not direct_url:
            skipped += 1      # not marked seen → retried next run
            continue

        new_items.append({"title": title, "link": direct_url, "desc": desc, "pubDate": pub})
        seen_list.append(guid)
        seen_set.add(guid)

    print(f"New items   : {len(new_items)} (skipped undecodable: {skipped})")

    all_items = (new_items + existing)[:MAX_ITEMS]
    write_rss(all_items, OUTPUT_FILE)
    print(f"Output      : {len(all_items)} items → {OUTPUT_FILE}")

    save_seen(STATE_FILE, seen_list)
    print("State saved.")


if __name__ == "__main__":
    main()
