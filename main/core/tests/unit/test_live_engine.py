"""LiveEngine 单测（0009 切片4）— Mock dispatcher（httpx MockTransport）+ 内存 SQLite。

验证实盘端到端链路：bar → 信号 → 真实下单（桥受理）→ 落 live_orders/live_trades，
以及 Core 重启后从 live_trades 恢复虚拟持仓/虚拟现金。引擎核心（Portfolio/ExecutionEngine）
复用回测逻辑，仅注入 HttpBridgeDispatcher + LiveT1Checker。
"""
import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.engine.account import Account
from core.engine.event import BarEvent, OrderEvent, TradeEvent
from core.engine.execution_engine import LiveT1Checker
from core.engine.http_bridge_dispatcher import (
    BridgeUnavailableError,
    HttpBridgeDispatcher,
)
from core.engine.live_engine import LiveEngine
from core.engine.portfolio import Portfolio
from core.engine.position import Position
from core.engine.risk_manager import PortfolioRiskManager, StrategyRiskManager
from core.engine.strategy_context import StrategyContext
from core.models import Base, LiveOrder, LiveTrade
from tq_iquant_shared.constants import SignalType, TradeType


# ---------------- 共用辅助 ----------------
def _db_factory():
    """内存 SQLite Session 工厂：返回 () -> Session，引擎每根 bar 取一个独立 Session。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # expire_on_commit=False：commit 后对象属性仍可访问（测试断言方便，不触发 refresh）
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def factory():
        return SessionLocal()

    return factory, engine


class _Recorder:
    """MockTransport：记录请求，/order 返回受理成功，/ping 返回 ok。"""

    def __init__(self, respond=None, fail_paths=None):
        self.requests = []
        self._respond = respond
        self._fail_paths = fail_paths or set()

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in self._fail_paths:
            raise httpx.ConnectError("connection refused")
        if self._respond is not None:
            return self._respond(request)
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {}})
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})


def _make_dispatcher(rec, fail_paths=None):
    client = httpx.Client(transport=httpx.MockTransport(rec.handler))
    return HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=client), rec


def _portfolio_single(period="1m", strategy_id=1):
    """单组合单策略，formula_signal 配 OPEN（trigger_value=1）。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=strategy_id, period=period,
        capital_ratio=Decimal("0.6"), max_positions=5,
        single_open_ratio=Decimal("0.1"),
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"),
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def _portfolio_two(periods=("1m", "5m")):
    """单组合两策略（periods 指定各自周期），各配 OPEN 信号。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctxs = []
    for sid, period in enumerate(periods, start=1):
        ctx = StrategyContext(
            strategy_id=sid, period=period,
            capital_ratio=Decimal("0.6"), max_positions=5,
            single_open_ratio=Decimal("0.1"),
        )
        ctx.formula_signals = [
            {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
        ]
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal("0.05"),
            take_profit_ratio=Decimal("0.2"),
            trailing_stop_ratio=Decimal("0"),
        )
        port.strategies.append(ctx)
        ctxs.append(ctx)
    return port, ctxs


def _bar(stock, close, bar_time):
    """单股票 OHLCV bar，close 触发信号用。"""
    return BarEvent(
        stocks={stock: {
            "open": Decimal("9.0"), "high": Decimal(close),
            "low": Decimal("9.0"), "close": Decimal(close), "volume": 10000,
        }},
        bar_time=bar_time,
    )


# ---------------- _handle_bar 下单落库（切片5：先 submitted 后回填）----------------
def test_handle_bar_signal_to_trade_persisted():
    """OPEN 信号 → BUY 发单 → LiveOrder(status=submitted) 落库，不写 LiveTrade、不 apply。

    切片5 时序：先写 submitted + commit，再发 passorder。submitted 阶段不写 LiveTrade
    （回填确认 filled 才写）、不 apply_trade（持仓/现金不变，避免受理即成交的近似）。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    # 预置信号缓存：触发 open_sig=1
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    trades = db.query(LiveTrade).all()
    orders = db.query(LiveOrder).all()
    assert len(trades) == 0  # 回填确认成交前不写 LiveTrade
    assert len(orders) == 1
    assert orders[0].status == "submitted"
    assert orders[0].signal_name == "open_sig"
    # 下单量由 _signal_to_order 计算：int(0.1 * 60000 / 9.3 / 100) * 100 = 600
    assert orders[0].quantity == 600
    assert orders[0].filled_quantity == 0
    # submitted 阶段不 apply：持仓/现金不变
    assert ctx.positions[stock].quantity == 0
    assert port.account.cash == Decimal("100000")
    db.close()


def test_no_signal_no_trade():
    """bar 不触发信号 → 不落 trade、不落 order。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 1)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    # 信号缓存空 → 无触发
    engine.signal_cache = {}
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    assert db.query(LiveTrade).count() == 0
    assert db.query(LiveOrder).count() == 0
    db.close()


# ---------------- 持仓恢复 ----------------
def test_recover_rebuilds_positions_from_trades():
    """预置 live_trades(1 笔 BUY 100 股) → recover → position.quantity==100、现金==初始-金额。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置一笔成交落库（模拟 Core 重启前的历史交易）
    db = factory()
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1, stock_code=stock,
        trade_type="buy", order_type="limit", price=Decimal("9.3"), quantity=100,
        filled_quantity=100, filled_price=Decimal("9.3"), status="accepted",
        signal_name="open_sig", signal_type="OPEN", bar_time=datetime(2026, 8, 5, 10, 0),
    )
    db.add(lo)
    db.flush()
    db.add(LiveTrade(
        live_session_id=1, live_order_id=lo.id, portfolio_strategy_id=1, strategy_id=1,
        stock_code=stock, trade_type="buy", price=Decimal("9.3"), quantity=100,
        amount=Decimal("930"), commission=Decimal("0"), stamp_duty=Decimal("0"),
        trade_time=datetime(2026, 8, 5, 10, 0),
    ))
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    db = factory()
    engine.recover(db)
    db.close()

    assert ctx.positions[stock].quantity == 100
    assert port.account.cash == Decimal("100000") - Decimal("930")


# ---------------- LiveT1Checker ----------------
def test_live_t1_checker_full_quantity_without_bridge_map():
    """F5：桥可用表未取到（空表）→ 全量放行（券商端 T+1 兜底，不误伤正常卖出）。"""
    pos = Position("600000.SH")
    pos.buy(100, Decimal("9.0"), datetime(2026, 8, 5, 10, 0))
    checker = LiveT1Checker()
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 100


def test_live_t1_checker_caps_by_bridge_available():
    """F5：桥 available=200（昨仓 200、今买 400 不可卖）→ SELL 上限 min(600,200)=200。"""
    pos = Position("600000.SH")
    pos.buy(600, Decimal("9.0"), datetime(2026, 8, 5, 10, 0))
    checker = LiveT1Checker()
    checker.set_available_map({"600000.SH": 200})
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 200


def test_live_t1_checker_min_when_bridge_available_larger():
    """F5：桥 available > 持有量 → 以持有量为准（min）。"""
    pos = Position("600000.SH")
    pos.buy(100, Decimal("9.0"), datetime(2026, 8, 5, 10, 0))
    checker = LiveT1Checker()
    checker.set_available_map({"600000.SH": 500})
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 100


def test_handle_bar_sell_capped_by_bridge_available():
    """F5：CLOSE 信号 SELL 600，桥 /positions available=200 → LiveOrder.quantity==200。

    先 cap_quantity 定最终量再落 submitted——DB 下单量与实发一致，回填不误判 partial。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single(strategy_id=1, period="1m")
    ctx.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1},
    ]
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    # 虚拟持仓 600 股（昨仓 200 + 今买 400，今买不可卖 → 桥 available=200）
    ctx.positions[stock] = Position(stock)
    ctx.positions[stock].buy(600, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))

    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": 200, "volume": 600,
                 "yesterday_volume": 200, "on_road_volume": 400},
            ]})
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})
    rec = _Recorder(respond=respond)
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "close_sig", "value": -1}]}
    bar = _bar(stock, "9.0", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1
    assert orders[0].status == "submitted"
    assert orders[0].quantity == 200  # 先 cap 再落库：DB 量与实发一致
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert placed and json.loads(placed[0].content)["volume"] == 200
    db.close()


def test_handle_bar_refreshes_available_once_per_bar():
    """F5：多组合共享同一 bar 对象 → 每 bar 只刷一次 /positions（强引用去重）。"""
    factory, _ = _db_factory()
    port1, _ = _portfolio_single(strategy_id=1)
    port2, _ = _portfolio_single(strategy_id=2)
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port1, port2], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port1, bar)
    engine._handle_bar(port2, bar)

    pos_reqs = [r for r in rec.requests if r.url.path == "/positions"]
    assert len(pos_reqs) == 1


# ---------------- 桥离线暂停 ----------------
def test_bridge_offline_pauses_no_trade():
    """dispatcher heartbeat=False → _loop 不下单、bridge_online=False、不抛。"""
    import asyncio

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    # /ping 也 fail → heartbeat False
    rec = _Recorder(fail_paths={"/ping", "/quote"})
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, poll_interval=0.01,
    )
    # 预置信号，若下单会落 trade（此处验证不下单）
    engine.signal_cache = {
        (1, stock, datetime(2026, 8, 5, 10, 0)): [{"name": "open_sig", "value": 1}]
    }

    async def run_a_bit():
        await engine.start()
        await asyncio.sleep(0.05)  # 跑几轮心跳
        await engine.stop()

    asyncio.run(run_a_bit())

    assert engine.bridge_online is False
    db = factory()
    assert db.query(LiveTrade).count() == 0
    db.close()


def test_handle_bar_bridge_unavailable_no_persist():
    """_handle_bar 下单时桥抛 BridgeUnavailableError → 先写 submitted 再标 rejected。

    切片5 时序：submitted 已先落库（I4 命门窗口闭合），发单桥异常 → 标 rejected、
    _bridge_online=False（上层心跳暂停下单），不写 LiveTrade、不 apply。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    # /order fail → place_order 抛 BridgeUnavailableError
    rec = _Recorder(fail_paths={"/order"})
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time)

    engine._on_bar(bar)  # _on_bar 捕获 BridgeUnavailableError，不外抛

    assert engine.bridge_online is False
    db = factory()
    assert db.query(LiveTrade).count() == 0
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1
    assert orders[0].status == "rejected"
    assert orders[0].error_message == "bridge unavailable"
    assert ctx.positions[stock].quantity == 0  # 未 apply
    db.close()


# ---------------- 生命周期 ----------------
def test_start_stop_lifecycle():
    """start 起 loop 任务、stop 取消任务（短 poll_interval + mock poll）。"""
    import asyncio

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, poll_interval=0.01,
    )

    async def run():
        await engine.start()
        assert engine._task is not None
        await asyncio.sleep(0.03)
        await engine.stop()
        assert engine._task is None
        assert engine._running is False

    asyncio.run(run())


# ---------------- 公式信号注入（0010）----------------
from unittest.mock import MagicMock  # noqa: E402

from core.tq.formula import TQFormula  # noqa: E402


def _quote_bars(code, bar_times):
    """构造桥 /quote 返回的 bar dict 列表（字段同 xtdata：index/time/open/..）。"""
    bars = []
    for i, bt in enumerate(bar_times):
        bars.append({
            "index": bt.strftime("%Y%m%d%H%M%S"),
            "time": int(bt.timestamp() * 1000),
            "open": 9.0 + i * 0.01, "high": 9.3 + i * 0.01,
            "low": 9.0, "close": 9.2 + i * 0.01,
            "volume": 10000 + i, "amount": 92000.0 + i,
        })
    return bars


def _mock_dispatcher_with_quote(rec, code, bars):
    """让 _Recorder 的 /quote 返回固定 bars。"""
    def respond(request):
        if request.url.path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {code: bars}})
        if request.url.path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})
    rec._respond = respond


def _make_engine_with_formula(disp, factory, formula_by_strategy, formula_count=200):
    """构造带 tq_formula + formula_by_strategy 的 LiveEngine。"""
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    return LiveEngine(
        session_id=1, portfolios=[], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        tq_formula=TQFormula(), formula_by_strategy=formula_by_strategy,
        formula_count=formula_count,
    )


def test_bars_to_formula_df_field_mapping():
    """桥 bar dict（index 时间 + 小写 OHLCV）→ {大写字段: DataFrame}，列=code，DatetimeIndex。"""
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(3)])
    df = LiveEngine._bars_to_formula_df(bars, "600000.SH")
    # 六个字段齐全
    for field in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        assert field in df
    # 每个都是单列 DataFrame（列=code）
    assert list(df["Close"].columns) == ["600000.SH"]
    # 行数 = bar 数
    assert len(df["Close"]) == 3
    # DatetimeIndex 升序
    idx = list(df["Close"].index)
    assert idx == sorted(idx)
    # 值正确映射（首 bar close=9.2）
    assert float(df["Close"].iloc[0, 0]) == 9.2


def test_bars_to_formula_df_empty_returns_none():
    """空 bar 列表 → None（无法注入）。"""
    assert LiveEngine._bars_to_formula_df([], "600000.SH") is None


def test_extract_latest_signal_takes_last_bar():
    """raw 多条输出 → 只取最后一条 bar 的 {var: value} → [{name, value}]。"""
    raw = {
        "ErrorId": "0",
        "600000.SH": {
            "open_sig": [
                {"Date": "202608051000", "Value": 0.0},
                {"Date": "202608051001", "Value": 1.0},
                {"Date": "202608051002", "Value": 0.0},
            ],
            "ma5": [
                {"Date": "202608051000", "Value": 9.1},
                {"Date": "202608051001", "Value": 9.2},
                {"Date": "202608051002", "Value": 9.3},
            ],
        },
    }
    outputs = LiveEngine._extract_latest_signal(raw, "600000.SH")
    # 取最后一条：open_sig=0, ma5=9.3
    out_map = {o["name"]: o["value"] for o in outputs}
    assert out_map["open_sig"] == 0
    assert out_map["ma5"] == 9


def test_extract_latest_signal_errorid_nonzero_returns_empty():
    """ErrorId 非 0/19 → 返回空列表（公式出错）。"""
    raw = {"ErrorId": "1", "Error": "compute fail"}
    assert LiveEngine._extract_latest_signal(raw, "600000.SH") == []


def test_extract_latest_signal_no_stock_returns_empty():
    """raw 中无该股票 → 空列表。"""
    raw = {"ErrorId": "0", "000001.SZ": {"x": [{"Value": 1.0}]}}
    assert LiveEngine._extract_latest_signal(raw, "600000.SH") == []


def test_fill_signal_cache_populates_cache():
    """mock query_quote 返回 bars + mock compute_injected 返回 raw → signal_cache 填入。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)

    engine = _make_engine_with_formula(disp, factory, {1: "MACROSSPRO"})
    engine.portfolios = [port]
    # mock compute_injected：返回最后一条 open_sig=1
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0",
        stock: {"open_sig": [
            {"Date": "202608051000", "Value": 0.0},
            {"Date": "202608051001", "Value": 0.0},
            {"Date": "202608051002", "Value": 1.0},
        ]},
    }
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    key = (1, stock, bar_time)
    assert key in engine.signal_cache
    out_map = {o["name"]: o["value"] for o in engine.signal_cache[key]}
    assert out_map["open_sig"] == 1


def test_fill_signal_cache_skips_strategy_without_formula():
    """策略不在 formula_by_strategy → 跳过，不查 quote 不算公式。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_with_formula(disp, factory, {})  # 空：无策略映射
    engine.portfolios = [port]
    engine._tq_formula.compute_injected = MagicMock(return_value=None)
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    assert engine.signal_cache == {}
    engine._tq_formula.compute_injected.assert_not_called()


def test_fill_signal_cache_empty_bars_skipped():
    """query_quote 返回空 bars → _bars_to_formula_df None → 跳过该股票。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    _mock_dispatcher_with_quote(rec, stock, [])  # 空 bars
    engine = _make_engine_with_formula(disp, factory, {1: "MACROSSPRO"})
    engine.portfolios = [port]
    engine._tq_formula.compute_injected = MagicMock(return_value=None)
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    engine._tq_formula.compute_injected.assert_not_called()
    assert engine.signal_cache == {}


def test_handle_bar_with_formula_signal_triggers_trade():
    """_handle_bar 接入 _fill_signal_cache：mock 公式返回 open_sig=1 → BUY 落 submitted（非预置 cache）。

    切片5：公式信号触发发单 → LiveOrder(status=submitted) + signal_name=open_sig；
    不写 LiveTrade（回填确认 filled 才写）。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)

    engine = _make_engine_with_formula(disp, factory, {1: "MACROSSPRO"})
    engine.portfolios = [port]
    # mock compute_injected：最后一条 open_sig=1 → 触发 OPEN
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0",
        stock: {"open_sig": [
            {"Date": "202608051000", "Value": 0.0},
            {"Date": "202608051001", "Value": 0.0},
            {"Date": "202608051002", "Value": 1.0},
        ]},
    }
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    trades = db.query(LiveTrade).all()
    assert len(trades) == 0
    # signal_name 来自公式信号 open_sig（非风控），status=submitted 待回填
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1
    assert orders[0].signal_name == "open_sig"
    assert orders[0].status == "submitted"
    assert ctx.positions[stock].quantity == 0  # 未 apply
    db.close()


# ===========================================================================
# 切片5：订单状态机 + /deals 回填（0011）
# ===========================================================================
def _mock_orders_deals(rec, orders=None, deals=None):
    """让 _Recorder 的 /orders 与 /deals 返回固定数据。"""
    orders = orders if orders is not None else []
    deals = deals if deals is not None else []

    def respond(request):
        path = request.url.path
        if path == "/orders":
            return httpx.Response(200, json={"ok": True, "data": orders})
        if path == "/deals":
            return httpx.Response(200, json={"ok": True, "data": deals})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {}})
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})

    rec._respond = respond


def _make_engine(disp, factory, port):
    """构造 LiveEngine（含 poller）。dispatcher 由 _make_dispatcher 提供。"""
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    return LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )


def _submitted_order(db, quantity=600, status="submitted"):
    """预置一笔 submitted 状态的 LiveOrder（模拟 _handle_bar 已发单）。"""
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="600000.SH", trade_type="buy", order_type="limit",
        price=Decimal("9.3"), quantity=quantity, filled_quantity=0,
        filled_price=None, status=status, signal_name="open_sig",
        signal_type="OPEN", bar_time=datetime(2026, 8, 5, 10, 0),
    )
    db.add(lo)
    db.flush()
    return lo


def test_persist_order_submitted_writes_status_submitted():
    """_persist_order_submitted → LiveOrder(status=submitted)，无 LiveTrade。"""
    factory, _ = _db_factory()
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    port, _ = _portfolio_single()
    engine = _make_engine(disp, factory, port)
    order = OrderEvent(
        strategy_id=1, portfolio_id=1, stock_code="600000.SH",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN,
        quantity=600, price=Decimal("9.3"), bar_time=datetime(2026, 8, 5, 10, 0),
        signal_name="open_sig",
    )
    db = factory()
    lo = engine._persist_order_submitted(db, order)
    lo_id = lo.id  # commit 前取 id（commit 后对象 expire，闭 session 前访问会 Detached）
    db.commit()
    db.close()

    db = factory()
    lo2 = db.get(LiveOrder, lo_id)
    assert lo2.status == "submitted"
    assert lo2.filled_quantity == 0
    assert lo2.filled_price is None
    assert db.query(LiveTrade).count() == 0
    db.close()


def test_order_status_transitions_submitted_to_filled():
    """submitted → _poll_deals 回填 → filled，真实价/量/佣金写入 LiveTrade + apply。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    # /orders 返回 BRIDGE 委托（匹配 order_ref），/deals 返回真实成交
    _mock_orders_deals(rec, orders=[{
        "source": "BRIDGE", "instrument": "600000", "exchange": "SH",
        "direction": 48, "volume": 600, "order_ref": "ref-abc-123",
    }], deals=[{
        "order_ref": "ref-abc-123", "price": 9.25, "volume": 600,
        "amount": 5550.0, "commission": 1.39, "trade_time": "150001",
        "trade_date": "20260805",
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    engine._poll_deals()

    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    assert lo2.status == "filled"
    assert lo2.order_ref == "ref-abc-123"
    assert lo2.filled_quantity == 600
    assert lo2.filled_price == Decimal("9.25")
    trades = db.query(LiveTrade).all()
    assert len(trades) == 1
    assert trades[0].price == Decimal("9.25")
    assert trades[0].quantity == 600
    assert trades[0].amount == Decimal("5550.0")
    assert trades[0].commission == Decimal("1.39")
    # filled 后 apply：持仓 600，现金扣 amount+commission
    assert ctx.positions[stock].quantity == 600
    assert port.account.cash == Decimal("100000") - (Decimal("5550.0") + Decimal("1.39"))
    db.close()


def test_order_status_transitions_partial_fill():
    """部分成交 → status=partial，写部分 LiveTrade，不 apply（等最终确认）。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[{
        "source": "BRIDGE", "instrument": "600000", "exchange": "SH",
        "direction": 48, "volume": 600, "order_ref": "ref-partial",
    }], deals=[{
        "order_ref": "ref-partial", "price": 9.25, "volume": 300,
        "amount": 2775.0, "commission": 0.69, "trade_time": "150001",
        "trade_date": "20260805",
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    engine._poll_deals()

    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    assert lo2.status == "partial"
    assert lo2.filled_quantity == 300
    trades = db.query(LiveTrade).all()
    assert len(trades) == 1
    assert trades[0].quantity == 300
    # partial 不 apply：持仓不变（该股无 Position 或 quantity==0）
    assert ctx.positions.get(stock) is None or ctx.positions[stock].quantity == 0
    assert port.account.cash == Decimal("100000")
    db.close()


def test_try_match_order_ref_ignores_gui_and_mismatch():
    """_try_match_order_ref：GUI 单/不匹配方向量的单被忽略，找不到 order_ref 留 None。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    # 只有 GUI 单 + 不匹配的 BRIDGE 单（量不符）
    _mock_orders_deals(rec, orders=[
        {"source": "GUI", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "gui-ref"},
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 999, "order_ref": "wrong-vol"},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref is None  # 都不匹配
    db.close()


def test_try_match_order_ref_finds_bridge_order():
    """_try_match_order_ref 定位 BRIDGE 委托 → order_ref 回写。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[{
        "source": "BRIDGE", "instrument": "600000", "exchange": "SH",
        "direction": 48, "volume": 600, "order_ref": "ref-found",
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref == "ref-found"
    db.close()


def test_poll_deals_bridge_offline_skips():
    """query_deals 抛 BridgeUnavailableError → 本轮跳过，状态不变，下轮重试。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    # /orders 匹配成功（order_ref 回写），但 /deals 桥不可用（ConnectError）
    rec = _Recorder(fail_paths={"/deals"})
    orders_data = [{
        "source": "BRIDGE", "instrument": "600000", "exchange": "SH",
        "direction": 48, "volume": 600, "order_ref": "ref-offline",
    }]

    def respond(request):
        path = request.url.path
        if path == "/orders":
            return httpx.Response(200, json={"ok": True, "data": orders_data})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})

    rec._respond = respond
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    engine._poll_deals()

    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    assert lo2.status == "submitted"  # 未回填，下轮重试
    assert db.query(LiveTrade).count() == 0
    db.close()


def test_recover_finds_pending_orders():
    """recover 挂回 submitted/partial，忽略 filled/rejected。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    _submitted_order(db, quantity=600, status="submitted")
    _submitted_order(db, quantity=600, status="partial")
    _submitted_order(db, quantity=600, status="filled")
    _submitted_order(db, quantity=600, status="rejected")
    db.commit()
    db.close()

    db = factory()
    engine.recover(db)
    db.close()

    assert len(engine._pending_orders) == 2  # 只有 submitted + partial


# ===========================================================================
# G7：桥状态并入 session API（0011 §5.11）
# ===========================================================================
def test_handle_bar_tracks_pending_orders():
    """_handle_bar 发单后该单计入 _pending_orders（G7 pending 计数）。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port, bar)

    assert engine.pending_orders_count == 1  # 发单后在途
    db = factory()
    assert db.query(LiveOrder).count() == 1
    db.close()


def test_poll_deals_syncs_pending_orders_from_db():
    """未成交的单：_poll_deals 后仍在 pending（计数 1），last_backfill_time 为空。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[], deals=[])  # 无匹配 → 保持 pending
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    engine._poll_deals()

    assert engine.pending_orders_count == 1
    assert engine.last_backfill_time is None


def test_backfill_updates_last_backfill_time_and_clears_pending():
    """_poll_deals 回填 filled 后：last_backfill_time 更新，pending 计数清零（G7）。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[{
        "source": "BRIDGE", "instrument": "600000", "exchange": "SH",
        "direction": 48, "volume": 600, "order_ref": "ref-abc-123",
    }], deals=[{
        "order_ref": "ref-abc-123", "price": 9.25, "volume": 600,
        "amount": 5550.0, "commission": 1.39, "trade_time": "150001",
        "trade_date": "20260805",
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    engine._poll_deals()

    assert engine.last_backfill_time is not None
    assert engine.pending_orders_count == 0  # filled 后不在途
    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    assert lo2.status == "filled"
    db.close()


# ===========================================================================
# C6 三段式实盘周期链路 + E8 离线恢复（0011 切片5 定案）
# ===========================================================================
from core.engine.live_engine import periods_on_boundary  # noqa: E402


def _make_engine_formula_portfolio(disp, factory, port, formula_by_strategy,
                                   formula_count=200, formula_count_by_name=None):
    """构造带 tq_formula + formula_by_strategy 且含组合的 LiveEngine（C6 注入/分发用）。"""
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    return LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        tq_formula=TQFormula(), formula_by_strategy=formula_by_strategy,
        formula_count=formula_count, formula_count_by_name=formula_count_by_name,
    )


def _respond_quote_bars(stock, bars, fail_orders=False):
    """respond：/quote 固定返回 bars，/order 受理成功。"""
    def respond(request):
        path = request.url.path
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {stock: bars}})
        if path == "/order":
            if fail_orders:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"ok": True})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})
    return respond


# ---- 周期边界判定 ----
def test_periods_on_boundary():
    """C6：1m bar 结束时刻 → 命中边界周期（可累积，只读 stime 不引本机时钟）。"""
    assert periods_on_boundary(datetime(2026, 8, 5, 10, 5)) == ["5m"]
    assert periods_on_boundary(datetime(2026, 8, 5, 10, 15)) == ["5m", "15m"]
    assert periods_on_boundary(datetime(2026, 8, 5, 10, 30)) == ["5m", "15m", "30m"]
    assert periods_on_boundary(datetime(2026, 8, 5, 11, 0)) == ["5m", "15m", "30m", "1h"]
    assert periods_on_boundary(datetime(2026, 8, 5, 10, 3)) == []
    assert periods_on_boundary(None) == []


# ---- 周期过滤（核心节拍正确性）----
def test_on_bar_1m_bar_only_drives_1m_strategy():
    """C6：1m bar(period=1m) 只驱动 1m 策略；5m 策略不被 1m 节拍触发。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    # 两策略同 bar_time 同价都有 open_sig=1
    engine.signal_cache = {
        (1, stock, bar_time): [{"name": "open_sig", "value": 1}],
        (2, stock, bar_time): [{"name": "open_sig", "value": 1}],
    }
    bar = _bar(stock, "9.3", bar_time)
    bar.period = "1m"

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert [o.strategy_id for o in orders] == [1]  # 只有 1m 策略下单
    db.close()


def test_on_bar_5m_bar_only_drives_5m_strategy():
    """C6：5m 边界 bar(period=5m) 只驱动 5m 策略；1m 策略不被 5m 边界触发（防风控单串周期）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    engine.signal_cache = {
        (1, stock, bar_time): [{"name": "open_sig", "value": 1}],
        (2, stock, bar_time): [{"name": "open_sig", "value": 1}],
    }
    bar = _bar(stock, "9.3", bar_time)
    bar.period = "5m"

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert [o.strategy_id for o in orders] == [2]  # 只有 5m 策略下单
    db.close()


def test_on_bar_no_period_processes_all_strategies():
    """C6：bar 无 period（回测/旧调用）→ 处理全部策略（向后兼容）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    engine.signal_cache = {
        (1, stock, bar_time): [{"name": "open_sig", "value": 1}],
        (2, stock, bar_time): [{"name": "open_sig", "value": 1}],
    }
    bar = _bar(stock, "9.3", bar_time)  # period=None

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert sorted(o.strategy_id for o in orders) == [1, 2]
    db.close()


# ---- 边界分发 C6(A) ----
def test_dispatch_period_bar_drives_5m_strategy_only():
    """C6(A)：5m 边界 → 拉 5m bars → 注入信号填 cache(2, code, boundary) → 只 5m 策略下单。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    boundary = datetime(2026, 8, 5, 10, 5)
    bars_5m = [
        {"stime": "20260805100000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805100500", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805101000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, bars_5m))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS"})
    # mock compute_injected：返回 open_sig=1（无论周期）
    engine._tq_formula.compute_injected = lambda **kw: {
        stock: {"open_sig": [{"Date": "20260805", "Value": 1}], "ErrorId": 0}
    }

    engine._dispatch_period_bar("5m", boundary)

    # 信号缓存填了 5m 策略(2)的 key（bar.period=5m → 只注入 5m 策略）
    assert (2, stock, boundary) in engine.signal_cache
    assert (1, stock, boundary) not in engine.signal_cache
    db = factory()
    orders = db.query(LiveOrder).all()
    assert [o.strategy_id for o in orders] == [2]
    db.close()


def test_dispatch_period_bar_uses_latest_completed_bar():
    """C6(A)：BarEvent.stocks 用「最新已完成 bar」（非 forming 最新一根）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    boundary = datetime(2026, 8, 5, 10, 5)
    # forming 最新一根 10:10 的 close=9.8，完成 bar 10:05 close=9.4
    bars_5m = [
        {"stime": "20260805100000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805100500", "open": 9.2, "high": 9.5, "low": 9.2, "close": 9.4, "volume": 10000, "amount": 94000.0},
        {"stime": "20260805101000", "open": 9.4, "high": 9.8, "low": 9.4, "close": 9.8, "volume": 10000, "amount": 98000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, bars_5m))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS"})
    engine._tq_formula.compute_injected = lambda **kw: {
        stock: {"open_sig": [{"Date": "20260805", "Value": 1}], "ErrorId": 0}
    }

    engine._dispatch_period_bar("5m", boundary)

    db = factory()
    orders = db.query(LiveOrder).all()
    # 下单价用完成 bar 10:05 的 close=9.4（而非 forming 10:10 的 9.8）
    assert orders[0].price == Decimal("9.4")
    db.close()


def test_dispatch_period_bar_bridge_offline_sets_offline():
    """C6(A)：分发拉 quote 桥离线 → 置离线返回，不崩。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))
    stock = "600000.SH"
    boundary = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder(fail_paths={"/quote"})
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS"})

    with pytest.raises(BridgeUnavailableError):
        engine._dispatch_period_bar("5m", boundary)


# ---- 日终 1d C6(B) ----
def test_maybe_daily_bars_14_30_drives_1d_strategy():
    """C6(B)：14:30 日终 → 拉 1d 快照 → 注入 1d 策略 → 下单；同日幂等只驱动一次。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1d")
    stock = "600000.SH"
    daily_bars = [
        {"stime": "20260807000000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260810000000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, daily_bars))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS"})
    engine._tq_formula.compute_injected = lambda **kw: {
        stock: {"open_sig": [{"Date": "20260810", "Value": 1}], "ErrorId": 0}
    }
    daily_time = datetime(2026, 8, 10, 0, 0)

    engine._maybe_daily_bars(now=datetime(2026, 8, 10, 14, 30))

    assert (1, stock, daily_time) in engine.signal_cache
    assert engine._last_daily_bar_date == datetime(2026, 8, 10).date()
    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1 and orders[0].strategy_id == 1
    db.close()

    # 同日幂等：14:31 再调不重复驱动
    engine._maybe_daily_bars(now=datetime(2026, 8, 10, 14, 31))
    db = factory()
    assert db.query(LiveOrder).count() == 1
    db.close()


def test_maybe_daily_bars_before_1430_no_trigger():
    """C6(B)：未到 14:30 不触发（不拉快照、不驱动、不记幂等标记）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1d")
    stock = "600000.SH"
    daily_bars = [{"stime": "20260810000000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0}]
    rec = _Recorder(respond=_respond_quote_bars(stock, daily_bars))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS"})

    engine._maybe_daily_bars(now=datetime(2026, 8, 10, 14, 29))

    assert engine._last_daily_bar_date is None
    db = factory()
    assert db.query(LiveOrder).count() == 0
    db.close()


# ---- 1w/1mon 通达信注入 C6(C) ----
def test_inject_startup_periods_fills_signal_cache():
    """C6(C)：1w 启动注入 → TQFormula.compute 通达信 → signal_cache 填 (sid, code, daily_time)。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1w", strategy_id=1)
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS"})
    daily_time = datetime(2026, 8, 10, 0, 0)
    engine._tq_formula.compute = lambda *a, **kw: {
        stock: {"open_sig": [{"Date": "20260810", "Value": 1}], "ErrorId": 0}
    }

    engine._inject_startup_periods(daily_time)

    assert engine.signal_cache[(1, stock, daily_time)] == [{"name": "open_sig", "value": 1}]


def test_maybe_daily_bars_drives_1w_from_prefilled_cache():
    """C6(C)：1w 策略日终驱动命中启动预填信号，不拉桥注入（compute_injected 不调）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1w")
    stock = "600000.SH"
    daily_bars = [{"stime": "20260810000000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0}]
    rec = _Recorder(respond=_respond_quote_bars(stock, daily_bars))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS"})
    daily_time = datetime(2026, 8, 10, 0, 0)
    engine.signal_cache[(1, stock, daily_time)] = [{"name": "open_sig", "value": 1}]
    engine._tq_formula.compute_injected = MagicMock(return_value=None)

    engine._maybe_daily_bars(now=datetime(2026, 8, 10, 14, 30))

    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1 and orders[0].strategy_id == 1
    db.close()
    engine._tq_formula.compute_injected.assert_not_called()  # 1w 不走桥注入


def test_maybe_daily_bars_day_rollover_reinjects_1w():
    """C6(C)：日切后 1w cache miss → 通达信补注入再驱动。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1w")
    stock = "600000.SH"
    daily_bars = [{"stime": "20260811000000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0}]
    rec = _Recorder(respond=_respond_quote_bars(stock, daily_bars))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS"})
    # 启动注入在 08-10，日终落在 08-11 → 新 daily_time cache miss
    old_daily_time = datetime(2026, 8, 10, 0, 0)
    new_daily_time = datetime(2026, 8, 11, 0, 0)
    engine.signal_cache[(1, stock, old_daily_time)] = [{"name": "open_sig", "value": 1}]
    engine._tq_formula.compute = lambda *a, **kw: {
        stock: {"open_sig": [{"Date": "20260811", "Value": 1}], "ErrorId": 0}
    }

    engine._maybe_daily_bars(now=datetime(2026, 8, 11, 14, 30))

    # 日切补注入：新 daily_time 的 cache 已填
    assert (1, stock, new_daily_time) in engine.signal_cache
    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1 and orders[0].strategy_id == 1
    db.close()


# ---- _bars_to_formula_df 时间解析统一（修潜在 bug）----
def test_bars_to_formula_df_accepts_stime_only():
    """C6：stime-only bars（无 index/time）→ 仍能解析时间注入（统一 parse_bar_time）。"""
    bars = [
        {"stime": "20260805100000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805100100", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    df = LiveEngine._bars_to_formula_df(bars, "600000.SH")
    assert df is not None
    assert len(df["Close"]) == 2
    idx = list(df["Close"].index)
    assert idx[0] == datetime(2026, 8, 5, 10, 0, 0)
    assert idx[1] == datetime(2026, 8, 5, 10, 1, 0)


# ---- E8 离线→在线转场 ----
def test_loop_offline_to_online_resets_baseline():
    """E8：桥离线→在线转场 → _loop 调 reset_baseline（重建基线，不补 bar）。"""
    import asyncio

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"

    class FlakyPing:
        """首次 /ping 失败（模拟启动时离线），之后恢复。"""
        def __init__(self):
            self.requests = []
            self.fail_remaining = 1

        def handler(self, request):
            self.requests.append(request)
            path = request.url.path
            if path == "/ping":
                if self.fail_remaining > 0:
                    self.fail_remaining -= 1
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(200, json={"ok": True})
            if path == "/quote":
                return httpx.Response(200, json={"ok": True, "data": {}})
            if path == "/order":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404, json={"ok": False})

    rec = FlakyPing()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    reset_calls = []
    orig_reset = poller.reset_baseline
    poller.reset_baseline = lambda: (reset_calls.append(1), orig_reset())[1]

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, poll_interval=0.01,
    )

    async def run_a_bit():
        await engine.start()
        await asyncio.sleep(0.08)
        await engine.stop()

    asyncio.run(run_a_bit())

    assert engine.bridge_online is True
    assert len(reset_calls) == 1  # 离线→在线只转场一次


# ===========================================================================
# C4 三维去重（#28：拉取 (code,period) + 计算 (code,period,formula)）
# ===========================================================================
def _count_quote(rec):
    """统计 _Recorder 中 /quote 请求数。"""
    return [r for r in rec.requests if r.url.path == "/quote"]


def _counting_compute(engine, stock, value=1):
    """把 engine._tq_formula.compute_injected 换成计数版，返回 dict 计数。"""
    calls = {"n": 0}

    def _compute(**kw):
        calls["n"] += 1
        return {
            "ErrorId": "0",
            stock: {"open_sig": [
                {"Date": "202608051000", "Value": 0.0},
                {"Date": "202608051001", "Value": 0.0},
                {"Date": "202608051002", "Value": float(value)},
            ]},
        }

    engine._tq_formula.compute_injected = _compute
    return calls


def test_on_bar_dedups_quote_and_compute_across_portfolios():
    """C4：#28 跨组合去重——同股票+周期+公式的多组合只拉一次/算一次。

    _on_bar 级共享 df_cache/raw_cache → /quote 只 1 次、compute_injected 只 1 次；
    signal_cache 两策略各有条目（隔离不变）。
    """
    factory, _ = _db_factory()
    port1, _ = _portfolio_single(strategy_id=1)
    port2, _ = _portfolio_single(strategy_id=2)
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port1, port2], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        tq_formula=TQFormula(), formula_by_strategy={1: "MACROSSPRO", 2: "MACROSSPRO"},
    )
    calls = _counting_compute(engine, stock)
    bar = _bar(stock, "9.3", bar_time)

    engine._on_bar(bar)

    assert calls["n"] == 1  # 同 (code,period,formula) 只算一次
    assert len(_count_quote(rec)) == 1  # 同 (code,period) 只拉一次
    for sid in (1, 2):
        out_map = {o["name"]: o["value"] for o in engine.signal_cache[(sid, stock, bar_time)]}
        assert out_map["open_sig"] == 1


def test_fill_signal_cache_dedups_compute_by_formula():
    """C4：#28 计算去重——同 code+period 不同公式 → 拉一次、各算一次（formula 进 raw_cache key）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "1m"))  # 策略 1/2 同周期不同公式
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MACD"})
    calls = _counting_compute(engine, stock)
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    assert calls["n"] == 2  # 两公式各算一次
    assert len(_count_quote(rec)) == 1  # 同 code+period 拉一次
    for sid in (1, 2):
        assert (sid, stock, bar_time) in engine.signal_cache


def test_fill_signal_cache_uses_formula_count():
    """#27→#28：注入 count 来自 Formula.formula_count（按公式配），非全局 200。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(strategy_id=1, period="1m")
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "MA_CROSS"}, formula_count=200,
        formula_count_by_name={"MA_CROSS": 300},
    )
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0", stock: {"open_sig": [{"Date": "202608051000", "Value": 1}]},
    }
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    quotes = _count_quote(rec)
    assert len(quotes) == 1
    assert "count=300" in str(quotes[0].url)  # 用公式级 count，非全局 200


def test_fill_signal_cache_upgrades_quote_count_for_larger_formula():
    """C4：#28 df_cache 升级——同 code+period 两公式 count 200/500 → 大 count 的公式升级重拉。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "1m"))  # 策略1 FORM_A(200) 策略2 FORM_B(500)
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, stock, bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A", 2: "FORM_B"}, formula_count=200,
        formula_count_by_name={"FORM_A": 200, "FORM_B": 500},
    )
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0", stock: {"open_sig": [{"Date": "202608051000", "Value": 1}]},
    }
    bar = _bar(stock, "9.3", bar_time)

    engine._fill_signal_cache(port, bar)

    queries = [str(q.url) for q in _count_quote(rec)]
    assert len(queries) == 2  # 200 后升级重拉 500
    assert any("count=200" in c for c in queries)
    assert any("count=500" in c for c in queries)


def test_dispatch_period_bar_uses_period_max_formula_count():
    """C4：#28 边界分发预拉 count = 该周期策略最大 formula_count（供注入，够最长公式）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))  # 5m 策略 strategy_id=2
    stock = "600000.SH"
    boundary = datetime(2026, 8, 5, 10, 5)
    bars_5m = [
        {"stime": "20260805100000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805100500", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260805101000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, bars_5m))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS"}, formula_count=200,
        formula_count_by_name={"MA_CROSS": 400},
    )
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0", stock: {"open_sig": [{"Date": "20260805", "Value": 1}]},
    }

    engine._dispatch_period_bar("5m", boundary)

    quotes = _count_quote(rec)
    assert len(quotes) == 1
    assert "count=400" in str(quotes[0].url)  # 该周期公式最大 count=400

