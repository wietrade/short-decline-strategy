import base64
import json
import urllib.request

auth = base64.b64encode(b"admin:admin123").decode()


def api(path):
    url = f"http://localhost:8000/api/v1{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# whitelist
wl = api("/whitelist")
print(f"=== Whitelist: {len(wl.get('whitelist', []))} pairs ===")
for p in wl.get("whitelist", [])[:5]:
    print(f"  {p}")

# status (trades)
trades = api("/status")
print(f"\n=== Open trades: {len(trades)} ===")
for t in trades[:3]:
    print(
        f"  #{t['trade_id']} {t['pair']} open={t['is_open']} profit={t.get('profit_ratio', 0):.4f}"
    )

# balance
bal = api("/balance")
print("\n=== Balance ===")
print(f"  Total: {bal.get('total', '?')}")
print(f"  Free: {bal.get('free', '?')}")
print(f"  Used: {bal.get('used', '?')}")

# daily profit
daily = api("/daily")
print("\n=== Daily profit (last 5) ===")
for d in daily.get("data", [])[:5]:
    print(f"  {d}")
