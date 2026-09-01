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
    # open 价表供 SimulatedDispatcher 成交
    open_prices = {
        stock: {
            datetime(2026, 7, 30): Decimal("10.2"),  # bar2 open 成交 BUY
            datetime(2026, 7, 31): Decimal("9.0"),   # bar3 open 成交 SELL
        }
    }

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

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
    open_prices = {
        stock: {
            datetime(2026, 7, 30): Decimal("10.2"),
            datetime(2026, 7, 31): Decimal("9.0"),
        }
    }

    result = BacktestEngine().run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

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
    open_prices = {stock: {datetime(2026, 7, 30): Decimal("10.2")}}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

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
    open_prices = {stock: {
        datetime(2026, 7, 30): Decimal("30"),
        datetime(2026, 7, 31): Decimal("30"),
        datetime(2026, 8, 1): Decimal("30"),
    }}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    # bar1(7/29) 收盘价 40 → 峰值 100000，无回撤；ADD 生成 bar1 订单（bar2 open 成交 #1）。
    # bar2(7/30) 收盘价 30 → 总值 80000，回撤 20% → update 触发熔断；
    #   bar2 on_bar 的 ADD 订单在熔断前生成（update 在 on_bar 后）→ bar3 open 成交 #2。
    # bar3/bar4 on_bar：熔断已激活 → ADD BUY 被剥，无新订单 → 无第 3 笔成交。
    assert port.risk_manager.consecutive_drawdown_triggers >= 1
    assert port.risk_manager.circuit_breaker_active is True
    buy_trades = [t for t in result["trades"] if t.trade_type == TradeType.BUY]
    assert len(buy_trades) == 2  # 仅熔断生效前两笔；后续被剥


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
    result = engine.run(port, klines=klines, signal_cache=cache,
                        open_prices={stock: {}})

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
    open_prices = {stock: {datetime(2026, 7, 30): Decimal("10.2")}}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

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
        open_prices={stock: {}}, benchmark_data=benchmark_data,
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
        port, klines=klines, signal_cache=cache, open_prices={stock: {}}
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
