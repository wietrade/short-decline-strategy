import base64
import json
import urllib.request

url = "http://localhost:8000/api/v1/status"
credentials = base64.b64encode(b"admin:admin123").decode()
req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read())
print(f"Open trades: {len(data)}")
for t in data[:5]:
    print(f"  #{t['trade_id']} {t['pair']} open={t['is_open']}")
if len(data) > 5:
    print(f"  ... and {len(data) - 5} more")
