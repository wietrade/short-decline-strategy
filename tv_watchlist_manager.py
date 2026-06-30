#!/usr/bin/env python3
"""
TradingView 自选表管理器

功能：
  1. 从成交量异动扫描器 API 获取最新异动交易对
  2. 通过浏览器自动化（Playwright）自动添加到 TradingView 自选表
  3. 生成 TradingView 可导入的 .tvs 自选表文件
  4. 直接生成 TV 搜索 URL（手动添加）

用法：
  # 列出当前异动交易对
  python tv_watchlist_manager.py list

  # 用浏览器自动添加到 TV 自选表（需要安装 Playwright）
  pip install playwright && playwright install chromium
  python tv_watchlist_manager.py add --browser

  # 生成 .tvs 文件
  python tv_watchlist_manager.py export

  # 生成 TV 搜索链接
  python tv_watchlist_manager.py url

依赖：
  pip install playwright
  playwright install chromium
"""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

# ─── 配置 ───────────────────────────────────────
SCREENER_API_URL = "http://43.165.167.132:3001/api/data"
WATCHLIST_FILE = Path(__file__).parent / "data" / "tradingview_watchlist.tvs"
STORAGE_STATE_FILE = Path(__file__).parent / "data" / "tv_storage_state.json"
SERVE_PORT = 3002
# ────────────────────────────────────────────────


def fetch_symbols(api_url: str = SCREENER_API_URL) -> list[str]:
    """从成交量异动扫描器获取 TV 格式的交易对列表（仅活跃的）。"""
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "TVWatchlist/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        # 兼容 /api/data 和 /api/tvlist 两种格式
        items = raw.get("results") or raw.get("symbols") or []
        symbols = []
        for r in items:
            if isinstance(r, str):
                symbols.append(r)
            elif isinstance(r, dict):
                name = r.get("name", "")
                # 过滤：只有 vol_change_24h_pct 有值才是活跃交易对（退出的为空字符串）
                vol = r.get("vol_change_24h_pct", "")
                if name and vol != "" and vol is not None:
                    symbols.append(f"BINANCE:{name}")
        if not symbols:
            print("  ⚠ API 返回空列表")
        return symbols
    except Exception as e:
        print(f"  ❌ 连接扫描器失败: {e}")
        print("     请确保成交量异动扫描器 (tv_binance_volume_screener.py) 已在运行")
        return []


def print_symbols(symbols: list[str]):
    """打印交易对列表。"""
    if not symbols:
        print("  (无交易对)")
        return
    print(f"  共 {len(symbols)} 个交易对:")
    for i, s in enumerate(symbols, 1):
        print(f"    {i:3d}. {s}")
    print()


# ═══════════════════════════════════════════════════
#  功能 1: 导出 .tvs 文件
# ═══════════════════════════════════════════════════


def export_tvs(symbols: list[str], output: Path = WATCHLIST_FILE):
    """导出 TradingView 可导入的 .tvs 自选表文件。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(symbols), encoding="utf-8")
    print(f"  ✅ 已导出 {len(symbols)} 个交易对到: {output}")
    print("     在 TradingView 中点击: 自选 → 导入符号列表 → 选择此文件")
    return output


# ═══════════════════════════════════════════════════
#  功能 2: 生成 TV 搜索 URL
# ═══════════════════════════════════════════════════


def generate_urls(symbols: list[str]):
    """生成 TradingView 搜索链接（可手动添加到自选）。"""
    if not symbols:
        return
    single_url = f"https://www.tradingview.com/search/?q={symbols[0]}"
    print("\n  📌 搜索链接（第一个符号）:")
    print(f"     {single_url}")

    chart_url = f"https://www.tradingview.com/chart/?symbol={symbols[0]}"
    print("\n  📊 图表链接（第一个符号）:")
    print(f"     {chart_url}")

    print("\n  📋 在 TV 中手动添加自选的方法:")
    print(f"     1. 打开 {chart_url}")
    print("     2. 按 Ctrl+D (或点击⭐) 加入自选")
    print(f"     3. 重复添加其余 {len(symbols) - 1} 个交易对")
    print()


# ═══════════════════════════════════════════════════
#  功能 3: Playwright 浏览器自动添加（推荐）
# ═══════════════════════════════════════════════════


def _get_storage_path() -> str:
    STORAGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    return str(STORAGE_STATE_FILE)


def _save_storage(context):
    try:
        context.storage_state(path=_get_storage_path())
    except Exception:
        pass


def add_via_browser(symbols: list[str], headless: bool = False):
    """使用 Playwright 控制浏览器，自动将交易对添加到 TV 自选表。

    前提：
      1. pip install playwright && playwright install chromium
      2. 已在浏览器中登录 TradingView（脚本会自动加载之前保存的登录态）
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ❌ 未安装 Playwright。请运行:")
        print("     pip install playwright")
        print("     playwright install chromium")
        return

    if not symbols:
        print("  ⚠ 没有交易对可添加")
        return

    print(f"  🚀 启动浏览器，准备添加 {len(symbols)} 个交易对到 TV 自选表...")
    if not headless:
        print("     浏览器窗口将自动打开，请确保已登录 TradingView")
    print("     按 Ctrl+C 随时停止\n")

    added_count = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        storage_path = _get_storage_path()
        if os.path.exists(storage_path):
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                storage_state=storage_path,
            )
            print("  📦 已加载之前保存的登录状态")
        else:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )

        page = context.new_page()

        try:
            for i, symbol in enumerate(symbols, 1):
                print(f"\n  [{i}/{len(symbols)}] {symbol} ...", end=" ", flush=True)

                try:
                    chart_url = f"https://www.tradingview.com/chart/?symbol={symbol}"
                    page.goto(chart_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2.5)

                    clicked = False
                    selectors = [
                        'button[data-name="watchlist-star"]',
                        'button[aria-label*="watchlist" i]',
                        'button[aria-label*="Watchlist"]',
                        '[data-name="watchlist-star"]',
                        'button[class*="watchlist"]',
                        'button[class*="star"]',
                    ]

                    for sel in selectors:
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=800):
                                btn.click(timeout=1500)
                                time.sleep(0.8)
                                clicked = True
                                break
                        except Exception:
                            continue

                    if clicked:
                        print("✅")
                        added_count += 1
                    else:
                        try:
                            page.keyboard.press("Control+d")
                            time.sleep(1)
                            print("⌨️ (Ctrl+D)")
                            added_count += 1
                        except Exception:
                            print("⚠️ 未能自动添加")
                            search_url = (
                                f"https://www.tradingview.com/search/?q={symbol}"
                            )
                            page.goto(
                                search_url, wait_until="domcontentloaded", timeout=10000
                            )
                            time.sleep(1)
                            print("     请在打开的页面中手动添加")

                except Exception as e:
                    print(f"❌ {e}")

        except KeyboardInterrupt:
            print("\n     ⏹ 用户中断")
        finally:
            _save_storage(context)
            print("\n  💾 已保存浏览器登录状态")
            browser.close()

    print(f"\n  📊 完成: 成功添加 {added_count}/{len(symbols)} 个交易对")


# ═══════════════════════════════════════════════════
#  功能 4: Cookie 管理
# ═══════════════════════════════════════════════════


def save_cookie_input(auto: bool = False):
    """保存 TradingView cookie。auto=True 时用 Playwright 自动抓取。"""
    if auto:
        return _capture_cookies_auto()

    print("\n📌 请从浏览器获取完整 TradingView cookie（包括 HttpOnly 的）:")
    print("   1. 在浏览器中打开 https://www.tradingview.com 并登录")
    print("   2. 按 F12 打开开发者工具")
    print("   3. 切换到 Application（应用）→ Cookies → https://www.tradingview.com")
    print("   4. 右键任意 cookie → Copy All（复制全部）")
    print("   5. 在下方粘贴（鼠标右键粘贴）")
    print("   输入 q 取消\n")

    raw = input("  Cookie > ").strip()
    if raw.lower() in ("q", "quit", "exit", ""):
        print("  ❌ 已取消")
        return False

    cookie_pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookie_pairs.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".tradingview.com",
                }
            )

    if not cookie_pairs:
        print("  ❌ 未解析到有效 cookie")
        return False

    state = {"cookies": cookie_pairs, "origins": []}
    STORAGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  ✅ 已保存 {len(cookie_pairs)} 个 cookie 到 {STORAGE_STATE_FILE}")
    return True


def _capture_cookies_auto():
    """用 Playwright 打开浏览器，用户登录后自动抓取 cookie。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ❌ 未安装 Playwright。请运行:")
        print("     pip install playwright")
        print("     playwright install chromium")
        print("  或使用手动模式: python tv_watchlist_manager.py cookie")
        return False

    print("\n  🚀 启动浏览器...")
    print("     请在浏览器中登录 https://www.tradingview.com")
    print("     登录完成后回到此终端按回车继续\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto("https://www.tradingview.com", wait_until="domcontentloaded")

        input("  登录完成后按回车 > ")

        # 获取所有 cookie（包括 HttpOnly）
        cookies = []
        try:
            cdp = context.new_cdp_session(page)
            result = cdp.send("Network.getAllCookies")
            cookies = [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path", "/"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", "Lax"),
                }
                for c in result.get("cookies", [])
            ]
        except Exception:
            try:
                docCookies = page.evaluate("document.cookie")
                for part in docCookies.split(";"):
                    if "=" in part:
                        n, v = part.split("=", 1)
                        cookies.append(
                            {
                                "name": n.strip(),
                                "value": v.strip(),
                                "domain": ".tradingview.com",
                            }
                        )
            except Exception:
                pass

        browser.close()

    if not cookies:
        print("  ❌ 未获取到 cookie")
        return False

    state = {"cookies": cookies, "origins": []}
    STORAGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  ✅ 已自动抓取 {len(cookies)} 个 cookie 到 {STORAGE_STATE_FILE}")
    return True


# ═══════════════════════════════════════════════════
#  功能 5: 通过 TV 内部 API 添加自选（核心功能）
# ═══════════════════════════════════════════════════


def _load_cookies() -> tuple[str, str]:
    """加载保存的 TV cookie，返回 (cookie_str, csrf_token)。"""
    if not STORAGE_STATE_FILE.exists():
        return "", ""
    try:
        state = json.loads(STORAGE_STATE_FILE.read_text())
        cookies = state.get("cookies", [])
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        csrf_token = ""
        for c in cookies:
            if c.get("name") in ("csrf_token", "X-CSRF-Token", "token", "csrftoken"):
                csrf_token = c["value"]
                break
        return cookie_str, csrf_token
    except Exception:
        return "", ""


def _tv_api_call(
    method: str, path: str, body: dict | list | None = None
) -> tuple[int, any]:
    """调用 TradingView 内部 API。"""
    cookie_str, csrf_token = _load_cookies()
    if not cookie_str:
        return -1, None

    # 修正 API 路径格式
    if path.startswith("/lists") and not path.startswith("/api/v1/symbols_list"):
        path = path.replace("/lists", "/api/v1/symbols_list", 1)

    url = f"https://www.tradingview.com{path}"
    headers = {
        "Content-Type": "application/json",
        "Cookie": cookie_str,
        "X-CSRF-Token": csrf_token,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            if resp_body:
                return resp.status, json.loads(resp_body)
            return resp.status, None
    except urllib.request.HTTPError as e:
        err_body = e.read().decode("utf-8")[:500] if e.fp else ""
        try:
            return e.code, json.loads(err_body) if err_body else None
        except Exception:
            return e.code, err_body
    except Exception as e:
        return -2, str(e)


def ensure_list(list_name: str = "new") -> str | None:
    """查找或创建 TV 自选表，返回 list_id。"""
    # 先获取所有列表
    status, lists = _tv_api_call("GET", "/api/v1/symbols_list/all/?source=web")
    if status > 0 and isinstance(lists, list):
        for lst in lists:
            if lst.get("name") == list_name:
                print(f"  📋 已存在自选表「{list_name}」")
                return lst.get("id")
    return None


def add_via_api(symbols: list[str], list_name: str = "new"):
    """通过 TradingView 内部 API 自动添加自选（核心功能）。

    使用 POST .../replace/?unsafe=true 原地替换列表内容（ID 不变）。
    """
    # 检查 cookie
    cookie_str, _ = _load_cookies()
    if not cookie_str:
        print("  🔑 需要 TradingView 登录 cookie 才能自动添加\n")
        if not save_cookie_input():
            print("  ❌ 无法继续，请先设置 cookie")
            return

    print(f"\n  📡 通过 TV API 更新自选表「{list_name}」({len(symbols)} 个交易对)\n")

    # 查找列表 ID
    print("  📋 查找自选表...")
    list_id = None
    status, lists = _tv_api_call("GET", "/api/v1/symbols_list/all/?source=web")
    if status > 0 and isinstance(lists, list):
        for lst in lists:
            if lst.get("name") == list_name:
                list_id = lst["id"]
                print(f"     ✅ 找到自选表「{list_name}」(ID={list_id})")
                break

    if not list_id:
        # 列表不存在，创建新列表
        print("     列表不存在，创建新列表...")
        status, created = _tv_api_call(
            "POST",
            "/api/v1/symbols_list/custom/",
            {"name": list_name, "symbols": symbols, "active": True},
        )
        if status in (200, 201):
            list_id = created.get("id") if created else None
            print(f"     ✅ 创建成功 (ID={list_id})")
        else:
            print(f"     ❌ 创建失败 (HTTP {status})")
            return
    else:
        # 原地替换内容（ID 不变，TV 警报不会失效）
        print("     原地替换列表内容...")
        status, _ = _tv_api_call(
            "POST",
            f"/api/v1/symbols_list/custom/{list_id}/replace/?unsafe=true",
            symbols,  # 直接传数组，不包装成对象
        )
        if status == 200:
            print("     ✅ 替换成功")
        else:
            print(f"     ⚠ 替换返回 (HTTP {status})，尝试重建...")
            _tv_api_call("DELETE", f"/api/v1/symbols_list/custom/{list_id}/")
            status, created = _tv_api_call(
                "POST",
                "/api/v1/symbols_list/custom/",
                {"name": list_name, "symbols": symbols, "active": True},
            )
            if status in (200, 201):
                list_id = created.get("id") if created else None
                print(f"     ✅ 重建成功 (ID={list_id})")

    print(
        f"\n  📊 结果: {len(symbols)} 个交易对已同步到自选表「{list_name}」(ID={list_id})"
    )
    print(f"     在 TradingView 中查看: 自选 → {list_name}")


# ═══════════════════════════════════════════════════
#  功能 5: 启动本地网页服务（浏览器端调用 TV API）
# ═══════════════════════════════════════════════════


def serve_webpage(symbols: list[str], port: int = SERVE_PORT):
    """启动本地 HTTP 服务，提供一个可在浏览器中一键添加到 TV 自选的页面。

    原理：页面 JS 在用户浏览器中直接调用 TV 内部 API（利用已登录的 cookie）。
    无需安装 Playwright，打开页面点一下按钮即可。
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    html = f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📌 一键添加到 TV 自选表</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0b0e17; color: #e0e6f0; padding: 30px; text-align: center; }}
.container {{ max-width: 800px; margin: 0 auto; }}
h1 {{ color: #f0b90b; font-size: 24px; }}
.card {{ background: #1a2332; border-radius: 12px; padding: 24px; margin: 20px 0;
         border: 1px solid #2a3a50; text-align: left; }}
.sym-list {{ max-height: 300px; overflow-y: auto; background: #111b26;
             border-radius: 8px; padding: 10px; margin: 12px 0;
             font-family: monospace; font-size: 13px; line-height: 1.6; }}
.sym-item {{ color: #7ec8e3; }}
.btn {{ background: #2962ff; color: #fff; border: none; padding: 14px 36px;
        border-radius: 8px; font-size: 16px; cursor: pointer; font-weight: 600;
        margin: 8px; }}
.btn:hover {{ background: #1a4fd0; }}
.btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.btn-success {{ background: #2ecc71; }}
.btn-warning {{ background: #ffa502; }}
.status {{ margin: 16px 0; padding: 12px; border-radius: 8px; font-size: 14px; }}
.status-ok {{ background: rgba(46,204,113,0.15); color: #2ecc71; }}
.status-err {{ background: rgba(255,107,107,0.15); color: #ff6b6b; }}
.status-info {{ background: rgba(41,98,255,0.15); color: #5b8dff; }}
.footer {{ color: #4a5a6a; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
    <h1>📌 一键添加到 TradingView 自选表</h1>
    <p style="color:#8a9aaa;">将从成交量扫描器获取的 {len(symbols)} 个异动交易对添加到 TV 自选</p>

    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>📊 异动交易对列表</strong>
            <span style="color:#7a8da0; font-size:13px;">共 {len(symbols)} 个</span>
        </div>
        <div class="sym-list" id="symList"></div>
    </div>

    <div class="status status-info" id="status">✅ 请确认已在浏览器中登录 TradingView，然后点击下方按钮</div>

    <button class="btn" id="addBtn" onclick="addAll()">📌 一键全部添加到 TV 自选</button>
    <button class="btn btn-warning" onclick="copyClipboard()">📋 复制到剪贴板</button>

    <div class="footer">
        脚本自动调用 TradingView 内部 API · 利用您当前浏览器的登录状态<br>
        如果 TV 自选表未更新，请刷新页面重试
    </div>
</div>

<script>
const SYMBOLS = {json.dumps(symbols, ensure_ascii=False)};
const LIST_NAME = 'new';
let LIST_ID = null;

// 渲染列表
document.getElementById('symList').innerHTML = SYMBOLS.map(s =>
    '<div class="sym-item">' + s + '</div>'
).join('');

let added = 0;
let failed = 0;

// 通过本地后端代理调用 TV API（解决 CORS + HttpOnly cookie 问题）
const API_PROXY = '/api/tv-proxy';

async function ensureList() {{
    if (LIST_ID) return LIST_ID;
    // 1) 获取已有列表，查找名为 LIST_NAME 的
    try {{
        const r = await fetch(API_PROXY + '/lists/');
        if (r.ok) {{
            const lists = await r.json();
            const found = lists.find(l => l.name === LIST_NAME || l.id === LIST_NAME);
            if (found) {{
                LIST_ID = found.id;
                return LIST_ID;
            }}
        }}
    }} catch(e) {{ /* 忽略 */ }}

    // 2) 创建新列表
    try {{
        const r = await fetch(API_PROXY + '/lists/', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ name: LIST_NAME, symbols: [] }})
        }});
        if (r.ok) {{
            const created = await r.json();
            LIST_ID = created.id;
            return LIST_ID;
        }}
    }} catch(e) {{ /* 忽略 */ }}

    // 3) 兜底：直接用 default
    LIST_ID = 'default';
    return LIST_ID;
}}

async function addOne(symbol) {{
    try {{
        const r = await fetch(API_PROXY + '/lists/' + LIST_ID + '/symbols', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ symbols: [symbol] }})
        }});
        if (r.ok || r.status === 204) return true;
        if (r.status === 409) return true;
        return false;
    }} catch(e) {{
        return false;
    }}
}}

async function addAll() {{
    const btn = document.getElementById('addBtn');
    const status = document.getElementById('status');
    btn.disabled = true;
    btn.textContent = '⏳ 创建/查找自选表...';

    // 先创建或找到 "new" 自选表
    const listId = await ensureList();
    status.textContent = '📋 自选表: \\'' + LIST_NAME + '\\' (ID: ' + listId + ')';
    btn.textContent = '⏳ 添加中...';
    added = 0; failed = 0;

    for (let i = 0; i < SYMBOLS.length; i++) {{
        const ok = await addOne(SYMBOLS[i]);
        if (ok) added++; else failed++;
        btn.textContent = '⏳ [' + (i+1) + '/' + SYMBOLS.length + '] 成功 ' + added + ' 失败 ' + failed;
    }}

    if (failed === 0) {{
        status.className = 'status status-ok';
        status.textContent = '✅ 全部 ' + added + ' 个已成功添加到自选表「' + LIST_NAME + '」！';
        btn.className = 'btn btn-success';
        btn.textContent = '✅ 完成 (' + added + ' 个)';
    }} else {{
        status.className = 'status status-err';
        status.textContent = '⚠️ 成功 ' + added + ' 个，失败 ' + failed + ' 个。可能原因：未登录 TV / 网络问题';
        btn.className = 'btn';
        btn.textContent = '📌 重试失败项';
        btn.disabled = false;
    }}
}}

function copyClipboard() {{
    const text = SYMBOLS.join('\\n');
    navigator.clipboard.writeText(text).then(() => {{
        const s = document.getElementById('status');
        s.className = 'status status-ok';
        s.textContent = '✅ 已复制 ' + SYMBOLS.length + ' 个符号到剪贴板';
    }}).catch(() => {{
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
        alert('已复制到剪贴板');
    }});
}}
</script>
</body>
</html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/tvlist":
                data = json.dumps({"symbols": symbols, "total": len(symbols)})
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data.encode("utf-8"))
                return
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            """代理转发到 TV API（解决 CORS + 需要 HttpOnly cookie 的问题）"""
            import urllib.parse

            # /api/tv-proxy/lists/... -> https://www.tradingview.com/lists/...
            if self.path.startswith("/api/tv-proxy/"):
                target_path = self.path[len("/api/tv-proxy") :]
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else None

                cookie_str, csrf_token = _load_cookies()
                if not cookie_str:
                    self.send_json(
                        401,
                        {
                            "error": "No cookies. Run: python tv_watchlist_manager.py cookie"
                        },
                    )
                    return

                try:
                    tv_url = f"https://www.tradingview.com{target_path}"
                    req = urllib.request.Request(
                        tv_url,
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "Cookie": cookie_str,
                            "X-CSRF-Token": csrf_token,
                            "Origin": "https://www.tradingview.com",
                            "Referer": "https://www.tradingview.com/",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        resp_body = resp.read()
                        self.send_response(resp.status)
                        self.send_header(
                            "Content-Type", "application/json; charset=utf-8"
                        )
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(resp_body)
                except urllib.request.HTTPError as e:
                    err = e.read() if e.fp else b""
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(err)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(err or b"{}")
                except Exception as e:
                    self.send_json(500, {"error": str(e)})
                return
            self.send_json(404, {"error": "Not found"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def send_json(self, status, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    print("\n  🌐 网页服务已启动!")
    print(f"     打开链接: http://localhost:{port}")
    print("     🔄 自动检测到已保存的 cookie，通过后端代理调用 TV API")
    print("     然后点击「一键全部添加到 TV 自选」按钮")
    print("     按 Ctrl+C 停止服务\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n     ⏹ 服务已停止")
        server.shutdown()


# ═══════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="TradingView 自选表管理器 - 自动将异动交易对添加到 TV 自选",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 查看异动列表
  python tv_watchlist_manager.py list

  # ★ 自动上传异动交易对到 TV「new」自选表（推荐）
  python tv_watchlist_manager.py add --api

  # 首次使用需要先设置 TV cookie
  python tv_watchlist_manager.py cookie

  # 浏览器自动添加（需要 Playwright）
  python tv_watchlist_manager.py add --browser

  # 导出 .tvs 文件手动导入
  python tv_watchlist_manager.py export

  # 指定扫描器 API 地址
  python tv_watchlist_manager.py list --api http://43.165.167.132:3001/api/data

工作流程:
  1. 首次运行: python tv_watchlist_manager.py cookie    # 粘贴 TV cookie
  2. 每日运行: python tv_watchlist_manager.py add --api  # 自动上传到 new 自选表
        """,
    )

    parser.add_argument(
        "--api",
        dest="api_url",
        default=SCREENER_API_URL,
        help=f"成交量扫描器 API 地址 (默认: {SCREENER_API_URL})",
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    cookie_p = sub.add_parser(
        "cookie", help="设置/更新 TradingView 登录 cookie（首次必须运行）"
    )
    cookie_p.add_argument(
        "--auto",
        action="store_true",
        help="自动模式：用 Playwright 打开浏览器抓取 cookie",
    )

    sub.add_parser("list", help="列出当前异动交易对")

    export_p = sub.add_parser("export", help="导出 .tvs 文件")
    export_p.add_argument(
        "-o", "--output", default=str(WATCHLIST_FILE), help="输出文件路径"
    )

    sub.add_parser("url", help="生成 TV 搜索链接")

    serve_p = sub.add_parser(
        "serve", help="启动网页服务，在浏览器中点按钮一键添加到 TV 自选（无需安装依赖）"
    )
    serve_p.add_argument(
        "-p", "--port", type=int, default=SERVE_PORT, help=f"端口 (默认: {SERVE_PORT})"
    )

    add_p = sub.add_parser("add", help="上传异动交易对到 TV 自选表")
    add_group = add_p.add_mutually_exclusive_group()
    add_group.add_argument(
        "--api",
        dest="use_api",
        action="store_true",
        default=True,
        help="通过 TV 内部 API 添加（默认模式，推荐）",
    )
    add_group.add_argument(
        "--browser",
        action="store_true",
        help="使用 Playwright 浏览器自动化添加",
    )
    add_p.add_argument(
        "--headless", action="store_true", help="浏览器模式使用无头模式（不显示窗口）"
    )
    add_p.add_argument(
        "--list", dest="list_name", default="new", help="TV 自选表名称 (默认: new)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "cookie":
        save_cookie_input(auto=getattr(args, "auto", False))
        return

    symbols = fetch_symbols(args.api_url)

    if args.command == "list":
        print("\n📊 成交量异动交易对列表:")
        print(f"  {'=' * 50}")
        print_symbols(symbols)

    elif args.command == "export":
        print("\n📥 导出 .tvs 自选表文件:")
        print(f"  {'=' * 50}")
        export_tvs(symbols, Path(args.output))

    elif args.command == "url":
        print("\n🔗 生成 TradingView 链接:")
        print(f"  {'=' * 50}")
        generate_urls(symbols)

    elif args.command == "serve":
        serve_webpage(symbols, port=args.port)

    elif args.command == "add":
        print(f"\n📌 上传到 TV 自选表「{args.list_name}」:")
        print(f"  {'=' * 50}")

        if args.browser:
            add_via_browser(symbols, headless=args.headless)
        else:
            add_via_api(symbols, list_name=args.list_name)
            generate_urls(symbols)
            print("  💡 提示: 使用 --browser 可自动添加到 TV 自选表")


if __name__ == "__main__":
    main()
