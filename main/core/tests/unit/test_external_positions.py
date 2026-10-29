"""实盘持仓分级显示（策略持仓/原有持仓）单元测试。

- engine_external_positions：dispatcher 真实持仓并入，虚拟持仓已持有代码剔除，
  TTL 60s 缓存（60s 内二次调用不重读 dispatcher），异常/缺方法 → 无外部行
- session_positions：运行中虚拟行标 source=strategy 并追加外部行；停止态重放仅
  strategy（无外部数据可得）
- ThsUiDispatcher.get_positions：UI 原始读回 → 通用形状（6 位代码补交易所后缀）
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.engine.position import Position
from core.models import Base, LiveSession, LiveTrade
from core.services import live_service


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _FakeCtx:
    def __init__(self, strategy_id, positions):
        self.strategy_id = strategy_id
        self.positions = positions


class _FakePortfolio:
    def __init__(self, portfolio_id, strategies):
        self.portfolio_id = portfolio_id
        self.strategies = strategies


class _FakeEngine:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.portfolios = [_FakePortfolio(1, [_FakeCtx(1, {})])]

    def hold(self, code, qty, price):
        pos = Position(code)
        pos.buy(qty, Decimal(price))
        self.portfolios[0].strategies[0].positions[code] = pos


class _FakeDispatcher:
    """TDX 口径：get_positions 返回 6 位无后缀 symbol 的通用形状行。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        return self.rows


def _virtual_engine(dispatcher, code="600000.SH"):
    eng = _FakeEngine(dispatcher)
    eng.hold(code, 500, "10")
    return eng


def test_external_rows_labeled_and_virtual_excluded():
    disp = _FakeDispatcher([
        {"symbol": "600000", "volume": 500, "available": 500,
         "cost": 10.0, "market_value": 5200.0},
        {"symbol": "000001", "volume": 1000, "available": 1000,
         "cost": 12.0, "market_value": 13000.0},
        {"symbol": "510300", "volume": 300, "available": 300,
         "cost": 4.0, "market_value": 1260.0},
    ])
    eng = _virtual_engine(disp)
    rows = live_service.engine_external_positions(eng)
    assert [r["stock_code"] for r in rows] == ["000001.SZ", "510300.SH"]
    assert all(r["source"] == "external" for r in rows)
    assert all(r["portfolio_id"] is None and r["strategy_id"] is None for r in rows)
    assert rows[0]["quantity"] == 1000
    assert rows[0]["avg_cost"] == 12.0
    assert rows[0]["market_value"] == 13000.0


def test_session_positions_running_labels_strategy_and_external():
    db = _db()
    db.add(LiveSession(id=1, name="s", mode="simulation", status="running",
                       engine="tdx"))
    db.commit()
    disp = _FakeDispatcher([
        {"symbol": "000001", "volume": 1000, "available": 1000,
         "cost": 12.0, "market_value": 13000.0},
    ])
    eng = _virtual_engine(disp)
    rows = live_service.session_positions(db, 1, eng)
    by_code = {r["stock_code"]: r for r in rows}
    assert by_code["600000.SH"]["source"] == "strategy"
    assert by_code["600000.SH"]["portfolio_id"] == 1
    assert by_code["000001.SZ"]["source"] == "external"
    assert by_code["000001.SZ"]["portfolio_id"] is None


def test_external_positions_ttl_cache_skips_reread():
    disp = _FakeDispatcher([
        {"symbol": "000001", "volume": 1000, "available": 1000,
         "cost": 12.0, "market_value": 13000.0},
    ])
    eng = _virtual_engine(disp)
    live_service.engine_external_positions(eng)
    live_service.engine_external_positions(eng)
    assert disp.calls == 1  # TTL 60s 内不重读 dispatcher
    ts, cached = eng._external_pos_cache
    eng._external_pos_cache = (ts - 61.0, cached)
    live_service.engine_external_positions(eng)
    assert disp.calls == 2  # 过期后重读
    assert cached == disp.rows  # 缓存的是 dispatcher 原始行


def test_external_positions_dispatcher_exception_returns_empty():
    class _Boom:
        def get_positions(self):
            raise RuntimeError("bridge down")

    eng = _virtual_engine(_Boom())
    assert live_service.engine_external_positions(eng) == []


def test_external_positions_missing_get_positions_returns_empty():
    """dispatcher 无 get_positions（如 iQuant 桥）→ 无外部行，不抛错。"""
    eng = _virtual_engine(object())
    assert live_service.engine_external_positions(eng) == []


def test_external_positions_skips_zero_volume_rows():
    disp = _FakeDispatcher([
        {"symbol": "000001", "volume": 0, "available": 0,
         "cost": 12.0, "market_value": 0.0},
    ])
    eng = _virtual_engine(disp)
    assert live_service.engine_external_positions(eng) == []


def test_external_positions_drops_unnormalizable_symbol():
    disp = _FakeDispatcher([
        {"symbol": "abc", "volume": 100, "available": 100,
         "cost": 1.0, "market_value": 100.0},
    ])
    eng = _virtual_engine(disp)
    assert live_service.engine_external_positions(eng) == []


def test_session_positions_stopped_replay_strategy_only():
    db = _db()
    db.add(LiveSession(id=2, name="s2", mode="simulation", status="stopped",
                       engine="tdx"))
    db.add(LiveTrade(
        live_session_id=2, portfolio_strategy_id=1, strategy_id=1,
        stock_code="000001.SZ", trade_type="BUY", price=Decimal("12"),
        quantity=1000, amount=Decimal("12000"), commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=datetime(2026, 10, 28, 9, 31)))
    db.commit()
    rows = live_service.session_positions(db, 2, None)
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "000001.SZ"
    assert rows[0]["source"] == "strategy"


def test_ths_get_positions_normalizes_to_universal_shape():
    from core.engine.ths_ui_dispatcher import ThsUiDispatcher

    class _FakeThs(ThsUiDispatcher):
        def __init__(self):
            super().__init__()
            self._connected = True

        def _read_position_rows(self):
            return [
                {"symbol": "000001", "name": "平安银行", "volume": 1000,
                 "available": 0, "cost": 10.0, "price": 10.5,
                 "market_value": 10500.0},
                {"symbol": "510300", "name": "沪深ETF", "volume": 300,
                 "available": 300, "cost": 4.0, "price": 4.2,
                 "market_value": 1260.0},
            ]

    rows = _FakeThs().get_positions()
    assert rows == [
        {"symbol": "000001.SZ", "volume": 1000, "available": 0,
         "cost": 10.0, "market_value": 10500.0},
        {"symbol": "510300.SH", "volume": 300, "available": 300,
         "cost": 4.0, "market_value": 1260.0},
    ]


def test_ths_service_reads_ths_positions_as_external():
    """THS 通道 get_positions（带后缀 symbol）经服务层归一 → 外部行。"""
    from core.engine.ths_ui_dispatcher import ThsUiDispatcher

    class _FakeThsDispatcher(ThsUiDispatcher):
        def __init__(self):
            super().__init__()
            self._connected = True

        def _read_position_rows(self):
            return [
                {"symbol": "600519", "name": "贵州茅台", "volume": 100,
                 "available": 100, "cost": 1700.0, "price": 1750.0,
                 "market_value": 175000.0},
            ]

    eng = _virtual_engine(_FakeThsDispatcher())
    rows = live_service.engine_external_positions(eng)
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "600519.SH"
    assert rows[0]["source"] == "external"
    assert rows[0]["quantity"] == 100
