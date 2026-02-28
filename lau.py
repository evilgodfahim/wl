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
APNEWS_URL              = "https://apnews.com/world-news"
APNEWS_BASE             = "https://apnews.com"
FRANCE24_RSS            = "https://www.france24.com/en/rss"
HTML_FILE               = "opinin.html"
COMMENTARY_HTML_FILE    = "commentary.html"
APNEWS_HTML_FILE        = "apnews.html"
XML_FILE                = "pau.xml"
REUTERS_XML_FILE        = "reuters.xml"
MAX_ITEMS               = 500
REUTERS_BASE            = "https://www.reuters.com"
TIMEOUT_MS              = 120000

FRANCE24_EXCLUDE = ["/video/", "/live-news/", "/sport/", "/tv-shows/", "/sports/", "/videos/"]

# ------------------------------
# BotBrowser Configuration
# ------------------------------

BOTBROWSER_BINARY   = os.environ.get("BOTBROWSER_PATH", "./BotBrowser/dist/botbrowser")
BOTBROWSER_CDP_PORT = int(os.environ.get("BOTBROWSER_CDP_PORT", "9222"))
# CHANGED: default to a persistent local profile dir instead of empty string
BOTBROWSER_PROFILE  = os.environ.get("BOTBROWSER_PROFILE", "./botbrowser-profile")

DATADOME_SIGNALS = [
    "captcha-delivery.com",
    "geo.captcha",
    "ct.captcha-delivery.com",
    "'host':'geo.captcha",
]

def is_datadome_page(html: str) -> bool:
    if not html or len(html) > 5000:
        return False
    return any(sig in html for sig in DATADOME_SIGNALS)


_botbrowser_proc: subprocess.Popen | None = None


def _start_botbrowser() -> bool:
    """Launch a fresh BotBrowser process and wait for CDP to be ready."""
    global _botbrowser_proc

    if _botbrowser_proc is not None and _botbrowser_proc.poll() is None:
        debug("Killing existing BotBrowser (pid %d)", _botbrowser_proc.pid)
        _botbrowser_proc.kill()
        try:
            _botbrowser_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _botbrowser_proc = None

    if not os.path.isfile(BOTBROWSER_BINARY):
        warn("BotBrowser binary not found at '%s'.", BOTBROWSER_BINARY)
        return False

    # CHANGED: always create profile dir and always pass --user-data-dir
    os.makedirs(BOTBROWSER_PROFILE, exist_ok=True)

    cmd = [
        BOTBROWSER_BINARY,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        f"--remote-debugging-port={BOTBROWSER_CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--disable-blink-features=AutomationControlled",
        f"--user-data-dir={BOTBROWSER_PROFILE}",  # CHANGED: always use profile
    ]

    debug("Launching BotBrowser: %s", " ".join(cmd))
    try:
        _botbrowser_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        warn("Failed to launch BotBrowser: %s", e)
        return False

    cdp_url = f"http://127.0.0.1:{BOTBROWSER_CDP_PORT}/json/version"
    for _ in range(30):
        try:
            r = requests.get(cdp_url, timeout=2)
            if r.status_code == 200:
                debug("BotBrowser CDP ready on port %d", BOTBROWSER_CDP_PORT)
                return True
        except Exception:
            pass
        time.sleep(0.5)

    warn("BotBrowser CDP did not become ready within 15 s")
    return False


def _ensure_botbrowser_running() -> bool:
    """Start BotBrowser if not already running."""
    global _botbrowser_proc
    if _botbrowser_proc is not None and _botbrowser_proc.poll() is None:
        return True
    return _start_botbrowser()


def _botbrowser_fetch_once(url: str) -> str | None:
    """Single attempt to fetch a URL via BotBrowser + Playwright CDP."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        warn("playwright is not installed.")
        return None

    cdp_endpoint = f"http://127.0.0.1:{BOTBROWSER_CDP_PORT}"
    debug("BotBrowser GET via Playwright CDP: %s", url)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                java_script_enabled=True,
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            except PWTimeout:
                warn("BotBrowser navigation timed out for %s", url)
                page.close(); context.close(); browser.close()
                return None

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                debug("networkidle timed out for %s (non-fatal)", url)

            html = page.content()
            page.close(); context.close(); browser.close()

    except Exception as e:
        warn("BotBrowser Playwright error for %s: %s", url, e)
        return None

    if not html or len(html) < 500:
        warn("BotBrowser returned suspiciously short HTML (%d bytes) for %s", len(html), url)
        return None

    # CHANGED: detect DataDome before returning, treat as hard failure
    if is_datadome_page(html):
        warn("DataDome CAPTCHA detected for %s — skipping retry", url)
        return "DATADOME"

    debug("BotBrowser received %d bytes for %s", len(html), url)
    return html


def botbrowser_get(url: str, retries: int = 2) -> str | None:
    for attempt in range(1, retries + 1):
        if not _ensure_botbrowser_running():
            warn("BotBrowser not available (attempt %d/%d)", attempt, retries)
            time.sleep(2)
            continue

        if _botbrowser_proc is None or _botbrowser_proc.poll() is not None:
            warn("BotBrowser died before fetch attempt %d — restarting", attempt)
            if not _start_botbrowser():
                time.sleep(2)
                continue

        result = _botbrowser_fetch_once(url)

        # CHANGED: DataDome sentinel — don't retry, don't restart browser
        if result == "DATADOME":
            return None

        if result:
            return result

        if _botbrowser_proc is not None and _botbrowser_proc.poll() is not None:
            warn("BotBrowser process exited (code %d) after attempt %d — will restart",
                 _botbrowser_proc.returncode, attempt)
        else:
            warn("BotBrowser fetch failed on attempt %d — restarting for retry", attempt)

        if attempt < retries:
            _start_botbrowser()
            time.sleep(2)

    warn("BotBrowser: all %d attempts failed for %s", retries, url)
    return None


def _botbrowser_shutdown():
    global _botbrowser_proc
    if _botbrowser_proc is not None and _botbrowser_proc.poll() is None:
        debug("Shutting down BotBrowser (pid %d)", _botbrowser_proc.pid)
        _botbrowser_proc.terminate()
        try:
            _botbrowser_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _botbrowser_proc.kill()
        _botbrowser_proc = None


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
        debug("Routing to BotBrowser: %s", url)
        return botbrowser_get(url)
    else:
        debug("Routing to FlareSolverr: %s", url)
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

# ------------------------------
# 1B. FETCH REUTERS COMMENTARY  (BotBrowser)
# ------------------------------

info("Fetching Reuters commentary page via BotBrowser: %s", REUTERS_COMMENTARY_URL)
commentary_html = fetch_page(REUTERS_COMMENTARY_URL)

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
# 1C. FETCH AP NEWS WORLD  (FlareSolverr)
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
# 2. FETCH FRANCE24 RSS  (FlareSolverr)
# ------------------------------

info("Fetching France24 RSS via FlareSolverr: %s", FRANCE24_RSS)
rss_html = fetch_page(FRANCE24_RSS)
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
for item in reuters_articles + apnews_articles + france24_articles:
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
        debug("Skipping full fetch for AP News: %s", a.get("url"))
        continue

    info("Processing %d/%d [%s]: %s", i, len(all_articles), a.get("source"), a.get("title", "")[:80])

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

# Cleanup
flare_session_destroy()
_botbrowser_shutdown()

# ------------------------------
# 5. LOAD OR CREATE XML
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

# ------------------------------
# 6. DEDUPLICATE EXISTING
# ------------------------------

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
# 7. ADD NEW ARTICLES
# ------------------------------

new_count = reuters_new_count = 0
for art in all_articles:
    is_reuters = art.get("source") == "Reuters"
    target_channel  = reuters_channel if is_reuters else channel
    target_existing = reuters_existing if is_reuters else existing

    if art["url"] in target_existing:
        debug("Already exists, skipping: %s", art["url"])
        continue
    if art.get("source") != "APNews" and not (art.get("desc") or "").strip():
        warn("Skipping (no description): %s", art.get("title", "")[:60])
        continue

    item = ET.SubElement(target_channel, "item")
    ET.SubElement(item, "title").text       = art["title"]
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = art["desc"]
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
# 8. TRIM OLD ITEMS
# ------------------------------

for ch in (channel, reuters_channel):
    all_items = ch.findall("item")
    if len(all_items) > MAX_ITEMS:
        for old in all_items[:-MAX_ITEMS]:
            ch.remove(old)
        info("Trimmed feed to %d items", MAX_ITEMS)

# ------------------------------
# 9. SAVE XML
# ------------------------------

os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
info("Done! Main feed saved to %s", XML_FILE)
info("Done! Reuters feed saved to %s", REUTERS_XML_FILE)
info("Debug log saved to %s", LOG_FILENAME)
