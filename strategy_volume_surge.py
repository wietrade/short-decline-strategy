"""
成交量异动多空策略
==================
基于成交量异动监控 API 的 Freqtrade 多空策略。

核心逻辑：
    1. 轮询 127.0.0.1:3001 获取 24h 成交量暴涨 >500% 的币安永续合约交易对
  2. 【方向逻辑由用户手工定义】
  3. 使用账户总余额 10% 保证金、10 倍杠杆市价开仓
  4. 移动止盈（做多带硬止损）
  5. 每天更新一次单笔保证金：账户总余额 × 10%

作者: auto-generated
"""

import threading
import time
from datetime import date, datetime
from typing import Optional

import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class VolumeSurgeShortStrategy(IStrategy):
    """成交量异动多空策略"""

    # ── 版本 ──
    INTERFACE_VERSION = 3

    # ── 时间框架 ──
    # 实盘建议 1m，回测可用 15m
    timeframe = "15m"

    # ── 期货（杠杆由配置文件 config_trade_surge.json 控制） ──
    trading_mode = "futures"
    margin_mode = "cross"
    can_short = True

    # ── DCA 设置（做空加仓） ──
    position_adjustment_enable = True
    max_entry_position_adjustment = 5  # 最多加仓 5 次

    # ── 风控 ──
    stoploss = -0.99  # 做空不止损，做多由 custom_stoploss 控制
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False

    # ── 资金管理 ──
    stake_amount = "unlimited"

    # ── 参数（可通过 Freqtrade 配置覆盖） ──
    surge_api_url = "http://127.0.0.1:3001/api/list"
    stake_balance_pct = 0.05  # 账户总余额 5% 作为单笔保证金
    fallback_amount = 100
    stoploss_price_pct = 0.30  # 做多硬止损
    trailing_tp_activate_price_pct = 0.05
    trailing_tp_distance = 0.04
    short_add_threshold = 0.10  # 价格上涨 10% 加仓一次

    # ── API 缓存 ──
    _surge_data: dict[str, dict] = {}  # pair -> {recommend_all, ...}
    _surge_pairs_set: set = set()
    _last_api_fetch: float = 0
    _api_lock = threading.Lock()
    _api_update_interval = 60

    # ── 每日保证金缓存 ──
    _daily_stake_amount: Optional[float] = None
    _daily_stake_date: Optional[date] = None

    # ── 移动止盈状态缓存 ──
    _extreme_rates: dict = {}  # trade.id → 极端价格（做空=最低价，做多=最高价）
    _trailing_activated: set = set()
    _trade_entry_price: dict = {}  # trade.id → 首次开仓价（用于加仓判断，不受加仓影响）

    # ── 自定义离场 ──

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """
        做多/做空均使用移动止盈（基于价格回撤）。
        做空：追踪最低价，反弹超过阈值平仓。
        做多：追踪最高价，回落超过阈值平仓。
        """
        if trade.id in self._trailing_activated and trade.id in self._extreme_rates:
            extreme = self._extreme_rates[trade.id]
            if trade.is_short:
                # 做空：价格从最低点反弹超过阈值
                if extreme < current_rate:
                    retracement = (current_rate - extreme) / extreme
                    if retracement >= self.trailing_tp_distance:
                        return f"trailing_tp_{retracement:.1%}"
            else:
                # 做多：价格从最高点回落超过阈值
                if extreme > current_rate:
                    pullback = (extreme - current_rate) / extreme
                    if pullback >= self.trailing_tp_distance:
                        return f"trailing_tp_{pullback:.1%}"
        return None

    # ── 自定义止损 / 移动止盈 ──

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> float:
        """
        做空：不止损（返回 -1），但追踪最低价用于移动止盈。
        做多：硬止损 + 追踪最高价 + 移动止盈。
        """
        tid = trade.id

        # ── 追踪极端价格（用于移动止盈） ──
        if tid not in self._extreme_rates:
            self._extreme_rates[tid] = current_rate
            # 记录首次开仓价（用于加仓判断，不受加仓影响）
            if tid not in self._trade_entry_price:
                self._trade_entry_price[tid] = trade.open_rate
        else:
            if trade.is_short:
                # 做空追踪新低
                if current_rate < self._extreme_rates[tid]:
                    self._extreme_rates[tid] = current_rate
            else:
                # 做多追踪新高
                if current_rate > self._extreme_rates[tid]:
                    self._extreme_rates[tid] = current_rate

        # ── 标记移动止盈激活 ──
        if trade.is_short:
            # 做空：价格从加权持仓成本下跌超过阈值后激活
            # trade.open_rate 由 Freqtrade 自动维护为加权平均（含加仓）
            open_rate = trade.open_rate
            price_drop = (open_rate - current_rate) / open_rate
        else:
            price_drop = (current_rate - trade.open_rate) / trade.open_rate

        if price_drop >= self.trailing_tp_activate_price_pct:
            self._trailing_activated.add(tid)

        # ── 做多硬止损（固定 30% 低于加权平均开仓价） ──
        if not trade.is_short:
            # 目标止损价 = 加权平均开仓价 × (1 - 止损比例)
            target_stop = trade.open_rate * (1 - self.stoploss_price_pct)
            # Freqtrade 公式: stop_price = current_rate * (1 + stoploss / leverage)
            # 反推所需 stoploss 值使 stop_price 始终 = target_stop
            return (target_stop / current_rate - 1) * trade.leverage

        return -1

    # ── 杠杆设置 ──

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
        target = self.config.get("futures_leverage", 1)
        return max(1.0, min(target, max_leverage))

    # ── API 数据获取 ──

    def _fetch_surge_pairs(self) -> None:
        """从 API 拉取异动交易对及趋势数据"""
        now = time.time()
        if now - self._last_api_fetch < self._api_update_interval:
            return
        try:
            resp = requests.get(self.surge_api_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()  # [{"pair":"SAFE/USDT","perf_1w":-10.87,...}, ...]
                with self._api_lock:
                    self._surge_data = {}
                    pairs_set = set()
                    for item in data:
                        pair = item.get("pair", "")
                        if pair:
                            self._surge_data[pair] = item
                            pairs_set.add(pair)
                    self._surge_pairs_set = pairs_set
                    self._last_api_fetch = now
        except Exception as e:
            print(f"[VolumeSurge] API 请求失败: {e}")

    def _is_surge_pair(self, pair: str) -> bool:
        """检查交易对是否在异动列表中"""
        if ":" in pair:
            pair = pair.split(":")[0]
        with self._api_lock:
            return pair in self._surge_pairs_set

    def _refresh_cache(self) -> None:
        self._fetch_surge_pairs()

    # ── 每日保证金计算 ──

    def _get_total_account_balance(self, proposed_stake: float) -> float:
        try:
            if hasattr(self, "wallets") and self.wallets:
                return float(self.wallets.get_total_stake_amount())
        except Exception as e:
            print(f"[VolumeSurge] 读取账户余额失败: {e}")
        dry_run_wallet = self.config.get("dry_run_wallet")
        if dry_run_wallet:
            return float(dry_run_wallet)
        return float(proposed_stake or self.fallback_amount)

    def _get_daily_stake_amount(
        self,
        current_time: datetime,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
    ) -> float:
        today = current_time.date()
        if self._daily_stake_amount is None or self._daily_stake_date != today:
            total_balance = self._get_total_account_balance(proposed_stake)
            self._daily_stake_amount = total_balance * self.stake_balance_pct
            self._daily_stake_date = today
            print(
                f"[VolumeSurge] 每日单笔保证金更新: 账户余额={total_balance:.2f}, "
                f"比例={self.stake_balance_pct:.0%}, 单笔={self._daily_stake_amount:.2f}"
            )
        stake = float(self._daily_stake_amount)
        if min_stake is not None:
            stake = max(stake, float(min_stake))
        if max_stake is not None and max_stake > 0:
            stake = min(stake, float(max_stake))
        return stake

    # ── 指标计算 ──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._refresh_cache()
        return dataframe

    # ── 入场信号 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        入场逻辑：
          - 24 小时内下跌超过 8% → 禁止做空
          - 1月/3月涨幅均 > 0 → 做多（硬止损 30% + 移动止盈）
          - perf_1m/perf_3m 数据缺失 → 不交易
          - 其他情况 → 做空（不止损，每涨10%加仓一次，移动止盈基于持仓成本）
        """
        pair = metadata.get("pair", "")
        # 根据 timeframe 动态计算 24 小时对应的 K 线数
        tf_minutes = int(self.timeframe.replace("m", ""))
        candles_24h = int(24 * 60 / tf_minutes)
        price_change_24h = dataframe["close"].pct_change(candles_24h).fillna(0.0)

        is_surge = self._is_surge_pair(pair)
        has_vol = dataframe["volume"] > 0
        no_sharp_move = price_change_24h > -0.20  # 24小时内不暴跌超过20%

        # 24 小时价格下跌超过 8%，禁止做空
        short_by_price = price_change_24h < -0.08

        # 获取趋势数据判断方向
        perf_1m = None
        perf_3m = None
        if ":" in pair:
            p = pair.split(":")[0]
        else:
            p = pair
        with self._api_lock:
            info = self._surge_data.get(p)
            if info:
                perf_1m = info.get("perf_1m")
                perf_3m = info.get("perf_3m")

        # 数据完整时才能判断方向；数据缺失时不交易
        data_valid = perf_1m is not None and perf_3m is not None

        # 1月/3月 涨幅全部 > 0 → 做多
        go_long = data_valid and perf_1m > 0 and perf_3m > 0

        if is_surge:
            if go_long:
                # 做多
                dataframe.loc[has_vol & no_sharp_move, "enter_long"] = 1
            elif data_valid:
                # 数据完整且不做多 → 做空
                dataframe.loc[
                    has_vol & no_sharp_move & ~short_by_price, "enter_short"
                ] = 1
            # else: 数据缺失，不交易

        return dataframe

    # ── 离场信号 ──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 离场信号完全由 custom_exit 控制，此处不做任何操作
        return dataframe

    # ── 做空加仓 ──

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
        """
        做空加仓：从首次开仓价每上涨 10%，加仓一次。
        Freqtrade 自动维护 trade.open_rate 为加权平均持仓成本，
        移动止盈基于此成本计算。
        """
        if not trade.is_short:
            return None

        count = len(trade.orders) - 1  # 已加仓次数（扣除首次开仓）
        if count >= self.max_entry_position_adjustment:
            return None

        # 使用首次开仓价判断加仓，不受加权均价变化影响
        entry_price = self._trade_entry_price.get(trade.id, trade.open_rate)
        price_rise = (current_rate - entry_price) / entry_price

        if price_rise >= self.short_add_threshold * (count + 1):
            # 加仓：使用与初始仓位相同的金额
            daily_stake = self._get_daily_stake_amount(
                current_time=current_time,
                proposed_stake=self.wallets.get_trade_stake_amount(trade.pair, None),
                min_stake=min_stake,
                max_stake=max_stake,
            )
            print(
                f"[VolumeSurge] 做空加仓 #{count + 1}: {trade.pair}, "
                f"价格={current_rate:.6f}, 距开仓上涨={price_rise:.1%}, "
                f"加仓金额={daily_stake:.2f}"
            )
            return daily_stake

        return None

    # ── 自定义开仓金额 ──

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
        return self._get_daily_stake_amount(
            current_time=current_time,
            proposed_stake=proposed_stake,
            min_stake=min_stake,
            max_stake=max_stake,
        )

    # ── 确认入场回调 ──

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
        return True

    # ── 确认平仓回调 ──

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
        return True
