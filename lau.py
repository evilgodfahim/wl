#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import re
import logging
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
REUTERS_URL             = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL  = "https://www.reuters.com/commentary/"
FRANCE24_RSS            = "https://www.france24.com/en/rss"
HTML_FILE               = "opinin.html"
COMMENTARY_HTML_FILE    = "commentary.html"
XML_FILE                = "pau.xml"
MAX_ITEMS               = 500
REUTERS_BASE            = "https://www.reuters.com"
TIMEOUT_MS              = 120000   # 2 min — give FlareSolverr time to solve CAPTCHA

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

def now_utc():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def save_debug_html(path, html):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        debug("Saved HTML to %s (%d bytes)", path, len(html))
    except Exception as e:
        warn("Failed saving HTML %s: %s", path, e)

def build_full_url(href):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return REUTERS_BASE + href
    return None

# ------------------------------
# FlareSolverr
# ------------------------------

# Reuse the same session cookie across requests so DataDome sees
# a consistent browser fingerprint and is less likely to re-challenge.
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

    debug("FlareSolverr HTTP %s", r.status_code)
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

    # Detect if we still got a CAPTCHA/challenge page
    if len(html) < 5000 and any(x in html for x in ["captcha-delivery.com", "DataDome", "geo.captcha"]):
        warn("FlareSolverr returned a CAPTCHA challenge page for %s (%d bytes)", url, len(html))
        debug("Challenge snippet: %s", html[:400].replace("\n", " "))
        return None

    debug("FlareSolverr received %d bytes for %s", len(html), url)
    return html


def flare_session_destroy():
    try:
        requests.post(FLARESOLVERR_URL, json={"cmd": "sessions.destroy", "session": _flare_session_id}, timeout=10)
    except Exception:
        pass

# ------------------------------
# Extractors
# ------------------------------

def extract_full_text_reuters(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("div", class_="article-body-module__content__bnXL1")
    if container:
        parts = []
        for p in container.find_all(attrs={"data-testid": True}):
            if "paragraph-" in (p.get("data-testid") or ""):
                text = p.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)

    blocks = s.select('div[data-testid="Body"] p') or s.select("article p")
    parts  = [p.get_text(" ", strip=True) for p in blocks if p.get_text(" ", strip=True)]
    return "\n\n".join(parts)


def extract_full_text_france24(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("div", class_="t-content__body") or s.find(
        "div", class_=lambda c: c and "t-content__body" in c
    )
    if container:
        parts = []
        for p in container.find_all("p"):
            text = p.get_text(" ", strip=True)
            if text and not text.startswith("Read more") and not text.startswith("(FRANCE 24"):
                parts.append(text)
        if parts:
            return "\n\n".join(parts)

    blocks = s.select("article p")
    parts  = [p.get_text(" ", strip=True) for p in blocks if p.get_text(" ", strip=True)]
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
# 1. FETCH REUTERS WORLD
# ------------------------------

info("Fetching Reuters world page: %s", REUTERS_URL)
html = flare_get(REUTERS_URL)
reuters_articles = []

if html is None:
    warn("Failed to fetch Reuters world page")
else:
    save_debug_html(HTML_FILE, html)
    soup = BeautifulSoup(html, "html.parser")

    primary_items = []
    try:
        nodes = soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]')
        debug("Primary selector nodes found: %d", len(nodes))
        for blk in nodes:
            href  = blk.get("href", "").strip()
            url   = build_full_url(href)
            if not url:
                continue
            span  = blk.select_one('span[data-testid="TitleHeading"]')
            title = span.get_text(" ", strip=True) if span else blk.get_text(" ", strip=True)
            if title:
                primary_items.append((url, title))
    except Exception as e:
        warn("Exception in primary world selector: %s", e)

    if primary_items:
        info("Primary world selector matched %d items.", len(primary_items))
        for u, t in primary_items[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | %s", u, t)
        for url, title in primary_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})
    else:
        seen, fallback_items = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(
                r'^/(world|article|business|markets|breakingviews|technology|investigations|commentary)/',
                href
            ) or '/article/' in href:
                title_text = a.get_text(" ", strip=True)
                if not title_text:
                    parent = a.find_parent()
                    title_text = parent.get_text(" ", strip=True) if parent else ""
                if not title_text:
                    continue
                full = build_full_url(href)
                if not full or full in seen:
                    continue
                seen.add(full)
                fallback_items.append((full, title_text))

        info("Fallback anchor scan found %d candidates.", len(fallback_items))
        for full, title in fallback_items[:DEBUG_SAMPLE_LIMIT]:
            debug("  - %s | %s", full, title)
        if not fallback_items:
            warn("No world article candidates found. HTML snippet:\n%s",
                 html[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))
        for url, title in fallback_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})

    info("Found %d Reuters world articles", len(reuters_articles))

# ------------------------------
# 1B. FETCH REUTERS COMMENTARY
# ------------------------------

info("Fetching Reuters commentary page: %s", REUTERS_COMMENTARY_URL)
commentary_html = flare_get(REUTERS_COMMENTARY_URL)

if commentary_html is None:
    warn("Failed to fetch Reuters commentary page")
else:
    save_debug_html(COMMENTARY_HTML_FILE, commentary_html)
    csoup = BeautifulSoup(commentary_html, "html.parser")

    primary_cards = []
    try:
        for card in csoup.select('[data-testid="StoryCard"]'):
            title_el = card.select_one('[data-testid="TitleHeading"]')
            link_el  = card.select_one('[data-testid="TitleLink"]')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(" ", strip=True)
            href  = link_el.get("href", "").strip()
            thumb_el = card.select_one(
                '[data-testid="MediaImageLink"] [data-testid="EagerImageContainer"] img[data-testid="EagerImage"]'
            )
            thumb = ""
            if thumb_el:
                thumb = (thumb_el.get("src") or thumb_el.get("data-src") or "").strip()
            primary_cards.append((href, title, thumb))
    except Exception as e:
        warn("Exception in primary commentary selector: %s", e)

    if primary_cards:
        info("Primary commentary selector matched %d cards.", len(primary_cards))
        for href, title, thumb in primary_cards[:DEBUG_SAMPLE_LIMIT]:
            debug("  - href=%s | title=%s", href, title)
        for href, title, thumb in primary_cards:
            url = build_full_url(href)
            if url:
                reuters_articles.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})
    else:
        seen, fallback_cards = set(), []
        for a in csoup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(
                r'^/(commentary|breakingviews|article|business|world|opinions)/', href
            ) or '/article/' in href:
                title_text = a.get_text(" ", strip=True)
                if not title_text:
                    parent = a.find_parent()
                    title_text = parent.get_text(" ", strip=True) if parent else ""
                if not title_text:
                    continue
                full = build_full_url(href)
                if not full or full in seen:
                    continue
                seen.add(full)
                thumb = ""
                parent = a.find_parent()
                if parent:
                    img = parent.find("img")
                    if img:
                        thumb = (img.get("src") or img.get("data-src") or "").strip()
                fallback_cards.append((full, title_text, thumb))

        info("Fallback commentary scan found %d candidates.", len(fallback_cards))
        if not fallback_cards:
            warn("No commentary candidates found. HTML snippet:\n%s",
                 commentary_html[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))
        for full, title, thumb in fallback_cards:
            reuters_articles.append({"url": full, "title": title, "source": "Reuters", "thumb": thumb})

    info("Total Reuters articles (world + commentary): %d", len(reuters_articles))

# ------------------------------
# 2. FETCH FRANCE24 RSS
# ------------------------------

info("Fetching France24 RSS: %s", FRANCE24_RSS)
rss_html = flare_get(FRANCE24_RSS)
france24_articles = []

if rss_html:
    items = re.findall(r'<item>(.*?)</item>', rss_html, re.DOTALL)
    info("Found %d total items in France24 RSS", len(items))
    added = excluded = 0
    for item_content in items:
        title_m = re.search(r'<title>(.*?)</title>', item_content)
        link_m  = re.search(r'<link>(.*?)</link>', item_content)
        if not title_m or not link_m:
            continue
        title = title_m.group(1).strip()
        url   = link_m.group(1).strip()
        if not url.startswith("http"):
            continue
        skip = False
        for ex in FRANCE24_EXCLUDE:
            if ex in url:
                excluded += 1
                skip = True
                break
        if skip:
            continue
        france24_articles.append({"url": url, "title": title, "source": "France24"})
        added += 1
    info("France24: kept %d items, excluded %d", added, excluded)
else:
    warn("Failed to fetch France24 RSS")

# ------------------------------
# 3. COMBINE & DEDUPE
# ------------------------------

combined, seen_combined = [], set()
for item in reuters_articles + france24_articles:
    u = item.get("url")
    if not u or u in seen_combined:
        continue
    seen_combined.add(u)
    combined.append(item)

all_articles = combined
info("Total unique articles to process: %d", len(all_articles))
for i, a in enumerate(all_articles[:DEBUG_SAMPLE_LIMIT], 1):
    debug("  sample %d: [%s] %s", i, a.get("source"), a.get("url"))

# ------------------------------
# 4. FETCH FULL TEXT
# ------------------------------

for i, a in enumerate(all_articles, 1):
    info("Processing %d/%d: %s", i, len(all_articles), a.get("title", "")[:80])
    page = flare_get(a["url"])
    if page is None:
        warn("Failed to fetch: %s", a.get("url"))
        a["desc"] = ""
        a["img"]  = a.get("thumb", "") or ""
        a["pub"]  = now_utc()
        continue

    a["desc"] = (
        extract_full_text_reuters(page)
        if a.get("source") == "Reuters"
        else extract_full_text_france24(page)
    ) or ""

    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page) or a.get("thumb", "") or ""
    a["pub"] = now_utc()

    desc_len = len(a["desc"])
    debug("  desc length: %d, img: %s", desc_len, (a["img"] or "")[:120])
    if desc_len == 0:
        warn("  Empty description for: %s\n  Page snippet: %s",
             a["url"], page[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))

# Cleanup FlareSolverr session
flare_session_destroy()

# ------------------------------
# 5. LOAD OR CREATE XML
# ------------------------------

if os.path.exists(XML_FILE):
    try:
        tree = ET.parse(XML_FILE)
        root = tree.getroot()
        info("Loaded existing XML: %s", XML_FILE)
    except ET.ParseError as e:
        warn("XML parse error (%s) — creating new", e)
        root = ET.Element("rss", version="2.0")
        tree = ET.ElementTree(root)
else:
    root = ET.Element("rss", version="2.0")
    tree = ET.ElementTree(root)
    info("Created new XML root")

channel = root.find("channel")
if channel is None:
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text       = "Reuters + France24 Combined Feed"
    ET.SubElement(channel, "link").text        = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Combined scraped articles from Reuters and France24"

# ------------------------------
# 6. DEDUPLICATE EXISTING
# ------------------------------

existing = {
    item.find("link").text.strip()
    for item in channel.findall("item")
    if item.find("link") is not None and item.find("link").text
}
info("Existing items in feed: %d", len(existing))

# ------------------------------
# 7. ADD NEW ARTICLES
# ------------------------------

new_count = 0
for art in all_articles:
    if art["url"] in existing:
        debug("Already exists, skipping: %s", art["url"])
        continue
    if not (art.get("desc") or "").strip():
        warn("Skipping (no description): %s", art.get("title", "")[:60])
        continue
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text       = art["title"]
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = art["desc"]
    ET.SubElement(item, "pubDate").text     = art["pub"]
    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")
    new_count += 1
    debug("Added: %s", art["url"])

info("Added %d new articles", new_count)

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

os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Feed saved to %s", XML_FILE)
info("Debug log saved to %s", LOG_FILENAME)
