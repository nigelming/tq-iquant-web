from datetime import date, datetime
from decimal import Decimal

from core.engine.evaluator import Evaluator
from core.engine.event import TradeEvent
from tq_iquant_shared.constants import TradeType, SignalType


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


def _trade(stock_code, trade_type, price, quantity, trade_time):
    amount = Decimal(str(price)) * quantity
    return TradeEvent(
        strategy_id=1, portfolio_id=1,
        stock_code=stock_code,
        trade_type=trade_type,
        price=Decimal(str(price)),
        quantity=quantity,
        amount=amount,
        commission=Decimal("5"),
        stamp_duty=Decimal("0"),
        trade_time=trade_time,
    )


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


# ===========================================================================
# 交易指标（win_rate / profit_factor / total_trades / avg_holding_days）
# ===========================================================================
def test_trade_metrics_basic():
    """两次完整 round-trip：1 赢 + 1 输 → total_trades=4, win_rate=0.5, profit_factor>1。
    交易序列：
      BUY A @10×1000=10000, SELL A @12×1000=12000 (赢 +2000)
      BUY B @10×1000=10000, SELL B @8×1000=8000  (输 -2000)
    """
    snaps = [_snap(100000, date(2024, 1, i + 2)) for i in range(3)]
    trades = [
        _trade("A", TradeType.BUY, 10, 1000, datetime(2024, 1, 2, 10, 0)),
        _trade("A", TradeType.SELL, 12, 1000, datetime(2024, 1, 3, 10, 0)),
        _trade("B", TradeType.BUY, 10, 1000, datetime(2024, 1, 2, 11, 0)),
        _trade("B", TradeType.SELL, 8, 1000, datetime(2024, 1, 3, 11, 0)),
    ]
    ev = Evaluator()
    result = ev.evaluate(snaps, trades=trades)

    assert result["total_trades"] == 4
    # win_rate 基于已平仓 round-trip：2 次卖出 → 1 赢 1 输 → 50%
    assert result["win_rate"] == Decimal("0.5")
    # profit_factor: 赢 2000 / 输 2000 = 1
    assert result["profit_factor"] == Decimal("1")
    # avg_holding_days: 1 天 → 1.0
    assert result["avg_holding_days"] == Decimal("1")


def test_trade_metrics_no_trades():
    """没有 trades → total_trades=0，win_rate/profit_factor 为 None（非除零）。"""
    snaps = [_snap(100000, date(2024, 1, i + 2)) for i in range(3)]
    ev = Evaluator()
    result = ev.evaluate(snaps, trades=[])
    assert result["total_trades"] == 0
    assert result["win_rate"] is None
    assert result["profit_factor"] is None
    assert result["avg_holding_days"] is None


def test_trade_metrics_partial_close():
    """部分平仓：BUY 1000 股 → SELL 500 股（赢）+ SELL 500 股（输）。
    两次卖出 P&L：500*(12-10)=+1000，500*(8-10)=-1000。
    """
    snaps = [_snap(100000, date(2024, 1, i + 2)) for i in range(3)]
    trades = [
        _trade("A", TradeType.BUY, 10, 1000, datetime(2024, 1, 2, 10, 0)),
        _trade("A", TradeType.SELL, 12, 500, datetime(2024, 1, 3, 10, 0)),
        _trade("A", TradeType.SELL, 8, 500, datetime(2024, 1, 4, 10, 0)),
    ]
    ev = Evaluator()
    result = ev.evaluate(snaps, trades=trades)
    assert result["total_trades"] == 3
    # 2 次卖出：1 赢 1 输 → 50%
    assert result["win_rate"] == Decimal("0.5")
    assert result["avg_holding_days"] is not None


def test_benchmark_return():
    """benchmark_data 提供基准净值序列 → 计算 benchmark_return。"""
    snaps = [_snap(100000, date(2024, 1, i + 2)) for i in range(3)]
    benchmark_data = [
        {"date": date(2024, 1, 2), "value": Decimal("1000")},
        {"date": date(2024, 1, 3), "value": Decimal("1050")},
        {"date": date(2024, 1, 4), "value": Decimal("1100")},
    ]
    ev = Evaluator()
    result = ev.evaluate(snaps, benchmark_data=benchmark_data)
    assert result["benchmark_return"] == Decimal("0.1")  # (1100-1000)/1000


def test_recovery_days_from_snapshots():
    """快照回撤到恢复 → 计算 avg_recovery_days 和 max_recovery_days。
    序列：100→120→90→110→85→105
    回撤 1: peak=120 at day 1 → recover=110 at day 3 (恢复未到峰值) → 未完全恢复，不算
    回撤 2: peak=120 at day 1 → 到 105 也超过 90 但不及 120
    简化：peak=120, trough=90, 后一天 110 > 90 但 < 120, 两天后?
    实际：- 回撤 peak=120→ trough=90, 恢复 back above 120 未发生 → 未恢复
          但 85 新低, 105 也未恢复
    修正数据：100→120→90→130→85→140（清晰两次回撤+恢复）
    """
    snaps = [
        _snap(100000, date(2024, 1, 1)),
        _snap(120000, date(2024, 1, 2)),  # peak=120
        _snap(90000, date(2024, 1, 3)),   # dd 25%
        _snap(130000, date(2024, 1, 4)),  # 恢复！距 peak 日 2 天
        _snap(85000, date(2024, 1, 5)),   # new peak=130 → dd to 85
        _snap(140000, date(2024, 1, 6)),  # 恢复！距上个 peak 日 2 天
    ]
    ev = Evaluator()
    result = ev.evaluate(snaps)
    # 两次恢复：各 2 天 = 平均 2 天
    assert result["avg_recovery_days"] == Decimal("2.0000")
    assert result["max_recovery_days"] == 2

    # 后向兼容：不传 trades 时仍然可用