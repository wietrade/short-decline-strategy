#!/usr/bin/env python3
"""Test more TV API endpoints."""

import json
import urllib.request
from pathlib import Path

state_path = Path("i:/1H/data/tv_storage_state.json")
if not state_path.exists():
    print("No cookie file found")
    exit(1)

state = json.loads(state_path.read_text())
cookies = state.get("cookies", [])
cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
csrf = ""
for c in cookies:
    if c.get("name") in ("csrf_token", "X-CSRF-Token", "token", "csrftoken", "__CSRF"):
        csrf = c["value"]
        break

print(f"Cookies: {len(cookies)}")
print(f"CSRF: {'YES' if csrf else 'NO'}")
print()

headers = {
    "Content-Type": "application/json",
    "Cookie": cookie_str,
    "X-CSRF-Token": csrf,
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# Try different hosts and paths
hosts = [
    "https://www.tradingview.com",
    "https://api.tradingview.com",
    "https://tv-api.tradingview.com",
]

paths = [
    "/lists/",
    "/lists",
    "/v1/lists/",
    "/api/v1/lists/",
    "/rest-api/lists/",
    "/user/watchlist/",
    "/watchlist/",
    "/graphql",
]

for host in hosts:
    for path in paths:
        url = host + path
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read()[:200]
                print(f"  GET {url} -> HTTP {resp.status}")
                if body:
                    print(f"     Body: {body[:100]}")
        except urllib.request.HTTPError as e:
            err = e.read()[:100] if e.fp else b""
            print(f"  GET {url} -> HTTP {e.code}")
            if "is-not-authenticated" not in str(err) and err:
                print(f"     Body: {err[:100]}")
        except Exception:
            pass  # skip connection errors silently

print("\n--- Testing GraphQL ---")
gql = '{"query":"query { currentUser { id username } }"}'
try:
    req = urllib.request.Request(
        "https://www.tradingview.com/graphql",
        data=gql.encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
        print(f"  GraphQL -> {json.dumps(body, indent=2)[:300]}")
except urllib.request.HTTPError as e:
    err = e.read()[:300] if e.fp else b""
    print(f"  GraphQL -> HTTP {e.code}: {err[:200]}")
except Exception as e:
    print(f"  GraphQL -> {e}")
