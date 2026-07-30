from datetime import datetime
from decimal import Decimal

from core.engine.strategy_context import StrategyContext
from core.engine.event import BarEvent
from tq_iquant_shared.constants import SignalType


def _bar(stock_code, bar_time):
    return BarEvent(
        stocks={stock_code: {
            "open": Decimal("10"), "high": Decimal("11"),
            "low": Decimal("9"), "close": Decimal("10.5"), "volume": 1000,
        }},
        bar_time=bar_time,
    )


def _ctx(formula_signals=None):
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = formula_signals or []
    return ctx


def test_get_signal_cache_hit_returns_prefilled():
    """cache 命中 → 直接返回预填信号，不调 TQ。"""
    ctx = _ctx([{"signal_name": "buy_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}])
    bar = _bar("000001.SZ", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "buy_sig", "value": 1}]}
    signals = ctx.get_signal(bar, signal_cache=cache)
    assert len(signals) == 1
    assert signals[0].signal_name == "buy_sig"
    assert signals[0].signal_type == SignalType.OPEN
    assert signals[0].stock_code == "000001.SZ"
    assert signals[0].strategy_id == 1


def test_get_signal_cache_miss_calls_tq_formula():
    """cache miss → 调 TQFormula.compute（注入 mock），结果填入 cache。"""
    ctx = _ctx([{"signal_name": "sell_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1}])
    bar = _bar("000001.SZ", datetime(2026, 7, 30, 15, 0))
    cache = {}
    calls = []

    def mock_compute(stock_code, period, bar):
        calls.append((stock_code, period))
        return [{"name": "sell_sig", "value": -1}]

    signals = ctx.get_signal(bar, signal_cache=cache, tq_compute=mock_compute)
    assert len(signals) == 1
    assert signals[0].signal_type == SignalType.CLOSE
    assert calls == [("000001.SZ", "1d")]
    # 结果应填入 cache
    assert (1, "000001.SZ", bar.bar_time) in cache


def test_get_signal_trigger_value_mismatch_no_signal():
    """公式输出值 ≠ trigger_value → 不触发信号。"""
    ctx = _ctx([{"signal_name": "buy_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}])
    bar = _bar("000001.SZ", datetime(2026, 7, 30, 15, 0))
    # 公式输出 -1，但 trigger_value=1 → 不匹配
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "buy_sig", "value": -1}]}
    signals = ctx.get_signal(bar, signal_cache=cache)
    assert signals == []


def test_get_signal_no_formula_output_no_signal():
    """公式未输出该 signal_name → 无信号。"""
    ctx = _ctx([{"signal_name": "buy_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}])
    bar = _bar("000001.SZ", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "other_sig", "value": 1}]}
    signals = ctx.get_signal(bar, signal_cache=cache)
    assert signals == []


def test_get_signal_multiple_stocks_multiple_signals():
    """多股票 bar → 各股票分别查 cache 产出信号。"""
    ctx = _ctx([{"signal_name": "buy_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}])
    bar = BarEvent(
        stocks={
            "000001.SZ": {"open": Decimal("10"), "high": Decimal("11"), "low": Decimal("9"), "close": Decimal("10"), "volume": 100},
            "600000.SH": {"open": Decimal("20"), "high": Decimal("21"), "low": Decimal("19"), "close": Decimal("20"), "volume": 200},
        },
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    cache = {
        (1, "000001.SZ", bar.bar_time): [{"name": "buy_sig", "value": 1}],
        (1, "600000.SH", bar.bar_time): [{"name": "buy_sig", "value": 1}],
    }
    signals = ctx.get_signal(bar, signal_cache=cache)
    assert len(signals) == 2
    codes = {s.stock_code for s in signals}
    assert codes == {"000001.SZ", "600000.SH"}
