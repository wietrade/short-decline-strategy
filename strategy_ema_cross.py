"""
EMA 交叉策略（EMA9 / EMA21）

多头逻辑:
  入场: EMA9 > EMA21 且 ADX > 25
  平仓: EMA9 < EMA21

空头逻辑:
  入场: EMA9 < EMA21 且 ADX > 25
  平仓: EMA9 > EMA21

止损: -20%（10x杠杆下 = 价格反向2%触发）
止盈: 移动止盈（20%激活 / 10%回撤）
"""

import logging
from datetime import datetime
from math import isfinite

import pandas as pd
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class EmaCrossStrategy(IStrategy):
    """EMA9/EMA21 交叉策略 — 顺势多空切换"""

    INTERFACE_VERSION = 3

    timeframe = "15m"
    startup_candle_count = 100
    process_only_new_candles = True

    can_short = True
    trading_mode = "futures"
    margin_mode = "cross"

    minimal_roi = {"0": 100}
    stoploss = -0.20  # 固定止损 -20%保证金（10x杠杆下 = 价格反向2%触发）
    use_custom_stoploss = False
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    position_adjustment_enable = False

    # ── 订单类型 ──
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ── EMA 参数 ──
    ema_short = 9
    ema_long = 21

    # ── ADX 过滤 ──
    adx_period = 14
    adx_threshold = 25  # ADX > 25 才允许入场

    # ── 移动止盈参数 ──
    trail_activate = 0.20  # 盈利偏离均价 20% 激活
    trail_pullback = 0.10  # 从极值点回撤 10% 平仓

    # ── 持仓极值追踪 ──
    _lowest_price: dict[str, float] = {}  # 空头期间最低价
    _highest_price: dict[str, float] = {}  # 多头期间最高价

    # ── 指标 ──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EMA
        dataframe["ema_short"] = (
            dataframe["close"].ewm(span=self.ema_short, adjust=False).mean()
        )
        dataframe["ema_long"] = (
            dataframe["close"].ewm(span=self.ema_long, adjust=False).mean()
        )

        # ADX
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
        dataframe["adx"] = dx.ewm(span=self.adx_period, adjust=False).mean()

        return dataframe

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        # 排除 BTC/USDT
        if "BTC/USDT" in pair:
            return dataframe

        ema_s = dataframe["ema_short"]
        ema_l = dataframe["ema_long"]
        adx = dataframe["adx"]

        # ADX 过滤
        trend_strong = adx > self.adx_threshold

        # EMA9 上穿 EMA21 → 做多
        long_cond = (ema_s > ema_l) & trend_strong
        dataframe.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "ema_long")

        # EMA9 下穿 EMA21 → 做空
        short_cond = (ema_s < ema_l) & trend_strong
        dataframe.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "ema_short")

        return dataframe

    # ── 离场 ──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        # 排除 BTC/USDT
        if "BTC/USDT" in pair:
            return dataframe

        ema_s = dataframe["ema_short"]
        ema_l = dataframe["ema_long"]

        # EMA9 下穿 EMA21 → 平多
        dataframe.loc[ema_s < ema_l, ["exit_long", "exit_tag"]] = (1, "ema_cross_down")

        # EMA9 上穿 EMA21 → 平空
        dataframe.loc[ema_s > ema_l, ["exit_short", "exit_tag"]] = (1, "ema_cross_up")

        return dataframe

    # ── 杠杆 ──

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

    # ── 移动止盈 ──

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        avg_entry = self._safe_float(getattr(trade, "open_rate", None))
        if avg_entry is None or avg_entry <= 0:
            return None

        pair_key = pair.split(":")[0] if ":" in pair else pair

        if trade.is_short:
            # ── 空头移动止盈 ──
            low = current_rate
            tmin = self._safe_float(getattr(trade, "min_rate", None))
            if tmin is not None and tmin > 0:
                low = min(low, tmin)
            mem_low = self._lowest_price.get(pair_key)
            if mem_low is not None:
                low = min(low, mem_low)
            self._lowest_price[pair_key] = low

            drop_from_avg = (avg_entry - low) / avg_entry
            current_drop = (avg_entry - current_rate) / avg_entry
            rebound = (current_rate - low) / low if low > 0 else 0.0

            if (
                current_drop > 0
                and current_profit > 0
                and drop_from_avg >= self.trail_activate
                and rebound >= self.trail_pullback
            ):
                return "trailing_take_profit"
        else:
            # ── 多头移动止盈 ──
            high = current_rate
            tmax = self._safe_float(getattr(trade, "max_rate", None))
            if tmax is not None and tmax > 0:
                high = max(high, tmax)
            mem_high = self._highest_price.get(pair_key)
            if mem_high is not None:
                high = max(high, mem_high)
            self._highest_price[pair_key] = high

            rise_from_avg = (high - avg_entry) / avg_entry
            current_rise = (current_rate - avg_entry) / avg_entry
            pullback = (high - current_rate) / high if high > 0 else 0.0

            if (
                current_rise > 0
                and current_profit > 0
                and rise_from_avg >= self.trail_activate
                and pullback >= self.trail_pullback
            ):
                return "trailing_take_profit"

        return None

    # ── 下单金额 ──

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

    # ── 平仓清理 ──

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
        pair_key = pair.split(":")[0] if ":" in pair else pair
        self._lowest_price.pop(pair_key, None)
        self._highest_price.pop(pair_key, None)
        return True

    @staticmethod
    def _safe_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            result = float(value)
            return result if isfinite(result) else None
        except (TypeError, ValueError):
            return None
