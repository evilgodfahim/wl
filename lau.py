#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resilient APNews scraper with FlareSolverr + requests fallback.
Scrapes multiple AP News sections and merges into a single RSS feed.
"""

from __future__ import annotations
import sys
import os
import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ------------------------------
# CONFIG
# ------------------------------
DEBUG = True
LOG_FILENAME = "debug.log"
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
APNEWS_BASE = "https://apnews.com"

# ── All sections to scrape ────────────────────────────────────────────────────
SOURCES = [
    "https://apnews.com/world-news",
    "https://apnews.com/climate-and-environment",
]
# ─────────────────────────────────────────────────────────────────────────────

XML_FILE = "pau.xml"
APNEWS_HTML_FILE = "apnews.html"   # only the last section is kept for debug
MAX_ITEMS = 500

# retries / backoff
FLARE_RETRIES = 2
FLARE_BACKOFF = 2.0
SIMPLE_RETRIES = 3
SIMPLE_BACKOFF = 1.5
SIMPLE_TIMEOUT = 20

# ------------------------------
# LOGGING
# ------------------------------
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILENAME, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("scraper")

def debug(msg, *args): log.debug(msg, *args)
def info(msg, *args):  log.info(msg, *args)
def warn(msg, *args):  log.warning(msg, *args)
def now_utc():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def normalize_title(title: str) -> str:
    """Lowercase + strip for case-insensitive title dedup."""
    return title.lower().strip()

def title_guid(title: str) -> str:
    """Stable guid derived from the normalized title (SHA-1 hex)."""
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()

# ------------------------------
# HTTP helpers
# ------------------------------
def simple_get(url: str, timeout: int = SIMPLE_TIMEOUT) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/117 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
    except Exception as e:
        debug("simple_get exception for %s : %s", url, e)
        return None
    if r.status_code != 200:
        debug("simple_get: HTTP %s for %s", r.status_code, url)
        return None
    return r.text


_flare_session_id = "scraper_session_1"

def flare_get(url: str, timeout_ms: int = 120_000) -> str | None:
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout_ms,
        "session": _flare_session_id,
    }
    debug("FlareSolverr GET -> %s", url)
    try:
        r = requests.post(
            FLARESOLVERR_URL,
            json=payload,
            timeout=(timeout_ms // 1000) + 15,
        )
    except Exception as e:
        debug("FlareSolverr request exception: %s", e)
        return None

    if r.status_code != 200:
        body = (r.text or "")[:2000]
        warn(
            "FlareSolverr returned HTTP %s for %s | body (truncated): %s",
            r.status_code, url, body,
        )
        return None

    try:
        data = r.json()
    except Exception as e:
        warn(
            "FlareSolverr returned non-json response for %s: %s | body (truncated): %s",
            url, e, (r.text or "")[:2000],
        )
        return None

    status = data.get("status", "")
    if status != "ok":
        warn(
            "FlareSolverr status=%s for %s | message: %s | response (truncated): %s",
            status, url, data.get("message", ""), str(data)[:4000],
        )
        return None

    sol = data.get("solution", {}) or {}
    html = sol.get("response") or ""
    if isinstance(html, dict):
        html = html.get("data") or html.get("body") or html.get("html") or ""

    if not html:
        warn("FlareSolverr returned empty HTML for %s", url)
        return None

    return html


def fetch_page(url: str) -> str | None:
    """Try FlareSolverr first, then fall back to plain requests."""
    for attempt in range(1, FLARE_RETRIES + 1):
        html = flare_get(url)
        if html and len(html) > 200:
            debug("fetch_page: got HTML from FlareSolverr (attempt %d)", attempt)
            return html
        debug("fetch_page: FlareSolverr attempt %d failed for %s", attempt, url)
        time.sleep(FLARE_BACKOFF * attempt)

    for attempt in range(1, SIMPLE_RETRIES + 1):
        html = simple_get(url, timeout=SIMPLE_TIMEOUT)
        if html and len(html) > 200:
            debug("fetch_page: got HTML from simple_get (attempt %d)", attempt)
            return html
        debug("fetch_page: simple_get attempt %d failed for %s", attempt, url)
        time.sleep(SIMPLE_BACKOFF * attempt)

    warn("fetch_page: all attempts failed for %s", url)
    return None

# ------------------------------
# Parsing helpers
# ------------------------------
def save_debug_html(path: str, html: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        debug("Saved HTML to %s (%d bytes)", path, len(html))
    except Exception as e:
        warn("Failed saving HTML %s: %s", path, e)


def build_full_url(href: str, base: str = APNEWS_BASE) -> str | None:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return None


def _thumb_from_card(card) -> str:
    """Extract best available thumbnail URL from a PagePromo card."""
    img_el = card.select_one("div.PagePromo-media img")
    if img_el:
        raw = (
            img_el.get("src")
            or img_el.get("data-src")
            or img_el.get("data-lazy-src")
            or img_el.get("data-original")
            or ""
        ).strip()
        if raw and not raw.startswith("data:") and len(raw) > 20:
            return raw
        # lazy-loaded: srcset holds the real URL
        ss = (img_el.get("srcset") or "").strip()
        if ss:
            return ss.split()[0].rstrip(",").strip()

    # fallback: first <source srcset> inside <picture>
    picture = card.select_one("div.PagePromo-media picture")
    if picture:
        for src_el in picture.find_all("source"):
            ss = src_el.get("srcset", "").strip()
            if ss:
                return ss.split()[0].rstrip(",").strip()

    return ""


# ------------------------------
# Per-section scraper
# ------------------------------
def scrape_section(url: str) -> list[dict]:
    """
    Fetch one AP News section page and return a list of article dicts:
      { "url": str, "title": str, "thumb": str, "source": str }
    Falls back to a raw anchor scan if the card selector finds nothing.
    """
    info("Fetching section: %s", url)
    html = fetch_page(url)
    articles: list[dict] = []

    if not html:
        warn("scrape_section: failed to fetch %s", url)
        return articles

    # keep a debug copy of the last-fetched page
    save_debug_html(APNEWS_HTML_FILE, html)

    soup = BeautifulSoup(html, "html.parser")
    primary: list[tuple[str, str, str]] = []

    try:
        for card in soup.select("div.PagePromo"):
            # title + href from the headline anchor
            title_el = (
                card.select_one("h3.PagePromo-title a.Link")
                or card.select_one("h2.PagePromo-title a.Link")
            )
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            href  = title_el.get("href", "").strip()

            # some cards have the link only on the media wrapper
            if not href:
                media_link = card.select_one("div.PagePromo-media > a.Link")
                if media_link:
                    href = media_link.get("href", "").strip()

            full_url = build_full_url(href)
            if not full_url or not title:
                continue

            thumb = _thumb_from_card(card)
            primary.append((full_url, title, thumb))

    except Exception as e:
        warn("scrape_section: exception in card selector for %s: %s", url, e)

    if primary:
        info("Section %s — card selector matched %d articles", url, len(primary))
        for u, t, th in primary:
            articles.append({"url": u, "title": t, "thumb": th, "source": url})
        return articles

    # ── fallback: raw anchor scan ─────────────────────────────────────────────
    warn("scrape_section: card selector empty for %s — falling back to anchor scan", url)
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/article/" not in href:
            continue
        title_text = a.get_text(" ", strip=True)
        if not title_text:
            continue
        full_url = build_full_url(href)
        if not full_url or full_url in seen:
            continue
        seen.add(full_url)
        thumb = ""
        parent = a.find_parent()
        if parent:
            img = parent.find("img")
            if img:
                thumb = (img.get("src") or img.get("data-src") or "").strip()
        articles.append({"url": full_url, "title": title_text, "thumb": thumb, "source": url})

    info("Section %s — fallback anchor scan found %d articles", url, len(articles))
    return articles

# ------------------------------
# Feed XML helpers
# ------------------------------
def load_or_create_xml(path: str, title: str, link: str, description: str):
    if os.path.exists(path):
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            info("Loaded existing XML: %s", path)
        except ET.ParseError as e:
            warn("XML parse error (%s) — creating new: %s", e, path)
            root = ET.Element("rss", version="2.0")
            tree = ET.ElementTree(root)
    else:
        root = ET.Element("rss", version="2.0")
        tree = ET.ElementTree(root)
        info("Created new XML root: %s", path)

    channel = root.find("channel")
    if channel is None:
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text = title
        ET.SubElement(channel, "link").text = link
        ET.SubElement(channel, "description").text = description

    return tree, root, channel

# ------------------------------
# Main
# ------------------------------
def main():
    # ── 1. Scrape every section ───────────────────────────────────────────────
    raw_articles: list[dict] = []
    for section_url in SOURCES:
        raw_articles.extend(scrape_section(section_url))

    # ── 2. Global deduplication by title (case-insensitive) ───────────────────
    combined: list[dict] = []
    seen_titles: set[str] = set()
    for item in raw_articles:
        t = normalize_title(item.get("title", ""))
        if not t or t in seen_titles:
            continue
        seen_titles.add(t)
        combined.append(item)

    info("Total unique articles across all sections: %d", len(combined))

    # ── 3. Load / create XML feed ─────────────────────────────────────────────
    tree, root, channel = load_or_create_xml(
        XML_FILE,
        title="AP News Feed",
        link="https://apnews.com",
        description="Scraped articles from AP News (world news + climate)",
    )

    # Existing items keyed by guid (title hash) — RSS readers use <guid> as
    # the canonical identity key, NOT <link>, so same-URL/new-title = new item.
    existing_guids: set[str] = {
        item.find("guid").text.strip()
        for item in channel.findall("item")
        if item.find("guid") is not None and item.find("guid").text
    }
    info("Existing items in feed: %d", len(existing_guids))

    # ── 4. Insert new items ───────────────────────────────────────────────────
    new_count = 0
    for art in combined:
        title = (art.get("title") or "").strip()
        if not title:
            warn("Skipping (no title): %s", art.get("url"))
            continue

        guid = title_guid(title)
        if guid in existing_guids:
            continue

        thumb = (art.get("thumb") or "").strip()
        desc  = (
            f'<img src="{thumb}" alt="" style="max-width:100%"/><br/>{title}'
            if thumb else title
        )

        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text       = title
        ET.SubElement(item_el, "link").text        = art["url"]
        ET.SubElement(item_el, "description").text = desc
        ET.SubElement(item_el, "pubDate").text     = now_utc()
        guid_el = ET.SubElement(item_el, "guid")
        guid_el.text = guid
        guid_el.set("isPermaLink", "false")
        if thumb:
            ET.SubElement(item_el, "enclosure", url=thumb, type="image/jpeg")

        new_count += 1
        debug("Added: [%s] %s", guid[:8], art["url"])

    info("Added %d new articles to feed", new_count)

    # ── 5. Rolling cap ────────────────────────────────────────────────────────
    all_items = channel.findall("item")
    if len(all_items) > MAX_ITEMS:
        for old in all_items[:-MAX_ITEMS]:
            channel.remove(old)
        info("Trimmed feed to %d items", MAX_ITEMS)

    # ── 6. Save ───────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
    tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
    info("Done! Feed saved to %s", XML_FILE)
    info("Debug log saved to %s", LOG_FILENAME)


if __name__ == "__main__":
    main()
