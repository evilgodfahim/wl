#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Reuters World + Commentary listing pages via FlareSolverr,
extract full article text, and write/update a simple RSS XML file.

Requires a local FlareSolverr instance at FLARESOLVERR_URL.
Skips articles that have no description after scraping.
"""

import requests
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup

# ------------------------------
# CONFIGURATION
# ------------------------------

FLARESOLVERR_URL  = "http://localhost:8191/v1"
REUTERS_WORLD_URL = "https://www.reuters.com/world/"
REUTERS_COMM_URL  = "https://www.reuters.com/commentary/"
REUTERS_BASE      = "https://www.reuters.com"
HTML_FILE         = "opinin.html"
XML_FILE          = "pau.xml"
MAX_ITEMS         = 500
TIMEOUT_MS        = 60000

# ------------------------------
# FlareSolverr helper
# ------------------------------

def flare_get(url):
    """Call FlareSolverr to fetch rendered HTML. Returns HTML string or None."""
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
    Primary: find <div class="article-body-module__content__bnXL1">
    and collect all divs whose data-testid contains 'paragraph-'.
    Falls back to common Reuters selectors.
    """
    s = BeautifulSoup(article_html, "html.parser")

    # 1) Preferred container
    container = s.find("div", class_="article-body-module__content__bnXL1")
    if container:
        paragraphs = container.find_all(attrs={"data-testid": True})
        parts = []
        for p in paragraphs:
            dt = p.get("data-testid", "")
            if dt and "paragraph-" in dt:
                text = p.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)

    # 2) Fallback
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
# Helper: best image from article page
# ------------------------------

def extract_image_url(soup_page):
    """Return a representative image URL or empty string."""
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
# Parser: Reuters World page
# (original selector)
# ------------------------------

def parse_world_page(html):
    soup = BeautifulSoup(html, "html.parser")
    articles = []

    for blk in soup.select('a[data-testid="TitleLink"]'):
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
        title = span.get_text(strip=True) if span else blk.get_text(" ", strip=True)
        if not title:
            continue

        articles.append({
            "url":           url,
            "title":         title,
            "listing_thumb": "",
            "source":        "Reuters World"
        })

    return articles

# ------------------------------
# Parser: Reuters Commentary page
# (new StoryCard selectors)
# ------------------------------

def parse_commentary_page(html):
    soup = BeautifulSoup(html, "html.parser")
    articles = []

    for card in soup.select('[data-testid="StoryCard"]'):

        title_link = card.select_one('[data-testid="TitleLink"]')
        if not title_link:
            continue

        href = title_link.get("href", "").strip()
        if not href:
            continue

        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = REUTERS_BASE + href
        else:
            continue

        span = title_link.select_one('[data-testid="TitleHeading"]')
        title = span.get_text(strip=True) if span else title_link.get_text(" ", strip=True)
        if not title:
            continue

        # Thumbnail from listing card
        thumb_img = card.select_one(
            '[data-testid="MediaImageLink"] '
            '[data-testid="EagerImageContainer"] '
            'img[data-testid="EagerImage"]'
        )
        listing_thumb = ""
        if thumb_img:
            listing_thumb = (
                thumb_img.get("src") or
                thumb_img.get("data-src") or
                ""
            ).strip()

        articles.append({
            "url":           url,
            "title":         title,
            "listing_thumb": listing_thumb,
            "source":        "Reuters Commentary"
        })

    return articles

# ------------------------------
# 1. FETCH BOTH LISTING PAGES
# ------------------------------

all_articles = []
seen_urls = set()

# — World —
print("Fetching Reuters World articles...")
html_world = flare_get(REUTERS_WORLD_URL)
if html_world is None:
    print("Failed to fetch Reuters World page. Skipping.")
else:
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_world)
    world_articles = parse_world_page(html_world)
    for a in world_articles:
        if a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            all_articles.append(a)
    print(f"Found {len(world_articles)} Reuters World articles")

# — Commentary —
print("\nFetching Reuters Commentary articles...")
html_comm = flare_get(REUTERS_COMM_URL)
if html_comm is None:
    print("Failed to fetch Reuters Commentary page. Skipping.")
else:
    comm_articles = parse_commentary_page(html_comm)
    for a in comm_articles:
        if a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            all_articles.append(a)
    print(f"Found {len(comm_articles)} Reuters Commentary articles")

print(f"\nTotal unique articles: {len(all_articles)}")

# ------------------------------
# 2. FETCH FULL TEXT FOR EACH ARTICLE
# ------------------------------

print(f"\nFetching full text for {len(all_articles)} articles...")

for i, a in enumerate(all_articles, 1):
    print(f"[{a['source']}] Processing {i}/{len(all_articles)}: {a['title'][:60]}...")

    page = flare_get(a["url"])
    if page is None:
        a["desc"] = ""
        a["img"]  = a.get("listing_thumb", "")
        a["pub"]  = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        continue

    a["desc"] = extract_full_text_reuters(page)

    soup_page = BeautifulSoup(page, "html.parser")
    img = extract_image_url(soup_page)
    a["img"]  = img if img else a.get("listing_thumb", "")
    a["pub"]  = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

# ------------------------------
# 3. LOAD OR CREATE XML
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
    ET.SubElement(channel, "title").text       = "Reuters World Feed"
    ET.SubElement(channel, "link").text        = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Scraped articles from Reuters World"

# ------------------------------
# 4. DEDUPLICATE EXISTING ITEMS
# ------------------------------

existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None and link_tag.text:
        existing.add(link_tag.text.strip())

# ------------------------------
# 5. ADD NEW ARTICLES
# ------------------------------

new_count = 0
for art in all_articles:
    if art["url"] in existing:
        continue

    if not art.get("desc") or not art["desc"].strip():
        print(f"Skipping (no description): {art['title'][:60]}")
        continue

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text       = art["title"]
    ET.SubElement(item, "link").text        = art["url"]
    ET.SubElement(item, "description").text = art["desc"]
    ET.SubElement(item, "pubDate").text     = art["pub"]

    if art.get("img"):
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")

    new_count += 1

print(f"\nAdded {new_count} new articles to feed")

# ------------------------------
# 6. TRIM OLD ITEMS
# ------------------------------

all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)
    print(f"Trimmed to {MAX_ITEMS} items")

# ------------------------------
# 7. SAVE XML
# ------------------------------

os.makedirs(os.path.dirname(XML_FILE) or ".", exist_ok=True)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)

print(f"\nDone! Feed saved to {XML_FILE}")
