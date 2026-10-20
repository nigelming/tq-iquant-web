import json
from datetime import datetime, date
from decimal import Decimal

import polars as pl
import pytest
from fastapi.testclient import TestClient

from core.main import app
from core.db import get_db
from core.models import (
    Base, StockPool, StockPoolStock, Formula, FormulaSignal,
    PortfolioStrategy, Strategy, BacktestRecord,
    BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
    BacktestCrisisAlpha,
)
import core.api.backtest as bt_api
import core.services.backtest_service as svc


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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _seed(db):
    """建最小依赖链：StockPool(+成分) → Formula → FormulaSignal → PortfolioStrategy → Strategy。

    成分股必须落 stock_pool_stocks：assemble_portfolio 按池解析 stock_pool 过滤信号
    （#3 跨池下单防护），回测 bar 只覆盖池内股票（_pool_stocks 决定 klines 范围）。
    """
    pool = StockPool(code="TEST", name="test_pool")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ", stock_name="平安银行"))
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
    # monkeypatch 数据获取层：返回 mock klines / signal_cache
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
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
    assert rec.portfolio_strategy_id == ps_id

    trades = db.query(BacktestTrade).filter_by(backtest_record_id=record_id).all()
    assert len(trades) == 2  # BUY + SELL
    assert trades[0].trade_type == "BUY"
    assert trades[1].trade_type == "SELL"

    snaps = db.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=record_id, target_type="portfolio"
    ).all()
    assert len(snaps) == 3  # 3 个交易日（组合层）

    evals = db.query(BacktestEvaluation).filter_by(
        backtest_record_id=record_id, target_type="portfolio"
    ).all()
    assert len(evals) == 1
    assert evals[0].total_return is not None
    db.close()


def test_post_backtest_back_dividend_mode_threaded(client, monkeypatch):
    """dividend_mode='back' 穿透到 build_klines/build_signal_cache/build_benchmark_data，
    且写入 params_snapshot。"""
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.close()

    seen = {}

    def _fake_klines(ps, start, end, db=None, dividend_mode="front"):
        seen["klines"] = dividend_mode
        return _mock_klines()

    def _fake_cache(ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front"):
        seen["cache"] = dividend_mode
        return {}

    def _fake_bench(ps, start, end, db=None, dividend_mode="front"):
        seen["bench"] = dividend_mode
        return {}

    monkeypatch.setattr(svc, "build_klines", _fake_klines)
    monkeypatch.setattr(svc, "build_signal_cache", _fake_cache)
    monkeypatch.setattr(svc, "build_benchmark_data", _fake_bench)

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bt_back",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "dividend_mode": "back",
    }
    resp = c.post("/api/backtest", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert seen == {"klines": "back", "cache": "back", "bench": "back"}

    record_id = resp.json()["data"]["record_id"]
    db = Session()
    rec = db.get(BacktestRecord, record_id)
    snap = json.loads(rec.params_snapshot)
    assert snap["dividend_mode"] == "back"
    db.close()


def test_post_backtest_invalid_dividend_mode_returns_400(client):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bt_bad",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "dividend_mode": "hfq",
    }
    resp = c.post("/api/backtest", json=payload)
    body = resp.json()
    assert body["code"] == 400
    assert "front/back" in body["message"]


def test_post_backtest_robustness_recompute(client, monkeypatch):
    """回测完成后自动算稳健性检验；POST recompute 返回 completed，详情含 robustness。"""
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.close()

    stock = "000001.SZ"
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })

    payload = {
        "portfolio_strategy_id": ps_id, "name": "bt_rb",
        "start_date": "2026-07-29", "end_date": "2026-07-31",
    }
    resp = c.post("/api/backtest", json=payload)
    record_id = resp.json()["data"]["record_id"]

    # 手动重算稳健性检验
    resp = c.post(f"/api/backtest/records/{record_id}/robustness/recompute")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "completed"

    detail = c.get(f"/api/backtest/records/{record_id}").json()["data"]
    assert detail["robustness"] is not None
    assert detail["robustness"]["status"] == "completed"
    assert "annual" in detail["robustness"]["data"]


def test_post_backtest_robustness_recompute_missing_404(client):
    c, _ = client
    resp = c.post("/api/backtest/records/99999/robustness/recompute")
    assert resp.json()["code"] == 404


def test_post_pool_compare_runs_and_returns_comparisons(client, monkeypatch):
    """换池对比：POST /robustness/compare 逐池跑回测并返回对比结果。"""
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.add(StockPool(code="T9", name="池9"))
    db.add(StockPool(code="T10", name="池10"))
    db.commit()
    pools = {r.code: r.id for r in db.query(StockPool).all()}
    db.close()

    stock = "000001.SZ"
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})

    payload = {
        "portfolio_strategy_id": ps_id,
        "pool_ids": [pools["T9"], pools["T10"]],
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "dividend_mode": "dual",
    }
    resp = c.post("/api/backtest/robustness/compare", json=payload)
    body = resp.json()
    assert body["code"] == 0
    comps = body["data"]["comparisons"]
    assert len(comps) == 2
    assert all(c["status"] == "completed" for c in comps)
    assert all(c["record_id"] is not None for c in comps)


def test_post_pool_compare_empty_pools_400(client):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    payload = {
        "portfolio_strategy_id": ps_id, "pool_ids": [],
        "start_date": "2026-07-29", "end_date": "2026-07-31",
    }
    resp = c.post("/api/backtest/robustness/compare", json=payload)
    assert resp.json()["code"] == 400


def test_post_backtest_no_signal_no_trade(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, strat_id, formula_id = _seed(db)
    db.close()

    stock = "000001.SZ"
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })

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
    snaps = db.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=record_id, target_type="portfolio"
    ).all()
    assert len(snaps) == 3
    db.close()


# ===========================================================================
# list_records — 真实查询（非桩）
# ===========================================================================
def _mock_data(monkeypatch):
    """统一 mock 数据获取层，供多测试复用。"""
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {
        (1, "000001.SZ", datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, "000001.SZ", datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, "000001.SZ", datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    })
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "safe_compute_market_regime", lambda *a, **kw: None)


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
# DELETE /records/{id} — 删 record + 级联子表（trades/snapshots/evaluations）
# ===========================================================================
def test_delete_record_removes_record_and_children(client, monkeypatch):
    """DELETE /records/{id} → 删 record，trades/snapshots/evaluations 全清。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]

    # 删前：子表有数据（按 target_type 分层计数）
    db = Session()
    assert db.query(BacktestTrade).filter_by(backtest_record_id=record_id).count() == 2
    assert db.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=record_id, target_type="portfolio").count() == 3
    assert db.query(BacktestEvaluation).filter_by(
        backtest_record_id=record_id, target_type="portfolio").count() == 1
    db.close()

    resp = c.delete(f"/api/backtest/records/{record_id}")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 删后：record + 三张子表全空（含策略层）
    db = Session()
    assert db.get(BacktestRecord, record_id) is None
    assert db.query(BacktestTrade).filter_by(backtest_record_id=record_id).count() == 0
    assert db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).count() == 0
    assert db.query(BacktestEvaluation).filter_by(backtest_record_id=record_id).count() == 0
    db.close()


def test_delete_record_not_found(client):
    """DELETE /records/9999 → 404。"""
    c, _ = client
    body = c.delete("/api/backtest/records/9999").json()
    assert body["code"] == 404


def test_delete_record_running_returns_409(client):
    """#4: 删除 status=running 的回测记录 → 409（运行中拒绝删除）。

    防止并发删除运行中的回测引发 FK 500 / 数据竞争。回测同步内联执行，
    运行中即代表有另一请求正在跑回测，删除会破坏其写入。
    """
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    rec = BacktestRecord(
        portfolio_strategy_id=ps_id, name="running_one",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="running",
    )
    db.add(rec)
    db.commit()
    rid = rec.id
    db.close()

    resp = c.delete(f"/api/backtest/records/{rid}")
    body = resp.json()
    assert body["code"] == 409
    assert "运行中" in body["message"]

    # 记录仍在，状态不变
    db = Session()
    still = db.get(BacktestRecord, rid)
    assert still is not None
    assert still.status == "running"
    db.close()


def test_lifespan_recovers_stale_running_backtests_on_startup(monkeypatch):
    """#5: 应用启动 lifespan 会调用 recover_stale_running_backtests 清理僵尸 running
    记录（进程崩溃/重启残留），防止前端永久显示"运行中"。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.main import app

    called = {"n": 0, "db_ok": False}

    def fake_recover(db):
        called["n"] += 1
        # 校验传入的是可用 Session（表已建好）
        from core.models import BacktestRecord
        assert db.query(BacktestRecord).count() == 0  # 启动时本应空
        called["db_ok"] = True
        return 0

    # 避免 lifespan 跑 Alembic 真实迁移 + 连真实 dev.db（只测 recover 接线）
    monkeypatch.setattr("core.main.init_db", lambda: None)
    monkeypatch.setattr("core.main.recover_stale_running_backtests", fake_recover)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("core.main.SessionLocal", TestSession)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False):
            assert called["n"] == 1
            assert called["db_ok"] is True
    finally:
        app.dependency_overrides.clear()


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
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})

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


def test_post_backtest_engine_exception_persists_failed(client, monkeypatch):
    """引擎/TQ 抛异常时（如 polars panic），record 必须落库为 failed（非永久 running）。
    验证 except 块的 rollback+重提交能持久化失败状态。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    # build_klines 抛异常 — 模拟 polars PanicException 等真机错误
    def _boom(ps, start, end, db=None, dividend_mode="front"):
        raise RuntimeError("polars panic: str cannot be int")
    monkeypatch.setattr(svc, "build_klines", _boom)

    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "boom_test",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    }
    # raise_server_exceptions 在 TestClient 构造时已关，异常转成 500 响应
    resp = c.post("/api/backtest", json=payload)
    assert resp.status_code == 500  # 异常上抛
    # #13：全局 Exception 处理器 → 统一 envelope（非 FastAPI 默认 {"detail":...}）
    body = resp.json()
    assert body["code"] == 500
    assert body["data"] is None
    assert "message" in body and body["message"]

    # 从 DB 直接查（异常上抛后端点未返回 record_id，但 record 已建）
    db = Session()
    rec = db.query(BacktestRecord).filter_by(name="boom_test").first()
    assert rec is not None
    assert rec.status == "failed"  # 关键：不是 running
    assert "polars panic" in rec.error_message
    db.close()


def _post_payload(ps_id, name):
    return {
        "portfolio_strategy_id": ps_id,
        "name": name,
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
    }


def test_post_backtest_409_when_already_running(client, monkeypatch):
    """并发保护：已达并发上限（占满 slot）→ 新 POST 返回 HTTP 409，不被放行。

    回测是同步内联执行（一个请求阻塞到跑完），两并发请求不会真的重叠，故用
    「直接占满并发 slot」模拟"回测进行中"：占满后 _try_acquire_slot 失败 → 409。
    释放 slot 后正常放行。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    payload = _post_payload(ps_id, "conflict_test")

    # 占满并发 slot 模拟"已有回测在跑" → 新 POST 应 409（HTTP 状态码，非 body code）
    monkeypatch.setattr(bt_api, "_max_concurrent_backtest", lambda: 1)
    bt_api._running_backtests = 1
    try:
        resp = c.post("/api/backtest", json=payload)
        assert resp.status_code == 409
        assert "回测正在进行中" in resp.json()["detail"]
        # 未被放行 → 不应落库 record
        db = Session()
        assert db.query(BacktestRecord).filter_by(name="conflict_test").count() == 0
        db.close()
    finally:
        bt_api._running_backtests = 0

    # slot 释放后 → 正常放行
    resp2 = c.post("/api/backtest", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["code"] == 0


def test_post_backtest_lock_released_after_failure(client, monkeypatch):
    """回测中途抛异常（→ 500）后，锁必须已释放——后续回测不被 409 误拒（防永久楔死）。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    payload = _post_payload(ps_id, "wedge_test")

    def _boom(ps, start, end, db=None):
        raise RuntimeError("polars panic: str cannot be int")
    monkeypatch.setattr(svc, "build_klines", _boom)

    resp = c.post("/api/backtest", json=payload)
    assert resp.status_code == 500  # 异常上抛
    # #13：500 响应体为统一 envelope
    assert resp.json()["code"] == 500

    # 恢复正常数据层 → 再次 POST 应 200（锁未被异常卡死）
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    resp2 = c.post("/api/backtest", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["code"] == 0


def test_post_recompute_crisis_alpha_returns_status(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    monkeypatch.setattr(
        bt_api, "_svc_recompute_crisis_alpha",
        lambda db, rid: {"status": "completed", "error": None, "computed_at": "2026-09-25T10:00:00"},
    )
    body = c.post(f"/api/backtest/records/{record_id}/crisis-alpha/recompute").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "completed"
    assert body["data"]["computed_at"] == "2026-09-25T10:00:00"


def test_post_recompute_crisis_alpha_not_found(client):
    c, _ = client
    body = c.post("/api/backtest/records/999999/crisis-alpha/recompute").json()
    assert body["code"] == 404


def test_post_recompute_market_regime_returns_status(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    monkeypatch.setattr(
        bt_api, "_svc_recompute_market_regime",
        lambda db, rid: {"status": "completed", "error": None, "computed_at": "2026-09-27T10:00:00"},
    )
    body = c.post(f"/api/backtest/records/{record_id}/market-regime/recompute").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "completed"
    assert body["data"]["computed_at"] == "2026-09-27T10:00:00"


def test_post_recompute_market_regime_not_found(client):
    c, _ = client
    body = c.post("/api/backtest/records/999999/market-regime/recompute").json()
    assert body["code"] == 404


def test_post_recompute_evaluation_returns_payload(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    monkeypatch.setattr(
        bt_api, "_svc_recompute_evaluation",
        lambda db, rid: {"status": "ok", "evaluations": {"beta": 0.5}},
    )
    body = c.post(f"/api/backtest/records/{record_id}/evaluations/recompute").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["evaluations"]["beta"] == 0.5


def test_post_recompute_evaluation_not_found(client):
    c, _ = client
    body = c.post("/api/backtest/records/999999/evaluations/recompute").json()
    assert body["code"] == 404


def test_export_html_returns_attachment_with_disposition(client, monkeypatch):
    """GET /api/backtest/records/{id}/export/html：text/html + Content-Disposition attachment。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]

    resp = c.get(f"/api/backtest/records/{record_id}/export/html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "attachment" in resp.headers.get("content-disposition", "")
    # 单文件：无外链 CSS/JS（除内嵌 ECharts）；含报告标题与净值图表容器
    assert "<html" in resp.text
    assert "回测分析报告" in resp.text
    assert 'class="chart"' in resp.text
    # 不允许引用外部资源（script src / link href）
    assert 'src="http' not in resp.text
    assert 'href="http' not in resp.text
    assert 'link rel' not in resp.text


def test_export_html_not_found(client):
    c, _ = client
    resp = c.get("/api/backtest/records/999999/export/html")
    assert resp.status_code == 200  # 统一 envelope：HTTP 200 + code 404
    assert resp.json()["code"] == 404


def test_get_record_detail_includes_crisis_alpha(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    monkeypatch.setattr(
        bt_api, "_svc_recompute_crisis_alpha",
        lambda db, rid: {"status": "completed", "error": None, "computed_at": "2026-09-25T10:00:00"},
    )
    db = Session()
    db.add(BacktestCrisisAlpha(
        backtest_record_id=record_id,
        benchmark_code="000300.SH",
        benchmark_name="PROBE_BENCH",
        status="insufficient",
        error="probe error text",
        skipped_benchmarks_json=json.dumps(["000905.SH", "399006.SZ"]),
        payload_json=json.dumps({"total_score": 3}),
        computed_at=datetime(2026, 9, 25, 10, 0, 0),
    ))
    db.commit()
    db.close()

    c.post(f"/api/backtest/records/{record_id}/crisis-alpha/recompute")
    body = c.get(f"/api/backtest/records/{record_id}").json()
    assert body["code"] == 0
    ca = body["data"]["crisis_alpha"]
    assert ca is not None
    assert ca["status"] == "insufficient"
    assert ca["benchmark_code"] == "000300.SH"
    assert ca["benchmark_name"] == "PROBE_BENCH"
    assert ca["skipped_benchmarks"] == ["000905.SH", "399006.SZ"]
    assert ca["error"] == "probe error text"
    assert ca["computed_at"] == "2026-09-25T10:00:00"
    assert ca["data"] == {"total_score": 3}


# ===========================================================================
# B6：evaluation_profile 落库 + 详情 acceptance 块 + ?profile= 临时切换口径
# ===========================================================================
def test_evaluation_profile_persisted_and_acceptance_block(client, monkeypatch):
    """POST 带 evaluation_profile='relative' → 落库；详情 acceptance 按 relative 口径。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    resp = c.post("/api/backtest", json={
        "portfolio_strategy_id": ps_id,
        "name": "bt_profile_relative",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "evaluation_profile": "relative",
    })
    assert resp.status_code == 200
    record_id = resp.json()["data"]["record_id"]

    db = Session()
    assert db.get(BacktestRecord, record_id).evaluation_profile == "relative"
    db.close()

    data = c.get(f"/api/backtest/records/{record_id}").json()["data"]
    assert data["record"]["evaluation_profile"] == "relative"
    acc = data["acceptance"]
    assert acc["profile"] == "relative"
    assert {chk["metric"] for chk in acc["checks"]} == {
        "information_ratio", "excess_annual_return", "down_capture_ratio"}
    assert "pass" in acc


def test_acceptance_default_absolute_and_profile_override(client, monkeypatch):
    """默认 absolute；?profile=relative 仅临时切换 acceptance 口径，不改记录存储值。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]

    # 默认：absolute 口径
    data = c.get(f"/api/backtest/records/{record_id}").json()["data"]
    assert data["record"]["evaluation_profile"] == "absolute"
    acc = data["acceptance"]
    assert acc["profile"] == "absolute"
    assert {chk["metric"] for chk in acc["checks"]} == {
        "annual_return", "max_drawdown", "calmar_ratio"}

    # ?profile=relative：acceptance 切换，record 序列化与落库值不变
    data2 = c.get(f"/api/backtest/records/{record_id}?profile=relative").json()["data"]
    assert data2["acceptance"]["profile"] == "relative"
    assert data2["record"]["evaluation_profile"] == "absolute"
    db = Session()
    assert db.get(BacktestRecord, record_id).evaluation_profile == "absolute"
    db.close()


def test_evaluation_profile_invalid_falls_back_to_absolute(client, monkeypatch):
    """非法 evaluation_profile → 回落 absolute 落库（防御性，不 422）。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    resp = c.post("/api/backtest", json={
        "portfolio_strategy_id": ps_id,
        "name": "bt_profile_bogus",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "evaluation_profile": "bogus",
    })
    assert resp.status_code == 200
    record_id = resp.json()["data"]["record_id"]
    db = Session()
    assert db.get(BacktestRecord, record_id).evaluation_profile == "absolute"
    db.close()


# ===========================================================================
# C7：IS/OOS 采纳门槛（默认关）——OOS CAGR ≥ ratio × IS CAGR 且 OOS 不跑输基准
# ===========================================================================
class _FakeRobustnessRow:
    """safe_compute_robustness 的替身：只带 payload_json。"""

    def __init__(self, payload):
        self.payload_json = json.dumps(payload) if payload is not None else None


def _oos_payload(in_cagr, out_cagr, out_return=0.30, bench=0.10):
    return {"oos": {
        "split_year": "2024",
        "in_sample": {"cagr": in_cagr, "return": 0.80, "benchmark_return": 0.20},
        "out_of_sample": {"cagr": out_cagr, "return": out_return, "benchmark_return": bench},
    }}


def _post_gate(c, ps_id, **extra):
    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "bt_gate",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "is_oos_gate_enabled": True,
    }
    payload.update(extra)
    return c.post("/api/backtest", json=payload)


def test_oos_gate_default_disabled(client, monkeypatch):
    """默认不启用：不评估，gate_status=None，POST 响应 gate=None。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    assert _post_backtest(c, ps_id).json()["data"].get("gate") is None

    db = Session()
    rec = db.get(BacktestRecord, record_id)
    assert rec.is_oos_gate_enabled is False
    assert rec.gate_status is None
    db.close()

    data = c.get(f"/api/backtest/records/{record_id}").json()["data"]
    assert data["record"]["is_oos_gate_enabled"] is False
    assert data["record"]["gate_status"] is None
    assert data["record"]["gate_reasons"] == []


def test_oos_gate_rejected_when_oos_collapses(client, monkeypatch):
    """启用且 OOS 崩塌（IS 24.8% → OOS 6.6% < 50%×IS）→ rejected + 人读原因，记录保留。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    monkeypatch.setattr(svc, "safe_compute_robustness",
                        lambda db, rid, **kw: _FakeRobustnessRow(_oos_payload(0.248, 0.066)))
    resp = _post_gate(c, ps_id)
    assert resp.status_code == 200
    gate = resp.json()["data"]["gate"]
    assert gate["status"] == "rejected"
    assert any("6.6%" in r and "24.8%" in r for r in gate["reasons"])
    record_id = resp.json()["data"]["record_id"]

    # 落库 + 记录保留（rejected 仅标记，不删除）
    db = Session()
    rec = db.get(BacktestRecord, record_id)
    assert rec is not None
    assert rec.status == "completed"
    assert rec.gate_status == "rejected"
    assert json.loads(rec.gate_reasons)
    db.close()

    data = c.get(f"/api/backtest/records/{record_id}").json()["data"]
    assert data["record"]["gate_status"] == "rejected"
    assert data["record"]["oos_gate_min_ratio"] == 0.5
    assert len(data["record"]["gate_reasons"]) == 1


def test_oos_gate_passed_when_healthy(client, monkeypatch):
    """启用且 OOS 健康（16.6% ≥ 12.4%，OOS 收益跑赢基准）→ passed。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    monkeypatch.setattr(svc, "safe_compute_robustness",
                        lambda db, rid, **kw: _FakeRobustnessRow(_oos_payload(0.248, 0.166)))
    resp = _post_gate(c, ps_id)
    gate = resp.json()["data"]["gate"]
    assert gate["status"] == "passed"
    assert gate["reasons"] == []

    db = Session()
    assert db.get(BacktestRecord, resp.json()["data"]["record_id"]).gate_status == "passed"
    db.close()


def test_oos_gate_not_evaluated_when_data_insufficient(client, monkeypatch):
    """启用但数据不足（真实稳健性：3 天快照仅 1 个日历年 → oos=None）→ gate_status=None。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)  # 不 mock robustness：真实计算，oos 必然 None
    resp = _post_gate(c, ps_id)
    gate = resp.json()["data"]["gate"]
    assert gate["status"] is None
    assert gate["reasons"]  # 人读原因：数据不足未评估

    db = Session()
    rec = db.get(BacktestRecord, resp.json()["data"]["record_id"])
    assert rec.gate_status is None
    assert rec.gate_reasons  # 原因仍落库
    db.close()


def test_oos_gate_invalid_ratio_400(client):
    """门槛比例越界（>2、<0.1 或 0）→ 400。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    resp = _post_gate(c, ps_id, oos_gate_min_ratio=3.0)
    assert resp.json()["code"] == 400
    resp = _post_gate(c, ps_id, oos_gate_min_ratio=0)
    assert resp.json()["code"] == 400
    resp = _post_gate(c, ps_id, oos_gate_min_ratio=0.05)
    assert resp.json()["code"] == 400


# ===========================================================================
# C8：walk-forward 滚动验证（滚动 3 年训练 + 1 年验证逐段前移）
# ===========================================================================
import core.services.walk_forward_service as wf_svc
from core.models import WalkForwardRun


def _wf_payload(ps_id, **extra):
    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "wf_test",
        "start_date": "2020-01-01",
        "end_date": "2023-01-01",
        "train_years": 1,
        "test_years": 1,
    }
    payload.update(extra)
    return payload


def test_walk_forward_happy_path(client, monkeypatch):
    """2 窗滚动（2020~2022 / 2021~2023）：子回测落库并关联 run、列表过滤子记录、
    run summary 聚合各窗门槛，全窗 passed → overall_pass=True。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    monkeypatch.setattr(
        wf_svc, "safe_compute_robustness",
        lambda db, rid, **kw: _FakeRobustnessRow(_oos_payload(0.248, 0.166)),
    )
    resp = c.post("/api/backtest/walk-forward", json=_wf_payload(ps_id))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["windows_passed"] == "2/2"
    assert data["overall_pass"] is True
    run_id = data["run_id"]

    # 子回测落库并关联 run；命名 名称#wN
    db = Session()
    children = db.query(BacktestRecord).filter_by(walk_forward_run_id=run_id).all()
    assert len(children) == 2
    assert {ch.name for ch in children} == {"wf_test#w1", "wf_test#w2"}
    assert all(ch.status == "completed" for ch in children)
    child_ids = {ch.id for ch in children}
    db.close()

    # 列表接口默认过滤子记录
    recs = c.get("/api/backtest/records").json()["data"]
    assert all(r["id"] not in child_ids for r in recs)

    # run 详情：summary 各窗带 IS/OOS 与门槛结果
    run = c.get(f"/api/backtest/walk-forward/{run_id}").json()["data"]
    assert run["status"] == "completed"
    assert run["train_years"] == 1 and run["test_years"] == 1
    summary = run["summary"]
    assert summary["overall_pass"] is True
    assert summary["windows_passed"] == "2/2"
    assert len(summary["windows"]) == 2
    w1 = summary["windows"][0]
    assert w1["train"] == {"start": "2020-01-01", "end": "2021-01-01"}
    assert w1["test"] == {"start": "2021-01-01", "end": "2022-01-01"}
    assert w1["is_cagr"] == 0.248 and w1["oos_cagr"] == 0.166
    assert w1["gate"]["status"] == "passed"
    assert w1["record_id"] in child_ids

    # 列表端点含该 run
    runs = c.get("/api/backtest/walk-forward").json()["data"]
    assert any(r["id"] == run_id for r in runs)


def test_walk_forward_delete_removes_run_and_children(client, monkeypatch):
    """删除 walk-forward：run 与子回测记录（含其快照/成交）一并删除。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    monkeypatch.setattr(
        wf_svc, "safe_compute_robustness",
        lambda db, rid, **kw: _FakeRobustnessRow(_oos_payload(0.248, 0.166)),
    )
    run_id = c.post("/api/backtest/walk-forward", json=_wf_payload(ps_id)).json()["data"]["run_id"]

    db = Session()
    children = db.query(BacktestRecord).filter_by(walk_forward_run_id=run_id).all()
    child_ids = [ch.id for ch in children]
    assert len(child_ids) == 2
    db.close()

    resp = c.delete(f"/api/backtest/walk-forward/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    db = Session()
    assert db.query(BacktestRecord).filter(BacktestRecord.id.in_(child_ids)).count() == 0
    assert db.query(WalkForwardRun).filter_by(id=run_id).first() is None
    db.close()
    # 再次删除 → body code=404（envelope 模式：HTTP 保持 200，code 与 HTTP 对齐）
    assert c.delete(f"/api/backtest/walk-forward/{run_id}").json()["code"] == 404


def test_walk_forward_overall_fail_when_window_rejected(client, monkeypatch):
    """任一窗 OOS 崩塌 → overall_pass=False，windows_passed 计数正确。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    monkeypatch.setattr(
        wf_svc, "safe_compute_robustness",
        lambda db, rid, **kw: _FakeRobustnessRow(_oos_payload(0.248, 0.066)),
    )
    data = c.post("/api/backtest/walk-forward", json=_wf_payload(ps_id)).json()["data"]
    assert data["status"] == "completed"
    assert data["windows_passed"] == "0/2"
    assert data["overall_pass"] is False

    run = c.get(f"/api/backtest/walk-forward/{data['run_id']}").json()["data"]
    assert run["summary"]["windows"][0]["gate"]["status"] == "rejected"
    assert run["summary"]["windows"][0]["gate"]["reasons"]


def test_walk_forward_insufficient_range_marks_failed(client, monkeypatch):
    """区间不足一个完整窗口 → run failed + 人读错误，无子回测。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    payload = _wf_payload(ps_id, start_date="2022-01-01", end_date="2023-06-01",
                          train_years=3, test_years=1)
    data = c.post("/api/backtest/walk-forward", json=payload).json()["data"]
    assert data["status"] == "failed"
    assert "不足一个完整窗口" in data["error"]

    db = Session()
    assert db.query(BacktestRecord).filter_by(
        walk_forward_run_id=data["run_id"]).count() == 0
    run = db.get(WalkForwardRun, data["run_id"])
    assert run.status == "failed" and run.error_message
    db.close()


def test_walk_forward_validation_errors(client):
    """ps 不存在 404；train_years<1 / 门槛越界 400；run 不存在 404。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    assert c.post("/api/backtest/walk-forward",
                  json=_wf_payload(99999)).json()["code"] == 404
    assert c.post("/api/backtest/walk-forward",
                  json=_wf_payload(ps_id, train_years=0)).json()["code"] == 400
    assert c.post("/api/backtest/walk-forward",
                  json=_wf_payload(ps_id, test_years=0)).json()["code"] == 400
    assert c.post("/api/backtest/walk-forward",
                  json=_wf_payload(ps_id, oos_gate_min_ratio=3.0)).json()["code"] == 400
    assert c.get("/api/backtest/walk-forward/99999").json()["code"] == 404


# ===========================================================================
# P2b：参数微扰敏感性分析（单参数逐一 ±pct，新表落库 + 独立端点）
# ===========================================================================
from core.models import Strategy


def _pp_payload(ps_id, **extra):
    payload = {
        "portfolio_strategy_id": ps_id,
        "name": "pp_test",
        "start_date": "2026-07-29",
        "end_date": "2026-07-31",
        "perturb_pct": 0.10,
    }
    payload.update(extra)
    return payload


def test_param_perturb_happy_path(client, monkeypatch):
    """基准 1 轮 + 9 参数 × 2 方向 18 轮（trailing_stop_ratio=0 跳过）：
    子回测落库并关联 run、列表过滤子记录、summary 聚合敏感度，
    且扰动不回写 strategies 表（expunge 保护）。"""
    c, Session = client
    db = Session()
    ps_id, strat_id, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    resp = c.post("/api/backtest/param-perturb", json=_pp_payload(ps_id))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["rounds_run"] == "18/18"
    assert data["most_sensitive"] is not None
    run_id = data["run_id"]

    # 子回测落库并关联 run：1 基准 + 18 扰动 = 19 条
    db = Session()
    children = db.query(BacktestRecord).filter_by(param_perturb_run_id=run_id).all()
    assert len(children) == 19
    assert {ch.name for ch in children} >= {"pp_test#base"}
    child_ids = {ch.id for ch in children}

    # 扰动不回写 strategies 表：原参数保持种子值
    strat = db.get(Strategy, strat_id)
    assert strat.stop_loss_ratio == Decimal("0.05")
    assert strat.max_positions == 5
    assert strat.capital_ratio == Decimal("0.6")
    db.close()

    # 列表接口默认过滤子记录
    recs = c.get("/api/backtest/records").json()["data"]
    assert all(r["id"] not in child_ids for r in recs)

    # run 详情：summary 含基准/轮次/参数聚合
    run = c.get(f"/api/backtest/param-perturb/{run_id}").json()["data"]
    assert run["status"] == "completed"
    assert run["perturb_pct"] == pytest.approx(0.10)
    summary = run["summary"]
    assert summary["baseline"]["record_id"] in child_ids
    assert summary["baseline"]["status"] == "completed"
    assert len(summary["rounds"]) == 18
    r0 = summary["rounds"][0]
    assert r0["param"] and r0["direction"] in ("low", "high")
    assert r0["record_id"] in child_ids
    params = {p["param"]: p for p in summary["params"]}
    assert "trailing_stop_ratio" not in params  # 基准 0 不可扰动
    assert "stop_loss_ratio" in params
    assert params["stop_loss_ratio"]["delta_cagr"] is not None
    assert summary["most_sensitive"] in params

    # 列表端点含该 run
    runs = c.get("/api/backtest/param-perturb").json()["data"]
    assert any(r["id"] == run_id for r in runs)


def test_param_perturb_validation_errors(client):
    """ps 不存在 404；扰动幅度越界 400；run 不存在 404。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    assert c.post("/api/backtest/param-perturb",
                  json=_pp_payload(99999)).json()["code"] == 404
    assert c.post("/api/backtest/param-perturb",
                  json=_pp_payload(ps_id, perturb_pct=0)).json()["code"] == 400
    assert c.post("/api/backtest/param-perturb",
                  json=_pp_payload(ps_id, perturb_pct=0.6)).json()["code"] == 400
    assert c.get("/api/backtest/param-perturb/99999").json()["code"] == 404

