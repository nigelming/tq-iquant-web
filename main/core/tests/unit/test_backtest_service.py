"""backtest_service 独立单测（P1 #9 阶段 4）。

直接 `from core.services.backtest_service import ...`（不经 core.api.backtest re-export），
验证 service 层脱离 HTTP 入口仍可独立工作。覆盖：

- run_backtest 主链路：record 状态流转 running→completed，结果持久化，返回计数
- run_backtest 空行情保护：标 failed（非静默 completed），返回零计数，不抛
- run_backtest 异常路径：标 failed + re-raise
- get_record_detail：不存在返回 None
- delete_record：不存在返回 False；存在返回 True 且子表清空

monkeypatch 目标统一是 backtest_service 模块（与阶段 3 后集成测试一致），
不依赖 HTTP TestClient，不依赖真实 TQ/TDX。
"""

import json
from datetime import datetime, date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import polars as pl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.models import (
    Base, StockPool, StockPoolStock, Formula, FormulaSignal,
    PortfolioStrategy, Strategy, BacktestRecord,
    BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
    BacktestDecisionEvent,
    BacktestCrisisAlpha,
)
from core.services import backtest_service as svc


@pytest.fixture
def db_session():
    """内存 SQLite + StaticPool（单连接共享，保证同一内存库跨 session 可见）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()


def _seed(db) -> tuple:
    """建最小依赖链：StockPool(+成分) → Formula → FormulaSignal →
    PortfolioStrategy → Strategy。与集成测试 _seed 同构。"""
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
    return ps, strat.id


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


class _Req:
    """轻量请求替身——service.run_backtest 只读 .name/.start_date/.end_date。"""

    def __init__(self, name="svc_test", start=date(2026, 7, 29), end=date(2026, 7, 31)):
        self.name = name
        self.start_date = start
        self.end_date = end


def _req(**kw):
    """_Req 工厂（调用处 _req(name=...) 更轻量）。"""
    return _Req(**kw)


# ---------------------------------------------------------------------------
# run_backtest 主链路
# ---------------------------------------------------------------------------
def test_run_backtest_happy_path_marks_completed_and_persists(db_session, monkeypatch):
    """主链路：record running→completed；trades/snapshots/evaluations 落库；
    返回 {record_id, trades_count, snapshots_count, evaluations}。"""
    ps, strat_id = _seed(db_session)

    # patch 数据获取层（避开真实 TQ/TDX）
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)

    # patch 引擎：返回受控 result（避免构造真实 Trade 对象）
    trade = SimpleNamespace(
        strategy_id=strat_id, signal_name="open_sig",
        signal_type=SimpleNamespace(value="OPEN"), stock_code="000001.SZ",
        trade_type=SimpleNamespace(value="buy"), price=Decimal("10"),
        quantity=100, amount=Decimal("1000"), commission=Decimal("5"),
        stamp_duty=Decimal("0"), trade_time=datetime(2026, 7, 30),
    )
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0")},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100995"),
         "cash": Decimal("99995"), "market_value": Decimal("1000")},
    ]
    fake_result = {
        "trades": [trade],
        "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.00995")},
        "strategy_snapshots": {},
        "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)

    result = svc.run_backtest(db_session, ps, _req())

    # 返回契约
    assert result["record_id"] > 0
    assert result["trades_count"] == 1
    assert result["snapshots_count"] == 2
    assert result["evaluations"]["total_return"] == Decimal("0.00995")

    # record 状态
    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "completed"
    assert rec.progress == 100
    assert rec.completed_at is not None

    # 持久化：1 trade + 2 snapshot(portfolio) + 1 evaluation(portfolio)
    assert db_session.query(BacktestTrade).filter_by(
        backtest_record_id=rec.id).count() == 1
    assert db_session.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").count() == 2
    assert db_session.query(BacktestEvaluation).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").count() == 1


def test_run_backtest_empty_klines_marks_failed_not_completed(db_session, monkeypatch):
    """空行情保护：TQ 拉不到 K 线 → 标 failed 并写明原因，返回零计数，不抛异常。"""
    ps, _ = _seed(db_session)

    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)

    result = svc.run_backtest(db_session, ps, _req(name="empty"))

    assert result["trades_count"] == 0
    assert result["snapshots_count"] == 0
    assert result["evaluations"] == {}

    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "failed"
    assert rec.error_message is not None and rec.error_message != ""
    assert "未取到任何行情数据" in rec.error_message


def test_run_backtest_exception_marks_failed_and_reraises(db_session, monkeypatch):
    """异常路径：build_klines 抛错 → record 标 failed 落库 + 异常 re-raise
    （路由层全局处理器兜底，与原 _run_backtest_locked 行为一致）。"""
    ps, _ = _seed(db_session)

    def _boom(ps, start, end, db=None):
        raise RuntimeError("polars panic: str cannot be int")

    monkeypatch.setattr(svc, "build_klines", _boom)
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)

    with pytest.raises(RuntimeError, match="polars panic"):
        svc.run_backtest(db_session, ps, _req(name="boom"))

    # record 已建（running 入库后抛错），异常块标 failed 并落库
    rec = db_session.query(BacktestRecord).order_by(
        BacktestRecord.id.desc()).first()
    assert rec is not None
    assert rec.status == "failed"
    assert "polars panic" in (rec.error_message or "")


# ---------------------------------------------------------------------------
# 查询：get_record_detail
# ---------------------------------------------------------------------------
def test_get_record_detail_returns_none_when_missing(db_session):
    assert svc.get_record_detail(db_session, 99999) is None


def test_get_record_detail_returns_full_payload(db_session, monkeypatch):
    """完整详情：record + snapshots + trades + evaluations + 策略层四件套齐全。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    trade = SimpleNamespace(
        strategy_id=strat_id, signal_name="open_sig",
        signal_type=SimpleNamespace(value="OPEN"), stock_code="000001.SZ",
        trade_type=SimpleNamespace(value="buy"), price=Decimal("10"),
        quantity=100, amount=Decimal("1000"), commission=Decimal("5"),
        stamp_duty=Decimal("0"), trade_time=datetime(2026, 7, 30),
    )
    fake_result = {
        "trades": [trade],
        "snapshots": [{"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
                       "cash": Decimal("100000"), "market_value": Decimal("0")}],
        "evaluations": {"total_return": Decimal("0.01")},
        "strategy_snapshots": {},
        "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    result = svc.run_backtest(db_session, ps, _req())

    detail = svc.get_record_detail(db_session, result["record_id"])
    assert detail is not None
    assert detail["record"]["id"] == result["record_id"]
    assert detail["record"]["status"] == "completed"
    assert len(detail["snapshots"]) == 1
    assert len(detail["trades"]) == 1
    # 带策略名（trades 经 _serialize_trade_with_name）
    assert detail["trades"][0]["strategy_name"] == "s1"
    assert detail["evaluations"]["total_return"] == 0.01  # _f 转 float
    assert detail["strategy_evaluations"] == []
    assert detail["strategy_snapshots"] == []


# ---------------------------------------------------------------------------
# 查询：delete_record
# ---------------------------------------------------------------------------
def test_delete_record_returns_false_when_missing(db_session):
    assert svc.delete_record(db_session, 99999) is False


def test_delete_record_removes_record_and_children(db_session, monkeypatch):
    """存在返回 True；record + trades/snapshots/evaluations 全清。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    trade = SimpleNamespace(
        strategy_id=strat_id, signal_name="open_sig",
        signal_type=SimpleNamespace(value="OPEN"), stock_code="000001.SZ",
        trade_type=SimpleNamespace(value="buy"), price=Decimal("10"),
        quantity=100, amount=Decimal("1000"), commission=Decimal("5"),
        stamp_duty=Decimal("0"), trade_time=datetime(2026, 7, 30),
    )
    fake_result = {
        "trades": [trade],
        "snapshots": [{"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
                       "cash": Decimal("100000"), "market_value": Decimal("0")}],
        "evaluations": {"total_return": Decimal("0.01")},
        "strategy_snapshots": {},
        "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    result = svc.run_backtest(db_session, ps, _req())
    rid = result["record_id"]

    # 删除前子表有数据
    assert db_session.query(BacktestTrade).filter_by(backtest_record_id=rid).count() == 1
    assert svc.delete_record(db_session, rid) is True

    # 删除后 record + 子表全空
    assert db_session.get(BacktestRecord, rid) is None
    assert db_session.query(BacktestTrade).filter_by(backtest_record_id=rid).count() == 0
    assert db_session.query(BacktestDailySnapshot).filter_by(backtest_record_id=rid).count() == 0
    assert db_session.query(BacktestEvaluation).filter_by(backtest_record_id=rid).count() == 0


# ---------------------------------------------------------------------------
# 决策闸门事件：持久化 + 详情聚合 + 级联删除
# ---------------------------------------------------------------------------
def _fake_decisions(strat_id):
    """result["decisions"] 形态：engine.run 产出的 to_dict() 列表。"""
    base = {"portfolio_id": 1, "strategy_id": strat_id}
    return [
        {**base, "gate": "stop_loss", "layer": "strategy_risk", "action": "trigger",
         "stock_code": "000001.SZ", "bar_time": datetime(2026, 7, 30),
         "param_name": "stop_loss_ratio", "param_value": 0.05, "actual_value": 0.06,
         "requested_qty": None, "final_qty": None, "message": "止损触发"},
        {**base, "gate": "stop_loss", "layer": "strategy_risk", "action": "trigger",
         "stock_code": "000002.SZ", "bar_time": datetime(2026, 7, 31),
         "param_name": "stop_loss_ratio", "param_value": 0.05, "actual_value": 0.07,
         "requested_qty": None, "final_qty": None, "message": "止损触发"},
        {**base, "gate": "insufficient_funds", "layer": "capital_gate", "action": "reject",
         "stock_code": "000003.SZ", "bar_time": datetime(2026, 7, 30),
         "param_name": "cash", "param_value": None, "actual_value": 500.0,
         "requested_qty": 1000, "final_qty": 0, "message": "资金不足拒单"},
    ]


def test_decisions_persisted_and_detail_returns_summary(db_session, monkeypatch):
    """result["decisions"] 逐行落 backtest_decision_events；详情带聚合统计 + 明细。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    fake_result = {
        "trades": [], "snapshots": [], "evaluations": {},
        "strategy_snapshots": {}, "strategy_evaluations": {},
        "decisions": _fake_decisions(strat_id),
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    rid = svc.run_backtest(db_session, ps, _req())["record_id"]

    # 落库 3 行
    assert db_session.query(BacktestDecisionEvent).filter_by(
        backtest_record_id=rid).count() == 3

    detail = svc.get_record_detail(db_session, rid)
    # 聚合：stop_loss×2 + insufficient_funds×1，按 count 降序
    summary = detail["decision_summary"]
    assert len(summary) == 2
    assert summary[0]["gate"] == "stop_loss" and summary[0]["count"] == 2
    assert summary[0]["stock_count"] == 2  # 000001/000002 两只
    assert summary[0]["param_name"] == "stop_loss_ratio"
    assert summary[0]["param_value"] == 0.05
    assert summary[1]["gate"] == "insufficient_funds" and summary[1]["count"] == 1
    assert summary[1]["requested_qty_sum"] == 1000
    assert summary[1]["final_qty_sum"] == 0
    # 时间范围（ISO 字符串）
    assert summary[0]["first_bar_time"].startswith("2026-07-30")
    assert summary[0]["last_bar_time"].startswith("2026-07-31")
    # 原始明细：按 bar_time 排序，字段齐全
    decisions = detail["decisions"]
    assert len(decisions) == 3
    assert decisions[0]["bar_time"].startswith("2026-07-30")
    assert {d["gate"] for d in decisions} == {"stop_loss", "insufficient_funds"}
    assert decisions[0]["message"]  # 人读原因透传


def test_delete_record_cascades_decisions(db_session, monkeypatch):
    """删除回测记录时 decision 事件一并清空。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    fake_result = {
        "trades": [], "snapshots": [], "evaluations": {},
        "strategy_snapshots": {}, "strategy_evaluations": {},
        "decisions": _fake_decisions(strat_id),
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    rid = svc.run_backtest(db_session, ps, _req())["record_id"]
    assert db_session.query(BacktestDecisionEvent).filter_by(
        backtest_record_id=rid).count() == 3

    assert svc.delete_record(db_session, rid) is True
    assert db_session.query(BacktestDecisionEvent).filter_by(
        backtest_record_id=rid).count() == 0


def test_list_records_returns_newest_first(db_session, monkeypatch):
    """list_records：序列化字段齐全；按 created_at 倒序（同秒插入时
    created_at 相等，故断言非递增而非严格递减——避免对亚秒分辨率过约束）。"""
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    svc.run_backtest(db_session, ps, _req(name="r1"))
    svc.run_backtest(db_session, ps, _req(name="r2"))

    recs = svc.list_records(db_session)
    assert len(recs) == 2
    # 序列化字段齐全
    for r in recs:
        assert {"id", "portfolio_strategy_id", "name", "status",
                "progress", "start_date", "end_date", "created_at"} <= set(r.keys())
    # 按 created_at 倒序（非递增）；两条 name 都在
    assert recs[0]["created_at"] >= recs[1]["created_at"]
    names = {r["name"] for r in recs}
    assert names == {"r1", "r2"}


class TestReturnBucketsInDetail:
    """get_record_detail 返回 returns_buckets 字段。"""

    def test_detail_includes_returns_buckets(self, db_session):
        """构造最小快照记录，验证 detail 响应含 returns_buckets。"""
        from datetime import date as D

        # _seed 返回 (ps, strat.id)，需显式创建 BacktestRecord
        ps, strat_id = _seed(db_session)
        rec = BacktestRecord(
            portfolio_strategy_id=ps.id, name="rb_test",
            start_date=D(2024, 1, 15), end_date=D(2024, 1, 16),
            status="completed", progress=100,
        )
        db_session.add(rec)
        db_session.flush()
        rec_id = rec.id

        # 插入 2 天组合快照（target_id=组合 id，与持久化口径一致）
        db_session.add_all([
            BacktestDailySnapshot(
                backtest_record_id=rec_id, target_type="portfolio",
                target_id=ps.id,
                snap_date=D(2024, 1, 15), total_value="100.00",
                cash="100.00", market_value="0.00",
            ),
            BacktestDailySnapshot(
                backtest_record_id=rec_id, target_type="portfolio",
                target_id=ps.id,
                snap_date=D(2024, 1, 16), total_value="101.00",
                cash="101.00", market_value="0.00",
            ),
        ])
        db_session.commit()

        result = svc.get_record_detail(db_session, rec_id)
        assert "returns_buckets" in result
        rb = result["returns_buckets"]
        assert "monthly_matrix" in rb
        assert "yearly" in rb
        assert len(rb["yearly"]) >= 1
        yr = rb["yearly"][0]
        assert yr["year"] == 2024
        assert len(yr["daily_returns"]) >= 1


# ---------------------------------------------------------------------------
# build_benchmark_data：自定义基准代码
# ---------------------------------------------------------------------------
def test_build_benchmark_data_uses_custom_benchmark_index(monkeypatch):
    """ps.benchmark_index 为自定义代码时，用该代码（而非默认 000300.SH）拉指数日线，
    返回 {日期: 收盘价 Decimal}。防止回退为硬编码默认基准。"""
    import pandas as pd

    captured = {}

    class _FakeTQ:
        def get_history_raw(self, stocks, periods, start="", end="",
                            dividend_type="front", count=100):
            captured["stocks"] = stocks
            captured["periods"] = periods
            captured["start"] = start
            captured["end"] = end
            close = pd.DataFrame(
                {"000905.SH": [6000.0, 6060.0, 6030.0]},
                index=pd.to_datetime(["2026-07-29", "2026-07-30", "2026-07-31"]),
            )
            return {"1d": {"Close": close}}

    monkeypatch.setattr(svc, "TQData", lambda: _FakeTQ())
    ps = SimpleNamespace(benchmark_index="000905.SH")

    result = svc.build_benchmark_data(ps, date(2026, 7, 29), date(2026, 7, 31))

    assert captured["stocks"] == ["000905.SH"]
    assert captured["periods"] == ["1d"]
    assert captured["start"] == "20260729"
    assert captured["end"] == "20260731"
    assert result == {
        date(2026, 7, 29): Decimal("6000.0"),
        date(2026, 7, 30): Decimal("6060.0"),
        date(2026, 7, 31): Decimal("6030.0"),
    }


_CA_WEEKS_B = [-0.10, -0.10, -0.10, -0.10, 0.05, 0.05, 0.05, 0.05, -0.04, 0.04, -0.08, 0.02]
_CA_WEEKS_S = [0.02, -0.02, -0.06, -0.12, 0.03, 0.06, -0.01, 0.04, -0.03, 0.02, -0.09, 0.01]


def _ca_daily_points(weekly_returns, start="2024-01-01"):
    dates = pd.bdate_range(start=start, periods=len(weekly_returns) * 5)
    points = {}
    idx = 0
    level = 100.0
    for r in weekly_returns:
        base = level
        for _ in range(5):
            level = base * (1 + r * (_ + 1) / 5)
            points[dates[idx].date()] = level
            idx += 1
    return points


def _seed_record(db):
    ps, _ = _seed(db)
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id, name="ca_test",
        start_date=date(2024, 1, 1), end_date=date(2024, 3, 31),
        status="completed", progress=100,
    )
    db.add(rec)
    db.commit()
    return rec, ps


class _FakeIndexTQ:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def get_history_raw(self, stocks, periods, start="", end="",
                        dividend_type="front", count=100):
        self.calls.append({
            "stocks": list(stocks), "periods": list(periods), "start": start,
            "end": end, "dividend_type": dividend_type, "count": count,
        })
        rows = {}
        for code in stocks:
            series = self.frames.get(code)
            if not series:
                continue
            for d, v in series.items():
                rows.setdefault(d, {})[code] = v
        keys = sorted(rows)
        frame = pd.DataFrame([rows[d] for d in keys], index=keys)
        return {"1d": {"Close": frame}}


def _ca_snapshots(points):
    return [{"snap_date": d, "total_value": v} for d, v in points.items()]


def _ca_decimal_frames(points):
    return {d: Decimal(str(v)) for d, v in points.items()}


def test_build_index_close_series_batches_codes_and_skips_missing(monkeypatch):
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    fake = _FakeIndexTQ({
        "000300.SH": {days[0]: 3000.0, days[1]: 3010.0},
        "000905.SH": {days[0]: 5000.0, days[1]: 4990.0},
    })
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.build_index_close_series(
        ["000300.SH", "000905.SH", "000985.CSI"], date(2026, 1, 1), date(2026, 1, 31)
    )
    assert set(out) == {"000300.SH", "000905.SH"}
    assert float(out["000300.SH"][days[0]]) == pytest.approx(3000.0)
    assert float(out["000905.SH"][days[1]]) == pytest.approx(4990.0)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["stocks"] == ["000300.SH", "000905.SH", "000985.CSI"]
    assert call["periods"] == ["1d"]
    assert call["start"] == "20260101"
    assert call["end"] == "20260131"
    assert call["dividend_type"] == "front"
    assert call["count"] == -1


def test_build_index_close_series_swallows_tq_error(monkeypatch):
    class _Boom:
        def get_history_raw(self, **kw):
            raise RuntimeError("tdx down")

    monkeypatch.setattr(svc, "TQData", lambda: _Boom())
    assert svc.build_index_close_series(
        ["000300.SH"], date(2026, 1, 1), date(2026, 1, 31)
    ) == {}


def test_build_index_close_series_empty_codes_skips_tq(monkeypatch):
    def _boom(**kw):
        raise AssertionError("不应调用 TQ")

    monkeypatch.setattr(svc, "TQData", lambda: SimpleNamespace(get_history_raw=_boom))
    assert svc.build_index_close_series([], date(2026, 1, 1), date(2026, 1, 31)) == {}
    assert svc.build_index_close_series([""], date(2026, 1, 1), date(2026, 1, 31)) == {}


def test_build_benchmark_data_returns_empty_on_tq_error(monkeypatch):
    class _Boom:
        def get_history_raw(self, **kw):
            raise RuntimeError("tdx down")

    monkeypatch.setattr(svc, "TQData", lambda: _Boom())
    ps = SimpleNamespace(benchmark_index="000300.SH")
    assert svc.build_benchmark_data(ps, date(2026, 1, 1), date(2026, 1, 31)) == {}


def test_build_benchmark_data_uses_default_code_when_unset(monkeypatch):
    fake = _FakeIndexTQ({"000300.SH": {date(2026, 1, 5): 3000.0}})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.build_benchmark_data(
        SimpleNamespace(benchmark_index=None), date(2026, 1, 1), date(2026, 1, 31)
    )
    assert fake.calls[0]["stocks"] == ["000300.SH"]
    assert list(out) == [date(2026, 1, 5)]


def test_compute_and_store_crisis_alpha_completed(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(bm_pts),
        "000905.SH": _ca_decimal_frames(bm_pts),
    })
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert row.status == "completed"
    assert row.benchmark_code == "000300.SH"
    assert row.benchmark_name == "沪深300"
    assert row.error is None
    data = json.loads(row.payload_json)
    assert data["n_weeks"] == 12
    assert data["alpha_rating"] == "较弱"
    assert data["strategy_name"] == ps.name
    assert [r["name"] for r in data["multi_bench"]] == ["沪深300", "中证500"]
    assert [r["is_main"] for r in data["multi_bench"]] == [True, False]
    assert len(data["nav"]["dates"]) == 65
    assert len(data["weekly_detail"]) == 12
    skipped = json.loads(row.skipped_benchmarks_json)
    assert "中证500" not in skipped
    assert set(skipped) == {"中证全指", "中证1000", "创业板指", "科创50"}
    assert row.computed_at is not None


def test_compute_and_store_crisis_alpha_insufficient_when_common_days_short(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    pts = _ca_daily_points([0.0, 0.01, 0.01])
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(pts),
        date(2024, 1, 1), date(2024, 1, 31),
    )
    assert row.status == "insufficient"
    assert "共同交易日不足" in row.error
    assert row.payload_json is None


def test_compute_and_store_crisis_alpha_failed_when_main_benchmark_missing(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000905.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert row.status == "failed"
    assert "主基准" in row.error
    assert "沪深300" in json.loads(row.skipped_benchmarks_json)
    assert row.payload_json is None


def test_compute_and_store_crisis_alpha_upserts_single_row(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    args = (
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    svc.compute_and_store_crisis_alpha(*args)
    svc.compute_and_store_crisis_alpha(*args)
    rows = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).all()
    assert len(rows) == 1


def test_safe_compute_crisis_alpha_swallows_and_marks_failed(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "compute_and_store_crisis_alpha", _boom)
    row = svc.safe_compute_crisis_alpha(
        db_session, rec.id, ps, [], date(2024, 1, 1), date(2024, 3, 31)
    )
    assert row is not None
    assert row.status == "failed"
    assert "boom" in row.error


def test_run_backtest_still_completed_when_crisis_alpha_fails(db_session, monkeypatch):
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0")},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100995"),
         "cash": Decimal("99995"), "market_value": Decimal("1000")},
    ]
    fake_result = {
        "trades": [], "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.00995")},
        "strategy_snapshots": {}, "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)

    result = svc.run_backtest(db_session, ps, _req())
    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "completed"
    assert rec.progress == 100
    assert db_session.query(BacktestCrisisAlpha).count() == 0


def test_run_backtest_calls_crisis_alpha_after_main_commit(db_session, monkeypatch):
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0")},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100995"),
         "cash": Decimal("99995"), "market_value": Decimal("1000")},
    ]
    fake_result = {
        "trades": [], "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.00995")},
        "strategy_snapshots": {}, "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    seen = {}

    def _capture(db, record_id, ps_arg, snaps, start, end):
        seen["record_id"] = record_id
        seen["status_at_call"] = db.get(BacktestRecord, record_id).status
        seen["snapshot_count"] = len(snaps)
        seen["start"] = start
        seen["end"] = end

    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", _capture)
    result = svc.run_backtest(db_session, ps, _req())
    assert seen["record_id"] == result["record_id"]
    assert seen["status_at_call"] == "completed"
    assert seen["snapshot_count"] == 2
    assert seen["start"] == _req().start_date
    assert seen["end"] == _req().end_date


def test_recompute_crisis_alpha_rebuilds_from_db_snapshots(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    for d, v in nav_pts.items():
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal("0"),
        ))
    db_session.commit()
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "completed"
    assert out["error"] is None
    assert out["computed_at"] is not None
    row = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).one()
    data = json.loads(row.payload_json)
    assert data["n_weeks"] == 12
    assert len(data["nav"]["dates"]) == 65


def test_recompute_crisis_alpha_returns_none_for_missing_record(db_session):
    assert svc.recompute_crisis_alpha(db_session, 9999) is None


def test_recompute_crisis_alpha_without_snapshots_returns_failed(db_session):
    rec, _ = _seed_record(db_session)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "failed"
    assert "无组合日快照" in out["error"]


def test_get_record_detail_includes_crisis_alpha(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    detail = svc.get_record_detail(db_session, rec.id)
    ca = detail["crisis_alpha"]
    assert ca["status"] == "completed"
    assert ca["benchmark_code"] == "000300.SH"
    assert ca["data"]["n_weeks"] == 12
    assert "中证全指" in ca["skipped_benchmarks"]
    assert ca["computed_at"] is not None


def test_get_record_detail_crisis_alpha_is_null_without_row(db_session):
    rec, _ = _seed_record(db_session)
    detail = svc.get_record_detail(db_session, rec.id)
    assert detail["crisis_alpha"] is None
