import sys
import os
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime

HTML_FILE = "opinion.html"
XML_FILE = "article.xml"
MAX_ITEMS = 500

# Load HTML
if not os.path.exists(HTML_FILE):
    print("HTML not found")
    sys.exit(1)

with open(HTML_FILE, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

articles = []

# ------------------------------------------------------------
# Reuters-style blocks
# ------------------------------------------------------------

BASE = "https://www.reuters.com"

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
        "title": title,
        "desc": "",
        "pub": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000"),
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

# Ensure channel exists
channel = root.find("channel")
if channel is None:
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Custom Feed"
    ET.SubElement(channel, "link").text = "https://evilgodfahim.github.io/reur/"
    ET.SubElement(channel, "description").text = "Custom scraped articles"

# Deduplicate existing URLs
existing = set()
for item in channel.findall("item"):
    link_tag = item.find("link")
    if link_tag is not None:
        existing.add(link_tag.text.strip())

# Append new unique articles
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

# Trim to last MAX_ITEMS
all_items = channel.findall("item")
if len(all_items) > MAX_ITEMS:
    for old_item in all_items[:-MAX_ITEMS]:
        channel.remove(old_item)

# Save XML
tree = ET.ElementTree(root)
tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
