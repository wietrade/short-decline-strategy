# 成交量异动做空策略 — Freqtrade 模拟交易

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
| **成交量异动 API** | `3001` | 提供 24h 成交量暴涨 >800% 的交易对列表 |
| **Freqtrade UI** | `8000` | Freqtrade REST API 及 WebSocket 控制面板 |

### API 接口

| 接口 | 地址 | 说明 |
| :--- | :--- | :--- |
| 异动列表 | `http://43.165.167.132:3001/api/list` | 返回当前异动交易对 |
| 动态交易对 | `http://localhost:3001/api/pairlist` | Freqtrade RemotePairList 数据源（内网） |
| Freqtrade API | `http://43.165.167.132:8000/api/v1` | Freqtrade 控制 API |
| WebSocket | `ws://43.165.167.132:8000/api/v1/message/ws` | 实时推送 |

### 成交量异动 API 服务

| 项目 | 值 |
| :--- | :--- |
| 部署目录 | `/www/wwwroot/volume_screener` |
| 启动脚本 | `tv_binance_volume_screener.py` |
| 运行方式 | `python3 tv_binance_volume_screener.py` |
| Python 虚拟环境 | `/www/wwwroot/volume_screener/venv` |
| 日志文件 | `/www/wwwroot/volume_screener/volume_screener.log` |

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
| 单笔保证金 | `100 USDT` | `stake_amount` |
| 杠杆 | `10×` | 名义价值 1000 USDT |
| 最大持仓 | `10` | `max_open_trades` |
| 交易模式 | `futures` | 永续合约 |
| 保证金模式 | `isolated` | 逐仓 |

### 风控规则

| 规则 | 条件 | 说明 |
| :--- | :--- | :--- |
| 移动止盈 | 价格下跌 ≥ 0.5%（= 盈利 5U）后激活，从最低点反弹 0.4% 即平仓 | 锁定利润 |
| 硬止损 | 开仓价反向波动 0.5%（= 5U）即止损 | 基于开仓价格计算，1000U 名义价值 × 0.5% = 亏损 5U |
| 退出异动列表 | 交易对不再出现在异动 API 中 | 强制平仓 |

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

## 本地文件

| 文件 | 说明 |
| :--- | :--- |
| `strategy_volume_surge.py` | 策略源码 |
| `config.json` | 本地配置模板 |
| `tv_binance_volume_screener.py` | TradingView 成交量筛选脚本 |
| `requirements.txt` | Python 依赖 |
