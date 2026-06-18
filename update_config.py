import json

path = "/www/wwwroot/freqtrade/user_data/config_trade_surge.json"
with open(path) as f:
    c = json.load(f)

c["max_open_trades"] = 10
c["stake_amount"] = 100

with open(path, "w") as f:
    json.dump(c, f, indent=2)

print("OK: max_open_trades=10, stake_amount=100")
