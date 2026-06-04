#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import logging
import time
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from bs4 import BeautifulSoup

from invisible_playwright import InvisiblePlaywright

# ------------------------------
# CONFIGURATION
# ------------------------------

DEBUG = True
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

REUTERS_URLS = [
    "https://www.reuters.com/world/",
    "https://www.reuters.com/commentary/",
    "https://www.reuters.com/business/energy/",
    "https://www.reuters.com/business/environment/",
    "https://www.reuters.com/sustainability/climate-energy/",
    "https://www.reuters.com/sustainability/reuters-impact/",
]

REUTERS_XML_FILE = "reuters.xml"
MAX_ITEMS = 500
REUTERS_BASE = "https://www.reuters.com"

REUTERS_SKIP_PATHS = ("/newsletters/", "/graphics/", "/live-blog/", "/podcast/", "/video/")
REUTERS_JUNK_TITLES = {"video", "live", "graphic", "graphics", "podcast"}

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"

# ------------------------------
# HELPERS
# ------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def is_valid_article_url(url: str, title: str = "") -> bool:
    if not url or not re.match(r"https?://(?:www\.)?reuters\.com/", url):
        return False
    path = url.split("reuters.com", 1)[-1].split("?")[0]
    if any(path.startswith(p) for p in REUTERS_SKIP_PATHS):
        return False
    if title and title.strip().lower() in REUTERS_JUNK_TITLES:
        return False
    last = path.strip("/").split("/")[-1]
    if not last:
        return False
    if last.count("-") >= 3 or re.search(r"-\d{4}-\d{2}-\d{2}$", last) or re.search(r"-id[A-Z0-9]{5,}$", last, re.IGNORECASE):
        return True
    return False

def build_full_url(href: str) -> str:
    href = (href or "").strip()
    if not href: return ""
    if href.startswith("http"): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"): return REUTERS_BASE + href
    return ""

def load_or_create_xml(path: str, title: str, link: str, description: str):
    if os.path.exists(path):
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            channel = root.find("channel")
            if channel is not None:
                return tree, root, channel
        except ET.ParseError:
            pass
    root = ET.Element("rss", version="2.0")
    tree = ET.ElementTree(root)
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    return tree, root, channel

def fetch_page_html(browser, url: str, retries: int = 3):
    """Fetch URL via Invisible Playwright. Retries on failures or DataDome blocks."""
    for attempt in range(1, retries + 1):
        log.info(f"Fetching {url} (Attempt {attempt}/{retries})")
        page = None
        try:
            # Jitter to avoid ML timing heuristics
            time.sleep(random.uniform(2.0, 5.0))
            
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Brief pause to let frontend JS frameworks mount
            page.wait_for_timeout(3000) 
            
            html = page.content()
            
            if "datadome" in html.lower() or "just a moment..." in html.lower():
                log.warning(f"DataDome block detected on {url}. Backing off...")
                page.close()
                time.sleep(attempt * 10)
                continue
                
            page.close()
            return html
            
        except Exception as e:
            log.warning(f"Fetch error: {e}")
            if page:
                try:
                    page.close()
                except:
                    pass
            time.sleep(attempt * 5)
    return ""

# ------------------------------
# MAIN
# ------------------------------

def main():
    log.info("Starting up Invisible Playwright pipeline...")
    all_articles = {}

    # Initialize the C++ patched anti-detect browser
    with InvisiblePlaywright(headless=HEADLESS) as browser:
        
        # 1. Fetch URLs and Extract Links
        for page_url in REUTERS_URLS:
            html = fetch_page_html(browser, page_url)
            if not html:
                continue
                
            # Parse the extracted DOM with BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            
            for a_tag in soup.find_all("a", href=True):
                raw_href = a_tag.get("href", "").strip()
                raw_title = a_tag.get_text(strip=True)
                full_url = build_full_url(raw_href)

                if is_valid_article_url(full_url, raw_title) and full_url not in all_articles:
                    if len(raw_title) > 5: 
                        all_articles[full_url] = raw_title

    log.info(f"Total unique Reuters articles found: {len(all_articles)}")

    # 2. Setup RSS XML Document
    reuters_tree, _, reuters_channel = load_or_create_xml(
        REUTERS_XML_FILE,
        "Reuters Feed",
        "https://evilgodfahim.github.io/wl/reuters",
        "Scraped articles from Reuters",
    )

    reuters_existing = {
        item.find("link").text.strip()
        for item in reuters_channel.findall("item")
        if item.find("link") is not None and item.find("link").text
    }

    # 3. Populate XML
    new_count = 0
    current_time = now_utc()
    
    for url, title in all_articles.items():
        if url in reuters_existing:
            continue

        el = ET.SubElement(reuters_channel, "item")
        ET.SubElement(el, "title").text = title
        ET.SubElement(el, "link").text = url
        ET.SubElement(el, "description").text = title
        ET.SubElement(el, "pubDate").text = current_time
        new_count += 1

    # 4. Trim to rolling cap
    all_items = reuters_channel.findall("item")
    for old in all_items[:-MAX_ITEMS]:
        reuters_channel.remove(old)

    # 5. Save State
    reuters_tree.write(REUTERS_XML_FILE, encoding="utf-8", xml_declaration=True)
    log.info(f"Done! {new_count} new articles saved to {REUTERS_XML_FILE}")

if __name__ == "__main__":
    main()