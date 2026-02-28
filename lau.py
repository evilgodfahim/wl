#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Reuters listing page + Reuters commentary page + France24 RSS feed via Playwright,
extract full article text, and write/update a simple RSS XML file.

Replaces FlareSolverr with Playwright (headless Chromium) for JS-rendered pages.
"""

import sys
import os
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

REUTERS_URL             = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL  = "https://www.reuters.com/commentary/"
FRANCE24_RSS            = "https://www.france24.com/en/rss"
HTML_FILE               = "opinin.html"
COMMENTARY_HTML_FILE    = "commentary.html"
XML_FILE                = "pau.xml"
MAX_ITEMS               = 500
REUTERS_BASE            = "https://www.reuters.com"
PAGE_TIMEOUT_MS         = 60_000   # navigation timeout
WAIT_AFTER_LOAD_MS      = 3_000    # extra wait for JS hydration

FRANCE24_EXCLUDE = ["/video/", "/live-news/", "/sport/", "/tv-shows/", "/sports/", "/videos/"]

# User-agent that looks like a regular browser
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

def build_full_url(href):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return REUTERS_BASE + href
    return None

# ------------------------------
# Playwright context (singleton)
# ------------------------------

_pw        = None
_browser   = None
_context   = None

def get_browser_context():
    """Lazy-init a persistent Playwright browser context."""
    global _pw, _browser, _context
    if _context is not None:
        return _context

    info("Launching Playwright Chromium (headless)…")
    _pw      = sync_playwright().start()
    _browser = _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    _context = _browser.new_context(
        user_agent=UA,
        locale="en-US",
        viewport={"width": 1280, "height": 900},
        # Pretend to be a real browser
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    # Hide navigator.webdriver
    _context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return _context


def close_browser():
    global _pw, _browser, _context
    try:
        if _context:
            _context.close()
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = _context = None


def pw_get(url: str) -> str | None:
    """
    Fetch a URL using Playwright and return the fully-rendered HTML.
    Returns None on error.
    """
    debug("Playwright fetch: %s", url)
    ctx = get_browser_context()
    page = None
    try:
        page = ctx.new_page()
        # Block images/fonts/media to speed things up
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_(),
        )
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        # Let JS hydrate
        page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
        html = page.content()
        debug("Playwright received %d bytes from %s", len(html), url)
        return html
    except PWTimeout:
        warn("Playwright timeout fetching %s", url)
        return None
    except Exception as e:
        warn("Playwright error fetching %s: %s", url, e)
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


# Keep the same public name the rest of the script used
flare_get = pw_get

# ------------------------------
# Extractors
# ------------------------------

def extract_full_text_reuters(article_html):
    s = BeautifulSoup(article_html, "html.parser")

    container = s.find("div", class_="article-body-module__content__bnXL1")
    if container:
        paragraphs = container.find_all(attrs={"data-testid": True})
        parts = []
        for p in paragraphs:
            dt = p.get("data-testid", "") or ""
            if "paragraph-" in dt:
                text = p.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)

    blocks = s.select('div[data-testid="Body"] p')
    if not blocks:
        blocks = s.select("article p")

    parts = [p.get_text(" ", strip=True) for p in blocks if p.get_text(" ", strip=True)]
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
            href = blk.get("href", "").strip()
            url  = build_full_url(href)
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
        # Fallback anchor scan
        seen, fallback_items = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(
                r'^/(world|article|business|markets|breakingviews|technology|investigations|commentary)/',
                href
            ) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or (
                    a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
                )
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
                title_text = a.get_text(" ", strip=True) or (
                    a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
                )
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
rss_html = None
try:
    rss_response = requests.get(
        FRANCE24_RSS,
        headers={"User-Agent": UA},
        timeout=30,
    )
    rss_html = rss_response.text
    debug("France24 RSS HTTP status: %s, length: %d", rss_response.status_code, len(rss_html))
except Exception as e:
    warn("Direct fetch of France24 RSS failed: %s; falling back to Playwright", e)
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
    warn("Failed to fetch France24 RSS content")

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

# ------------------------------
# 4. FETCH FULL TEXT
# ------------------------------

for i, a in enumerate(all_articles, 1):
    info("Processing %d/%d: %s", i, len(all_articles), a.get("title", "")[:80])
    page = flare_get(a["url"])
    if page is None:
        warn("Failed to fetch article page: %s", a.get("url"))
        a["desc"] = ""
        a["img"]  = a.get("thumb", "") or ""
        a["pub"]  = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        continue

    a["desc"] = (
        extract_full_text_reuters(page)
        if a.get("source") == "Reuters"
        else extract_full_text_france24(page)
    ) or ""

    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page) or a.get("thumb", "") or ""
    a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    if DEBUG:
        desc_len = len(a["desc"])
        debug("  desc length: %d, img: %s", desc_len, (a["img"] or "")[:120])
        if desc_len == 0:
            warn("  Empty description. Page snippet:\n%s",
                 page[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))

# Cleanly close Playwright when done fetching
close_browser()

# ------------------------------
# 5. LOAD OR CREATE XML
# ------------------------------

if os.path.exists(XML_FILE):
    try:
        tree = ET.parse(XML_FILE)
        root = tree.getroot()
        info("Loaded existing XML: %s", XML_FILE)
    except ET.ParseError as e:
        warn("XML parse error (%s) – creating new root", e)
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
