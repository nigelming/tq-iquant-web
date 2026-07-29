from datetime import date
from decimal import Decimal

from core.engine.evaluator import Evaluator


def _snap(total_value, snap_date=date(2024, 1, 2)):
    return {
        "target_type": "portfolio",
        "target_id": 1,
        "snap_date": snap_date,
        "total_value": Decimal(str(total_value)),
        "cash": Decimal("0"),
        "market_value": Decimal(str(total_value)),
        "daily_return": None,
        "cumulative_return": None,
        "benchmark_value": Decimal("100000"),
    }


def test_total_return():
    ev = Evaluator()
    result = ev.evaluate([
        _snap(100000, date(2024, 1, 2)),
        _snap(110000, date(2024, 1, 3)),
    ])
    assert result["total_return"] == Decimal("0.1")


def test_max_drawdown():
    ev = Evaluator()
    result = ev.evaluate([
        _snap(100000, date(2024, 1, 2)),
        _snap(120000, date(2024, 1, 3)),
        _snap(90000, date(2024, 1, 4)),
        _snap(110000, date(2024, 1, 5)),
    ])
    dd = result["max_drawdown"]
    assert dd is not None
    assert dd > 0


def test_var_95():
    snapshots = [_snap(100000 + i * 1000, date(2024, 1, i + 2)) for i in range(30)]
    ev = Evaluator()
    result = ev.evaluate(snapshots)
    assert result.get("var_95") is not None


def test_insufficient_data():
    snapshots = [_snap(100000, date(2024, 1, i + 2)) for i in range(5)]
    ev = Evaluator()
    result = ev.evaluate(snapshots)
    assert result["var_95"] is None
