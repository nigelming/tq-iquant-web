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
    pool = StockPool(name="test_pool")
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
