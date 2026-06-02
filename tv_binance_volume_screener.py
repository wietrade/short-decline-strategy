"""
TradingView 筛选器：定时获取币安永续合约中 24h 成交量涨跌 > 800% 的交易对
每分钟更新一次，历史数据保存在 SQLite 数据库（最多保留 50 条）。
使用 TradingView Scanner API (非官方)
"""

import json
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# ==================== 配置 ====================
MIN_VOL_CHANGE_PCT = 800  # 24h 成交量变化最小百分比
MAX_RESULTS = 200  # 最大结果数
INTERVAL_SECONDS = 60  # 更新间隔（秒），60 = 1分钟
DB_PATH = Path(__file__).parent / "data" / "volume_surge.db"
HTTP_PORT = 3000  # HTTP 服务器端口
MAX_DISPLAY_RESULTS = 30  # 最多显示记录数
MAX_HISTORY_RECORDS = 50  # 数据库最多保留记录数
# ==============================================

# 全局状态
_running = True
_seen_symbols: set[str] = set()  # 已写入 CSV 的交易对（内存缓存）


def signal_handler(sig, frame):
    global _running
    print("\n\n收到停止信号，正在退出...")
    _running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ================ HTTP 服务器全局状态 ================
_latest_scan: list[dict] = []
_latest_new_symbols: set[str] = set()
_latest_exited_symbols: set[str] = set()
_latest_timestamp: str = ""
_scan_lock = threading.Lock()
# ====================================================

# 上一次扫描的交易对名称集合，用于检测退出
_previous_scan_symbols: set[str] = set()

# 交易对进入/退出时间记录
_symbol_entry_time: dict[str, str] = {}  # name -> 首次进入时间
_symbol_exit_time: dict[str, str] = {}  # name -> 退出时间


class VolumeSurgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：显示最新的扫描结果。"""

    def do_GET(self):
        if self.path == "/api/data":
            self._serve_json()
        elif self.path == "/" or self.path == "/index.html":
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _serve_json(self):
        with _scan_lock:
            display = _latest_scan[:MAX_DISPLAY_RESULTS]
            # 为每个结果附加时间信息
            enriched = []
            for r in display:
                item = dict(r)
                item["entry_time"] = _symbol_entry_time.get(r["name"], "")
                item["exit_time"] = _symbol_exit_time.get(r["name"], "")
                enriched.append(item)
            data = {
                "timestamp": _latest_timestamp,
                "total": len(_latest_scan),
                "displayed": len(display),
                "history_total": len(_seen_symbols),
                "latest_new_count": len(_latest_new_symbols),
                "latest_exited_count": len(_latest_exited_symbols),
                "results": enriched,
                "latest_new_symbols": list(_latest_new_symbols),
                "latest_exited_symbols": list(_latest_exited_symbols),
            }
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(
            json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        )

    def _serve_html(self):
        html = self._build_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _build_html(self) -> str:
        """构建美观的 HTML 页面。"""
        with _scan_lock:
            display = _latest_scan[:MAX_DISPLAY_RESULTS]
            rows_html = ""
            for i, r in enumerate(display, 1):
                name = r["name"]
                price = f"{r['price']}" if r.get("price") else "-"
                vol_chg = (
                    f"{r['vol_change_24h_pct']:.1f}%"
                    if r.get("vol_change_24h_pct") != ""
                    else "-"
                )
                vol = f"{r['vol_24h']:,.0f}" if r.get("vol_24h") != "" else "-"
                price_chg = (
                    f"{r['price_change_24h_pct']:.2f}%"
                    if r.get("price_change_24h_pct")
                    else "-"
                )
                is_new = name in _latest_new_symbols
                is_exited = name in _latest_exited_symbols
                if is_new:
                    row_class = "row-new"
                    badge = '<span class="badge-new">🆕 NEW</span>'
                elif is_exited:
                    row_class = "row-exited"
                    badge = '<span class="badge-exited">🚫 EXIT</span>'
                else:
                    row_class = ""
                    badge = ""
                # 时间列：进入显示进入时间，退出显示退出时间（不显示进入时间）
                if is_exited:
                    time_label = _symbol_exit_time.get(name, "")
                    time_cell_class = "time-cell time-exited"
                else:
                    time_label = _symbol_entry_time.get(name, "")
                    time_cell_class = "time-cell"
                rows_html += f"""\
            <tr class="{row_class}">
                <td>{i}</td>
                <td class="symbol-cell">{name}{badge}</td>
                <td>{price}</td>
                <td class="vol-change">{vol_chg}</td>
                <td>{vol}</td>
                <td>{price_chg}</td>
                <td class="{time_cell_class}">{time_label}</td>
            </tr>
"""
            timestamp = _latest_timestamp
            total = len(_latest_scan)
            displayed = len(display)
            hist = len(_seen_symbols)
            new_count = len(_latest_new_symbols)
            exited_count = len(_latest_exited_symbols)

        return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>币安永续合约 - 成交量异动监控</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0b0e17; color: #e0e6f0; padding: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1a2332 0%, #0f1923 100%);
           border-radius: 12px; padding: 24px 28px; margin-bottom: 20px;
           border: 1px solid #2a3a50; }}
.header h1 {{ font-size: 22px; color: #f0b90b; margin-bottom: 8px; }}
.header h1 small {{ font-size: 14px; color: #7a8da0; font-weight: normal; }}
.stats {{ display: flex; gap: 20px; flex-wrap: wrap; margin-top: 12px; }}
.stat-item {{ background: #1a2633; padding: 10px 18px; border-radius: 8px;
              border: 1px solid #2a3a50; }}
.stat-item .label {{ font-size: 12px; color: #7a8da0; }}
.stat-item .value {{ font-size: 20px; font-weight: 600; color: #f0f4f8; }}
.stat-item .value.new {{ color: #ff6b6b; animation: pulse-glow 1.5s ease-in-out infinite; }}
.stat-item .value.exited {{ color: #ffa502; animation: pulse-glow-exit 1.5s ease-in-out infinite; }}
@keyframes pulse-glow {{
    0%, 100% {{ text-shadow: 0 0 8px rgba(255,107,107,0.4); }}
    50% {{ text-shadow: 0 0 20px rgba(255,107,107,0.8); }}
}}
@keyframes pulse-glow-exit {{
    0%, 100% {{ text-shadow: 0 0 8px rgba(255,165,2,0.4); }}
    50% {{ text-shadow: 0 0 20px rgba(255,165,2,0.8); }}
}}
table {{ width: 100%; border-collapse: collapse; background: #111b26;
        border-radius: 12px; overflow: hidden; border: 1px solid #2a3a50; }}
th {{ background: #1a2332; padding: 12px 16px; text-align: left;
     font-size: 13px; font-weight: 600; color: #7a8da0; text-transform: uppercase;
     letter-spacing: 0.5px; border-bottom: 1px solid #2a3a50; }}
td {{ padding: 12px 16px; border-bottom: 1px solid #1c2a3a; font-size: 14px; }}
tr:last-child td {{ border-bottom: none; }}
tr:hover {{ background: #1a2635; }}

/* ===== 🆕 新增行 ===== */
tr.row-new {{
    background: linear-gradient(135deg, rgba(255, 107, 107, 0.12) 0%, rgba(255, 107, 107, 0.03) 100%);
}}
tr.row-new:hover {{
    background: linear-gradient(135deg, rgba(255, 107, 107, 0.22) 0%, rgba(255, 107, 107, 0.06) 100%);
}}
tr.row-new td:first-child {{
    padding-left: 20px; position: relative;
}}
tr.row-new td:first-child::before {{
    content: '';
    position: absolute; left: 0; top: 4px; bottom: 4px; width: 4px;
    background: linear-gradient(180deg, #ff6b6b, #ee5a24);
    border-radius: 2px;
    box-shadow: 0 0 10px rgba(255, 107, 107, 0.6);
}}

/* ===== 🚫 退出行 ===== */
tr.row-exited {{
    background: linear-gradient(135deg, rgba(255, 165, 2, 0.10) 0%, rgba(255, 165, 2, 0.02) 100%);
}}
tr.row-exited:hover {{
    background: linear-gradient(135deg, rgba(255, 165, 2, 0.18) 0%, rgba(255, 165, 2, 0.05) 100%);
}}
tr.row-exited td:first-child {{
    padding-left: 20px; position: relative;
}}
tr.row-exited td:first-child::before {{
    content: '';
    position: absolute; left: 0; top: 4px; bottom: 4px; width: 4px;
    background: linear-gradient(180deg, #ffa502, #e67e22);
    border-radius: 2px;
    box-shadow: 0 0 10px rgba(255, 165, 2, 0.5);
}}

.symbol-cell {{ font-weight: 600; color: #f0f4f8; }}

/* ===== NEW 徽章 ===== */
.badge-new {{
    display: inline-block; margin-left: 8px; padding: 3px 10px;
    background: linear-gradient(135deg, #ff6b6b, #ee5a24, #ff6b6b);
    background-size: 200% 100%;
    border-radius: 12px; font-size: 11px; font-weight: 700;
    color: white; letter-spacing: 0.5px;
    box-shadow: 0 0 12px rgba(255, 107, 107, 0.4);
    animation: badge-shine 2s ease-in-out infinite, pulse-glow 1.5s ease-in-out infinite;
}}

/* ===== EXIT 徽章 ===== */
.badge-exited {{
    display: inline-block; margin-left: 8px; padding: 3px 10px;
    background: linear-gradient(135deg, #ffa502, #e67e22, #ffa502);
    background-size: 200% 100%;
    border-radius: 12px; font-size: 11px; font-weight: 700;
    color: white; letter-spacing: 0.5px;
    box-shadow: 0 0 10px rgba(255, 165, 2, 0.3);
    animation: badge-shine 2.5s ease-in-out infinite, pulse-glow-exit 1.5s ease-in-out infinite;
}}

@keyframes badge-shine {{
    0% {{ background-position: 0% 50%; }}
    50% {{ background-position: 100% 50%; }}
    100% {{ background-position: 0% 50%; }}
}}

.vol-change {{ color: #ff6b6b; font-weight: 600; }}
.time-cell {{ font-size: 12px; color: #8a9aaa; white-space: nowrap; }}
.time-exited {{ color: #ffa502; font-weight: 600; }}
.footer {{ text-align: center; margin-top: 20px; color: #4a5a6a; font-size: 13px; }}
.no-data {{ text-align: center; padding: 40px; color: #6a7a8a; font-size: 16px; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 币安永续合约 · 24h成交量异动监控 <small>TradingView Scanner</small></h1>
        <div class="stats">
            <div class="stat-item">
                <div class="label">最后更新</div>
                <div class="value">{timestamp or "等待中..."}</div>
            </div>
            <div class="stat-item">
                <div class="label">当前结果</div>
                <div class="value">{total}</div>
            </div>
            <div class="stat-item">
                <div class="label">1h内新增</div>
                <div class="value new">{new_count}</div>
            </div>
            <div class="stat-item">
                <div class="label">已退出</div>
                <div class="value exited">{exited_count}</div>
            </div>
            <div class="stat-item">
                <div class="label">历史累计</div>
                <div class="value">{hist}</div>
            </div>
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
                <th>进入/退出时间</th>
            </tr>
        </thead>
        <tbody>
            {rows_html if rows_html else '<tr><td colspan="7" class="no-data">暂无数据，等待首次扫描...</td></tr>'}
        </tbody>
    </table>
    <div class="footer">
        页面每 60 秒自动刷新 &nbsp;|&nbsp; 条件: 24h成交量变化 &gt; {MIN_VOL_CHANGE_PCT}% &nbsp;|&nbsp;
        更新间隔: {INTERVAL_SECONDS}s &nbsp;|&nbsp; 显示 {displayed}/{total} 条 &nbsp;|&nbsp;
        🆕 红色行=1小时内新增 &nbsp;|&nbsp; 🚫 橙色行=已退出交易对
    </div>
</div>
</body>
</html>"""

    def log_message(self, format, *args):
        """抑制 HTTP 日志输出，保持终端干净。"""
        pass


def start_http_server():
    """在后台线程启动 HTTP 服务器。"""
    server = HTTPServer(("0.0.0.0", HTTP_PORT), VolumeSurgeHandler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="HttpServer"
    )
    thread.start()
    return server, thread


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
            "currency",
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
        d = item["d"]
        results.append(
            {
                "symbol": item["s"],
                "name": d[0],
                "price": d[1],
                "type": d[2],
                "exchange": d[3],
                "vol_change_24h_pct": d[4],
                "vol_24h": d[5],
                "price_change_24h_pct": d[6] if len(d) > 6 else None,
                "currency": d[7] if len(d) > 7 else None,
            }
        )
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
            symbol      TEXT NOT NULL
        )
    """)
    conn.commit()

    # 检查是否有旧 CSV 数据需要迁移
    count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if count == 0:
        csv_file = Path(__file__).parent / "data" / "binance_volume_surge_history.csv"
        if csv_file.exists():
            import csv

            migrated = 0
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO symbols (name, entry_time, symbol) VALUES (?, ?, ?)",
                            (row["name"], row["first_seen"], row["symbol"]),
                        )
                        migrated += 1
                    except Exception:
                        pass
            conn.commit()
            # 迁移后清理旧 CSV
            csv_file.rename(csv_file.with_suffix(".csv.bak"))
            print(f"  已从 CSV 迁移 {migrated} 条记录到 SQLite")
    conn.close()


def load_from_db():
    """从数据库加载所有记录到内存缓存。返回 (seen_symbols, entry_time, exit_time)。"""
    global _seen_symbols, _symbol_entry_time, _symbol_exit_time
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

    _seen_symbols = seen
    _symbol_entry_time = entry
    _symbol_exit_time = exit_t
    print(f"  从数据库加载 {len(seen)} 个交易对记录")
    return seen


def save_to_db(new_entries: list[dict], timestamp: str) -> tuple[int, list[str]]:
    """保存新增交易对到数据库，并清理超出 MAX_HISTORY_RECORDS 的旧数据。
    返回 (新增数量, 新增name列表)。"""
    global _seen_symbols, _symbol_entry_time

    fresh = [r for r in new_entries if r["name"] not in _seen_symbols]
    if not fresh:
        return 0, []

    conn = get_db()
    fresh_names = []
    for r in fresh:
        conn.execute(
            "INSERT OR IGNORE INTO symbols (name, entry_time, symbol) VALUES (?, ?, ?)",
            (r["name"], timestamp, r["symbol"]),
        )
        _seen_symbols.add(r["name"])
        _symbol_entry_time[r["name"]] = timestamp
        fresh_names.append(r["name"])

    # 清理：只保留最新的 MAX_HISTORY_RECORDS 条
    conn.execute(f"""
        DELETE FROM symbols WHERE name IN (
            SELECT name FROM symbols ORDER BY entry_time DESC LIMIT -1 OFFSET {MAX_HISTORY_RECORDS}
        )
    """)
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
    active.sort(key=lambda r: _symbol_entry_time.get(r["name"], ""), reverse=True)
    exited.sort(key=lambda r: _symbol_exit_time.get(r["name"], ""), reverse=True)
    return active + exited


def update_exit_in_db(name: str, exit_timestamp: str):
    """更新交易对的退出时间。"""
    global _symbol_exit_time
    conn = get_db()
    conn.execute(
        "UPDATE symbols SET exit_time = ? WHERE name = ?", (exit_timestamp, name)
    )
    conn.commit()
    conn.close()
    _symbol_exit_time[name] = exit_timestamp


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
        f"  显示 {len(results)} 个 | 1h内新增 {new_count} 个 | 已退出 {len(exited_names)} 个 | 历史累计 {len(_seen_symbols)} 个"
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
        name = r["name"]
        price = f"{r['price']}"
        vol_chg = f"{r['vol_change_24h_pct']:.1f}%"
        vol = f"{r['vol_24h']:,.0f}"
        price_chg = (
            f"{r['price_change_24h_pct']:.2f}%" if r["price_change_24h_pct"] else "N/A"
        )
        if name in new_names:
            tag = "🆕 NEW"
        elif name in exited_names:
            tag = "🚫 EXIT"
        else:
            tag = ""
        # 时间列
        t = _symbol_entry_time.get(name, "")
        if name in exited_names:
            t = _symbol_exit_time.get(name, t)
        print(
            f"{i:<4} {name:<22} {price:>10} {vol_chg:>12} {vol:>18} {price_chg:>10} {t:<20} {tag:<8}"
        )
    if len(results) > MAX_DISPLAY_RESULTS:
        print(f"  ... 还有 {len(results) - MAX_DISPLAY_RESULTS} 个未显示")


def main_loop():
    """主循环：定时获取数据并保存。"""
    global \
        _seen_symbols, \
        _latest_scan, \
        _latest_new_symbols, \
        _latest_exited_symbols, \
        _latest_timestamp, \
        _previous_scan_symbols, \
        _symbol_exit_time

    # 初始化数据库 + 加载已有记录
    init_db()
    _seen_symbols = load_from_db()

    # 启动 HTTP 服务器
    http_server, _ = start_http_server()

    print("启动定时监控...")
    print(
        f"  间隔: {INTERVAL_SECONDS}s ({INTERVAL_SECONDS // 60}分{INTERVAL_SECONDS % 60}秒)"
    )
    print(f"  阈值: 24h成交量变化 > {MIN_VOL_CHANGE_PCT}%")
    print(f"  最多显示: {MAX_DISPLAY_RESULTS} 条")
    print(f"  数据库最多保留: {MAX_HISTORY_RECORDS} 条记录")
    print(f"  数据库: {DB_PATH}")
    print(f"  历史已记录: {len(_seen_symbols)} 个交易对")
    print(f"  HTTP 显示: http://localhost:{HTTP_PORT}")
    print("  按 Ctrl+C 停止\n")

    error_count = 0
    while _running:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            results = get_binance_perpetual_volume_surge()
            current_names = {r["name"] for r in results}

            # 检测本轮退出并写入DB
            newly_exited = set()
            if _previous_scan_symbols:
                newly_exited = _previous_scan_symbols - current_names
                for en in newly_exited:
                    update_exit_in_db(en, timestamp)
            _previous_scan_symbols = current_names

            # 保存新增到DB
            save_to_db(results, timestamp)

            # ---- NEW 标记：1小时内进入的 ----
            one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            new_names_set = {
                r["name"]
                for r in results
                if _symbol_entry_time.get(r["name"], "") >= one_hour_ago
            }

            # ---- EXIT 标记：DB中所有有退出时间的 ----
            exited_names_set = set(_symbol_exit_time.keys())

            # ---- 从DB获取已退出交易对，追加到显示列表 ----
            all_display = list(results)
            conn = get_db()
            exited_rows = conn.execute(
                "SELECT name, symbol, exit_time FROM symbols WHERE exit_time IS NOT NULL ORDER BY exit_time DESC"
            ).fetchall()
            conn.close()
            for row in exited_rows:
                name = row["name"]
                if name not in current_names and name not in {
                    r["name"] for r in all_display
                }:
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
                        }
                    )

            # 排序：进入的在上（按进入时间降序），退出的在下（按退出时间降序）
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
            with _scan_lock:
                _latest_scan = all_display
                _latest_timestamp = timestamp
                _latest_new_symbols = new_names_set
                _latest_exited_symbols = exited_names_set
        except Exception as e:
            error_count += 1
            print(f"  [{timestamp}] 错误: {e}")
            if error_count >= 5:
                print(f"  连续 {error_count} 次失败，5秒后重试...")

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
