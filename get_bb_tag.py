#!/usr/bin/env python3
import json
import sys

# Try /releases/latest response first
try:
    with open("/tmp/bb_release.json") as f:
        d = json.load(f)
except Exception as e:
    print(f"ERROR reading /tmp/bb_release.json: {e}", file=sys.stderr)
    sys.exit(1)

print("=== Raw API response (first 300 chars) ===", file=sys.stderr)
print(str(d)[:300], file=sys.stderr)

if isinstance(d, dict) and "tag_name" in d:
    print(d["tag_name"])
    sys.exit(0)

# Fallback: try list response
try:
    with open("/tmp/bb_releases_list.json") as f:
        lst = json.load(f)
    if isinstance(lst, list) and len(lst) > 0 and "tag_name" in lst[0]:
        # overwrite bb_release.json with first item for later steps
        with open("/tmp/bb_release.json", "w") as out:
            json.dump(lst[0], out)
        print(lst[0]["tag_name"])
        sys.exit(0)
    print(f"ERROR: list response has no usable releases: {str(lst)[:200]}", file=sys.stderr)
except Exception as e:
    print(f"ERROR reading list fallback: {e}", file=sys.stderr)

print(f"ERROR: tag_name not found. API said: {d.get('message', str(d)[:200])}", file=sys.stderr)
sys.exit(1)