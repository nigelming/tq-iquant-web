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

def test_intraday_bars_volatility_uses_daily_returns():
    """30m 等日内周期回测：一天多根 bar（同日多快照），收益统计按日聚合。

    日收益序列取「每日最后一根 bar 的净值」（日收盘），per-bar 收益不得直接
    当日记收益年化——否则 vol 低估 ~√每日bar数、Sharpe 虚高（回测"30"实测
    Sharpe 9.17 根因）。
    """
    import math
    # 4 个交易日 × 每日 2 根 bar：日内波动小（+100），日间波动大
    closes = [100000, 100100,   # day1 收盘 100100
              110000, 110100,   # day2 收盘 110100
              99000, 99100,     # day3 收盘 99100
              108900, 109000]   # day4 收盘 109000
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    snapshots = []
    for d, (a, b) in zip(dates, [closes[i:i + 2] for i in range(0, 8, 2)]):
        snapshots.append(_snap(a, d))
        snapshots.append(_snap(b, d))

    ev = Evaluator().evaluate(snapshots)

    # 日收益序列（每日收盘）：110100/100100-1, 99100/110100-1, 109000/99100-1
    daily = [110100 / 100100 - 1, 99100 / 110100 - 1, 109000 / 99100 - 1]
    mean = sum(daily) / 3
    var = sum((r - mean) ** 2 for r in daily) / 3
    expected_vol = math.sqrt(var * 252)
    assert ev["volatility"] == Decimal(str(expected_vol)).quantize(Decimal("0.0001"))

    # Sharpe 用同一日频口径：sharpe = (annual - rf) / vol
    total = 109000 / 100000 - 1
    years = 3 / 365.25
    annual = (1 + total) ** (1 / years) - 1
    expected_sharpe = (annual - 0.02) / expected_vol
    assert ev["sharpe_ratio"] == Decimal(str(expected_sharpe)).quantize(Decimal("0.0001"))


def test_intraday_bars_daily_returns_not_per_bar():
    """对照：若按 per-bar 收益计算，vol 会明显不同（此测固化按日聚合的口径）。"""
    # 20 个交易日，每日两根 bar：首根 100000、收盘 120000——per-bar 口径
    # 每 2 根就有一次 +20% 收益（vol > 0），按日口径每日收盘相等 → 日收益全 0
    snapshots = []
    for i in range(20):
        d = date(2024, 1, 2 + i)
        snapshots.append(_snap(100000, d))
        snapshots.append(_snap(120000, d))
    ev = Evaluator().evaluate(snapshots)
    assert ev["volatility"] == Decimal("0.0000")  # 每日收盘相等 → 日收益 0
