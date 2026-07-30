from typing import Callable, Dict, List, Optional
from decimal import Decimal

from .position import Position
from .event import SignalEvent, BarEvent
from tq_iquant_shared.constants import SignalType


class StrategyContext:
    def __init__(
        self,
        strategy_id: int,
        period: str,
        capital_ratio: Decimal,
        max_positions: int,
    ):
        self.strategy_id = strategy_id
        self.period = period
        self.capital_ratio = capital_ratio
        self.max_positions = max_positions
        self.positions: Dict[str, Position] = {}
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
