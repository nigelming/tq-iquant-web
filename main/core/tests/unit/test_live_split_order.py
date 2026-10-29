"""批次2：TDX 大额拆单（成交驱动子单队列）单测。

需求（方案 D）：策略开启 split_order_enabled 且通道为 tdx_tq 且单笔委托金额
（price×quantity）≥ split_order_threshold → 拆为 N 笔等量子单（每子单金额 ≤ 阈值）
依次提交。关键约束（F7 同向在途闸 + 柜台按笔冻结资金）：子单必须成交驱动——
子单 1 成交回填后才能提交子单 2（_tick_deals 回填循环末尾续发）。

拆分公式：n=ceil(金额/阈值)，每子单数量=vol//n，余数依次补给前 vol%n 笔（Σ=vol）。
子单 bar_time 毫秒级递增：D6 跨重启去重键互异、收盘/日历/deferred 判定不变。
"""

import asyncio
import hashlib
from datetime import datetime
from decimal import Decimal

from core.engine.bar_poller import BarPoller
from core.engine.event import OrderEvent
from core.engine.http_bridge_dispatcher import BridgeOrderRejected
from core.engine.live_engine import LiveEngine
from core.engine.portfolio import Portfolio
from core.engine.risk_manager import PortfolioRiskManager, StrategyRiskManager
from core.engine.strategy_context import StrategyContext
from core.engine.live.timing import now_shanghai
from core.models import Base, LiveDecisionEvent, LiveOrder
from tq_iquant_shared.constants import SignalType, TradeType

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

STOCK = "600000.SH"


# ---------------- fixtures ----------------
def _db_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def factory():
        return SessionLocal()

    return factory, engine


class _FakeTdxDispatcher:
    """TDX 通道 fake：place 即成（可关），支持注入拒单。

    委托/成交行形状对齐 OrderStateMachine 的 legacy 模糊匹配与 /deals 回填
    （source=BRIDGE、direction 48/49、insert_date/insert_time 上海本地、无 remark）。
    """

    channel = "tdx_tq"
    supports_remark = False

    def __init__(self):
        self.broker_orders = []
        self.broker_deals = []
        self.reject_next = False
        self.auto_fill = True
        self._ref_seq = 0

    @staticmethod
    def order_id(order):
        key = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            order.strategy_id, order.portfolio_id, order.stock_code,
            order.trade_type.value, order.signal_type.value, order.signal_name,
            order.quantity, order.price,
        )
        if order.bar_time is not None:
            key += "|%s" % order.bar_time.isoformat()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    def heartbeat(self):
        return True

    def close(self):
        pass

    def query_calendar(self):
        return []  # fail-open：工作日放行

    def place_order(self, order):
        if self.reject_next:
            self.reject_next = False
            raise BridgeOrderRejected("fake reject")
        self._ref_seq += 1
        ref = "REF%03d" % self._ref_seq
        now = now_shanghai()
        inst, _, exch = order.stock_code.partition(".")
        self.broker_orders.append({
            "source": "BRIDGE", "instrument": inst, "exchange": exch,
            "direction": 48 if order.trade_type == TradeType.BUY else 49,
            "volume": order.quantity, "order_ref": ref,
            "insert_date": now.strftime("%Y%m%d"),
            "insert_time": now.strftime("%H%M%S"),
        })
        if self.auto_fill:
            self.broker_deals.append({
                "order_ref": ref, "price": float(order.price),
                "volume": order.quantity,
                "amount": float(order.price) * order.quantity,
                "commission": 0.0,
                "trade_time": now.strftime("%H%M%S"),
                "trade_date": now.strftime("%Y%m%d"),
            })
        from core.engine.event import TradeEvent
        return TradeEvent(
            strategy_id=order.strategy_id, portfolio_id=order.portfolio_id,
            stock_code=order.stock_code, trade_type=order.trade_type,
            price=order.price, quantity=order.quantity,
            amount=order.price * order.quantity, commission=Decimal("0"),
            stamp_duty=Decimal("0"),
            trade_time=order.bar_time or now_shanghai(),
            signal_type=order.signal_type, signal_name=order.signal_name,
        )

    def query_orders(self, order_id=None):
        return list(self.broker_orders)

    def query_deals(self, order_id=None):
        return list(self.broker_deals)

    def query_positions(self):
        return []

    def query_account(self):
        return []

    def query_quote(self, code, period="1m", count=10):
        return []

    def check_price_limit(self, stock_code, direction, price):
        return None


def _portfolio_split(threshold=Decimal("10000"), enabled=True, strategy_id=1):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"),
                              daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"),
                     risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=strategy_id, period="1m",
        capital_ratio=Decimal("0.6"), max_positions=5,
        single_open_ratio=Decimal("0.1"),
        split_order_enabled=enabled,
        split_order_threshold=threshold,
    )
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"),
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def _make_engine(disp, factory, port):
    poller = BarPoller(disp, [STOCK], period="1m", count=10)
    return LiveEngine(
        session_id=1, portfolios=[port], dispatcher=disp,
        bar_poller=poller, db_session_factory=factory,
    )


def _order(qty, price=Decimal("10"), bar_time=None, strategy_id=1):
    return OrderEvent(
        strategy_id=strategy_id, portfolio_id=1, stock_code=STOCK,
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN,
        quantity=qty, price=price,
        bar_time=bar_time or datetime(2026, 8, 5, 10, 0),
        signal_name="open_sig",
    )


def _submit(engine, port, order):
    db = engine._db_session_factory()
    try:
        engine._submit_one_order(db, port, order, "1m")
        db.commit()
    finally:
        db.close()


def _rows(factory):
    db = factory()
    try:
        return db.query(LiveOrder).order_by(LiveOrder.id).all()
    finally:
        db.close()


def _split_decisions(factory):
    db = factory()
    try:
        return (
            db.query(LiveDecisionEvent)
            .filter(LiveDecisionEvent.gate == "split_order")
            .order_by(LiveDecisionEvent.id)
            .all()
        )
    finally:
        db.close()


# ---------------- 拆单资格 ----------------
def test_split_eligible_creates_fill_driven_queue():
    """金额≥阈值 + tdx 通道 + 已启用 → 拆 2 笔：仅子单 1 落库，其余入队列。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1001))  # 1001×10=10010 ≥ 10000 → n=2

    rows = _rows(factory)
    assert len(rows) == 1  # 原单不落库，仅子单 1
    assert rows[0].quantity == 501
    assert rows[0].status == "submitted"
    entry = engine._split_children[(1, STOCK)]
    assert entry["inflight"].quantity == 501
    assert [c.quantity for c in entry["pending"]] == [500]  # Σ=1001
    events = engine._recorder.drain()
    splits = [e for e in events if e.gate == "split_order" and e.action == "split"]
    assert len(splits) == 1
    assert "2 笔子单" in (splits[0].message or "")


def test_remainder_distributed_to_first_children():
    """余数补给前 vol%n 笔：10001 股 @1 → n=2 → [5001, 5000]，Σ=原量。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(10001, price=Decimal("1")))

    entry = engine._split_children[(1, STOCK)]
    assert entry["inflight"].quantity == 5001
    assert [c.quantity for c in entry["pending"]] == [5000]


def test_split_disabled_single_order():
    """未启用拆单 → 单笔原样提交。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split(enabled=False)
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1001))

    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].quantity == 1001
    assert engine._split_children == {}


def test_below_threshold_single_order():
    """金额 < 阈值 → 单笔原样。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(999))  # 9990 < 10000

    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].quantity == 999
    assert engine._split_children == {}


def test_non_tdx_channel_single_order():
    """非 tdx_tq 通道 → 单笔原样（拆单仅 TDX 通道）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    disp.channel = "iquant_bridge"  # 实例属性覆盖类属性
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1001))

    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].quantity == 1001
    assert engine._split_children == {}


def test_amount_equal_threshold_no_split():
    """阈值边界：金额=阈值 → N=1 不拆。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1000))  # 1000×10=10000 == 阈值

    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].quantity == 1000
    assert engine._split_children == {}
    assert not [e for e in engine._recorder.drain() if e.gate == "split_order"]


def test_amount_threshold_plus_one_splits_two():
    """阈值边界：金额=阈值+1 → 拆 2 笔。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(10001, price=Decimal("1")))  # 10001 = 阈值+1

    entry = engine._split_children[(1, STOCK)]
    assert entry["inflight"].quantity == 5001
    assert [c.quantity for c in entry["pending"]] == [5000]


# ---------------- 成交驱动续发 ----------------
def test_fill_driven_child2_submitted_after_child1_filled():
    """子 1 成交回填 → 引擎 tick → 子 2 自动提交；全队列成交 → complete 清队。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1001))
    engine._tick_deals()  # 回填子 1 filled → 续发子 2

    rows = _rows(factory)
    assert [(r.quantity, r.status) for r in rows] == [(501, "filled"), (500, "submitted")]
    assert ctx.positions[STOCK].quantity == 501  # 子 1 已 apply
    entry = engine._split_children[(1, STOCK)]
    assert entry["inflight"].quantity == 500
    assert entry["pending"] == []

    engine._tick_deals()  # 回填子 2 filled → 队列完成

    rows = _rows(factory)
    assert [(r.quantity, r.status) for r in rows] == [(501, "filled"), (500, "filled")]
    assert ctx.positions[STOCK].quantity == 1001
    assert port.account.cash == Decimal("100000") - Decimal("10010")
    assert (1, STOCK) not in engine._split_children
    decisions = _split_decisions(factory)
    assert any(d.action == "complete" for d in decisions)


def test_child1_rejected_aborts_queue():
    """子 1 废单/被拒 → 整队丢弃 + abort 决策事件（message 含剩余数量）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    disp.reject_next = True
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1001))
    rows = _rows(factory)
    assert len(rows) == 1 and rows[0].status == "rejected"
    assert (1, STOCK) in engine._split_children

    engine._tick_deals()

    assert (1, STOCK) not in engine._split_children
    aborts = [d for d in _split_decisions(factory) if d.action == "abort"]
    assert len(aborts) == 1
    assert "1001" in (aborts[0].message or "")  # 剩余 = 501 未成 + 500 未提交


def test_same_key_new_signal_appends_to_queue():
    """同策略同票新信号撞键 → 新单子单排队等待当前队列完成（append 语义）。"""
    factory, _ = _db_factory()
    port, ctx = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    t1 = datetime(2026, 8, 5, 10, 0)
    t2 = datetime(2026, 8, 5, 10, 5)
    _submit(engine, port, _order(1001, bar_time=t1))
    engine._poll_deals()  # 仅回填子 1 filled（不续发，制造撞键窗口）

    _submit(engine, port, _order(1001, bar_time=t2))

    entry = engine._split_children[(1, STOCK)]
    assert [c.quantity for c in entry["pending"]] == [500, 501, 500]
    assert len(_rows(factory)) == 1  # B 未立即落库（排队）

    for _ in range(4):
        engine._tick_deals()

    rows = _rows(factory)
    assert [(r.quantity, r.status) for r in rows] == [
        (501, "filled"), (500, "filled"), (501, "filled"), (500, "filled"),
    ]
    assert ctx.positions[STOCK].quantity == 2002
    assert (1, STOCK) not in engine._split_children
    assert any(d.action == "complete" for d in _split_decisions(factory))


def test_split_children_do_not_cross_bind_same_volume_refs():
    """等量子单不得误绑前子单的 order_ref（提交时认领须带 DB 已占 refs）。

    同票同向等量的子单 2 在提交时同步定位 ref，若 claimed 集为空会绑到子单 1
    的委托行 → /deals 按 ref 双份回填 → 持仓翻倍。修复后提交时认领携带
    DB 全部已占 ref，子单 2 只能绑自己的委托行。
    """
    factory, _ = _db_factory()
    port, ctx = _portfolio_split(threshold=Decimal("5000"))
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)

    _submit(engine, port, _order(1000))  # 10000 ≥ 5000 → n=2 → [500, 500]

    engine._tick_deals()
    engine._tick_deals()

    rows = _rows(factory)
    assert [(r.quantity, r.status) for r in rows] == [(500, "filled"), (500, "filled")]
    assert rows[0].order_ref != rows[1].order_ref
    assert ctx.positions[STOCK].quantity == 1000


def test_stop_clears_split_children():
    """stop() 清空拆单队列（未提交子单随队列丢弃，在途单由撤单兜底）。"""
    factory, _ = _db_factory()
    port, _ = _portfolio_split()
    disp = _FakeTdxDispatcher()
    engine = _make_engine(disp, factory, port)
    engine._split_children[(1, STOCK)] = {
        "inflight": _order(501), "pending": [_order(500)],
        "portfolio": port, "period": "1m",
    }

    asyncio.run(engine.stop())

    assert engine._split_children == {}
