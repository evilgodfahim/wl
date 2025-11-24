import requests
import sys
import os
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime

# ------------------------------
# CONSTANTS
# ------------------------------
FLARESOLVERR_URL = "http://localhost:8191/v1"
TARGET_URL = "https://www.reuters.com/world/"
HTML_FILE = "opinion.html"
XML_FILE = "pau.xml"
MAX_ITEMS = 500
BASE = "https://www.reuters.com"


# ------------------------------
# FUNCTION: call flaresolverr
# ------------------------------
def flare_get(url):
    """
    FlareSolverr wrapper.
    A wrapper is a small helper function that hides repeated logic.
    Returns raw HTML string.
    """
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000
    }

    r = requests.post(FLARESOLVERR_URL, json=payload)
    data = r.json()

    if "error" in data:
        print("FlareSolverr error:", data["error"])
        return None

    if "solution" not in data or "response" not in data["solution"]:
        print("Invalid FlareSolverr response:", data)
        return None

    return data["solution"]["response"]


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
        continue

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

def extract_full_text(article_html):
    """
    Extract readable article text.
    Most Reuters articles place text inside <p> tags under an <article> element.
    """
    s = BeautifulSoup(article_html, "html.parser")

    # Reuters uses content blocks inside <div data-testid="Body"> or <article>
    blocks = s.select('div[data-testid="Body"] p')
    if not blocks:
        blocks = s.select("article p")

    parts = []
    for p in blocks:
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)

    return "\n\n".join(parts)


for a in articles:
    page = flare_get(a["url"])
    if page is None:
        a["desc"] = ""
        a["img"] = ""
        continue

    full_text = extract_full_text(page)
    a["desc"] = full_text

    # optional image capture (simple)
    soup_page = BeautifulSoup(page, "html.parser")
    img_tag = soup_page.find("img")
    if img_tag and img_tag.get("src"):
        a["img"] = img_tag["src"]
    else:
        a["img"] = ""

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
    if link_tag is not None:
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
    ET.SubElement(item, "description").text = art["desc"]
    ET.SubElement(item, "pubDate").text = art["pub"]

    if art["img"]:
        ET.SubElement(item, "enclosure", url=art["img"], type="image/jpeg")


# ------------------------------
# 7. TRIM OLD ITEMS
# ------------------------------

all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old in all_items[:-MAX_ITEMS]:
        channel.remove(old)


# ------------------------------
# 8. SAVE XML
# ------------------------------

tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)

print("Done.")