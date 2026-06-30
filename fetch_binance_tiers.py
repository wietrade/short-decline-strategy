#!/usr/bin/env python3
"""Fetch Binance Futures leverage tiers and update the JSON tiers file."""

import json
import urllib.request

TIERS_FILE = "binance_leverage_tiers.json"


def fetch_exchange_info():
    """Get symbol info from Binance Futures public API."""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    print(f"Fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print(f"Got {len(data.get('symbols', []))} symbols")
    return data


def fetch_leverage_bracket(symbol):
    """Fetch leverage bracket for a specific symbol (needs API key)."""
    # This endpoint requires API key - won't work without one
    url = f"https://fapi.binance.com/fapi/v1/leverageBracket?symbol={symbol}"
    print(f"  {url} -> needs API key, skipping")
    return None


def main():
    # 1. Fetch exchangeInfo
    info = fetch_exchange_info()

    # Check if exchangeInfo has leverage info embedded
    symbols = info.get("symbols", [])

    # Look for leverageBracket data in exchangeInfo
    sample = symbols[0] if symbols else {}
    print(f"\nSample symbol keys: {list(sample.keys())}")

    # Check if there's any leverage/tier info
    for key in ["leverageBracket", "tier", "leverage", "bracket", "margin"]:
        if key in sample:
            print(f"  Found '{key}' in symbol data!")

    # Try the /fapi/v1/leverageBracket public endpoint (some endpoints allow it)
    # Actually this requires API key, so let's check other approaches

    # Save exchangeInfo for inspection
    with open("/tmp/exchange_info_sample.json", "w") as f:
        json.dump(sample, f, indent=2)
    print("\nSample saved to /tmp/exchange_info_sample.json")

    # Let's see if there's any tier information in the symbol data
    # Check specific fields
    for field in ["filters", "permissions", "marginType", "leverage", "brackets"]:
        if field in sample:
            val = sample[field]
            print(f"\n{field}: {json.dumps(val, indent=2)[:500]}")

    # If exchangeInfo doesn't have tiers, we need to try the leverageBracket endpoint
    # which requires API keys, or use ccxt
    print("\n--- Alternative: Check if /fapi/v1/exchangeInfo has bracket info ---")

    # Look for bracket-related filters
    filters = sample.get("filters", [])
    for f in filters:
        ft = f.get("filterType", "")
        if "bracket" in ft.lower() or "leverage" in ft.lower() or "tier" in ft.lower():
            print(f"  Filter: {json.dumps(f, indent=2)[:300]}")


if __name__ == "__main__":
    main()
