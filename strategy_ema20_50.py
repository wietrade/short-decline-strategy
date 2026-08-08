"""
EMA20/50 多空反转策略（freqtrade 期货，15m K 线）

实现依据 freqtrade 官方文档:
  - Stoploss:  https://www.freqtrade.io/en/stable/stoploss/
  - Callbacks: https://www.freqtrade.io/en/stable/strategy-callbacks/
  - Trade:     https://www.freqtrade.io/en/stable/trade-object/

入场（统一开仓过滤，首仓 / 反转 / 止损后反手 全部适用）:
  1. 排除 BTC/USDT（策略内 + config pair_blacklist 双保险）
  2. 1h 涨跌幅 |chg| > 8% → 多空都不开（对称）
  3. ADX(14) 排序: 存在未持仓且 ADX 更高的待入场交易对 → 拒绝本次开仓

首仓方向（无仓位时）:
  价格 > EMA50 → 开多；价格 < EMA50 → 开空（与 EMA20/50 大小方向一致）

反转（价格穿越 EMA50）:
  价格跌破 EMA50: 持有多仓 → 平多 + 尝试开空；无仓位 → 尝试开空
  价格升破 EMA50: 持有空仓 → 平空 + 尝试开多；无仓位 → 尝试开多
  反转开仓同样受 1h 涨跌 + ADX 过滤

离场（多条件同时生效，谁先触发谁平）:
  内置止损（兜底）: stoploss = -0.20，10x 杠杆下 = 价格反向 2%（基于开仓价）
  移动止盈: 盈利偏离均价 ≥3% 激活，从极值回撤 1.5% 平仓
  EMA 交叉: EMA20 < EMA50 平多；EMA20 > EMA50 平空
  价格穿越 EMA50: 平旧仓（反手开仓在入场逻辑处理）
  24h 超时: 盈利 → 立即平仓；亏损 → 等价格回开仓价平仓（止损兜底，长期不回人工平）

止损后行为: 立即反手（受 1h + ADX 过滤）
"""

import logging
import threading
from datetime import datetime
from math import isfinite

import talib
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class Ema20Ema50Strategy(IStrategy):
    """EMA20/50 多空反转策略"""

    INTERFACE_VERSION = 3

    timeframe = "15m"
    startup_candle_count = 100
    process_only_new_candles = True

    can_short = True
    trading_mode = "futures"
    margin_mode = "cross"

    # 禁用最小 ROI（止盈完全由 custom_exit 的移动止盈 + 超时管理）
    minimal_roi = {}

    # 内置价格止损（兜底）:
    #   官方文档: stoploss 是本笔交易的亏损风险（保证金比例），价格位移 = |stoploss| / 杠杆
    #   futures_leverage=10 时，-0.20 → 价格反向 2%（基于开仓价）触发
    stoploss = -0.20
    use_custom_stoploss = False
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    position_adjustment_enable = False

    # ── 订单类型（全市价单，止损由 freqtrade 端管理）──
    order_types = {
        "entry": "market",
        "exit": "market",
        "emergency_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ── EMA 参数 ──
    ema_short = 20
    ema_long = 50

    # ── ADX（TA-Lib，仅用于入场优先级排序，不做方向过滤）──
    adx_period = 14

    # ── 1h 剧烈波动过滤（对称）──
    max_1h_change_pct = 8.0  # |1h 涨跌幅| > 8% → 多空都不开

    # ── 移动止盈（基于价格）──
    trail_activate = 0.03  # 盈利偏离均价 3% 激活
    trail_pullback = 0.015  # 从极值回撤 1.5% 平仓

    # ── 持仓超时 ──
    max_hold_hours = 24

    # ── 运行态缓存（live/dry-run 的跨交易对排序用）──
    _adx_cache: dict[str, float] = {}  # pair -> 当前 ADX（入场排序）
    _lowest_price: dict[str, float] = {}  # 空头持仓期间最低价（移动止盈用）
    _highest_price: dict[str, float] = {}  # 多头持仓期间最高价（移动止盈用）
    _reversal_pending: dict[str, str] = {}  # pair -> "long"/"short"（止损后反手）
    _pending_entry_pairs: set[str] = set()  # 本周期有待入场信号的交易对
    _pending_cycle_date = None  # 周期重置标记
    _api_lock = threading.Lock()

    # ── 指标（全部向量化）──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        dataframe["ema_short"] = close.ewm(span=self.ema_short, adjust=False).mean()
        dataframe["ema_long"] = close.ewm(span=self.ema_long, adjust=False).mean()

        # ADX（TA-Lib）: 仅用于入场优先级排序
        dataframe["adx"] = talib.ADX(
            dataframe["high"], dataframe["low"], close, timeperiod=self.adx_period
        )

        # 1h 涨跌幅（15m × 4 根 K 线）: 入场过滤用
        dataframe["chg_1h_pct"] = close.pct_change(4) * 100.0

        # 缓存当前 ADX 用于跨交易对排序（live/dry-run）
        if self.config["runmode"].value in ("live", "dry_run"):
            pair = self._norm_pair(metadata.get("pair", ""))
            adx_value = float(dataframe["adx"].iloc[-1])
            with self._api_lock:
                self._adx_cache[pair] = adx_value if isfinite(adx_value) else 0.0

        return dataframe

    # ── 入场 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = self._norm_pair(metadata.get("pair", ""))

        # 周期切换时清空待入场集合（按最新 K 线日期判断）
        with self._api_lock:
            last_date = dataframe["date"].iloc[-1]
            if self._pending_cycle_date != last_date:
                self._pending_cycle_date = last_date
                self._pending_entry_pairs.clear()

        # 排除 BTC/USDT
        if pair == "BTC/USDT":
            with self._api_lock:
                self._pending_entry_pairs.discard(pair)
            return dataframe

        # 统一开仓过滤: |1h 涨跌幅| <= 8% 且成交量 > 0
        filter_ok = (dataframe["chg_1h_pct"].abs() <= self.max_1h_change_pct) & (
            dataframe["volume"] > 0
        )

        # ── 止损后反手（优先于普通开仓，同样受 1h + ADX 过滤）──
        with self._api_lock:
            reversal = self._reversal_pending.get(pair)
        if reversal == "short":
            dataframe.loc[filter_ok, ["enter_short", "enter_tag"]] = (
                1,
                "reversal_short",
            )
            with self._api_lock:
                self._pending_entry_pairs.add(pair)
            return dataframe
        if reversal == "long":
            dataframe.loc[filter_ok, ["enter_long", "enter_tag"]] = (
                1,
                "reversal_long",
            )
            with self._api_lock:
                self._pending_entry_pairs.add(pair)
            return dataframe

        # ── 无仓位首仓/直接开仓: 价格 vs EMA50（与 EMA20/50 方向一致）──
        dataframe.loc[
            filter_ok & (dataframe["close"] > dataframe["ema_long"]),
            ["enter_long", "enter_tag"],
        ] = (1, "price_above_ema50")
        dataframe.loc[
            filter_ok & (dataframe["close"] < dataframe["ema_long"]),
            ["enter_short", "enter_tag"],
        ] = (1, "price_below_ema50")

        # 记录本周期当前 K 线是否产生入场信号（ADX 排序用，live/dry-run）
        if self.config["runmode"].value in ("live", "dry_run"):
            has_signal = bool(
                (dataframe["enter_long"].fillna(0).iloc[-1] == 1)
                or (dataframe["enter_short"].fillna(0).iloc[-1] == 1)
            )
            with self._api_lock:
                if has_signal:
                    self._pending_entry_pairs.add(pair)
                else:
                    self._pending_entry_pairs.discard(pair)

        return dataframe

    # ── 离场（全部向量化）──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = self._norm_pair(metadata.get("pair", ""))
        # 排除 BTC/USDT
        if pair == "BTC/USDT":
            return dataframe

        ema_short = dataframe["ema_short"]
        ema_long = dataframe["ema_long"]
        close = dataframe["close"]

        # EMA 交叉离场: EMA20 < EMA50 → 平多；EMA20 > EMA50 → 平空
        dataframe.loc[ema_short < ema_long, ["exit_long", "exit_tag"]] = (
            1,
            "ema_cross_down",
        )
        dataframe.loc[ema_short > ema_long, ["exit_short", "exit_tag"]] = (
            1,
            "ema_cross_up",
        )

        # 价格穿越 EMA50 离场（平旧仓；反手开仓在入场逻辑处理）
        dataframe.loc[close < ema_long, ["exit_long", "exit_tag"]] = (
            1,
            "ema50_break_long",
        )
        dataframe.loc[close > ema_long, ["exit_short", "exit_tag"]] = (
            1,
            "ema50_break_short",
        )

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
        """返回目标杠杆；最终会被截断到 [1.0, max_leverage]"""
        return max(
            1.0, min(float(self.config.get("futures_leverage", 10)), max_leverage)
        )

    # ── 移动止盈 + 24h 超时（与内置止损同时生效）──

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        open_rate = self._safe_float(getattr(trade, "open_rate", None))
        if open_rate is None or open_rate <= 0:
            return None

        np = self._norm_pair(pair)

        # ── 24h 超时 ──
        open_date = getattr(trade, "open_date_utc", None)
        if open_date is not None:
            hours = (
                current_time.replace(tzinfo=open_date.tzinfo) - open_date
            ).total_seconds() / 3600.0
            if hours >= self.max_hold_hours:
                if current_profit > 0:
                    logger.info(
                        "[Ema20Ema50] %s %s 持仓%.1fh 盈利%.2f%% 超时平仓",
                        pair,
                        "空头" if trade.is_short else "多头",
                        hours,
                        current_profit * 100,
                    )
                    return "timeout_profit_exit"
                # 亏损: 等价格回到开仓价（成本价）再平；止损兜底；长期不回人工平
                if trade.is_short:
                    if current_rate <= open_rate:
                        return "timeout_cost_exit"
                else:
                    if current_rate >= open_rate:
                        return "timeout_cost_exit"

        # ── 移动止盈（基于价格）──
        if trade.is_short:
            # 空头: 跟踪持仓期间最低价
            low = current_rate
            min_rate = self._safe_float(getattr(trade, "min_rate", None))
            if min_rate is not None and min_rate > 0:
                low = min(low, min_rate)
            with self._api_lock:
                mem_low = self._lowest_price.get(np)
            if mem_low is not None:
                low = min(low, mem_low)
            with self._api_lock:
                self._lowest_price[np] = low

            drop_from_open = (open_rate - low) / open_rate
            current_drop = (open_rate - current_rate) / open_rate
            rebound = (current_rate - low) / low if low > 0 else 0.0

            if (
                current_drop > 0
                and current_profit > 0
                and drop_from_open >= self.trail_activate
                and rebound >= self.trail_pullback
            ):
                return "trailing_take_profit"
        else:
            # 多头: 跟踪持仓期间最高价
            high = current_rate
            max_rate = self._safe_float(getattr(trade, "max_rate", None))
            if max_rate is not None and max_rate > 0:
                high = max(high, max_rate)
            with self._api_lock:
                mem_high = self._highest_price.get(np)
            if mem_high is not None:
                high = max(high, mem_high)
            with self._api_lock:
                self._highest_price[np] = high

            rise_from_open = (high - open_rate) / open_rate
            current_rise = (current_rate - open_rate) / open_rate
            pullback = (high - current_rate) / high if high > 0 else 0.0

            if (
                current_rise > 0
                and current_profit > 0
                and rise_from_open >= self.trail_activate
                and pullback >= self.trail_pullback
            ):
                return "trailing_take_profit"

        return None

    # ── 入场确认: ADX 优先级排序 + 清理反转标记 ──

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

        # 当前持仓交易对（回调内可安全查询 Trade）
        open_pairs = {
            self._norm_pair(t.pair) for t in Trade.get_trades_proxy(is_open=True)
        }
        with self._api_lock:
            my_adx = self._adx_cache.get(np, 0.0)
            pending = set(self._pending_entry_pairs)

        # ADX 优先级: 有未持仓且 ADX 更高的待入场交易对 → 拒绝本次开仓
        for other, other_adx in sorted(self._adx_cache.items(), key=lambda kv: -kv[1]):
            if (
                other in pending
                and other not in open_pairs
                and other != np
                and other_adx > my_adx
            ):
                logger.info(
                    "[Ema20Ema50] %s denied: %s ADX %.1f > %.1f",
                    np,
                    other,
                    other_adx,
                    my_adx,
                )
                return False
            if other_adx <= my_adx:
                break

        # 入场成功: 清理止损反手标记
        if entry_tag in ("reversal_long", "reversal_short"):
            with self._api_lock:
                self._reversal_pending.pop(np, None)

        return True

    # ── 平仓清理 + 止损后反手 ──

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

        # 内置止损（兜底）出场 → 立即反手（受 1h + ADX 过滤）
        if exit_reason == "stop_loss":
            with self._api_lock:
                self._reversal_pending[np] = "long" if trade.is_short else "short"
            logger.info(
                "[Ema20Ema50] %s 止损平仓 @%.6f → 反手%s",
                pair,
                rate,
                "做多" if trade.is_short else "做空",
            )

        # 清理持仓极值缓存
        with self._api_lock:
            self._lowest_price.pop(np, None)
            self._highest_price.pop(np, None)
        return True

    # ── 辅助 ──

    @staticmethod
    def _norm_pair(pair: str) -> str:
        return pair.split(":")[0] if ":" in pair else pair

    @staticmethod
    def _safe_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            result = float(value)
            return result if isfinite(result) else None
        except (TypeError, ValueError):
            return None
