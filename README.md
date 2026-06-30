# 成交量异动多空策略 — Freqtrade 模拟交易

## 服务器信息

| 项目 | 值 |
| :--- | :--- |
| IP 地址 | `43.165.167.132` |
| SSH 密钥 | `43.165.167.132_id_ed25519` |
| SSH 用户 | `root` |
| SSH 连接 | `ssh -i "43.165.167.132_id_ed25519" root@43.165.167.132` |

---

## Freqtrade 部署

| 项目 | 路径 |
| :--- | :--- |
| 安装目录 | `/www/wwwroot/freqtrade` |
| Python 虚拟环境 | `/www/wwwroot/freqtrade/.venv` |
| Python 版本 | 3.12（虚拟环境 `.venv`） |
| 运行方式 | `screen -S freqtrade` 后台守护 |
| 启动命令 | `cd /www/wwwroot/freqtrade && screen -dmS freqtrade bash -c '.venv/bin/freqtrade trade --strategy VolumeSurgeShortStrategy --userdir user_data -c user_data/config_trade_surge.json'` |

### Screen 会话管理

```bash
# 创建新会话（后台启动 Freqtrade）
screen -dmS freqtrade bash -c \
  '.venv/bin/freqtrade trade \
    --strategy VolumeSurgeShortStrategy \
    --userdir user_data \
    -c user_data/config_trade_surge.json'

# 列出所有 screen 会话
screen -ls

# 附加到 freqtrade 会话（查看实时日志）
screen -r freqtrade

# 分离会话（不中断运行）：Ctrl+A, D

# 终止 freqtrade 会话
screen -S freqtrade -X quit

# 清除已死亡的会话
screen -wipe

```

### 策略文件

| 文件 | 路径 |
| :--- | :--- |
| 策略 | `/www/wwwroot/freqtrade/user_data/strategies/strategy_volume_surge.py` |
| 策略名称 | `VolumeSurgeShortStrategy` |

### 配置文件

| 文件 | 路径 |
| :--- | :--- |
| 交易配置 | `/www/wwwroot/freqtrade/user_data/config_trade_surge.json` |

### 数据库

| 文件 | 路径 |
| :--- | :--- |
| 模拟交易数据库 | `/www/wwwroot/freqtrade/tradesv3.dryrun.sqlite` |
| 回测结果 | `/www/wwwroot/freqtrade/user_data/backtest_results/` |
| 日志 | `/www/wwwroot/freqtrade/user_data/logs/` |

---

## 服务端口

| 服务 | 端口 | 说明 |
| :--- | :---: | :--- |
| **成交量异动 API** | `3001` | 提供 24h 成交量暴涨 >500% 的交易对列表（含活跃/退出标记） |
| **Freqtrade UI** | `8000` | Freqtrade REST API 及 WebSocket 控制面板 |

### 域名访问

| 域名 | 说明 |
| :--- | :--- |
| [https://bot2.1230sb.com](https://bot2.1230sb.com) | 成交量异动监控 Web 界面（Nginx 反代 → 127.0.0.1:3001） |

### API 接口

| 接口 | 地址 | 说明 |
| :--- | :--- | :--- |
| 异动列表 (JSON) | `https://bot2.1230sb.com/api/data` | 返回当前活跃 + 已退出历史的展示数据 |
| 异动列表 (策略用) | `https://bot2.1230sb.com/api/list` | 返回最近 60 分钟进入且仍活跃的交易对及趋势数据 |
| 动态交易对 | `http://127.0.0.1:3001/api/pairlist` | Freqtrade RemotePairList 数据源（内网），返回所有当前活跃交易对，格式为 `PAIR/USDT:USDT` |
| Freqtrade API | `http://43.165.167.132:8000/api/v1` | Freqtrade 控制 API |
| WebSocket | `ws://43.165.167.132:8000/api/v1/message/ws` | 实时推送 |

### 成交量异动 API 服务

| 项目 | 值 |
| :--- | :--- |
| 部署目录 | `/www/wwwroot/volume_screener` |
| 启动脚本 | `tv_binance_volume_screener.py` |
| 运行方式 | `screen -dmS screener venv/bin/python3 tv_binance_volume_screener.py` |
| Python 虚拟环境 | `/www/wwwroot/volume_screener/venv` |
| 监听地址 | `127.0.0.1:3001`（仅本机，公网通过 Nginx 域名访问） |

### 扫描程序重启

```bash
cd /www/wwwroot/volume_screener

# 停止旧的 screener 会话（如果存在）
screen -S screener -X quit 2>/dev/null

# 后台启动扫描程序
screen -dmS screener venv/bin/python3 tv_binance_volume_screener.py

# 查看进程与监听端口
ps aux | grep tv_binance | grep -v grep
ss -tlnp | grep 3001
```

### Freqtrade UI 登录凭据

| 项目 | 值 |
| :--- | :--- |
| 地址 | `http://43.165.167.132:8000` |
| 用户名 | `admin` |
| 密码 | `admin123` |

---

## 模拟交易参数

| 参数 | 值 | 说明 |
| :---: | :--- | :--- |
| 模拟总资金 | `1000 USDT` | `dry_run_wallet` |
| 单笔保证金 | 账户余额 5%（动态每日更新） | `stake_amount = unlimited`，由 `custom_stake_amount` 计算 |
| 杠杆 | `10×` | 名义价值 1000 USDT |
| 最大持仓 | `10` | `max_open_trades` |
| 交易模式 | `futures` | 永续合约 |
| 保证金模式 | `isolated` | 逐仓 |

### 风控规则

| 规则 | 条件 | 说明 |
| :--- | :--- | :--- |
| 移动止盈（多/空） | 价格朝有利方向波动 ≥ 5% 后激活，从极端价格回撤 4% 即平仓 | 锁定利润 |
| 硬止损（仅做多） | 开仓价反向波动 30% 即止损 | 基于加权平均开仓价计算 |
| 做空加仓 | 从首次开仓价每上涨 10% 加仓一次，最多 5 次 | 逆势加仓，无硬止损 |

---

## TV 自选表管理器

`tv_watchlist_manager.py` 可自动将成交量异动交易对添加到 TradingView 自选表。

### 工作原理

从 `tv_binance_volume_screener.py` 的 HTTP API 获取数据，通过 TV 内部 API 操作自选表。

**活跃/退出机制：**

- 扫描器每 60 秒查询 TV Scanner API，只返回 24h 成交量变化 > 500% 的交易对
- 如果一个交易对之前 > 500%，但某次扫描掉到 < 500%，自动标记为「退出」并记录退出时间
- 退出交易对仍在 API 中保留（`vol_change_24h_pct` 为空字符串），方便查看历史
- `tv_watchlist_manager.py` 的 `fetch_symbols()` 自动过滤退出交易对，**只上传活跃的到 TV**

**更新流程：**

```text
GET  /api/v1/symbols_list/all/?source=web
     → 查找"new"列表的 ID
POST /api/v1/symbols_list/custom/{id}/replace/?unsafe=true  → 原地替换全部交易对
```

使用 `POST .../replace/?unsafe=true` 原地替换列表内容，**列表 ID 保持不变**，因此已设置的 TV 警报不会失效（如果用 DELETE + CREATE 的方式，ID 会变，警报就需要重新设置）。

> body 格式为 **JSON 数组**，例如：`["BINANCE:ONGUSDT.P", "BINANCE:MAVIAUSDT.P"]`

因为操作的是**个人自选表数据**，需要登录认证——首次使用需粘贴一次 TV cookie，之后保存在本地重复使用。

### 首次使用：获取 Cookie

**自动模式（推荐）：**

```bash
python tv_watchlist_manager.py cookie --auto
```

会自动打开浏览器窗口 → 你登录 TV → 回到终端按回车 → 脚本自动抓取全部 cookie（含 HttpOnly 的 `sessionid`）。

**手动模式：**

```bash
python tv_watchlist_manager.py cookie
```

按提示操作：F12 → **Application → Cookies** → `https://www.tradingview.com` → 右键 Copy All → 粘贴

> cookie 保存在 `data/tv_storage_state.json`，后续运行 `add --api` 会自动读取，无需重复设置。

### 日常同步

```bash
# 日常：自动同步活跃异动交易对到 TV「new」自选表（自动过滤已退出的）
python tv_watchlist_manager.py add --api
```

每次运行会**原地替换**列表内容，只上传活跃交易对（`vol_change_24h_pct` 有值），列表 ID 保持不变。

### 完整命令

| 命令 | 说明 |
| :--- | :--- |
| `cookie` | 首次设置 TV 登录 cookie（一次即可，后续复用） |
| `add --api` | 自动上传活跃异动交易对到 TV 自选表（默认只保留活跃的） |
| `list` | 查看当前活跃异动交易对列表 |
| `export` | 导出 `.tvs` 文件（TV 手动导入: 右键自选 → 导入符号列表） |
| `serve` | 启动网页服务 `http://localhost:3002`，浏览器中点一键复制 |
| `url` | 生成 TV 图表/搜索链接 |

```bash
# 指定扫描器地址（默认连接服务器 43.165.167.132:3001）
python tv_watchlist_manager.py list --api http://43.165.167.132:3001/api/data
```

### Cookie 说明

| 项目 | 说明 |
| :--- | :--- |
| 保存位置 | `data/tv_storage_state.json` |
| 有效期 | 同 TV 登录会话（一般数周） |
| 重新设置 | cookie 过期后重新运行 `python tv_watchlist_manager.py cookie` |

### 接口说明

| 方法 | 接口 | body 格式 | 说明 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/symbols_list/custom/{id}/replace/?unsafe=true` | `["SYM1", "SYM2"]` 数组 | **原地替换**列表内容，ID 不变 ✅ |
| `POST` | `/api/v1/symbols_list/custom/{id}/append/` | `["SYM1"]` 数组 | 追加交易对到已有列表 |
| `POST` | `/api/v1/symbols_list/custom/` | `{"name": "new", "symbols": [...]}` | 创建新自选表 |
| `GET` | `/api/v1/symbols_list/all/?source=web` | — | 获取所有自选表 |
| `GET` | `/api/v1/symbols_list/custom/{id}?source=web` | — | 获取单个自选表详情 |
| `DELETE` | `/api/v1/symbols_list/custom/{id}/` | — | 删除自选表 |

---

## 常用运维命令

```bash
# 连接到服务器
ssh -i "43.165.167.132_id_ed25519" root@43.165.167.132

# 查看 Freqtrade 运行状态
ps aux | grep freqtrade | grep -v grep

# 进入 screen 会话查看实时日志
screen -r freqtrade

# 退出 screen（不中断）
Ctrl+A, D

# 重启 Freqtrade
screen -S freqtrade -X quit
cd /www/wwwroot/freqtrade
screen -dmS freqtrade bash -c '.venv/bin/freqtrade trade \
  --strategy VolumeSurgeShortStrategy \
  --userdir user_data \
  -c user_data/config_trade_surge.json'

# 查看数据库中的交易记录
sqlite3 /www/wwwroot/freqtrade/tradesv3.dryrun.sqlite \
  "SELECT id, pair, open_date, close_date, profit_ratio, stake_amount \
   FROM trades ORDER BY close_date DESC LIMIT 10;"

```

---

## 杠杆配置注意事项

### 杠杆调用链

```text
freqtradebot.py::get_valid_enter_price_and_stake()
  → exchange.get_max_leverage(pair, stake_amount)   # 获取 max_leverage
  → strategy.leverage(pair, ..., max_leverage, ...) # 策略返回目标杠杆（如 10）
  → leverage = min(max(leverage, 1.0), max_leverage) # 最终截断

```

**关键：即使策略返回 10x，如果 `max_leverage == 1.0`，结果也是 1.0。**

### 常见原因：新交易对不在杠杆分级文件中

Freqtrade dry-run 模式下从本地文件读取杠杆分级数据：

| 文件 | 路径 |
| :--- | :--- |
| 杠杆分级文件 | `/www/wwwroot/freqtrade/freqtrade/exchange/binance_leverage_tiers.json` |
| 来源 | `binance.py::load_leverage_tiers()` — dry-run 读本地 JSON，实盘调 API |

- 该文件**不会自动更新**，新上线的交易对（如 GME、CRWD）可能缺失
- 缺失时 `get_max_leverage()` 返回 `1.0`，导致杠杆被强制截断为 1x
- 修复方法：手动在 JSON 中添加缺失交易对的标准 tier（maxLeverage=75x 等），然后重启 Freqtrade

### 策略 leverage() 回调

```python
def leverage(self, pair, current_time, current_rate,
             proposed_leverage, max_leverage, entry_tag, side, **kwargs):
    return self.config.get("futures_leverage", 1)

```

- `proposed_leverage` 固定为 1.0（Freqtrade 传参）
- `max_leverage` 来自 `get_max_leverage()`，受 tiers 文件限制
- 配置文件中使用 `"futures_leverage": 10`（下划线），CLI 参数为 `--futures-leverage`

---

## 本地文件

| 文件 | 说明 |
| :--- | :--- |
| `strategy_volume_surge.py` | 策略源码 |
| `config.json` | 本地配置模板 |
| `tv_binance_volume_screener.py` | TV Scanner API 成交量异动扫描器（HTTP 服务 + SQLite 历史记录） |
| `tv_watchlist_manager.py` | TV 自选表管理器（自动上传活跃异动交易对到 TV） |
| `requirements.txt` | Python 依赖 |
| `data/volume_surge.db` | SQLite 数据库，记录交易对进入/退出时间 |
| `data/tv_storage_state.json` | TV 登录 cookie（Playwright 格式） |
| `data/tradingview_watchlist.tvs` | 导出的 .tvs 自选表文件 |

---

## 遇到的问题及解决

### 问题1：Whitelist 只显示 3 个交易对 (IBM/ALAB/UBER)

**症状**：RemotePairList API 返回 50+ 个交易对，但 Freqtrade whitelist 只有 3 个。

**原因**：API 返回格式为 `SAFE/USDT`，但 Binance 合约市场使用 `SAFE/USDT:USDT`。Freqtrade 的 `expand_pairlist()` 用 `re.fullmatch()` 做精确匹配，导致 `SAFE/USDT` 匹配不上 `SAFE/USDT:USDT`，所有交易对被静默丢弃。

**修复**：在 `_serve_pairlist()` 中给交易对添加 `:USDT` 后缀：

```python
pair = self._tv_to_pair(r.get("name", ""))
if pair.endswith("/USDT"):
    pair = f"{pair}:USDT"
elif pair.endswith("/USDC"):
    pair = f"{pair}:USDC"

```

### 问题2：AgeFilter 无效

**原因**：同问题1，所有交易对在 expand_pairlist 阶段已被丢弃，AgeFilter 无可过滤的对象。格式正确后 AgeFilter 正常工作。

### 问题3：AgeFilter 使用日线 K 线数据量而非 onboardDate

**说明**：AgeFilter 不使用交易所 API 的 `onboardDate` 字段，而是下载 1d 日线 K 线，统计多少根日线来判断上市天数。若某个交易对日线数据下载失败，该交易对会被移除。

### 问题4：扫描程序 HTTP 服务阻塞 / socket 泄漏导致无法连接

**症状**：进程在运行，但 HTTP 端口 3001 无法建立新连接，`ss -tnp` 显示大量 `CLOSE-WAIT` 状态的连接，或线程卡在 `tcp_recvmsg` 等待客户端数据。

**原因**：单线程 `HTTPServer` 容易被半开连接阻塞；同时 `BaseHTTPRequestHandler` 的 keep-alive 行为会让客户端断开后的 socket 长时间滞留，运行数天后可能积累 CLOSE-WAIT 连接，导致新请求无法被接受。

**修复**：

- 使用 `ThreadingHTTPServer`，单个异常连接不阻塞其他请求
- `VolumeSurgeHandler.close_connection = True`，每次请求后主动关闭连接
- 在 `setup()` 中设置 `HTTP_REQUEST_TIMEOUT = 10` 秒，避免半开连接长时间占用线程
- 监听地址改为 `127.0.0.1:3001`，公网只通过 Nginx 域名反代访问

```python
HTTP_HOST = "127.0.0.1"
HTTP_REQUEST_TIMEOUT = 10

class VolumeSurgeHandler(BaseHTTPRequestHandler):
  close_connection = True

  def setup(self):
    self.request.settimeout(HTTP_REQUEST_TIMEOUT)
    super().setup()

server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), VolumeSurgeHandler)
```

### 问题5：`/api/data` 展示数据与 Freqtrade 活跃列表混用

**症状**：`latest_exited_count` 有值，但页面 `results` 中没有退出交易对，前端无法显示退出记录；或者为了显示退出记录而影响 Freqtrade 白名单。

**原因**：展示列表和交易列表都使用同一个 `_latest_scan`，语义混在一起。

**修复**：拆成两个全局状态：

- `_latest_scan`：页面展示数据，包含当前活跃 + 已退出历史
- `_latest_active_scan`：交易接口数据，只包含当前仍满足 >500% 条件的活跃交易对

接口分工：

| 接口 | 数据范围 |
| :--- | :--- |
| `/api/data` | 当前活跃 + 已退出历史 |
| `/api/list` | 最近 60 分钟进入且仍活跃的交易对 |
| `/api/pairlist` | 所有当前活跃交易对，供 Freqtrade RemotePairList 使用 |
