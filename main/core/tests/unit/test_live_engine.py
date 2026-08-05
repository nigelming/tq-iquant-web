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
    SessionLocal = sessionmaker(bind=engine)

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
        return httpx.Response(404, json={"ok": False, "error": "unknown"})


def _make_dispatcher(rec, fail_paths=None):
    client = httpx.Client(transport=httpx.MockTransport(rec.handler))
    return HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=client), rec


def _portfolio_single():
    """单组合单策略，formula_signal 配 OPEN（trigger_value=1）。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1m",
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


def _bar(stock, close, bar_time):
    """单股票 OHLCV bar，close 触发信号用。"""
    return BarEvent(
        stocks={stock: {
            "open": Decimal("9.0"), "high": Decimal(close),
            "low": Decimal("9.0"), "close": Decimal(close), "volume": 10000,
        }},
        bar_time=bar_time,
    )


# ---------------- _handle_bar 下单落库 ----------------
def test_handle_bar_signal_to_trade_persisted():
    """OPEN 信号 → BUY 成交 → live_trades/live_orders 各落一行，现金扣减、持仓增加。"""
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
    assert len(trades) == 1
    assert len(orders) == 1
    assert trades[0].trade_type == "buy"
    # 下单量由 _signal_to_order 计算：int(0.1 * 60000 / 9.3 / 100) * 100 = 600
    assert trades[0].quantity == 600
    assert trades[0].live_order_id == orders[0].id
    assert orders[0].status == "accepted"
    assert orders[0].signal_name == "open_sig"
    # 持仓 + 现金：account.apply_trade 已扣现金(amount+佣金)，pos.quantity 增加
    assert ctx.positions[stock].quantity == 600
    # amount = 9.3 * 600 = 5580，佣金/印花税首期为 0
    assert port.account.cash == Decimal("100000") - Decimal("5580")
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
def test_live_t1_checker_returns_full_quantity():
    """实盘首期：持仓全量可卖（真实 T+1 交券商端）。"""
    pos = Position("600000.SH")
    pos.buy(100, Decimal("9.0"), datetime(2026, 8, 5, 10, 0))
    checker = LiveT1Checker()
    # 即便当日买入，实盘首期也返回全量可卖
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 100


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
    """_handle_bar 下单时桥抛 BridgeUnavailableError → 中断、不落库、bridge_online=False。"""
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
    """_handle_bar 接入 _fill_signal_cache：mock 公式返回 open_sig=1 → BUY 落库（非预置 cache）。"""
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
    assert len(trades) == 1
    assert trades[0].trade_type == "buy"
    # signal_name 来自公式信号 open_sig（非风控）
    orders = db.query(LiveOrder).all()
    assert orders[0].signal_name == "open_sig"
    db.close()

