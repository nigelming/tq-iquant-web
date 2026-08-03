from datetime import datetime, date
from decimal import Decimal

import polars as pl
import pytest
from fastapi.testclient import TestClient

from core.main import app
from core.db import get_db
from core.models import (
    Base, StockPool, Formula, FormulaSignal,
    PortfolioStrategy, Strategy, BacktestRecord,
    BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
)
import core.api.backtest as bt_api


@pytest.fixture
def client(tmp_path):
    """内存 SQLite + TestClient，覆盖 get_db。
    用 StaticPool 共享单连接（sqlite :memory: 每连接独立库）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _seed(db):
    """建最小依赖链：StockPool → Formula → FormulaSignal → PortfolioStrategy → Strategy。"""
    pool = StockPool(code="TEST", name="test_pool")
    db.add(pool)
    db.flush()
    formula = Formula(name="open_formula", content="REF(CLOSE,1)")
    db.add(formula)
    db.flush()
    sig = FormulaSignal(
        formula_id=formula.id, signal_name="open_sig",
        signal_type="OPEN", trigger_value=1,
    )
    db.add(sig)
    db.flush()
    ps = PortfolioStrategy(
        name="test_portfolio", stock_pool_id=pool.id,
        initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps)
    db.flush()
    strat = Strategy(
        portfolio_id=ps.id, name="s1", formula_id=formula.id,
        period="1d", role="master",
        capital_ratio=Decimal("0.6"), max_positions=5,
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    db.add(strat)
    db.commit()
    return ps.id, strat.id, formula.id


def _mock_klines():
    stock = "000001.SZ"
    df = pl.DataFrame({
        "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31)],
        "Open": [Decimal("10"), Decimal("10.2"), Decimal("9.0")],
        "High": [Decimal("10.3"), Decimal("10.5"), Decimal("9.2")],
        "Low": [Decimal("9.9"), Decimal("8.9"), Decimal("8.8")],
        "Close": [Decimal("10.2"), Decimal("9.0"), Decimal("9.1")],
        "Volume": [1000, 1000, 1000],
    })
    return {stock: {"1d": df}}


def test_post_backtest_end_to_end(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.close()

    stock = "000001.SZ"
    # monkeypatch 数据获取层：返回 mock klines / signal_cache / open_prices
    monkeypatch.setattr(bt_api, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(bt_api, "build_signal_cache", lambda ps, klines, db=None: {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })
    monkeypatch.setattr(bt_api, "build_open_prices", lambda ps, klines: {
        stock: {
            datetime(2026, 7, 30): Decimal("10.2"),
            datetime(2026, 7, 31): Decimal("9.0"),
        }
    })

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bt_test",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    }
    resp = c.post("/api/backtest", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    record_id = body["data"]["record_id"]

    # 校验 DB 写入
    db = Session()
    rec = db.get(BacktestRecord, record_id)
    assert rec is not None
    assert rec.status == "completed"
    assert rec.progress == 100
    assert rec.portfolio_strategy_id == ps_id

    trades = db.query(BacktestTrade).filter_by(backtest_record_id=record_id).all()
    assert len(trades) == 2  # BUY + SELL
    assert trades[0].trade_type == "BUY"
    assert trades[1].trade_type == "SELL"

    snaps = db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).all()
    assert len(snaps) == 3  # 3 个交易日

    evals = db.query(BacktestEvaluation).filter_by(backtest_record_id=record_id).all()
    assert len(evals) == 1
    assert evals[0].total_return is not None
    db.close()


def test_post_backtest_no_signal_no_trade(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.close()

    stock = "000001.SZ"
    monkeypatch.setattr(bt_api, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(bt_api, "build_signal_cache", lambda ps, klines, db=None: {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })
    monkeypatch.setattr(bt_api, "build_open_prices", lambda ps, klines: {stock: {}})

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bt_nosig",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    }
    resp = c.post("/api/backtest", json=payload)

    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    record_id = resp.json()["data"]["record_id"]

    db = Session()
    trades = db.query(BacktestTrade).filter_by(backtest_record_id=record_id).all()
    assert len(trades) == 0
    snaps = db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).all()
    assert len(snaps) == 3
    db.close()


# ===========================================================================
# list_records — 真实查询（非桩）
# ===========================================================================
def _mock_data(monkeypatch):
    """统一 mock 数据获取层，供多测试复用。"""
    monkeypatch.setattr(bt_api, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(bt_api, "build_signal_cache", lambda ps, klines, db=None: {
        (1, "000001.SZ", datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, "000001.SZ", datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, "000001.SZ", datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })
    monkeypatch.setattr(bt_api, "build_open_prices", lambda ps, klines: {
        "000001.SZ": {
            datetime(2026, 7, 30): Decimal("10.2"),
            datetime(2026, 7, 31): Decimal("9.0"),
        }
    })


def _post_backtest(c, ps_id):
    return c.post("/api/backtest", json={
        "portfolio_strategy_id": ps_id,
        "name": "bt_list_test",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    })


def test_list_records_returns_persisted(client, monkeypatch):
    """POST 跑回测后，GET /records 返回该记录（非空桩）。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    resp = _post_backtest(c, ps_id)
    record_id = resp.json()["data"]["record_id"]

    # GET 列表
    body = c.get("/api/backtest/records").json()
    assert body["code"] == 0
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1
    rec = next(r for r in body["data"] if r["id"] == record_id)
    assert rec["name"] == "bt_list_test"
    assert rec["status"] == "completed"
    assert rec["progress"] == 100
    assert rec["portfolio_strategy_id"] == ps_id
    assert "created_at" in rec


def test_get_record_detail(client, monkeypatch):
    """GET /records/{id} 返回 record + snapshots + trades + evaluations。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]

    body = c.get(f"/api/backtest/records/{record_id}").json()
    assert body["code"] == 0
    data = body["data"]

    # record 元信息
    assert data["record"]["id"] == record_id
    assert data["record"]["name"] == "bt_list_test"
    assert data["record"]["status"] == "completed"

    # snapshots：按日期升序，3 个交易日
    snaps = data["snapshots"]
    assert len(snaps) == 3
    assert snaps[0]["snap_date"] == "2026-07-29"
    assert snaps[2]["snap_date"] == "2026-07-31"
    for s in snaps:
        assert "total_value" in s and "cash" in s and "market_value" in s

    # trades：按 bar_time 升序，2 笔（BUY+SELL）
    trades = data["trades"]
    assert len(trades) == 2
    assert trades[0]["trade_type"] == "BUY"
    for t in trades:
        assert "stock_code" in t and "price" in t and "quantity" in t and "amount" in t

    # evaluations：单对象，含核心指标
    ev = data["evaluations"]
    assert ev is not None
    assert "total_return" in ev
    assert "max_drawdown" in ev
    assert "sharpe_ratio" in ev


def test_get_record_detail_not_found(client):
    """GET /records/9999 → 404。"""
    c, _ = client
    body = c.get("/api/backtest/records/9999").json()
    assert body["code"] == 404


# ===========================================================================
# 日期区间校验 — start 必须 < end；起止不可在未来。
# 防止 TQ 拉到空行情后静默"成功"标 completed（真机 bug 根因）。
# ===========================================================================
def test_post_backtest_start_after_end_rejected(client):
    """start_date 晚于 end_date → 400，不写 record。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bad_range",
        "start_date": "2026-08-31",
        "end_date": "2026-08-01",
    }
    resp = c.post("/api/backtest", json=payload)
    assert resp.json()["code"] == 400

    # 不应落库任何 record
    db = Session()
    recs = db.query(BacktestRecord).filter_by(name="bad_range").all()
    assert recs == []
    db.close()


def test_post_backtest_future_date_rejected(client):
    """start_date 在未来 → 400（今天 2026-08-03，2027 是未来）。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "future_range",
        "start_date": "2027-01-01",
        "end_date": "2027-06-01",
    }
    resp = c.post("/api/backtest", json=payload)
    assert resp.json()["code"] == 400


def test_post_backtest_empty_klines_marks_failed_not_completed(client, monkeypatch):
    """TQ 返回空行情（区间无交易日/股票池无数据）→ 标 failed 并写明原因，
    而非静默标 completed（这正是"启动了但没运行直接完成"的根因）。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    # mock 三层全返回空 — 模拟 TQ 拉不到行情
    monkeypatch.setattr(bt_api, "build_klines", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(bt_api, "build_signal_cache", lambda ps, klines, db=None: {})
    monkeypatch.setattr(bt_api, "build_open_prices", lambda ps, klines: {})

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "empty_klines",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    }
    resp = c.post("/api/backtest", json=payload)
    assert resp.json()["code"] == 0  # 请求本身成功，record 已建
    record_id = resp.json()["data"]["record_id"]

    db = Session()
    rec = db.get(BacktestRecord, record_id)
    assert rec.status == "failed"  # 不是 completed
    assert rec.error_message is not None and rec.error_message != ""
    db.close()

