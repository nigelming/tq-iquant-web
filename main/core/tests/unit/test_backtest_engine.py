from datetime import datetime, date
from decimal import Decimal

import polars as pl

from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.execution_engine import SimulatedDispatcher
from core.engine.position import Position
from core.engine.event import BarEvent
from tq_iquant_shared.constants import SignalType, TradeType


def _klines(stock_code, rows):
    """构造 Mock klines：单股票单周期日线 polars DataFrame。

    rows: [(date, open, high, low, close, volume), ...]
    """
    df = pl.DataFrame({
        "datetime": [r[0] for r in rows],
        "Open": [r[1] for r in rows],
        "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows],
        "Close": [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    })
    return {stock_code: {"1d": df}}


def _portfolio_with_strategy(stop_loss=Decimal("0.05")):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=stop_loss,
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def test_run_minimal_buy_then_stop_loss():
    """3 根日线 bar：bar1 触发 BUY→bar2 open 成交→bar2 close 亏损触发 STOP_LOSS→bar3 open 成交 SELL。
    产出 2 笔 trades + 3 个 snapshots。"""
    stock = "000001.SZ"
    # bar1(7/29): open=10, close=10.2 → on_bar 触发 BUY(OPEN 信号)
    # bar2(7/30): open=10.2 成交 BUY；close=9.0 → 亏损 11.8% > 5% 触发 STOP_LOSS
    # bar3(7/31): open=9.0 成交 SELL（T+1 允许：7/31 > 7/30）
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()

    # signal_cache：bar1 触发 OPEN，bar2/bar3 无公式触发
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    # open 价表供 SimulatedDispatcher 成交（成交价取 price_index 的 Open 列）
    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    trades = result["trades"]
    snapshots = result["snapshots"]
    # 2 笔成交：BUY @10.2，SELL @9.0
    assert len(trades) == 2
    assert trades[0].trade_type == TradeType.BUY
    assert trades[0].price == Decimal("10.2")
    assert trades[1].trade_type == TradeType.SELL
    assert trades[1].price == Decimal("9.0")
    # 信号来源透传：BUY 来自公式 open_sig(OPEN)，SELL 来自风控 stop_loss(STOP_LOSS)
    assert trades[0].signal_name == "open_sig"
    assert trades[0].signal_type == SignalType.OPEN
    assert trades[1].signal_name == "stop_loss"
    assert trades[1].signal_type == SignalType.STOP_LOSS
    # 3 个日终快照
    assert len(snapshots) == 3
    # 末态：已清仓，cash = 100000 - 买入支出 + 卖出回款
    pos = ctx.positions[stock]
    assert pos.quantity == 0
    # 买入 1000 股 @10.2，金额 10200；卖出 1000 股 @9.0，金额 9000
    # cash = 100000 - (10200 + 买入费用) + (9000 - 卖出费用)
    assert port.account.cash < Decimal("100000")  # 亏损，现金减少


def test_run_collects_decision_events():
    """run 内建 DecisionRecorder 并注入组合/执行引擎：止损触发事件随 result['decisions'] 返回。

    复用 test_run_minimal_buy_then_stop_loss 的行情：bar2 close=9.0 亏损 11.8% > 5%
    → 记录一条 stop_loss(strategy_risk/trigger) 闸门事件，字段带策略/股票/阈值/实际。
    """
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    decisions = result["decisions"]
    stop = [d for d in decisions if d["gate"] == "stop_loss"]
    assert len(stop) == 1
    ev = stop[0]
    assert ev["layer"] == "strategy_risk" and ev["action"] == "trigger"
    assert ev["strategy_id"] == 1 and ev["stock_code"] == stock
    assert ev["param_name"] == "stop_loss_ratio" and ev["param_value"] == 0.05
    assert ev["actual_value"] is not None and ev["actual_value"] >= 0.05
    # run 结束后 recorder 已 drain 干净（结果事件即全量）
    assert isinstance(decisions, list) and len(decisions) >= 1


def test_run_no_signal_no_trade():
    """无信号触发 → 0 笔 trades，快照数 = bar 数。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
    }
    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    assert result["trades"] == []
    assert len(result["snapshots"]) == 2
    # 全程无交易，现金不变
    assert port.account.cash == Decimal("100000")



def _buy_trade(price, quantity, trade_time):
    """构造 BUY TradeEvent 用于预置持仓。"""
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


def test_run_breaker_triggers_on_drawdown_and_halts_next_bar_buy():
    """预置持仓后让组合回撤破 20% → 熔断触发；次日 ADD 加仓买单被剥（无 BUY trade）。

    构造：预置 2000 股 @40（市值 80000）+ 现金 20000 = 总值 100000（峰值）。
    bar1 close=30 → 市值 60000 + 现金 20000 = 80000 → 回撤 20% → update 触发熔断。
    bar1 同时触发 ADD 信号 → 生成 bar1 BUY 订单（下一 bar open 成交）。
    bar2 open=30 执行 bar1 的 BUY 订单（熔断前已生成，仍成交）。
    bar2 close=30 → update 已熔断；bar2 ADD 信号 → on_bar 因熔断剥 BUY（无新订单）。
    最终 bar2 无新增 BUY trade（熔断生效）。

    注：加仓信号用 ADD（非 OPEN）——OPEN 只开新仓，对已持仓本票直接忽略（见 portfolio._signal_to_order）。
    """
    stock = "000001.SZ"
    # 4 根 bar，价格从 40 跌到 30 触发回撤
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("40"), Decimal("40"), Decimal("40"), Decimal("40"), 1000),
        (datetime(2026, 7, 30), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 7, 31), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 8, 1), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
    ])
    port, ctx = _portfolio_with_strategy(stop_loss=Decimal("0.5"))  # 止损放宽避免抢跑
    # 加仓用 ADD（OPEN 只开新仓、对已持仓本票直接忽略）：放低加仓阈值/次数，
    # 使每根 bar 都对已持仓票发出 ADD 买单，以验证熔断后 BUY 被剥。
    ctx.add_position_threshold = Decimal("0")
    ctx.max_add_count = 10
    ctx.formula_signals = [
        {"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1},
    ]

    # 预置持仓 2000 股 @40，现金调整到 20000
    pos = ctx.positions.setdefault(stock, Position(stock))
    pos.apply_trade(_buy_trade("40", 2000, datetime(2026, 7, 28, 9, 30)))
    port.account.cash = Decimal("20000")  # 总值 80000+20000=100000

    # 每根 bar 都触发 ADD（对已持仓票加仓）
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "add_sig", "signal_type": SignalType.ADD, "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "add_sig", "signal_type": SignalType.ADD, "value": 1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "add_sig", "signal_type": SignalType.ADD, "value": 1}],
        (1, stock, datetime(2026, 8, 1)): [{"name": "add_sig", "signal_type": SignalType.ADD, "value": 1}],
    }

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    # bar1(7/29) 收盘价 40 → 峰值 100000，无回撤；ADD 生成 bar1 订单（bar2 open 成交 #1）。
    # bar2(7/30) 收盘价 30 → 总值 80000，回撤 20% → update 触发熔断；
    #   bar2 on_bar 的 ADD 订单在熔断前生成（update 在 on_bar 后）→ bar3 open 成交 #2。
    # bar3/bar4 on_bar：熔断已激活 → ADD BUY 被剥，无新订单 → 无第 3 笔成交。
    assert port.risk_manager.consecutive_drawdown_triggers >= 1
    assert port.risk_manager.circuit_breaker_active is True
    buy_trades = [t for t in result["trades"] if t.trade_type == TradeType.BUY]
    assert len(buy_trades) == 2  # 仅熔断生效前两笔；后续被剥


def test_daily_loss_halts_next_day_and_recovers_after():
    """daily_loss「当日暂停，次日恢复」在日线回测中正确落位（熔断 off-by-one 修复）。

    旧实现：update() 在 on_bar 之后跨日补算 daily_loss → 暂停日 B 首根 bar 的
    on_bar 未剥（日线周期整日失效），恢复日 C 首根 bar 却仍被剥。
    新实现：enter_day() 在 on_bar 之前补算 → 暂停日 B 整日剥 BUY、恢复日 C 不剥。

    场景：预置 2000 股 @40 + 现金 20000 = 总值 100000（7/30 收盘基准）。
    7/31 收盘跌到 30 → 总值 80000，日内 -20% ≥ 5% → 触发 daily_loss。
    8/1 暂停日（on_bar 剥 ADD）、8/2 恢复日（不再剥）。
    """
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 30), Decimal("40"), Decimal("40"), Decimal("40"), Decimal("40"), 1000),
        (datetime(2026, 7, 31), Decimal("40"), Decimal("40"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 8, 1), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 8, 2), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
    ])
    # max_drawdown 抬高到 50%，隔离 daily_loss（20% 回撤不触发熔断，避免干扰剥单归属）
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.5"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(strategy_id=1, period="1d", capital_ratio=Decimal("0.6"), max_positions=5)
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.2"), trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    ctx.add_position_threshold = Decimal("0")
    ctx.max_add_count = 10
    ctx.formula_signals = [
        {"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1},
    ]
    pos = ctx.positions.setdefault(stock, Position(stock))
    pos.apply_trade(_buy_trade("40", 2000, datetime(2026, 7, 29, 9, 30)))
    port.account.cash = Decimal("20000")  # 总值 80000+20000=100000

    cache = {
        (1, stock, t): [{"name": "add_sig", "signal_type": SignalType.ADD, "value": 1}]
        for t in (datetime(2026, 7, 30), datetime(2026, 7, 31),
                  datetime(2026, 8, 1), datetime(2026, 8, 2))
    }

    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    # 触发日在亏损日 7/31
    assert port.risk_manager.daily_loss_trigger_date == date(2026, 7, 31)
    # 剥单只发生在暂停日 8/1（旧实现：剥在恢复日 8/2、暂停日 8/1 漏剥）
    strips = [d for d in result["decisions"] if d["gate"] == "halted_buy_strip"]
    strip_dates = {d["bar_time"].date() for d in strips}
    assert strip_dates == {date(2026, 8, 1)}, strip_dates
    # 恢复事件在恢复日 8/2（暂停日后一个交易日）
    recovers = [d for d in result["decisions"] if d["gate"] == "risk_recover"]
    assert {d["bar_time"].date() for d in recovers} == {date(2026, 8, 2)}


def _portfolio_two_strategies():
    """两策略组合：s1 capital_ratio=0.6，s2=0.4，各自有公式信号。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.5"), daily_loss_limit=Decimal("0.5"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    for sid, ratio in [(1, Decimal("0.6")), (2, Decimal("0.4"))]:
        ctx = StrategyContext(
            strategy_id=sid, period="1d",
            capital_ratio=ratio, max_positions=5,
        )
        ctx.formula_signals = [
            {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
        ]
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal("0.5"),
            take_profit_ratio=Decimal("0.5"),
            trailing_stop_ratio=Decimal("0"),
        )
        port.strategies.append(ctx)
    return port


def test_strategy_snapshots_additivity():
    """两策略快照：每日 Σ(策略总净值) == 组合总净值（现金按 ratio 归一化分摊）。
    无交易时各策略市值=0，分摊现金之和 = 组合现金，故 Σ策略净值 = 组合现金 = 组合净值。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
    ])
    port = _portfolio_two_strategies()
    # 无信号 → 无交易，全程纯现金
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (2, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (2, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
    }

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    strategy_snaps = result["strategy_snapshots"]
    # 两个策略各 2 个快照
    assert set(strategy_snaps.keys()) == {1, 2}
    assert len(strategy_snaps[1]) == 2 and len(strategy_snaps[2]) == 2

    # 可加性：每日 Σ策略净值 == 组合净值
    portfolio_snaps = result["snapshots"]
    for i in range(2):
        s1 = strategy_snaps[1][i]["total_value"]
        s2 = strategy_snaps[2][i]["total_value"]
        p = portfolio_snaps[i]["total_value"]
        assert s1 + s2 == p  # 纯现金 + 市值0，精确相等


def test_strategy_evaluations_present():
    """有交易的策略 → strategy_evaluations 含该策略的评估（total_return 等非空）。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port = _portfolio_two_strategies()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
        (2, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (2, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (2, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    # s1 有交易（BUY@10.2），其评估应存在
    sev = result["strategy_evaluations"]
    assert 1 in sev
    assert sev[1].get("total_return") is not None
    # s2 无交易，快照不足或全现金 → 评估存在但指标可能为 None/0
    assert "total_trades" in sev.get(2, {}) or 2 not in sev


def test_benchmark_filled_into_snapshots_and_evaluated():
    """传 benchmark_data={date: Decimal} → 每个 portfolio 快照带 benchmark_value，
    且 evaluations.benchmark_return 反映基准涨跌（首末日 close 之比）。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    # 基准指数：1000 → 1100（+10%）
    benchmark_data = {
        datetime(2026, 7, 29).date(): Decimal("1000"),
        datetime(2026, 7, 30).date(): Decimal("1050"),
        datetime(2026, 7, 31).date(): Decimal("1100"),
    }

    engine = BacktestEngine()
    result = engine.run(
        port, klines=klines, signal_cache=cache,
        benchmark_data=benchmark_data,
    )

    # 每个组合快照都带 benchmark_value
    snaps = result["snapshots"]
    assert len(snaps) == 3
    assert [s["benchmark_value"] for s in snaps] == [
        Decimal("1000"), Decimal("1050"), Decimal("1100"),
    ]
    # benchmark_return = (1100-1000)/1000 = 0.1
    ev = result["evaluations"]
    assert ev["benchmark_return"] == Decimal("0.1000")


def test_no_benchmark_data_no_curve():
    """不传 benchmark_data → 快照 benchmark_value 全 None，benchmark_return 退化为 0。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
    }

    engine = BacktestEngine()
    result = engine.run(
        port, klines=klines, signal_cache=cache
    )

    snaps = result["snapshots"]
    assert all(s["benchmark_value"] is None for s in snaps)
    assert result["evaluations"]["benchmark_return"] == Decimal("0")



def test_strategy_snapshots_additivity_with_suspended_stock():
    """停牌/缺 bar 时，策略层市值必须与组合层同口径（缺席持仓沿用昨收而非按 0），
    保证 Σ策略市值 == 组合市值。回归 2026-08-25：旧 _strategy_snapshots 对不在
    bar.stocks 的持仓 continue 当 0，与已修复的 total_value 口径分叉。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)

    def _ctx(sid, code, qty, cost):
        c = StrategyContext(
            strategy_id=sid, period="1d",
            capital_ratio=Decimal("0.5"), max_positions=5,
        )
        c.formula_signals = []
        c.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal("0.2"), take_profit_ratio=Decimal("0.2"),
            trailing_stop_ratio=Decimal("0"),
        )
        p = Position(code)
        p.quantity = qty
        p.avg_cost = Decimal(str(cost))
        c.positions[code] = p
        return c

    # 策略1 持 A(1000@10)，策略2 持 B(5000@10)；现金 40000，总市值应恒为 100000
    port.strategies.append(_ctx(1, "000001.SZ", 1000, 10))
    port.strategies.append(_ctx(2, "600000.SH", 5000, 10))
    port.account.cash = Decimal("40000")

    # 先给全量 bar 建立两只票的价格快照
    full = BarEvent(stocks={
        "000001.SZ": {"open": Decimal("10"), "high": Decimal("10"),
                      "low": Decimal("10"), "close": Decimal("10"), "volume": 1},
        "600000.SH": {"open": Decimal("10"), "high": Decimal("10"),
                      "low": Decimal("10"), "close": Decimal("10"), "volume": 1},
    }, bar_time=datetime(2026, 8, 24))
    port.total_value(full)

    # 次日 B 停牌：bar 只含 A，B 缺席 → 应按昨收 10 估，策略2 市值不塌
    suspended = BarEvent(stocks={
        "000001.SZ": {"open": Decimal("10"), "high": Decimal("10"),
                      "low": Decimal("10"), "close": Decimal("10"), "volume": 1},
    }, bar_time=datetime(2026, 8, 25))

    snaps = BacktestEngine()._strategy_snapshots(port, suspended, date(2026, 8, 25))
    by_id = {s["target_id"]: s for s in snaps}
    # 两只各 10000 市值（A=1000×10，B=5000×10 沿用昨收）
    assert by_id[1]["market_value"] == Decimal("10000")
    assert by_id[2]["market_value"] == Decimal("50000")
    # 可加性：Σ策略持仓市值 == 组合层持仓市值（total_value - 现金）
    holdings_sum = sum((s["market_value"] for s in snaps), Decimal("0"))
    assert holdings_sum == port.total_value(suspended) - port.account.cash
    assert port.total_value(suspended) == Decimal("100000")


def test_run_first_day_signal_burst_respects_max_positions():
    """首日 10 只股票齐发 OPEN（回测 id7 实测场景）：max_positions=5 → 次日只成交 5 笔。

    修复前：闸门 held 只算已成交持仓（首日 0 只），10 单全排队全成交 → 持仓 10
    超上限，此后 OPEN 被 max_positions_full 永久拦截，策略只卖不买。
    """
    codes = ["%06d.SZ" % i for i in range(1, 11)]
    klines = {}
    for c in codes:
        klines.update(_klines(c, [
            (datetime(2026, 1, 4), Decimal("10"), Decimal("10.2"), Decimal("9.9"), Decimal("10"), 1000),
            (datetime(2026, 1, 5), Decimal("10"), Decimal("10.2"), Decimal("9.9"), Decimal("10"), 1000),
        ]))
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, c, datetime(2026, 1, 4)): [{"name": "open_sig", "value": 1}]
        for c in codes
    }
    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache)

    buys = [t for t in result["trades"] if t.trade_type == TradeType.BUY]
    assert len(buys) == 5  # max_positions=5，不超开
    assert len({t.stock_code for t in buys}) == 5


# ===== 未来函数修复测试 =====
# 日线信号 bar_time=00:00 在混合周期时间轴中排在当日分钟 bar 之前。
# 修复：OrderEvent 携带 signal_bar_date，日线信号须在 t.date() > signal_bar_date 的下一交易日成交。

def test_daily_signal_not_filled_same_day_in_mixed_timeline():
    """混合 1d+5m 时间轴：1d 信号 D 日 00:00 生成，D 日 09:30 不可成交，
    D+1 日 09:30 才成交。防止日线信号当日开盘成交（未来函数）。"""
    stock = "000001.SZ"
    # 1d bars：7/29 和 7/30（00:00）
    df_1d = pl.DataFrame({
        "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30)],
        "Open": [Decimal("10"), Decimal("11")],
        "High": [Decimal("10.3"), Decimal("11.2")],
        "Low": [Decimal("9.9"), Decimal("10.8")],
        "Close": [Decimal("10.2"), Decimal("11.0")],
        "Volume": [1000, 1000],
    })
    # 5m bars：7/29 09:30 和 7/30 09:30
    df_5m = pl.DataFrame({
        "datetime": [datetime(2026, 7, 29, 9, 30), datetime(2026, 7, 30, 9, 30)],
        "Open": [Decimal("10.1"), Decimal("11.0")],
        "High": [Decimal("10.5"), Decimal("11.3")],
        "Low": [Decimal("10.0"), Decimal("10.8")],
        "Close": [Decimal("10.3"), Decimal("11.1")],
        "Volume": [500, 500],
    })
    klines = {stock: {"1d": df_1d, "5m": df_5m}}

    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("1.0"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)

    # 1d 信号在 7/29 00:00 触发
    cache = {(1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}]}

    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    trades = result["trades"]
    # 有 1 笔 BUY，成交日 = 7/30（而非 7/29 当日开盘）
    assert len(trades) == 1
    assert trades[0].trade_type == TradeType.BUY
    assert trades[0].trade_time.date() == date(2026, 7, 30)
    assert trades[0].price == Decimal("11.0")  # 7/30 次日 open 价


def test_minute_signal_filled_next_bar_no_delay():
    """分钟级信号 signal_bar_date=None，下一 bar open 立即成交（现有行为不变）。

    3 根 5m bar：7/29 09:30（信号）→ 09:35（成交）→ 7/30 09:30（无信号，
    仅为 Evaluator 提供 2 个日终行避免 ZeroDivisionError）。
    """
    stock = "000001.SZ"
    df = pl.DataFrame({
        "datetime": [
            datetime(2026, 7, 29, 9, 30),
            datetime(2026, 7, 29, 9, 35),
            datetime(2026, 7, 30, 9, 30),
        ],
        "Open": [Decimal("10"), Decimal("10.2"), Decimal("10.4")],
        "High": [Decimal("10.3"), Decimal("10.5"), Decimal("10.6")],
        "Low": [Decimal("9.9"), Decimal("10.1"), Decimal("10.2")],
        "Close": [Decimal("10.2"), Decimal("10.3"), Decimal("10.5")],
        "Volume": [1000, 1000, 1000],
    })
    klines = {stock: {"5m": df}}

    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="5m",
        capital_ratio=Decimal("1.0"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)

    cache = {
        (1, stock, datetime(2026, 7, 29, 9, 30)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 29, 9, 35)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30, 9, 30)): [{"name": "open_sig", "value": -1}],
    }
    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    trades = result["trades"]
    assert len(trades) == 1
    assert trades[0].trade_type == TradeType.BUY
    # 09:30 信号 → 09:35 成交（同日，分钟信号无延迟）
    assert trades[0].trade_time == datetime(2026, 7, 29, 9, 35)


def test_daily_signal_order_carries_signal_bar_date():
    """日线策略的 _signal_to_order 生成的 OrderEvent 有 signal_bar_date。"""
    from core.engine.event import OrderEvent
    from core.engine.portfolio import _DAILY_PERIODS
    stock = "000001.SZ"
    bar = BarEvent(
        stocks={stock: {"open": Decimal("10"), "high": Decimal("10.3"),
                         "low": Decimal("9.9"), "close": Decimal("10.2"), "volume": 1000}},
        bar_time=datetime(2026, 7, 29),
    )
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("1.0"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    # 预置持仓使 signal 能走到 order（OPEN 需无持仓才能触发）
    from core.engine.event import SignalEvent
    sig = SignalEvent(
        strategy_id=1, stock_code=stock, signal_name="open_sig",
        signal_type=SignalType.OPEN, bar_time=bar.bar_time,
    )
    order = port._signal_to_order(ctx, sig, bar)
    assert order is not None
    assert order.signal_bar_date == date(2026, 7, 29)


def test_minute_signal_order_has_none_signal_bar_date():
    """分钟级策略的 OrderEvent.signal_bar_date 为 None。"""
    from core.engine.event import SignalEvent
    stock = "000001.SZ"
    bar = BarEvent(
        stocks={stock: {"open": Decimal("10"), "high": Decimal("10.3"),
                         "low": Decimal("9.9"), "close": Decimal("10.2"), "volume": 1000}},
        bar_time=datetime(2026, 7, 29, 9, 30),
    )
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="5m",
        capital_ratio=Decimal("1.0"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    sig = SignalEvent(
        strategy_id=1, stock_code=stock, signal_name="open_sig",
        signal_type=SignalType.OPEN, bar_time=bar.bar_time,
    )
    order = port._signal_to_order(ctx, sig, bar)
    assert order is not None
    assert order.signal_bar_date is None


# ===========================================================================
# #2 静默丢弃修复：待成交订单遇停牌/无行情不丢弃，延后至下一有价 bar 重试
# ===========================================================================
def _two_stock_klines(rows_a, rows_b):
    """双股票 1d klines：A 全程有行情，B 可缺行（模拟停牌）。"""
    def _df(rows):
        return pl.DataFrame({
            "datetime": [r[0] for r in rows],
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        })
    return {
        "000001.SZ": {"1d": _df(rows_a)},
        "600001.SH": {"1d": _df(rows_b)},
    }


_ROWS_A = [  # A 全程有行情（撑起 7/30、7/31 的 bar）
    (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
    (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("10.0"), Decimal("10.4"), 1000),
    (datetime(2026, 7, 31), Decimal("10.4"), Decimal("10.6"), Decimal("10.1"), Decimal("10.5"), 1000),
    (datetime(2026, 8, 1), Decimal("10.5"), Decimal("10.8"), Decimal("10.2"), Decimal("10.6"), 1000),
]
# B：7/30、7/31 停牌缺行，8/1 复牌。7/29 close=18.5（成本 20，亏 7.5% 触发止损）
_ROWS_B_SUSPENDED = [
    (datetime(2026, 7, 29), Decimal("20"), Decimal("20.5"), Decimal("18.3"), Decimal("18.5"), 1000),
    (datetime(2026, 8, 1), Decimal("21"), Decimal("21.5"), Decimal("20.8"), Decimal("21.0"), 1000),
]


def test_run_suspended_buy_carried_until_quoted():
    """#2 BUY 遇停牌不静默丢弃：延后至复牌 bar open 成交，no_market_data 仅首 bar 记 1 条。

    7/29 B 触发 OPEN → BUY 排队；7/30、7/31 bar 存在（A 有行情）但 B 无 open 价
    → 延后重试（首 bar 记 1 条 defer 决策事件）；8/1 复牌 @21 成交。
    """
    klines = _two_stock_klines(_ROWS_A, _ROWS_B_SUSPENDED)
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, "600001.SH", datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
    }

    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    trades = result["trades"]
    # 修复前：订单在 7/30 无价被静默丢弃，trades 为空
    assert len(trades) == 1
    assert trades[0].trade_type == TradeType.BUY
    assert trades[0].stock_code == "600001.SH"
    assert trades[0].price == Decimal("21")  # 8/1 复牌 open 成交
    assert trades[0].trade_time == datetime(2026, 8, 1)
    # 决策事件：仅首次延后记 1 条（两个停牌 bar 不刷屏）
    no_md = [d for d in result["decisions"] if d["gate"] == "no_market_data"]
    assert len(no_md) == 1
    assert no_md[0]["action"] == "defer"
    assert no_md[0]["stock_code"] == "600001.SH"


def test_run_suspended_sell_stop_loss_carried_until_quoted():
    """#2 止损 SELL 遇停牌不静默丢弃：延后至复牌 open 成交（止损不被遗忘）。

    预置 B 1000 股 @20；7/29 close=18.5 亏 7.5% > 5% 触发 STOP_LOSS → SELL 排队；
    7/30、7/31 B 停牌延后；8/1 复牌 @21 成交。
    """
    klines = _two_stock_klines(_ROWS_A, _ROWS_B_SUSPENDED)
    port, ctx = _portfolio_with_strategy()
    from core.engine.event import TradeEvent
    pos = Position("600001.SH")
    pos.apply_trade(TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="600001.SH",
        trade_type=TradeType.BUY, price=Decimal("20"), quantity=1000,
        amount=Decimal("20000"), commission=Decimal("0"), stamp_duty=Decimal("0"),
        trade_time=datetime(2026, 7, 28, 9, 30),
    ))
    ctx.positions["600001.SH"] = pos

    result = BacktestEngine().run(port, klines=klines, signal_cache={})

    trades = result["trades"]
    # 修复前：SELL 在 7/30 无价被静默丢弃，止损永远不执行
    assert len(trades) == 1
    assert trades[0].trade_type == TradeType.SELL
    assert trades[0].stock_code == "600001.SH"
    assert trades[0].price == Decimal("21")  # 8/1 复牌 open 成交
    no_md = [d for d in result["decisions"] if d["gate"] == "no_market_data"]
    assert len(no_md) == 1


# ===========================================================================
# 主从策略端到端回测：master 建仓 → slave 次 bar 跟进建仓 → master 清仓
# → slave 按 slave_exit_mode 跟随全平。走完整 BacktestEngine（下单次 bar open
# 成交 + 日线 signal_bar_date 延迟闸门 + T+1），验证 portfolio.py 主从编排在
# 真实多 bar 时序下闭环，而非单 bar 单测。
# ===========================================================================
_FLAT_ROWS = [  # 单股 5 根日线，价格全程平稳（避开止损/止盈/移动止损）
    (datetime(2026, 7, 29), Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000),
    (datetime(2026, 7, 30), Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000),
    (datetime(2026, 7, 31), Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000),
    (datetime(2026, 8, 1), Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000),
    (datetime(2026, 8, 4), Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000),
]


def _master_slave_portfolio(slave_exit_mode="follow_master"):
    """master(id=1) + slave(id=2, master=1) 组合。
    master 配 OPEN/CLOSE 公式；slave 只配 OPEN；两者风控阈值放宽避免干扰。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)

    master = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5, role="master",
    )
    master.formula_signals = [
        {"signal_name": "m_open", "signal_type": SignalType.OPEN, "trigger_value": 1},
        {"signal_name": "m_close", "signal_type": SignalType.CLOSE, "trigger_value": 1},
    ]
    master.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.5"),
        trailing_stop_ratio=Decimal("0"),
    )

    slave = StrategyContext(
        strategy_id=2, period="1d",
        capital_ratio=Decimal("0.4"), max_positions=5,
        role="slave", master_strategy_id=1, slave_exit_mode=slave_exit_mode,
    )
    slave.formula_signals = [
        {"signal_name": "s_open", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    slave.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.5"),
        trailing_stop_ratio=Decimal("0"),
    )

    port.strategies.extend([master, slave])
    return port, master, slave


def _master_slave_cache(stock):
    """信号时序：master 7/29 开仓、7/31 清仓；slave 7/29 与 7/30 各发一次 OPEN。
    7/29 slave OPEN 时 master 尚无持仓（被闸门拦）；7/30 master 已持仓 → 放行。"""
    return {
        (1, stock, datetime(2026, 7, 29)): [{"name": "m_open", "value": 1}],
        (2, stock, datetime(2026, 7, 29)): [{"name": "s_open", "value": 1}],
        (2, stock, datetime(2026, 7, 30)): [{"name": "s_open", "value": 1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "m_close", "value": 1}],
    }


def test_e2e_master_follow_buy_then_follow_sell():
    """follow_master 闭环（状态镜像语义）：5 根日线 bar。

    7/29 master OPEN + slave OPEN（master 未持仓→slave 被拦）
    7/30 master 买单成交（@10）；slave 再发 OPEN → 放行，排单
    7/31 slave 买单成交（@10）；master CLOSE → master 卖单（master 仍持仓→slave 不跟随）
    8/1  master 卖单成交（清仓）→ slave 检出 master 已清仓 → 追加跟随卖单
    8/4  slave 跟随卖成交 → 双方清仓
    期待 4 笔成交：master BUY / slave BUY / master SELL / slave follow SELL。
    """
    stock = "000001.SZ"
    port, master, slave = _master_slave_portfolio("follow_master")
    result = BacktestEngine().run(
        port, klines=_klines(stock, _FLAT_ROWS), signal_cache=_master_slave_cache(stock),
    )
    trades = result["trades"]

    assert len(trades) == 4
    m_buy, s_buy, m_sell, s_sell = trades
    # master 先建仓（7/30 成交），slave 次 bar 跟进（7/31 成交）
    assert (m_buy.strategy_id, m_buy.trade_type, m_buy.signal_name) == (1, TradeType.BUY, "m_open")
    assert (s_buy.strategy_id, s_buy.trade_type, s_buy.signal_name) == (2, TradeType.BUY, "s_open")
    assert m_buy.trade_time == datetime(2026, 7, 30)
    assert s_buy.trade_time == datetime(2026, 7, 31)
    assert s_buy.quantity == 400  # 0.1 × (0.4×100000) / 10
    assert m_buy.quantity == 600  # 0.1 × (0.6×100000) / 10
    # master 清仓（8/1 成交）→ slave 下一 bar 检出并全平（8/4 成交）
    assert (m_sell.strategy_id, m_sell.trade_type, m_sell.signal_name) == (1, TradeType.SELL, "m_close")
    assert m_sell.trade_time == datetime(2026, 8, 1)
    assert (s_sell.strategy_id, s_sell.trade_type) == (2, TradeType.SELL)
    assert s_sell.signal_name == "follow_master"
    assert s_sell.quantity == s_buy.quantity
    # slave 跟随发生在 master 清仓成交之后的下一 slave bar（晚一拍，非同期）
    assert s_sell.trade_time > m_sell.trade_time
    # 末态双方清仓
    assert master.positions[stock].quantity == 0
    assert slave.positions[stock].quantity == 0
    # 决策闸门：7/29 slave 被拦截 + follow_master 触发
    gates = {d["gate"] for d in result["decisions"]}
    assert "slave_master_block" in gates
    assert "follow_master_sell" in gates


def test_e2e_master_slave_independent_slave_keeps_position():
    """slave_exit_mode=independent：master 清仓 → slave 不跟随，保留存量持仓。

    同一信号/行情下只应成交 3 笔（master 买、slave 买、master 卖），
    slave 末态仍持有 400 股，且无 follow_master 决策事件。
    """
    stock = "000001.SZ"
    port, master, slave = _master_slave_portfolio("independent")
    result = BacktestEngine().run(
        port, klines=_klines(stock, _FLAT_ROWS), signal_cache=_master_slave_cache(stock),
    )
    trades = result["trades"]

    assert len(trades) == 3
    assert [t.signal_name for t in trades] == ["m_open", "s_open", "m_close"]
    assert master.positions[stock].quantity == 0
    assert slave.positions[stock].quantity == 400
    assert "follow_master_sell" not in {d["gate"] for d in result["decisions"]}
