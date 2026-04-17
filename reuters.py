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
TIMEOUT_MS             = 120000
BOTBROWSER_FETCH_DELAY = 1.5

DATADOME_MARKERS_LOWER = [
    "#cmsg{animation: a 1.5s;}",
    "#cmsg{animation:a 1.5s}",
    "captcha-delivery",
    "<title>just a moment...</title>",
    "datadome",
]

REUTERS_SKIP_PATHS  = ("/newsletters/", "/graphics/", "/live-blog/", "/podcast/", "/video/")
REUTERS_JUNK_TITLES = {"video", "live", "graphic", "graphics", "podcast"}

_ARTICLE_HREF_RE = re.compile(
    r"^/(world|article|business|markets|breakingviews|technology"
    r"|investigations|commentary|sustainability)/"
)
_COMMENTARY_HREF_RE = re.compile(
    r"^/(commentary|breakingviews|article|business|world|opinions)/"
)

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
        log.debug("BotBrowser profile: random from dir '%s'", BOTBROWSER_PROFILE_DIR)
    elif BOTBROWSER_PROFILE:
        args.append(f"--bot-profile={BOTBROWSER_PROFILE}")
        log.debug("BotBrowser profile: '%s'", BOTBROWSER_PROFILE)
    else:
        log.debug("BotBrowser profile: built-in default")
    return args


def botbrowser_get(url: str, retries: int = 3) -> str | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright is not installed.")
        return None

    if not os.path.isfile(BOTBROWSER_BINARY):
        log.warning("BotBrowser binary not found at '%s'.", BOTBROWSER_BINARY)
        return None

    launch_args = _build_launch_args()

    for attempt in range(1, retries + 1):
        log.debug("BotBrowser attempt %d/%d for %s", attempt, retries, url)
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
                            '[data-testid="Title"], [data-testid="Body"], '
                            'article, [data-testid="StoryCard"]',
                            timeout=5000,
                        )
                    except PWTimeout:
                        log.debug("Timeout waiting for content selectors. Proceeding.")
                    html = page.content()
                except PWTimeout:
                    log.warning("Navigation timed out for %s (attempt %d/%d)", url, attempt, retries)
                except Exception as e:
                    log.debug("goto/content error: %s", e)
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("BotBrowser outer error for %s (attempt %d/%d): %s", url, attempt, retries, e)
            time.sleep(1)
            continue

        if not html or len(html) < 500:
            log.warning("No usable HTML for %s (attempt %d/%d)", url, attempt, retries)
            time.sleep(1)
            continue

        html_lower = html.lower()
        if any(m in html_lower for m in DATADOME_MARKERS_LOWER):
            log.warning("DataDome CAPTCHA for %s (attempt %d/%d)", url, attempt, retries)
            time.sleep(2)
            continue

        log.debug("BotBrowser: %d bytes for %s", len(html), url)
        return html

    log.warning("BotBrowser: all %d attempts exhausted for %s", retries, url)
    return None


# ------------------------------
# Helpers
# ------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def save_debug_html(path: str, html: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log.debug("Saved HTML to %s (%d bytes)", path, len(html))
    except Exception as e:
        log.warning("Failed saving HTML %s: %s", path, e)


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
    url_path = url.split("reuters.com", 1)[-1].split("?")[0]
    if any(url_path.startswith(p) for p in REUTERS_SKIP_PATHS):
        return False
    if title and title.strip().lower() in REUTERS_JUNK_TITLES:
        return False
    last_segment = url_path.strip("/").split("/")[-1]
    if not last_segment:
        return False
    if last_segment.count("-") >= 3:
        return True
    if re.search(r"-\d{4}-\d{2}-\d{2}$", last_segment):
        return True
    if re.search(r"-id[A-Z0-9]{5,}$", last_segment, re.IGNORECASE):
        return True
    return False


def extract_full_text_reuters(article_html: str) -> str:
    s = BeautifulSoup(article_html, "html.parser")
    container = s.find("div", class_=re.compile(r"article-body-module__content__"))
    if container:
        parts = [
            p.get_text(" ", strip=True)
            for p in container.find_all(attrs={"data-testid": True})
            if "paragraph-" in (p.get("data-testid") or "")
        ]
        if parts:
            return "\n\n".join(parts)
    blocks = s.select('div[data-testid="Body"] p') or s.select("article p")
    return "\n\n".join(p.get_text(" ", strip=True) for p in blocks if p.get_text(" ", strip=True))


def extract_image_url(soup_page) -> str:
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


def extract_reuters_extra_cards(soup, page_url: str) -> list[dict]:
    results = []
    seen    = set()

    def _add(href, title, thumb=""):
        url   = build_full_url(href)
        title = (title or "").strip()
        if not url or url in seen or not is_valid_article_url(url, title):
            return
        seen.add(url)
        results.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})

    def _eager_image(el):
        img = el.select_one('img[data-testid="EagerImage"]')
        return (img.get("src") or img.get("data-src") or "").strip() if img else ""

    def _noscript_image(el):
        ns = el.select_one("noscript > img[src]")
        return ns.get("src", "").strip() if ns else ""

    for card in soup.select('[data-testid="BasicCard"]'):
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

    for card in soup.select('[data-testid="StoryCard"], [data-testid="FeedListItem"]'):
        link_el  = card.select_one('a[data-testid="TitleLink"]')
        title_el = card.select_one('[data-testid="TitleHeading"]')
        if link_el and title_el:
            _add(link_el.get("href", ""), title_el.get_text(" ", strip=True), _eager_image(card))

    log.debug("extract_reuters_extra_cards: %d items from %s", len(results), page_url)
    return results


def _fallback_anchor_scan(soup, href_pattern, seen: set) -> list[dict]:
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not (href_pattern.match(href) or "/article/" in href):
            continue
        title  = a.get_text(" ", strip=True) or (
            a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
        )
        url = build_full_url(href)
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

def scrape_world_page() -> list[dict]:
    log.info("Fetching Reuters world page: %s", REUTERS_URL)
    html = botbrowser_get(REUTERS_URL)
    if html is None:
        log.warning("Failed to fetch Reuters world page")
        return []

    save_debug_html(HTML_FILE, html)
    soup  = BeautifulSoup(html, "html.parser")
    seen  = set()
    items = []

    try:
        for blk in soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]'):
            href  = blk.get("href", "").strip()
            url   = build_full_url(href)
            span  = blk.select_one('span[data-testid="TitleHeading"]')
            title = span.get_text(" ", strip=True) if span else blk.get_text(" ", strip=True)
            if is_valid_article_url(url, title) and url not in seen:
                seen.add(url)
                items.append({"url": url, "title": title, "source": "Reuters"})
    except Exception as e:
        log.warning("Exception in primary world selector: %s", e)

    if items:
        log.info("Primary world selector matched %d items.", len(items))
        return items

    fallback = _fallback_anchor_scan(soup, _ARTICLE_HREF_RE, seen)
    log.info("Fallback anchor scan found %d candidates.", len(fallback))
    return fallback


def scrape_commentary_page() -> list[dict]:
    log.info("Fetching Reuters commentary page: %s", REUTERS_COMMENTARY_URL)
    html = botbrowser_get(REUTERS_COMMENTARY_URL)
    if html is None:
        log.warning("Failed to fetch Reuters commentary page")
        return []

    save_debug_html(COMMENTARY_HTML_FILE, html)
    soup  = BeautifulSoup(html, "html.parser")
    seen  = set()
    items = []

    try:
        for card in soup.select('[data-testid="StoryCard"]'):
            title_el = card.select_one('[data-testid="TitleHeading"]')
            link_el  = card.select_one('[data-testid="TitleLink"]')
            if not title_el or not link_el:
                continue
            title    = title_el.get_text(" ", strip=True)
            href     = link_el.get("href", "").strip()
            thumb_el = card.select_one(
                '[data-testid="MediaImageLink"] '
                '[data-testid="EagerImageContainer"] '
                'img[data-testid="EagerImage"]'
            )
            thumb = (thumb_el.get("src") or thumb_el.get("data-src") or "").strip() if thumb_el else ""
            url   = build_full_url(href)
            if is_valid_article_url(url, title) and url not in seen:
                seen.add(url)
                items.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})
    except Exception as e:
        log.warning("Exception in primary commentary selector: %s", e)

    if items:
        log.info("Commentary primary selector matched %d items.", len(items))
        return items

    fallback = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not (_COMMENTARY_HREF_RE.match(href) or "/article/" in href):
            continue
        title  = a.get_text(" ", strip=True) or (
            a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
        )
        url = build_full_url(href)
        if url and url not in seen and is_valid_article_url(url, title):
            seen.add(url)
            thumb  = ""
            parent = a.find_parent()
            if parent:
                img   = parent.find("img")
                thumb = (img.get("src") or img.get("data-src") or "").strip() if img else ""
            fallback.append({"url": url, "title": title, "source": "Reuters", "thumb": thumb})

    log.info("Commentary fallback scan found %d candidates.", len(fallback))
    return fallback


def scrape_extra_pages() -> list[dict]:
    items = []
    seen  = set()

    for extra_url in REUTERS_EXTRA_URLS:
        log.info("Fetching Reuters extra page: %s", extra_url)
        html = botbrowser_get(extra_url)
        if html is None:
            continue

        soup  = BeautifulSoup(html, "html.parser")
        cards = extract_reuters_extra_cards(soup, extra_url)

        if cards:
            for c in cards:
                if c["url"] not in seen:
                    seen.add(c["url"])
                    items.append(c)
        else:
            for item in _fallback_anchor_scan(soup, _ARTICLE_HREF_RE, seen):
                items.append(item)

        time.sleep(BOTBROWSER_FETCH_DELAY)

    return items


# ------------------------------
# MAIN
# ------------------------------

def main():
    seen_global  = set()
    all_articles = []

    def _add(batch):
        for item in batch:
            u = item.get("url")
            if u and u not in seen_global:
                seen_global.add(u)
                all_articles.append(item)

    _add(scrape_world_page())
    _add(scrape_commentary_page())
    _add(scrape_extra_pages())

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

        page = botbrowser_get(art["url"])
        if page is not None:
            art["desc"] = extract_full_text_reuters(page) or ""
            art["img"]  = extract_image_url(BeautifulSoup(page, "html.parser")) or art.get("thumb", "") or ""
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
        time.sleep(BOTBROWSER_FETCH_DELAY)

    all_items = reuters_channel.findall("item")
    for old in all_items[:-MAX_ITEMS]:
        reuters_channel.remove(old)

    reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
    log.info("Done! %d new articles saved to %s", new_count, REUTERS_XML_FILE)


if __name__ == "__main__":
    main()