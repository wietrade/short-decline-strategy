#!/usr/bin/env python3
"""Save cookies from clipboard/file to tv_storage_state.json"""

import json
import sys
from pathlib import Path

state_path = Path("i:/1H/data/tv_storage_state.json")
state_path.parent.mkdir(parents=True, exist_ok=True)

# Read cookies from stdin
print("Paste the JSON cookie data from Playwright output:")
print("(Copy the cookies JSON from the conversation above)")
print("Then press Ctrl+Z then Enter (or Ctrl+D)")
raw = sys.stdin.read()

try:
    data = json.loads(raw)
    state_path.write_text(json.dumps(data, indent=2))
    print(f"✅ Saved {len(data.get('cookies', []))} cookies to {state_path}")
except json.JSONDecodeError as e:
    print(f"❌ Invalid JSON: {e}")
