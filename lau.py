#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Reuters listing page + Reuters commentary page + France24 RSS feed via FlareSolverr,
extract full article text, and write/update a simple RSS XML file.

This variant adds thorough debugging output (controlled by DEBUG).
"""

import requests
import sys
import os
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup

# ------------------------------
# DEBUG / CONFIG
# ------------------------------

DEBUG = True  # set False to reduce output
DEBUG_HTML_SNIPPET_LEN = 800  # chars to print when selectors find nothing
DEBUG_SAMPLE_LIMIT = 12       # how many sample items to show when debugging

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
# CONFIGURATION (unchanged)
# ------------------------------

FLARESOLVERR_URL = "http://localhost:8191/v1"
REUTERS_URL = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL = "https://www.reuters.com/commentary/"
FRANCE24_RSS = "https://www.france24.com/en/rss"
HTML_FILE = "opinin.html"
COMMENTARY_HTML_FILE = "commentary.html"
XML_FILE = "pau.xml"
MAX_ITEMS = 500
REUTERS_BASE = "https://www.reuters.com"
TIMEOUT_MS = 60000

FRANCE24_EXCLUDE = ["/video/", "/live-news/", "/sport/", "/tv-shows/", "/sports/", "/videos/"]

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

def save_debug_html(path, html):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        debug("Saved HTML to %s (%d bytes)", path, len(html))
    except Exception as e:
        warn("Failed saving HTML %s: %s", path, e)

# ------------------------------
# FlareSolverr helper
# ------------------------------

def flare_get(url):
    """
    Call FlareSolverr to fetch rendered HTML.
    Returns HTML string or None on error.
    """
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": TIMEOUT_MS
    }

    debug("FlareSolverr request: %s", url)
    try:
        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=30)
    except Exception as e:
        warn("Request error to FlareSolverr: %s", e)
        return None

    debug("FlareSolverr HTTP %s", r.status_code)
    if r.status_code != 200:
        warn("FlareSolverr returned HTTP %s", r.status_code)
        try:
            debug("FlareSolverr response head: %s", r.text[:400])
        except Exception:
            pass
        return None

    try:
        data = r.json()
    except Exception as e:
        warn("Invalid JSON from FlareSolverr: %s", e)
        return None

    if "error" in data:
        warn("FlareSolverr error field present: %s", data.get("error"))
        return None

    sol = data.get("solution")
    if not sol:
        warn("No 'solution' in FlareSolverr response")
        return None

    resp = sol.get("response")
    if resp is None:
        warn("No 'response' in FlareSolverr solution")
        return None

    # FlareSolverr may return response as a dict with 'data' or as the raw HTML string
    if isinstance(resp, dict):
        html = resp.get("data") or resp.get("body") or resp.get("html")
    else:
        html = resp

    if not html:
        warn("Empty HTML returned by FlareSolverr")
        return None

    debug("Received HTML length: %d", len(html))
    return html

# ------------------------------
# Extractors (unchanged behavior)
# ------------------------------

def extract_full_text_reuters(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("div", class_="article-body-module__content__bnXL1")
    if container:
        paragraphs = container.find_all(attrs={"data-testid": True})
        parts = []
        for p in paragraphs:
            dt = p.get("data-testid", "") or p.attrs.get("data_testid", "")
            if dt and "paragraph-" in dt:
                text = p.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)

    blocks = s.select('div[data-testid="Body"] p')
    if not blocks:
        blocks = s.select("article p")

    parts = []
    for p in blocks:
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)

    return "\n\n".join(parts)

def extract_full_text_france24(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("div", class_="t-content__body")
    if not container:
        container = s.find("div", class_=lambda c: c and "t-content__body" in c)

    if container:
        paragraphs = container.find_all("p")
        parts = []
        for p in paragraphs:
            text = p.get_text(" ", strip=True)
            if text and not text.startswith("Read more") and not text.startswith("(FRANCE 24"):
                parts.append(text)
        if parts:
            return "\n\n".join(parts)

    blocks = s.select("article p")
    parts = []
    for p in blocks:
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)

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

def build_full_url(href):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return REUTERS_BASE + href
    return None

# ------------------------------
# 1. FETCH REUTERS WORLD
# ------------------------------

info("Fetching Reuters world page: %s", REUTERS_URL)
html = flare_get(REUTERS_URL)
reuters_articles = []

if html is None:
    warn("Failed to fetch Reuters world page via FlareSolverr")
else:
    save_debug_html(HTML_FILE, html)
    soup = BeautifulSoup(html, "html.parser")

    # Primary attempt: existing selectors
    primary_items = []
    try:
        nodes = soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]')
        debug("Primary selector nodes found: %d", len(nodes))
        for blk in nodes:
            href = blk.get("href", "").strip()
            url = build_full_url(href)
            if not url:
                continue
            span = blk.select_one('span[data-testid="TitleHeading"]')
            title = span.get_text(" ", strip=True) if span else blk.get_text(" ", strip=True)
            if not title:
                continue
            primary_items.append((url, title))
    except Exception as e:
        warn("Exception while running primary world selector: %s", e)
        primary_items = []

    # Log primary samples
    if primary_items:
        info("Primary world selector matched %d items. Sample:", len(primary_items))
        for u, t in primary_items[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | %s", u, t)
        for url, title in primary_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})
    else:
        # Fallback: scan anchors for likely article links
        anchors = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'^/(world|article|business|markets|breakingviews|technology|investigations|commentary)/', href) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or ""
                if not title_text:
                    parent = a.find_parent()
                    title_text = parent.get_text(" ", strip=True) if parent else ""
                if title_text:
                    anchors.append((href, title_text))
        # dedupe & normalize
        seen = set()
        fallback_items = []
        for href, title in anchors:
            full = build_full_url(href)
            if not full:
                continue
            if full in seen:
                continue
            seen.add(full)
            fallback_items.append((full, title))
        info("Primary world selector returned 0 items. Fallback anchor scan found %d candidates.", len(fallback_items))
        for full, title in fallback_items[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | %s", full, title)
        # if nothing found, show HTML snippet for debugging
        if not fallback_items:
            snippet = html[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " ")
            warn("No world article candidates found. HTML snippet (first %d chars):\n%s", DEBUG_HTML_SNIPPET_LEN, snippet)
        for url, title in fallback_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})

    info("Found %d Reuters world articles (after fallback/dedupe)", len(reuters_articles))

# ------------------------------
# 1B. FETCH REUTERS COMMENTARY
# ------------------------------

info("Fetching Reuters commentary page: %s", REUTERS_COMMENTARY_URL)
commentary_html = flare_get(REUTERS_COMMENTARY_URL)
if commentary_html is None:
    warn("Failed to fetch Reuters commentary page via FlareSolverr")
else:
    save_debug_html(COMMENTARY_HTML_FILE, commentary_html)
    csoup = BeautifulSoup(commentary_html, "html.parser")

    primary_cards = []
    try:
        cards = csoup.select('[data-testid="StoryCard"]')
        debug("Primary commentary selector cards found: %d", len(cards))
        for card in cards:
            title_el = card.select_one('[data-testid="TitleHeading"]')
            link_el = card.select_one('[data-testid="TitleLink"]')
            thumb_el = card.select_one('[data-testid="MediaImageLink"] [data-testid="EagerImageContainer"] img[data-testid="EagerImage"]')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(" ", strip=True)
            href = link_el.get("href", "").strip()
            thumb = ""
            if thumb_el:
                thumb = thumb_el.get("src") or thumb_el.get("data-src") or thumb_el.get("data-lazy-src") or ""
                thumb = thumb.strip() if thumb else ""
            primary_cards.append((href, title, thumb))
    except Exception as e:
        warn("Exception while running primary commentary selector: %s", e)
        primary_cards = []

    if primary_cards:
        info("Primary commentary selector matched %d cards. Sample:", len(primary_cards))
        for href, title, thumb in primary_cards[:DEBUG_SAMPLE_LIMIT]:
            debug("  - href=%s | title=%s | thumb=%s", href, title, thumb)
        for href, title, thumb in primary_cards:
            url = build_full_url(href)
            if not url:
                continue
            reuters_articles.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})
    else:
        anchors = []
        for a in csoup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'^/(commentary|breakingviews|article|business|world|opinions)/', href) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or ""
                if not title_text:
                    parent = a.find_parent()
                    title_text = parent.get_text(" ", strip=True) if parent else ""
                if title_text:
                    # try to capture image from parent
                    thumb = ""
                    parent = a.find_parent()
                    if parent:
                        img = parent.find("img")
                        if img:
                            thumb = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
                            thumb = thumb.strip() if thumb else ""
                    anchors.append((href, title_text, thumb))
        seen = set()
        fallback_cards = []
        for href, title, thumb in anchors:
            full = build_full_url(href)
            if not full:
                continue
            if full in seen:
                continue
            seen.add(full)
            fallback_cards.append((full, title, thumb))
        info("Primary commentary selector returned 0 items. Fallback anchor scan found %d candidates.", len(fallback_cards))
        for full, title, thumb in fallback_cards[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | %s | %s", full, title, thumb)
        if not fallback_cards:
            snippet = commentary_html[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " ")
            warn("No commentary candidates found. Commentary HTML snippet (first %d chars):\n%s", DEBUG_HTML_SNIPPET_LEN, snippet)
        for full, title, thumb in fallback_cards:
            reuters_articles.append({"url": full, "title": title, "source": "Reuters", "thumb": thumb})

    info("Total Reuters articles (world + commentary): %d", len(reuters_articles))

# ------------------------------
# 2. FETCH FRANCE24 RSS
# ------------------------------

info("Fetching France24 RSS: %s", FRANCE24_RSS)
try:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    rss_response = requests.get(FRANCE24_RSS, headers=headers, timeout=30)
    rss_html = rss_response.text
    debug("France24 RSS HTTP status: %s", rss_response.status_code)
except Exception as e:
    warn("Direct fetch of France24 RSS failed: %s; falling back to FlareSolverr", e)
    rss_html = flare_get(FRANCE24_RSS)

france24_articles = []
if rss_html:
    item_pattern = re.compile(r'<item>(.*?)</item>', re.DOTALL)
    items = item_pattern.findall(rss_html)
    info("Found %d total items in France24 RSS", len(items))
    added = 0
    excluded = 0
    for item_content in items:
        title_match = re.search(r'<title>(.*?)</title>', item_content)
        link_match = re.search(r'<link>(.*?)</link>', item_content)
        if not title_match or not link_match:
            debug("Skipping France24 item: missing title or link")
            continue
        title = title_match.group(1).strip()
        url = link_match.group(1).strip()
        if not url.startswith("http"):
            debug("Skipping France24 item (invalid url): %s", url)
            continue
        excluded_flag = False
        for ex in FRANCE24_EXCLUDE:
            if ex in url:
                excluded += 1
                excluded_flag = True
                debug("Excluded France24: %s contains %s", title[:60], ex)
                break
        if excluded_flag:
            continue
        france24_articles.append({"url": url, "title": title, "source": "France24"})
        added += 1
    info("France24: kept %d items, excluded %d by pattern", added, excluded)
else:
    warn("Failed to fetch France24 RSS content")

# ------------------------------
# 3. COMBINE & DEDUPE
# ------------------------------

combined = []
seen_combined = set()
for item in reuters_articles + france24_articles:
    u = item.get("url")
    if not u or u in seen_combined:
        continue
    seen_combined.add(u)
    combined.append(item)

all_articles = combined
info("Total unique articles to process: %d", len(all_articles))
if DEBUG:
    for i, a in enumerate(all_articles[:DEBUG_SAMPLE_LIMIT], 1):
        debug("  sample %d: %s | %s", i, a.get("source"), a.get("url"))

# ------------------------------
# 4. FETCH FULL TEXT
# ------------------------------

for i, a in enumerate(all_articles, 1):
    info("Processing %d/%d: %s", i, len(all_articles), a.get("title", "")[:80])
    page = flare_get(a["url"])
    if page is None:
        warn("Failed to fetch article page: %s", a.get("url"))
        a["desc"] = ""
        a["img"] = a.get("thumb", "") or ""
        a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        continue

    if a.get("source") == "Reuters":
        full_text = extract_full_text_reuters(page)
    else:
        full_text = extract_full_text_france24(page)

    a["desc"] = full_text or ""
    soup_page = BeautifulSoup(page, "html.parser")
    page_img = extract_image_url(soup_page)
    a["img"] = page_img or a.get("thumb", "") or ""
    a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    if DEBUG:
        desc_len = len(a["desc"]) if a["desc"] else 0
        debug("  desc length: %d, img: %s", desc_len, a["img"][:200] if a["img"] else "")
        if desc_len == 0:
            snippet = page[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " ")
            warn("  Article page produced empty description. Page snippet first %d chars:\n%s", DEBUG_HTML_SNIPPET_LEN, snippet)

# ------------------------------
# 5. LOAD OR CREATE XML
# ------------------------------

if os.path.exists(XML_FILE):
    try:
        tree = ET.parse(XML_FILE)
        root = tree.getroot()
        info("Loaded existing XML file: %s", XML_FILE)
    except ET.ParseError as e:
        warn("Existing XML parse error: %s -- creating new root", e)
        root = ET.Element("rss", version="2.0")
        tree = ET.ElementTree(root)
else:
    root = ET.Element("rss", version="2.0")
    tree = ET.ElementTree(root)
    info("Created new XML root")

channel = root.find("channel")
if channel is None:
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Reuters + France24 Combined Feed"
    ET.SubElement(channel, "link").text = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Combined scraped articles from Reuters and France24"
    info("Initialized channel in XML")

# ------------------------------
# 6. DEDUPLICATE EXISTING ITEMS
# ------------------------------

existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None and link_tag.text:
        existing.add(link_tag.text.strip())
info("Existing items in feed: %d", len(existing))

# ------------------------------
# 7. ADD NEW ARTICLES
# ------------------------------

new_count = 0
for art in all_articles:
    if art["url"] in existing:
        debug("Skipping already-existing URL: %s", art["url"])
        continue
    if not art.get("desc") or not art["desc"].strip():
        warn("Skipping (no description): %s - %s", art.get("title", "")[:60], art.get("url"))
        continue
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = art["title"]
    ET.SubElement(item, "link").text = art["url"]
    ET.SubElement(item, "description").text = art["desc"] or ""
    ET.SubElement(item, "pubDate").text = art["pub"]
    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")
    new_count += 1
    debug("Added new item: %s", art["url"])

info("Added %d new articles to feed", new_count)

# ------------------------------
# 8. TRIM OLD ITEMS
# ------------------------------

all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)
    info("Trimmed feed to %d items", MAX_ITEMS)

# ------------------------------
# 9. SAVE XML
# ------------------------------

os.makedirs(os.path.dirname(XML_FILE) or '.', exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Feed saved to %s", XML_FILE)
info("Debug log saved to %s (same directory)", LOG_FILENAME)