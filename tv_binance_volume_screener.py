"""
成交量异动扫描程序 — TradingView Scanner API
============================================

功能：
  - 每分钟轮询 TradingView Scanner API，筛选币安 USDT 永续合约中
    24h 成交量变化 > 500% 的交易对
  - 交易对首次出现后保留 30 分钟（ACTIVE_DURATION_MINUTES），
    超时自动从 pairlist 移除
  - 退出后重新满足条件时重置计时器
  - 历史数据保存在 SQLite（data/volume_surge.db），最多保留 50 条
  - 所有数值字段经过 _to_float_or_none() 清洗，确保下游不收到脏数据

HTTP API（127.0.0.1:3001）：
  /              → HTML 监控看板
  /api/data      → 完整扫描结果（含已退出历史）
  /api/list      → 活跃交易对 + 技术指标（供 Freqtrade 策略轮询）
  /api/pairlist  → RemotePairList 格式（供 Freqtrade 配置引用）
"""

import json
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

# ═══════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════
MIN_VOL_CHANGE_PCT = 500  # 24h 成交量变化最小百分比
MIN_CHG4H_PCT = 8.0  # 4h 涨幅最小百分比（低于此值不出现在 pairlist 中）
MAX_RESULTS = 200  # 最大结果数
INTERVAL_SECONDS = 60  # 更新间隔（秒），60 = 1分钟
DB_PATH = Path(__file__).parent / "data" / "volume_surge.db"
HTTP_HOST = "127.0.0.1"  # 仅本机监听，由 Nginx 反代到公网域名
HTTP_PORT = 3001  # HTTP 服务器端口
MAX_DISPLAY_RESULTS = 50  # 最多显示记录数
MAX_HISTORY_RECORDS = 50  # 数据库最多保留记录数
ACTIVE_DURATION_MINUTES = 30  # 交易对在 pairlist 中最长保留时间（分钟）
HTTP_REQUEST_TIMEOUT = 10  # 防止半开连接阻塞 HTTP 线程
# ==============================================

_running = True


def signal_handler(sig, frame):
    global _running
    print("\n\n收到停止信号，正在退出...")
    _running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ═══════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════
# 所有读写必须通过 G.lock 保护
class _GlobalState:
    """集中管理所有运行时全局状态，减少散落变量。"""

    def __init__(self):
        self.latest_scan: list[dict] = []  # 完整展示列表（含活跃+已退出）
        self.latest_active_scan: list[dict] = []  # 仅活跃交易对
        self.latest_new_symbols: set[str] = set()
        self.latest_exited_symbols: set[str] = set()
        self.latest_timestamp: str = ""
        self.seen_symbols: set[str] = set()  # 所有曾出现的交易对
        self.entry_time: dict[str, str] = {}  # name -> 首次进入时间
        self.exit_time: dict[str, str] = {}  # name -> 退出时间
        self.current_ratings: dict[str, float] = {}  # name -> 上一次 recommend_all
        self.previous_scan_symbols: set[str] = set()  # 上一次扫描的集合（检测退出）
        self.lock = threading.Lock()


G = _GlobalState()


# ═══════════════════════════════════════════════
# 模块二：API — HTTP 接口
# ═══════════════════════════════════════════════


class VolumeSurgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：显示最新的扫描结果。"""

    # 关闭 HTTP keep-alive，防止 socket 泄漏（CLOSE-WAIT）
    close_connection = True

    def setup(self):
        self.request.settimeout(HTTP_REQUEST_TIMEOUT)
        super().setup()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            self._serve_json()
        elif path == "/api/list":
            self._serve_list()
        elif path == "/api/pairlist":
            self._serve_pairlist()
        elif path == "/api/rating_changes":
            self._serve_rating_changes()
        elif path == "/api/entry_perf":
            self._serve_entry_perf()
        elif path == "/api/strategies":
            self._serve_strategies_json()
        elif path == "/strategies":
            self._serve_strategies_html()
        elif path == "/" or path == "/index.html":
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _serve_json(self):
        with G.lock:
            display = G.latest_scan[:MAX_DISPLAY_RESULTS]
            enriched = []
            for r in display:
                item = dict(r)
                item["entry_time"] = G.entry_time.get(r["name"], "")
                item["exit_time"] = G.exit_time.get(r["name"], "")
                # 技术评级: 数值转文字（复用 _rating_to_text）
                rec = r.get("recommend_all")
                if isinstance(rec, (int, float)):
                    item["rating_text"] = _rating_to_text(rec)
                else:
                    item["rating_text"] = ""
                enriched.append(item)
            data = {
                "timestamp": G.latest_timestamp,
                "total": len(G.latest_scan),
                "displayed": len(display),
                "history_total": len(G.seen_symbols),
                "latest_new_count": len(G.latest_new_symbols),
                "latest_exited_count": len(G.latest_exited_symbols),
                "results": enriched,
                "latest_new_symbols": list(G.latest_new_symbols),
                "latest_exited_symbols": list(G.latest_exited_symbols),
            }
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = self._build_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _tv_to_pair(raw: str) -> str:
        """转换 TradingView 名称 "SAFEUSDT.P" → "SAFE/USDT" """
        name = raw.replace(".P", "")
        for quote in ("USDT", "USDC", "BUSD"):
            if name.endswith(quote) and len(name) > len(quote):
                return f"{name[: -len(quote)]}/{quote}"
        return f"{name}/USDT"

    def _serve_list(self):
        """
        /api/list — 返回当前交易对及趋势数据，供 Freqtrade 策略使用。
        数据源 _latest_active_scan 与看板一致。
        返回格式: [{"pair": "SAFE/USDT", "perf_1w": -10.87, ...}]
        """
        with G.lock:
            results = []
            for r in G.latest_active_scan:
                name = r.get("name", "")
                pair = self._tv_to_pair(name)
                results.append(
                    {
                        "pair": pair,
                        "price": r.get("price"),
                        "vol_24h": r.get("vol_24h"),
                        "vol_change_24h_pct": r.get("vol_change_24h_pct"),
                        "price_change_24h_pct": r.get("price_change_24h_pct"),
                        "price_change_4h_pct": r.get("price_change_4h_pct"),
                        "perf_1w": r.get("perf_1w"),
                        "perf_1m": r.get("perf_1m"),
                        "perf_3m": r.get("perf_3m"),
                        "recommend_all": r.get("recommend_all"),
                        "rsi": r.get("rsi"),
                    }
                )
        body = json.dumps(results, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_pairlist(self):
        """
        /api/pairlist - 返回当前所有交易对（按成交量降序），供 Freqtrade RemotePairList 使用。
        Binance 合约 (swap) 格式需要 ":USDT" 后缀才能被 expand_pairlist 匹配。
        返回格式: {"pairs": ["SAFE/USDT:USDT", "ME/USDT:USDT", ...], "refresh_period": 60}
        过滤条件: 4h 涨幅 < MIN_CHG4H_PCT 的交易对不进入 pairlist。
        """
        with G.lock:
            items = []
            for r in G.latest_active_scan:
                chg4h = r.get("price_change_4h_pct")
                if chg4h is None or chg4h == "":
                    continue
                try:
                    if float(chg4h) < MIN_CHG4H_PCT:
                        continue
                except (TypeError, ValueError):
                    continue
                pair = self._tv_to_pair(r.get("name", ""))
                # Binance 合约格式: expand_pairlist 需带 :USDT 后缀才能匹配
                if pair.endswith("/USDT"):
                    pair = f"{pair}:USDT"
                elif pair.endswith("/USDC"):
                    pair = f"{pair}:USDC"
                vol = r.get("vol_24h")
                if vol is None or vol == "":
                    vol = 0
                try:
                    vol_value = float(vol)
                except (TypeError, ValueError):
                    vol_value = 0
                items.append((pair, vol_value))
            # 按成交量降序排序
            items.sort(key=lambda x: x[1], reverse=True)
            pairlist = [p[0] for p in items]
        result = {"pairs": pairlist, "refresh_period": INTERVAL_SECONDS}
        body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_rating_changes(self):
        """返回评级信号记录，按交易对分组。

        查询参数:
          pair=XXX  — 可选，筛选指定交易对（不传则返回全部）
        """
        try:
            from urllib.parse import parse_qs

            params = parse_qs(urlparse(self.path).query)
            filter_pair = params.get("pair", [None])[0]

            conn = get_db()
            if filter_pair:
                rows = conn.execute(
                    "SELECT name, timestamp, price, signal, rating_val "
                    "FROM rating_changes WHERE name = ? ORDER BY timestamp DESC LIMIT 200",
                    (filter_pair,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name, timestamp, price, signal, rating_val "
                    "FROM rating_changes ORDER BY timestamp DESC LIMIT 200"
                ).fetchall()
            conn.close()

            pairs: dict[str, list[dict]] = {}
            total = 0
            for row in rows:
                name = row["name"].replace(".P", "")
                entry = {
                    "timestamp": row["timestamp"],
                    "price": row["price"],
                    "signal": row["signal"],
                    "rating_val": row["rating_val"],
                }
                pairs.setdefault(name, []).append(entry)
                total += 1

            data = {
                "count": total,
                "pairs": pairs,
            }
        except Exception:
            data = {"count": 0, "pairs": {}}
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    STRATEGIES_CONFIG = [
        {
            "name": "RatingSignalStrategy",
            "label": "评级信号策略",
            "db_path": str(
                Path(__file__).parent.parent / "freqtrade" / "tradesv3.dryrun.sqlite"
            ),
        },
        {
            "name": "ShortDeclineStrategy",
            "label": "短期下跌策略",
            "db_path": str(
                Path(__file__).parent.parent / "freqtrade" / "tradesv3_short.sqlite"
            ),
        },
    ]

    @staticmethod
    def _load_trades_from_db(db_path: str) -> list[dict]:
        """从 Freqtrade SQLite 加载所有交易记录。"""
        import sqlite3

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, pair, is_open, open_rate, close_rate, "
                "realized_profit, close_profit, close_profit_abs, "
                "stake_amount, amount, open_date, close_date, "
                "exit_reason, strategy, enter_tag, is_short, leverage, "
                "funding_fees "
                "FROM trades ORDER BY open_date DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            return [{"error": str(e)}]

    def _serve_strategies_json(self):
        """返回两个策略的交易数据。"""
        result = []
        for cfg in self.STRATEGIES_CONFIG:
            trades = self._load_trades_from_db(cfg["db_path"])
            open_trades = [t for t in trades if t["is_open"]]
            closed_trades = [t for t in trades if not t["is_open"]]
            total_profit = sum(t["close_profit_abs"] or 0 for t in closed_trades)
            win_trades = sum(1 for t in closed_trades if (t["close_profit"] or 0) > 0)
            result.append(
                {
                    "name": cfg["name"],
                    "label": cfg["label"],
                    "total_trades": len(trades),
                    "open_trades": len(open_trades),
                    "closed_trades": len(closed_trades),
                    "win_trades": win_trades,
                    "total_profit": round(total_profit, 2),
                    "win_rate": round(win_trades / len(closed_trades) * 100, 1)
                    if closed_trades
                    else 0,
                    "open": open_trades,
                    "closed": closed_trades[-20:],  # 最近20条已平仓
                }
            )
        body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_strategies_html(self):
        """策略监控看板 HTML。"""
        html = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>策略交易监控</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:#0b0e17; color:#e0e6f0; padding:20px; }
.container { max-width:1200px; margin:0 auto; }
h1 { color:#f0b90b; font-size:22px; margin-bottom:20px; }
.strategy-card { background:#111b26; border-radius:12px; border:1px solid #2a3a50;
                 padding:20px; margin-bottom:20px; }
.strategy-card h2 { font-size:18px; color:#f0b90b; margin-bottom:12px; }
.stats { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
.stat-item { background:#1a2633; padding:8px 16px; border-radius:8px;
             border:1px solid #2a3a50; }
.stat-item .label { font-size:11px; color:#7a8da0; }
.stat-item .value { font-size:18px; font-weight:600; color:#f0f4f8; }
.profit-pos { color:#2ecc71; }
.profit-neg { color:#ff6b6b; }
table { width:100%; border-collapse:collapse; margin-top:8px; }
th { background:#1a2332; padding:8px 12px; font-size:11px; font-weight:700;
     color:#7a8da0; text-align:left; border-bottom:1px solid #2a3a50; }
td { padding:8px 12px; font-size:13px; border-bottom:1px solid #1c2a3a; }
tr:hover td { background:#1a2635; }
.badge-open { color:#2ecc71; font-weight:700; }
.badge-closed { color:#7a8da0; }
.signal-long { color:#2ecc71; }
.signal-short { color:#ff6b6b; }
.nav { margin-bottom:16px; }
.nav a { color:#f0b90b; text-decoration:none; font-size:13px; padding:6px 14px;
         border:1px solid #2a3a50; border-radius:6px; background:#1a2633; }
.nav a:hover { background:#2a3a50; }
.loading { text-align:center; padding:40px; color:#6a7a8a; }
</style>
</head>
<body>
<div class="container">
    <div class="nav"><a href="/">← 返回扫描看板</a></div>
    <h1>📊 策略交易监控</h1>
    <div id="content"><div class="loading">加载中...</div></div>
</div>
<script>
async function loadData() {
    try {
        const r = await fetch('/api/strategies', { cache:'no-cache' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        let html = '';
        for (const s of data) {
            html += '<div class="strategy-card">' +
                '<h2>' + s.label + ' <small style="color:#7a8da0;font-size:13px">(' + s.name + ')</small></h2>' +
                '<div class="stats">' +
                '<div class="stat-item"><div class="label">总交易</div><div class="value">' + s.total_trades + '</div></div>' +
                '<div class="stat-item"><div class="label">持仓中</div><div class="value">' + s.open_trades + '</div></div>' +
                '<div class="stat-item"><div class="label">已完成</div><div class="value">' + s.closed_trades + '</div></div>' +
                '<div class="stat-item"><div class="label">胜率</div><div class="value">' + s.win_rate + '%</div></div>' +
                '<div class="stat-item"><div class="label">总盈亏</div><div class="value ' + (s.total_profit >= 0 ? 'profit-pos' : 'profit-neg') + '">' +
                (s.total_profit >= 0 ? '+' : '') + s.total_profit + ' USDT</div></div>' +
                '</div>';

            // 持仓
            if (s.open && s.open.length > 0) {
                html += '<h3 style="color:#2ecc71;font-size:14px;margin:8px 0">🔴 当前持仓</h3>' +
                    '<table><tr><th>交易对</th><th>方向</th><th>开仓价</th><th>杠杆</th><th>数量</th><th>开仓时间</th></tr>';
                for (const t of s.open) {
                    const dir = t.is_short ? '空' : '多';
                    const dirCls = t.is_short ? 'signal-short' : 'signal-long';
                    html += '<tr><td>' + esc(t.pair) + '</td>' +
                        '<td class="' + dirCls + '">' + dir + '</td>' +
                        '<td>' + fmtPrice(t.open_rate) + '</td>' +
                        '<td>' + (t.leverage || 1) + 'x</td>' +
                        '<td>' + fmtAmt(t.amount) + '</td>' +
                        '<td style="font-size:12px;color:#8a9aaa">' + esc(t.open_date || '') + '</td></tr>';
                }
                html += '</table>';
            } else {
                html += '<p style="color:#6a7a8a;font-size:13px">暂无持仓</p>';
            }

            // 最近成交
            if (s.closed && s.closed.length > 0) {
                html += '<h3 style="color:#7a8da0;font-size:14px;margin:12px 0 8px 0">📋 最近成交</h3>' +
                    '<table><tr><th>交易对</th><th>方向</th><th>开仓价</th><th>平仓价</th><th>盈亏</th><th>收益率</th><th>时间</th></tr>';
                for (const t of s.closed) {
                    const dir = t.is_short ? '空' : '多';
                    const dirCls = t.is_short ? 'signal-short' : 'signal-long';
                    const profit = t.close_profit_abs || 0;
                    const profitPct = t.close_profit ? (t.close_profit * 100).toFixed(2) + '%' : '-';
                    html += '<tr><td>' + esc(t.pair) + '</td>' +
                        '<td class="' + dirCls + '">' + dir + '</td>' +
                        '<td>' + fmtPrice(t.open_rate) + '</td>' +
                        '<td>' + fmtPrice(t.close_rate) + '</td>' +
                        '<td class="' + (profit >= 0 ? 'profit-pos' : 'profit-neg') + '">' +
                        (profit >= 0 ? '+' : '') + profit.toFixed(2) + '</td>' +
                        '<td class="' + (profit >= 0 ? 'profit-pos' : 'profit-neg') + '">' + profitPct + '</td>' +
                        '<td style="font-size:12px;color:#8a9aaa">' + esc(t.close_date || '') + '</td></tr>';
                }
                html += '</table>';
            }
            html += '</div>';
        }
        document.getElementById('content').innerHTML = html;
    } catch(e) {
        document.getElementById('content').innerHTML =
            '<div style="text-align:center;padding:40px;color:#ff6b6b">加载失败: ' + e.message + '</div>';
    }
}
function esc(s) { return String(s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]||c; }); }
function fmtPrice(v) { if (v==null) return '-'; const n=Number(v); if (n<0.001) return n.toFixed(6); if (n<1) return n.toFixed(4); return n.toFixed(2); }
function fmtAmt(v) { if (v==null) return '-'; return Number(v).toFixed(0); }
loadData();
setInterval(loadData, 10000);
</script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_entry_perf(self):
        """返回交易对入场时的三阶段涨幅数据。

        查询参数:
          pair=XXX  — 必选，交易对名称（.P 后缀可选）
        """
        try:
            from urllib.parse import parse_qs

            params = parse_qs(urlparse(self.path).query)
            raw_pair = params.get("pair", [None])[0]
            if not raw_pair:
                self._send_json({"error": "missing pair parameter"}, 400)
                return

            # 统一加 .P 后缀查询
            if not raw_pair.endswith(".P"):
                raw_pair += ".P"

            conn = get_db()
            row = conn.execute(
                "SELECT entry_time, perf_1w, perf_1m, perf_3m "
                "FROM symbols WHERE name = ?",
                (raw_pair,),
            ).fetchone()
            conn.close()

            if not row:
                self._send_json({"error": "pair not found", "pair": raw_pair}, 404)
                return

            data = {
                "entry_time": row["entry_time"],
                "perf_1w": row["perf_1w"],
                "perf_1m": row["perf_1m"],
                "perf_3m": row["perf_3m"],
            }
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ═══════════════════════════════════════════════
    # 模块三：HTML — 主看板页面
    # ═══════════════════════════════════════════════

    def _build_html(self) -> str:
        """构建 JS 动态渲染的 HTML 页面（主看板）。"""
        return """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>币安永续合约 - 成交量异动监控</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0b0e17; color: #e0e6f0; padding: 20px; }
.container { max-width: 1200px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #1a2332 0%, #0f1923 100%);
           border-radius: 12px; padding: 24px 28px; margin-bottom: 20px;
           border: 1px solid #2a3a50; }
.header h1 { font-size: 22px; color: #f0b90b; margin-bottom: 8px; }
.header h1 small { font-size: 14px; color: #7a8da0; font-weight: normal; }
.stats { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 12px; }
.stat-item { background: #1a2633; padding: 10px 18px; border-radius: 8px;
             border: 1px solid #2a3a50; min-width: 100px; }
.stat-item .label { font-size: 12px; color: #7a8da0; }
.stat-item .value { font-size: 20px; font-weight: 600; color: #f0f4f8; }
.stat-item .value.new { color: #2ecc71; animation: pulse-glow 1.5s ease-in-out infinite; }
.stat-item .value.exited { color: #ffa502; animation: pulse-glow-exit 1.5s ease-in-out infinite; }
@keyframes pulse-glow {
    0%, 100% { text-shadow: 0 0 8px rgba(46,204,113,0.4); }
    50% { text-shadow: 0 0 20px rgba(46,204,113,0.8); }
}
@keyframes pulse-glow-exit {
    0%, 100% { text-shadow: 0 0 8px rgba(255,165,2,0.4); }
    50% { text-shadow: 0 0 20px rgba(255,165,2,0.8); }
}
table { width: 100%; border-collapse: collapse; background: #111b26;
        border-radius: 12px; overflow: hidden; border: 1px solid #2a3a50;
        table-layout: auto; }
th { background: #1a2332; padding: 12px 16px; font-size: 11px; font-weight: 700;
     color: #7a8da0; text-transform: uppercase; letter-spacing: 0.8px;
     border-bottom: 1px solid #2a3a50; text-align: left; white-space: nowrap; }
td { padding: 12px 16px; font-size: 14px; border-bottom: 1px solid #1c2a3a;
     white-space: nowrap; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #1a2635; }

/* NEW 行（绿色竖条） */
tr.row-new td:first-child { position: relative; padding-left: 20px; }
tr.row-new td:first-child::before { content: '';
    position: absolute; left: 0; top: 4px; bottom: 4px; width: 4px;
    background: linear-gradient(180deg, #2ecc71, #27ae60);
    border-radius: 2px; box-shadow: 0 0 10px rgba(46,204,113,0.6); }
tr.row-new td { background: linear-gradient(135deg,
    rgba(46,204,113,0.12) 0%, rgba(46,204,113,0.03) 100%); }
tr.row-new:hover td { background: linear-gradient(135deg,
    rgba(46,204,113,0.22) 0%, rgba(46,204,113,0.06) 100%); }

/* EXIT 行 */
tr.row-exited td:first-child { position: relative; padding-left: 20px; }
tr.row-exited td:first-child::before { content: '';
    position: absolute; left: 0; top: 4px; bottom: 4px; width: 4px;
    background: linear-gradient(180deg, #ffa502, #e67e22);
    border-radius: 2px; box-shadow: 0 0 10px rgba(255,165,2,0.5); }
tr.row-exited td { background: linear-gradient(135deg,
    rgba(255,165,2,0.10) 0%, rgba(255,165,2,0.02) 100%); }
tr.row-exited:hover td { background: linear-gradient(135deg,
    rgba(255,165,2,0.18) 0%, rgba(255,165,2,0.05) 100%); }

.symbol-cell { font-weight: 600; color: #f0f4f8; }
.vol-change { color: #ff6b6b; font-weight: 600; }
.time-cell { font-size: 12px; color: #8a9aaa; white-space: nowrap; }
.time-exited { color: #ffa502; font-weight: 600; }
.footer { text-align: center; margin-top: 20px; color: #4a5a6a; font-size: 13px; }
.loading { text-align: center; padding: 60px; color: #6a7a8a; font-size: 16px; }
.loading-small { display:block; padding:20px; text-align:center; color:#6a7a8a; font-size:13px; }
.rating-detail-content { padding: 12px 16px; background: #0d1520; border-top: 1px solid #1c2a3a; }
.rating-detail-table { width:100%; border-collapse:collapse; margin:0; background:transparent; border:none; border-radius:0; }
.rating-detail-table th { background: #141e2c; padding:8px 12px; font-size:11px; font-weight:700; color:#7a8da0; border-bottom:1px solid #1c2a3a; }
.rating-detail-table td { padding:6px 12px; font-size:13px; border-bottom:1px solid #141e2c; }
.rating-detail-table tr:last-child td { border-bottom:none; }
.signal-strong-buy { color:#2ecc71; font-weight:700; }
.signal-buy { color:#55d388; font-weight:600; }
.signal-strong-sell { color:#ff6b6b; font-weight:700; }
.signal-sell { color:#ff8a8a; font-weight:600; }
.signal-neutral { color:#8a9aaa; font-weight:600; }
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 币安永续合约 · 24h成交量异动监控 <small>TradingView Scanner</small></h1>
        <div class="stats" id="stats">
            <div class="stat-item"><div class="label">最后更新</div><div class="value" id="stat-time">-</div></div>
            <div class="stat-item"><div class="label">当前结果</div><div class="value" id="stat-total">-</div></div>
            <div class="stat-item"><div class="label">30min内新增</div><div class="value" id="stat-new">-</div></div>
            <div class="stat-item"><div class="label">已退出</div><div class="value" id="stat-exited">-</div></div>
            <div class="stat-item"><div class="label">历史累计</div><div class="value" id="stat-hist">-</div></div>
        </div>
    </div>
    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>交易对</th>
                <th>价格</th>
                <th>24h量变化</th>
                <th>24h成交量</th>
                <th>24h价变化</th>
                <th>1周涨幅</th>
                <th>1月涨幅</th>
                <th>3月涨幅</th>
                <th>技术评级</th>
                <th>进入/退出时间</th>
            </tr>
        </thead>
        <tbody id="table-body"><tr><td colspan="11" style="text-align:center;padding:40px;color:#6a7a8a">加载中...</td></tr></tbody>
    </table>
    <div class="footer" id="footer">
        条件: 24h成交量变化 &gt; 500% &nbsp;|&nbsp; 更新间隔: 60s &nbsp;|&nbsp; 🟢 绿色竖条=30分钟内新增 &nbsp;|&nbsp; 🟠 橙色竖条=已退出
        &nbsp;|&nbsp; 点击"技术评级"列查看评级历史
        &nbsp;|&nbsp; <a href="javascript:void(0)" id="notif-btn" onclick="enableNotifications()" style="color:#f0b90b;text-decoration:none;font-weight:600">🔔 开启桌面通知</a>
        &nbsp;|&nbsp; <a href="/strategies" style="color:#f0b90b;text-decoration:none;font-weight:600">📊 策略监控</a>
    </div>
</div>
<script>
const MAX = 30;
// 浏览器桌面通知（须用户点击触发）
let _notified = new Set();
function enableNotifications() {
    if (!('Notification' in window)) { alert('当前浏览器不支持桌面通知'); return; }
    if (Notification.permission === 'granted') {
        new Notification('✅ 通知已开启', { body: '有新异动交易对时会收到通知' });
        document.getElementById('notif-btn').textContent = '🔔 通知已开启';
        document.getElementById('notif-btn').style.cursor = 'default';
        return;
    }
    if (Notification.permission === 'denied') {
        alert('通知已被拒绝，请在浏览器站点设置中重新允许通知');
        return;
    }
    Notification.requestPermission().then(function(p) {
        if (p === 'granted') {
            new Notification('✅ 通知已开启', { body: '有新异动交易对时会收到通知' });
            document.getElementById('notif-btn').textContent = '🔔 通知已开启';
            document.getElementById('notif-btn').style.cursor = 'default';
        } else {
            alert('通知被拒绝，可在浏览器设置中重新开启');
        }
    });
}
function notifyNewPairs(symbols) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const fresh = symbols.filter(s => !_notified.has(s));
    if (fresh.length === 0) return;
    fresh.forEach(s => _notified.add(s));
    const title = fresh.length === 1 ? '🆕 新异动交易对' : '🆕 新异动交易对 ×' + fresh.length;
    const body = fresh.length <= 5
        ? fresh.join(', ')
        : fresh.slice(0, 5).join(', ') + ' … 等' + fresh.length + '个';
    try { new Notification(title, { body }); } catch {}
}

let _ratingCache = {};

async function toggleRating(pairName, detailId) {
    const el = document.getElementById(detailId);
    if (!el) return;
    if (el.style.display === 'none' || !el.style.display) {
        el.style.display = '';
        const content = el.querySelector('.rating-detail-content');
        if (!content) return;
        // 加载缓存或首次请求
        if (!_ratingCache[pairName]) {
            try {
                const r = await fetch('/api/rating_changes?pair=' + encodeURIComponent(pairName + '.P'), { cache: 'no-cache' });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const data = await r.json();
                _ratingCache[pairName] = data;
            } catch(e) {
                content.innerHTML = '<span style="color:#ff6b6b">加载失败: ' + e.message + '</span>';
                return;
            }
        }
        renderRatingDetail(content, pairName, _ratingCache[pairName]);
    } else {
        el.style.display = 'none';
    }
}

function renderRatingDetail(el, pairName, data) {
    const pairs = data && data.pairs || {};
    const records = pairs[pairName] || [];
    if (records.length === 0) {
        el.innerHTML = '<span style="color:#6a7a8a">暂无评级历史记录</span>';
        return;
    }
    // 按时间降序排列（最新的在前）
    const sorted = [...records].sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    let html = '<table class="rating-detail-table"><tr><th>时间</th><th>信号</th><th>价格</th><th>评级值</th></tr>';
    for (const r of sorted) {
        const ts = esc(r.timestamp || '-');
        const price = r.price != null ? Number(r.price).toFixed(6) : '-';
        const sig = r.signal || '-';
        const val = r.rating_val != null ? Number(r.rating_val).toFixed(4) : '-';
        let sigClass = '';
        if (sig === '强烈买入') sigClass = 'signal-strong-buy';
        else if (sig === '买入') sigClass = 'signal-buy';
        else if (sig === '强烈卖出') sigClass = 'signal-strong-sell';
        else if (sig === '卖出') sigClass = 'signal-sell';
        else if (sig === '中性') sigClass = 'signal-neutral';
        html += '<tr><td class="time-cell">' + ts + '</td><td class="' + sigClass + '">' + esc(sig) + '</td><td>' + price + '</td><td style="color:#6a7a8a">' + val + '</td></tr>';
    }
    html += '</table>';
    el.innerHTML = html;
}

function esc(s) { return String(s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]||c; }); }
function fmt(n) { try { return Number(n).toLocaleString(); } catch { return '-'; } }
function fmt1(n) { try { return Number(n).toFixed(1) + '%'; } catch { return '-'; } }
function fmt2(n) { try { return Number(n).toFixed(2) + '%'; } catch { return '-'; } }

async function fetchData() {
    try {
        const api = '/api/data';
        const r = await fetch(api, { cache: 'no-cache' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();
        render(d);
    } catch(e) {
        document.getElementById('table-body').innerHTML =
            '<tr><td colspan="7" style="text-align:center;padding:40px;color:#ff6b6b;font-size:16px">\u26a0\ufe0f ' + e.message + '</td></tr>';
    }
}

function render(d) {
    // 统计
    document.getElementById('stat-time').textContent = d.timestamp || '-';
    document.getElementById('stat-total').textContent = d.total;
    const newEl = document.getElementById('stat-new');
    newEl.textContent = d.latest_new_count;
    newEl.className = 'value' + (d.latest_new_count > 0 ? ' new' : '');
    const extEl = document.getElementById('stat-exited');
    extEl.textContent = d.latest_exited_count;
    extEl.className = 'value' + (d.latest_exited_count > 0 ? ' exited' : '');
    document.getElementById('stat-hist').textContent = d.history_total;

    // 浏览器通知：新出现的交易对
    if (d.latest_new_symbols && d.latest_new_symbols.length > 0) {
        notifyNewPairs(d.latest_new_symbols);
    }

    // 表
    const results = d.results || [];
    const limit = Math.min(results.length, MAX);
    const newSet = new Set(d.latest_new_symbols || []);
    const extSet = new Set(d.latest_exited_symbols || []);

    // 格式化涨跌幅（移出循环避免重复定义）
    function fmtPerf(v) {
        if (v == null) return '-';
        const n = Number(v);
        const s = n.toFixed(2) + '%';
        return n >= 0 ? '<span style="color:#2ecc71">+' + s + '</span>'
                     : '<span style="color:#ff6b6b">' + s + '</span>';
    }

    let html = '';
    for (let i = 0; i < limit; i++) {
        const r = results[i];
        const name = esc(r.name);
        const isNew = newSet.has(r.name);
        const isExt = extSet.has(r.name);
        const rowClass = isNew ? 'row-new' : (isExt ? 'row-exited' : '');

        let badge = '';  // 只用颜色竖条，不需要文字徽章

        const price = r.price ? esc(String(r.price)) : '-';
        const volChg = fmt1(r.vol_change_24h_pct);
        const vol = fmt(r.vol_24h);
        const priceChg = fmt2(r.price_change_24h_pct);

        // 技术评级（点击展开历史）
        const pairName = r.name.replace('.P', '');
        const rating = r.rating_text || '-';
        // 表现
        const perf1w = fmtPerf(r.perf_1w);
        const perf1m = fmtPerf(r.perf_1m);
        const perf3m = fmtPerf(r.perf_3m);

        let timeLabel, timeClass;
        if (isExt) {
            timeLabel = r.exit_time || '-';
            timeClass = 'time-cell time-exited';
        } else {
            timeLabel = r.entry_time || '-';
            timeClass = 'time-cell';
        }

        const rowId = 'pair-' + i;
        html += '<tr class="' + rowClass + '" id="' + rowId + '-main">' +
            '<td>' + (i + 1) + '</td>' +
            '<td class="symbol-cell">' + name + badge + '</td>' +
            '<td>' + price + '</td>' +
            '<td class="vol-change">' + volChg + '</td>' +
            '<td>' + vol + '</td>' +
            '<td>' + priceChg + '</td>' +
            '<td style="font-size:12px">' + perf1w + '</td>' +
            '<td style="font-size:12px">' + perf1m + '</td>' +
            '<td style="font-size:12px">' + perf3m + '</td>' +
            '<td style="font-size:12px;cursor:pointer" onclick="toggleRating(\\'' + esc(pairName) + '\\',\\'' + rowId + '-detail\\')" title="点击查看评级历史">' + rating + '</td>' +
            '<td class="' + timeClass + '">' + timeLabel + '</td>' +
            '</tr>' +
            '<tr id="' + rowId + '-detail" class="rating-detail" style="display:none">' +
            '<td colspan="11" style="padding:0"><div class="rating-detail-content"><span class="loading-small">加载中...</span></div></td>' +
            '</tr>';
    }

    if (results.length > MAX) {
        html += '<tr><td colspan="7" style="text-align:center;padding:8px;color:#6a7a8a;font-size:13px">... 还有 ' + (results.length - MAX) + ' 个未显示</td></tr>';
    }

    document.getElementById('table-body').innerHTML = html || '<tr><td colspan="7" style="text-align:center;padding:40px;color:#6a7a8a;font-size:16px">暂无数据</td></tr>';
}

fetchData();
setInterval(fetchData, 10000);
</script>
</body>
</html>"""

    def log_message(self, format, *args):
        """抑制 HTTP 日志输出，保持终端干净。"""
        pass


# ═══════════════════════════════════════════════
# 模块一：扫描 + 记录
# ═══════════════════════════════════════════════


def start_http_server():
    """在后台线程启动 HTTP 服务器。"""
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), VolumeSurgeHandler)
    server.timeout = 0.5
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="HttpServer"
    )
    thread.start()
    return server, thread


def _to_float_or_none(val):
    """安全转换为 float，无效值返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def get_binance_perpetual_volume_surge(
    min_vol_change_pct: float = MIN_VOL_CHANGE_PCT,
    max_results: int = MAX_RESULTS,
    sort_by: str = "24h_vol_change|5",
) -> list[dict]:
    """获取币安永续合约中 24h 成交量变化超过指定百分比的交易对。"""
    url = "https://scanner.tradingview.com/crypto/scan"

    payload = {
        "symbols": {},
        "columns": [
            "name",
            "close",
            "type",
            "exchange",
            "24h_vol_change|5",
            "24h_vol|5",
            "24h_close_change|5",
            "change|240",
            "currency",
            "Recommend.All|15",
            "Recommend.MA|15",
            "Recommend.Other|15",
            "Perf.W",
            "Perf.1M",
            "Perf.3M",
        ],
        "filter2": {
            "operator": "and",
            "operands": [
                {
                    "expression": {
                        "left": "centralization",
                        "operation": "equal",
                        "right": "cex",
                    }
                },
                {"expression": {"left": "type", "operation": "equal", "right": "swap"}},
                {
                    "expression": {
                        "left": "exchange",
                        "operation": "equal",
                        "right": "BINANCE",
                    }
                },
                {
                    "expression": {
                        "left": "currency",
                        "operation": "equal",
                        "right": "USDT",
                    }
                },
                {
                    "expression": {
                        "left": "24h_vol_change|5",
                        "operation": "greater",
                        "right": min_vol_change_pct,
                    }
                },
            ],
        },
        "sort": {"sortBy": sort_by, "sortOrder": "desc"},
        "range": [0, max_results],
        "options": {"lang": "en"},
        "markets": ["crypto"],
    }

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("data", []):
        try:
            d = item.get("d")
            if not isinstance(d, (list, tuple)) or len(d) < 6:
                continue
            results.append(
                {
                    "symbol": item.get("s", ""),
                    "name": d[0] or "",
                    "price": _to_float_or_none(d[1]),
                    "type": d[2] or "",
                    "exchange": d[3] or "",
                    "vol_change_24h_pct": _to_float_or_none(d[4]),
                    "vol_24h": _to_float_or_none(d[5]),
                    "price_change_24h_pct": _to_float_or_none(d[6])
                    if len(d) > 6
                    else None,
                    "price_change_4h_pct": (
                        (_to_float_or_none(d[7]) / _to_float_or_none(d[1]) * 100)
                        if len(d) > 7 and _to_float_or_none(d[1])
                        else None
                    ),
                    "currency": d[8] if len(d) > 8 else None,
                    "recommend_all": _to_float_or_none(d[9]) if len(d) > 9 else None,
                    "recommend_ma": _to_float_or_none(d[10]) if len(d) > 10 else None,
                    "recommend_other": _to_float_or_none(d[11])
                    if len(d) > 11
                    else None,
                    "perf_1w": _to_float_or_none(d[12]) if len(d) > 12 else None,
                    "perf_1m": _to_float_or_none(d[13]) if len(d) > 13 else None,
                    "perf_3m": _to_float_or_none(d[14]) if len(d) > 14 else None,
                }
            )
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return results


def get_db() -> sqlite3.Connection:
    """获取数据库连接（每次调用创建新连接，线程安全）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表，并迁移旧 CSV 数据（如有）。"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            name        TEXT PRIMARY KEY,
            entry_time  TEXT NOT NULL,
            exit_time   TEXT,
            symbol      TEXT NOT NULL,
            perf_1w     REAL,
            perf_1m     REAL,
            perf_3m     REAL
        )
    """)
    # 兼容旧表：如果缺少涨幅列则补齐
    for col in ("perf_1w", "perf_1m", "perf_3m"):
        try:
            conn.execute(f"ALTER TABLE symbols ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute("""
        DROP TABLE IF EXISTS rating_changes
    """)
    conn.execute("""
        CREATE TABLE rating_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            price       REAL,
            signal      TEXT NOT NULL,
            rating_val  REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rating_changes_name
        ON rating_changes(name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rating_changes_ts
        ON rating_changes(timestamp)
    """)
    conn.commit()
    conn.close()


def load_from_db():
    """从数据库加载所有记录到内存缓存。返回 (seen_symbols, entry_time, exit_time)。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT name, entry_time, exit_time FROM symbols ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()

    seen: set[str] = set()
    entry: dict[str, str] = {}
    exit_t: dict[str, str] = {}

    for row in rows:
        name = row["name"]
        seen.add(name)
        if name not in entry:
            entry[name] = row["entry_time"]
        if row["exit_time"] and name not in exit_t:
            exit_t[name] = row["exit_time"]

    G.seen_symbols = seen
    G.entry_time = entry
    G.exit_time = exit_t
    print(f"  从数据库加载 {len(seen)} 个交易对记录")
    return seen


def save_to_db(new_entries: list[dict], timestamp: str) -> tuple[int, list[str]]:
    """保存新增交易对到数据库，并清理超出 MAX_HISTORY_RECORDS 的旧数据。
    返回 (新增数量, 新增name列表)。"""

    fresh = [r for r in new_entries if r["name"] not in G.seen_symbols]
    if not fresh:
        return 0, []

    conn = get_db()
    fresh_names = []
    active_names = [r["name"] for r in new_entries]
    for r in fresh:
        conn.execute(
            "INSERT OR IGNORE INTO symbols (name, entry_time, symbol, perf_1w, perf_1m, perf_3m) VALUES (?, ?, ?, ?, ?, ?)",
            (
                r["name"],
                timestamp,
                r["symbol"],
                r.get("perf_1w"),
                r.get("perf_1m"),
                r.get("perf_3m"),
            ),
        )
        G.seen_symbols.add(r["name"])
        G.entry_time[r["name"]] = timestamp
        fresh_names.append(r["name"])

    # 清理：保留当前活跃交易对，只删除超过上限的旧历史记录
    placeholders = ",".join("?" for _ in active_names)
    stale_rows = conn.execute(
        f"""
        SELECT name FROM symbols
        WHERE name NOT IN ({placeholders})
        ORDER BY entry_time DESC
        LIMIT -1 OFFSET ?
        """,
        (*active_names, MAX_HISTORY_RECORDS),
    ).fetchall()
    stale_names = [row["name"] for row in stale_rows]
    if stale_names:
        stale_placeholders = ",".join("?" for _ in stale_names)
        conn.execute(
            f"DELETE FROM symbols WHERE name IN ({stale_placeholders})",
            stale_names,
        )
        for name in stale_names:
            G.seen_symbols.discard(name)
            G.entry_time.pop(name, None)
            G.exit_time.pop(name, None)
    conn.commit()
    conn.close()
    return len(fresh), fresh_names


def sort_by_status_and_time(
    results: list[dict],
    exited_names: set[str],
) -> list[dict]:
    """排序：进入的在上（按进入时间降序），退出的在下（按退出时间降序）。"""
    active = [r for r in results if r["name"] not in exited_names]
    exited = [r for r in results if r["name"] in exited_names]
    active.sort(key=lambda r: G.entry_time.get(r["name"], ""), reverse=True)
    exited.sort(key=lambda r: G.exit_time.get(r["name"], ""), reverse=True)
    return active + exited


def update_exit_in_db(name: str, exit_timestamp: str):
    """更新交易对的退出时间。"""
    conn = get_db()
    conn.execute(
        "UPDATE symbols SET exit_time = ? WHERE name = ?", (exit_timestamp, name)
    )
    conn.commit()
    conn.close()
    G.exit_time[name] = exit_timestamp


def _rating_to_text(rating: float) -> str:
    """将 recommend_all 数值转为中文文字评级（TradingView 官方标准）。"""
    if rating > 0.5:
        return "强烈买入"
    elif rating > 0.1:
        return "买入"
    elif rating >= -0.1:
        return "中性"
    elif rating > -0.5:
        return "卖出"
    else:
        return "强烈卖出"


def record_rating_changes(active_data: list[dict], timestamp: str) -> None:
    """记录评级信号变化。

    规则：
      - 交易对首次进入列表 → 记录 TV 官方评级，含时间和价格
      - 后期 TV 评级文字变化时 → 记录新信号、时间、价格
      - 同等级内的数值波动不记录
    """
    changes = []

    for r in active_data:
        name = r.get("name", "")
        if not name:
            continue
        new_rating = r.get("recommend_all")
        price = r.get("price")
        if new_rating is None:
            continue
        try:
            new_val = float(new_rating)
        except (TypeError, ValueError):
            continue

        old_val = G.current_ratings.get(name)

        # ── 首次出现：记录 TV 官方评级 ──
        if old_val is None:
            changes.append((name, timestamp, price, _rating_to_text(new_val), new_val))
            G.current_ratings[name] = new_val
            continue

        # 数值未变化，跳过
        if old_val == new_val:
            continue

        new_text = _rating_to_text(new_val)
        old_text = _rating_to_text(old_val)

        # ── TV 评级文字变化时记录 ──
        if old_text != new_text:
            changes.append((name, timestamp, price, new_text, new_val))

        # 更新缓存
        G.current_ratings[name] = new_val

    if not changes:
        return

    # 写入数据库
    conn = get_db()
    conn.executemany(
        """INSERT INTO rating_changes
           (name, timestamp, price, signal, rating_val)
           VALUES (?, ?, ?, ?, ?)""",
        changes,
    )
    conn.commit()
    conn.close()

    # 控制台输出
    print(f"\n  📊 评级信号记录 ({len(changes)} 个):")
    for c in changes:
        name = c[0].replace(".P", "")
        sig = c[3]
        val = c[4]
        if sig in ("强烈买入", "强烈卖出"):
            arrow = "🟢" if "买入" in sig else "🔴"
            print(f"    {arrow} {name:<20s}  ⚡{sig}  (值={val:+.4f})")
        else:
            print(f"    📥 {name:<20s}  初始{sig}  (值={val:+.4f})")
    print()


def print_results(
    results: list[dict],
    timestamp: str,
    new_count: int,
    new_names: set[str] | None = None,
    exited_names: set[str] | None = None,
) -> None:
    """格式化打印结果（最多显示 MAX_DISPLAY_RESULTS 条）。"""
    new_names = new_names or set()
    exited_names = exited_names or set()
    display = results[:MAX_DISPLAY_RESULTS]
    print(f"\n{'=' * 95}")
    print(f"  时间: {timestamp}")
    print(f"  条件: 币安永续合约 24h成交量涨跌 > {MIN_VOL_CHANGE_PCT}%")
    print(
        f"  显示 {len(results)} 个 | {ACTIVE_DURATION_MINUTES}min内新增 {new_count} 个 | 已退出 {len(exited_names)} 个 | 历史累计 {len(G.seen_symbols)} 个"
    )
    print(f"{'=' * 95}")

    if not display:
        print("  (无符合条件的交易对)")
        return

    print(
        f"{'排名':<4} {'交易对':<22} {'价格':>10} {'24h量变化':>12} {'24h成交量':>18} {'24h价变化':>10} {'时间':<20} {'标记':<8}"
    )
    print("-" * 114)
    for i, r in enumerate(display, 1):
        name = r.get("name", "")
        if not name:
            continue
        price = f"{r.get('price', '')}" if r.get("price") else "-"
        try:
            vol_chg = f"{float(r['vol_change_24h_pct']):.1f}%"
        except (ValueError, TypeError):
            vol_chg = "-"
        try:
            vol = f"{float(r['vol_24h']):,.0f}"
        except (ValueError, TypeError):
            vol = "-"
        try:
            price_chg = f"{float(r['price_change_24h_pct']):.2f}%"
        except (ValueError, TypeError):
            price_chg = "-"
        if name in new_names:
            tag = "🆕 NEW"
        elif name in exited_names:
            tag = "🚫 EXIT"
        else:
            tag = ""
        # 时间列
        t = G.entry_time.get(name, "")
        if name in exited_names:
            t = G.exit_time.get(name, t)
        print(
            f"{i:<4} {name:<22} {price:>10} {vol_chg:>12} {vol:>18} {price_chg:>10} {t:<20} {tag:<8}"
        )
    if len(results) > MAX_DISPLAY_RESULTS:
        print(f"  ... 还有 {len(results) - MAX_DISPLAY_RESULTS} 个未显示")


def main_loop():
    """主循环：定时获取数据并保存。"""
    # 初始化数据库 + 加载已有记录
    init_db()
    G.seen_symbols = load_from_db()

    # 启动 HTTP 服务器
    http_server, _ = start_http_server()

    print("启动定时监控...")
    print(
        f"  间隔: {INTERVAL_SECONDS}s ({INTERVAL_SECONDS // 60}分{INTERVAL_SECONDS % 60}秒)"
    )
    print(f"  阈值: 24h成交量变化 > {MIN_VOL_CHANGE_PCT}%")
    print(f"  最多显示: {MAX_DISPLAY_RESULTS} 条")
    print(f"  数据库最多保留: {MAX_HISTORY_RECORDS} 条记录")
    print(f"  交易对活跃时长: {ACTIVE_DURATION_MINUTES} 分钟")
    print(f"  数据库: {DB_PATH}")
    print(f"  历史已记录: {len(G.seen_symbols)} 个交易对")
    print(f"  HTTP 显示: http://{HTTP_HOST}:{HTTP_PORT}")
    print("  按 Ctrl+C 停止\n")

    error_count = 0
    while _running:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            results = get_binance_perpetual_volume_surge()
            current_names = {r["name"] for r in results}
            name_to_data = {r["name"]: r for r in results}

            # ── 检测退出并写入DB ──
            newly_exited = set()
            if G.previous_scan_symbols:
                newly_exited = G.previous_scan_symbols - current_names
                for en in newly_exited:
                    update_exit_in_db(en, timestamp)
            G.previous_scan_symbols = set(current_names)

            # ── 构建活跃交易对数据列表 ──
            active_data = []
            for name in current_names:
                active_data.append(name_to_data[name])

            # 保存新增到DB
            save_to_db(active_data, timestamp)

            # ── 回填已有记录的空涨幅（仅一次，旧数据迁移） ──
            backfill_needed = False
            for r in active_data:
                if r.get("perf_1w") is not None and r["name"] in G.seen_symbols:
                    backfill_needed = True
                    break
            if backfill_needed:
                conn = get_db()
                conn.executemany(
                    "UPDATE symbols SET perf_1w = COALESCE(perf_1w, ?), perf_1m = COALESCE(perf_1m, ?), perf_3m = COALESCE(perf_3m, ?) WHERE name = ?",
                    [
                        (
                            r.get("perf_1w"),
                            r.get("perf_1m"),
                            r.get("perf_3m"),
                            r["name"],
                        )
                        for r in active_data
                    ],
                )
                conn.commit()
                conn.close()

            # ── 重新进入检测：已退出的交易对再次出现时，重置 entry_time ──
            now_str = timestamp
            for name in list(current_names):
                if name in G.exit_time:
                    conn = get_db()
                    conn.execute(
                        "UPDATE symbols SET exit_time = NULL, entry_time = ? WHERE name = ?",
                        (now_str, name),
                    )
                    conn.commit()
                    conn.close()
                    G.entry_time[name] = now_str
                    del G.exit_time[name]

            # ---- NEW 标记：ACTIVE_DURATION_MINUTES 内进入的 ----
            new_cutoff = (
                datetime.now() - timedelta(minutes=ACTIVE_DURATION_MINUTES)
            ).strftime("%Y-%m-%d %H:%M:%S")
            new_names_set = {
                r["name"]
                for r in active_data
                if G.entry_time.get(r["name"], "") >= new_cutoff
            }

            # ---- EXIT 标记：DB中所有有退出时间的 ----
            exited_names_set = set(G.exit_time.keys())

            # ---- 从DB获取已退出交易对，追加到显示列表 ----
            all_display = list(active_data)
            active_names = {r["name"] for r in active_data}
            conn = get_db()
            exited_rows = conn.execute(
                "SELECT name, symbol, exit_time FROM symbols WHERE exit_time IS NOT NULL ORDER BY exit_time DESC"
            ).fetchall()
            conn.close()
            for row in exited_rows:
                name = row["name"]
                if name not in active_names:
                    all_display.append(
                        {
                            "name": name,
                            "symbol": row["symbol"],
                            "price": "",
                            "type": "",
                            "exchange": "",
                            "vol_change_24h_pct": "",
                            "vol_24h": "",
                            "price_change_24h_pct": "",
                            "currency": "",
                            "recommend_all": None,
                            "recommend_ma": None,
                            "recommend_other": None,
                            "perf_1w": None,
                            "perf_1m": None,
                            "perf_3m": None,
                        }
                    )

            # 排序：进入的在上，退出的在下
            all_display = sort_by_status_and_time(all_display, exited_names_set)

            print_results(
                all_display,
                timestamp,
                len(new_names_set),
                new_names_set,
                exited_names_set,
            )
            error_count = 0

            # 更新 HTTP 全局状态
            with G.lock:
                # G.latest_scan 用于页面展示，包含当前活跃和已退出记录
                G.latest_scan = list(all_display)
                # G.latest_active_scan 用于 Freqtrade 拉取，与看板数据一致
                G.latest_active_scan = list(active_data)
                G.latest_timestamp = timestamp
                G.latest_new_symbols = new_names_set
                G.latest_exited_symbols = exited_names_set

            # 记录交易对的评级变化（基于扫描原始数据，与看板一致）
            record_rating_changes(active_data, timestamp)
        except Exception as e:
            error_count += 1
            print(f"  [{timestamp}] 错误: {e}")
            if error_count >= 5:
                print(
                    f"  连续 {error_count} 次失败，将在下一次 {INTERVAL_SECONDS} 秒轮询时重试..."
                )

        # 等待下一次更新（支持 Ctrl+C 即时中断）
        for _ in range(INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    # 关闭 HTTP 服务器
    http_server.shutdown()
    print(f"\n已停止。历史数据保存在: {DB_PATH}")


if __name__ == "__main__":
    main_loop()
