#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Install: pip install "scrapling[fetchers]" && python -m camoufox fetch

import os
import re
import sys
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ------------------------------
# DEBUG / CONFIG
# ------------------------------

DEBUG        = True
LOG_FILENAME = "debug.log"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILENAME, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("scraper")

# ------------------------------
# CONFIGURATION
# ------------------------------

REUTERS_URL            = "https://www.reuters.com/world/"
REUTERS_COMMENTARY_URL = "https://www.reuters.com/commentary/"
REUTERS_EXTRA_URLS     = [
    "https://www.reuters.com/business/energy/",
    "https://www.reuters.com/business/environment/",
    "https://www.reuters.com/sustainability/climate-energy/",
    "https://www.reuters.com/sustainability/reuters-impact/",
]
HTML_FILE              = "opinin.html"
COMMENTARY_HTML_FILE   = "commentary.html"
REUTERS_XML_FILE       = "reuters.xml"
MAX_ITEMS              = 500
REUTERS_BASE           = "https://www.reuters.com"
TIMEOUT_MS             = 120_000
FETCH_DELAY            = 1.5

DATADOME_MARKERS_LOWER = [
    "#cmsg{animation: a 1.5s;}",
    "#cmsg{animation:a 1.5s}",
    "captcha-delivery",
    "<title>just a moment...</title>",
    "datadome",
]

REUTERS_SKIP_PATHS  = ("/newsletters/", "/graphics/", "/live-blog/", "/podcast/", "/video/")
REUTERS_JUNK_TITLES = {"video", "live", "graphic", "graphics", "podcast"}

_ARTICLE_HREF_RE    = re.compile(
    r"^/(world|article|business|markets|breakingviews|technology"
    r"|investigations|commentary|sustainability)/"
)
_COMMENTARY_HREF_RE = re.compile(
    r"^/(commentary|breakingviews|article|business|world|opinions)/"
)

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"


# ------------------------------
# Scrapling fetch helper
# ------------------------------

def scrapling_get(url: str, fetcher, retries: int = 3):
    """Fetch URL via StealthyFetcher. Returns a Scrapling page or None."""
    for attempt in range(1, retries + 1):
        log.debug("Fetch attempt %d/%d: %s", attempt, retries, url)
        try:
            page = fetcher.fetch(url, timeout=TIMEOUT_MS)
        except Exception as e:
            log.warning("Fetch error (%d/%d) %s: %s", attempt, retries, url, e)
            time.sleep(2)
            continue

        if not page or not hasattr(page, "html") or len(page.html) < 500:
            log.warning("Empty/tiny/invalid response (%d/%d): %s", attempt, retries, url)
            time.sleep(2)
            continue

        if any(m in page.html.lower() for m in DATADOME_MARKERS_LOWER):
            log.warning("DataDome detected (%d/%d): %s", attempt, retries, url)
            time.sleep(3)
            continue

        log.debug("OK: %d bytes for %s", len(page.html), url)
        return page

    log.warning("All %d attempts failed for %s", retries, url)
    return None


# ------------------------------
# Element helpers
# ------------------------------

def _text(el) -> str:
    """Safe text extraction from a Scrapling element."""
    if el is None:
        return ""
    try:
        return el.get_all_text(strip=True) or ""
    except Exception:
        return (getattr(el, "text", None) or "").strip()


def _attr(el, key: str, default: str = "") -> str:
    """Safe attribute extraction from a Scrapling element."""
    if el is None:
        return default
    return (el.attrib.get(key) or default).strip()


# ------------------------------
# Helpers
# ------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def save_debug_html(path: str, page) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.html)
        log.debug("Saved debug HTML: %s (%d bytes)", path, len(page.html))
    except Exception as e:
        log.warning("Failed to save debug HTML %s: %s", path, e)


def build_full_url(href, base=REUTERS_BASE) -> str | None:
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


def is_valid_article_url(url, title="") -> bool:
    if not url:
        return False
    if not re.match(r"https?://(?:www\.)?reuters\.com/", url):
        return False
    path = url.split("reuters.com", 1)[-1].split("?")[0]
    if any(path.startswith(p) for p in REUTERS_SKIP_PATHS):
        return False
    if title and title.strip().lower() in REUTERS_JUNK_TITLES:
        return False
    last = path.strip("/").split("/")[-1]
    if not last:
        return False
    if last.count("-") >= 3:
        return True
    if re.search(r"-\d{4}-\d{2}-\d{2}$", last):
        return True
    if re.search(r"-id[A-Z0-9]{5,}$", last, re.IGNORECASE):
        return True
    return False


# ------------------------------
# Page-level extractors
# ------------------------------

def extract_full_text_reuters(page) -> str:
    paragraphs = page.css(
        'div[class*="article-body-module__content__"] [data-testid*="paragraph-"]'
    )
    if paragraphs:
        parts = [_text(p) for p in paragraphs if _text(p)]
        if parts:
            return "\n\n".join(parts)

    blocks = page.css('div[data-testid="Body"] p') or page.css("article p")
    return "\n\n".join(_text(p) for p in blocks if _text(p))


def extract_image_url(page) -> str:
    meta = page.css_first('meta[property="og:image"]')
    if meta and _attr(meta, "content"):
        return _attr(meta, "content")

    link_img = page.css_first('link[rel="image_src"]')
    if link_img and _attr(link_img, "href"):
        return _attr(link_img, "href")

    for img in page.css("img"):
        src = _attr(img, "src") or _attr(img, "data-src") or _attr(img, "data-lazy-src")
        if src:
            return src

    return ""


def extract_reuters_extra_cards(page, page_url: str) -> list[dict]:
    results = []
    seen    = set()

    def _add(href, title, thumb=""):
        url   = build_full_url(href)
        title = (title or "").strip()
        if not url or url in seen or not is_valid_article_url(url, title):
            return
        seen.add(url)
        results.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})

    def _eager_img(el):
        img = el.css_first('img[data-testid="EagerImage"]')
        return (_attr(img, "src") or _attr(img, "data-src")) if img else ""

    def _noscript_img(el):
        ns = el.css_first("noscript > img[src]")
        return _attr(ns, "src") if ns else ""

    for card in page.css('[data-testid="BasicCard"]'):
        link_el = card.css_first('a[data-testid="Title"]')
        if link_el:
            _add(_attr(link_el, "href"), _text(link_el), _eager_img(card))

    for cell in page.css('li[data-testid="TalkingPointsCell"] > a[data-testid="MediaCard"]'):
        heading = cell.css_first('span[data-testid="MediaCardHeading"]')
        if heading:
            _add(_attr(cell, "href"), _text(heading), _noscript_img(cell))

    for card in page.css('a[data-testid="MediaCard"]'):
        heading = card.css_first('span[data-testid="MediaCardHeading"]')
        if heading:
            _add(_attr(card, "href"), _text(heading), _noscript_img(card))

    for card in page.css('[data-testid="StoryCard"], [data-testid="FeedListItem"]'):
        link_el  = card.css_first('a[data-testid="TitleLink"]')
        title_el = card.css_first('[data-testid="TitleHeading"]')
        if link_el and title_el:
            _add(_attr(link_el, "href"), _text(title_el), _eager_img(card))

    log.debug("extract_reuters_extra_cards: %d items from %s", len(results), page_url)
    return results


def _fallback_anchor_scan(page, href_pattern, seen: set) -> list[dict]:
    items = []
    for a in page.css("a[href]"):
        href = _attr(a, "href")
        if not (href_pattern.match(href) or "/article/" in href):
            continue
        title = _text(a)
        url   = build_full_url(href)
        if url and url not in seen and is_valid_article_url(url, title):
            seen.add(url)
            items.append({"url": url, "title": title, "source": "Reuters"})
    return items


# ------------------------------
# XML helpers
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


# ------------------------------
# Section scrapers
# ------------------------------

def scrape_world_page(fetcher) -> list[dict]:
    log.info("Fetching Reuters world: %s", REUTERS_URL)
    page = scrapling_get(REUTERS_URL, fetcher)
    if page is None:
        log.warning("Failed to fetch Reuters world page")
        return []

    save_debug_html(HTML_FILE, page)
    seen  = set()
    items = []

    try:
        for blk in page.css('div[data-testid="Title"] a[data-testid="TitleLink"]'):
            href  = _attr(blk, "href")
            url   = build_full_url(href)
            span  = blk.css_first('span[data-testid="TitleHeading"]')
            title = (_text(span) or _text(blk)).strip()
            if is_valid_article_url(url, title) and url not in seen:
                seen.add(url)
                items.append({"url": url, "title": title, "source": "Reuters"})
    except Exception as e:
        log.warning("Primary world selector error: %s", e)

    if items:
        log.info("Primary world selector: %d items", len(items))
        return items

    fallback = _fallback_anchor_scan(page, _ARTICLE_HREF_RE, seen)
    log.info("Fallback anchor scan: %d items", len(fallback))
    return fallback


def scrape_commentary_page(fetcher) -> list[dict]:
    log.info("Fetching Reuters commentary: %s", REUTERS_COMMENTARY_URL)
    page = scrapling_get(REUTERS_COMMENTARY_URL, fetcher)
    if page is None:
        log.warning("Failed to fetch Reuters commentary page")
        return []

    save_debug_html(COMMENTARY_HTML_FILE, page)
    seen  = set()
    items = []

    try:
        for card in page.css('[data-testid="StoryCard"]'):
            title_el = card.css_first('[data-testid="TitleHeading"]')
            link_el  = card.css_first('[data-testid="TitleLink"]')
            if not title_el or not link_el:
                continue
            title    = _text(title_el)
            href     = _attr(link_el, "href")
            thumb_el = card.css_first(
                '[data-testid="MediaImageLink"] '
                '[data-testid="EagerImageContainer"] '
                'img[data-testid="EagerImage"]'
            )
            thumb = (_attr(thumb_el, "src") or _attr(thumb_el, "data-src")) if thumb_el else ""
            url   = build_full_url(href)
            if is_valid_article_url(url, title) and url not in seen:
                seen.add(url)
                items.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})
    except Exception as e:
        log.warning("Primary commentary selector error: %s", e)

    if items:
        log.info("Commentary primary selector: %d items", len(items))
        return items

    fallback = []
    for a in page.css("a[href]"):
        href = _attr(a, "href")
        if not (_COMMENTARY_HREF_RE.match(href) or "/article/" in href):
            continue
        title  = _text(a)
        url    = build_full_url(href)
        if not (url and url not in seen and is_valid_article_url(url, title)):
            continue
        seen.add(url)
        thumb  = ""
        parent = a.parent
        if parent:
            img   = parent.css_first("img")
            thumb = (_attr(img, "src") or _attr(img, "data-src")) if img else ""
        fallback.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})

    log.info("Commentary fallback scan: %d items", len(fallback))
    return fallback


def scrape_extra_pages(fetcher) -> list[dict]:
    items = []
    seen  = set()

    for extra_url in REUTERS_EXTRA_URLS:
        log.info("Fetching extra page: %s", extra_url)
        page = scrapling_get(extra_url, fetcher)
        if page is None:
            continue

        cards = extract_reuters_extra_cards(page, extra_url)
        if cards:
            for c in cards:
                if c["url"] not in seen:
                    seen.add(c["url"])
                    items.append(c)
        else:
            for item in _fallback_anchor_scan(page, _ARTICLE_HREF_RE, seen):
                items.append(item)

        time.sleep(FETCH_DELAY)

    return items


# ------------------------------
# MAIN
# ------------------------------

def main():
    from scrapling.fetchers import StealthyFetcher

    seen_global  = set()
    all_articles = []

    def _add(batch):
        for item in batch:
            u = item.get("url")
            if u and u not in seen_global:
                seen_global.add(u)
                all_articles.append(item)

    fetcher = StealthyFetcher(headless=HEADLESS, network_idle=True)

    _add(scrape_world_page(fetcher))
    _add(scrape_commentary_page(fetcher))
    _add(scrape_extra_pages(fetcher))

    log.info("Total unique Reuters articles: %d", len(all_articles))

    reuters_tree, _, reuters_channel = load_or_create_xml(
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

    new_count = 0
    for art in all_articles:
        if art["url"] in reuters_existing:
            continue

        page = scrapling_get(art["url"], fetcher)
        if page is not None:
            art["desc"] = extract_full_text_reuters(page) or ""
            art["img"]  = extract_image_url(page) or art.get("thumb", "") or ""
        else:
            art["desc"] = ""
            art["img"]  = art.get("thumb", "") or ""
        art["pub"] = now_utc()

        title = (art.get("title") or "").strip()
        desc  = (art.get("desc")  or "").strip() or title
        if not title and not desc:
            continue

        el = ET.SubElement(reuters_channel, "item")
        ET.SubElement(el, "title").text       = title
        ET.SubElement(el, "link").text        = art["url"]
        ET.SubElement(el, "description").text = desc
        ET.SubElement(el, "pubDate").text     = art["pub"]
        if art.get("img"):
            ET.SubElement(el, "enclosure", url=art["img"], type="image/jpeg")

        new_count += 1
        time.sleep(FETCH_DELAY)

    # Trim to rolling cap
    all_items = reuters_channel.findall("item")
    for old in all_items[:-MAX_ITEMS]:
        reuters_channel.remove(old)

    reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
    log.info("Done! %d new articles saved to %s", new_count, REUTERS_XML_FILE)


if __name__ == "__main__":
    main()