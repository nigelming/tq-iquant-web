"""决策闸门埋点单测（调参可观测性）。

信号→成交链路上每个会「触发风控单 / 丢单 / 拒单 / 缩量 / 压制」的点都应往
DecisionRecorder 记一条 DecisionEvent。本测覆盖共用决策层（portfolio /
execution_engine / risk_manager）的各闸门；默认 NULL_RECORDER 时现有行为不变。

回测/实盘共用这一层，故埋点一次两路同时覆盖；实盘专属闸门（收盘/日历/去重/
在途/桥）在 live_engine 测。
"""
from datetime import datetime, date
from decimal import Decimal

from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.position import Position
from core.engine.account import Account
from core.engine.execution_engine import ExecutionEngine, LiveT1Checker
from core.engine.event import BarEvent, OrderEvent
from core.engine.decision import DecisionRecorder
from tq_iquant_shared.constants import SignalType, TradeType


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------
def _bar(stocks, bar_time):
    return BarEvent(stocks=stocks, bar_time=bar_time)


def _ohlcv(close):
    return {
        "open": Decimal(str(close)), "high": Decimal(str(close)),
        "low": Decimal(str(close)), "close": Decimal(str(close)), "volume": 1000,
    }


def _buy(price, quantity, trade_time, code="000001.SZ"):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code=code,
        trade_type=TradeType.BUY, price=Decimal(str(price)), quantity=quantity,
        amount=Decimal(str(price)) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


def _portfolio(strategy_id=1, code="000001.SZ", *, recorder=None,
               stop_loss="0.05", take_profit="0.15", trailing="0.03",
               capital_ratio="0.6", max_positions=5, single_open_ratio="0.1",
               add_position_threshold="0.05", max_add_count=2,
               add_position_ratio="0.1", reduce_position_ratio="0.3",
               role="independent", master_strategy_id=None,
               initial_capital="100000", attach_risk=True):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal(initial_capital), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=strategy_id, period="1d",
        capital_ratio=Decimal(capital_ratio), max_positions=max_positions,
        single_open_ratio=Decimal(single_open_ratio),
        add_position_threshold=Decimal(add_position_threshold),
        max_add_count=max_add_count,
        add_position_ratio=Decimal(add_position_ratio),
        reduce_position_ratio=Decimal(reduce_position_ratio),
        role=role, master_strategy_id=master_strategy_id,
    )
    if attach_risk:
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal(stop_loss),
            take_profit_ratio=Decimal(take_profit),
            trailing_stop_ratio=Decimal(trailing),
        )
    port.strategies.append(ctx)
    if recorder is not None:
        port.set_recorder(recorder)
    return port, ctx


def _hold(ctx, code, price, qty=1000, when=None):
    when = when or datetime(2026, 7, 29, 9, 30)
    pos = Position(code)
    pos.apply_trade(_buy(price, qty, when, code=code))
    ctx.positions[code] = pos
    return pos


def _gates(events):
    return [e.gate for e in events]


def _find(events, gate):
    return next(e for e in events if e.gate == gate)


# ===========================================================================
# Portfolio._signal_to_order —— 信号闸门
# ===========================================================================
class TestSignalGates:
    def test_max_positions_full_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, max_positions=5)
        for i in range(5):
            _hold(ctx, f"00000{i}.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        stocks = {f"00000{i}.SZ": _ohlcv(10.5) for i in range(5)}
        stocks["000009.SZ"] = _ohlcv(10.5)
        bar = _bar(stocks, t)
        cache = {(1, "000009.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        orders = port.on_bar(bar, signal_cache=cache)
        assert orders == []
        ev = _find(r.drain(), "max_positions_full")
        assert ev.layer == "signal_gate" and ev.action == "block"
        assert ev.param_name == "max_positions"
        assert ev.param_value == 5 and ev.actual_value == 5
        assert ev.stock_code == "000009.SZ" and ev.strategy_id == 1

    def test_open_already_holding_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, max_positions=5)
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(10.5)}, t)
        cache = {(1, "000001.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "open_already_holding")
        assert ev.action == "block" and ev.stock_code == "000001.SZ"

    def test_open_qty_too_small_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, single_open_ratio="0.001")
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(10.2)}, t)
        cache = {(1, "000001.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "open_qty_too_small")
        assert ev.layer == "capital_gate" and ev.param_name == "single_open_ratio"
        assert ev.param_value == 0.001

    def test_reduce_qty_too_small_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, reduce_position_ratio="0.3")
        _hold(ctx, "000001.SZ", 10, qty=300)  # 300 × 0.3 = 90 < 100
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(11)}, t)
        cache = {(1, "000001.SZ", t): [{"name": "red_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "red_sig", "signal_type": SignalType.REDUCE, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "reduce_qty_too_small")
        assert ev.param_name == "reduce_position_ratio"
        assert ev.param_value == 0.3 and ev.actual_value == 0  # 算出 90 股 → int 90

    def test_add_threshold_not_met_recorded(self):
        r = DecisionRecorder()
        # trailing 放宽：跌 4% 否则先触发 3% 移动止损（风控信号优先于公式信号）
        port, ctx = _portfolio(recorder=r, add_position_threshold="0.05",
                               trailing="0.5", stop_loss="0.5")
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(9.6)}, t)  # 跌 4% < 5%
        cache = {(1, "000001.SZ", t): [{"name": "add_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "add_threshold_not_met")
        assert ev.param_name == "add_position_threshold"
        assert ev.param_value == 0.05
        # actual = 实际跌幅 (10-9.6)/10 = 0.04
        assert abs(ev.actual_value - 0.04) < 1e-9

    def test_add_count_exceeded_recorded(self):
        r = DecisionRecorder()
        # trailing 放宽：跌 10% 否则先触发 3% 移动止损（风控信号优先于公式信号）
        port, ctx = _portfolio(recorder=r, max_add_count=2, stop_loss="0.2",
                               trailing="0.5")
        pos = _hold(ctx, "000001.SZ", 10)
        pos.add_count = 2
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(9.0)}, t)  # 跌 10% 过阈值，但次数已满
        cache = {(1, "000001.SZ", t): [{"name": "add_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "add_count_exceeded")
        assert ev.param_name == "max_add_count"
        assert ev.param_value == 2 and ev.actual_value == 2

    def test_add_qty_too_small_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, add_position_ratio="0.001",
                               add_position_threshold="-1")
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(10)}, t)
        cache = {(1, "000001.SZ", t): [{"name": "add_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "add_qty_too_small")
        assert ev.layer == "capital_gate" and ev.param_name == "add_position_ratio"

    def test_slave_master_block_recorded(self):
        r = DecisionRecorder()
        # slave(id=2) OPEN，master(id=1) 无持仓 → 拦
        port, ctx = _portfolio(strategy_id=2, recorder=r, role="slave",
                               master_strategy_id=1)
        master = StrategyContext(strategy_id=1, period="1d",
                                 capital_ratio=Decimal("0.6"), max_positions=5,
                                 role="master")
        master.strategy_risk = ctx.strategy_risk
        port.strategies.insert(0, master)
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(10.5)}, t)
        cache = {(2, "000001.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "slave_master_block")
        assert ev.action == "block" and ev.strategy_id == 2

    def test_halted_bar_no_price_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r)
        t = datetime(2026, 7, 30, 13, 5)
        bar = _bar({"000001.SZ": _ohlcv(0)}, t)  # 停牌 close=0
        cache = {(1, "000001.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        ev = _find(r.drain(), "halted_bar_no_price")
        assert ev.action == "block"

    def test_halted_buy_strip_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r)
        port.risk_manager.circuit_breaker_active = True
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000009.SZ": _ohlcv(10.5)}, t)
        cache = {(1, "000009.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []  # BUY 被剥
        ev = _find(r.drain(), "halted_buy_strip")
        assert ev.layer == "portfolio_risk" and ev.action == "strip"
        assert ev.stock_code == "000009.SZ"
        assert ev.requested_qty is not None and ev.requested_qty > 0


# ===========================================================================
# Portfolio._check_risks —— 策略风控触发
# ===========================================================================
class TestStrategyRiskTriggers:
    def _fire(self, **kw):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, **kw)
        return port, ctx, r

    def test_stop_loss_trigger_recorded(self):
        port, ctx, r = self._fire(stop_loss="0.05", take_profit="0.5", trailing="0.5")
        _hold(ctx, "000001.SZ", 10)  # 成本 10
        t = datetime(2026, 7, 30, 15, 0)
        port.on_bar(_bar({"000001.SZ": _ohlcv(9.4)}, t), signal_cache={})  # 亏 6%
        ev = _find(r.drain(), "stop_loss")
        assert ev.layer == "strategy_risk" and ev.action == "trigger"
        assert ev.param_name == "stop_loss_ratio" and ev.param_value == 0.05
        assert abs(ev.actual_value - 0.06) < 1e-9
        assert ev.stock_code == "000001.SZ"

    def test_take_profit_trigger_recorded(self):
        port, ctx, r = self._fire(stop_loss="0.5", take_profit="0.15", trailing="0.5")
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        port.on_bar(_bar({"000001.SZ": _ohlcv(12)}, t), signal_cache={})  # 盈 20%
        ev = _find(r.drain(), "take_profit")
        assert ev.action == "trigger"
        assert ev.param_name == "take_profit_ratio" and ev.param_value == 0.15
        assert abs(ev.actual_value - 0.20) < 1e-9

    def test_trailing_stop_trigger_recorded(self):
        port, ctx, r = self._fire(stop_loss="0.5", take_profit="0.5", trailing="0.03")
        _hold(ctx, "000001.SZ", 12)  # 成本/最高 12
        t = datetime(2026, 7, 30, 15, 0)
        port.on_bar(_bar({"000001.SZ": _ohlcv(11.4)}, t), signal_cache={})  # 自高点回撤 5%
        ev = _find(r.drain(), "trailing_stop")
        assert ev.action == "trigger"
        assert ev.param_name == "trailing_stop_ratio" and ev.param_value == 0.03
        assert abs(ev.actual_value - 0.05) < 1e-9

    def test_missing_strategy_risk_recorded(self):
        r = DecisionRecorder()
        port, ctx = _portfolio(recorder=r, attach_risk=False)
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        port.on_bar(_bar({"000001.SZ": _ohlcv(9.0)}, t), signal_cache={})
        ev = _find(r.drain(), "missing_strategy_risk")
        assert ev.layer == "signal_gate" and ev.action == "block"
        assert ev.strategy_id == 1


# ===========================================================================
# ExecutionEngine.cap_quantity —— 资金 / T+1 闸门
# ===========================================================================
def _order(trade_type, quantity, *, price="10", code="000001.SZ",
           sid=1, bar_time=None):
    return OrderEvent(
        strategy_id=sid, portfolio_id=1, stock_code=code,
        trade_type=trade_type,
        signal_type=SignalType.OPEN if trade_type == TradeType.BUY else SignalType.CLOSE,
        signal_name="t", quantity=quantity,
        price=Decimal(price) if price is not None else None,
        bar_time=bar_time or datetime(2026, 7, 30, 15, 0),
    )


class TestCapitalAndT1Gates:
    def test_insufficient_funds_reject_recorded_and_counted(self):
        r = DecisionRecorder()
        acc = Account(initial_capital=Decimal("500"))  # 价 10 → 500/10=50 股 <100
        eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
        out = eng.cap_quantity(_order(TradeType.BUY, 1000, price="10"), acc, None)
        assert out is None
        assert acc.insufficient_count == 1  # 死计数仍自增（不破坏现有语义）
        ev = _find(r.drain(), "insufficient_funds")
        assert ev.layer == "capital_gate" and ev.action == "reject"
        assert ev.requested_qty == 1000 and ev.final_qty == 0
        assert ev.actual_value == 500.0  # 可用购买力

    def test_order_shrunk_recorded(self):
        r = DecisionRecorder()
        acc = Account(initial_capital=Decimal("1500"))  # 价 10 → 够 100 股，不够 1000
        eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
        out = eng.cap_quantity(_order(TradeType.BUY, 1000, price="10"), acc, None)
        assert out == 100  # 缩到 1 手
        ev = _find(r.drain(), "order_shrunk")
        assert ev.action == "shrink"
        assert ev.requested_qty == 1000 and ev.final_qty == 100

    def test_full_buy_records_nothing(self):
        r = DecisionRecorder()
        acc = Account(initial_capital=Decimal("1000000"))
        eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
        out = eng.cap_quantity(_order(TradeType.BUY, 1000, price="10"), acc, None)
        assert out == 1000
        assert r.drain() == []  # 足额成交无闸门

    def test_t1_clamp_recorded(self):
        r = DecisionRecorder()
        acc = Account(initial_capital=Decimal("0"))
        t1 = LiveT1Checker()
        t1.set_available_map({"000001.SZ": 300})  # 可卖 300 < 请求 1000
        eng = ExecutionEngine(None, t1, recorder=r)
        pos = Position("000001.SZ")
        pos.quantity = 1000
        out = eng.cap_quantity(_order(TradeType.SELL, 1000), acc, pos)
        assert out == 300
        ev = _find(r.drain(), "t1_clamp")
        assert ev.layer == "t1" and ev.action == "clamp"
        assert ev.requested_qty == 1000 and ev.final_qty == 300

    def test_t1_insufficient_recorded(self):
        r = DecisionRecorder()
        acc = Account(initial_capital=Decimal("0"))
        t1 = LiveT1Checker()
        t1.set_available_map({"000001.SZ": 50})  # 可卖 50 <100
        eng = ExecutionEngine(None, t1, recorder=r)
        pos = Position("000001.SZ")
        pos.quantity = 1000
        out = eng.cap_quantity(_order(TradeType.SELL, 1000), acc, pos)
        assert out is None
        ev = _find(r.drain(), "t1_insufficient")
        assert ev.layer == "t1" and ev.action == "block"
        assert ev.final_qty == 50


# ===========================================================================
# PortfolioRiskManager —— 组合熔断 / 恢复
# ===========================================================================
class TestBreakerGates:
    def test_max_drawdown_halt_recorded(self):
        r = DecisionRecorder()
        port, _ = _portfolio(recorder=r)
        rm = port.risk_manager
        d1 = date(2026, 8, 25)
        rm.update_peak(Decimal("100000"), d1)  # 建峰
        rm.update_peak(Decimal("70000"), d1)   # 回撤 30% > 20%
        ev = _find(r.drain(), "max_drawdown")
        assert ev.layer == "portfolio_risk" and ev.action == "halt"
        assert ev.param_name == "max_drawdown" and ev.param_value == 0.2
        assert abs(ev.actual_value - 0.3) < 1e-9
        assert ev.portfolio_id == 1
        assert rm.circuit_breaker_active

    def test_daily_loss_halt_recorded(self):
        r = DecisionRecorder()
        port, _ = _portfolio(recorder=r)
        rm = port.risk_manager
        rm.update_peak(Decimal("100000"), date(2026, 8, 24))
        rm.update_peak(Decimal("100000"), date(2026, 8, 25))  # 跨日 → prev_close=100000
        rm.update_daily(Decimal("90000"), date(2026, 8, 25), Decimal("100000"))  # 亏 10%
        ev = _find(r.drain(), "daily_loss")
        assert ev.action == "halt"
        assert ev.param_name == "daily_loss_limit" and ev.param_value == 0.05
        assert abs(ev.actual_value - 0.1) < 1e-9
        assert rm.daily_pause_active

    def test_next_day_recovery_recorded(self):
        r = DecisionRecorder()
        port, _ = _portfolio(recorder=r)
        rm = port.risk_manager
        d1 = date(2026, 8, 25)
        rm.update_peak(Decimal("100000"), d1)
        rm.update_peak(Decimal("70000"), d1)  # 熔断
        r.drain()  # 清掉 halt 事件
        # 次日回升到 95000（回撤 5% < 20%）→ 自动恢复且不再触发
        rm.update_peak(Decimal("95000"), date(2026, 8, 26))
        evs = r.drain()
        ev = _find(evs, "risk_recover")
        assert ev.layer == "portfolio_risk" and ev.action == "recover"
        assert not rm.circuit_breaker_active
        assert "max_drawdown" not in _gates(evs)  # 回升后未再熔断

    def test_daily_loss_next_day_recovery_recorded(self):
        r = DecisionRecorder()
        port, _ = _portfolio(recorder=r)
        rm = port.risk_manager
        rm.update_peak(Decimal("100000"), date(2026, 8, 24))
        rm.update_peak(Decimal("100000"), date(2026, 8, 25))
        rm.update_daily(Decimal("90000"), date(2026, 8, 25), Decimal("100000"))  # 14:30 触发
        rm.update_peak(Decimal("90000"), date(2026, 8, 25))  # 收盘 bar：昨日基准落到 90000
        r.drain()
        # 次日（生产调用序：update_peak 每 bar 跨日刷新 prev_close → 14:30 update_daily）
        # 平开 90000：pnl=0 不再触发，仅恢复翻转
        rm.update_peak(Decimal("90000"), date(2026, 8, 26))
        rm.update_daily(Decimal("90000"), date(2026, 8, 26), Decimal("100000"))
        evs = r.drain()
        ev = _find(evs, "risk_recover")
        assert ev.action == "recover"
        assert not rm.daily_pause_active
        assert "daily_loss" not in _gates(evs)  # 次日未再触发


# ===========================================================================
# 默认 NULL_RECORDER：不接 recorder 时零影响（现有构造/测试不破）
# ===========================================================================
class TestNullRecorderDefault:
    def test_portfolio_without_recorder_records_nothing(self):
        # 不接 recorder：触发 max_positions 也不报错、行为不变
        port, ctx = _portfolio(max_positions=1)
        _hold(ctx, "000001.SZ", 10)
        t = datetime(2026, 7, 30, 15, 0)
        bar = _bar({"000001.SZ": _ohlcv(10.5), "000002.SZ": _ohlcv(10.5)}, t)
        cache = {(1, "000002.SZ", t): [{"name": "open_sig", "value": 1}]}
        ctx.formula_signals = [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}]
        assert port.on_bar(bar, signal_cache=cache) == []
        # recorder 是 NULL，drain 为空
        assert bool(port.recorder) is False
        assert port.risk_manager.recorder.drain() == []

    def test_execution_engine_without_recorder_ok(self):
        acc = Account(initial_capital=Decimal("500"))
        eng = ExecutionEngine(None, LiveT1Checker())  # 不传 recorder
        assert eng.cap_quantity(_order(TradeType.BUY, 1000, price="10"), acc, None) is None
        assert acc.insufficient_count == 1
