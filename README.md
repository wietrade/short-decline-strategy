# 成交量异动做空策略 — TradingView Scanner + Freqtrade

## 架构总览

```text
TradingView Scanner API（非官方）
        │ 每 60 秒轮询
        ▼
tv_binance_volume_screener.py  ── 本机 :3001 ──┬── /api/pairlist ──► Freqtrade RemotePairList（选币）
        │                                       ├── /api/list     ──► ShortDeclineStrategy（数据源：perf/24h）
        │                                       ├── /api/data     ──► HTML 看板（含已退出历史）
        │                                       └── /             ──► 监控页面
        │
        ▼
strategy_short_decline.py  ── Freqtrade 纯做空 DCA 策略（ShortDeclineStrategy）
```

---

## 程序说明

### 1. 扫描程序 `tv_binance_volume_screener.py`

每 60 秒调用 TradingView Scanner API，筛选 **币安 USDT 永续合约** 中 **24h 成交量变化 > 500%** 的交易对。

| 特性 | 说明 |
| :--- | :--- |
| 活跃窗口 | 交易对首次出现后保留 **30 分钟**（`ACTIVE_DURATION_MINUTES`），超时移出 pairlist |
| 重新激活 | 退出后再次满足条件，`entry_time` 重置，重新获得 30 分钟窗口 |
| 数据清洗 | 数值字段经 `_to_float_or_none()` 处理 → `float \| null` |
| 历史记录 | SQLite（`data/volume_surge.db`），最多保留 50 条 |
| HTTP 健壮性 | `ThreadingHTTPServer` + `close_connection=True` + 10s 超时，防 socket 泄漏 |
| 优雅退出 | 捕获 SIGINT/SIGTERM，`_running=False` 后关闭 HTTP 服务 |
| 运行方式 | 服务器上用 screen 守护 |

### 2. 策略 `strategy_short_decline.py`（ShortDeclineStrategy）

**纯做空 + DCA 金字塔加仓策略**，针对长期下跌、短期异动爆拉的山寨币。

| 环节 | 逻辑 |
| :--- | :--- |
| 数据源 | 每 60 秒轮询 `http://127.0.0.1:3001/api/list`，缓存 perf 与 24h 涨跌幅 |
| 入场排除 | ① 24h 跌幅 > 10% 不做空；② `perf_1w/1m/3m − 24h涨幅` 任一 > 0 不做空；③ 资金费率 < -0.05% 暂缓开空 |
| 数据缺失保护 | 任一字段缺失（perf 或 24h）则该交易对不交易 |
| 入场方向 | 通过筛选 → 做空（`enter_short`，tag=`short_decline`） |
| ADX 排序 | 首次开仓按 ADX(14) 从高到低排队，只有候选中最高 ADX 才放行 |
| 开仓 | 100U 保证金 × 10x 杠杆 = 1000U 名义；记录首仓价与币数量 |
| DCA 加仓 | 逆势上涨触发，间隔按斐波那契数列递增，币数量与首仓相同，最多 5 次 |
| 出场 | 基于加权持仓均价 `trade.open_rate` 做移动止盈（激活 15%、回撤 5%） |
| 止损 | `custom_stoploss` 返回 -10.0（1000%），等效不止损 |

**出场逻辑关键点**：`use_exit_signal = True` 是 `custom_exit` 被调用的**前提**。Freqtrade 源码中 `custom_exit` 的调用被包在 `if self.use_exit_signal:` 内，若设为 `False` 则出场逻辑完全失效（会导致盈利单永不平仓）。`populate_exit_trend` 不设任何信号，所有出场都在 `custom_exit` 中完成。

**重启健壮性**：首仓价和币数量优先从 `trade.orders` 的真实成交入场订单恢复（`_get_first_entry_state`），内存缓存丢失（重启）也能正确管理已有持仓。

### 资金费率过滤

策略每 60 秒从币安 API（`fapi/v1/premiumIndex`）获取所有永续合约的实时资金费率，作为额外入场过滤：

| 条件 | 行为 |
|:---|:---|
| 资金费率 < **-0.05%**（如 -0.1%） | 暂缓开空，交易对加入 **监控列表** |
| 监控中的交易对费率恢复到 **≥ -0.05%** | 自动解除限制，重新加入开仓候选 |
| 长期停留在监控列表中且已不在扫描结果中 | 自动清理（防内存泄漏） |

阈值在策略代码中通过 `funding_rate_threshold` 定义，默认 `-0.005`（即 -0.5%）。

**筛选流程：**

```
扫描器数据 → 原有筛选(perf+ADX) → 资金费率检查 → 最终候选列表
                                        │
                                  费率 < -0.05%?
                                   ├─ 是 → 加入监控列表，暂缓开空
                                   └─ 否 → 正常加入候选
                                 每次循环检查监控列表
                                   └─ 费率已恢复 → 重新进入候选
```

### DCA 加仓间隔（斐波那契递增）

间隔按斐波那契数列递增，前期密集拉平成本、后期拉宽保留弹药（`_dca_trigger_rise`）：

```text
gap_i     = short_add_threshold(10%) × fib(i)   # fib = 1,1,2,3,5
trigger(n) = Σ gap_i (i=1..n)
```

| 第几次加仓 | fib | 单步间隔 | 累计触发涨幅 |
| :---: | :---: | :---: | :---: |
| 1 | 1 | +10% | **+10%** |
| 2 | 1 | +10% | **+20%** |
| 3 | 2 | +20% | **+40%** |
| 4 | 3 | +30% | **+70%** |
| 5 | 5 | +50% | **+120%** |

### 移动止盈（固定参数）

移动止盈从加权持仓均价 `trade.open_rate` 开始计算。策略记录持仓期间最低价；当价格向盈利方向从均价下跌 **15%** 以上（激活移动止盈），再从最低点反弹 **5%** 以上时平仓。

平仓还有一道硬条件：当前必须仍是盈利状态。也就是空单当前价仍低于加权持仓均价，且 `current_profit > 0`。如果价格已经反弹到均价上方，即使历史最低价曾经触发过激活，也不会按止盈平仓。

---

## DCA 如何改善做空均价

做空 DCA 在**更高价位**继续加空，会把加权空单均价抬高。后续移动止盈以 `trade.open_rate` 这个加权持仓均价为盈利起算点，而不是以首仓价为准：

| 仓位 | 开空价 | 平仓价（回首仓） | 盈亏 |
| :--- | :---: | :---: | :---: |
| 首仓 | 100 | 100 | 0 |
| 加仓1 | 110 | 100 | +10 |
| 加仓2 | 120 | 100 | +20 |

这个例子只说明 DCA 后均价会改善：价格即使只回到首仓附近，加仓部分也已有利润。当前真实出场不再固定等“回到首仓价”，而是等价格先低于加权均价达到动态激活阈值，再从持仓最低点反弹达到动态阈值后平仓。

---

## 服务器

| 项目 | 值 |
| :--- | :--- |
| IP | `43.165.167.132` |
| SSH | `ssh -i "43.165.167.132_id_ed25519" root@43.165.167.132` |
| 公网入口 | [https://bot2.1230sb.com](https://bot2.1230sb.com)（Nginx 反代 `127.0.0.1:3001`） |
| 策略路径 | `/www/wwwroot/freqtrade/user_data/strategies/strategy_short_decline.py` |
| 配置路径 | `/www/wwwroot/freqtrade/user_data/config_trade_surge.json` |
| 交易数据库 | `/www/wwwroot/freqtrade/tradesv3.dryrun.sqlite` |

### 端口

| 服务 | 端口 | 说明 |
| :--- | :---: | :--- |
| 扫描程序 API | 3001 | HTTP（仅本机，Nginx 反代到公网） |
| Freqtrade API | 8000 | REST API + WebSocket |

---

## API 接口

### `/api/pairlist` — RemotePairList 数据源

供 Freqtrade 配置引用，带 `:USDT` 后缀适配 Binance 合约 `expand_pairlist`。

```json
{"pairs": ["SAFE/USDT:USDT", "ME/USDT:USDT"], "refresh_period": 60}
```

仅含进入时间 ≤ 30 分钟的交易对，按 24h 成交量降序。

### `/api/list` — 策略数据源

策略实际拉取的接口，返回当前活跃交易对的完整数值字段（含 `price_change_24h_pct`）。

```json
[{"pair": "SAFE/USDT", "price": 0.1234, "vol_change_24h_pct": 850.5,
  "price_change_24h_pct": -3.2, "perf_1w": -10.87, "perf_1m": -25.3,
  "perf_3m": -40.2, "recommend_all": -0.75, "rsi": 42.3}]
```

### `/api/data` — 完整数据（含已退出历史）

含 `entry_time`/`exit_time`/`rating_text` 等字段，供浏览器渲染看板。

---

## 部署

### 扫描程序（screen 守护）

```bash
cd /www/wwwroot/volume_screener
screen -dmS screener venv/bin/python3 tv_binance_volume_screener.py

# 管理
screen -r screener            # 附加查看（Ctrl+A D 分离）
screen -S screener -X quit    # 停止
ps aux | grep tv_binance | grep -v grep
```

### Freqtrade（setsid 脱离会话）

```bash
cd /www/wwwroot/freqtrade

# 停止旧进程（交互式 SSH 中执行；不要嵌在同一条远程启动命令里）
pkill -9 -f "[f]reqtrade trade"

# 后台启动（setsid 确保脱离 SSH 会话，不随连接断开退出）
setsid bash -c '.venv/bin/freqtrade trade \
  --strategy ShortDeclineStrategy \
  --userdir user_data \
  -c user_data/config_trade_surge.json > /tmp/ft.log 2>&1' < /dev/null &

# 管理
pgrep -af "[f]reqtrade trade" # 查看进程
tail -f /tmp/ft.log           # 查看日志
```

> ⚠️ 用 `nohup ... &` 直接跟在 SSH 命令后可能随会话退出被杀，务必用 `setsid`。
> ⚠️ 在 Windows PowerShell 里把 `pkill -f "freqtrade trade"` 和启动命令塞进同一条 SSH 命令，可能误杀当前远程 shell。建议分步执行，或用 `[f]reqtrade trade` 这种不会自匹配的模式查看进程。
> 更换策略或清空模拟盘时：`rm -f /www/wwwroot/freqtrade/tradesv3.dryrun.sqlite*`

---

## 配置 `config_trade_surge.json`

| 参数 | 值 | 说明 |
| :--- | :---: | :--- |
| 模式 | dry_run | 模拟盘 |
| 模拟资金 | 2000 USDT | `dry_run_wallet` |
| 单笔保证金 | 100 USDT | 由策略 `custom_stake_amount` 控制 |
| 杠杆 | 10x | `futures_leverage` |
| 最大持仓 | 10 | `max_open_trades` |
| 保证金模式 | cross | 全仓 |
| 入场/退出价侧 | other | 吃对手盘 |
| 交易对过滤 | 上市 ≥ 90 天 | RemotePairList + AgeFilter |

### 扫描程序常量

| 参数 | 默认值 | 说明 |
| :--- | :---: | :--- |
| `MIN_VOL_CHANGE_PCT` | 500 | 24h 成交量变化最小百分比 |
| `INTERVAL_SECONDS` | 60 | 轮询间隔（秒） |
| `ACTIVE_DURATION_MINUTES` | 30 | 交易对在 pairlist 中最长保留时间 |
| `MAX_HISTORY_RECORDS` | 50 | 数据库最多保留记录数 |

---

## 文件结构

```text
i:\1H\
├── tv_binance_volume_screener.py    # 成交量异动扫描程序
├── strategy_short_decline.py        # Freqtrade 纯做空 DCA 策略
├── config_trade_surge.json          # Freqtrade 交易配置
├── 43.165.167.132_id_ed25519        # SSH 密钥
├── requirements.txt                 # Python 依赖
├── data/
│   └── volume_surge.db              # 扫描程序 SQLite 数据库
└── README.md                        # 本文件
```

---

## 常见问题

**Q：盈利很高却不平仓？**
检查 `use_exit_signal` 是否为 `True`。设为 `False` 会导致 `custom_exit` 从不被调用，出场逻辑失效。

**Q：重启后已有持仓丢失止盈？**
`_get_first_entry_state` 会从 `trade.orders` 恢复首仓价，无需依赖内存。若订单里读不到，回退用 `trade.open_rate`。

**Q：杠杆变成 1x？**
dry-run 从 `freqtrade/exchange/binance_leverage_tiers.json` 读杠杆分级，新上线交易对缺失时 `max_leverage` 返回 1.0。手动补该交易对的 tier 后重启。
