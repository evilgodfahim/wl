#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import re
import logging
import subprocess
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
REUTERS_URL             = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL  = "https://www.reuters.com/commentary/"
REUTERS_EXTRA_URLS      = [
    "https://www.reuters.com/business/energy/",
    "https://www.reuters.com/business/environment/",
    "https://www.reuters.com/sustainability/climate-energy/",
    "https://www.reuters.com/sustainability/reuters-impact/",
]
APNEWS_URL              = "https://apnews.com/world-news"
APNEWS_BASE             = "https://apnews.com"
FRANCE24_URL            = "https://www.france24.com/en/"
HTML_FILE               = "opinin.html"
COMMENTARY_HTML_FILE    = "commentary.html"
APNEWS_HTML_FILE        = "apnews.html"
XML_FILE                = "pau.xml"
REUTERS_XML_FILE        = "reuters.xml"
MAX_ITEMS               = 500
REUTERS_BASE            = "https://www.reuters.com"
TIMEOUT_MS              = 120000

FRANCE24_EXCLUDE = ["/video/", "/live-news/", "/sport/", "/tv-shows/", "/sports/", "/videos/"]

# DataDome CAPTCHA challenge page unique CSS — never present in real pages.
# (Script URLs like captcha-delivery.com appear on ALL Reuters pages, so must not be used.)
DATADOME_MARKERS = [
    "#cmsg{animation: A 1.5s;}",
    "#cmsg{animation:A 1.5s}",
]

# URL path prefixes that are never fetchable articles on reuters.com
REUTERS_SKIP_PATHS = (
    "/newsletters/",
    "/graphics/",
    "/live-blog/",
    "/podcast/",
    "/video/",
)

# Titles that are purely UI labels / non-article cards.
REUTERS_JUNK_TITLES = {
    "video", "live", "graphic", "graphics", "podcast",
}

# ------------------------------
# Playwright (rebrowser-patched) Configuration
# ------------------------------

_pw       = None
_browser  = None


def _start_browser() -> bool:
    """Launch a fresh Playwright Chromium instance (rebrowser-patched)."""
    global _pw, _browser
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        warn("playwright is not installed.")
        return False

    _stop_browser()
    debug("Launching Playwright Chromium (rebrowser-patched)")
    try:
        _pw      = sync_playwright().start()
        _browser = _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        return True
    except Exception as e:
        warn("Failed to launch browser: %s", e)
        return False


def _stop_browser():
    global _pw, _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


def _fetch_once(url: str) -> str | None:
    """Single attempt to fetch a URL via Playwright."""
    global _browser
    if _browser is None:
        return None

    try:
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        return None

    try:
        page = _browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        except PWTimeout:
            warn("Navigation timed out for %s", url)
            page.close()
            return None

        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            debug("networkidle timed out for %s (non-fatal)", url)

        html = page.content()
        page.close()
    except Exception as e:
        warn("Playwright error for %s: %s", url, e)
        return None

    if not html or len(html) < 500:
        warn("Suspiciously short HTML (%d bytes) for %s", len(html), url)
        return None

    if any(m in html for m in DATADOME_MARKERS):
        warn("DataDome CAPTCHA detected for %s (%d bytes)", url, len(html))
        return None

    debug("Received %d bytes for %s", len(html), url)
    return html


def botbrowser_get(url: str, retries: int = 2) -> str | None:
    for attempt in range(1, retries + 1):
        if not _start_browser():
            warn("Browser not available (attempt %d/%d)", attempt, retries)
            continue

        result = _fetch_once(url)
        if result:
            return result

        warn("Fetch failed on attempt %d/%d for %s", attempt, retries, url)

    warn("All %d attempts failed for %s", retries, url)
    return None


def _botbrowser_shutdown():
    _stop_browser()


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
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return None

def is_reuters_url(url: str) -> bool:
    return "reuters.com" in (url or "")

def fetch_page(url: str) -> str | None:
    if is_reuters_url(url):
        return botbrowser_get(url)
    else:
        return flare_get(url)


# ------------------------------
# FlareSolverr
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

    if len(html) < 5000 and any(x in html for x in ["captcha-delivery.com", "DataDome", "geo.captcha"]):
        warn("FlareSolverr returned a CAPTCHA challenge page for %s (%d bytes)", url, len(html))
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


def extract_reuters_extra_cards(soup, page_url):
    results = []
    seen = set()

    def _is_valid(url, title):
        if not url or not title:
            return False
        if not re.match(r'https?://(?:www\.)?reuters\.com/', url):
            return False
        parsed_path = url.split("reuters.com", 1)[-1]
        if any(parsed_path.startswith(p) for p in REUTERS_SKIP_PATHS):
            return False
        if title.strip().lower() in REUTERS_JUNK_TITLES:
            return False
        return True

    def _add(href, title, thumb=""):
        url = build_full_url(href)
        title = (title or "").strip()
        if url in seen:
            return
        if not _is_valid(url, title):
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
# 1. FETCH REUTERS WORLD  (BotBrowser)
# ------------------------------

info("Fetching Reuters world page via BotBrowser: %s", REUTERS_URL)
html = fetch_page(REUTERS_URL)
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

info("Total Reuters articles: %d", len(reuters_articles))

# ------------------------------
# 1D. FETCH AP NEWS WORLD  (FlareSolverr)
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
# 2. FETCH FRANCE24  (FlareSolverr)
# ------------------------------

FRANCE24_BASE = "https://www.france24.com"

info("Fetching France24 page via FlareSolverr: %s", FRANCE24_URL)
f24_html = fetch_page(FRANCE24_URL)
france24_articles = []

if f24_html is None:
    warn("Failed to fetch France24 page")
else:
    save_debug_html("france24.html", f24_html)
    f24soup = BeautifulSoup(f24_html, "html.parser")
    seen_f24 = set()
    added = excluded = 0

    for art in f24soup.select("div.m-item-list-article[data-article-list]"):
        # Title and URL
        title_el = art.select_one(".article__infos .article__title a[data-article-item-link]")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href  = title_el.get("href", "").strip()
        # Fallback: get URL from thumbnail anchor if title link has no href
        if not href:
            thumb_anchor = art.select_one("a.m-item-image[data-article-item-link]")
            if thumb_anchor:
                href = thumb_anchor.get("href", "").strip()
        if not href:
            continue
        url = href if href.startswith("http") else FRANCE24_BASE + href
        if not url or url in seen_f24:
            continue

        # Filter excluded paths
        skip = any(ex in url for ex in FRANCE24_EXCLUDE)
        if skip:
            excluded += 1
            continue

        # Thumbnail
        thumb = ""
        if not art.select_one(".m-item-list-article--no-image"):
            img_el = art.select_one("a.m-item-image picture.a-picture img.a-img")
            if img_el:
                thumb = (img_el.get("src") or img_el.get("srcset") or "").strip()
            if not thumb:
                src_el = art.select_one("a.m-item-image picture.a-picture source[type='image/webp']")
                if src_el:
                    thumb = src_el.get("srcset", "").split()[0].rstrip(",").strip()

        seen_f24.add(url)
        france24_articles.append({"url": url, "title": title, "source": "France24", "thumb": thumb})
        added += 1

    # Also pick up carousel items
    for art in f24soup.select(".m-carousel-item"):
        title_el = art.select_one(".m-carousel-item__title a[href]")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href  = title_el.get("href", "").strip()
        if not href:
            continue
        url = href if href.startswith("http") else FRANCE24_BASE + href
        if not url or url in seen_f24:
            continue
        if any(ex in url for ex in FRANCE24_EXCLUDE):
            excluded += 1
            continue
        thumb = ""
        img_el = art.select_one("a.m-item-image picture.a-picture img.a-img")
        if img_el:
            thumb = (img_el.get("src") or img_el.get("srcset") or "").strip()
        seen_f24.add(url)
        france24_articles.append({"url": url, "title": title, "source": "France24", "thumb": thumb})
        added += 1

    info("France24: kept %d items, excluded %d", added, excluded)

# ------------------------------
# 3. COMBINE & DEDUPE
# ------------------------------

combined, seen_combined = [], set()
for item in reuters_articles + apnews_articles + france24_articles:
    u = item.get("url")
    if not u or u in seen_combined:
        continue
    seen_combined.add(u)
    combined.append(item)

all_articles = combined
info("Total unique articles to process: %d", len(all_articles))

# ------------------------------
# 4. LOAD XML EARLY (to skip already-known articles)
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
    "AP News + France24 Combined Feed",
    "https://evilgodfahim.github.io/reur/",
    "Combined scraped articles from AP News and France24",
)

reuters_tree, reuters_root, reuters_channel = load_or_create_xml(
    REUTERS_XML_FILE,
    "Reuters Feed",
    "https://evilgodfahim.github.io/reur/reuters",
    "Scraped articles from Reuters",
)

existing = {
    item.find("link").text.strip()
    for item in channel.findall("item")
    if item.find("link") is not None and item.find("link").text
}
reuters_existing = {
    item.find("link").text.strip()
    for item in reuters_channel.findall("item")
    if item.find("link") is not None and item.find("link").text
}
info("Existing items in main feed: %d", len(existing))
info("Existing items in Reuters feed: %d", len(reuters_existing))

# ------------------------------
# 5. FETCH FULL TEXT
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

reuters_fetch_count = 0
for i, a in enumerate(all_articles, 1):
    if a.get("source") == "APNews":
        continue

    # Skip articles already in the feed — no need to fetch them
    is_reuters = a.get("source") == "Reuters"
    if a["url"] in (reuters_existing if is_reuters else existing):
        continue

    info("Processing %d/%d [%s]: %s", i, len(all_articles), a.get("source"), a.get("title", "")[:80])

    # Periodically restart BotBrowser to reset DataDome fingerprint.
    if a.get("source") == "Reuters":
        reuters_fetch_count += 1
        if reuters_fetch_count > 1 and reuters_fetch_count % BOTBROWSER_RESTART_EVERY == 0:
            info("DataDome prevention: restarting BotBrowser after %d Reuters fetches", reuters_fetch_count)
            _start_botbrowser()
            time.sleep(3)

    page = fetch_page(a["url"])

    if page is None:
        warn("Failed to fetch: %s", a.get("url"))
        a["desc"] = ""
        a["img"]  = a.get("thumb", "") or ""
        a["pub"]  = now_utc()
        continue

    source = a.get("source")
    if source == "Reuters":
        a["desc"] = extract_full_text_reuters(page)
    else:
        a["desc"] = extract_full_text_france24(page)
    a["desc"] = a["desc"] or ""

    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page) or a.get("thumb", "") or ""
    a["pub"] = now_utc()

    desc_len = len(a["desc"])
    debug("  desc length: %d, img: %s", desc_len, (a["img"] or "")[:120])
    if desc_len == 0:
        warn("  Empty description for: %s\n  Page snippet: %s",
             a["url"], page[:DEBUG_HTML_SNIPPET_LEN].replace("\n", " "))

    # Throttle BotBrowser requests to reduce DataDome rate-limiting
    if a.get("source") == "Reuters":
        time.sleep(BOTBROWSER_FETCH_DELAY)

# Cleanup
flare_session_destroy()
_botbrowser_shutdown()

# ------------------------------
# 6. ADD NEW ARTICLES
# ------------------------------

new_count = reuters_new_count = 0
for art in all_articles:
    is_reuters = art.get("source") == "Reuters"
    target_channel  = reuters_channel if is_reuters else channel
    target_existing = reuters_existing if is_reuters else existing

    if art["url"] in target_existing:
        continue

    title = (art.get("title") or "").strip()
    desc  = (art.get("desc")  or "").strip()

    # Skip only if both title and description are empty
    if not title and not desc:
        warn("Skipping (no title or description): %s", art["url"])
        continue

    # If description is missing, fall back to the title
    if not desc:
        warn("No description for '%s' — using title as fallback", title[:60])
        desc = title

    item = ET.SubElement(target_channel, "item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = desc
    ET.SubElement(item, "pubDate").text     = art["pub"]
    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")

    if is_reuters:
        reuters_new_count += 1
    else:
        new_count += 1
    debug("Added: %s", art["url"])

info("Added %d new articles to main feed", new_count)
info("Added %d new articles to Reuters feed", reuters_new_count)

# ------------------------------
# 7. TRIM OLD ITEMS
# ------------------------------

for ch in (channel, reuters_channel):
    all_items = ch.findall("item")
    if len(all_items) > MAX_ITEMS:
        for old in all_items[:-MAX_ITEMS]:
            ch.remove(old)
        info("Trimmed feed to %d items", MAX_ITEMS)

# ------------------------------
# 8. SAVE XML
# ------------------------------

os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Main feed saved to %s", XML_FILE)
info("Done! Reuters feed saved to %s", REUTERS_XML_FILE)
info("Debug log saved to %s", LOG_FILENAME)



