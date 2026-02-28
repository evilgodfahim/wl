#!/usr/bin/env python3
import json

with open("/tmp/bb_release.json") as f:
    d = json.load(f)

assets = d.get("assets", [])
print(f"Available assets for {d.get('tag_name', '?')}:")
for a in assets:
    print(" -", a["name"])