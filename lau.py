#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape Reuters listing page + France24 RSS feed via FlareSolverr, 
extract full article text, and write/update a simple RSS XML file.

Save as a single file and run with a local FlareSolverr instance available at FLARESOLVERR_URL.
"""

import requests
import sys
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup

# ------------------------------
# CONFIGURATION
# ------------------------------
FLARESOLVERR_URL = "http://localhost:8191/v1"
REUTERS_URL = "https://www.reuters.com/world/"
FRANCE24_RSS = "https://www.france24.com/en/rss"
HTML_FILE = "opinin.html"
XML_FILE = "pau.xml"
MAX_ITEMS = 500
REUTERS_BASE = "https://www.reuters.com"
TIMEOUT_MS = 60000

# France24 exclusion patterns
FRANCE24_EXCLUDE = ["/video/", "/live-news/", "/sport/"]


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

    try:
        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=30)
    except Exception as e:
        print("Request error:", e)
        return None

    if r.status_code != 200:
        print("FlareSolverr returned HTTP", r.status_code)
        try:
            print("Response:", r.text[:400])
        except Exception:
            pass
        return None

    try:
        data = r.json()
    except Exception as e:
        print("Invalid JSON from FlareSolverr:", e)
        return None

    # Basic error checks
    if "error" in data:
        print("FlareSolverr error:", data["error"])
        return None

    sol = data.get("solution")
    if not sol:
        print("No 'solution' in FlareSolverr response.")
        return None

    resp = sol.get("response")
    if resp is None:
        print("No 'response' in FlareSolverr solution.")
        return None

    # FlareSolverr may return response as a dict with 'data' or as the raw HTML string
    if isinstance(resp, dict):
        html = resp.get("data") or resp.get("body") or resp.get("html")
    else:
        html = resp

    if not html:
        print("Empty HTML in FlareSolverr response.")
        return None

    return html


# ------------------------------
# Extract full text - Reuters
# ------------------------------
def extract_full_text_reuters(article_html):
    """
    Primary extraction: use the selector for Reuters:
      <div class="article-body-module__content__bnXL1"> ... <div data-testid="paragraph-..."> ... </div>

    If that selector isn't present, fall back to common approaches.
    """
    s = BeautifulSoup(article_html, "html.parser")

    # 1) Preferred container and paragraph structure (user-provided selector)
    container = s.find("div", class_="article-body-module__content__bnXL1")
    if container:
        # find divs with a data-testid attribute, filter those whose data-testid contains 'paragraph-'
        paragraphs = container.find_all(attrs={"data-testid": True})
        parts = []
        for p in paragraphs:
            dt = p.get("data-testid", "") or p.attrs.get("data_testid", "")
            # normalize: ensure we look for substring 'paragraph-'
            if dt and "paragraph-" in dt:
                text = p.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)

    # 2) Fallback: Reuters common body selectors
    blocks = s.select('div[data-testid="Body"] p')
    if not blocks:
        blocks = s.select("article p")

    parts = []
    for p in blocks:
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)

    return "\n\n".join(parts)


# ------------------------------
# Extract full text - France24
# ------------------------------
def extract_full_text_france24(article_html):
    """
    Extract article text from France24 using the specific div:
      <div class="t-content__body u-clearfix">
    """
    s = BeautifulSoup(article_html, "html.parser")

    # Find the main content div
    container = s.find("div", class_="t-content__body")
    if not container:
        # Try alternative selector
        container = s.find("div", class_=lambda c: c and "t-content__body" in c)
    
    if container:
        # Extract all paragraphs from this container
        paragraphs = container.find_all("p")
        parts = []
        for p in paragraphs:
            text = p.get_text(" ", strip=True)
            # Filter out "Read more" and other non-content paragraphs
            if text and not text.startswith("Read more") and not text.startswith("(FRANCE 24"):
                parts.append(text)
        if parts:
            return "\n\n".join(parts)

    # Fallback: try to find article content by common selectors
    blocks = s.select("article p")
    parts = []
    for p in blocks:
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)

    return "\n\n".join(parts)


# ------------------------------
# Helper: get best image from article page
# ------------------------------
def extract_image_url(soup_page):
    """
    Try to find a representative image for the article.
    Strategies:
      - og:image meta tag
      - first <img> with a usable src
      - img from schema or figure tags
    Returns empty string if none found.
    """
    # 1) Open Graph
    meta_og = soup_page.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        return meta_og["content"].strip()

    # 2) link rel=image_src
    link_img = soup_page.find("link", rel="image_src")
    if link_img and link_img.get("href"):
        return link_img["href"].strip()

    # 3) first <img> with src or data-src
    for img in soup_page.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and src.strip():
            return src.strip()

    return ""


# ------------------------------
# 1. FETCH REUTERS LIST PAGE
# ------------------------------
print("Fetching Reuters articles...")
html = flare_get(REUTERS_URL)
if html is None:
    print("Failed to fetch Reuters page")
    reuters_articles = []
else:
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    soup = BeautifulSoup(html, "html.parser")

    # Parse links from Reuters listing
    reuters_articles = []
    for blk in soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]'):
        href = blk.get("href", "").strip()
        if not href:
            continue

        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = REUTERS_BASE + href
        else:
            continue

        span = blk.select_one('span[data-testid="TitleHeading"]')
        if not span:
            title = blk.get_text(" ", strip=True)
        else:
            title = span.get_text(strip=True)

        if not title:
            continue

        reuters_articles.append({
            "url": url,
            "title": title,
            "source": "Reuters"
        })

    print(f"Found {len(reuters_articles)} Reuters articles")


# ------------------------------
# 2. FETCH FRANCE24 RSS FEED
# ------------------------------
print("Fetching France24 RSS feed...")
rss_html = flare_get(FRANCE24_RSS)
if rss_html is None:
    print("Failed to fetch France24 RSS")
    france24_articles = []
else:
    rss_soup = BeautifulSoup(rss_html, "html.parser")
    
    france24_articles = []
    for item in rss_soup.find_all("item"):
        link_tag = item.find("link")
        title_tag = item.find("title")
        
        if not link_tag or not title_tag:
            continue
        
        url = link_tag.get_text(strip=True)
        title = title_tag.get_text(strip=True)
        
        # Filter out excluded categories
        if any(exclude in url for exclude in FRANCE24_EXCLUDE):
            continue
        
        france24_articles.append({
            "url": url,
            "title": title,
            "source": "France24"
        })
    
    print(f"Found {len(france24_articles)} France24 articles (after filtering)")


# ------------------------------
# 3. COMBINE ALL ARTICLES
# ------------------------------
all_articles = reuters_articles + france24_articles
print(f"\nTotal articles to process: {len(all_articles)}")


# ------------------------------
# 4. FETCH FULL TEXT FOR EACH ARTICLE
# ------------------------------
for i, a in enumerate(all_articles, 1):
    print(f"Processing {i}/{len(all_articles)}: {a['title'][:50]}...")
    
    page = flare_get(a["url"])
    if page is None:
        a["desc"] = ""
        a["img"] = ""
        a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        continue

    # Extract text based on source
    if a["source"] == "Reuters":
        full_text = extract_full_text_reuters(page)
    else:  # France24
        full_text = extract_full_text_france24(page)
    
    a["desc"] = full_text

    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page)
    a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


# ------------------------------
# 5. LOAD OR CREATE XML
# ------------------------------
if os.path.exists(XML_FILE):
    try:
        tree = ET.parse(XML_FILE)
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
    ET.SubElement(channel, "title").text = "Reuters + France24 Combined Feed"
    ET.SubElement(channel, "link").text = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Combined scraped articles from Reuters and France24"


# ------------------------------
# 6. DEDUPLICATE EXISTING ITEMS
# ------------------------------
existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None and link_tag.text:
        existing.add(link_tag.text.strip())


# ------------------------------
# 7. ADD NEW ARTICLES
# ------------------------------
new_count = 0
for art in all_articles:
    if art["url"] in existing:
        continue

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = art["title"]
    ET.SubElement(item, "link").text = art["url"]
    ET.SubElement(item, "description").text = art["desc"] or ""
    ET.SubElement(item, "pubDate").text = art["pub"]

    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")
    
    new_count += 1

print(f"\nAdded {new_count} new articles to feed")


# ------------------------------
# 8. TRIM OLD ITEMS
# ------------------------------
all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)
    print(f"Trimmed to {MAX_ITEMS} items")


# ------------------------------
# 9. SAVE XML
# ------------------------------
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)

print(f"\nDone! Feed saved to {XML_FILE}")
