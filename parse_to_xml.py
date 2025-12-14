import sys
import os
import time
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HTML_FILE = "opinion.html"
XML_FILE = "article.xml"
MAX_ITEMS = 500

FLARESOLVER_URL = "http://localhost:8191/v1"
BASE = "https://www.reuters.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# ------------------------------------------------------------
# Fetch HTML (FlareSolverr if available, else direct)
# ------------------------------------------------------------

def fetch_html(url):
    # --- Try FlareSolverr ---
    try:
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000
        }
        r = requests.post(FLARESOLVER_URL, json=payload, timeout=5)
        if r.ok:
            data = r.json()
            if isinstance(data, dict):
                sol = data.get("solution", {})
                if sol.get("response"):
                    return sol["response"]
    except Exception:
        pass

    # --- Fallback: direct request ---
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception:
        return None

# ------------------------------------------------------------
# Extract full Reuters article text (robust)
# ------------------------------------------------------------

def extract_full_article(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe", "aside"]):
        tag.decompose()

    # Primary Reuters structure
    body = soup.find("div", attrs={"data-testid": "article-body"})
    if body:
        paragraphs = [p.get_text(strip=True) for p in body.find_all("p") if p.get_text(strip=True)]
        if paragraphs:
            return "\n\n".join(paragraphs)

    # Fallback: itemprop
    body = soup.find("div", itemprop="articleBody")
    if body:
        paragraphs = [p.get_text(strip=True) for p in body.find_all("p") if p.get_text(strip=True)]
        if paragraphs:
            return "\n\n".join(paragraphs)

    # Last-resort heuristic: largest paragraph block
    best = ""
    best_len = 0

    for container in soup.find_all(["article", "div", "section", "main"]):
        paras = [p.get_text(strip=True) for p in container.find_all("p") if p.get_text(strip=True)]
        text = "\n\n".join(paras)
        if len(text) > best_len:
            best_len = len(text)
            best = text

    return best

# ------------------------------------------------------------
# Load listing HTML
# ------------------------------------------------------------

if not os.path.exists(HTML_FILE):
    print("HTML not found")
    sys.exit(1)

with open(HTML_FILE, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

articles = []

for blk in soup.select('div[data-testid="Title"] a[data-testid="TitleLink"]'):
    href = blk.get("href", "").strip()
    if not href:
        continue

    url = href if href.startswith("http") else BASE + href

    span = blk.select_one('span[data-testid="TitleHeading"]')
    title = span.get_text(strip=True) if span else blk.get_text(strip=True)
    if not title:
        continue

    html = fetch_html(url)
    if not html:
        continue

    full_text = extract_full_article(html)
    if not full_text:
        continue

    articles.append({
        "url": url,
        "title": title,
        "desc": full_text,
        "pub": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "img": ""
    })

# ------------------------------------------------------------
# Load or create XML
# ------------------------------------------------------------

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

existing = set(
    item.find("link").text
    for item in channel.findall("item")
    if item.find("link") is not None
)

for art in articles:
    if art["url"] in existing:
        continue

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = art["title"]
    ET.SubElement(item, "link").text = art["url"]
    ET.SubElement(item, "description").text = art["desc"]
    ET.SubElement(item, "pubDate").text = art["pub"]

# Trim
items = channel.findall("item")
if len(items) > MAX_ITEMS:
    for old in items[:-MAX_ITEMS]:
        channel.remove(old)

ET.ElementTree(root).write(XML_FILE, encoding="utf-8", xml_declaration=True)
