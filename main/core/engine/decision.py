"""决策闸门采集器（调参可观测性）。

信号→成交链路上每个会「触发风控单 / 丢单 / 拒单 / 缩量 / 压制」的决策点都记一条
DecisionEvent。回测与实盘共用同一决策层（portfolio/execution_engine/account/
risk_manager），在此层埋点即两路同时覆盖；实盘专属闸门（收盘/日历/去重/在途/桥）
在 live_engine 补埋。

设计：
- DecisionEvent：一条闸门触发记录（闸门码、层、动作、关联参数名/阈值/实际值、
  请求量vs最终量、时间、策略/股票、人读原因）。
- DecisionRecorder：run/session 级缓冲，record() 追加、drain() 取并清空。
- NULL_RECORDER：空操作单例，Portfolio/Account/ExecutionEngine/risk_manager 默认持有，
  使现有直接构造（不带 recorder）的调用与测试零改动。
- summarize_decisions：按 (gate, param_name) 聚合的纯函数，回测/实盘/前端接口复用；
  既能吃 DecisionEvent 也能吃 asdict 后的 dict（回测 result 透传）。

注意：本模块在 main/core/engine（Python 3.13），不进 shared/，无 3.7 兼容约束。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


@dataclass
class DecisionEvent:
    """一条决策闸门触发记录。"""
    gate: str            # 闸门码，见各埋点（stop_loss/max_positions_full/insufficient_funds...）
    layer: str           # strategy_risk/portfolio_risk/signal_gate/capital_gate/t1/live_gate
    action: str          # trigger/halt/recover/strip/block/shrink/clamp/reject
    portfolio_id: int
    strategy_id: Optional[int] = None
    stock_code: Optional[str] = None
    bar_time: Optional[datetime] = None
    param_name: Optional[str] = None    # 关联调参名，如 max_positions/stop_loss_ratio
    param_value: Optional[float] = None  # 阈值（参数设定值）
    actual_value: Optional[float] = None  # 实际值（亏损%/回撤%/持仓数/可用资金…）
    requested_qty: Optional[int] = None
    final_qty: Optional[int] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DecisionRecorder:
    """run/session 级决策事件缓冲。"""

    def __init__(self) -> None:
        self._events: List[DecisionEvent] = []

    def record(self, **kwargs: Any) -> None:
        self._events.append(DecisionEvent(**kwargs))

    def drain(self) -> List[DecisionEvent]:
        """取出并清空缓冲。"""
        events = self._events
        self._events = []
        return events

    def __bool__(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._events)


class _NullRecorder:
    """空操作采集器：默认占位，record/drain 均无副作用。"""

    def record(self, *args: Any, **kwargs: Any) -> None:
        return None

    def drain(self) -> List[DecisionEvent]:
        return []

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0


# 单例：所有未显式注入 recorder 的对象共用
NULL_RECORDER = _NullRecorder()


def _get(ev: Union[DecisionEvent, Dict[str, Any]], key: str, default: Any = None) -> Any:
    if isinstance(ev, dict):
        return ev.get(key, default)
    return getattr(ev, key, default)


def summarize_decisions(
    events: Union[List[DecisionEvent], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """按 (gate, param_name) 聚合决策事件，供调参统计表直接消费。

    每组返回：gate/layer/action/param_name/param_value/count/first_bar_time/
    last_bar_time/stock_count/requested_qty_sum/final_qty_sum。
    param_value 取组内首个非空（同参数阈值一致）。结果按 count 降序、gate 升序排列，
    让「最常触发的闸门」排在最前。
    """
    groups: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for ev in events:
        gate = _get(ev, "gate")
        param_name = _get(ev, "param_name")
        key = (gate, param_name)
        if key not in groups:
            order.append(key)
            groups[key] = {
                "gate": gate,
                "layer": _get(ev, "layer"),
                "action": _get(ev, "action"),
                "param_name": param_name,
                "param_value": _get(ev, "param_value"),
                "count": 0,
                "first_bar_time": None,
                "last_bar_time": None,
                "_stocks": set(),
                "requested_qty_sum": 0,
                "final_qty_sum": 0,
                "_has_req": False,
                "_has_fin": False,
            }
        g = groups[key]
        g["count"] += 1
        if g["param_value"] is None:
            g["param_value"] = _get(ev, "param_value")
        bt = _get(ev, "bar_time")
        if bt is not None:
            if g["first_bar_time"] is None or bt < g["first_bar_time"]:
                g["first_bar_time"] = bt
            if g["last_bar_time"] is None or bt > g["last_bar_time"]:
                g["last_bar_time"] = bt
        code = _get(ev, "stock_code")
        if code is not None:
            g["_stocks"].add(code)
        rq = _get(ev, "requested_qty")
        if rq is not None:
            g["requested_qty_sum"] += rq
            g["_has_req"] = True
        fq = _get(ev, "final_qty")
        if fq is not None:
            g["final_qty_sum"] += fq
            g["_has_fin"] = True

    result: List[Dict[str, Any]] = []
    for key in order:
        g = groups[key]
        g["stock_count"] = len(g.pop("_stocks"))
        if not g.pop("_has_req"):
            g["requested_qty_sum"] = None
        if not g.pop("_has_fin"):
            g["final_qty_sum"] = None
        result.append(g)
    # 最常触发在前；同频按 gate 名字稳定排序
    result.sort(key=lambda s: (-s["count"], str(s["gate"]), str(s["param_name"] or "")))
    return result
