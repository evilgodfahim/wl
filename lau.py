#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape Reuters listing page via FlareSolverr, extract full article text using the
'[...]article-body-module__content__bnXL1' selector (and a sensible fallback),
and write/update a simple RSS XML file.

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
TARGET_URL = "https://www.reuters.com/world/"
HTML_FILE = "opinin.html"
XML_FILE = "pau.xml"
MAX_ITEMS = 500
BASE = "https://www.reuters.com"
TIMEOUT_MS = 60000


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
# Extract full article text
# ------------------------------
def extract_full_text(article_html):
    """
    Primary extraction: use the selector the user requested:
      <div class="article-body-module__content__bnXL1"> ... <div data-testid="paragraph-..."> ... </div>

    If that selector isn't present, fall back to common approaches:
     - 'div[data-testid="Body"] p'
     - 'article p'
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
# 1. FETCH LIST PAGE
# ------------------------------
html = flare_get(TARGET_URL)
if html is None:
    sys.exit(1)

with open(HTML_FILE, "w", encoding="utf-8") as f:
    f.write(html)

soup = BeautifulSoup(html, "html.parser")


# ------------------------------
# 2. PARSE LINKS FROM LISTING
# ------------------------------
articles = []

# Keep the original selector the user used earlier for Reuters listing
for blk in soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]'):
    href = blk.get("href", "").strip()
    if not href:
        continue

    if href.startswith("http"):
        url = href
    elif href.startswith("/"):
        url = BASE + href
    else:
        continue

    span = blk.select_one('span[data-testid="TitleHeading"]')
    if not span:
        # fallback: use link text
        title = blk.get_text(" ", strip=True)
    else:
        title = span.get_text(strip=True)

    if not title:
        continue

    articles.append({
        "url": url,
        "title": title
    })


# ------------------------------
# 3. FETCH FULL TEXT FOR EACH ARTICLE
# ------------------------------
for a in articles:
    page = flare_get(a["url"])
    if page is None:
        a["desc"] = ""
        a["img"] = ""
        a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        continue

    full_text = extract_full_text(page)
    a["desc"] = full_text

    soup_page = BeautifulSoup(page, "html.parser")
    a["img"] = extract_image_url(soup_page)
    a["pub"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


# ------------------------------
# 4. LOAD OR CREATE XML
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
    ET.SubElement(channel, "title").text = "Custom Feed"
    ET.SubElement(channel, "link").text = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Custom scraped articles"


# ------------------------------
# 5. DEDUPLICATE EXISTING ITEMS
# ------------------------------
existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None and link_tag.text:
        existing.add(link_tag.text.strip())


# ------------------------------
# 6. ADD NEW ARTICLES
# ------------------------------
for art in articles:
    if art["url"] in existing:
        continue

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = art["title"]
    ET.SubElement(item, "link").text = art["url"]

    # Description: keep as plain text (newlines will be escaped)
    ET.SubElement(item, "description").text = art["desc"] or ""

    ET.SubElement(item, "pubDate").text = art["pub"]

    if art.get("img"):
        # Use enclosure element for image
        # type left generic; many images are jpeg/png
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")


# ------------------------------
# 7. TRIM OLD ITEMS
# ------------------------------
all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    # remove oldest items first (assumes older items are earlier in the list)
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)


# ------------------------------
# 8. SAVE XML
# ------------------------------
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)

print("Done.")
