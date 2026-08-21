"""LiveEngine 单测（0009 切片4）— Mock dispatcher（httpx MockTransport）+ 内存 SQLite。

验证实盘端到端链路：bar → 信号 → 真实下单（桥受理）→ 落 live_orders/live_trades，
以及 Core 重启后从 live_trades 恢复虚拟持仓/虚拟现金。引擎核心（Portfolio/ExecutionEngine）
复用回测逻辑，仅注入 HttpBridgeDispatcher + LiveT1Checker。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.engine.account import Account
from core.engine.bar_poller import parse_bar_time
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
from core.models import Base, LiveOrder, LiveTrade, LiveSessionPortfolio
from tq_iquant_shared.constants import SignalType, TradeType


# ---------------- 共用辅助 ----------------
def _db_factory():
    """内存 SQLite Session 工厂：返回 () -> Session，引擎每根 bar 取一个独立 Session。

    StaticPool + check_same_thread=False：审计 #3 后 _loop/_deals_loop 的 tick 在
    单 worker 线程执行，_poll_deals 在 worker 线程访问 DB。默认 :memory: 每连接一个
    独立库 → 跨线程新连接看不到建表；StaticPool 复用同一连接、check_same_thread=False
    允许跨线程使用，让 worker 线程的 Session 见到同一份表数据。单线程测试行为不变。
    """
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_handle_bar_buy_rejected_by_cap_logs_info(caplog):
    """⑤ 实盘 cap_quantity 返回 None（资金不足不足1手）→ logger.info。

    实盘专用路径（回测走 engine.execute 不经此），INFO 级别。账户 cash 清零
    使 approve_order 不足1手拒绝 → cap_quantity None → 不下单 + 打日志。
    """
    import logging
    factory, _ = _db_factory()
    port, ctx = _portfolio_single(strategy_id=1, period="1m")
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    port.account.cash = Decimal("0")  # 现金清零 → 不足1手拒绝

    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.0", bar_time)

    with caplog.at_level(logging.INFO, logger="core.engine.live_engine"):
        engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert orders == []  # 不下单
    assert any(
        r.levelno == logging.INFO for r in caplog.records
    ), "cap_quantity None 拦截应打 info 日志"
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


# 测试用确定性 oid（模拟 Core 下发的 bridge_order_id，前 20 位 = m_strRemark）。
_TEST_OID = "a" * 32
_TEST_REMARK = _TEST_OID[:20]


def _submitted_order(db, quantity=600, status="submitted", bridge_order_id=_TEST_OID):
    """预置一笔 submitted 状态的 LiveOrder（模拟 _handle_bar 已发单）。

    bridge_order_id 默认 _TEST_OID：真实链路下单后必回写 oid，匹配器据此走 remark
    精确匹配。测遗留/模糊路径时显式传 None。
    """
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="600000.SH", trade_type="buy", order_type="limit",
        price=Decimal("9.3"), quantity=quantity, filled_quantity=0,
        filled_price=None, status=status, signal_name="open_sig",
        signal_type="OPEN", bar_time=datetime(2026, 8, 5, 10, 0),
        bridge_order_id=bridge_order_id,
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
        "remark": _TEST_REMARK,
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
        "remark": _TEST_REMARK,
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
        "remark": _TEST_REMARK,
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref == "ref-found"
    db.close()


def test_try_match_order_ref_does_not_collide_same_code_dir_volume():
    """回归：同股票同向同量的多笔在途单，order_ref 匹配不得撞同一个 ref。

    实盘根因：1m 公式持续发 OPEN，连续多笔买单代码/方向/委托量完全相同，
    _try_match_order_ref 取第一个匹配项 → 多笔 LiveOrder 共用同一 order_ref →
    同一笔成交回填到所有单 → apply_trade 重复执行 → 虚拟持仓虚高。
    修复：候选按 insert_date+insert_time 取最新，且排除已被本 session 其他单占用的 ref。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    # 两笔同代码同向同量的 BRIDGE 委托，但 remark（= 各单 oid 前缀）不同：
    # remark 精确匹配天然把两单各认各的，不可能撞同一个 order_ref。
    oid1, oid2 = "b" * 32, "c" * 32
    _mock_orders_deals(rec, orders=[
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-old",
         "remark": oid1[:20],
         "insert_date": "20260805", "insert_time": "095200"},
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-new",
         "remark": oid2[:20],
         "insert_date": "20260805", "insert_time": "095300"},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo1 = _submitted_order(db, quantity=600, bridge_order_id=oid1)
    lo2 = _submitted_order(db, quantity=600, bridge_order_id=oid2)
    db.commit()

    # 经 _poll_deals 一轮匹配（无成交，仅回写 order_ref）
    engine._poll_deals()

    db.refresh(lo1)
    db.refresh(lo2)
    assert lo1.order_ref is not None
    assert lo2.order_ref is not None
    assert lo1.order_ref != lo2.order_ref, "两笔在途单不得共用同一 order_ref"
    assert {lo1.order_ref, lo2.order_ref} == {"ref-old", "ref-new"}
    db.close()


def test_match_remark_exact_ignores_legacy_same_code_dir_volume():
    """真机回归（2026-08-13）：新单不得匹配到跨会话遗留的同代码同向同量旧单。

    场景：本单 11:21（bridge_order_id 已回写 → 走 remark 精确匹配），桥 /orders 里
    有一笔上个会话 09:55 的遗留单（同代码同向同量、无 remark）。旧逻辑按 code+dir+vol
    模糊匹配 + “取最新”，在真单尚未可查时把新单绑到旧单 → 回填错误成交、真单丢失。
    修复：remark 精确匹配只认 m_strRemark == oid 前缀的委托；无 remark 的遗留单被忽略。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[
        # 上个会话遗留单：同代码同向同量、无 remark，时间早于本单
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-legacy",
         "insert_date": "20260813", "insert_time": "095500"},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)  # bridge_order_id=_TEST_OID
    lo.created_at = datetime(2026, 8, 13, 3, 21, 0)  # UTC = 北京 11:21
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref is None  # 遗留单无 remark，绝不绑定
    db.close()


def test_match_remark_exact_binds_correct_order_among_similar():
    """remark 精确匹配在多笔相似委托中认领到本单（即便委托量不同也能认到）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[
        # 别的单的委托（带它自己的 remark），代码方向相同但量不同
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 999, "order_ref": "ref-other",
         "remark": "z" * 20},
        # 本单的委托：remark 命中
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-mine",
         "remark": _TEST_REMARK},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref == "ref-mine"
    db.close()


def test_match_legacy_fuzzy_excludes_candidate_before_creation():
    """遗留单（无 bridge_order_id）走模糊匹配，但 insert 早于本单创建的候选必须排除。

    真机 bug 的等价场景：新单 11:21 创建时，/orders 里只有上个会话 09:55 的同代码同向
    同量遗留单（真单尚未可查）。旧逻辑“取最新匹配项”会把新单绑到这笔旧单。时间窗要求
    候选 insert >= 本单 created_at（容差），故此时 order_ref 留 None，下轮真单出现再绑。
    insert_date/time 是 Asia/Shanghai 本地时间，created_at 是 UTC naive，匹配器内部换算。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[
        # 太早：本地 09:55（= UTC 01:55）< 本单 UTC 03:21 → 排除，不绑定
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-too-old",
         "insert_date": "20260813", "insert_time": "095500"},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600, bridge_order_id=None)  # 遗留单，无 remark
    lo.created_at = datetime(2026, 8, 13, 3, 21, 0)  # UTC = 北京 11:21
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref is None  # 早于本单的遗留单绝不绑定
    db.close()


def test_match_legacy_fuzzy_binds_recent_candidate():
    """遗留单模糊匹配：insert 晚于本单创建（容差内）的候选正常绑定。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[
        # 本地 11:22（= UTC 03:22）>= 本单 UTC 03:21 - 容差 → 命中
        {"source": "BRIDGE", "instrument": "600000", "exchange": "SH",
         "direction": 48, "volume": 600, "order_ref": "ref-recent",
         "insert_date": "20260813", "insert_time": "112200"},
    ])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600, bridge_order_id=None)
    lo.created_at = datetime(2026, 8, 13, 3, 21, 0)  # UTC = 北京 11:21
    db.commit()

    engine._try_match_order_ref(lo)

    assert lo.order_ref == "ref-recent"
    db.close()


def test_poll_deals_expires_stale_order_without_order_ref():
    """order_ref 始终匹配不到（桥 /orders 无此单）的陈旧 submitted 单 → 超时置 rejected。

    回归：passorder 受理后桥侧从未出现该委托（被 iQuant 静默丢弃/拒单），Core 端
    order_ref 永远为 None，旧逻辑无限轮询。超过阈值（默认 180s）判定失效，置 rejected
    + error_message 并移出 pending。fresh 单（未超时）保持 submitted 不动。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[], deals=[])  # 桥侧无任何匹配委托
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    stale = _submitted_order(db, quantity=600)
    stale.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    fresh = _submitted_order(db, quantity=700)
    fresh.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.close()

    engine._poll_deals()

    db = factory()
    stale2 = db.get(LiveOrder, stale.id)
    fresh2 = db.get(LiveOrder, fresh.id)
    assert stale2.status == "rejected"
    assert stale2.order_ref is None
    assert stale2.error_message  # 有失效原因
    assert fresh2.status == "submitted"  # 未超时不动
    # 陈旧单已移出在途集合
    pending = db.query(LiveOrder).filter(
        LiveOrder.live_session_id == 1,
        LiveOrder.status.in_(["submitted", "partial"]),
    ).all()
    assert [o.id for o in pending] == [fresh.id]
    db.close()


def test_poll_deals_backfills_via_remark_when_order_ref_missing():
    """order_ref 匹配不上（/orders 实时表已无此单）但 /deals 有 remark 匹配成交 → 回填。

    真机 2026-08-19 id45/46 根因：iQuant get_trade_detail_data(ORDER) 对已成交单不可靠
    （成交后从 ORDER 实时表移除），Core 轮询 /orders 拿不到 order_ref → 旧逻辑走到超时
    rejected 且成交不回填。但 /deals（DEAL 表）保留全部已成交记录，且 DEAL 对象也带
    m_strRemark。本测试验证修复 A：order_ref 匹配失败的单，按 bridge_order_id[:20] 在
    /deals 直连 remark 匹配回填，绕过 order_ref。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    # /orders 不返回本单（模拟成交后已从 ORDER 实时表移除 → order_ref 永远匹配不上）
    # /deals 返回本单成交，remark = bridge_order_id[:20]（DEAL 对象带 m_strRemark）
    _mock_orders_deals(rec, orders=[], deals=[{
        "order_ref": "ref-never-matched",  # 真实 order_ref，但 Core 拿不到（/orders 无此单）
        "remark": _TEST_REMARK,            # 按 remark 直连匹配
        "price": 9.25, "volume": 600,
        "amount": 5550.0, "commission": 1.39,
        "trade_time": "150001", "trade_date": "20260805",
    }])
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)  # bridge_order_id=_TEST_OID, remark=_TEST_REMARK
    db.commit()
    db.close()

    engine._poll_deals()

    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    # 修复 A：order_ref 匹配不上，但按 remark 从 /deals 直连回填 → filled
    assert lo2.status == "filled"
    assert lo2.filled_quantity == 600
    assert lo2.filled_price == Decimal("9.25")
    trades = db.query(LiveTrade).all()
    assert len(trades) == 1
    assert trades[0].quantity == 600
    assert trades[0].amount == Decimal("5550.0")
    # filled 后 apply：持仓 600，现金扣 amount+commission（同正常回填路径）
    assert ctx.positions[stock].quantity == 600
    assert port.account.cash == Decimal("100000") - (Decimal("5550.0") + Decimal("1.39"))
    db.close()


def test_poll_deals_remark_backfill_partial_then_filled_across_rounds():
    """慢成交单：第1轮 /deals 只有部分成交(partial) → 第2轮补齐 → filled。

    真机 id45 场景：限价单 17 分钟才补完，前 4800 股先成交。修复 A 让每轮按 remark
    从 /deals 回填，partial 不 apply，补齐后 filled + apply。不依赖 order_ref、不超时。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    # /orders 始终无此单；/deals 第1轮 300 股，第2轮补到 600
    deals_round1 = [{
        "order_ref": "ref-slow", "remark": _TEST_REMARK,
        "price": 9.25, "volume": 300, "amount": 2775.0, "commission": 0.69,
        "trade_time": "150001", "trade_date": "20260805",
    }]
    deals_round2 = [{
        "order_ref": "ref-slow", "remark": _TEST_REMARK,
        "price": 9.25, "volume": 600, "amount": 5550.0, "commission": 1.39,
        "trade_time": "150002", "trade_date": "20260805",
    }]
    _mock_orders_deals(rec, orders=[], deals=deals_round1)
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    lo = _submitted_order(db, quantity=600)
    db.commit()
    db.close()

    # 第1轮：partial（300/600），不 apply
    engine._poll_deals()
    db = factory()
    lo2 = db.get(LiveOrder, lo.id)
    assert lo2.status == "partial"
    assert lo2.filled_quantity == 300
    assert ctx.positions.get(stock) is None or ctx.positions[stock].quantity == 0
    db.close()

    # 第2轮：/deals 补齐到 600 → filled + apply
    _mock_orders_deals(rec, orders=[], deals=deals_round2)
    engine._poll_deals()
    db = factory()
    lo3 = db.get(LiveOrder, lo.id)
    assert lo3.status == "filled"
    assert lo3.filled_quantity == 600
    assert ctx.positions[stock].quantity == 600
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
        "remark": _TEST_REMARK,
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


# ---------------- 跨重启去重门（D6：BUY 同 bar_time 已有未完结单则跳过）----------------
def _seed_buy_submitted(db, bar_time, status="submitted", portfolio_strategy_id=1, strategy_id=1, trade_type="buy"):
    """预置一笔 LiveOrder（模拟重启前同一 bar 已下过的单）。trade_type 可 buy/sell。"""
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=portfolio_strategy_id, strategy_id=strategy_id,
        stock_code="600000.SH", trade_type=trade_type, order_type="limit",
        price=Decimal("9.3"), quantity=600, filled_quantity=0,
        filled_price=None, status=status, signal_name="open_sig",
        signal_type="OPEN", bar_time=bar_time,
    )
    db.add(lo)
    db.flush()
    return lo


def test_handle_bar_dup_buy_same_bar_time_skipped():
    """D6：BUY 在同一 bar_time 已有 submitted LiveOrder → 重启后重驱到同一 bar 不重复下单。

    根因：页面停止/再启动 = Core 重启 = 新 LiveEngine 实例内存丢失，1d/1w/1mon
    被重新驱动到同一 daily_time，且 order_id=live_order.id（自增 PK）每次新号，
    桥侧 _placed 幂等无效。按 (组合/股票/bar_time/buy) 去重 → 重启不重复。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置：重启前该 bar 已下过 BUY（submitted）
    db = factory()
    _seed_buy_submitted(db, bar_time, status="submitted")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    # 仍只有预置的那一笔，未新增
    assert db.query(LiveOrder).count() == 1
    # 桥未收到新的 /order 请求
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 0
    db.close()


def test_handle_bar_dup_buy_different_bar_time_allowed():
    """在途单门安全侧：上一单已确认（filled）→ 不同 bar_time 的 BUY 不被误杀。

    bar_time 本身不是拦截依据——分钟策略日内多 bar 多次开仓正常。
    在途单门只拦「同股同向未确认」单（submitted/partial）；上一单已成交（filled）
    或已拒（rejected）→ 门释放，不同 bar_time 的信号照常下单。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time_first = datetime(2026, 8, 5, 10, 0)
    bar_time_next = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置：10:00 的 BUY 已成交回填（filled）——在途集合外，不拦 10:05
    db = factory()
    _seed_buy_submitted(db, bar_time_first, status="filled")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    # 10:05 的信号（不同 bar_time）
    engine.signal_cache = {(1, stock, bar_time_next): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time_next)

    engine._handle_bar(port, bar)

    db = factory()
    # 预置 1 笔 + 新增 1 笔 = 2 笔
    assert db.query(LiveOrder).count() == 2
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 1  # 桥收到新单
    db.close()


def test_handle_bar_sell_not_blocked_by_existing_buy():
    """D6 跨方向：SELL 不被同 bar 的 BUY 单拦截——方向是去重键的一部分。

    同 bar 已有 BUY 单不影响同 bar 的 SELL（不同 trade_type 不匹配）。
    平仓与开仓各自独立去重，互不干扰。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single(strategy_id=1, period="1m")
    ctx.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1},
    ]
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    # 虚拟持仓 600（可卖）
    ctx.positions[stock] = Position(stock)
    ctx.positions[stock].buy(600, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))

    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": 600, "volume": 600,
                 "yesterday_volume": 600, "on_road_volume": 0},
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

    # 预置：同 bar 已有一笔 BUY submitted（不该拦住 SELL——方向不同）
    db = factory()
    _seed_buy_submitted(db, bar_time, status="submitted")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "close_sig", "value": -1}]}
    bar = _bar(stock, "9.0", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    # 预置 1 笔 BUY + 新增 1 笔 SELL
    assert len(orders) == 2
    sell_orders = [o for o in orders if o.trade_type == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0].status == "submitted"
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 1  # SELL 单发到桥
    db.close()


def test_handle_bar_dup_sell_same_bar_time_skipped():
    """D6 对称：SELL 在同一 bar_time 已有未完结单 → 重启后重驱到同一 bar 不重复卖。

    重复 SELL 危害不亚于重复 BUY：已卖完再卖 → 桥超卖拒单噪音 / 持仓误判。
    BUY/SELL 对称去重，按 (策略+股票+bar_time+方向) 拦同方向重驱。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single(strategy_id=1, period="1m")
    ctx.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1},
    ]
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    # 虚拟持仓 600（可卖）
    ctx.positions[stock] = Position(stock)
    ctx.positions[stock].buy(600, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))

    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": 600, "volume": 600,
                 "yesterday_volume": 600, "on_road_volume": 0},
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

    # 预置：重启前该 bar 已下过 SELL（submitted）
    db = factory()
    _seed_buy_submitted(db, bar_time, status="submitted", trade_type="sell")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "close_sig", "value": -1}]}
    bar = _bar(stock, "9.0", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    # 仍只有预置的那一笔 SELL，未新增
    assert db.query(LiveOrder).count() == 1
    # 桥未收到新的 /order 请求
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 0
    db.close()


def test_handle_bar_buy_blocked_by_inflight_buy_next_bar():
    """在途单门：同 (策略,股票) 已有 submitted 买单 → 下一根 bar 的 OPEN 被压掉。

    根因（真机 2026-08-13，159888/159929/159936）：下单→/deals 成交回填之间存在
    在途窗口，期间虚拟持仓仍是 0，下一根连续 bar 的 OPEN 信号看持仓=0 以为可开仓
    → 同股同向重复买单（信号却记成 OPEN）。在途单门按「同股同向未确认单
    （submitted/partial）」拦截；回填确认后由 portfolio.py 持仓守卫接管。
    bar_time 不同不构成放行理由——不同 bar 只是两次信号时点，上一单未确认就再买
    仍是重复。方向同向才拦：在途 BUY 不拦 SELL。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time_first = datetime(2026, 8, 5, 10, 0)
    bar_time_next = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置：10:00 已下过 BUY，仍在途（submitted，成交未回填）
    db = factory()
    _seed_buy_submitted(db, bar_time_first, status="submitted")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    # 10:05 的信号（不同 bar_time，但上一单还在途）
    engine.signal_cache = {(1, stock, bar_time_next): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time_next)

    engine._handle_bar(port, bar)

    db = factory()
    # 仍只有预置的那一笔，未新增
    assert db.query(LiveOrder).count() == 1
    # 桥未收到新的 /order 请求
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 0
    db.close()


def test_handle_bar_sell_blocked_by_inflight_sell_next_bar():
    """在途单门对称：同 (策略,股票) 已有 submitted 卖单 → 下一根 bar 的 SELL 被压掉。

    与 BUY 同理（用户要求卖出同样处理）：上一笔 SELL 未回填前虚拟持仓未减，
    下一根 bar 的 CLOSE 信号看到持仓仍满 → 同量再卖 → 超卖/桥拒单噪音。
    方向同向才拦：在途 SELL 不拦 BUY、在途 BUY 不拦 SELL。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single(strategy_id=1, period="1m")
    ctx.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1},
    ]
    stock = "600000.SH"
    bar_time_first = datetime(2026, 8, 5, 10, 0)
    bar_time_next = datetime(2026, 8, 5, 10, 5)
    # 虚拟持仓 600（可卖）
    ctx.positions[stock] = Position(stock)
    ctx.positions[stock].buy(600, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))

    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": 600, "volume": 600,
                 "yesterday_volume": 600, "on_road_volume": 0},
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

    # 预置：10:00 已下过 SELL，仍在途
    db = factory()
    _seed_buy_submitted(db, bar_time_first, status="submitted", trade_type="sell")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time_next): [{"name": "close_sig", "value": -1}]}
    bar = _bar(stock, "9.0", bar_time_next)

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    # 仍只有预置的那一笔 SELL，未新增
    assert len(orders) == 1
    assert orders[0].trade_type == "sell"
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 0
    db.close()


def test_handle_bar_buy_allowed_after_inflight_rejected():
    """在途单门释放：上一单 rejected（桥拒单/离线）→ 门不拦，新 OPEN 正常下单。

    status=rejected 不在在途集合（submitted/partial）：被拒的单不会成交建仓，
    必须允许重试，否则同一股票会被一纸拒单卡死无法再买。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    bar_time_first = datetime(2026, 8, 5, 10, 0)
    bar_time_next = datetime(2026, 8, 5, 10, 5)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置：10:00 的 BUY 已被桥拒（rejected）
    db = factory()
    _seed_buy_submitted(db, bar_time_first, status="rejected")
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time_next): [{"name": "open_sig", "value": 1}]}
    bar = _bar(stock, "9.3", bar_time_next)

    engine._handle_bar(port, bar)

    db = factory()
    # 预置 1 笔（rejected）+ 新增 1 笔 = 2 笔
    assert db.query(LiveOrder).count() == 2
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 1  # 桥收到新单
    db.close()


def test_handle_bar_dup_buy_different_strategy_same_bar_allowed():
    """D6 安全侧：同组合/同股票/同 bar，不同策略各自下单不被误杀。

    去重键含 strategy_id：主从策略 / 多周期策略可能同 bar 同股各自开仓，
    仅 (组合+股票+bar) 去重会误拦第二个策略。必须加 strategy_id 才精确到
    「同一策略重驱到同一 bar」这一真正的重复场景。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "1m"))  # 同组合两策略，均 1m
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)

    # 预置：策略 1 该 bar 已下过 BUY
    db = factory()
    _seed_buy_submitted(db, bar_time, status="submitted", strategy_id=1)
    db.commit()
    db.close()

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    # 两个策略同 bar 都触发信号
    engine.signal_cache = {
        (1, stock, bar_time): [{"name": "open_sig", "value": 1}],
        (2, stock, bar_time): [{"name": "open_sig", "value": 1}],
    }
    bar = _bar(stock, "9.3", bar_time)  # period=None → 两策略都处理

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    # 预置 1 笔（策略1）+ 新增 1 笔（策略2）；策略1 重复被跳过，策略2 放行
    assert sorted(o.strategy_id for o in orders) == [1, 2]
    placed = [r for r in rec.requests if r.url.path == "/order"]
    assert len(placed) == 1  # 仅策略2 发单
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
        "remark": _TEST_REMARK,
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


def test_maybe_daily_bars_1d_uses_code_period_count_and_dedup():
    """C6(B)：日终 1d 拉取规则同启动预热——长度按 (code,'1d') 的 _code_period_count、去重按 stime。

    桥端可能返回同 stime 重复 bar（历史/拼接重叠）；_sort_and_cap 去重 + 截断后
    注入公式的窗口 = count 根唯一 bar，而非原样塞进公式（同 _preheat 语义）。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1d")
    stock = "600000.SH"
    # 4 根含 1 根重复 stime；_code_period_count[(code,'1d')]=3 → 去重后截断为 3 根唯一 bar
    daily_bars = [
        {"stime": "20260806000000", "open": 9.0, "high": 9.2, "low": 8.9, "close": 9.1, "volume": 10000, "amount": 91000.0},
        {"stime": "20260807000000", "open": 9.1, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260807000000", "open": 9.0, "high": 9.2, "low": 8.9, "close": 9.0, "volume": 10000, "amount": 90000.0},  # dup stime
        {"stime": "20260810000000", "open": 9.2, "high": 9.5, "low": 9.1, "close": 9.4, "volume": 10000, "amount": 94000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, daily_bars))
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    from core.tq.formula import TQFormula
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        tq_formula=TQFormula(), formula_by_strategy={1: "MA_CROSS"},
        formula_count=200, formula_count_by_name={"MA_CROSS": 3},
        code_period_count={(stock, "1d"): 3},
    )
    captured = {}

    def spy(**kw):
        captured["df"] = kw.get("ohlcv_df")
        return {stock: {"open_sig": [{"Date": "20260810", "Value": 1}], "ErrorId": 0}}

    engine._tq_formula.compute_injected = spy

    engine._maybe_daily_bars(now=datetime(2026, 8, 10, 14, 30))

    # 长度规则：/quote 按 (code,'1d') 的 _code_period_count=3 拉（非周期级 _period_count/全局 200）
    assert any("period=1d" in str(r.url) and "count=3" in str(r.url) for r in rec.requests), \
        "日终 1d 拉取 count 应取该股该周期 _code_period_count"
    # 去重规则：注入公式的 df 行数 = 3 根唯一 bar（4 根去 1 重复 stime 再截断到 3）
    df = captured.get("df")
    assert df is not None and len(df["Close"]) == 3


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
        # 审计 #37：原固定 sleep(0.08) 在调度抖动下偶发不足 1 次 reset_baseline。
        # 改为有界轮询：轮询 reset_calls 直到离线→在线转场完成或超时（同 :1831-1839 先例）。
        deadline = asyncio.get_event_loop().time() + 0.5
        while len(reset_calls) < 1 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.005)
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


def test_on_bar_1m_inject_reuses_poller_bars_merge_cache_no_fetch():
    """优化：BarPoller 透传的本轮 1m bars 经 _on_bar → 注入并入预热缓存复用，零额外拉取。

    优化前：BarPoller 拉 count=10 判完成即弃，注入走 _get_bars_with_increment 增量
    再拉 count=10（双份冗余）；且 _fetch_cached_bars 遇 bars_by_code 无脑复用——拿
    3 根喂 200 根窗口公式（长均线 NaN 静默失效，真实缺陷）。优化后：本轮已拉 bars
    并入预热缓存（启动预热 code_period_count 根历史）→ 复用完整窗口，本轮零 /quote。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1m")
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 2)
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    provided = _quote_bars(stock, [datetime(2026, 8, 5, 10, i) for i in range(3)])  # BarPoller 本轮拉 3 根
    # 预热历史：200 根到 09:59 止（升序，provided 10:00-10:02 更新）
    hist = _quote_bars(stock, [datetime(2026, 8, 5, 9, 59) - timedelta(minutes=i) for i in range(200)])
    _mock_dispatcher_with_quote(rec, stock, hist)   # 兜底：任何 fallback 拉取也有数据
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "MA_CROSS"}, formula_count=200,
    )
    engine._preheat_cache[("600000.SH", "1m")] = engine._make_cache_entry(hist, 200)

    received = {}
    def _compute(**kw):
        df = kw.get("ohlcv_df") or {}
        received["rows"] = len(df["Close"]) if "Close" in df else 0
        return {"ErrorId": "0", stock: {"open_sig": [{"Date": "202608051002", "Value": 1}]}}
    engine._tq_formula.compute_injected = _compute

    bar = _bar(stock, "9.3", bar_time)
    bar.period = "1m"
    bar.bars_by_code = {stock: provided}   # BarPoller 本轮透传
    engine._on_bar(bar)

    # 公式拿到完整窗口（200 根）——并入预热缓存后复用，不是拿 3 根裸复用
    assert received["rows"] == 200
    # 本轮注入零额外 /quote 拉取（BarPoller 自身的拉取由 poll 外部计数）
    assert _count_quote(rec) == []
    # 信号仍正确填充（窗口完整 → 公式算出信号）
    assert (1, stock, bar_time) in engine.signal_cache


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


def test_dispatch_period_bar_skips_period_no_strategy_uses():
    """边界 guard：实例无 15m 策略时 _dispatch_period_bar('15m') 直接 return，不拉桥、不下单。

    periods_on_boundary 是纯算术（minute%15==0 → '15m'），不查实例有无 15m 策略。
    真机日志：实例只有 1m/5m/30m，14:30 却白拉 17 只 15m count=200（拉完 period 过滤全跳过）。
    guard 按 _strategy_periods 过滤——实例无该周期策略 → 0 次 /quote、0 订单。
    """
    factory, _ = _db_factory()
    # 实例周期 = 1m/5m/30m（对齐真机 sec 实例，无 15m）
    port, _ = _portfolio_two(periods=("1m", "5m"))
    # 再加一个 30m 策略 ctx，确保 _strategy_periods = {1m,5m,30m}，15m 仍不在内
    pm = port.risk_manager
    ctx30 = StrategyContext(
        strategy_id=3, period="30m",
        capital_ratio=Decimal("0.6"), max_positions=5,
        single_open_ratio=Decimal("0.1"),
    )
    ctx30.formula_signals = []
    ctx30.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"),
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx30)
    stock = "600000.SH"
    boundary = datetime(2026, 8, 12, 14, 30)  # 14:30 → periods_on_boundary 含 15m
    bars_15m = [
        {"stime": "20260812140000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260812141500", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260812143000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, bars_15m))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS", 3: "MA_CROSS"})

    engine._dispatch_period_bar("15m", boundary)

    # guard 拦截：未拉任何 15m quote、未产生订单
    assert _count_quote(rec) == []
    db = factory()
    assert db.query(LiveOrder).all() == []
    db.close()


def test_on_bar_dispatches_boundary_once_for_same_bar_time():
    """边界去重：同根 1m bar（14:30）被两轮 poll 触发时，周期边界只分发一次。

    BarPoller 按 code 独立判定完成——慢股票在下一轮 poll 才完成同一 stime
    （真机日志：14:30 的 15m 白拉两次，第二次即此重复分发）。_dispatched_boundaries
    挡掉第二次 _on_bar（同 bar_time）→ 不再拉周期 quote、不再重复求值周期策略。
    1m 策略按当轮 bar.stocks 逐股求值，不受此 guard 影响（测试单股票简化，不断言 1m 路径）。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("1m", "5m"))  # 14:30 边界 → 5m 分发（15m/30m 被周期 guard 挡）
    stock = "600000.SH"
    boundary = datetime(2026, 8, 12, 14, 30)
    bars_5m = [
        {"stime": "20260812142500", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
        {"stime": "20260812143000", "open": 9.0, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 10000, "amount": 92000.0},
    ]
    rec = _Recorder(respond=_respond_quote_bars(stock, bars_5m))
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(disp, factory, port, {1: "MA_CROSS", 2: "MA_CROSS"})
    engine._tq_formula.compute_injected = lambda **kw: {
        "ErrorId": "0", stock: {"open_sig": [{"Date": "20260812", "Value": 1}]},
    }

    def _period_quotes(p):
        return [r for r in rec.requests if r.url.path == "/quote" and "period=%s" % p in str(r.url)]

    # bar.period="1m" → _fill_signal_cache 只注入 1m 策略，5m quote 全部来自边界分发
    # （否则 period=None 放行所有策略，1m bar 也注入 5m 公式数据，污染计数）。
    bar1 = _bar(stock, "9.3", boundary)
    bar1.period = "1m"
    engine._on_bar(bar1)  # 第一轮 poll：快股票完成 14:30 → 5m 边界分发一次
    five_after_first = len(_period_quotes("5m"))
    assert five_after_first == 1

    # 第二轮 poll：慢股票再次完成 14:30（同 bar_time）→ 边界不再重复分发
    bar2 = _bar(stock, "9.3", boundary)
    bar2.period = "1m"
    engine._on_bar(bar2)
    assert len(_period_quotes("5m")) == five_after_first


# ---------------- 预热 + 增量拼接（拉取优化）----------------
def test_preheat_pulls_each_code_period_once_with_code_period_count():
    """预热：遍历 _code_period_count，每 (code,period) 拉一次 code_period_count 根，
    跳过 1d/1w/1mon。按需不浪费——只拉实际有策略的 (code,period)。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_two(periods=("5m", "5m"))  # 两 5m 策略
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A", 2: "FORM_B"}, formula_count=200,
        formula_count_by_name={"FORM_A": 200, "FORM_B": 250},
    )
    # 模拟 _build_engine 算好的 (code,period)->max：600000.SH·5m = max(200,250)=250
    engine._code_period_count = {("600000.SH", "5m"): 250}

    engine._preheat()

    quotes = _count_quote(rec)
    assert len(quotes) == 1                      # 该 (code,period) 只拉一次
    assert "count=250" in str(quotes[0].url)     # 取两公式最大值 250
    assert ("600000.SH", "5m") in engine._preheat_cache
    entry = engine._preheat_cache[("600000.SH", "5m")]
    assert entry["count"] == 250
    assert entry["last_stime"] == datetime(2026, 8, 5, 10, 4)  # 最新 bar stime


def test_preheat_skips_1d_1w_1mon():
    """预热跳过 1d/1w/1mon（1d 走日终、1w/1mon 走通达信），不拉桥。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="1d")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5) - timedelta(days=i) for i in range(3)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    engine._code_period_count = {("600000.SH", "1d"): 200, ("600000.SH", "1w"): 200}

    engine._preheat()

    assert len(_count_quote(rec)) == 0           # 1d/1w 都跳过，零拉取
    assert engine._preheat_cache == {}


def test_preheat_single_code_failure_does_not_block_others():
    """预热单 (code,period) 失败不阻断——其余正常入缓存，失败 key 运行期自愈。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder(fail_paths={"/quote"})  # /quote 连接失败
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    engine._code_period_count = {("600000.SH", "5m"): 200}

    engine._preheat()  # 不抛异常

    assert engine._preheat_cache == {}           # 失败 → 未入缓存，运行期走全量补


def test_get_bars_with_increment_miss_full_pull_and_backfill():
    """缓存未命中：全量拉 count 根 + 回填缓存（异常/首次路径）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    assert engine._preheat_cache == {}           # 未预热

    out = engine._get_bars_with_increment("600000.SH", "5m", 200)

    quotes = _count_quote(rec)
    assert len(quotes) == 1
    assert "count=200" in str(quotes[0].url)     # 全量拉 200
    assert ("600000.SH", "5m") in engine._preheat_cache  # 回填
    assert len(out) == 5


def test_get_bars_with_increment_hit_no_new_bar_returns_cache():
    """缓存命中 + 增量拉无新 bar：直接返缓存，不拼接（最省）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)  # 增量拉也返同样 5 根（无新 bar）
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    engine._preheat_cache[("600000.SH", "5m")] = engine._make_cache_entry(bars, 200)

    out = engine._get_bars_with_increment("600000.SH", "5m", 200)

    quotes = _count_quote(rec)
    assert len(quotes) == 1                      # 仅增量拉一次（count=10）
    assert "count=10" in str(quotes[0].url)
    assert len(out) == 5                         # 无新 bar，缓存原样返回


def test_get_bars_with_increment_hit_new_bar_merge_and_cap():
    """缓存命中 + 增量有新 bar：拼到末尾，截断保持 count 长。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    # 缓存已有 10:00~10:04，增量拉返回 10:03~10:06（含 10:05/10:06 两根新）
    cached_bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    new_bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(3, 7)])
    _mock_dispatcher_with_quote(rec, "600000.SH", new_bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    engine._preheat_cache[("600000.SH", "5m")] = engine._make_cache_entry(cached_bars, 6)

    out = engine._get_bars_with_increment("600000.SH", "5m", 6)

    stimes = [parse_bar_time(b) for b in out]
    assert stimes == sorted(stimes)              # 升序
    assert len(out) == 6                         # 截断到 count=6
    assert parse_bar_time(out[-1]) == datetime(2026, 8, 5, 10, 6)  # 末尾是最新 10:06
    # 合并后 7 根（10:00~10:06），截断到 count=6 丢弃最旧的 10:00，保留 10:01~10:06
    assert parse_bar_time(out[0]) == datetime(2026, 8, 5, 10, 1)
    assert engine._preheat_cache[("600000.SH", "5m")]["last_stime"] == datetime(2026, 8, 5, 10, 6)


def test_get_bars_with_increment_upgrade_when_count_exceeds_cache():
    """缓存存在但请求 count > 缓存 count：升级全量拉（够长公式窗口，不增量）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    # 缓存只有 200 根，请求 500 → 升级全量拉 500
    engine._preheat_cache[("600000.SH", "5m")] = engine._make_cache_entry(bars, 200)

    engine._get_bars_with_increment("600000.SH", "5m", 500)

    quotes = _count_quote(rec)
    assert len(quotes) == 1
    assert "count=500" in str(quotes[0].url)     # 升级全量 500，非增量 10
    assert engine._preheat_cache[("600000.SH", "5m")]["count"] == 500  # 缓存升级


def test_offline_recovery_clears_preheat_cache():
    """E8 离线恢复：_tick_main 清预热缓存，下次走全量重建。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single(period="5m")
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    bars = _quote_bars("600000.SH", [datetime(2026, 8, 5, 10, i) for i in range(5)])
    _mock_dispatcher_with_quote(rec, "600000.SH", bars)
    engine = _make_engine_formula_portfolio(
        disp, factory, port, {1: "FORM_A"}, formula_count=200,
    )
    engine._preheat_cache[("600000.SH", "5m")] = engine._make_cache_entry(bars, 200)
    assert engine._preheat_cache != {}

    # 模拟离线→在线转场：was_online=False + heartbeat 成功
    engine._bridge_online = False
    rec2 = _Recorder(respond=_respond_quote_bars("600000.SH", bars))
    disp2, _ = _make_dispatcher(rec2)
    engine._dispatcher = disp2  # 在线桥
    engine._bar_poller._dispatcher = disp2

    # _tick_main 需在 worker 线程跑（run_in_executor），这里直接调同步体
    engine._tick_main()

    # 离线恢复清了预热缓存
    assert engine._preheat_cache == {}



def _breaker_engine(factory):
    """带 LiveSessionPortfolio link 的引擎（session_id=1, portfolio_strategy_id=1）。"""
    port, _ = _portfolio_single()
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    db = factory()
    db.add(LiveSessionPortfolio(session_id=1, portfolio_strategy_id=1, circuit_breaker_count=0))
    db.commit()
    db.close()
    return engine, port


def _breaker_link(db, count):
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=1, portfolio_strategy_id=1
    ).first()
    assert link is not None
    assert link.circuit_breaker_count == count
    return link


def test_persist_breaker_count_writes_on_change():
    """H4：max_drawdown 触发计数变化 → 写 LiveSessionPortfolio.circuit_breaker_count。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)

    port.risk_manager.consecutive_drawdown_triggers = 1
    engine._persist_breaker_count(port)

    db = factory()
    link = _breaker_link(db, 1)
    assert link.status == "active"  # <3 不转 circuit_broken
    db.close()
    assert engine._breaker_count_written[1] == 1  # 记录已写计数，避免重复落库


def test_persist_breaker_count_unchanged_skips_write():
    """H4：计数未变 → 直接跳过（不查询/不落库）。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    engine._breaker_count_written[1] = 2
    port.risk_manager.consecutive_drawdown_triggers = 2

    engine._persist_breaker_count(port)  # 计数 == written → return，不写库

    db = factory()
    _breaker_link(db, 0)  # DB 仍是初始 0
    db.close()


def test_persist_breaker_count_three_sets_circuit_broken():
    """H4：达 3 次 → status 转 circuit_broken（design §8.3）。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)

    port.risk_manager.consecutive_drawdown_triggers = 3
    engine._persist_breaker_count(port)

    db = factory()
    link = _breaker_link(db, 3)
    assert link.status == "circuit_broken"
    db.close()


def test_handle_bar_persists_breaker_count_on_drawdown_trigger():
    """H4：_handle_bar 接 update_peak——max_drawdown 熔断触发（计数+1）→ circuit_breaker_count 落库。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    # 清风控比例：drawdown bar 不触发止损单，聚焦熔断计数断言
    port.strategies[0].strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0"), take_profit_ratio=Decimal("0"),
        trailing_stop_ratio=Decimal("0"),
    )
    stock = "600000.SH"
    # 持仓 1000 股 + 现金 0：总市值随 bar close 波动
    pos = Position(stock)
    pos.buy(1000, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))
    port.strategies[0].positions[stock] = pos
    port.account.cash = Decimal("0")

    # bar1: close=100 → 总市值 100000 建峰（无熔断）
    engine._handle_bar(port, _bar(stock, "100", datetime(2026, 8, 5, 10, 0)))
    db = factory()
    _breaker_link(db, 0)
    db.close()

    # bar2: close=70 → 回撤 30% > 20% → 熔断，计数+1 落库
    engine._handle_bar(port, _bar(stock, "70", datetime(2026, 8, 5, 10, 1)))
    db = factory()
    link = _breaker_link(db, 1)
    assert link.status == "active"
    db.close()


def test_recover_restores_breaker_count():
    """D4：recover 读回 circuit_breaker_count → consecutive_drawdown_triggers（重启后不丢累计次数）。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    db = factory()
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=1, portfolio_strategy_id=1
    ).first()
    link.circuit_breaker_count = 2
    db.commit()
    db.close()

    db = factory()
    engine.recover(db)
    db.close()

    assert port.risk_manager.consecutive_drawdown_triggers == 2
    assert port.risk_manager.manual_recovery is False   # 2 < 3 未转手动
    assert port.risk_manager.circuit_breaker_active is False
    assert engine._breaker_count_written[1] == 2  # 预置，避免首 bar 重复写


def test_recover_breaker_count_three_sets_manual_halt():
    """D4：达 3 次 → recover 后转手动恢复：manual_recovery + circuit_breaker_active（停新开仓）。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    db = factory()
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=1, portfolio_strategy_id=1
    ).first()
    link.circuit_breaker_count = 3
    link.status = "circuit_broken"
    db.commit()
    db.close()

    db = factory()
    engine.recover(db)
    db.close()

    assert port.risk_manager.consecutive_drawdown_triggers == 3
    assert port.risk_manager.manual_recovery is True
    assert port.risk_manager.circuit_breaker_active is True
    assert port.risk_manager.is_trading_halted() is True


def test_recover_breaker_resets_manual_recovery():
    """§8.3 转手动恢复后的人工恢复入口：recover_breaker 清零计数 + 解除手动恢复 + 落库 active。

    构造 count=3 + status=circuit_broken（同 test_recover_breaker_count_three_sets_manual_halt
    前置），调 engine.recover_breaker(1) → 内存态全清 + DB status=active/count=0 +
    _breaker_count_written 同步（否则下 bar _persist_breaker_count 比对跳过回写）。
    peak_value 不重置（用户决策：保留历史峰值）。
    """
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    # 先置 3 次熔断转手动（内存 + DB）
    db = factory()
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=1, portfolio_strategy_id=1
    ).first()
    link.circuit_breaker_count = 3
    link.status = "circuit_broken"
    db.commit()
    db.close()
    db = factory()
    engine.recover(db)  # 还原内存态：count=3 → manual_recovery=True
    db.close()
    assert port.risk_manager.manual_recovery is True  # 前置：确实卡在手动恢复
    engine._breaker_count_written[1] = 3  # 模拟已落库 3（recover 预置）

    ok_flag = engine.recover_breaker(1)

    assert ok_flag is True
    # 内存态全清
    assert port.risk_manager.consecutive_drawdown_triggers == 0
    assert port.risk_manager.manual_recovery is False
    assert port.risk_manager.circuit_breaker_active is False
    assert port.risk_manager.breaker_trigger_date is None
    assert port.risk_manager.is_trading_halted() is False
    # _breaker_count_written 同步（防下 bar 跳过回写）
    assert engine._breaker_count_written[1] == 0
    # DB 落库
    db = factory()
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=1, portfolio_strategy_id=1
    ).first()
    assert link.status == "active"
    assert link.circuit_breaker_count == 0
    db.close()


def test_recover_breaker_unknown_portfolio_returns_false():
    """recover_breaker 对不属于本 session 的组合返回 False（不抛异常、不改任何状态）。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)

    ok_flag = engine.recover_breaker(999)  # 不存在的 portfolio_id

    assert ok_flag is False
    # 现有组合状态未被动过
    assert port.risk_manager.consecutive_drawdown_triggers == 0
    assert port.risk_manager.manual_recovery is False


def test_recover_breaker_emits_risk_event():
    """recover_breaker 后 emit risk(triggered=False) 事件——前端 SSE 实时看到恢复。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    port.risk_manager.manual_recovery = True  # 前置：卡在手动恢复
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    engine.recover_breaker(1)

    ev = q.get_nowait()
    assert ev["type"] == "risk"
    assert ev["rule"] == "max_drawdown"
    assert ev["triggered"] is False
    assert ev["count"] == 0
    assert ev["portfolio_id"] == 1
    assert q.empty()


# ---------------- F6 同 bar 多策略超卖（bar 内可用量递减记账）----------------
def test_live_t1_checker_consume_available_decrements():
    """F6：consume_available 递减 bar 可用量——同 bar 后续 SELL 见递减后的值；重设快照恢复全量。"""
    checker = LiveT1Checker()
    checker.set_available_map({"600000.SH": 800})
    pos = Position("600000.SH")
    pos.buy(1000, Decimal("9.0"), datetime(2026, 8, 5, 10, 0))
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 800

    checker.consume_available("600000.SH", 600)  # A 卖 600 → 余 200
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 200

    checker.consume_available("600000.SH", 500)  # 超出 → 钳到 0，不出现负值
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 0

    checker.set_available_map({"600000.SH": 800})  # 下一 bar 重设快照 → 恢复全量
    assert checker.get_available_shares(pos, datetime(2026, 8, 5).date()) == 800


def test_handle_bar_two_sells_share_bar_available():
    """F6：同 bar A 卖 600 + B 卖 400，桥 available 800 → 递减记账后 B 只下 200（不超卖）。"""
    factory, _ = _db_factory()
    port, ctxs = _portfolio_two(periods=("1m", "1m"))  # 策略 1/2 同周期
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    for ctx, qty in zip(ctxs, (600, 400)):
        ctx.formula_signals = [
            {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": -1},
        ]
        ctx.positions[stock] = Position(stock)
        ctx.positions[stock].buy(qty, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))

    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": 800, "volume": 1000,
                 "yesterday_volume": 1000, "on_road_volume": 0},
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
    engine.signal_cache = {
        (1, stock, bar_time): [{"name": "close_sig", "value": -1}],
        (2, stock, bar_time): [{"name": "close_sig", "value": -1}],
    }
    bar = _bar(stock, "9.0", bar_time)

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).order_by(LiveOrder.strategy_id).all()
    assert len(orders) == 2
    by_sid = {o.strategy_id: o for o in orders}
    assert by_sid[1].quantity == 600
    assert by_sid[2].quantity == 200  # B 见递减后 available=200，不超卖
    placed = [json.loads(r.content)["volume"] for r in rec.requests if r.url.path == "/order"]
    assert placed == [600, 200]
    db.close()


# ---------------- G5 回填轮询独立更短节拍（5s）----------------
def test_start_creates_independent_deals_task():
    """G5：start() 起独立 _deals_task（回填轮询，独立于主循环），stop() 取消。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    import asyncio

    async def run_a_bit():
        await engine.start()
        assert engine._deals_task is not None
        assert not engine._deals_task.done()
        await asyncio.sleep(0.01)
        await engine.stop()

    asyncio.run(run_a_bit())
    assert engine._deals_task is None
    assert engine._task is None


def test_deals_loop_polls_deals_at_own_interval():
    """G5：_deals_loop 按 deals_poll_interval(5s 默认) 独立调 _poll_deals，不依赖主循环节拍。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        deals_poll_interval=0.01,
    )
    calls = []
    engine._poll_deals = lambda: calls.append(1)
    import asyncio

    async def run_a_bit():
        await engine.start()
        # 审计 #37：原固定 sleep(0.05) 断言 >=3 在调度抖动下偶发失败（_tick_deals 经
        # 线程池派发，比直调多一次跨线程调度，时序更易抖）。改为有界等待：轮询 calls
        # 直到达到阈值或超时——稳态后必达 3（0.01s 间隔 × 0.5s 窗口足够余量）。
        deadline = asyncio.get_event_loop().time() + 0.5
        while len(calls) < 3 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.005)
        await engine.stop()

    asyncio.run(run_a_bit())
    assert len(calls) >= 3


# ---------------- D3 对账（虚拟持仓 vs 桥实际，仅告警不修正）----------------
def _seed_trade(db, stock, qty, price="9.0"):
    """预置一笔 BUY 成交（recover 据此重建虚拟持仓）。"""
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1, stock_code=stock,
        trade_type="buy", order_type="limit", price=Decimal(price), quantity=qty,
        filled_quantity=qty, filled_price=Decimal(price), status="accepted",
        signal_name="open_sig", signal_type="OPEN", bar_time=datetime(2026, 8, 5, 10, 0),
    )
    db.add(lo)
    db.flush()
    db.add(LiveTrade(
        live_session_id=1, live_order_id=lo.id, portfolio_strategy_id=1, strategy_id=1,
        stock_code=stock, trade_type="buy", price=Decimal(price), quantity=qty,
        amount=Decimal(price) * qty, commission=Decimal("0"), stamp_duty=Decimal("0"),
        trade_time=datetime(2026, 8, 5, 10, 0),
    ))


def _positions_respond(volume):
    def respond(request):
        path = request.url.path
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": [
                {"instrument": "600000", "exchange": "SH",
                 "available": volume, "volume": volume,
                 "yesterday_volume": volume, "on_road_volume": 0},
            ]})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})
    return respond


def _reconcile_engine(factory, seed_qty, respond=None, fail_paths=None):
    """seeded BUY + 指定 /positions respond 的引擎；recover 后返回 (engine, port, ctx)。"""
    port, ctx = _portfolio_single()
    stock = "600000.SH"
    db = factory()
    _seed_trade(db, stock, seed_qty)
    db.commit()
    db.close()
    rec = _Recorder(respond=respond, fail_paths=fail_paths)
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    db = factory()
    engine.recover(db)
    db.close()
    return engine, port, ctx


def test_reconcile_positions_reports_virtual_vs_real_mismatch():
    """D3：虚拟 1000 vs 桥实际 800 → 记录 mismatch，账面不修正（仍 1000）。"""
    factory, _ = _db_factory()
    engine, _, ctx = _reconcile_engine(factory, 1000, respond=_positions_respond(800))

    assert ctx.positions["600000.SH"].quantity == 1000  # 只告警，不改虚拟持仓
    assert engine._reconcile_mismatches == [
        {"code": "600000.SH", "virtual": 1000, "real": 800, "diff": 200},
    ]


def test_reconcile_positions_clean_when_matching():
    """D3：虚拟 == 桥实际 → 无 mismatch 记录。"""
    factory, _ = _db_factory()
    engine, _, _ = _reconcile_engine(factory, 800, respond=_positions_respond(800))

    assert engine._reconcile_mismatches == []


def test_reconcile_positions_offline_skips():
    """D3：桥离线 → 跳过对账不崩，无 mismatch 记录。"""
    factory, _ = _db_factory()
    engine, _, _ = _reconcile_engine(factory, 1000, fail_paths={"/positions"})

    assert engine._reconcile_mismatches == []


# ---------------- B5 SSE 事件流（signal/order/trade/position/risk 推送）----------------
def _ss_engine(factory, port=None):
    """B5 辅助：单组合引擎 + 独立订阅队列，返回 (engine, q)。"""
    if port is None:
        port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)
    return engine, q


def test_emit_pushes_event_to_all_subscribers():
    """B5：_emit 向所有订阅队列广播 {type, **payload}；队列满丢弃不崩。"""
    factory, _ = _db_factory()
    engine, _ = _ss_engine(factory)
    q1 = asyncio.Queue()
    q2 = asyncio.Queue(maxsize=1)
    q2.put_nowait("full")
    engine._stream_subscribers.extend([q1, q2])

    engine._emit("signal", {"portfolio_id": 1, "strategy_id": 1})

    assert q1.get_nowait() == {"type": "signal", "portfolio_id": 1, "strategy_id": 1}
    assert q2.get_nowait() == "full"  # 积压队列丢新事件，原事件保留，不抛异常


def test_handle_bar_emits_signal_and_order_submitted():
    """B5：OPEN 信号 → 先 signal 后 order(submitted) 事件；未回填不 emit trade/position。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    engine, q = _ss_engine(factory, port)
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    events = [q.get_nowait(), q.get_nowait()]
    assert events[0]["type"] == "signal"
    assert events[0]["portfolio_id"] == 1
    assert events[0]["strategy_id"] == 1
    assert events[0]["stock_code"] == stock
    assert events[0]["signal_name"] == "open_sig"
    assert events[0]["signal_type"] == "OPEN"
    assert events[1]["type"] == "order"
    assert events[1]["status"] == "submitted"
    assert events[1]["portfolio_id"] == 1
    assert events[1]["order_id"] is not None
    assert events[1]["stock_code"] == stock
    assert events[1]["trade_type"] == "buy"
    assert events[1]["quantity"] > 0
    assert q.empty()  # 未成交回报前不 emit trade/position


def test_handle_bar_bridge_reject_emits_order_rejected():
    """B5：桥拒单（/order 连接失败）→ order(submitted) 后跟 order(rejected)，带 error_message。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)
    rec = _Recorder(fail_paths={"/order"})
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    events = [q.get_nowait(), q.get_nowait(), q.get_nowait()]
    assert events[0]["type"] == "signal"
    assert events[1]["type"] == "order" and events[1]["status"] == "submitted"
    assert events[2]["type"] == "order" and events[2]["status"] == "rejected"
    assert events[2]["order_id"] == events[1]["order_id"]
    assert "error_message" in events[2]
    assert q.empty()


def test_handle_bar_bridge_business_reject_surfaces_error():
    """#4：桥业务拒单（白名单/限额，{ok:false,error:...}）→ order(rejected) 回显桥侧真实原因。

    此前 dispatcher 把桥 error 吞成 None → error_message 笼统 "approval failed or bridge rejected"，
    真机查不了拒单原因。现抛 BridgeOrderRejected → 回显 volume 超限文案。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)

    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={
                "ok": False, "error": "volume 33900 exceeds max 100000",
            })
        return httpx.Response(200, json={"ok": True})

    rec = _Recorder(respond=respond)
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    events = [q.get_nowait(), q.get_nowait(), q.get_nowait()]
    assert events[0]["type"] == "signal"
    assert events[1]["type"] == "order" and events[1]["status"] == "submitted"
    assert events[2]["type"] == "order" and events[2]["status"] == "rejected"
    assert events[2]["error_message"] == "volume 33900 exceeds max 100000"
    assert q.empty()


def test_handle_bar_emits_risk_on_max_drawdown_trigger():
    """B5：max_drawdown 熔断触发（计数递增）→ risk(max_drawdown) 事件；计数未变不重发。"""
    factory, _ = _db_factory()
    engine, port = _breaker_engine(factory)
    # 下跌 bar（close 5）亏损 44% < 50% 止损线、无止盈 → 不产生风控 SELL，聚焦熔断事件
    port.strategies[0].strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("1.0"),
        trailing_stop_ratio=Decimal("0"),
    )
    stock = "600000.SH"
    pos = Position(stock)
    pos.buy(1000, Decimal("9.0"), datetime(2026, 8, 5, 9, 30))
    port.strategies[0].positions[stock] = pos
    port.account.cash = Decimal("0")
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    # bar1: close=9 → 总市值 9000 建峰，无熔断无信号
    engine._handle_bar(port, _bar(stock, "9", datetime(2026, 8, 5, 10, 0)))
    assert q.empty()

    # bar2: close=5 → 回撤 44% > 20% → 熔断，计数+1 → risk 事件
    engine._handle_bar(port, _bar(stock, "5", datetime(2026, 8, 5, 10, 1)))
    ev = q.get_nowait()
    assert ev["type"] == "risk"
    assert ev["rule"] == "max_drawdown"
    assert ev["triggered"] is True
    assert ev["count"] == 1
    assert ev["portfolio_id"] == 1
    assert q.empty()  # 计数未再变不重发


def test_backfill_emits_order_filled_trade_position():
    """B5：成交回报回填 filled → order(filled) + trade + position 事件（真实价/量）。"""
    factory, _ = _db_factory()
    db = factory()
    lo = LiveOrder(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1, stock_code="600000.SH",
        trade_type="buy", order_type="limit", price=Decimal("9.0"), quantity=1000,
        filled_quantity=0, filled_price=None, status="submitted",
        signal_name="open_sig", signal_type="OPEN",
        bar_time=datetime(2026, 8, 5, 10, 0), order_ref="ref1",
    )
    db.add(lo)
    db.commit()
    lo_id = lo.id
    db.close()

    port, _ = _portfolio_single()
    engine, q = _ss_engine(factory, port)

    db = factory()
    lo = db.query(LiveOrder).filter_by(id=lo_id).first()
    deals = [{
        "order_ref": "ref1", "volume": 1000, "amount": 9000,
        "commission": 0, "trade_date": "20260805", "trade_time": "100100",
    }]
    engine._backfill_order(db, lo, deals)
    db.commit()
    db.close()

    events = [q.get_nowait(), q.get_nowait(), q.get_nowait()]
    assert events[0]["type"] == "order" and events[0]["status"] == "filled"
    assert events[0]["order_id"] == lo_id
    assert events[0]["filled_quantity"] == 1000
    assert events[0]["filled_price"] == 9.0
    assert events[1]["type"] == "trade"
    assert events[1]["portfolio_id"] == 1
    assert events[1]["trade_id"] is not None
    assert events[1]["stock_code"] == "600000.SH"
    assert events[1]["trade_type"] == "buy"
    assert events[1]["quantity"] == 1000
    assert events[1]["price"] == 9.0
    assert events[1]["amount"] == 9000
    assert events[2]["type"] == "position"
    assert events[2]["portfolio_id"] == 1
    assert events[2]["stock_code"] == "600000.SH"
    assert events[2]["quantity"] == 1000
    assert events[2]["avg_cost"] == 9.0
    assert q.empty()


def test_maybe_daily_close_emits_risk_on_daily_loss(monkeypatch):
    """B5：日内亏损触发 → risk(daily_loss) 事件，当日暂停新开仓。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    port.account.cash = Decimal("90000")
    port.risk_manager.prev_close_value = Decimal("100000")  # 昨日收盘基准
    engine, q = _ss_engine(factory, port)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 5, 14, 30)
    monkeypatch.setattr("core.engine.live_engine.datetime", _FakeDateTime)

    engine._maybe_daily_close()

    ev = q.get_nowait()
    assert ev["type"] == "risk"
    assert ev["rule"] == "daily_loss"
    assert ev["triggered"] is True
    assert ev["portfolio_id"] == 1
    assert port.risk_manager.daily_pause_active is True


def test_now_shanghai_returns_shanghai_wall_clock(monkeypatch):
    """#23：now_shanghai() 返回上海时间（naive），不依赖本机时区。

    本机 datetime.now() 返回 UTC 06:30 时，now_shanghai() 应返回 14:30（+8）。
    证明日终 (14:30) 判定按上海时间，而非本机时间——Core 部署 UTC 服务器时不哑火。
    """
    from core.engine.live_engine import now_shanghai, _CST
    _fixed_utc = datetime(2026, 8, 10, 6, 30)  # 视为 UTC 06:30

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 模拟真实 datetime.now(tz=...) 语义：
            # 传 tz → 返回该 UTC 瞬时在 tz 的墙上时间（aware）。
            # _fixed_utc 视为 UTC，目标 tz 偏移 +8h → 上海 14:30。
            if tz is not None:
                return (_fixed_utc + tz.utcoffset(None)).replace(tzinfo=tz)
            return _fixed_utc
    monkeypatch.setattr("core.engine.live_engine.datetime", _FakeDateTime)

    # UTC 06:30 → 上海 14:30（+8）；now_shanghai 剥 tz 后仍 14:30
    assert now_shanghai() == datetime(2026, 8, 10, 14, 30)
    # 顺带验证 _CST 是 +8
    assert _CST.utcoffset(None) == timedelta(hours=8)


def test_maybe_daily_close_uses_shanghai_time(monkeypatch):
    """#23：_maybe_daily_close 日终判定按上海时间（14:30 阈值），可注入 now。

    now=14:30 → 触发 update_daily（_last_daily_date 置当日）；
    now=14:29 → 未触发（_last_daily_date 仍 None/初始）。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    engine, _ = _ss_engine(factory, port)

    # 14:29 → 不触发
    engine._last_daily_date = None
    engine._maybe_daily_close(now=datetime(2026, 8, 10, 14, 29))
    assert engine._last_daily_date is None

    # 14:30 → 触发
    engine._maybe_daily_close(now=datetime(2026, 8, 10, 14, 30))
    assert engine._last_daily_date == datetime(2026, 8, 10).date()


def _ss_plain_engine(factory, ping_interval=0.05):
    """B5 辅助：无预订阅队列的引擎（stream_events 测试用，断言退订精确）。"""
    port, _ = _portfolio_single()
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine._stream_ping_interval = ping_interval
    return engine


def test_stream_events_yields_events_then_ping_when_idle():
    """B5：stream_events 先 yield 事件，空闲超 ping 间隔 → yield ping 心跳；结束退订。"""
    factory, _ = _db_factory()
    engine = _ss_plain_engine(factory)
    engine._running = True

    async def drive():
        agen = engine.stream_events()
        it = agen.__aiter__()
        # 首轮 __anext__ 让生成器订阅（队列空开始等事件）；让出后 emit 才广播进队列
        first_task = asyncio.create_task(it.__anext__())
        await asyncio.sleep(0)
        engine._emit("signal", {"portfolio_id": 1, "strategy_id": 1})
        first = await first_task
        assert first["type"] == "signal"
        assert first["portfolio_id"] == 1
        second = await it.__anext__()  # 空闲 0.05s → ping
        assert second["type"] == "ping"
        assert "time" in second
        await agen.aclose()
        assert engine._stream_subscribers == []  # 退订

    asyncio.run(drive())


def test_stream_events_ends_when_engine_not_running():
    """B5：引擎未运行 → stream_events 无事件即结束并退订（端点侧转 ping-only 保活）。"""
    factory, _ = _db_factory()
    engine = _ss_plain_engine(factory)

    async def drive():
        events = [ev async for ev in engine.stream_events()]
        assert events == []
        assert engine._stream_subscribers == []

    asyncio.run(drive())


# ===========================================================================
# 审计 #3：异步循环内同步 I/O — _loop/_deals_loop 阻塞调用走线程池，事件循环不冻结
# ===========================================================================
# 现状（修复前）：_loop/_deals_loop 声明 async def，但内部直接调同步阻塞 I/O
# （dispatcher.heartbeat / bar_poller.poll / _poll_deals，全用同步 httpx.Client）。
# FastAPI 事件循环被这些阻塞调用占住期间，所有 HTTP 请求与 SSE 流冻结。
#
# 修复（方案 B）：整轮 tick 体通过 loop.run_in_executor 丢到线程池执行；
# 单 worker executor 让 _loop 与 _deals_loop 的 tick 串行（共享 _pending_orders /
# positions / signal_cache 无并发竞争）；_emit 跨线程用 call_soon_threadsafe
# 回到事件线程 put_nowait（asyncio.Queue 非线程安全）。

def test_loop_tick_runs_in_worker_thread_not_event_loop():
    """#3：_loop 整轮 tick 在线程池 worker 执行，事件循环线程不被阻塞占用。

    tick 内同步 I/O（heartbeat/poll）发生在 worker 线程；事件循环线程（drive 协程
    所在线程）能并发推进 —— 两者线程 id 必不同。
    """
    import asyncio
    import threading

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
    tick_threads = set()

    orig_heartbeat = disp.heartbeat

    def spy_heartbeat():
        tid = threading.get_ident()
        tick_threads.add(tid)
        return orig_heartbeat()

    disp.heartbeat = spy_heartbeat

    async def drive():
        await engine.start()
        await asyncio.sleep(0.06)  # 至少跑几轮 tick
        await engine.stop()

    loop_thread = threading.get_ident()
    asyncio.run(drive())

    assert tick_threads, "heartbeat 未被调用"
    assert loop_thread not in tick_threads, (
        "tick 在事件循环线程执行——同步 I/O 仍会冻结事件循环"
    )


def test_deals_loop_tick_runs_in_worker_thread():
    """#3：_deals_loop 的 _poll_deals 也在 worker 线程执行（同 _loop，不阻塞事件循环）。"""
    import asyncio
    import threading

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, deals_poll_interval=0.01,
    )
    poll_threads = set()

    def spy_poll_deals():
        poll_threads.add(threading.get_ident())
        return None

    engine._poll_deals = spy_poll_deals

    async def drive():
        await engine.start()
        await asyncio.sleep(0.05)
        await engine.stop()

    loop_thread = threading.get_ident()
    asyncio.run(drive())

    assert poll_threads, "_poll_deals 未被调用"
    assert loop_thread not in poll_threads, (
        "_poll_deals 在事件循环线程执行——同步 I/O 仍会冻结事件循环"
    )


def test_loop_and_deals_loop_ticks_serialize_via_single_worker():
    """#3：_loop 与 _deals_loop 共享单 worker 线程池 → tick 严格串行，无并发重叠。

    用两个带 sleep 的 spy 制造重叠窗口：若并发执行，两者会同时持有旗标；
    单 worker 串行 → 任一时刻最多一个在跑，never overlap。
    """
    import asyncio
    import threading

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
        poll_interval=0.005, deals_poll_interval=0.005,
    )

    active = {"n": 0, "max": 0, "lock": threading.Lock()}
    import time

    def _track(fn):
        def _w():
            with active["lock"]:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            try:
                time.sleep(0.01)  # 制造重叠窗口
                return fn() if fn is not None else None
            finally:
                with active["lock"]:
                    active["n"] -= 1
        return _w

    engine._poll_deals = _track(None)
    orig_heartbeat = disp.heartbeat
    disp.heartbeat = _track(orig_heartbeat)

    async def drive():
        await engine.start()
        await asyncio.sleep(0.1)  # 5ms × 100ms → 两循环各约 20 轮
        await engine.stop()

    asyncio.run(drive())

    assert active["max"] <= 1, (
        "_loop 与 _deals_loop tick 并发执行（max=%d）——共享状态竞争未消除"
        % active["max"]
    )


def test_emit_from_worker_thread_lands_on_sse_queue():
    """#3：worker 线程内 _emit 经 call_soon_threadsafe 回到事件线程 put_nowait。

    _loop tick 在 worker 线程触发下单 → _emit 在 worker 线程被调；
    SSE 队列由事件线程持有（asyncio.Queue 非线程安全），必须回事件线程投递。
    直接在事件线程 await 一下让 scheduled 回调落地，再断言队列收到事件。
    """
    import asyncio

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)  # 完成的 bar stime = 10:00
    # 状态化 /quote：每次拉取多返回一根 bar —— 轮1 [bar0] 建基线，轮2 [bar0,bar1]
    # → bar0 退居第二 → 完成 → 触发 _on_bar(bar_time=10:00) → _handle_bar → _emit
    all_bars = _quote_bars(stock, [datetime(2026, 8, 5, 10, 0), datetime(2026, 8, 5, 10, 1)])
    poll_count = {"n": 0}

    def growing_quote(request):
        path = request.url.path
        if path == "/quote":
            poll_count["n"] += 1
            n = min(poll_count["n"], len(all_bars))
            return httpx.Response(200, json={"ok": True, "data": {stock: all_bars[:n]}})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        if path == "/positions":
            return httpx.Response(200, json={"ok": True, "data": []})
        if path == "/deals":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404, json={"ok": False})

    rec = _Recorder(respond=growing_quote)
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, poll_interval=0.01,
    )
    # 预置信号让 _handle_bar 在 worker 线程 emit signal+order
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}

    async def drive():
        engine._stream_subscribers.append(asyncio.Queue(maxsize=200))
        await engine.start()
        # 多等几轮：建基线(轮1) → 完成bar触发(轮2+) → call_soon_threadsafe 调度落地
        await asyncio.sleep(0.15)
        await engine.stop()
        q = engine._stream_subscribers[0]
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    events = asyncio.run(drive())
    types = [e["type"] for e in events]
    assert "signal" in types, "worker 线程 _emit 未送达 SSE 队列（call_soon_threadsafe 缺失）"
    assert "order" in types


def test_emit_direct_call_still_synchronous_without_running_loop():
    """#3 回归：无事件循环时直接调 _emit 仍同步入队（保留既有同步测试行为）。

    test_emit_pushes_event_to_all_subscribers 等直接调 _emit 后立即 get_nowait；
    call_soon_threadsafe 改造不得破坏此同步路径（无 loop captured → 直接 put_nowait）。
    """
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
        bar_poller=poller, db_session_factory=factory,
    )
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    # 无 running loop、未 start（_loop_ref 为 None）→ 直接 put_nowait
    engine._emit("signal", {"portfolio_id": 1, "strategy_id": 1})

    assert q.get_nowait() == {"type": "signal", "portfolio_id": 1, "strategy_id": 1}


def test_executor_single_worker():
    """#3：LiveEngine 用单 worker 线程池（max_workers=1）串行两循环的 tick。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    rec = _Recorder()
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    assert engine._executor is not None
    assert engine._executor._max_workers == 1


def test_loop_offline_to_online_still_resets_baseline_after_thread_offload():
    """#3 回归：tick 走线程池后 E8 离线→在线转场仍调 reset_baseline。"""
    import asyncio

    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"

    class FlakyPing:
        def __init__(self):
            self.fail_remaining = 1

        def handler(self, request):
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
    orig = poller.reset_baseline
    poller.reset_baseline = lambda: (reset_calls.append(1), orig())[1]

    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory, poll_interval=0.01,
    )

    async def run_a_bit():
        await engine.start()
        # 审计 #37：原固定 sleep(0.1) 偶发抖动，同 :1347 测试——改有界轮询等转场完成。
        deadline = asyncio.get_event_loop().time() + 0.5
        while len(reset_calls) < 1 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.005)
        await engine.stop()

    asyncio.run(run_a_bit())

    assert engine.bridge_online is True
    assert len(reset_calls) == 1


# ---------------- 收盘后不下单守卫（修复2）----------------
def test_handle_bar_after_close_skips_order_but_emits_signal():
    """修复2：bar_time >= 15:00（深交所收盘）→ 信号仍 emit，但不落 LiveOrder。

    根因：真机 2026-08-14 id40-41 的 bar_time=15:02:42，报不进交易所成待报单冻结持仓
    永不成交。守卫用 bar.bar_time 判定（非墙钟：不依赖本机时钟/时区），bar_time>=15:00
    则信号推送后 continue 跳过落单。不写 DB、不进 pending。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 15, 1)  # 收盘后
    engine, q = _ss_engine(factory, port)
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    # 信号已推送（用户能看到收盘后的信号意图）
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert any(e["type"] == "signal" and e["stock_code"] == stock for e in events)
    # 无 order 事件、无 LiveOrder 落库
    assert not any(e["type"] == "order" for e in events)
    db = factory()
    assert db.query(LiveOrder).count() == 0
    db.close()


def test_handle_bar_before_close_places_order_normally():
    """修复2回归：bar_time < 15:00（盘中）→ 守卫不拦，正常落 LiveOrder(submitted)。

    守卫不能误伤盘中合法 bar（含 14:59 的 bar：主循环 60s 延迟处理时其 bar_time 仍 <15:00）。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 14, 59)  # 盘中最后一分钟
    engine, q = _ss_engine(factory, port)
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1
    assert orders[0].status == "submitted"
    db.close()


# ---------------- 桥到达确认 → 受理即丢弃即时 rejected（修复1）----------------
def test_handle_bar_bridge_dropped_order_marks_rejected_immediately():
    """修复1：桥 passorder 受理但回查无委托记录（受理即丢弃）→ ok:false + error
    "no order record (dropped)" → Core 走既有 BridgeOrderRejected 路径即时标 rejected。

    根因：真机 2026-08-14 id36-39，桥 passorder result=0（受理）但 iQuant 未生成委托记录，
    Core 轮询 /orders 找不到 order_ref，180s 后才标 rejected。修复后桥 _confirm_order_arrival
    回查 ORDER 表无记录 → 返回 ok:false，Core 即时 rejected（error 含 "no order record"），
    不再等 180s 超时。Core 侧零改动——接 dispatcher 既有 BridgeOrderRejected 路径。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    stock = "600000.SH"
    bar_time = datetime(2026, 8, 5, 10, 0)

    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={
                "ok": False,
                "error": "passorder accepted but no order record (dropped)",
            })
        return httpx.Response(200, json={"ok": True})

    rec = _Recorder(respond=respond)
    disp, _ = _make_dispatcher(rec)
    from core.engine.bar_poller import BarPoller
    poller = BarPoller(disp, [stock], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, stock, bar_time): [{"name": "open_sig", "value": 1}]}
    q = asyncio.Queue()
    engine._stream_subscribers.append(q)

    engine._handle_bar(port, _bar(stock, "9.0", bar_time))

    events = [q.get_nowait(), q.get_nowait(), q.get_nowait()]
    assert events[0]["type"] == "signal"
    assert events[1]["type"] == "order" and events[1]["status"] == "submitted"
    # 即时 rejected（不等 180s 超时），error 回显桥侧到达确认失败原因
    assert events[2]["type"] == "order" and events[2]["status"] == "rejected"
    assert "no order record" in events[2]["error_message"]
    assert q.empty()
    # DB 落库：submitted 后即 rejected，无残留 pending
    db = factory()
    lo = db.query(LiveOrder).filter(LiveOrder.status == "rejected").first()
    assert lo is not None
    assert "no order record" in lo.error_message
    db.close()


# ---------------- 超时检查移主循环（修复3）----------------
def test_tick_main_expires_stale_order_without_order_ref():
    """修复3：order_ref 始终匹配不到且超过 180s 的陈旧 submitted 单 → _tick_main 标 rejected。

    根因：超时检查原在 _poll_deals（deals 循环 5s 节拍），但 deals 循环被 60s 主循环饿死
    （单 worker 串行，主循环拉 ~73 只行情耗 50-57s/轮），180s 超时实际 440s 才生效。
    修复后 _tick_main 每轮调 _expire_stale_orders，超时检查随主循环 60s 节拍跑。
    直接调 _tick_main 验证（先例 test_offline_recovery_clears_preheat_cache:2539）。
    """
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    rec = _Recorder()
    _mock_orders_deals(rec, orders=[], deals=[])  # 桥侧无任何匹配委托
    disp, _ = _make_dispatcher(rec)
    engine = _make_engine(disp, factory, port)
    db = factory()
    stale = _submitted_order(db, quantity=600)
    stale.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    fresh = _submitted_order(db, quantity=700)
    fresh.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.close()

    engine._tick_main()

    db = factory()
    stale2 = db.get(LiveOrder, stale.id)
    fresh2 = db.get(LiveOrder, fresh.id)
    assert stale2.status == "rejected"
    assert stale2.error_message  # 有失效原因
    assert fresh2.status == "submitted"  # 未超时不动
    db.close()

