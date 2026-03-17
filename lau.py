#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ------------------------------
# DEBUG / CONFIG
# ------------------------------

DEBUG = True
DEBUG_HTML_SNIPPET_LEN = 800
DEBUG_SAMPLE_LIMIT = 12

LOG_FILENAME = "debug.log"
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILENAME, mode="w", encoding="utf-8")
    ]
)
log = logging.getLogger("scraper")

# ------------------------------
# CONFIGURATION
# ------------------------------

FLARESOLVERR_URL        = "http://localhost:8191/v1"
APNEWS_URL              = "https://apnews.com/world-news"
APNEWS_BASE             = "https://apnews.com"
HTML_FILE               = "opinin.html"
APNEWS_HTML_FILE        = "apnews.html"
XML_FILE                = "pau.xml"
MAX_ITEMS               = 500
TIMEOUT_MS              = 120000

# ------------------------------
# Helpers
# ------------------------------

def debug(msg, *args):
    if DEBUG:
        log.debug(msg, *args)

def info(msg, *args):
    log.info(msg, *args)

def warn(msg, *args):
    log.warning(msg, *args)

def now_utc():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def save_debug_html(path, html):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        debug("Saved HTML to %s (%d bytes)", path, len(html))
    except Exception as e:
        warn("Failed saving HTML %s: %s", path, e)

def build_full_url(href, base=APNEWS_BASE):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return None

# ------------------------------
# FlareSolverr (used for fetching)
# ------------------------------

_flare_session_id = "scraper_session_1"

def flare_get(url):
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": TIMEOUT_MS,
        "session": _flare_session_id,
    }
    debug("FlareSolverr GET: %s", url)
    try:
        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=TIMEOUT_MS // 1000 + 30)
    except Exception as e:
        warn("FlareSolverr request error: %s", e)
        return None

    if r.status_code != 200:
        warn("FlareSolverr returned HTTP %s for %s", r.status_code, url)
        return None

    try:
        data = r.json()
    except Exception as e:
        warn("Invalid JSON from FlareSolverr: %s", e)
        return None

    status = data.get("status", "")
    if status != "ok":
        warn("FlareSolverr status=%s for %s | message: %s", status, url, data.get("message", ""))
        return None

    sol = data.get("solution", {})
    html = sol.get("response") or ""

    if isinstance(html, dict):
        html = html.get("data") or html.get("body") or html.get("html") or ""

    if not html:
        warn("Empty HTML from FlareSolverr for %s", url)
        return None

    debug("FlareSolverr received %d bytes for %s", len(html), url)
    return html

def flare_session_destroy():
    try:
        requests.post(FLARESOLVERR_URL, json={"cmd": "sessions.destroy", "session": _flare_session_id}, timeout=10)
    except Exception:
        pass

def fetch_page(url: str) -> str | None:
    return flare_get(url)

# ------------------------------
# Extractors
# ------------------------------

def extract_full_text_generic(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("article") or s.find("div", {"role": "main"})
    parts = []
    if container:
        for p in container.find_all("p"):
            text = p.get_text(" ", strip=True)
            if text:
                parts.append(text)
    if parts:
        return "\n\n".join(parts)

    # fallback: any paragraphs
    blocks = s.select("p")
    parts = [p.get_text(" ", strip=True) for p in blocks if p.get_text(" ", strip=True)]
    return "\n\n".join(parts)

def extract_image_url(soup_page):
    meta_og = soup_page.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        return meta_og["content"].strip()
    link_img = soup_page.find("link", rel="image_src")
    if link_img and link_img.get("href"):
        return link_img["href"].strip()
    for img in soup_page.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and src.strip():
            return src.strip()
    return ""

# ------------------------------
# 1. FETCH AP NEWS WORLD
# ------------------------------

info("Fetching AP News world page via FlareSolverr: %s", APNEWS_URL)
apnews_html = fetch_page(APNEWS_URL)
apnews_articles = []

if apnews_html is None:
    warn("Failed to fetch AP News world page")
else:
    save_debug_html(APNEWS_HTML_FILE, apnews_html)
    apsoup = BeautifulSoup(apnews_html, "html.parser")

    primary_ap = []
    try:
        for card in apsoup.select("div.PagePromo"):
            title_el = card.select_one("h3.PagePromo-title a.Link")
            if not title_el:
                title_el = card.select_one("h2.PagePromo-title a.Link")
            if not title_el:
                continue

            title = title_el.get_text(" ", strip=True)
            href  = title_el.get("href", "").strip()

            media_link = card.select_one("div.PagePromo-media > a.Link")
            if not href and media_link:
                href = media_link.get("href", "").strip()

            url = build_full_url(href, base=APNEWS_BASE)
            if not url or not title:
                continue

            thumb = ""
            img_el = card.select_one("div.PagePromo-media img")
            if img_el:
                raw = (
                    img_el.get("src", "")
                    or img_el.get("data-src", "")
                    or img_el.get("data-lazy-src", "")
                    or img_el.get("data-original", "")
                    or ""
                ).strip()
                if raw and not raw.startswith("data:") and len(raw) > 20:
                    thumb = raw
                if not thumb:
                    srcset = img_el.get("srcset", "").strip()
                    if srcset:
                        thumb = srcset.split()[0].rstrip(",").strip()
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
        for u, t, th in primary_ap[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | thumb=%s | %s", u, th[:80] if th else "", t)
        for url, title, thumb in primary_ap:
            apnews_articles.append({
                "url": url, "title": title, "source": "APNews", "thumb": thumb
            })
    else:
        warn("AP News primary selector found nothing — falling back to anchor scan")
        seen_ap = set()
        for a in apsoup.find_all("a", href=True):
            href = a["href"].strip()
            if "/article/" not in href:
                continue
            title_text = a.get_text(" ", strip=True)
            if not title_text:
                continue
            full = build_full_url(href, base=APNEWS_BASE)
            if not full or full in seen_ap:
                continue
            seen_ap.add(full)
            thumb = ""
            parent = a.find_parent()
            if parent:
                img = parent.find("img")
                if img:
                    thumb = (img.get("src") or img.get("data-src") or "").strip()
            apnews_articles.append({
                "url": full, "title": title_text, "source": "APNews", "thumb": thumb
            })

        info("AP News fallback anchor scan found %d candidates.", len(apnews_articles))
        if not apnews_articles:
            warn("No AP News candidates found. HTML snippet:\n%s",
                 apnews_html[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))

    info("Found %d AP News articles", len(apnews_articles))

# ------------------------------
# 2. COMBINE & DEDUPE
# ------------------------------

combined, seen_combined = [], set()
for item in apnews_articles:
    u = item.get("url")
    if not u or u in seen_combined:
        continue
    seen_combined.add(u)
    combined.append(item)

all_articles = combined
info("Total unique articles to process: %d", len(all_articles))

# ------------------------------
# 3. LOAD XML (to skip already-known articles)
# ------------------------------

def load_or_create_xml(path, title, link, description):
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
        ET.SubElement(channel, "title").text       = title
        ET.SubElement(channel, "link").text        = link
        ET.SubElement(channel, "description").text = description

    return tree, root, channel

tree, root, channel = load_or_create_xml(
    XML_FILE,
    "AP News Feed",
    "https://example.local/apnews/",
    "Scraped articles from AP News",
)

existing = {
    item.find("link").text.strip()
    for item in channel.findall("item")
    if item.find("link") is not None and item.find("link").text
}
info("Existing items in feed: %d", len(existing))

# ------------------------------
# 4. FETCH FULL TEXT
# ------------------------------

for a in all_articles:
    if a.get("source") == "APNews":
        thumb = a.get("thumb", "") or ""
        a["img"] = thumb
        if thumb:
            a["desc"] = '<img src="{}" alt="" style="max-width:100%"/><br/>{}'.format(thumb, a.get("title", ""))
        else:
            a["desc"] = a.get("title", "")
        a["pub"] = now_utc()

for i, a in enumerate(all_articles, 1):
    if a.get("source") == "APNews":
        # already have a description above
        continue

    if a["url"] in existing:
        continue

    info("Processing %d/%d [%s]: %s", i, len(all_articles), a.get("source"), a.get("title", "")[:80])

    page = fetch_page(a["url"])
    if page is None:
        warn("Failed to fetch: %s", a.get("url"))
        a["desc"] = ""
        a["img"]  = a.get("thumb", "") or ""
        a["pub"]  = now_utc()
        continue

    a["desc"] = extract_full_text_generic(page) or ""
    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page) or a.get("thumb", "") or ""
    a["pub"] = now_utc()

    desc_len = len(a["desc"])
    debug("  desc length: %d, img: %s", desc_len, (a["img"] or "")[:120])
    if desc_len == 0:
        warn("  Empty description for: %s\n  Page snippet: %s",
             a["url"], page[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))

# Cleanup
flare_session_destroy()

# ------------------------------
# 5. ADD NEW ARTICLES
# ------------------------------

new_count = 0
for art in all_articles:
    if art["url"] in existing:
        continue

    title = (art.get("title") or "").strip()
    desc  = (art.get("desc")  or "").strip()

    if not title and not desc:
        warn("Skipping (no title or description): %s", art["url"])
        continue

    if not desc:
        warn("No description for '%s' — using title as fallback", title[:60])
        desc = title

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = desc
    ET.SubElement(item, "pubDate").text     = art["pub"]
    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")

    new_count += 1
    debug("Added: %s", art["url"])

info("Added %d new articles to main feed", new_count)

# ------------------------------
# 6. TRIM OLD ITEMS
# ------------------------------

all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)
    info("Trimmed feed to %d items", MAX_ITEMS)

# ------------------------------
# 7. SAVE XML
# ------------------------------

os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Main feed saved to %s", XML_FILE)
info("Debug log saved to %s", LOG_FILENAME)