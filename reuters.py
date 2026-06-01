#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Install: pip install botbrowser
# Run: python3 scraper.py

import os
import re
import sys
import logging
import time
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from botbrowser import extract

# ------------------------------
# CONFIGURATION & EVASION
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

# CRITICAL FOR DATADOME: Route through an Anti-Bot Proxy network. 
# Set this in your environment or hardcode it here.
ANTI_BOT_PROXY = os.environ.get("ANTI_BOT_PROXY", "")
if ANTI_BOT_PROXY:
    log.info("Anti-Bot Proxy detected. Routing traffic for DataDome evasion.")
    os.environ["HTTP_PROXY"] = ANTI_BOT_PROXY
    os.environ["HTTPS_PROXY"] = ANTI_BOT_PROXY

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

# ------------------------------
# ROBUST FETCHING LOGIC
# ------------------------------

def fetch_with_retry(url: str, retries: int = 3) -> list:
    """Fetches URLs with jittered delays and exponential backoff for DataDome blocks."""
    for attempt in range(1, retries + 1):
        try:
            # Behavioral Evasion: Randomize request spacing to avoid ML rate-limit heuristics
            jitter = random.uniform(1.5, 4.5)
            time.sleep(jitter)
            
            result = extract(url, format="text", timeout=25000, include_links=True)
            
            # Safely extract links whether the API returns an object or a dict
            links = getattr(result, "links", [])
            if not links and isinstance(result, dict):
                links = result.get("links", [])
                
            if not links:
                log.warning(f"No links found on {url} (Attempt {attempt}/{retries}). Possible DataDome block.")
                time.sleep(attempt * 5)  # Exponential backoff
                continue
                
            log.debug(f"Success! Found {len(links)} raw links on {url}")
            return links
            
        except Exception as e:
            log.warning(f"Network/Extraction error on {url} (Attempt {attempt}/{retries}): {e}")
            time.sleep(attempt * 5)
            
    log.error(f"Failed to fetch {url} after {retries} attempts.")
    return []

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
        
    # Standard Reuters article slug signatures
    if last.count("-") >= 3 or re.search(r"-\d{4}-\d{2}-\d{2}$", last) or re.search(r"-id[A-Z0-9]{5,}$", last, re.IGNORECASE):
        return True
        
    return False

def build_full_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return REUTERS_BASE + href
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

# ------------------------------
# MAIN
# ------------------------------

def main():
    log.info("Starting BotBrowser pipeline with Anti-Bot resilience...")
    all_articles = {}

    # 1. Fetch URLs and Extract Valid Links
    for page_url in REUTERS_URLS:
        log.info(f"Targeting: {page_url}")
        links = fetch_with_retry(page_url)

        for link_data in links:
            raw_href = link_data.get("href", "")
            raw_title = link_data.get("text", "").strip()
            full_url = build_full_url(raw_href)
            
            if is_valid_article_url(full_url, raw_title) and full_url not in all_articles:
                all_articles[full_url] = raw_title

    log.info(f"Total unique Reuters articles extracted: {len(all_articles)}")

    # 2. Setup RSS XML Document
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

    # 3. Populate XML with new entries
    new_count = 0
    current_time = now_utc()
    
    for url, title in all_articles.items():
        if url in reuters_existing or not title:
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