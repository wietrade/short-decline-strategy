"""
做空反弹策略

入场:
  排除条件:
    - 1周/1月/3月涨（剔除今日）任一>0
    - 24h跌幅 > 10%
  满足条件 → 做空

开仓: 100U保证金 × 10x杠杆 = 1000U名义, 记录合约数量
加仓: 逆势上涨触发, 间隔按斐波那契数列递增, 基准100U保证金, 最多加5次
      累计触发涨幅: +10% / +20% / +40% / +70% / +120%
离场:
    基于加权持仓均价移动止盈，并按15m波动率动态调整参数
止损: 1000%
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
    stoploss = -10.0
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

    # ── 移动止盈参数（相对加权均价，按15m ATR%分档） ──
    volatility_period = 20
    low_volatility_threshold = 0.02
    high_volatility_threshold = 0.05
    low_vol_trail_activate = 0.04
    low_vol_trail_pullback = 0.02
    mid_vol_trail_activate = 0.06
    mid_vol_trail_pullback = 0.03
    high_vol_trail_activate = 0.10
    high_vol_trail_pullback = 0.05

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
    _volatility_cache: dict[str, float] = {}  # 最近15m ATR%（用于移动止盈分档）
    _api_lock = threading.Lock()
    _last_api_fetch: float = 0
    _api_update_interval = 60

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
        volatility = tr.rolling(self.volatility_period).mean() / close

        pair = self._norm_pair(metadata.get("pair", ""))
        latest_volatility = self._safe_float(volatility.iloc[-1])
        with self._api_lock:
            self._adx_cache[pair] = float(adx.iloc[-1])
            if latest_volatility is not None:
                self._volatility_cache[pair] = latest_volatility

        self._fetch_perf_data()
        return dataframe

    # ── 获取扫描器数据 ──

    def _tv_to_pair(self, raw: str) -> str:
        name = raw.replace(".P", "")
        for quote in ("USDT", "USDC", "BUSD"):
            if name.endswith(quote) and len(name) > len(quote):
                return f"{name[: -len(quote)]}/{quote}"
        return f"{name}/USDT"

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
    @staticmethod
    def _is_eligible(
        perf_1w: float, perf_1m: float, perf_3m: float, chg_24h: float
    ) -> bool:
        if chg_24h < -10:
            return False
        return all((value - chg_24h) <= 0 for value in (perf_1w, perf_1m, perf_3m))

    def _dca_trigger_rise(self, n: int) -> float:
        """第 n 次加仓（n>=1）需要的累计涨幅（相对首仓价）。

        间隔按斐波那契数列递增（前期密集、后期稀疏）：
          fib      = 1, 1, 2, 3, 5, 8, ...
          gap_i    = short_add_threshold * fib(i)
          trigger(n) = Σ gap_i (i=1..n) = base * Σ fib(i)
        以 base=10% 为例：累计触发 = 10% / 20% / 40% / 70% / 120% / ...
        """
        base = self.short_add_threshold
        a, b, fib_sum = 1, 1, 0
        for _ in range(n):
            fib_sum += a
            a, b = b, a + b
        return base * fib_sum

    def _fetch_perf_data(self) -> None:
        now = time.time()
        if now - self._last_api_fetch < self._api_update_interval:
            return
        try:
            resp = requests.get(self.scanner_data_url, timeout=10)
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
                            self._tv_to_pair(name).split(":")[0]
                            if "/" not in name
                            else name.split(":")[0]
                        )
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
        except Exception as e:
            print(f"[ShortDecline] 获取扫描器数据失败: {e}")

    def _norm_pair(self, pair: str) -> str:
        return pair.split(":")[0] if ":" in pair else pair

    def _trailing_params(self, pair: str) -> tuple[float, float, str, Optional[float]]:
        with self._api_lock:
            volatility = self._volatility_cache.get(pair)

        if volatility is None:
            return (
                self.mid_vol_trail_activate,
                self.mid_vol_trail_pullback,
                "unknown",
                None,
            )
        if volatility < self.low_volatility_threshold:
            return (
                self.low_vol_trail_activate,
                self.low_vol_trail_pullback,
                "low",
                volatility,
            )
        if volatility >= self.high_volatility_threshold:
            return (
                self.high_vol_trail_activate,
                self.high_vol_trail_pullback,
                "high",
                volatility,
            )
        return (
            self.mid_vol_trail_activate,
            self.mid_vol_trail_pullback,
            "mid",
            volatility,
        )

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = self._norm_pair(metadata.get("pair", ""))
        with self._api_lock:
            perf_1w = self._perf_1w_cache.get(pair)
            perf_1m = self._perf_1m_cache.get(pair)
            perf_3m = self._perf_3m_cache.get(pair)
            chg_24h = self._price_change_24h_cache.get(pair)
            eligible = pair in self._eligible_pairs

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
        # 1000% 止损，等效于不止损
        return -10.0

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

        # 跟踪持仓期间最低价：优先用 trade.min_rate（重启可恢复），叠加内存缓存
        low = current_rate
        tmin = self._safe_float(getattr(trade, "min_rate", None))
        if tmin is not None and tmin > 0:
            low = min(low, tmin)
        with self._api_lock:
            mem_low = self._lowest_price.get(np)
            if mem_low is not None:
                low = min(low, mem_low)
            self._lowest_price[np] = low

        # 历史最低价相对均价的最大盈利方向跌幅
        drop_from_avg = (avg_entry - low) / avg_entry
        # 当前价相对均价的盈利方向跌幅；空单当前价高于均价时为亏损
        current_drop_from_avg = (avg_entry - current_rate) / avg_entry
        # 从最低点反弹幅度
        rebound = (current_rate - low) / low if low > 0 else 0.0
        trail_activate, trail_pullback, vol_bucket, volatility = self._trailing_params(
            np
        )

        logger.info(
            "custom_exit %s avg=%s low=%s cur=%s drop=%.3f cur_drop=%.3f profit=%.3f rebound=%.3f vol_bucket=%s vol=%s activate=%.3f pullback=%.3f",
            trade.pair,
            avg_entry,
            low,
            current_rate,
            drop_from_avg,
            current_drop_from_avg,
            current_profit,
            rebound,
            vol_bucket,
            volatility,
            trail_activate,
            trail_pullback,
        )

        # 移动止盈：价格已低于均价动态激活阈值，且从最低点反弹动态阈值 → 平仓
        if (
            current_drop_from_avg > 0
            and current_profit > 0
            and drop_from_avg >= trail_activate
            and rebound >= trail_pullback
        ):
            return f"trailing_take_profit_{vol_bucket}_vol"
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
            with self._api_lock:
                my_adx = self._adx_cache.get(np, 0)
                eligible_pairs = set(self._eligible_pairs)
                open_pairs = {
                    t.pair.split(":")[0] for t in Trade.get_trades_proxy(is_open=True)
                }
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
        with self._api_lock:
            if np not in self._first_entry_price:
                self._first_entry_price[np] = rate
                self._first_entry_qty[np] = amount  # 记录首次开仓的币数量
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
        if not trade.is_short:
            return None
        first_entry, first_qty = self._get_first_entry_state(trade)
        if first_entry is None or first_qty is None:
            return None
        count = self._entry_count(trade) - 1
        if count >= self.max_entry_position_adjustment:
            return None
        price_rise = (current_rate - first_entry) / first_entry
        if price_rise >= self._dca_trigger_rise(count + 1):
            # 加仓数量与首次相同：保证金 = 数量 × 当前价 / 杠杆
            leverage = float(self.config.get("futures_leverage", 10))
            return round(first_qty * current_rate / leverage, 2)
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
        with self._api_lock:
            self._first_entry_price.pop(np, None)
            self._first_entry_qty.pop(np, None)
            self._lowest_price.pop(np, None)
        return True
