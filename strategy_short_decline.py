"""
做空反弹策略（纯空头，无止损，无翻转）

空头逻辑:
  入场: 扫描器异动交易对，24h涨幅>4%且从最高点回调>2%时开空
  入场限制: 24h涨幅 ≤ 4% 或距最高点回调 ≤ 2% 不开空，平仓后永久锁定
  资金费率: < -0.07% 时禁止开空（DCA加仓不受限制）
  止损: 无（永不爆仓）
  止盈: 移动止盈（4%激活 / 2%回撤）
  超时: 18小时回到成本价平仓
  加仓: DCA金字塔
    - DCA #1: 首仓 +10% 涨幅（亏损中加仓）
    - DCA #2+: 基于上次加仓价，先涨 ≥X% 再回调 ≥Y% 补仓
      X/Y 由 dca_callback_rise / dca_callback_pullback 配置
"""

import logging
import threading
import time
from datetime import datetime
from math import isfinite

import pandas as pd
import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class ShortDeclineStrategy(IStrategy):
    """做空反弹策略 - 针对长期下跌的交易对做空并金字塔加仓"""

    INTERFACE_VERSION = 3

    timeframe = "15m"
    startup_candle_count = 120
    process_only_new_candles = True

    can_short = True
    trading_mode = "futures"
    margin_mode = "cross"

    minimal_roi = {"0": 100}
    stoploss = -1.0  # 无止损，永不爆仓
    use_custom_stoploss = True
    trailing_stop = False

    # custom_exit 只在 use_exit_signal=True 时才会被调用；
    # populate_exit_trend 不设任何信号，所有出场逻辑都在 custom_exit 中
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ── DCA 加仓 ──
    position_adjustment_enable = True
    max_entry_position_adjustment = 5

    # DCA#2+ 回调加仓参数（可按 DCA 次数逐次配置）
    # 第 N 次回调加仓 = 上次加仓价先涨 dca_callback_rise[N] 再回调 dca_callback_pullback[N]
    # 列表用完后沿用最后一个值
    dca_callback_rise = [0.50, 0.40]  # DCA#2 涨50%, DCA#3+ 涨40%
    dca_callback_pullback = [0.20]  # 统一回调20%

    # ── 订单类型 ──
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ── 移动止盈参数（相对加权均价）──
    trail_activate = 0.04  # 盈利方向偏离均价 4% 激活移动止盈
    trail_pullback = 0.02  # 从极值点回撤 2% 平仓

    # ── 持仓超时平仓 ──
    max_hold_hours = 18  # 持仓超过此时间后，价格回到成本价即平仓

    # ── ADX 排序（仅用于入场优先级）──
    adx_period = 14
    _adx_cache: dict[str, float] = {}

    # ── 扫描器数据 ──
    scanner_data_url = "http://127.0.0.1:3001/api/list"
    _perf_1w_cache: dict[str, float] = {}
    _perf_1m_cache: dict[str, float] = {}
    _perf_3m_cache: dict[str, float] = {}
    _price_change_24h_cache: dict[str, float] = {}
    _price_change_4h_cache: dict[str, float] = {}
    _eligible_pairs: set[str] = set()
    _first_entry_price: dict[str, float] = {}
    _first_entry_qty: dict[str, float] = {}  # 首次开仓的币数量（用于DCA保持相同数量）
    _lowest_price: dict[str, float] = {}  # 持仓期间最低价（用于移动止盈）
    _high_24h_cache: dict[str, float] = {}  # 最近24小时最高价（用于入场）
    _low_24h_cache: dict[str, float] = {}  # 最近24小时最低价（用于入场）
    _range_24h_cache: dict[str, float] = {}  # 24h波幅(高-低)/低（用于调整周月季涨幅）
    _dca_pullback_high: dict[str, float] = {}  # DCA回调模式下的阶段最高价（DCA#2+启用）
    _last_dca_price: dict[
        str, float
    ] = {}  # 最近一次加仓的成交价（DCA#2+用于计算4%涨幅基准）
    _exited_pairs: set[str] = set()  # 已平仓交易对，永久锁定不再开仓

    # ── 资金费率 ──
    _funding_rate_cache: dict[
        str, float
    ] = {}  # pair -> 当前资金费率（如 -0.005 = -0.5%）
    _funding_watch_pairs: set[str] = set()  # 因资金费率过负被暂缓的交易对
    funding_rate_threshold = (
        -0.0007
    )  # 资金费率阈值 -0.07%，低于此值禁止开空（DCA加仓不受限制）

    _api_lock = threading.Lock()
    _last_api_fetch: float = 0
    _api_update_interval = 60
    _data_stale_timeout = 300  # 数据过期阈值（秒），超时后暂停开仓

    # ── 指标 ──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ADX 仅用于入场优先级排序，不做过滤
        high, low, close = dataframe["high"], dataframe["low"], dataframe["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        s_tr = tr.ewm(span=self.adx_period, adjust=False).mean()
        up = (high - high.shift(1)).clip(lower=0)
        down = (low.shift(1) - low).clip(lower=0)
        plus_dm = up.where(up > down, 0)
        minus_dm = down.where(~(up > down) & (down > 0), 0)
        s_plus = plus_dm.ewm(span=self.adx_period, adjust=False).mean()
        s_minus = minus_dm.ewm(span=self.adx_period, adjust=False).mean()
        plus_di = 100 * s_plus / s_tr
        minus_di = 100 * s_minus / s_tr
        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).fillna(0)
        adx = dx.ewm(span=self.adx_period, adjust=False).mean()

        pair = self._norm_pair(metadata.get("pair", ""))
        with self._api_lock:
            self._adx_cache[pair] = float(adx.iloc[-1])

        # 24h 最高点和最低点（15m × 96 = 24h），用于入场条件和涨幅调整
        high_24h = dataframe["high"].rolling(window=96, min_periods=1).max()
        low_24h = dataframe["low"].rolling(window=96, min_periods=1).min()
        with self._api_lock:
            self._high_24h_cache[pair] = float(high_24h.iloc[-1])
            self._low_24h_cache[pair] = float(low_24h.iloc[-1])
            if low_24h.iloc[-1] > 0:
                self._range_24h_cache[pair] = float(
                    (high_24h.iloc[-1] - low_24h.iloc[-1]) / low_24h.iloc[-1] * 100
                )

        self._fetch_perf_data()
        return dataframe

    # ── 获取扫描器数据 ──

    @staticmethod
    def _safe_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            result = float(value)
            return result if isfinite(result) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_entry_order(order) -> bool:
        side = getattr(order, "ft_order_side", None) or getattr(order, "side", None)
        status = getattr(order, "status", None)
        amount = getattr(order, "filled", None) or getattr(order, "amount", None)
        is_entry = getattr(order, "ft_is_entry", False)
        return (
            (is_entry or side in ("short", "sell", "entry"))
            and status
            in (
                "closed",
                "filled",
            )
            and bool(amount)
        )

    def _entry_count(self, trade: Trade) -> int:
        count = getattr(trade, "nr_of_successful_entries", None)
        if isinstance(count, int) and count > 0:
            return count
        orders = [o for o in getattr(trade, "orders", []) if self._is_entry_order(o)]
        return max(1, len(orders))

    def _get_first_entry_state(self, trade: Trade) -> tuple[float | None, float | None]:
        np = self._norm_pair(trade.pair)
        orders = [o for o in getattr(trade, "orders", []) if self._is_entry_order(o)]
        if orders:
            orders.sort(
                key=lambda o: (
                    getattr(o, "order_filled_date", None)
                    or getattr(o, "order_date", None)
                    or datetime.max.replace(tzinfo=None)
                ).replace(tzinfo=None)
            )
            first_order = orders[0]
            price = (
                getattr(first_order, "safe_price", None)
                or getattr(first_order, "average", None)
                or getattr(first_order, "price", None)
            )
            qty = getattr(first_order, "filled", None) or getattr(
                first_order, "amount", None
            )
            first_entry = self._safe_float(price)
            first_qty = self._safe_float(qty)
        else:
            with self._api_lock:
                first_entry = self._first_entry_price.get(np)
                first_qty = self._first_entry_qty.get(np)
            if first_entry is not None and first_qty is not None:
                return first_entry, first_qty

            first_entry = self._safe_float(getattr(trade, "open_rate", None))
            amount = self._safe_float(getattr(trade, "amount", None))
            entry_count = self._entry_count(trade)
            first_qty = (amount / entry_count) if amount else None

        if first_entry is not None and first_qty is not None:
            with self._api_lock:
                self._first_entry_price[np] = first_entry
                self._first_entry_qty[np] = first_qty
        return first_entry, first_qty

    @staticmethod
    def _is_eligible(
        perf_1w: float, perf_1m: float, perf_3m: float, chg_4h: float, chg_24h: float
    ) -> bool:
        """判断交易对是否适合做空。

        条件: 4h涨≥8% ∧ 24h涨>0 ∧ (1w/1m/3m - 24h波幅) ≤ 0
        即: 最近4h突然拉盘，但减去24h波动后之前一直在跌/横盘
        """
        # 4h 涨幅 ≥ 8%（确认短期拉盘强度）
        if chg_4h < 8:
            return False
        # 24h 涨幅 > 0（确认上涨方向）
        if chg_24h <= 0:
            return False
        # 周月季涨幅已在上游减去24h波幅，直接判断 ≤ 0
        return all(v <= 0 for v in (perf_1w, perf_1m, perf_3m))

    def _dca_trigger_rise(self, n: int) -> float:
        """第 n 次加仓需要的累计涨幅（相对首仓价）。

        仅 DCA#1 使用涨幅触发（10%），DCA#2+ 全部走回调模式。
        """
        if n == 1:
            return 0.10
        return 999.0  # DCA#2+ 走回调模式

    def _fetch_perf_data(self) -> None:
        now = time.time()
        if now - self._last_api_fetch < self._api_update_interval:
            return
        try:
            resp = requests.get(self.scanner_data_url, timeout=10)
            all_scanner_pairs: set[str] = set()
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", []) if isinstance(data, dict) else data
                with self._api_lock:
                    self._perf_1w_cache.clear()
                    self._perf_1m_cache.clear()
                    self._perf_3m_cache.clear()
                    self._price_change_24h_cache.clear()
                    self._price_change_4h_cache.clear()
                    self._eligible_pairs.clear()
                    for r in results:
                        name = r.get("name") or r.get("pair", "")
                        if not name:
                            continue
                        pair_key = (
                            name.split(":")[0]
                            if "/" in name
                            else name.replace(".P", "")
                        )
                        # 确保是 xxx/USDT 格式
                        if "/" not in pair_key:
                            for quote in ("USDT", "USDC", "BUSD"):
                                if pair_key.endswith(quote) and len(pair_key) > len(
                                    quote
                                ):
                                    pair_key = f"{pair_key[: -len(quote)]}/{quote}"
                                    break
                            else:
                                pair_key = f"{pair_key}/USDT"
                        all_scanner_pairs.add(pair_key)
                        perf_1w = self._safe_float(r.get("perf_1w"))
                        perf_1m = self._safe_float(r.get("perf_1m"))
                        perf_3m = self._safe_float(r.get("perf_3m"))
                        chg_24h = self._safe_float(r.get("price_change_24h_pct"))
                        chg_4h = self._safe_float(r.get("price_change_4h_pct"))
                        if None in (perf_1w, perf_1m, perf_3m, chg_24h, chg_4h):
                            continue
                        # 减去24h波幅: 调整后 = 原始 - (24h最高-最低)/最低
                        range_24h = self._range_24h_cache.get(pair_key, 0)
                        adj_1w = perf_1w - range_24h
                        adj_1m = perf_1m - range_24h
                        adj_3m = perf_3m - range_24h
                        self._perf_1w_cache[pair_key] = perf_1w
                        self._perf_1m_cache[pair_key] = perf_1m
                        self._perf_3m_cache[pair_key] = perf_3m
                        self._price_change_24h_cache[pair_key] = chg_24h
                        self._price_change_4h_cache[pair_key] = chg_4h
                        if self._is_eligible(adj_1w, adj_1m, adj_3m, chg_4h, chg_24h):
                            self._eligible_pairs.add(pair_key)
                    self._last_api_fetch = now

            # ── 资金费率过滤（使用可配置阈值） ──
            fr_threshold = self.funding_rate_threshold
            self._fetch_funding_rates()
            funding_blocked: set[str] = set()
            for pair in list(self._eligible_pairs):
                fr = self._funding_rate_cache.get(pair)
                if fr is not None and fr < fr_threshold:
                    funding_blocked.add(pair)
                    self._funding_watch_pairs.add(pair)
                    logger.info(
                        "[ShortDecline] %s 资金费率 %.6f < %.4f，暂缓开空（加入监控）",
                        pair,
                        fr,
                        fr_threshold,
                    )
            self._eligible_pairs -= funding_blocked

            # ── 监控列表中恢复的交易对（_is_eligible 包含所有条件检查） ──
            recovered: set[str] = set()
            for pair in list(self._funding_watch_pairs):
                fr = self._funding_rate_cache.get(pair)
                if fr is not None and fr >= fr_threshold:
                    recovered.add(pair)
                    perf_1w = self._perf_1w_cache.get(pair)
                    perf_1m = self._perf_1m_cache.get(pair)
                    perf_3m = self._perf_3m_cache.get(pair)
                    chg_24h = self._price_change_24h_cache.get(pair)
                    chg_4h = self._price_change_4h_cache.get(pair)
                    range_24h = self._range_24h_cache.get(pair, 0)
                    if None not in (
                        perf_1w,
                        perf_1m,
                        perf_3m,
                        chg_24h,
                        chg_4h,
                    ) and self._is_eligible(
                        perf_1w - range_24h,
                        perf_1m - range_24h,
                        perf_3m - range_24h,
                        chg_4h,
                        chg_24h,
                    ):
                        self._eligible_pairs.add(pair)
                        logger.info(
                            "[ShortDecline] %s 资金费率已恢复 %.6f，重新加入候选",
                            pair,
                            fr,
                        )
                    else:
                        logger.info(
                            "[ShortDecline] %s 资金费率已恢复 %.6f，但其他条件不再满足，放弃监控",
                            pair,
                            fr,
                        )
            self._funding_watch_pairs -= recovered

            # ── 清理监控列表中已不在扫描结果的僵尸交易对 ──
            stale_watch = {
                p
                for p in self._funding_watch_pairs
                if p not in self._perf_1w_cache and p not in all_scanner_pairs
            }
            if stale_watch:
                logger.info("[ShortDecline] 清理僵尸监控 %s", stale_watch)
                self._funding_watch_pairs -= stale_watch

        except Exception as e:
            logger.error("[ShortDecline] 获取扫描器数据失败: %s", e)

    def _is_data_stale(self) -> bool:
        """扫描器数据是否过期（超过 _data_stale_timeout 秒未成功更新）。"""
        with self._api_lock:
            last = self._last_api_fetch
        if last == 0:
            return True  # 从未成功拉取过
        return (time.time() - last) > self._data_stale_timeout

    def _norm_pair(self, pair: str) -> str:
        return pair.split(":")[0] if ":" in pair else pair

    # ── 资金费率 ──

    def _fetch_funding_rates(self) -> None:
        """从币安 API 获取所有永续合约的当前资金费率。

        接口: GET /fapi/v1/premiumIndex
        返回示例: {"symbol":"BTCUSDT","lastFundingRate":"0.0001",...}
        阈值 funding_rate_threshold = -0.0007 即 -0.07%，策略代码中定义
        """
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                with self._api_lock:
                    self._funding_rate_cache.clear()
                    for item in data:
                        symbol = item.get("symbol", "")
                        rate = self._safe_float(item.get("lastFundingRate"))
                        if symbol and rate is not None:
                            for quote in ("USDT", "USDC", "BUSD"):
                                if symbol.endswith(quote) and len(symbol) > len(quote):
                                    pair = f"{symbol[: -len(quote)]}/{quote}"
                                    self._funding_rate_cache[pair] = rate
                                    break
        except Exception as e:
            logger.error("[ShortDecline] 获取资金费率失败: %s", e)

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = self._norm_pair(metadata.get("pair", ""))
        if self._is_data_stale():
            return dataframe

        # ── 空头入口 ──
        with self._api_lock:
            perf_1w = self._perf_1w_cache.get(pair)
            perf_1m = self._perf_1m_cache.get(pair)
            perf_3m = self._perf_3m_cache.get(pair)
            chg_24h = self._price_change_24h_cache.get(pair)
            eligible = pair in self._eligible_pairs

        # 已平仓过的交易对，不再开仓
        with self._api_lock:
            if pair in self._exited_pairs:
                return dataframe

        # 24h涨幅 > 4% 且 从最高点回调 > 2%，才开空
        high_24h = self._high_24h_cache.get(pair)
        low_24h = self._low_24h_cache.get(pair)
        if high_24h and low_24h and high_24h > 0 and low_24h > 0:
            current_close = float(dataframe["close"].iloc[-1])
            range_pct = (high_24h - low_24h) / low_24h
            pullback = (high_24h - current_close) / high_24h
            if range_pct <= 0.04 or pullback <= 0.02:
                logger.info(
                    "[ShortDecline] %s 24h波幅%.1f%% 回调%.1f%% 不满足(需>4%%且>2%%)",
                    pair,
                    range_pct * 100,
                    pullback * 100,
                )
                return dataframe

        if None in (perf_1w, perf_1m, perf_3m, chg_24h):
            return dataframe
        if not eligible:
            return dataframe

        dataframe.loc[dataframe["volume"] > 0, ["enter_short", "enter_tag"]] = (
            1,
            "short_decline",
        )
        return dataframe

    # ── 离场 ──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_stoploss(self, **kwargs) -> float:
        return -1.0  # 无止损，永不爆仓

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return max(
            1.0, min(float(self.config.get("futures_leverage", 10)), max_leverage)
        )

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        np = self._norm_pair(pair)
        avg_entry = self._safe_float(getattr(trade, "open_rate", None))
        if avg_entry is None or avg_entry <= 0:
            return None

        # ── 空头出场逻辑 ──

        low = current_rate
        tmin = self._safe_float(getattr(trade, "min_rate", None))
        if tmin is not None and tmin > 0:
            low = min(low, tmin)
        with self._api_lock:
            mem_low = self._lowest_price.get(np)
            if mem_low is not None:
                low = min(low, mem_low)
            self._lowest_price[np] = low

        open_dt = trade.open_date_utc
        if open_dt is not None:
            hours = (
                current_time.replace(tzinfo=open_dt.tzinfo) - open_dt
            ).total_seconds() / 3600
            if hours >= self.max_hold_hours and current_profit >= 0:
                logger.info(
                    "[ShortDecline] %s 持仓 %.1f小时(≥%d) 成本价=%.6f 现价=%.6f 超时平仓",
                    trade.pair,
                    hours,
                    self.max_hold_hours,
                    avg_entry,
                    current_rate,
                )
                return "timeout_cost_exit"

        drop_from_avg = (avg_entry - low) / avg_entry
        current_drop_from_avg = (avg_entry - current_rate) / avg_entry
        rebound = (current_rate - low) / low if low > 0 else 0.0

        logger.info(
            "custom_exit %s avg=%s low=%s cur=%s drop=%.3f cur_drop=%.3f profit=%.3f rebound=%.3f",
            trade.pair,
            avg_entry,
            low,
            current_rate,
            drop_from_avg,
            current_drop_from_avg,
            current_profit,
            rebound,
        )

        if (
            current_drop_from_avg > 0
            and current_profit > 0
            and drop_from_avg >= self.trail_activate
            and rebound >= self.trail_pullback
        ):
            return "trailing_take_profit"
        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        np = self._norm_pair(pair)

        # ADX 优先级：首次开仓按 ADX 从高到低排序，只有最高 ADX 的交易对才能入场
        if entry_tag == "short_decline":
            # 先获取持仓列表（DB 查询，放在锁外避免阻塞）
            open_pairs = {
                t.pair.split(":")[0] for t in Trade.get_trades_proxy(is_open=True)
            }
            with self._api_lock:
                my_adx = self._adx_cache.get(np, 0)
                eligible_pairs = set(self._eligible_pairs)
                for p, adx in sorted(self._adx_cache.items(), key=lambda x: -x[1]):
                    if (
                        p in eligible_pairs
                        and p not in open_pairs
                        and p != np
                        and adx > my_adx
                    ):
                        return False
                    if adx <= my_adx:
                        break

        # 确认入场后才记录首仓价格和数量
        # ⚠️ 注意：amount 是保证金（stake），需要换算为币数量
        leverage = float(self.config.get("futures_leverage", 10))
        coin_qty = amount * leverage / rate if rate > 0 else amount
        with self._api_lock:
            if np not in self._first_entry_price:
                self._first_entry_price[np] = rate
                self._first_entry_qty[np] = coin_qty  # 存储首次开仓的币数量
            self._last_dca_price[np] = rate  # 记录最近一次入场价（DCA#2+回调基准）
        return True

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | None:
        if not trade.is_short:
            return None

        np = self._norm_pair(trade.pair)
        first_entry, first_qty = self._get_first_entry_state(trade)
        if first_entry is None or first_qty is None:
            return None
        count = self._entry_count(trade) - 1
        if count >= self.max_entry_position_adjustment:
            return None
        price_rise = (current_rate - first_entry) / first_entry

        # ── DCA#1 (+10% 涨幅触发) ──
        if count < 1:
            trigger = self._dca_trigger_rise(count + 1)
            if price_rise >= trigger:
                with self._api_lock:
                    self._lowest_price[np] = current_rate
                    self._dca_pullback_high[np] = (
                        current_rate  # 为DCA#2回调模式初始化峰值
                    )
                leverage = float(self.config.get("futures_leverage", 10))
                stake = round(first_qty * current_rate / leverage, 2)
                logger.info(
                    "[ShortDecline] %s DCA#%d 触发: 首仓价=%.6f 涨幅=%.2f%%(阈值%.0f%%) "
                    "加仓价=%.6f 保证金=%.2f",
                    trade.pair,
                    count + 1,
                    first_entry,
                    price_rise * 100,
                    trigger * 100,
                    current_rate,
                    stake,
                )
                return stake
            return None

        # ── DCA#2+: 基于上次加仓价，先涨 ≥X% 再回调 ≥Y% ──
        callback_idx = count - 1  # count=1→idx0(DCA#2), count=2→idx1(DCA#3), ...
        rises = self.dca_callback_rise
        pulls = self.dca_callback_pullback
        rise_threshold = rises[min(callback_idx, len(rises) - 1)]
        pullback_threshold = pulls[min(callback_idx, len(pulls) - 1)]

        with self._api_lock:
            last_price = self._last_dca_price.get(np, first_entry)
            peak = self._dca_pullback_high.get(np, current_rate)
            peak = max(peak, current_rate)
            self._dca_pullback_high[np] = peak

        if peak <= 0 or last_price <= 0:
            return None
        rise_from_last = (peak - last_price) / last_price
        pullback = (peak - current_rate) / peak
        if rise_from_last < rise_threshold or pullback < pullback_threshold:
            return None

        # 回调 ≥ 3%，触发加仓
        with self._api_lock:
            self._lowest_price[np] = current_rate
            self._dca_pullback_high[np] = current_rate
        leverage = float(self.config.get("futures_leverage", 10))
        stake = round(first_qty * current_rate / leverage, 2)
        logger.info(
            "[ShortDecline] %s DCA#%d 回调触发: 上次价=%.6f 峰值=%.6f "
            "先涨=%.1f%%(需≥%.0f%%) 回调=%.1f%%(需≥%.0f%%) 加仓价=%.6f 保证金=%.2f",
            trade.pair,
            count + 1,
            last_price,
            peak,
            rise_from_last * 100,
            rise_threshold * 100,
            pullback * 100,
            pullback_threshold * 100,
            current_rate,
            stake,
        )
        return stake

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return 50.0

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        np = self._norm_pair(pair)

        # 清理缓存，加入锁定名单
        with self._api_lock:
            self._first_entry_price.pop(np, None)
            self._first_entry_qty.pop(np, None)
            self._lowest_price.pop(np, None)
            self._high_24h_cache.pop(np, None)
            self._low_24h_cache.pop(np, None)
            self._range_24h_cache.pop(np, None)
            self._dca_pullback_high.pop(np, None)
            self._last_dca_price.pop(np, None)
            self._funding_rate_cache.pop(np, None)
            self._funding_watch_pairs.discard(np)
            self._exited_pairs.add(np)
        return True
