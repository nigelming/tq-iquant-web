from typing import Callable, Dict, List, Optional
from decimal import Decimal

from .position import Position
from .event import SignalEvent, BarEvent
from .risk_manager import StrategyRiskManager
from tq_iquant_shared.constants import SignalType


class StrategyContext:
    def __init__(
        self,
        strategy_id: int,
        period: str,
        capital_ratio: Decimal,
        max_positions: int,
        *,
        single_open_ratio: Decimal = Decimal("0.1"),
        add_position_threshold: Decimal = Decimal("0.05"),
        max_add_count: int = 2,
        add_position_ratio: Decimal = Decimal("0.1"),
        reduce_position_ratio: Decimal = Decimal("0.3"),
        role: str = "independent",
        master_strategy_id: Optional[int] = None,
    ):
        self.strategy_id = strategy_id
        self.period = period
        self.capital_ratio = capital_ratio
        self.max_positions = max_positions
        # 下单量参数（对应策略表字段，默认值同表默认）
        self.single_open_ratio = single_open_ratio
        self.add_position_threshold = add_position_threshold
        self.max_add_count = max_add_count
        self.add_position_ratio = add_position_ratio
        self.reduce_position_ratio = reduce_position_ratio
        # 主从角色（independent/master/slave）
        self.role = role
        self.master_strategy_id = master_strategy_id
        self.positions: Dict[str, Position] = {}
        # 策略风控（assemble_portfolio 注入；未注入 → _check_risks 跳过并告警，不静默）
        self.strategy_risk: Optional[StrategyRiskManager] = None
        # 公式信号配置：[{"signal_name", "signal_type": SignalType, "trigger_value": int}]
        self.formula_signals: List[dict] = []

    def get_signal(
        self,
        bar: BarEvent,
        signal_cache: Optional[Dict] = None,
        tq_compute: Optional[Callable] = None,
    ) -> List[SignalEvent]:
        """取本策略在当前 bar 的公式信号。cache 优先，miss 调 tq_compute 兜底。

        cache key: (strategy_id, stock_code, bar_time)
        cache value: [{"name": str, "value": int}]
        按 formula_signals 配置（signal_name + trigger_value）匹配，转 SignalEvent。
        """
        signal_cache = signal_cache if signal_cache is not None else {}
        signals: List[SignalEvent] = []
        for stock_code in bar.stocks:
            key = (self.strategy_id, stock_code, bar.bar_time)
            if key in signal_cache:
                outputs = signal_cache[key]
            elif tq_compute is not None:
                outputs = tq_compute(stock_code, self.period, bar)
                signal_cache[key] = outputs
            else:
                outputs = []
            signals.extend(self._match_signals(outputs, stock_code, bar.bar_time))
        return signals

    def _match_signals(
        self, outputs: List[dict], stock_code: str, bar_time
    ) -> List[SignalEvent]:
        """公式输出按 formula_signals 配置匹配 trigger_value，转 SignalEvent。"""
        result: List[SignalEvent] = []
        # 公式输出名 → 值
        output_map = {o["name"]: o["value"] for o in outputs}
        for cfg in self.formula_signals:
            name = cfg["signal_name"]
            if name not in output_map:
                continue
            if output_map[name] != cfg["trigger_value"]:
                continue
            result.append(SignalEvent(
                strategy_id=self.strategy_id,
                stock_code=stock_code,
                signal_name=name,
                signal_type=cfg["signal_type"],
                bar_time=bar_time,
            ))
        return result
