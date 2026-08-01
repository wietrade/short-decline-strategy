# 做空反弹策略 (Short Decline Strategy)

基于 Freqtrade + 币安合约的纯做空反弹策略。

---

## 快速概览

| 项目 | 说明 |
|:---|:---|
| 策略文件 | `strategy_short_decline.py` |
| 配置文件 | `config_short_decline.json` |
| 扫描器 | `tv_binance_volume_screener.py` (TradingView 成交量异动) |
| 交易所 | Binance Futures (USDT 永续) |
| 周期 | 15m |
| 模式 | 模拟盘 (dry_run) |
| 保证金 | 全仓 (cross) |
| 杠杆 | 10x |
| 单笔开仓 | 50 USDT |
| 最大持仓 | 10 个 |

## 策略逻辑（纯空头）

| 阶段 | 参数 |
|:---|:---|
| 入场 | 扫描器异动交易对（4h涨≥8%, 24h涨>0, 1w/1m/3m排除24h后≤0） |
| 入场限制 | 当前价距24h最高点跌幅 > 8% 不开空 |
| 止损 | **无**（永不爆仓） |
| 止盈 | 移动止盈：均价偏离 **4%** 激活，从极值回撤 **2%** 平仓 |
| 超时 | 18 小时后价格回到成本价平仓 |
| 加仓 | DCA 金字塔：DCA#1 +10% / DCA#2 +20% / DCA#3+ 高点回调3%补仓 |
| 资金费率 | < **-0.07%** 禁止开空和DCA加仓（仅约束入场，不平仓） |

## 远程服务器

| 项目 | 值 |
|:---|:---|
| IP | `43.165.167.132` |
| 用户 | `root` |
| SSH 密钥 | `I:\1H\43.165.167.132_id_ed25519` |
| SSH 命令 | `ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132` |

### 服务器文件路径

| 文件 | 路径 |
|:---|:---|
| 策略目录 | `/www/wwwroot/freqtrade/user_data/strategies/` |
| 配置文件 | `/www/wwwroot/freqtrade/user_data/config_short_decline.json` |
| 扫描器 | `/www/wwwroot/volume_screener/tv_binance_volume_screener.py` |
| 扫描器日志 | `/tmp/screener.log` |
| 策略日志 | `/tmp/ft_short.log` |

### 服务端口

| 端口 | 服务 |
|:---|:---|
| 3001 | 扫描器 API |
| 8001 | Freqtrade (ShortDeclineStrategy) |

## 常用命令

### 上传并重启策略

```powershell
scp -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no "I:\1H\short-decline-strategy\strategy_short_decline.py" root@43.165.167.132:/www/wwwroot/freqtrade/user_data/strategies/

ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132 "pkill -9 -f '[f]reqtrade trade' && sleep 1 && cd /www/wwwroot/freqtrade && setsid .venv/bin/freqtrade trade --config user_data/config_short_decline.json --strategy ShortDeclineStrategy > /tmp/ft_short.log 2>&1 &"
```

### 查看策略日志

```powershell
ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132 "tail -50 /tmp/ft_short.log"
```

### 查看扫描器日志

```powershell
ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132 "tail -50 /tmp/screener.log"
```

### 检查运行状态

```powershell
ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132 "ps aux | grep freqtrade | grep -v grep; ss -tlnp | grep -E '3001|8001'"
```

### 比对本地与远程策略版本

```powershell
ssh -i "I:\1H\43.165.167.132_id_ed25519" -o StrictHostKeyChecking=no root@43.165.167.132 "md5sum /www/wwwroot/freqtrade/user_data/strategies/strategy_short_decline.py"
certutil -hashfile "I:\1H\short-decline-strategy\strategy_short_decline.py" MD5
```
