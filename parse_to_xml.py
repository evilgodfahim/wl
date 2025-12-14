import sys
import os
import json
import time
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime

HTML_FILE = "opinion.html"
XML_FILE = "article.xml"
MAX_ITEMS = 500

FLARESOLVER_URL = "http://localhost:8191/v1"      # FlareSolverr endpoint
BASE = "https://www.reuters.com"                  # base used for relative URLs

# Configuration: adjust if needed
FSR_RETRIES = 3
FSR_TIMEOUT = 70          # seconds for the HTTP call to FlareSolverr
FSR_BACKOFF = 2           # base backoff multiplier
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

# -------------------------
# FlareSolverr fetch (robust)
# -------------------------
def fetch_with_flaresolver(url, retries=FSR_RETRIES, timeout=FSR_TIMEOUT):
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": int(timeout * 1000),
        "headers": {"User-Agent": USER_AGENT},
    }

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(FLARESOLVER_URL, json=payload, timeout=timeout + 5)
            r.raise_for_status()
            data = r.json()

            # FlareSolverr typical response: {"status":"ok","solution":{"response":"<html>...</html>"}}
            if isinstance(data, dict):
                sol = data.get("solution") or data.get("data") or {}
                if isinstance(sol, dict) and sol.get("response"):
                    return sol.get("response")
                # Some setups return 'response' at top level
                if data.get("response"):
                    return data.get("response")
            # If we reach here, not the expected shape
            # Try to get 'text' field if present
            if 'text' in data:
                return data['text']
        except Exception as e:
            # silent but informative prints for debugging
            print(f"[FlareSolverr] attempt {attempt} failed for {url!r}: {e}", file=sys.stderr)

        # backoff before next attempt (don't sleep after last attempt)
        if attempt < retries:
            time.sleep(FSR_BACKOFF ** attempt)

    return None

# -------------------------
# Article extraction helpers
# -------------------------
def clean_soup(soup):
    # remove scripts, noscript, styles, ads, and hidden nodes
    for tagname in ("script", "style", "noscript", "iframe", "header", "footer", "aside"):
        for t in soup.find_all(tagname):
            t.decompose()
    # remove common ad markers
    for attr in ("ad", "adsbygoogle", "advert", "promo"):
        for t in soup.select(f'[class*="{attr}"]'):
            t.decompose()
    return soup

def extract_from_selectors(soup):
    """
    Try multiple precise selectors. Return plaintext paragraphs joined by double newline.
    """
    selectors = [
        'div[data-testid="article-body"]',
        'div[data-testid="ArticleBody"]',
        'article',
        'div[itemprop="articleBody"]',
        'div[class*="ArticleBody"]',
        'div[class*="article-body"]',
        'section[class*="article-body"]',
        'div[class*="StoryBodyCompanionColumn"]',
        'div[id*="article"]',
        'main',
    ]

    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            paras = [p.get_text(strip=True) for p in node.find_all("p") if p.get_text(strip=True)]
            if len(paras) >= 2:   # require at least 2 paragraphs to consider valid
                return "\n\n".join(paras)
    return None

def extract_largest_p_block(soup):
    """
    Heuristic fallback: find the tag (article/div/section) that has the largest combined <p> text length.
    Returns plaintext paragraphs joined by double newline.
    """
    candidates = soup.find_all(['article', 'div', 'section', 'main'])
    best_text = ""
    best_len = 0

    for c in candidates:
        # skip very small containers
        ps = c.find_all("p")
        if not ps:
            continue
        combined = []
        total_len = 0
        for p in ps:
            t = p.get_text(strip=True)
            if t:
                combined.append(t)
                total_len += len(t)
        if total_len > best_len:
            best_len = total_len
            best_text = "\n\n".join(combined)

    # require some minimum text length to accept
    if best_len > 200:
        return best_text
    return ""

def extract_full_article(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    clean_soup(soup)

    # 1) try precise selectors
    txt = extract_from_selectors(soup)
    if txt and len(txt) > 100:
        return txt

    # 2) fallback heuristic: largest paragraph block
    txt2 = extract_largest_p_block(soup)
    if txt2:
        return txt2

    # 3) as last resort, gather all top-level <p> in body
    body = soup.body
    if body:
        paras = [p.get_text(strip=True) for p in body.find_all("p") if p.get_text(strip=True)]
        if paras:
            combined = "\n\n".join(paras)
            if len(combined) > 50:
                return combined

    return ""  # nothing found

# -------------------------
# Load listing HTML and find article links
# -------------------------
if not os.path.exists(HTML_FILE):
    print("HTML not found", file=sys.stderr)
    sys.exit(1)

with open(HTML_FILE, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

articles = []

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
        # fallback to link text
        title = blk.get_text(strip=True)
    else:
        title = span.get_text(strip=True)

    if not title:
        continue

    # ----- fetch full page via FlareSolverr -----
    html = fetch_with_flaresolver(url)
    if not html:
        print(f"[Warning] could not fetch {url}", file=sys.stderr)
        full_text = ""
    else:
        full_text = extract_full_article(html)
        if not full_text:
            print(f"[Warning] extracted empty article for {url}", file=sys.stderr)

    articles.append({
        "url": url,
        "title": title,
        "desc": full_text,
        "pub": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "img": ""
    })

# -------------------------
# Load or create XML
# -------------------------
if os.path.exists(XML_FILE):
    try:
        tree = ET.parse(XML_FILE)
        root = tree.getroot()
    except ET.ParseError:
        root = ET.Element("rss", version="2.0")
else:
    root = ET.Element("rss", version="2.0")

channel = root.find("channel")
if channel is None:
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Custom Feed"
    ET.SubElement(channel, "link").text = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Custom scraped articles"

# -------------------------
# Deduplicate existing URLs
# -------------------------
existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None and link_tag.text:
        existing.add(link_tag.text.strip())

# -------------------------
# Append new unique articles
# -------------------------
for art in articles:
    if not art["desc"]:
        # still append if you want to keep record, but current logic skips empty descriptions to avoid noise
        # To append empty descriptions, remove this continue.
        continue

    if art["url"] in existing:
        continue

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = art["title"]
    ET.SubElement(item, "link").text = art["url"]
    # description as plain text (no HTML). XML writer will escape special chars.
    ET.SubElement(item, "description").text = art["desc"]
    ET.SubElement(item, "pubDate").text = art["pub"]

    if art["img"]:
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")

# -------------------------
# Trim to last MAX_ITEMS
# -------------------------
all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old_item in all_items[:-MAX_ITEMS]:
        channel.remove(old_item)

# -------------------------
# Save XML
# -------------------------
tree = ET.ElementTree(root)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
print("Saved", XML_FILE)
