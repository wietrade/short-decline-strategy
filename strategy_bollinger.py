"""
布林带中线斜率策略
==================

5分钟K线，仅使用布林带中线（SMA）斜率判断趋势方向。

入场:
  中线斜率 > 0  → 做多
  中线斜率 < 0  → 做空

出场:
  斜率归零（变水平）→ 平仓
"""

import logging
from datetime import datetime
from math import isfinite
from typing import Optional

from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class BollingerStrategy(IStrategy):
    """布林带中线斜率策略"""

    INTERFACE_VERSION = 3

    timeframe = "5m"
    startup_candle_count = 50
    process_only_new_candles = True

    can_short = True
    trading_mode = "futures"
    margin_mode = "cross"

    minimal_roi = {"0": 100}
    stoploss = -0.10
    use_custom_stoploss = False
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ── 中线参数 ──
    ma_period = 20  # SMA 周期（布林带中线）
    slope_period = 5  # 斜率计算周期（N根K线差值）
    slope_flat = 0.0001  # 斜率绝对值小于此值视为水平（平仓）

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            result = float(value)
            return result if isfinite(result) else None
        except (TypeError, ValueError):
            return None

    # ── 指标 ──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 中线 = SMA(close)
        mid = dataframe["close"].rolling(window=self.ma_period).mean()
        dataframe["ma_mid"] = mid
        # 斜率 = 当前中线 - N根前中线
        dataframe["slope"] = mid - mid.shift(self.slope_period)
        return dataframe

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        slope = dataframe["slope"]

        # 斜率向上 → 做多
        dataframe.loc[slope > 0, ["enter_long", "enter_tag"]] = (1, "slope_up")

        # 斜率向下 → 做空
        dataframe.loc[slope < 0, ["enter_short", "enter_tag"]] = (1, "slope_down")

        return dataframe

    # ── 离场 ──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 斜率变水平 → 多空都平
        is_flat = dataframe["slope"].abs() < self.slope_flat
        dataframe.loc[is_flat, "exit_long"] = 1
        dataframe.loc[is_flat, "exit_short"] = 1
        return dataframe

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
