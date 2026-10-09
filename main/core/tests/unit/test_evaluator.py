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


# ===========================================================================
# #6: 分钟回测一天多 bar 的口径一致性 — 路径指标按日聚合序列算。
# max_drawdown/ulcer/return_stability/recovery_days 旧实现直接用原始快照序列
# （含日内多条），与 sharpe/sortino（已按日聚合）口径不一致：日内假回撤会被
# 当真实回撤计入，恢复天数会被同日"假恢复"算成 0 天。
# ===========================================================================
def test_intraday_bars_max_drawdown_uses_daily_close():
    """#6: max_drawdown/ulcer/return_stability 按日聚合序列（每日最后一根 bar）
    算，不被日内中间 bar 的假回撤拉高。

    场景：day1 日内跌到 80000 又收回 100000（日内假回撤 20%），日收盘 100000；
          day2/day3 收盘 110000/120000。日聚合序列 [100000, 110000, 120000]
          线性上涨 → 无回撤、r2=1。
    原始快照序列含 80000 → max_drawdown=20%、r2<1（错）。
    """
    snaps = [
        _snap(100000, date(2024, 1, 2)),
        _snap(80000, date(2024, 1, 2)),   # 日内假回撤
        _snap(100000, date(2024, 1, 2)),  # day1 收盘
        _snap(110000, date(2024, 1, 3)),  # day2 收盘
        _snap(120000, date(2024, 1, 4)),  # day3 收盘
    ]
    ev = Evaluator().evaluate(snaps)
    # 日聚合序列 [100000, 110000, 120000] → 线性上涨无回撤
    assert ev["max_drawdown"] == Decimal("0")
    # ulcer = sqrt(Σ dd² / n)，日聚合无回撤 → 0
    assert ev["ulcer_index"] == Decimal("0.0000")
    # r2：日聚合 3 点完全线性 → 1
    assert ev["return_stability"] == Decimal("1.0000")


# ===========================================================================
# 基准相对指标（beta / Jensen α / 年化超额 / 跟踪误差 / IR / 捕获率 / 超额回撤）
#
# 数据源是快照自身的 snap_date + benchmark_value（每日收盘对齐），不是
# evaluate(benchmark_data=...) 那个只有 value 的简序列——后者无日期无法对齐。
# ===========================================================================
REL_KEYS = (
    "beta", "jensen_alpha", "excess_annual_return", "tracking_error",
    "information_ratio", "up_capture_ratio", "down_capture_ratio",
    "excess_max_drawdown",
)


def _rel_snap(strategy_value, bench_value, d):
    s = _snap(strategy_value, d)
    s["benchmark_value"] = (
        Decimal(str(bench_value)) if bench_value is not None else None
    )
    return s


def _linear_series(n_days, rb_seq, alpha=0.0003, beta_v=0.5):
    """构造 n_days 天快照：rb 按 rb_seq 循环，rs = beta_v*rb + alpha（日频线性）。"""
    snaps = []
    vs, vb = 100000.0, 1000.0
    from datetime import timedelta
    for i in range(n_days):
        snaps.append(_rel_snap(vs, vb, date(2024, 1, 1) + timedelta(days=i)))
        rb = rb_seq[i % len(rb_seq)]
        rs = beta_v * rb + alpha
        vs *= 1 + rs
        vb *= 1 + rb
    return snaps


def test_relative_metrics_linear_series():
    """rs = 0.5*rb + 0.0003（25 日，rb 循环 +1%/-1%/+2%/-2%，均值 0）。

    beta 精确 0.5；年化超额 = 0.0003*252 = 0.0756；
    上行捕获 = (0.5*1.5%+0.03%)/1.5% = 0.52；下行捕获 = 0.48；
    TE/IR/超额回撤有限值；Jensen α > 0。
    """
    snaps = _linear_series(25, [0.01, -0.01, 0.02, -0.02])
    result = Evaluator().evaluate(snaps)

    assert result["beta"] == Decimal("0.5000")
    assert result["jensen_alpha"] is not None and result["jensen_alpha"] > 0
    assert result["excess_annual_return"] == Decimal("0.0756")

    # TE = 0.5 * std_pop(rb) * sqrt(252)，std_pop(rb)=sqrt(0.00025)
    import math
    expected_te = 0.5 * math.sqrt(0.00025) * math.sqrt(252)
    assert result["tracking_error"] == Decimal(str(expected_te)).quantize(Decimal("0.0001"))
    expected_ir = Decimal("0.0756") / result["tracking_error"]
    assert result["information_ratio"] == expected_ir.quantize(Decimal("0.0001"))

    assert result["up_capture_ratio"] == Decimal("0.5200")
    assert result["down_capture_ratio"] == Decimal("0.4800")

    # rb=-2% 日 active return = -0.97% → 超额曲线有回撤
    assert result["excess_max_drawdown"] is not None
    assert result["excess_max_drawdown"] > 0


def test_relative_metrics_short_sample_is_none():
    """对齐日收益 < 20 → 8 个相对指标全部 None（样本不足不出数）。"""
    snaps = _linear_series(15, [0.01, -0.01, 0.02, -0.02])  # 14 个收益对
    result = Evaluator().evaluate(snaps)
    for k in REL_KEYS:
        assert result[k] is None, k


def test_relative_metrics_without_benchmark_values_is_none():
    """快照无 benchmark_value（如策略层评估）→ 8 个相对指标全部 None。"""
    snaps = [_snap(100000 + i * 100, date(2024, 1, 2 + i)) for i in range(25)]
    # _snap 默认塞了 100000，这里显式抹掉，模拟策略层快照
    for s in snaps:
        s.pop("benchmark_value", None)
    result = Evaluator().evaluate(snaps)
    for k in REL_KEYS:
        assert result[k] is None, k


def test_relative_metrics_aligns_by_date():
    """基准前 5 日缺失、之后 25 日有效 → 在有效对齐窗口（≥20 收益对）出数。"""
    from datetime import timedelta
    snaps = []
    vs, vb = 100000.0, 1000.0
    rb_seq = [0.01, -0.01, 0.02, -0.02]
    for i in range(30):
        bv = None if i < 5 else vb
        snaps.append(_rel_snap(vs, bv, date(2024, 1, 1) + timedelta(days=i)))
        if i >= 5:
            rb = rb_seq[(i - 5) % 4]
            vb *= 1 + rb
            vs *= 1 + 0.5 * rb + 0.0003
    result = Evaluator().evaluate(snaps)
    assert result["beta"] == Decimal("0.5000")
    assert result["up_capture_ratio"] == Decimal("0.5200")


def test_relative_metrics_flat_benchmark_guards():
    """基准零波动（rb=0）：beta/Jensen/捕获率/IR 退化为 None，超额仍可算。

    var(rb)=0 → beta None → Jensen α None；无上涨/下跌日 → 捕获率 None；
    active 曲线单调向上 → TE=0 → IR None、超额回撤 0；年化超额 = 0.001*252。
    """
    snaps = _linear_series(25, [0.0], alpha=0.001, beta_v=0.5)
    result = Evaluator().evaluate(snaps)
    assert result["beta"] is None
    assert result["jensen_alpha"] is None
    assert result["up_capture_ratio"] is None
    assert result["down_capture_ratio"] is None
    assert result["information_ratio"] is None
    assert result["tracking_error"] == Decimal("0.0000")
    assert result["excess_annual_return"] == Decimal("0.2520")
    assert result["excess_max_drawdown"] == Decimal("0.0000")


# ===========================================================================
# 交易磨损 / 仓位利用 / 持仓结构 / 路径痛苦度指标（FR-2 / FR-3）
# ===========================================================================
def _mv_snap(total, market_value, d):
    s = _snap(total, d)
    s["market_value"] = Decimal(str(market_value))
    s["cash"] = Decimal(str(Decimal(str(total)) - Decimal(str(market_value))))
    return s


def _pos_snap(total, holdings, d):
    """holdings: [(code, market_value), ...]，挂在快照 "positions" 键（Task 3 口径）。"""
    s = _snap(total, d)
    s["positions"] = [
        {"stock_code": code, "market_value": float(mv)} for code, mv in holdings
    ]
    return s


def test_cost_and_turnover_metrics():
    """双边成交额 20000、费用 5+5+10=20：年化换手与成本占初始资金比。"""
    from datetime import timedelta
    snaps = [
        _snap(100000, date(2024, 1, 1) + timedelta(days=i)) for i in range(25)
    ]
    trades = [
        _trade("A", TradeType.BUY, 10, 1000, datetime(2024, 1, 2, 10, 0)),
        _trade("A", TradeType.SELL, 10, 1000, datetime(2024, 1, 3, 10, 0)),
    ]
    trades[1].stamp_duty = Decimal("10")  # 卖出印花税 10
    result = Evaluator().evaluate(snaps, trades=trades)

    years = 24 / 365.25
    expected_turnover = (Decimal("20000") / Decimal("100000")) / Decimal(str(years))
    assert result["annual_turnover"] == expected_turnover.quantize(Decimal("0.0001"))
    assert result["total_cost_ratio"] == Decimal("0.000200")  # 20/100000


def test_cost_metrics_no_trades_are_zero():
    snaps = [_snap(100000, date(2024, 1, i + 2)) for i in range(25)]
    result = Evaluator().evaluate(snaps, trades=[])
    assert result["annual_turnover"] == Decimal("0.0000")
    assert result["total_cost_ratio"] == Decimal("0.000000")


def test_position_utilization_and_empty_days():
    """10 天满仓 + 15 天空仓 → 利用率 0.4，空仓占比 0.6。"""
    snaps = []
    for i in range(25):
        invested = i < 10
        snaps.append(_mv_snap(100000, 100000 if invested else 0, date(2024, 1, i + 1)))
    result = Evaluator().evaluate(snaps)
    assert result["avg_position_utilization"] == Decimal("0.4000")
    assert result["empty_days_ratio"] == Decimal("0.6000")


def test_loss_streak_and_worst_returns():
    """2024-01-01(周一)~05 净值 100000×4→92000（周内 -8%），01-08(周一) 90000。

    日收益：0,0,0,-8%,-2.17% → 最长连亏 2、最差日 -8%；
    周：W1=92000/100000-1=-8%，W2 单日=0 → 最差周 -8%。
    """
    snaps = [
        _snap(100000, date(2024, 1, 1)),
        _snap(100000, date(2024, 1, 2)),
        _snap(100000, date(2024, 1, 3)),
        _snap(100000, date(2024, 1, 4)),
        _snap(92000, date(2024, 1, 5)),
        _snap(90000, date(2024, 1, 8)),
    ]
    result = Evaluator().evaluate(snaps)
    assert result["max_consecutive_loss_days"] == 2
    assert result["max_consecutive_profit_days"] == 3
    assert result["worst_daily_return"] == Decimal("-0.0800")
    assert result["worst_weekly_return"] == Decimal("-0.0800")
    # 收益样本 < 20 → 偏度峰度为 None
    assert result["return_skewness"] is None
    assert result["return_kurtosis"] is None


def test_skewness_kurtosis_symmetric():
    """±1% 对称两分布（24 个日收益）：偏度 0，超额峰度 -2（两点分布）。"""
    snaps = []
    v = 100000.0
    from datetime import timedelta
    for i in range(25):
        snaps.append(_snap(v, date(2024, 1, 1) + timedelta(days=i)))
        v *= 1 + (-0.01 if i % 2 == 0 else 0.01)
    result = Evaluator().evaluate(snaps)
    assert abs(float(result["return_skewness"])) < 1e-6
    assert abs(float(result["return_kurtosis"]) + 2) < 1e-6


def test_position_structure_from_positions_payload():
    """奇数日两股 60/40（HHI .52, max .6），偶数日一股 100%（HHI 1, max 1）。"""
    from datetime import timedelta
    snaps = []
    for i in range(24):
        d = date(2024, 1, 1) + timedelta(days=i)
        if i % 2 == 0:
            snaps.append(_pos_snap(100000, [("A", 60000), ("B", 40000)], d))
        else:
            snaps.append(_pos_snap(100000, [("A", 100000)], d))
    result = Evaluator().evaluate(snaps)
    assert result["max_single_weight"] == Decimal("1.0000")
    assert result["hhi"] == Decimal("0.7600")  # (0.52+1)/2


def test_position_structure_none_without_payload():
    """老快照无 positions 负载 → 最大单票/HHI 为 None（不猜测、不报错）。"""
    snaps = [_snap(100000, date(2024, 1, i + 1)) for i in range(25)]
    result = Evaluator().evaluate(snaps)
    assert result["max_single_weight"] is None
    assert result["hhi"] is None
