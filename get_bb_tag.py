#!/usr/bin/env python3
import json
import sys
import os
import urllib.request

def fetch(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-actions",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

token = os.environ.get("GH_TOKEN", "")

urls_to_try = [
    "https://api.github.com/repositories/854084975/releases/latest",
    "https://api.github.com/repos/MiddleSchoolStudent/BotBrowser/releases/latest",
]

data = None
for url in urls_to_try:
    print(f"Trying: {url}", file=sys.stderr)
    try:
        data = fetch(url, token)
        if isinstance(data, dict) and "tag_name" in data:
            with open("/tmp/bb_release.json", "w") as f:
                json.dump(data, f)
            break
        elif isinstance(data, list) and data and "tag_name" in data[0]:
            data = data[0]
            with open("/tmp/bb_release.json", "w") as f:
                json.dump(data, f)
            break
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        data = None

if not data or "tag_name" not in data:
    # fallback: list endpoint
    for url in [
        "https://api.github.com/repositories/854084975/releases?per_page=5",
        "https://api.github.com/repos/MiddleSchoolStudent/BotBrowser/releases?per_page=5",
    ]:
        try:
            lst = fetch(url, token)
            if isinstance(lst, list) and lst and "tag_name" in lst[0]:
                data = lst[0]
                with open("/tmp/bb_release.json", "w") as f:
                    json.dump(data, f)
                break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

if not data or "tag_name" not in data:
    print("ERROR: Could not determine BotBrowser latest release tag.", file=sys.stderr)
    sys.exit(1)

tag = data["tag_name"]
assets = data.get("assets", [])

# Write asset names to file for the workflow to use
with open("/tmp/bb_assets.txt", "w") as f:
    for a in assets:
        f.write(a["name"] + "\n")

print(f"Tag: {tag}", file=sys.stderr)
print("Assets:", file=sys.stderr)
for a in assets:
    print(f"  - {a['name']}", file=sys.stderr)

# Print tag to stdout (captured by shell)
print(tag)