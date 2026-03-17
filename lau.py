#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resilient APNews scraper with FlareSolverr + requests fallback.
Drop-in replacement for lau.py — continues if FlareSolverr returns errors.
"""

from __future__ import annotations
import sys
import os
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import json

import requests
from bs4 import BeautifulSoup

# ------------------------------
# CONFIG
# ------------------------------
DEBUG = True
LOG_FILENAME = "debug.log"
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
APNEWS_URL = "https://apnews.com/world-news"
APNEWS_BASE = "https://apnews.com"
XML_FILE = "pau.xml"
APNEWS_HTML_FILE = "apnews.html"
MAX_ITEMS = 500

# retries/backoff
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
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILENAME, mode="w", encoding="utf-8")],
)
log = logging.getLogger("scraper")

def debug(msg, *args): log.debug(msg, *args)
def info(msg, *args): log.info(msg, *args)
def warn(msg, *args): log.warning(msg, *args)
def now_utc(): return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

# ------------------------------
# HTTP helpers
# ------------------------------
def simple_get(url: str, timeout: int = SIMPLE_TIMEOUT) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117 Safari/537.36"}
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

def flare_get(url: str, timeout_ms: int = 120000) -> str | None:
    payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout_ms, "session": _flare_session_id}
    debug("FlareSolverr GET -> %s", url)
    try:
        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=(timeout_ms // 1000) + 15)
    except Exception as e:
        debug("FlareSolverr request exception: %s", e)
        return None

    if r.status_code != 200:
        # log truncated body for diagnosis
        body = (r.text or "")[:2000]
        warn("FlareSolverr returned HTTP %s for %s | body (truncated): %s", r.status_code, url, body)
        return None

    try:
        data = r.json()
    except Exception as e:
        warn("FlareSolverr returned non-json response for %s: %s | body (truncated): %s", url, e, (r.text or "")[:2000])
        return None

    status = data.get("status", "")
    if status != "ok":
        warn("FlareSolverr status=%s for %s | message: %s | response (truncated): %s",
             status, url, data.get("message", ""), str(data)[:4000])
        return None

    sol = data.get("solution", {}) or {}
    html = sol.get("response") or ""
    if isinstance(html, dict):
        # some versions return nested fields
        html = html.get("data") or html.get("body") or html.get("html") or ""

    if not html:
        warn("FlareSolverr returned empty HTML for %s", url)
        return None

    return html

def fetch_page(url: str) -> str | None:
    # Try FlareSolverr first (if reachable), with retries/backoff, then fallback to requests
    for attempt in range(1, FLARE_RETRIES + 1):
        html = flare_get(url)
        if html and len(html) > 200:
            debug("fetch_page: got HTML from FlareSolverr (attempt %d)", attempt)
            return html
        debug("fetch_page: FlareSolverr attempt %d failed for %s", attempt, url)
        time.sleep(FLARE_BACKOFF * attempt)

    # fallback: try direct requests with retries/backoff
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

def extract_image_url(soup_page: BeautifulSoup) -> str:
    meta_og = soup_page.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        return meta_og["content"].strip()
    link_img = soup_page.find("link", rel="image_src")
    if link_img and link_img.get("href"):
        return link_img["href"].strip()
    for img in soup_page.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and src.strip() and not src.strip().startswith("data:"):
            return src.strip()
    return ""

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
# Main scraping logic
# ------------------------------
def main():
    info("Fetching AP News world page: %s", APNEWS_URL)
    apnews_html = fetch_page(APNEWS_URL)
    apnews_articles = []

    if not apnews_html:
        warn("Failed to fetch AP News world page; proceeding with empty list")
    else:
        save_debug_html(APNEWS_HTML_FILE, apnews_html)
        apsoup = BeautifulSoup(apnews_html, "html.parser")
        primary_ap = []
        try:
            for card in apsoup.select("div.PagePromo"):
                title_el = card.select_one("h3.PagePromo-title a.Link") or card.select_one("h2.PagePromo-title a.Link")
                if not title_el:
                    continue
                title = title_el.get_text(" ", strip=True)
                href = title_el.get("href", "").strip()
                media_link = card.select_one("div.PagePromo-media > a.Link")
                if not href and media_link:
                    href = media_link.get("href", "").strip()
                url = build_full_url(href, base=APNEWS_BASE)
                if not url or not title:
                    continue
                thumb = ""
                img_el = card.select_one("div.PagePromo-media img")
                if img_el:
                    raw = (img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or img_el.get("data-original") or "").strip()
                    if raw and not raw.startswith("data:") and len(raw) > 20:
                        thumb = raw
                    elif img_el.get("srcset"):
                        thumb = img_el.get("srcset").split()[0].rstrip(",").strip()
                if not thumb:
                    picture = card.select_one("div.PagePromo-media picture")
                    if picture:
                        for src_el in picture.find_all("source"):
                            ss = src_el.get("srcset", "").strip()
                            if ss:
                                thumb = ss.split()[0].rstrip(",").strip()
                                break
                primary_ap.append((url, title, thumb))
        except Exception as e:
            warn("Exception in AP News primary selector: %s", e)

        if primary_ap:
            info("AP News primary selector matched %d cards.", len(primary_ap))
            for url, title, thumb in primary_ap:
                apnews_articles.append({"url": url, "title": title, "source": "APNews", "thumb": thumb})
        else:
            # fallback anchor scan
            seen = set()
            for a in apsoup.find_all("a", href=True):
                href = a["href"].strip()
                if "/article/" not in href:
                    continue
                title_text = a.get_text(" ", strip=True)
                if not title_text:
                    continue
                full = build_full_url(href, base=APNEWS_BASE)
                if not full or full in seen:
                    continue
                seen.add(full)
                thumb = ""
                parent = a.find_parent()
                if parent:
                    img = parent.find("img")
                    if img:
                        thumb = (img.get("src") or img.get("data-src") or "").strip()
                apnews_articles.append({"url": full, "title": title_text, "source": "APNews", "thumb": thumb})
            info("AP News fallback anchor scan found %d candidates.", len(apnews_articles))

    # combine / dedupe
    combined = []
    seen_combined = set()
    for item in apnews_articles:
        u = item.get("url")
        if not u or u in seen_combined:
            continue
        seen_combined.add(u)
        combined.append(item)

    info("Total unique articles to process: %d", len(combined))

    # load xml
    tree, root, channel = load_or_create_xml(XML_FILE, "AP News Feed", "https://example.local/apnews/", "Scraped articles from AP News")
    existing = {
        item.find("link").text.strip()
        for item in channel.findall("item")
        if item.find("link") is not None and item.find("link").text
    }
    info("Existing items in feed: %d", len(existing))

    # prepare entries (APNews cards already include title/thumb; we don't need to fetch article pages)
    new_count = 0
    for art in combined:
        if art["url"] in existing:
            continue
        title = (art.get("title") or "").strip()
        thumb = art.get("thumb", "") or ""
        if thumb:
            desc = f'<img src="{thumb}" alt="" style="max-width:100%"/><br/>{title}'
        else:
            desc = title or ""
        if not title and not desc:
            warn("Skipping (no title or description): %s", art.get("url"))
            continue
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = art["url"]
        ET.SubElement(item, "description").text = desc
        ET.SubElement(item, "pubDate").text = now_utc()
        if thumb:
            ET.SubElement(item, "enclosure", url=thumb, type="image/jpeg")
        new_count += 1
        debug("Added: %s", art["url"])

    info("Added %d new articles to main feed", new_count)

    # trim
    all_items = channel.findall("item")
    if len(all_items) > MAX_ITEMS:
        for old in all_items[:-MAX_ITEMS]:
            channel.remove(old)
        info("Trimmed feed to %d items", MAX_ITEMS)

    # save
    os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
    tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
    info("Done! Main feed saved to %s", XML_FILE)
    info("Debug log saved to %s", LOG_FILENAME)

if __name__ == "__main__":
    main()