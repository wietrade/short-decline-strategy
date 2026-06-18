"""
成交量异动做空策略
==================
基于成交量异动监控 API 的 Freqtrade 做空策略。

核心逻辑：
  1. 轮询 43.165.167.132:3001 获取 24h 成交量暴涨 >800% 的币安永续合约交易对
  2. 新出现的交易对（启动API之后才出现的）→ 立即以 100U 保证金、10 倍杠杆市价开空
  3. 移动止盈: 价格下跌 >= 5%（= 盈利 50U）后激活，价格从最低点反弹 4% 即平仓
  4. 硬止损: 开仓价反向波动 5%（= 亏损 50U）即止损（基于开仓价格计算）
  5. 交易对退出异动列表时平仓
  6. 总模拟资金 1000U，单笔保证金 100U，最多同时持仓 10 个

作者: auto-generated
"""

import threading
import time
from datetime import datetime
from typing import Optional

import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class VolumeSurgeShortStrategy(IStrategy):
    """成交量异动做空策略"""

    # ── 版本 ──
    INTERFACE_VERSION = 3

    # ── 时间框架 ──
    # 实盘建议 1m，回测可用 15m
    timeframe = "15m"

    # ── 期货做空（杠杆由配置文件 config_trade_surge.json 控制） ──
    trading_mode = "futures"
    margin_mode = "isolated"
    can_short = True

    # ── DCA 设置（不启用多次加仓） ──
    position_adjustment_enable = False

    # ── 风控 ──
    stoploss = -0.05  # 备用（实际由 custom_stoploss 按开仓价计算）
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # 允许 populate_exit_trend 信号平仓
    exit_profit_only = False

    # ── 资金管理 ──
    stake_amount = 100  # 每笔保证金 100U

    # ── 参数（可通过 Freqtrade 配置覆盖） ──
    surge_api_url = "http://43.165.167.132:3001/api/list"
    base_short_amount = 100  # 每笔开仓保证金 100U（名义价值 1000U @10x）
    stoploss_price_pct = 0.05  # 硬止损：开仓价反向波动 5%（= 50U）即止损
    trailing_tp_activate_price_pct = (
        0.05  # 移动止盈激活阈值：价格从开仓价下跌 5%（= 盈利 50U）开始追踪
    )
    trailing_tp_distance = (
        0.04  # 移动止盈回撤距离：价格从最低点反弹 4%（= 回撤 40U）即平仓
    )

    # ── API 缓存 ──
    _surge_pairs: list = []
    _surge_pairs_set: set = set()
    _last_api_fetch: float = 0
    _api_lock = threading.Lock()
    _api_update_interval = 60  # 每 60 秒刷新一次

    # ── 最低价追踪缓存 ──
    # 记录每个 trade.id 达到的最低价格（做空最优点），用于计算价格回撤
    _lowest_rates: dict = {}

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
        移动止盈（基于价格回撤）+ 异动列表退出。
        populate_exit_trend 负责异动列表退出的信号。
        """
        # 价格反弹超过阈值 → 通过离场信号平仓（兜底）
        if trade.id in self._lowest_rates:
            lowest = self._lowest_rates[trade.id]
            # 基于价格计算：价格从开仓价下跌比例达到阈值才激活追踪
            price_drop_pct = (trade.open_rate - current_rate) / trade.open_rate
            if (
                lowest < current_rate
                and price_drop_pct >= self.trailing_tp_activate_price_pct
            ):
                retracement = (current_rate - lowest) / lowest
                if retracement >= self.trailing_tp_distance:
                    return f"trailing_tp_{retracement:.1%}"
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
        做空止损 + 价格回撤移动止盈。

        两层保护：
          1. 硬止损：价格从开仓价反向波动 >= 5%（= 50U）时止损
             - 基于开仓价格计算：price_rise% = (current_rate / open_rate) - 1
             - 与杠杆倍数无关，直接控制价格止损距离
             - 100U 保证金 × 10 倍杠杆 = 1000U 名义价值，反向 5% = 亏损 50U
          2. 移动止盈：价格从开仓价下跌 >= 5%（= 盈利 50U）后激活，追踪最低价，价格反弹 4% 即平仓
             - 例：价格 100→80→83.2（反弹 4%），止盈锁定 16.8U 利润

        原理：
          - 硬止损使用开仓价计算价格涨幅，不受 current_profit 杠杆影响
          - 移动止盈追踪做空期间的最低价格（最佳盈利点）
          - 计算当前价格相对最低价的反弹幅度
          - 反弹超过 4% → 平仓锁利
          - 同时通过 stoploss 设置防守价位，防止价格突然跳空穿透
        """
        # ── 硬止损（基于开仓价格计算） ──
        price_rise_pct = (current_rate / trade.open_rate) - 1
        if price_rise_pct >= self.stoploss_price_pct:
            return -self.stoploss_price_pct * trade.leverage

        # ── 追踪最低价（做空最佳价格） ──
        if trade.id not in self._lowest_rates:
            self._lowest_rates[trade.id] = current_rate
        else:
            if current_rate < self._lowest_rates[trade.id]:
                self._lowest_rates[trade.id] = current_rate  # 价格新低，更新

        lowest = self._lowest_rates[trade.id]

        # ── 移动止盈（基于价格计算） ──
        price_drop_pct = (trade.open_rate - current_rate) / trade.open_rate
        if price_drop_pct >= self.trailing_tp_activate_price_pct:
            # 当前价格相对最低价的反弹比例
            retracement = (current_rate - lowest) / lowest

            if retracement >= self.trailing_tp_distance:
                # 反弹超过阈值 → 在当前价位触发平仓
                return current_profit

            # 未超阈值：将止损价位设在「最低价 × (1 + 回撤距离)」
            # 做空正止盈: stop_price = open_rate × (1 - stoploss)
            # => stoploss = 1 - (lowest × (1 + distance) / open_rate)
            stoploss = 1 - (lowest * (1 + self.trailing_tp_distance) / trade.open_rate)
            return stoploss

        # ── 默认：保持硬止损价位（基于开仓价格计算） ──
        return -self.stoploss_price_pct * trade.leverage

    # ── 杠杆设置 ──

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """从配置文件读取杠杆倍数（config_trade_surge.json → futures_leverage）"""
        return self.config.get("futures_leverage", 1)

    # ── API 数据获取 ──

    def _fetch_surge_pairs(self) -> None:
        """从 API 拉取异动交易对列表（API 已过滤为仅 30 分钟内新增的）"""
        now = time.time()
        if now - self._last_api_fetch < self._api_update_interval:
            return
        try:
            resp = requests.get(
                f"{self.surge_api_url}?format=pair",
                timeout=10,
            )
            if resp.status_code == 200:
                pairs = resp.json()
                with self._api_lock:
                    self._surge_pairs = pairs
                    self._surge_pairs_set = set(pairs)
                    self._last_api_fetch = now
        except Exception as e:
            print(f"[VolumeSurge] API 请求失败: {e}")

    def _is_surge_pair(self, pair: str) -> bool:
        """检查交易对是否在异动列表中"""
        # API 返回格式: "RIF/USDT"
        # Freqtrade 永续合约格式: "RIF/USDT:USDT" 或 "TRUMP/USDC:USDC"
        # 去掉 ":XXX" 结算币种后缀以匹配 API 结果
        if ":" in pair:
            pair = pair.split(":")[0]
        with self._api_lock:
            return pair in self._surge_pairs_set

    def _refresh_cache(self) -> None:
        """强制刷新缓存（在主线程中安全调用）"""
        self._fetch_surge_pairs()

    # ── 指标计算 ──

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算所需指标"""
        # 定期刷新 API 缓存
        self._refresh_cache()
        return dataframe

    # ── 入场信号 ──

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        入场逻辑:
        当交易对出现在成交量异动列表中时，在当前 K 线开空。
        """
        pair = metadata.get("pair", "")

        # 检查是否在异动列表中（API 已过滤为仅 30 分钟内新增的）
        # 入场条件: 出现在异动列表中
        dataframe.loc[
            (dataframe["volume"] > 0) & (self._is_surge_pair(pair)),
            "enter_short",
        ] = 1

        return dataframe

    # ── 离场信号 ──

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        交易对退出成交量异动列表时平仓。
        """
        pair = metadata.get("pair", "")
        is_surge = self._is_surge_pair(pair)
        # 不在异动列表中 → 平仓
        dataframe.loc[
            (dataframe["volume"] > 0) & (~is_surge),
            "exit_short",
        ] = 1
        return dataframe

    # ── 自定义开仓金额 ──

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float,
        max_stake: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """初始开仓使用固定的 100U 保证金"""
        if side == "short":
            return self.base_short_amount
        return proposed_stake

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
        """确认入场前检查交易对是否仍在异动列表中"""
        if side == "short":
            return self._is_surge_pair(pair)
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


# ==========================
# 配置文件模板 (放入 config.json)
# ==========================
#
# {
#     "max_open_trades": 20,
#     "stake_currency": "USDT",
#     "stake_amount": 10,
#     "dry_run": true,
#     "trading_mode": "futures",
#     "margin_mode": "isolated",
#     "futures_leverage": 10,
#     "exchange": {
#         "name": "binance",
#         "key": "",
#         "secret": "",
#         "pair_whitelist": [],
#         "ccxt_config": {
#             "options": {
#                 "defaultType": "future"
#             }
#         }
#     },
#     "pairlists": [
#         {"method": "StaticPairList"}
#     ],
#     "entry_pricing": {
#         "price_side": "same"
#     },
#     "exit_pricing": {
#         "price_side": "same"
#     },
#     "unfilledtimeout": {
#         "entry": 30,
#         "exit": 30
#     },
#     "strategy": "VolumeSurgeShortStrategy"
# }
