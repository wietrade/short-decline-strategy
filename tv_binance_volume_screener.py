"""
TradingView 筛选器：定时获取币安永续合约中 24h 成交量涨跌 > 800% 的交易对
每分钟更新一次，历史数据追加保存到 CSV 文件。
使用 TradingView Scanner API (非官方)
"""

import csv
import json
import os
import time
import signal
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ==================== 配置 ====================
MIN_VOL_CHANGE_PCT = 800          # 24h 成交量变化最小百分比
MAX_RESULTS = 200                 # 最大结果数
INTERVAL_SECONDS = 60             # 更新间隔（秒），60 = 1分钟
CSV_FILE = Path(__file__).parent / "data" / "binance_volume_surge_history.csv"
HTTP_PORT = 3000                  # HTTP 服务器端口
MAX_DISPLAY_RESULTS = 30          # 最多显示记录数
# ==============================================

# 全局状态
_running = True
_seen_symbols: set[str] = set()   # 已写入 CSV 的交易对（内存缓存）


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
_symbol_entry_time: dict[str, str] = {}   # name -> 首次进入时间
_symbol_exit_time: dict[str, str] = {}    # name -> 退出时间


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
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

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
                price = f"{r['price']}"
                vol_chg = f"{r['vol_change_24h_pct']:.1f}%"
                vol = f"{r['vol_24h']:,.0f}"
                price_chg = f"{r['price_change_24h_pct']:.2f}%" if r.get("price_change_24h_pct") else "N/A"
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
<meta http-equiv="refresh" content="10">
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
tr.row-new {{ background: rgba(255, 107, 107, 0.08); }}
tr.row-new:hover {{ background: rgba(255, 107, 107, 0.15); }}
tr.row-new td {{ border-left: 3px solid #ff6b6b; }}
tr.row-exited {{ background: rgba(255, 165, 2, 0.08); }}
tr.row-exited:hover {{ background: rgba(255, 165, 2, 0.15); }}
tr.row-exited td {{ border-left: 3px solid #ffa502; }}
.symbol-cell {{ font-weight: 600; color: #f0f4f8; }}
.badge-new {{ display: inline-block; margin-left: 8px; padding: 2px 8px;
             background: linear-gradient(135deg, #ff6b6b, #ee5a24);
             border-radius: 10px; font-size: 11px; font-weight: 700;
             color: white; animation: pulse-glow 1.5s ease-in-out infinite; }}
.badge-exited {{ display: inline-block; margin-left: 8px; padding: 2px 8px;
                background: linear-gradient(135deg, #ffa502, #e67e22);
                border-radius: 10px; font-size: 11px; font-weight: 700;
                color: white; animation: pulse-glow-exit 1.5s ease-in-out infinite; }}
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
                <div class="value">{timestamp or '等待中...'}</div>
            </div>
            <div class="stat-item">
                <div class="label">当前结果</div>
                <div class="value">{total}</div>
            </div>
            <div class="stat-item">
                <div class="label">新增 (本轮)</div>
                <div class="value new">{new_count}</div>
            </div>
            <div class="stat-item">
                <div class="label">退出 (本轮)</div>
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
        页面每 10 秒自动刷新 &nbsp;|&nbsp; 条件: 24h成交量变化 &gt; {MIN_VOL_CHANGE_PCT}% &nbsp;|&nbsp;
        更新间隔: {INTERVAL_SECONDS}s &nbsp;|&nbsp; 显示 {displayed}/{total} 条 &nbsp;|&nbsp;
        🆕 红色行=本轮新增 &nbsp;|&nbsp; 🚫 橙色行=本轮退出
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
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="HttpServer")
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
            "name", "close", "type", "exchange",
            "24h_vol_change|5", "24h_vol|5", "24h_close_change|5", "currency",
        ],
        "filter2": {
            "operator": "and",
            "operands": [
                {"expression": {"left": "centralization", "operation": "equal", "right": "cex"}},
                {"expression": {"left": "type", "operation": "equal", "right": "swap"}},
                {"expression": {"left": "exchange", "operation": "equal", "right": "BINANCE"}},
                {"expression": {"left": "24h_vol_change|5", "operation": "greater", "right": min_vol_change_pct}},
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
        results.append({
            "symbol": item["s"],
            "name": d[0],
            "price": d[1],
            "type": d[2],
            "exchange": d[3],
            "vol_change_24h_pct": d[4],
            "vol_24h": d[5],
            "price_change_24h_pct": d[6] if len(d) > 6 else None,
            "currency": d[7] if len(d) > 7 else None,
        })
    return results


def load_seen_symbols() -> set[str]:
    """从已有 CSV 中加载已记录的交易对名称及进入时间。"""
    global _symbol_entry_time
    if not CSV_FILE.exists():
        return set()
    seen = set()
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["name"]
            seen.add(name)
            if name not in _symbol_entry_time:
                _symbol_entry_time[name] = row["first_seen"]
    return seen


def save_to_csv(new_entries: list[dict], timestamp: str) -> tuple[int, list[str]]:
    """仅保存首次出现的交易对到 CSV。返回 (新增数量, 新增name列表)。"""
    global _seen_symbols, _symbol_entry_time
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_exists = CSV_FILE.exists()

    # 过滤出未出现过的
    fresh = [r for r in new_entries if r["name"] not in _seen_symbols]
    if not fresh:
        return 0, []

    fresh_names = []
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "first_seen", "name", "symbol", "price",
                "vol_change_24h_pct", "vol_24h", "price_change_24h_pct", "currency"
            ])
        for r in fresh:
            writer.writerow([
                timestamp, r["name"], r["symbol"], r["price"],
                f"{r['vol_change_24h_pct']:.2f}",
                f"{r['vol_24h']:.2f}",
                f"{r['price_change_24h_pct']:.2f}" if r["price_change_24h_pct"] else "",
                r["currency"],
            ])
            _seen_symbols.add(r["name"])
            _symbol_entry_time[r["name"]] = timestamp
            fresh_names.append(r["name"])

    return len(fresh), fresh_names


def print_results(results: list[dict], timestamp: str, new_count: int,
                  new_names: set[str] | None = None,
                  exited_names: set[str] | None = None) -> None:
    """格式化打印结果（最多显示 MAX_DISPLAY_RESULTS 条）。"""
    new_names = new_names or set()
    exited_names = exited_names or set()
    display = results[:MAX_DISPLAY_RESULTS]
    print(f"\n{'='*85}")
    print(f"  时间: {timestamp}")
    print(f"  条件: 币安永续合约 24h成交量涨跌 > {MIN_VOL_CHANGE_PCT}%")
    print(f"  当前 {len(results)} 个 | 新增 {new_count} 个 | 退出 {len(exited_names)} 个 | 历史累计 {len(_seen_symbols)} 个")
    print(f"{'='*85}")

    if not display:
        print("  (无符合条件的交易对)")
        return

    print(f"{'排名':<4} {'交易对':<22} {'价格':>10} {'24h量变化':>12} {'24h成交量':>18} {'24h价变化':>10} {'时间':<20} {'标记':<8}")
    print("-" * 114)
    for i, r in enumerate(display, 1):
        name = r["name"]
        price = f"{r['price']}"
        vol_chg = f"{r['vol_change_24h_pct']:.1f}%"
        vol = f"{r['vol_24h']:,.0f}"
        price_chg = f"{r['price_change_24h_pct']:.2f}%" if r["price_change_24h_pct"] else "N/A"
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
        print(f"{i:<4} {name:<22} {price:>10} {vol_chg:>12} {vol:>18} {price_chg:>10} {t:<20} {tag:<8}")
    if len(results) > MAX_DISPLAY_RESULTS:
        print(f"  ... 还有 {len(results) - MAX_DISPLAY_RESULTS} 个未显示")


def main_loop():
    """主循环：定时获取数据并保存。"""
    global _seen_symbols, _latest_scan, _latest_new_symbols, _latest_exited_symbols, _latest_timestamp, _previous_scan_symbols, _symbol_exit_time

    # 启动时加载已有记录
    _seen_symbols = load_seen_symbols()

    # 启动 HTTP 服务器
    http_server, _ = start_http_server()

    print(f"启动定时监控...")
    print(f"  间隔: {INTERVAL_SECONDS}s ({INTERVAL_SECONDS//60}分{INTERVAL_SECONDS%60}秒)")
    print(f"  阈值: 24h成交量变化 > {MIN_VOL_CHANGE_PCT}%")
    print(f"  最多显示: {MAX_DISPLAY_RESULTS} 条")
    print(f"  保存: {CSV_FILE}")
    print(f"  历史已记录: {len(_seen_symbols)} 个交易对")
    print(f"  HTTP 显示: http://localhost:{HTTP_PORT}")
    print(f"  按 Ctrl+C 停止\n")

    error_count = 0
    while _running:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            results = get_binance_perpetual_volume_surge()
            new_count, fresh_names = save_to_csv(results, timestamp)
            new_names_set = set(fresh_names)

            # 检测退出交易对：上一次有但本次没有
            current_names = {r["name"] for r in results}
            exited_names_set = set()
            if _previous_scan_symbols:
                exited_names_set = _previous_scan_symbols - current_names
                for en in exited_names_set:
                    _symbol_exit_time[en] = timestamp
            _previous_scan_symbols = current_names

            print_results(results, timestamp, new_count, new_names_set, exited_names_set)
            error_count = 0

            # 更新 HTTP 全局状态
            with _scan_lock:
                _latest_scan = results
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
    print(f"\n已停止。历史数据保存在: {CSV_FILE}")


if __name__ == "__main__":
    main_loop()
