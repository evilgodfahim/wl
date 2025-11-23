import requests
import sys

FLARESOLVERR_URL = "http://localhost:8191/v1"
TARGET_URL = "https://www.reuters.com/world/"

payload = {
    "cmd": "request.get",
    "url": TARGET_URL,
    "maxTimeout": 60000
}

r = requests.post(FLARESOLVERR_URL, json=payload)
data = r.json()

# If FlareSolverr returns an error field, expose it
if "error" in data:
    print("FlareSolverr error:", data["error"])
    sys.exit(1)

# If FlareSolverr fails silently
if "solution" not in data or "response" not in data["solution"]:
    print("Invalid FlareSolverr response:", data)
    sys.exit(1)

html = data["solution"]["response"]

with open("opinion.html", "w", encoding="utf-8") as f:
    f.write(html)
