"""DecisionRecorder / DecisionEvent / summarize_decisions 单测。

调参可观测性：信号→成交链路上每个闸门触发/拦截/缩量都记一条 DecisionEvent，
回测实盘共用。本测只覆盖采集器本身与聚合纯函数，闸门埋点在各自模块测。
"""
from datetime import datetime

from core.engine.decision import (
    DecisionRecorder,
    NULL_RECORDER,
    DecisionEvent,
    summarize_decisions,
)


def _bar(h, m=0):
    return datetime(2026, 8, 28, h, m)


class TestDecisionRecorder:
    def test_record_appends_event(self):
        r = DecisionRecorder()
        r.record(gate="max_positions_full", layer="signal_gate", action="block",
                 portfolio_id=1, strategy_id=2, stock_code="000001.SZ",
                 bar_time=_bar(10), param_name="max_positions", param_value=5.0,
                 actual_value=5.0, message="已满")
        evs = r.drain()
        assert len(evs) == 1
        ev = evs[0]
        assert isinstance(ev, DecisionEvent)
        assert ev.gate == "max_positions_full"
        assert ev.param_name == "max_positions"
        assert ev.param_value == 5.0
        assert ev.actual_value == 5.0
        assert ev.stock_code == "000001.SZ"

    def test_drain_clears_events(self):
        r = DecisionRecorder()
        r.record(gate="stop_loss", layer="strategy_risk", action="trigger", portfolio_id=1)
        assert len(r.drain()) == 1
        # 第二次 drain 为空（已清空）
        assert r.drain() == []

    def test_recorder_is_truthy(self):
        assert bool(DecisionRecorder()) is True

    def test_default_fields_optional(self):
        r = DecisionRecorder()
        r.record(gate="x", layer="y", action="block", portfolio_id=1)
        ev = r.drain()[0]
        assert ev.strategy_id is None
        assert ev.stock_code is None
        assert ev.bar_time is None
        assert ev.param_name is None
        assert ev.param_value is None
        assert ev.actual_value is None
        assert ev.requested_qty is None
        assert ev.final_qty is None
        assert ev.message is None


class TestNullRecorder:
    def test_record_is_noop(self):
        # 不应抛异常，也不积累任何事件
        NULL_RECORDER.record(gate="x", layer="y", action="block", portfolio_id=1)
        assert NULL_RECORDER.drain() == []

    def test_null_is_falsy(self):
        assert bool(NULL_RECORDER) is False


class TestSummarize:
    def _events(self):
        return [
            DecisionEvent(gate="max_positions_full", layer="signal_gate", action="block",
                          portfolio_id=1, strategy_id=2, stock_code="000001.SZ",
                          bar_time=_bar(10), param_name="max_positions",
                          param_value=5.0, actual_value=5.0),
            DecisionEvent(gate="max_positions_full", layer="signal_gate", action="block",
                          portfolio_id=1, strategy_id=2, stock_code="600000.SH",
                          bar_time=_bar(11), param_name="max_positions",
                          param_value=5.0, actual_value=5.0),
            DecisionEvent(gate="insufficient_funds", layer="capital_gate", action="reject",
                          portfolio_id=1, strategy_id=3, stock_code="000002.SZ",
                          bar_time=_bar(10, 30), param_name="cash",
                          param_value=None, actual_value=1000.0,
                          requested_qty=1000, final_qty=0),
            # 同 gate 不同 param_name → 分两组
            DecisionEvent(gate="order_shrunk", layer="capital_gate", action="shrink",
                          portfolio_id=1, strategy_id=3, stock_code="000003.SZ",
                          bar_time=_bar(13), param_name="capital_ratio",
                          requested_qty=2000, final_qty=1000),
        ]

    def test_groups_by_gate_and_param(self):
        summary = summarize_decisions(self._events())
        # 三组：max_positions_full/max_positions、insufficient_funds/cash、order_shrunk/capital_ratio
        keys = {(s["gate"], s["param_name"]) for s in summary}
        assert keys == {
            ("max_positions_full", "max_positions"),
            ("insufficient_funds", "cash"),
            ("order_shrunk", "capital_ratio"),
        }

    def test_count_and_time_range_and_stocks(self):
        summary = summarize_decisions(self._events())
        mp = next(s for s in summary if s["gate"] == "max_positions_full")
        assert mp["count"] == 2
        assert mp["stock_count"] == 2
        assert mp["first_bar_time"] == _bar(10)
        assert mp["last_bar_time"] == _bar(11)
        assert mp["layer"] == "signal_gate"
        assert mp["action"] == "block"
        assert mp["param_value"] == 5.0

    def test_qty_sums(self):
        summary = summarize_decisions(self._events())
        ins = next(s for s in summary if s["gate"] == "insufficient_funds")
        assert ins["requested_qty_sum"] == 1000
        assert ins["final_qty_sum"] == 0
        shr = next(s for s in summary if s["gate"] == "order_shrunk")
        assert shr["requested_qty_sum"] == 2000
        assert shr["final_qty_sum"] == 1000

    def test_empty(self):
        assert summarize_decisions([]) == []

    def test_accepts_dicts(self):
        # 回测 result["decisions"] 是 dict 列表（asdict），聚合也应能吃 dict
        evs = [{
            "gate": "stop_loss", "layer": "strategy_risk", "action": "trigger",
            "portfolio_id": 1, "strategy_id": 2, "stock_code": "000001.SZ",
            "bar_time": _bar(10), "param_name": "stop_loss_ratio",
            "param_value": 0.05, "actual_value": 0.06,
            "requested_qty": None, "final_qty": None,
        }]
        summary = summarize_decisions(evs)
        assert len(summary) == 1
        assert summary[0]["count"] == 1
        assert summary[0]["gate"] == "stop_loss"
