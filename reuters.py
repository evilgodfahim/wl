#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import re
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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

REUTERS_URL             = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL  = "https://www.reuters.com/commentary/"
REUTERS_EXTRA_URLS      = [
    "https://www.reuters.com/business/energy/",
    "https://www.reuters.com/business/environment/",
    "https://www.reuters.com/sustainability/climate-energy/",
    "https://www.reuters.com/sustainability/reuters-impact/",
]
HTML_FILE               = "opinin.html"
COMMENTARY_HTML_FILE    = "commentary.html"
REUTERS_XML_FILE        = "reuters.xml"
MAX_ITEMS               = 500
REUTERS_BASE            = "https://www.reuters.com"
TIMEOUT_MS              = 120000

# Expanded DataDome & Captcha Markers
DATADOME_MARKERS = [
    "#cmsg{animation: A 1.5s;}",
    "#cmsg{animation:A 1.5s}",
    "captcha-delivery",
    "<title>Just a moment...</title>",
    "datadome"
]

REUTERS_SKIP_PATHS = (
    "/newsletters/",
    "/graphics/",
    "/live-blog/",
    "/podcast/",
    "/video/",
)

REUTERS_JUNK_TITLES = {
    "video", "live", "graphic", "graphics", "podcast",
}

BOTBROWSER_FETCH_DELAY = 1.5

# ------------------------------
# BotBrowser Configuration
# ------------------------------

BOTBROWSER_BINARY      = os.environ.get("BOTBROWSER_PATH", "./BotBrowser/dist/botbrowser")
BOTBROWSER_PROFILE_DIR = os.environ.get("BOTBROWSER_PROFILE_DIR", "")
BOTBROWSER_PROFILE     = os.environ.get("BOTBROWSER_PROFILE", "")


def _build_launch_args() -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    if BOTBROWSER_PROFILE_DIR:
        args.append(f"--bot-profile-dir={BOTBROWSER_PROFILE_DIR}")
        debug("BotBrowser profile: random from dir '%s'", BOTBROWSER_PROFILE_DIR)
    elif BOTBROWSER_PROFILE:
        args.append(f"--bot-profile={BOTBROWSER_PROFILE}")
        debug("BotBrowser profile: '%s'", BOTBROWSER_PROFILE)
    else:
        debug("BotBrowser profile: built-in default")
    return args


def botbrowser_get(url: str, retries: int = 3) -> str | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        warn("playwright is not installed.")
        return None

    if not os.path.isfile(BOTBROWSER_BINARY):
        warn("BotBrowser binary not found at '%s'.", BOTBROWSER_BINARY)
        return None

    launch_args = _build_launch_args()

    for attempt in range(1, retries + 1):
        debug("BotBrowser attempt %d/%d for %s", attempt, retries, url)
        html = None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path=BOTBROWSER_BINARY,
                    headless=True,
                    args=launch_args,
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    java_script_enabled=True,
                )
                page = context.new_page()

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    try:
                        page.wait_for_selector(
                            '[data-testid="Title"], [data-testid="Body"], article, [data-testid="StoryCard"]', 
                            timeout=5000
                        )
                    except PWTimeout:
                        debug("Timeout waiting for specific content selectors. Proceeding with current DOM.")

                    html = page.content()

                except PWTimeout:
                    warn("Navigation timed out for %s (attempt %d/%d)", url, attempt, retries)
                except Exception as e:
                    debug("goto/content error: %s", e)
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass

        except Exception as e:
            warn("BotBrowser outer error for %s (attempt %d/%d): %s",
                 url, attempt, retries, e)
            time.sleep(1)
            continue

        if not html or len(html) < 500:
            warn("No usable HTML captured for %s (attempt %d/%d)", url, attempt, retries)
            time.sleep(1)
            continue

        html_lower = html.lower()
        if any(m.lower() in html_lower for m in DATADOME_MARKERS):
            warn("DataDome CAPTCHA for %s (attempt %d/%d)", url, attempt, retries)
            time.sleep(2)
            continue

        debug("BotBrowser: %d bytes for %s", len(html), url)
        return html

    warn("BotBrowser: all %d attempts exhausted for %s", retries, url)
    return None


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

def build_full_url(href, base=REUTERS_BASE):
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    return None

def is_valid_article_url(url, title=""):
    """
    Globally filters out category pages, index hubs, and non-article links.
    """
    if not url:
        return False
    if not re.match(r'https?://(?:www\.)?reuters\.com/', url):
        return False
        
    url_path = url.split("reuters.com", 1)[-1].split("?")[0]
    
    if any(url_path.startswith(p) for p in REUTERS_SKIP_PATHS):
        return False
        
    if title and title.strip().lower() in REUTERS_JUNK_TITLES:
        return False
        
    last_segment = url_path.strip("/").split("/")[-1]
    
    if not last_segment:
        return False
        
    # Real article slugs almost always have 3+ hyphens (e.g., /biden-says-this-thing-today/)
    if last_segment.count('-') >= 3:
        return True
        
    # Fallbacks for very short titles that end in standard Reuters date/ID formats
    if re.search(r'-\d{4}-\d{2}-\d{2}$', last_segment):
        return True
    if re.search(r'-id[A-Z0-9]{5,}$', last_segment, re.IGNORECASE):
        return True
        
    return False

def extract_full_text_reuters(article_html):
    s = BeautifulSoup(article_html, "html.parser")
    container = s.find("div", class_=re.compile(r"article-body-module__content__"))

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

def extract_reuters_extra_cards(soup, page_url):
    results = []
    seen = set()

    def _add(href, title, thumb=""):
        url = build_full_url(href)
        title = (title or "").strip()
        if not url or url in seen:
            return
        if not is_valid_article_url(url, title):
            return
        seen.add(url)
        results.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})

    def _eager_image(el):
        img = el.select_one('img[data-testid="EagerImage"]')
        return (img.get("src") or img.get("data-src") or "").strip() if img else ""

    def _noscript_image(el):
        ns = el.select_one("noscript > img[src]")
        return ns.get("src", "").strip() if ns else ""

    for card in soup.select(
        'li.static-media-maximizer-module__card__F-y9S > '
        'div.basic-card-module__container__TucWe[data-testid="BasicCard"]'
    ):
        link_el = card.select_one('a[data-testid="Title"]')
        if link_el:
            _add(link_el.get("href", ""), link_el.get_text(" ", strip=True), _eager_image(card))

    for card in soup.select('div.basic-card-module__container__TucWe[data-testid="BasicCard"]'):
        link_el = card.select_one('a[data-testid="Title"]')
        if link_el:
            _add(link_el.get("href", ""), link_el.get_text(" ", strip=True), _eager_image(card))

    for cell in soup.select('li[data-testid="TalkingPointsCell"] > a[data-testid="MediaCard"]'):
        heading = cell.select_one('span[data-testid="MediaCardHeading"]')
        if heading:
            _add(cell.get("href", ""), heading.get_text(" ", strip=True), _noscript_image(cell))

    for card in soup.select('a[data-testid="MediaCard"]'):
        heading = card.select_one('span[data-testid="MediaCardHeading"]')
        if heading:
            _add(card.get("href", ""), heading.get_text(" ", strip=True), _noscript_image(card))

    debug("extract_reuters_extra_cards: %d items from %s", len(results), page_url)
    return results


# ------------------------------
# 1. FETCH REUTERS WORLD
# ------------------------------

info("Fetching Reuters world page: %s", REUTERS_URL)
html = botbrowser_get(REUTERS_URL)
reuters_articles = []

if html is None:
    warn("Failed to fetch Reuters world page")
else:
    save_debug_html(HTML_FILE, html)
    soup = BeautifulSoup(html, "html.parser")

    primary_items = []
    try:
        nodes = soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]')
        for blk in nodes:
            href  = blk.get("href", "").strip()
            url   = build_full_url(href)
            span  = blk.select_one('span[data-testid="TitleHeading"]')
            title = span.get_text(" ", strip=True) if span else blk.get_text(" ", strip=True)
            if is_valid_article_url(url, title):
                primary_items.append((url, title))
    except Exception as e:
        warn("Exception in primary world selector: %s", e)

    if primary_items:
        info("Primary world selector matched %d items.", len(primary_items))
        for url, title in primary_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})
    else:
        seen, fallback_items = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'^/(world|article|business|markets|breakingviews|technology|investigations|commentary)/', href) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or (a.find_parent().get_text(" ", strip=True) if a.find_parent() else "")
                full = build_full_url(href)
                if full and full not in seen and is_valid_article_url(full, title_text):
                    seen.add(full)
                    fallback_items.append((full, title_text))

        info("Fallback anchor scan found %d candidates.", len(fallback_items))
        for url, title in fallback_items:
            reuters_articles.append({"url": url, "title": title, "source": "Reuters"})


# ------------------------------
# 1B. FETCH REUTERS COMMENTARY
# ------------------------------

info("Fetching Reuters commentary page: %s", REUTERS_COMMENTARY_URL)
commentary_html = botbrowser_get(REUTERS_COMMENTARY_URL)

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
            thumb_el = card.select_one('[data-testid="MediaImageLink"] [data-testid="EagerImageContainer"] img[data-testid="EagerImage"]')
            thumb = (thumb_el.get("src") or thumb_el.get("data-src") or "").strip() if thumb_el else ""
            primary_cards.append((href, title, thumb))
    except Exception as e:
        warn("Exception in primary commentary selector: %s", e)

    if primary_cards:
        for href, title, thumb in primary_cards:
            url = build_full_url(href)
            if is_valid_article_url(url, title):
                reuters_articles.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})
    else:
        seen, fallback_cards = set(), []
        for a in csoup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'^/(commentary|breakingviews|article|business|world|opinions)/', href) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or (a.find_parent().get_text(" ", strip=True) if a.find_parent() else "")
                full = build_full_url(href)
                if full and full not in seen and is_valid_article_url(full, title_text):
                    seen.add(full)
                    thumb = ""
                    parent = a.find_parent()
                    if parent and parent.find("img"):
                        img = parent.find("img")
                        thumb = (img.get("src") or img.get("data-src") or "").strip()
                    fallback_cards.append((full, title_text, thumb))

        for full, title, thumb in fallback_cards:
            reuters_articles.append({"url": full, "title": title, "source": "Reuters", "thumb": thumb})


# ------------------------------
# 1C. FETCH REUTERS EXTRA SECTION PAGES
# ------------------------------

for extra_url in REUTERS_EXTRA_URLS:
    info("Fetching Reuters extra page: %s", extra_url)
    extra_html = botbrowser_get(extra_url)

    if extra_html is None:
        continue

    extra_soup = BeautifulSoup(extra_html, "html.parser")
    cards = extract_reuters_extra_cards(extra_soup, extra_url)

    if cards:
        reuters_articles.extend(cards)
    else:
        seen_extra = set()
        for a in extra_soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'^/(world|article|business|markets|breakingviews|technology|investigations|commentary|sustainability)/', href) or '/article/' in href:
                title_text = a.get_text(" ", strip=True) or (a.find_parent().get_text(" ", strip=True) if a.find_parent() else "")
                full = build_full_url(href)
                if full and full not in seen_extra and is_valid_article_url(full, title_text):
                    seen_extra.add(full)
                    reuters_articles.append({"url": full, "title": title_text, "source": "Reuters"})

info("Total Reuters articles after extra pages: %d", len(reuters_articles))

# Dedupe
seen_combined = set()
all_articles = []
for item in reuters_articles:
    u = item.get("url")
    if not u or u in seen_combined:
        continue
    seen_combined.add(u)
    all_articles.append(item)

info("Total unique Reuters articles: %d", len(all_articles))

# ------------------------------
# LOAD XML
# ------------------------------

def load_or_create_xml(path, title, link, description):
    if os.path.exists(path):
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except ET.ParseError:
            root = ET.Element("rss", version="2.0")
            tree = ET.ElementTree(root)
    else:
        root = ET.Element("rss", version="2.0")
        tree = ET.ElementTree(root)

    channel = root.find("channel")
    if channel is None:
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text       = title
        ET.SubElement(channel, "link").text        = link
        ET.SubElement(channel, "description").text = description

    return tree, root, channel

reuters_tree, reuters_root, reuters_channel = load_or_create_xml(
    REUTERS_XML_FILE,
    "Reuters Feed",
    "https://evilgodfahim.github.io/reur/reuters",
    "Scraped articles from Reuters",
)

reuters_existing = {
    item.find("link").text.strip()
    for item in reuters_channel.findall("item")
    if item.find("link") is not None and item.find("link").text
}

# ------------------------------
# FETCH FULL TEXT
# ------------------------------

for i, a in enumerate(all_articles, 1):
    if a["url"] in reuters_existing:
        continue

    page = botbrowser_get(a["url"])

    if page is None:
        a["desc"] = ""
        a["img"]  = a.get("thumb", "") or ""
        a["pub"]  = now_utc()
        continue

    a["desc"] = extract_full_text_reuters(page) or ""
    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page) or a.get("thumb", "") or ""
    a["pub"] = now_utc()

    time.sleep(BOTBROWSER_FETCH_DELAY)

# ------------------------------
# ADD NEW ARTICLES
# ------------------------------

new_count = 0
for art in all_articles:
    if art["url"] in reuters_existing:
        continue

    title = (art.get("title") or "").strip()
    desc  = (art.get("desc")  or "").strip()

    if not title and not desc:
        continue

    if not desc:
        desc = title

    item = ET.SubElement(reuters_channel, "item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = desc
    ET.SubElement(item, "pubDate").text     = art["pub"]
    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")

    new_count += 1

# ------------------------------
# TRIM & SAVE
# ------------------------------

all_items = reuters_channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        reuters_channel.remove(old)

os.makedirs(os.path.dirname(REUTERS_XML_FILE) or ".", exist_ok=True)
reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Reuters feed saved to %s", REUTERS_XML_FILE)