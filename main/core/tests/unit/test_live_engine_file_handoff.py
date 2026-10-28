"""LiveEngine × 执行契约文件交接层（file_builtin）单测。

覆盖：引擎构造时挂 _file_handoff（仅 file_handoff_enabled dispatcher）、_emit 的
signal/decision/risk 事件同步落 signals/risk.jsonl（order 事件不落）、
_handle_bar 提交链对 file 模式 place_order 秒回 None 的处理
（订单保持 submitted，不判 approval failed）。
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from core.engine.bar_poller import BarPoller
from core.engine.event import BarEvent, OrderEvent
from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
from core.engine.live.timing import now_shanghai
from core.engine.live_engine import LiveEngine
from core.engine.portfolio import Portfolio
from core.engine.position import Position  # noqa: F401
from core.engine.risk_manager import PortfolioRiskManager, StrategyRiskManager
from core.engine.strategy_context import StrategyContext
from core.engine.ths_ui_dispatcher import ThsUiDispatcher
from core.models import LiveOrder
from tq_iquant_shared.constants import SignalType, TradeType

STOCK = "600000.SH"
BAR_TIME = datetime(2026, 8, 5, 10, 0)


# ---------------- 共用辅助（与 test_live_engine 同式） ----------------

def _db_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.models import Base
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # expire_on_commit=True 对齐生产 SessionLocal：commit 后访问 created_at 会
    # 从库重载 server default（legacy 模糊匹配的时间窗依赖它）。
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=True)
    return (lambda: SessionLocal()), engine


def _portfolio_single():
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
    return BarEvent(
        stocks={stock: {
            "open": Decimal("9.0"), "high": Decimal(close),
            "low": Decimal("9.0"), "close": Decimal(close), "volume": 10000,
        }},
        bar_time=bar_time,
    )


class _Recorder:
    """MockTransport：/order /ping /positions 受理成功（iQuant 桥对照用）。"""

    def handler(self, request):
        path = request.url.path
        return httpx.Response(200, json={"ok": True, "data": []})


class _FakeThsFileDispatcher(ThsUiDispatcher):
    """ths_ui + file_builtin 假 dispatcher：查询类全部空实现（不触真实 UI/TQ）。"""

    def __init__(self, work_dir):
        super().__init__(
            execute_mode="file_builtin", work_dir=str(work_dir), session_id=1,
            execute_interval_seconds=3600,  # 不启动真实消费节拍
        )

    def query_calendar(self):
        return []

    def query_orders(self, order_id=None):
        return []

    def query_positions(self):
        return []

    def query_account(self):
        return []

    def query_quote(self, code, period="1m", count=10):
        return []

    def heartbeat(self):
        return True


def _ths_engine(factory, port, work_dir):
    disp = _FakeThsFileDispatcher(work_dir)
    poller = BarPoller(disp, [STOCK], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    return engine, disp


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line for line in f if line.strip()]


# ---------------- 引擎挂载 handoff + 事件落文件 ----------------

def test_engine_ths_file_mode_constructs_handoff(tmp_path):
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    engine, disp = _ths_engine(factory, port, tmp_path)
    assert engine._file_handoff is not None
    assert engine._file_handoff.dir_path.endswith("1")
    disp.close()


def test_engine_non_ths_dispatcher_has_no_handoff(tmp_path):
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    client = httpx.Client(transport=httpx.MockTransport(_Recorder().handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=client)
    poller = BarPoller(disp, [STOCK], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    assert engine._file_handoff is None


def test_engine_ths_direct_mode_has_no_handoff(tmp_path):
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    disp = ThsUiDispatcher(execute_mode="direct")
    poller = BarPoller(disp, [STOCK], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    assert engine._file_handoff is None


def test_engine_emits_signal_and_risk_to_files(tmp_path):
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    engine, disp = _ths_engine(factory, port, tmp_path)
    engine._emit("signal", {"signal_name": "open_sig", "stock_code": STOCK})
    engine._emit("decision", {"gate": "capital_gate", "action": "approve"})
    engine._emit("risk", {"rule": "stop_loss"})
    engine._emit("order", {"status": "submitted"})  # order 事件不落交接文件

    h = engine._file_handoff
    signals = _read_lines(h.signals_path)
    assert len(signals) == 1
    assert '"open_sig"' in signals[0]
    assert '"signal"' in signals[0]

    risks = _read_lines(h.risk_path)
    assert len(risks) == 2  # decision + risk
    assert '"capital_gate"' in risks[0]
    assert '"stop_loss"' in risks[1]

    assert not os.path.exists(h.orders_path)  # orders.jsonl 由 dispatcher 专职写
    disp.close()


def test_engine_emit_write_failure_swallowed(tmp_path, monkeypatch):
    """交接文件写失败只 warning，不影响 _emit 主流程（SSE 照常投递）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    engine, disp = _ths_engine(factory, port, tmp_path)

    def _boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(engine._file_handoff, "write_signal", _boom)
    engine._emit("signal", {"signal_name": "open_sig"})  # 不抛异常
    disp.close()


# ---------------- _handle_bar：file 模式 place_order 秒回 None → 保持 submitted ----------------

def test_handle_bar_file_mode_order_stays_submitted(tmp_path):
    factory, _ = _db_factory()
    port, ctx = _portfolio_single()
    engine, disp = _ths_engine(factory, port, tmp_path)
    engine.signal_cache = {(1, STOCK, BAR_TIME): [{"name": "open_sig", "value": 1}]}
    bar = _bar(STOCK, "9.3", BAR_TIME)

    engine._handle_bar(port, bar)

    db = factory()
    orders = db.query(LiveOrder).all()
    assert len(orders) == 1
    lo = orders[0]
    # file 模式 place_order 秒回 None ≠ approval failed：订单保持 submitted 等回填
    assert lo.status == "submitted"
    assert lo.error_message is None
    # 指令已入交接文件（bridge_order_id = 确定性 order_id，与指令行一致）
    with open(disp._handoff.orders_path, "r", encoding="utf-8") as f:
        entries = [line for line in f if line.strip()]
    assert len(entries) == 1
    assert lo.bridge_order_id in entries[0]
    assert disp.last_enqueued_order_id == lo.bridge_order_id
    # submitted 阶段不 apply：持仓/现金不变
    assert ctx.positions[STOCK].quantity == 0
    assert port.account.cash == Decimal("100000")
    db.close()
    disp.close()


def test_handle_bar_file_mode_skips_immediate_ref_match(tmp_path):
    """file 模式提交时执行器尚未真正下单，禁止立即回查绑 ref——否则 120s 时间窗内
    的同形状旧单（上一根 bar 的同股同向单，真实场景 ~60s 前成交）会被误绑到本单，
    成交回填张冠李戴。ref 留 None，由执行器真正下单后的 /deals 回填周期再定位。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_single()
    disp = _FakeThsFileDispatcher(tmp_path)
    # 模拟：委托表里已有一根同形状旧单（60 秒前插入，落在 120s 认领窗内）
    stale = now_shanghai() - timedelta(seconds=60)
    disp.query_orders = lambda order_id=None: [{
        "source": "BRIDGE", "remark": "", "order_ref": "STALE-1",
        "instrument": "600000", "exchange": "SH", "direction": 48,
        "volume": 600, "insert_date": stale.strftime("%Y%m%d"),
        "insert_time": stale.strftime("%H%M%S"),
    }]
    poller = BarPoller(disp, [STOCK], period="1m", count=10)
    engine = LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )
    engine.signal_cache = {(1, STOCK, BAR_TIME): [{"name": "open_sig", "value": 1}]}
    engine._handle_bar(port, _bar(STOCK, "9.3", BAR_TIME))

    db = factory()
    lo = db.query(LiveOrder).one()
    assert lo.status == "submitted"
    assert lo.order_ref is None  # 不误绑 STALE-1
    # 反证：同形状旧单在 120s 窗内确实可被 legacy 匹配——上面的 None 来自
    # 「file 模式提交时跳过立即回查」，不是「匹配不上」。
    engine._try_match_order_ref(lo)
    assert lo.order_ref == "STALE-1"
    db.close()
    disp.close()
