"""
做空反弹策略（含多头反转）

空头逻辑:
  入场: 扫描器异动交易对，做空
  止损: 200%（保证金亏损比例）
  止盈: 移动止盈（6%激活 / 3%回撤）
  超时: 18小时回到成本价平仓
  加仓: DCA金字塔（+10%/+20%/+50%/+100%/+200%）

多头逻辑（空头止损后触发）:
  入场: 空头止损平仓后，按同等数量立即开多
  止损: 50%
  止盈: 移动止盈（6%激活 / 3%回撤，与空头相同参数）
  超时: 18小时回到成本价平仓
  不加仓
  多头止损后 → 冷却1小时 → 回到空头模式
  多头止盈/超时 → 立即回到空头模式
"""

import logging
import threading
import time
from datetime import datetime
from math import isfinite
from typing import Optional

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
    stoploss = -2.0  # 默认空头止损 200%（custom_stoploss 中按方向区分）
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

    # ── 订单类型 ──
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ── 加仓参数 ──
    short_add_threshold = 0.10  # 斐波那契基准间隔（相对首仓价）

    # ── 移动止盈参数（相对加权均价，空头/多头共用）──
    trail_activate = 0.06  # 盈利方向偏离均价 6% 激活移动止盈
    trail_pullback = 0.03  # 从极值点回撤 3% 平仓

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
    _eligible_pairs: set[str] = set()
    _first_entry_price: dict[str, float] = {}
    _first_entry_qty: dict[str, float] = {}  # 首次开仓的币数量（用于DCA保持相同数量）
    _lowest_price: dict[str, float] = {}  # 持仓期间最低价（用于移动止盈）
    _highest_price: dict[str, float] = {}  # 多头持仓期间最高价

    # ── 多头反转状态 ──
    # pair -> {"stop_out_price": float, "stop_out_time": str, "short_qty": float}
    _flip_to_long: dict[str, dict] = {}
    # 多头止损冷却期（秒），防止反复翻转
    _long_cooldown_until: dict[str, float] = {}
    long_cooldown_seconds = 3600  # 多头止损后 1 小时内不重新开空

    # ── 资金费率 ──
    _funding_rate_cache: dict[
        str, float
    ] = {}  # pair -> 当前资金费率（如 -0.005 = -0.5%）
    _funding_watch_pairs: set[str] = set()  # 因资金费率过负被暂缓的交易对
    funding_rate_threshold = (
        -0.0005
    )  # 资金费率阈值 -0.05%，低于此值禁止开空（山寨币急拉时空头付钱）

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

        self._fetch_perf_data()
        return dataframe

    # ── 获取扫描器数据 ──

    @staticmethod
    def _safe_float(value) -> Optional[float]:
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

    def _get_first_entry_state(
        self, trade: Trade
    ) -> tuple[Optional[float], Optional[float]]:
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
        perf_1w: float, perf_1m: float, perf_3m: float, chg_24h: float
    ) -> bool:
        if chg_24h < -10:
            return False
        return all((value - chg_24h) <= 0 for value in (perf_1w, perf_1m, perf_3m))

    def _dca_trigger_rise(self, n: int) -> float:
        """第 n 次加仓（n>=1）需要的累计涨幅（相对首仓价）。

        间隔递增（base=10%）：
          n=1: 10% / n=2: 20% / n=3: 50% / n=4: 100% / n=5: 200%
        """
        triggers = [0.10, 0.20, 0.50, 1.00, 2.00]
        if 1 <= n <= len(triggers):
            return triggers[n - 1]
        # 超出范围继续按 2× 翻倍
        return 2.00 * (2 ** (n - 5))

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
                    self._eligible_pairs.clear()
                    for r in results:
                        name = r.get("name") or r.get("pair", "")
                        if not name:
                            continue
                        pair_key = (
                            name.split(":")[0]
                            if "/" in name
                            else name.replace(".P", "").split(":")[0]
                        )
                        # 标准化：去掉后缀如 :USDT
                        pair_key = pair_key.split(":")[0]
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
                        if None in (perf_1w, perf_1m, perf_3m, chg_24h):
                            continue
                        self._perf_1w_cache[pair_key] = perf_1w
                        self._perf_1m_cache[pair_key] = perf_1m
                        self._perf_3m_cache[pair_key] = perf_3m
                        self._price_change_24h_cache[pair_key] = chg_24h
                        if self._is_eligible(perf_1w, perf_1m, perf_3m, chg_24h):
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
                    if None not in (
                        perf_1w,
                        perf_1m,
                        perf_3m,
                        chg_24h,
                    ) and self._is_eligible(perf_1w, perf_1m, perf_3m, chg_24h):
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
            print(f"[ShortDecline] 获取扫描器数据失败: {e}")

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
        阈值 funding_rate_threshold = -0.0005 即 -0.05%，策略代码中定义
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
            print(f"[ShortDecline] 获取资金费率失败: {e}")

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = self._norm_pair(metadata.get("pair", ""))
        if self._is_data_stale():
            return dataframe

        # ── 多头反转入口 ──
        with self._api_lock:
            flip_info = self._flip_to_long.get(pair)

        if flip_info is not None:
            # 检查是否已有该交易对的持仓（避免重复开多）
            open_trades = Trade.get_trades_proxy(is_open=True)
            already_open = any(self._norm_pair(t.pair) == pair for t in open_trades)
            if not already_open:
                dataframe.loc[dataframe["volume"] > 0, ["enter_long", "enter_tag"]] = (
                    1,
                    "flip_long",
                )
                logger.info(
                    "[ShortDecline] %s 触发多头反转入口 (空头止损价=%.6f)",
                    pair,
                    flip_info.get("stop_out_price", 0),
                )
            return dataframe

        # ── 空头入口（原逻辑）──
        with self._api_lock:
            perf_1w = self._perf_1w_cache.get(pair)
            perf_1m = self._perf_1m_cache.get(pair)
            perf_3m = self._perf_3m_cache.get(pair)
            chg_24h = self._price_change_24h_cache.get(pair)
            eligible = pair in self._eligible_pairs
            cooldown_until = self._long_cooldown_until.get(pair, 0)

        # 多头止损冷却期内不开空
        if cooldown_until > 0 and time.time() < cooldown_until:
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
        trade = kwargs.get("trade")
        if trade is not None and not trade.is_short:
            return -0.5  # 多头止损 50%
        return -2.0  # 空头止损 200%

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
    ) -> Optional[str]:
        np = self._norm_pair(pair)
        avg_entry = self._safe_float(getattr(trade, "open_rate", None))
        if avg_entry is None or avg_entry <= 0:
            return None

        is_long = not trade.is_short

        # ── 多头出场逻辑 ──
        if is_long:
            # 跟踪持仓期间最高价
            high = current_rate
            tmax = self._safe_float(getattr(trade, "max_rate", None))
            if tmax is not None and tmax > 0:
                high = max(high, tmax)
            with self._api_lock:
                mem_high = self._highest_price.get(np)
                if mem_high is not None:
                    high = max(high, mem_high)
                self._highest_price[np] = high

            # 超时平仓
            open_dt = trade.open_date_utc
            if open_dt is not None:
                hours = (
                    current_time.replace(tzinfo=open_dt.tzinfo) - open_dt
                ).total_seconds() / 3600
                if hours >= self.max_hold_hours and current_profit >= 0:
                    return "long_timeout_cost_exit"

            rise_from_avg = (current_rate - avg_entry) / avg_entry
            highest_rise = (high - avg_entry) / avg_entry
            pullback = (high - current_rate) / high if high > 0 else 0.0

            # 多头移动止盈：价格高于均价激活阈值，从最高点回落 → 平仓
            if (
                rise_from_avg > 0
                and current_profit > 0
                and highest_rise >= self.trail_activate
                and pullback >= self.trail_pullback
            ):
                return "long_trailing_take_profit"
            return None

        # ── 空头出场逻辑（原逻辑）──
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
    ) -> Optional[float]:
        # 多头不加仓
        if not trade.is_short:
            return None
        first_entry, first_qty = self._get_first_entry_state(trade)
        if first_entry is None or first_qty is None:
            return None
        count = self._entry_count(trade) - 1
        if count >= self.max_entry_position_adjustment:
            return None
        price_rise = (current_rate - first_entry) / first_entry
        trigger = self._dca_trigger_rise(count + 1)
        if price_rise >= trigger:
            np = self._norm_pair(trade.pair)
            with self._api_lock:
                self._lowest_price[np] = current_rate
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
        return 100.0

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

        # ── 空头止损 → 触发多头反转 ──
        if trade.is_short and "stop_loss" in exit_reason:
            short_qty = self._safe_float(getattr(trade, "amount", None)) or 0
            entry_count = self._entry_count(trade)
            first_qty = short_qty / entry_count if entry_count > 0 else short_qty
            with self._api_lock:
                self._flip_to_long[np] = {
                    "stop_out_price": rate,
                    "stop_out_time": current_time.isoformat(),
                    "short_qty": first_qty,
                }
            logger.info(
                "[ShortDecline] %s 空头止损 @%.6f → 触发多头反转 (数量=%.4f)",
                pair,
                rate,
                first_qty,
            )

        # ── 多头止损 → 回到空头模式，设冷却期 ──
        if not trade.is_short and "stop_loss" in exit_reason:
            with self._api_lock:
                self._flip_to_long.pop(np, None)
                self._long_cooldown_until[np] = time.time() + self.long_cooldown_seconds
            logger.info(
                "[ShortDecline] %s 多头止损 @%.6f → 回到空头模式 (冷却%ds)",
                pair,
                rate,
                self.long_cooldown_seconds,
            )

        # ── 多头正常止盈 → 回到空头模式 ──
        if not trade.is_short and "stop_loss" not in exit_reason:
            with self._api_lock:
                self._flip_to_long.pop(np, None)
            logger.info("[ShortDecline] %s 多头止盈 @%.6f → 回到空头模式", pair, rate)

        # 清理缓存
        with self._api_lock:
            self._first_entry_price.pop(np, None)
            self._first_entry_qty.pop(np, None)
            self._lowest_price.pop(np, None)
            self._highest_price.pop(np, None)
            self._funding_rate_cache.pop(np, None)
            self._funding_watch_pairs.discard(np)
        return True
