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
from unittest.mock import MagicMock

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
    BacktestMarketRegimeAnalysis,
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
    """轻量请求替身——service.run_backtest 只读 .name/.start_date/.end_date/.dividend_mode。"""

    def __init__(self, name="svc_test", start=date(2026, 7, 29), end=date(2026, 7, 31),
                 dividend_mode="front"):
        self.name = name
        self.start_date = start
        self.end_date = end
        self.dividend_mode = dividend_mode


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
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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
    assert rec.completed_at is not None

    # 持久化：1 trade + 2 snapshot(portfolio) + 1 evaluation(portfolio)
    assert db_session.query(BacktestTrade).filter_by(
        backtest_record_id=rec.id).count() == 1
    assert db_session.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").count() == 2
    assert db_session.query(BacktestEvaluation).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").count() == 1


def test_run_backtest_persists_positions_json(db_session, monkeypatch):
    """新回测：快照 positions 明细落 positions_json；空仓日落 "[]" 而非 NULL。"""
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, s, e, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, k, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, s, e, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)

    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0"),
         "positions": []},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100000"),
         "cash": Decimal("90000"), "market_value": Decimal("10000"),
         "positions": [{
             "stock_code": "000001.SZ", "quantity": 1000, "sellable_quantity": 1000,
             "market_value": "10000.0", "cost_value": "10000.0",
         }]},
    ]
    fake_result = {
        "trades": [], "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0")},
        "strategy_snapshots": {}, "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    result = svc.run_backtest(db_session, ps, _req())

    rows = db_session.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=result["record_id"], target_type="portfolio",
    ).order_by(BacktestDailySnapshot.snap_date).all()
    assert rows[0].positions_json == "[]"  # 空仓日是有效负载
    payload = json.loads(rows[1].positions_json)
    assert payload[0]["stock_code"] == "000001.SZ"
    assert payload[0]["quantity"] == 1000
    assert payload[0]["sellable_quantity"] == 1000


def test_run_backtest_persists_daily_dedup_last_bar(db_session, monkeypatch):
    """存储口径：引擎快照为 bar 级（30m 每天多行），落库每日只存最后一根 bar（日收盘），
    与 Evaluator._daily_rows / build_return_buckets 口径一致；snapshots_count 返回日级行数。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, s, e, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, k, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, s, e, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)

    # 组合层：7-29 两根 bar、7-30 一根；策略层同理
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0"), "positions": []},
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100500"),
         "cash": Decimal("99500"), "market_value": Decimal("1000"), "positions": []},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("101000"),
         "cash": Decimal("100000"), "market_value": Decimal("1000"), "positions": []},
    ]
    strategy_snapshots = {
        strat_id: [
            {"snap_date": date(2026, 7, 29), "total_value": Decimal("50000"),
             "cash": Decimal("50000"), "market_value": Decimal("0"), "positions": []},
            {"snap_date": date(2026, 7, 29), "total_value": Decimal("50100"),
             "cash": Decimal("49800"), "market_value": Decimal("300"), "positions": []},
            {"snap_date": date(2026, 7, 30), "total_value": Decimal("50200"),
             "cash": Decimal("49900"), "market_value": Decimal("300"), "positions": []},
        ]
    }
    fake_result = {
        "trades": [],
        "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.01")},
        "strategy_snapshots": strategy_snapshots,
        "strategy_evaluations": {strat_id: {"total_return": Decimal("0.004")}},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    result = svc.run_backtest(db_session, ps, _req())

    # 组合层落库按日去重：2 行，同日取最后一根 bar（日收盘）
    p_rows = db_session.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=result["record_id"], target_type="portfolio",
    ).order_by(BacktestDailySnapshot.snap_date).all()
    assert len(p_rows) == 2
    assert p_rows[0].snap_date == date(2026, 7, 29)
    assert p_rows[0].total_value == Decimal("100500")
    assert p_rows[1].total_value == Decimal("101000")

    # 策略层同样按日去重
    s_rows = db_session.query(BacktestDailySnapshot).filter_by(
        backtest_record_id=result["record_id"], target_type="strategy",
    ).order_by(BacktestDailySnapshot.snap_date).all()
    assert len(s_rows) == 2
    assert s_rows[0].total_value == Decimal("50100")
    assert s_rows[1].total_value == Decimal("50200")

    # snapshots_count 返回日级行数（组合层）
    assert result["snapshots_count"] == 2


def test_run_backtest_empty_klines_marks_failed_not_completed(db_session, monkeypatch):
    """空行情保护：TQ 拉不到 K 线 → 标 failed 并写明原因，返回零计数，不抛异常。"""
    ps, _ = _seed(db_session)

    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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

    def _boom(ps, start, end, db=None, dividend_mode="front"):
        raise RuntimeError("polars panic: str cannot be int")

    monkeypatch.setattr(svc, "build_klines", _boom)
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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
    ok_flag, msg = svc.delete_record(db_session, 99999)
    assert ok_flag is False
    assert msg is not None and "不存在" in msg


def test_delete_record_removes_record_and_children(db_session, monkeypatch):
    """存在返回 True；record + trades/snapshots/evaluations 全清。"""
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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
    ok_flag, msg = svc.delete_record(db_session, rid)
    assert ok_flag is True
    assert msg is None

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
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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

    ok_flag, msg = svc.delete_record(db_session, rid)
    assert ok_flag is True
    assert msg is None
    assert db_session.query(BacktestDecisionEvent).filter_by(
        backtest_record_id=rid).count() == 0


# ---------------------------------------------------------------------------
# #4: delete_record 运行中守卫 — 防止并发删除运行中的回测引发 FK 500 / 数据竞争。
# 对照 live_service.delete_session 返回 (ok, msg) 风格，运行中拒绝并返回说明。
# ---------------------------------------------------------------------------
def test_delete_record_rejects_running(db_session):
    """status=running 的回测记录拒绝删除，返回 (False, '...运行中...')；
    记录与子表保持不变。回测同步内联执行，进程内运行中即代表并发删除。"""
    ps, _ = _seed(db_session)
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id, name="running_one",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="running",
    )
    db_session.add(rec)
    db_session.commit()
    rid = rec.id

    ok_flag, msg = svc.delete_record(db_session, rid)
    assert ok_flag is False
    assert msg is not None and "运行中" in msg

    # 记录仍在，状态不变
    still = db_session.get(BacktestRecord, rid)
    assert still is not None
    assert still.status == "running"


# ---------------------------------------------------------------------------
# #5: recover_stale_running_backtests — 应用启动时清理僵尸 running 记录。
# 进程崩溃/重启后 DB 残留 status=running 的记录，前端会永久显示"运行中"。
# 启动时统一标记 failed 并附说明，让用户能识别并重新发起。
# ---------------------------------------------------------------------------
def test_recover_stale_running_backtests_marks_failed(db_session):
    """残留 running 全部标记 failed + error_message 附说明；已完成的记录不动；
    返回受影响记录数。"""
    ps, _ = _seed(db_session)
    r1 = BacktestRecord(
        portfolio_strategy_id=ps.id, name="crashed1",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="running",
    )
    r2 = BacktestRecord(
        portfolio_strategy_id=ps.id, name="completed_ok",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="completed",
    )
    r3 = BacktestRecord(
        portfolio_strategy_id=ps.id, name="crashed2",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="running",
    )
    db_session.add_all([r1, r2, r3])
    db_session.commit()

    count = svc.recover_stale_running_backtests(db_session)
    assert count == 2

    assert db_session.get(BacktestRecord, r1.id).status == "failed"
    assert db_session.get(BacktestRecord, r1.id).error_message  # 含说明
    assert db_session.get(BacktestRecord, r3.id).status == "failed"
    # 已完成的不动
    assert db_session.get(BacktestRecord, r2.id).status == "completed"


def test_recover_stale_running_backtests_noop_when_clean(db_session):
    """无残留 running 时返回 0，不写库。"""
    ps, _ = _seed(db_session)
    db_session.add(BacktestRecord(
        portfolio_strategy_id=ps.id, name="ok",
        start_date=date(2026, 7, 29), end_date=date(2026, 7, 31),
        status="completed",
    ))
    db_session.commit()
    assert svc.recover_stale_running_backtests(db_session) == 0


def test_list_records_returns_newest_first(db_session, monkeypatch):
    """list_records：序列化字段齐全；按 created_at 倒序（同秒插入时
    created_at 相等，故断言非递增而非严格递减——避免对亚秒分辨率过约束）。"""
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    svc.run_backtest(db_session, ps, _req(name="r1"))
    svc.run_backtest(db_session, ps, _req(name="r2"))

    recs = svc.list_records(db_session)
    assert len(recs) == 2
    # 序列化字段齐全
    for r in recs:
        assert {"id", "portfolio_strategy_id", "name", "status",
                "start_date", "end_date", "created_at"} <= set(r.keys())
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
            status="completed",
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
        status="completed",
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


def _seed_robustness_record(db):
    rec, ps = _seed_record(db)
    db.add(BacktestDailySnapshot(
        backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
        snap_date=date(2024, 1, 2), total_value=Decimal("100000"),
        cash=Decimal("100000"), market_value=Decimal("0"), benchmark_value=Decimal("2000")))
    db.add(BacktestDailySnapshot(
        backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
        snap_date=date(2024, 12, 31), total_value=Decimal("120000"),
        cash=Decimal("0"), market_value=Decimal("120000"), benchmark_value=Decimal("2200")))
    db.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=1, signal_name="open_sig", signal_type="OPEN",
        stock_code="000001.SZ", trade_type="BUY", price=Decimal("10"),
        quantity=1000, amount=Decimal("10000"), commission=Decimal("5"), stamp_duty=Decimal("0"),
        bar_time=datetime(2024, 1, 3)))
    db.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=1, signal_name="open_sig", signal_type="OPEN",
        stock_code="000001.SZ", trade_type="SELL", price=Decimal("12"),
        quantity=1000, amount=Decimal("12000"), commission=Decimal("5"), stamp_duty=Decimal("6"),
        bar_time=datetime(2024, 12, 30)))
    db.commit()
    return rec, ps


def test_compute_and_store_robustness(db_session):
    rec, _ = _seed_robustness_record(db_session)
    row = svc.compute_and_store_robustness(db_session, rec.id)
    assert row.status == "completed"
    payload = json.loads(row.payload_json)
    assert round(payload["total_return"], 4) == 0.2
    assert payload["years_positive"] == "1/1"
    assert payload["annual"][0]["year"] == "2024"
    assert round(payload["benchmark_return"], 4) == 0.1
    assert payload["concentration"]["total_stocks"] == 1
    # 单笔买卖：净利 = 12000-5-6 - (10000+5) = 1984
    assert payload["concentration"]["total_realized_pnl"] == 1984.0


def test_recompute_robustness_missing_record_returns_none(db_session):
    assert svc.recompute_robustness(db_session, 99999) is None


def test_recompute_robustness_no_snapshots_returns_failed(db_session):
    rec, _ = _seed_record(db_session)
    r = svc.recompute_robustness(db_session, rec.id)
    assert r["status"] == "failed"
    assert "无组合日快照" in r["error"]


def test_run_backtest_auto_computes_robustness(db_session, monkeypatch):
    """回测完成后自动触发 safe_compute_robustness（记录 id 被调用）。"""
    ps, _ = _seed(db_session)
    _patch_data_layer(monkeypatch, _NO_TRADES_RESULT)
    called = []
    monkeypatch.setattr(svc, "safe_compute_robustness",
                        lambda db, record_id: called.append(record_id))
    rid = svc.run_backtest(db_session, ps, _req())["record_id"]
    assert called == [rid]


def test_ensure_pool_clone_copies_params_and_swaps_pool(db_session):
    ps, strat_id = _seed(db_session)
    db_session.add(StockPool(code="T2", name="池2"))
    db_session.flush()
    pool2 = db_session.query(StockPool).filter_by(code="T2").one()
    clone = svc.ensure_pool_clone(db_session, ps.id, pool2.id, "QZQ-池2")
    assert clone.id != ps.id
    assert clone.stock_pool_id == pool2.id
    assert clone.initial_capital == ps.initial_capital
    assert clone.max_drawdown == ps.max_drawdown
    strat = db_session.query(Strategy).filter_by(portfolio_id=clone.id).one()
    assert strat.stock_pool_id == pool2.id
    assert strat.formula_id is not None
    # 幂等：同名复用
    clone2 = svc.ensure_pool_clone(db_session, ps.id, pool2.id, "QZQ-池2")
    assert clone2.id == clone.id


def test_run_pool_comparison_runs_each_pool_and_returns_summary(db_session, monkeypatch):
    ps, strat_id = _seed(db_session)
    db_session.add(StockPool(code="T3", name="池3"))
    db_session.add(StockPool(code="T4", name="池4"))
    db_session.flush()
    pools = {r.code: r.id for r in db_session.query(StockPool).all()}

    _patch_data_layer(monkeypatch, _NO_TRADES_RESULT)
    results = svc.run_pool_comparison(
        db_session, ps.id, [pools["T3"], pools["T4"]],
        date(2026, 7, 29), date(2026, 7, 31), name_prefix="对比",
    )
    assert len(results) == 2
    for r in results:
        assert r["status"] == "completed"
        assert r["record_id"] is not None
        assert r["metrics"] is not None
        assert r["robustness"] is not None  # 每次回测自动算稳健性检验
    assert {r["pool_name"] for r in results} == {"池3", "池4"}
    assert results[0]["record_id"] != results[1]["record_id"]


def test_build_index_close_series_back_dividend_mode(monkeypatch):
    days = [date(2026, 1, 5)]
    fake = _FakeIndexTQ({"000300.SH": {days[0]: 3000.0}})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.build_index_close_series(
        ["000300.SH"], date(2026, 1, 1), date(2026, 1, 31), dividend_mode="back"
    )
    assert set(out) == {"000300.SH"}
    assert fake.calls[0]["dividend_type"] == "back"


def test_build_params_snapshot_includes_dividend_mode():
    ps = SimpleNamespace(
        benchmark_index="000300.SH", initial_capital=Decimal("1000000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
        max_holdings=10, min_commission=Decimal("5"),
        buy_commission_rate=Decimal("0.00025"), sell_commission_rate=Decimal("0.00025"),
        stamp_duty_rate=Decimal("0.0005"), slippage=Decimal("0"),
        trading_session="full", stock_pool_id=2,
    )
    snap = json.loads(svc.build_params_snapshot(ps, dividend_mode="back"))
    assert snap["dividend_mode"] == "back"
    assert snap["benchmark_index"] == "000300.SH"


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


def test_compute_and_store_crisis_alpha_reports_dropped_secondary_benchmark(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    short_pts = dict(list(bm_pts.items())[:10])
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(bm_pts),
        "000905.SH": _ca_decimal_frames(short_pts),
    })
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert row.status == "completed"
    data = json.loads(row.payload_json)
    assert [r["name"] for r in data["multi_bench"]] == ["沪深300"]
    skipped = json.loads(row.skipped_benchmarks_json)
    assert "中证500" in skipped
    assert set(skipped) == {"中证500", "中证全指", "中证1000", "创业板指", "科创50"}


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
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})

    def _boom(*a, **kw):
        raise RuntimeError("crisis_alpha_boom")

    monkeypatch.setattr(svc, "compute_and_store_crisis_alpha", _boom)
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
    rows = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failed"
    assert "crisis_alpha_boom" in row.error
    assert row.computed_at is not None


def test_run_backtest_calls_crisis_alpha_after_main_commit(db_session, monkeypatch):
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
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


# ---------------------------------------------------------------------------
# 市场状态×绩效分析（Task 8，TR-8.1 / TR-8.2）
# ---------------------------------------------------------------------------
def _mr_seed_snapshots(db, rec, ps, limit=None):
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    items = list(nav_pts.items())[:limit] if limit else nav_pts.items()
    for d, v in items:
        db.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal("0"),
        ))
    db.commit()


def test_recompute_market_regime_upserts_and_detail_embeds(db_session, monkeypatch):
    """TR-8.1: 重算→落库→详情返回；重复重算 upsert 不新增行。"""
    rec, ps = _seed_record(db_session)
    _mr_seed_snapshots(db_session, rec, ps)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))

    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "completed"
    assert out["error"] is None
    assert out["computed_at"] is not None
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    payload = json.loads(row.payload_json)
    assert set(payload) == {"meta", "regime_table", "opportunity",
                            "rebound", "neutral", "style", "dispersion",
                            "exposure", "quadrant_exposure",
                            "industry_attribution", "robustness"}
    # fake TQ 未提供四象限指数 → 全部 skipped，安全空结构
    assert payload["quadrant_exposure"]["available_codes"] == []
    assert len(payload["quadrant_exposure"]["skipped"]) == 6
    assert payload["regime_table"]["range"]["weeks"] >= 0
    # 老记录（无 positions_json）→ needs_rerun，不伪装结果
    assert payload["industry_attribution"]["status"] == "needs_rerun"

    # 重复重算：upsert 不新增行
    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    out2 = svc.recompute_market_regime(db_session, rec.id)
    assert out2["status"] == "completed"
    rows = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).all()
    assert len(rows) == 1

    # 详情接口内嵌 market_regime 摘要
    detail = svc.get_record_detail(db_session, rec.id)
    assert detail["market_regime"]["status"] == "completed"
    assert detail["market_regime"]["benchmark_code"] == "000300.SH"
    assert detail["market_regime"]["data"] == payload


def test_backfilled_positions_unblock_industry_attribution(db_session, monkeypatch):
    """回测 positions_json 后，recompute_market_regime 的行业归因不再 needs_rerun。

    这是回测功能的端到端验收：双门禁
    （backtest_service._build_industry_attribution 与
    industry_attribution.compute_industry_attribution 都以
    `any(positions is not None)` 为条件）。
    """
    from core.services.positions_backfill import backfill_record

    rec, ps = _seed_record(db_session)
    strat = db_session.query(Strategy).filter_by(portfolio_id=ps.id).one()
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    days = sorted(bm_pts)

    # 两腿持仓（前 5 日 + 第 6 日起），现金序列随之下降，使差分校验真正发挥作用
    for i, d in enumerate(days):
        held = i >= 5
        cash = Decimal("997900.00" if held else "999000.00")
        market_value = Decimal("2100.00" if held else "1000.00")
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=cash + market_value, cash=cash,
            market_value=market_value))
    for i, (price, amount) in enumerate(
            [("10.00", "1000.00"), ("11.00", "1100.00")]):
        d = days[0] if i == 0 else days[5]
        db_session.add(BacktestTrade(
            backtest_record_id=rec.id, strategy_id=strat.id,
            signal_name="open_sig", signal_type="OPEN", stock_code="000001.SZ",
            trade_type="BUY", price=Decimal(price), quantity=100,
            amount=Decimal(amount), commission=Decimal("0.00"),
            stamp_duty=Decimal("0.00"),
            bar_time=datetime(d.year, d.month, d.day, 15, 0)))
    db_session.commit()

    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    svc.recompute_market_regime(db_session, rec.id)
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    assert json.loads(row.payload_json)[
        "industry_attribution"]["status"] == "needs_rerun"

    # 价格与持仓市值逐日一致（加仓后按 10.50 → 市值 2100），缩放系数 k = 1
    prices = {d: (Decimal("10.00") if i < 5 else Decimal("10.50"))
              for i, d in enumerate(days)}
    out = backfill_record(db_session, rec.id,
                          close_provider=lambda codes, s, e: {
                              "000001.SZ": prices})
    assert out["status"] == "ok" and out["written"] is True
    assert out["cash_check"]["ok"] is True
    assert out["scale"]["max_dev"] == "0"   # 无缩放偏离

    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    svc.recompute_market_regime(db_session, rec.id)
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    # fake TQ 未提供行业指数 → failed（而非 needs_rerun），证明已越过双门禁
    assert json.loads(row.payload_json)[
        "industry_attribution"]["status"] == "failed"


def test_recompute_market_regime_without_benchmark_marks_failed(db_session, monkeypatch):
    """TR-8.2: 基准无数据 status=failed；详情主链路完整、其他模块不受影响。"""
    rec, ps = _seed_record(db_session)
    _mr_seed_snapshots(db_session, rec, ps)
    monkeypatch.setattr(svc, "TQData", lambda: _FakeIndexTQ({}))
    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "failed"
    assert "无数据" in out["error"]
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    assert row.status == "failed"
    assert row.payload_json is None

    detail = svc.get_record_detail(db_session, rec.id)
    assert detail["record"]["id"] == rec.id
    assert detail["snapshots"], "快照模块不受影响"
    assert detail["market_regime"]["status"] == "failed"
    assert detail["market_regime"]["data"] is None


def test_recompute_market_regime_insufficient_common_days(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    _mr_seed_snapshots(db_session, rec, ps, limit=10)  # 10 个共同日 < 30
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "insufficient"
    assert "不足" in out["error"]


class _FakeIndustryTQ(_FakeIndexTQ):
    """支持行业列表/个股行业反查的 TQ mock。"""

    def __init__(self, frames, industries, stock_industry):
        super().__init__(frames)
        self._industries = industries
        self._stock_industry = stock_industry

    def get_industry_list(self):
        return list(self._industries)

    def get_stock_industry(self, stock_code):
        return self._stock_industry.get(stock_code)


def test_tr16_industry_attribution_ok_with_positions(db_session, monkeypatch):
    """TR-16.1 service 链路：positions_json → 行业归因 ok 块并入 payload。"""
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    for d, v in nav_pts.items():
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal(str(round(v, 2))),
            positions_json=json.dumps([
                {"stock_code": "000001.SZ", "quantity": 100,
                 "sellable_quantity": 100,
                 "market_value": str(round(v, 2)), "cost_value": "1000"},
            ]),
        ))
    db_session.commit()

    industries = [
        {"code": "881001.SH", "name": "测试行业A"},
        {"code": "881002.SH", "name": "测试行业B"},
        {"code": "881003.SH", "name": "测试行业C"},
        {"code": "881004.SH", "name": "测试行业D"},
    ]
    # A（持仓行业）持续上涨；其余行业下跌
    frames = {"000300.SH": _ca_decimal_frames(bm_pts)}
    up = {d: Decimal(str(1000 * (1 + 0.004 * i)))
          for i, d in enumerate(sorted(nav_pts))}
    down = {d: Decimal("1000") for d in nav_pts}
    frames["881001.SH"] = up
    for c in ("881002.SH", "881003.SH", "881004.SH"):
        frames[c] = down
    fake = _FakeIndustryTQ(
        frames, industries,
        {"000001.SZ": {"code": "881001.SH", "name": "测试行业A"}})
    monkeypatch.setattr(svc, "TQData", lambda: fake)

    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "completed"
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    payload = json.loads(row.payload_json)
    ia = payload["industry_attribution"]
    assert ia["status"] == "ok"
    # 暴露：A 行业满仓
    ia_series = {s["code"]: s for s in ia["exposure"]["series"]}
    assert "881001.SH" in ia_series
    assert abs(ia_series["881001.SH"]["avg_weight"] - 1.0) < 1e-6
    # 命中率：每周都命中领涨 Top3
    wk = ia["hit_rate"]["weekly"]
    assert wk["periods_holding"] >= 10
    assert wk["hits"] == wk["periods_holding"]
    # 贡献：A 正贡献
    assert ia["contribution"]["available"] is True
    row_a = next(r for r in ia["contribution"]["rows"]
                 if r["code"] == "881001.SH")
    assert row_a["contribution"] > 0


def test_tr16_industry_attribution_failure_isolated(db_session, monkeypatch):
    """行业归因 TQ 异常不影响市场态主链路，落 failed 块。"""
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    for d, v in nav_pts.items():
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal(str(round(v, 2))),
            positions_json=json.dumps([
                {"stock_code": "000001.SZ", "quantity": 100,
                 "market_value": str(round(v, 2))},
            ]),
        ))
    db_session.commit()

    class _BoomIndustryTQ(_FakeIndexTQ):
        def get_industry_list(self):
            raise RuntimeError("industry_boom")

    monkeypatch.setattr(
        svc, "TQData",
        lambda: _BoomIndustryTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "completed"
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    payload = json.loads(row.payload_json)
    assert payload["industry_attribution"]["status"] == "failed"
    assert "industry_boom" in payload["industry_attribution"]["error"]


def test_tr18_robustness_never_calls_backtest_engine(db_session, monkeypatch):
    """AC-8/TR-18.1：稳健性只读快照/基准，BacktestEngine.run 零调用。"""
    from core.engine import backtest_engine
    rec, ps = _seed_record(db_session)
    _mr_seed_snapshots(db_session, rec, ps)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    run_mock = MagicMock(side_effect=AssertionError("稳健性分析禁止触发回测重跑"))
    monkeypatch.setattr(backtest_engine.BacktestEngine, "run", run_mock)

    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "completed"
    payload = json.loads(
        db_session.query(BacktestMarketRegimeAnalysis)
        .filter_by(backtest_record_id=rec.id).one().payload_json)
    rob = payload["robustness"]
    assert rob["status"] == "ok"
    assert "half_year_matrix" in rob and "rolling" in rob
    assert "longest_loss_window" in rob and "benchmark_sensitivity" in rob
    assert run_mock.call_count == 0


def test_recompute_market_regime_without_snapshots_returns_failed(db_session):
    rec, _ = _seed_record(db_session)
    out = svc.recompute_market_regime(db_session, rec.id)
    assert out["status"] == "failed"
    assert "无组合日快照" in out["error"]


def test_recompute_market_regime_missing_record_returns_none(db_session):
    assert svc.recompute_market_regime(db_session, 9999) is None


def test_safe_compute_market_regime_swallows_and_marks_failed(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)

    def _boom(*a, **kw):
        raise RuntimeError("mr_boom")

    monkeypatch.setattr(svc, "compute_and_store_market_regime", _boom)
    row = svc.safe_compute_market_regime(
        db_session, rec.id, ps, [], [], date(2024, 1, 1), date(2024, 3, 31)
    )
    assert row is not None
    assert row.status == "failed"
    assert "mr_boom" in row.error


# ---------------------------------------------------------------------------
# recompute_evaluation：离线从 DB 快照/成交重算评估（老记录补基准相对指标）
# ---------------------------------------------------------------------------
def _seed_linear_snapshots(db, rec, ps, n_days=25):
    """25 日线性序列入库：rs=0.5*rb+0.0003，rb 循环 ±1%/±2%（同 evaluator 测试）。

    快照列仅 Numeric(15,2)，基准则放大到百万点级以压低 2 位小数舍入噪声
    （服务测试只验证接线，数学精度已在 evaluator 单测固化）。
    """
    from datetime import timedelta
    rb_seq = [0.01, -0.01, 0.02, -0.02]
    vs, vb = 100_000_000.0, 10_000_000.0
    for i in range(n_days):
        db.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=date(2024, 1, 1) + timedelta(days=i),
            total_value=Decimal(str(round(vs, 2))),
            cash=Decimal("0"), market_value=Decimal(str(round(vs, 2))),
            benchmark_value=Decimal(str(round(vb, 2))),
        ))
        rb = rb_seq[i % 4]
        vs *= 1 + 0.5 * rb + 0.0003
        vb *= 1 + rb
    db.commit()


def test_recompute_evaluation_fills_relative_metrics(db_session):
    rec, ps = _seed_record(db_session)
    _seed_linear_snapshots(db_session, rec, ps)
    db_session.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=1, signal_name="", signal_type="OPEN",
        stock_code="000001.SZ", trade_type="BUY",
        price=Decimal("10"), quantity=1000, amount=Decimal("10000"),
        commission=Decimal("5"), stamp_duty=Decimal("0"),
        bar_time=datetime(2024, 1, 1, 10, 0),
    ))
    db_session.commit()

    out = svc.recompute_evaluation(db_session, rec.id)
    assert out["status"] == "ok"
    assert out["evaluations"]["beta"] == pytest.approx(0.5, abs=1e-3)
    assert out["evaluations"]["excess_annual_return"] == pytest.approx(0.0756, abs=2e-3)
    assert out["evaluations"]["up_capture_ratio"] == pytest.approx(0.52, abs=1e-2)
    assert out["evaluations"]["total_trades"] == 1

    row = db_session.query(BacktestEvaluation).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").one()
    assert float(row.beta) == pytest.approx(0.5, abs=1e-3)


def test_recompute_evaluation_updates_existing_row_in_place(db_session):
    rec, ps = _seed_record(db_session)
    _seed_linear_snapshots(db_session, rec, ps)
    # 老评估行：新指标列全 NULL（模拟老记录）
    db_session.add(BacktestEvaluation(
        backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
        total_return=Decimal("0.1"), beta=None,
    ))
    db_session.commit()

    out = svc.recompute_evaluation(db_session, rec.id)
    assert out["status"] == "ok"
    rows = db_session.query(BacktestEvaluation).filter_by(
        backtest_record_id=rec.id, target_type="portfolio").all()
    assert len(rows) == 1  # upsert，不新增
    assert rows[0].beta == Decimal("0.5000")


def test_recompute_evaluation_none_for_missing_record(db_session):
    assert svc.recompute_evaluation(db_session, 9999) is None


def test_recompute_evaluation_failed_without_snapshots(db_session):
    rec, _ = _seed_record(db_session)
    out = svc.recompute_evaluation(db_session, rec.id)
    assert out["status"] == "failed"
    assert "快照不足" in out["error"]


def test_recompute_evaluation_reads_positions_json(db_session):
    """positions_json（仅新回测有）经重算进入 HHI / 最大单票权重。"""
    from datetime import timedelta
    rec, ps = _seed_record(db_session)
    for i in range(24):
        holdings = (
            [{"stock_code": "A", "market_value": 60000},
             {"stock_code": "B", "market_value": 40000}]
            if i % 2 == 0
            else [{"stock_code": "A", "market_value": 100000}]
        )
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=date(2024, 1, 1) + timedelta(days=i),
            total_value=Decimal("100000"),
            cash=Decimal("0"), market_value=Decimal("100000"),
            benchmark_value=Decimal("1000"),
            positions_json=json.dumps(holdings),
        ))
    db_session.commit()

    out = svc.recompute_evaluation(db_session, rec.id)
    assert out["status"] == "ok"
    assert out["evaluations"]["hhi"] == 0.76
    assert out["evaluations"]["max_single_weight"] == 1.0
    # 基准恒定 → 相对指标仍为 None（互不干扰）
    assert out["evaluations"]["beta"] is None


def test_json_safe_sanitizes_nonfinite_floats():
    """_json_safe：NaN/±Inf → None（嵌套 dict/list/tuple），有限值原样。"""
    from core.services.backtest_service import _json_safe
    bad = {"a": float("nan"), "b": [1.5, float("inf"), (float("-inf"), 2)],
           "c": {"d": float("nan")}, "e": "文本", "f": None, "g": 3}
    out = _json_safe(bad)
    assert out == {"a": None, "b": [1.5, None, [None, 2]],
                   "c": {"d": None}, "e": "文本", "f": None, "g": 3}
    # 清洗后可过 allow_nan=False 严格序列化
    json.dumps(_json_safe(bad), allow_nan=False)


def test_recompute_evaluation_fills_strategy_level_metrics(db_session):
    """策略层：老快照无 benchmark_value → 重算时按日注入组合基准值，相对指标补齐。

    覆盖 2026-09 截图问题：策略卡「基准相对/磨损仓位/路径质量」整组「—」，
    根因是策略层评估无基准数据。修复后重算可补（持仓结构仍依赖 positions_json）。
    """
    from datetime import timedelta
    rec, ps = _seed_record(db_session)
    _seed_linear_snapshots(db_session, rec, ps)
    # 策略层快照（sid=7）：无 benchmark_value（模拟老记录），净值同组合线性规律
    rb_seq = [0.01, -0.01, 0.02, -0.02]
    vs = 50_000_000.0
    for i in range(25):
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="strategy", target_id=7,
            snap_date=date(2024, 1, 1) + timedelta(days=i),
            total_value=Decimal(str(round(vs, 2))),
            cash=Decimal("0"), market_value=Decimal(str(round(vs, 2))),
            benchmark_value=None,
        ))
        vs *= 1 + 0.5 * rb_seq[i % 4] + 0.0003
    db_session.commit()

    out = svc.recompute_evaluation(db_session, rec.id)
    assert out["status"] == "ok"
    srow = db_session.query(BacktestEvaluation).filter_by(
        backtest_record_id=rec.id, target_type="strategy", target_id=7).one()
    # 注入组合基准后：beta≈0.5（与组合同规律），年化超额≈7.56%
    assert float(srow.beta) == pytest.approx(0.5, abs=1e-3)
    assert float(srow.excess_annual_return) == pytest.approx(0.0756, abs=2e-3)


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


_SNAPSHOT_KEYS = {
    "benchmark_index", "initial_capital", "max_drawdown", "daily_loss_limit",
    "max_holdings", "min_commission", "buy_commission_rate",
    "sell_commission_rate", "stamp_duty_rate", "slippage", "trading_session",
    "stock_pool_id", "dividend_mode",
}

_NO_TRADES_RESULT = {
    "trades": [], "snapshots": [], "evaluations": {},
    "strategy_snapshots": {}, "strategy_evaluations": {},
}


def _patch_data_layer(monkeypatch, result=None):
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    if result is not None:
        monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: result)


def _seed_ca_daily_snapshots(db, rec, ps, points):
    for d, v in points.items():
        db.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal("0"),
        ))
    db.commit()


def _seed_stored_ca_row(db, rec, code, name):
    db.add(BacktestCrisisAlpha(
        backtest_record_id=rec.id, benchmark_code=code, benchmark_name=name,
        status="completed", skipped_benchmarks_json="[]", payload_json=None,
        computed_at=datetime(2026, 1, 2, 3, 4, 5),
    ))
    db.commit()


def test_run_backtest_writes_params_snapshot(db_session, monkeypatch):
    ps, _ = _seed(db_session)
    _patch_data_layer(monkeypatch, _NO_TRADES_RESULT)
    rid = svc.run_backtest(db_session, ps, _req(name="snap"))["record_id"]
    rec = db_session.get(BacktestRecord, rid)
    assert rec.params_snapshot is not None
    data = json.loads(rec.params_snapshot)
    assert data["benchmark_index"] == ps.benchmark_index == "000300.SH"


def test_params_snapshot_round_trips_source_attributes(db_session, monkeypatch):
    ps, _ = _seed(db_session)
    _patch_data_layer(monkeypatch, _NO_TRADES_RESULT)
    rid = svc.run_backtest(db_session, ps, _req(name="snap_rt"))["record_id"]
    data = json.loads(db_session.get(BacktestRecord, rid).params_snapshot)
    assert set(data) == _SNAPSHOT_KEYS
    assert data["benchmark_index"] == ps.benchmark_index
    assert data["trading_session"] == ps.trading_session
    assert int(data["max_holdings"]) == ps.max_holdings
    assert int(data["stock_pool_id"]) == ps.stock_pool_id
    assert Decimal(data["initial_capital"]) == ps.initial_capital
    assert Decimal(data["max_drawdown"]) == ps.max_drawdown
    assert Decimal(data["daily_loss_limit"]) == ps.daily_loss_limit
    assert Decimal(data["min_commission"]) == ps.min_commission
    assert Decimal(data["buy_commission_rate"]) == ps.buy_commission_rate
    assert Decimal(data["sell_commission_rate"]) == ps.sell_commission_rate
    assert Decimal(data["stamp_duty_rate"]) == ps.stamp_duty_rate
    assert Decimal(data["slippage"]) == ps.slippage
    assert json.dumps(data, allow_nan=False)


def test_run_backtest_dual_mode_resolves_signal_front_accounting_back(db_session, monkeypatch):
    """dual 模式：引擎 K线/基准走后复权（记账），公式信号走前复权（信号）；
    且 params_snapshot 记录 dividend_mode=dual。"""
    ps, _ = _seed(db_session)
    seen = {}

    def _kl(ps, start, end, db=None, dividend_mode="front"):
        seen["klines"] = dividend_mode
        return _mock_klines()

    def _cache(ps, klines, db=None, start=None, diagnostics=None, dividend_mode="front"):
        seen["cache"] = dividend_mode
        return {}

    def _bench(ps, start, end, db=None, dividend_mode="front"):
        seen["bench"] = dividend_mode
        return {}

    monkeypatch.setattr(svc, "build_klines", _kl)
    monkeypatch.setattr(svc, "build_signal_cache", _cache)
    monkeypatch.setattr(svc, "build_benchmark_data", _bench)
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    monkeypatch.setattr(svc.BacktestEngine, "run",
                        lambda self, portfolio, **kw: _NO_TRADES_RESULT)
    rid = svc.run_backtest(db_session, ps, _req(dividend_mode="dual"))["record_id"]
    assert seen == {"klines": "back", "cache": "front", "bench": "back"}
    snap = json.loads(db_session.get(BacktestRecord, rid).params_snapshot)
    assert snap["dividend_mode"] == "dual"


def test_recompute_prefers_snapshot_benchmark(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    _seed_ca_daily_snapshots(
        db_session, rec, ps, _ca_daily_points([0.0] + _CA_WEEKS_S))
    rec.params_snapshot = json.dumps({"benchmark_index": "000905.SH"})
    db_session.commit()
    ps.benchmark_index = "000300.SH"
    db_session.commit()
    fake = _FakeIndexTQ({
        "000905.SH": _ca_decimal_frames(_ca_daily_points([0.0] + _CA_WEEKS_B))})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "completed"
    br = out["benchmark_resolution"]
    assert br["from_snapshot"] is True
    assert br["benchmark_code"] == "000905.SH"
    assert br["benchmark_name"] == "中证500"
    assert br["live_benchmark_code"] == "000300.SH"
    assert br["mismatch"] is True
    row = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).one()
    assert row.benchmark_code == "000905.SH"
    assert row.benchmark_name == "中证500"
    data = json.loads(row.payload_json)
    assert data["benchmark_code"] == "000905.SH"
    assert data["benchmark_name"] == "中证500"
    assert "沪深300" in json.loads(row.skipped_benchmarks_json)


def test_recompute_falls_back_to_live_when_no_snapshot(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    _seed_ca_daily_snapshots(
        db_session, rec, ps, _ca_daily_points([0.0] + _CA_WEEKS_S))
    assert rec.params_snapshot is None
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(_ca_daily_points([0.0] + _CA_WEEKS_B))})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "completed"
    br = out["benchmark_resolution"]
    assert br["from_snapshot"] is False
    assert br["benchmark_code"] == "000300.SH"
    assert br["benchmark_name"] == "沪深300"
    assert br["mismatch"] is False
    assert br["stored_benchmark_code"] is None
    row = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).one()
    assert row.benchmark_code == "000300.SH"


def test_recompute_legacy_record_preserves_stored_benchmark_and_warns(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    _seed_ca_daily_snapshots(
        db_session, rec, ps, _ca_daily_points([0.0] + _CA_WEEKS_S))
    _seed_stored_ca_row(db_session, rec, "000300.SH", "沪深300")
    ps.benchmark_index = "000905.SH"
    db_session.commit()
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(_ca_daily_points([0.0] + _CA_WEEKS_B))})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "completed"
    br = out["benchmark_resolution"]
    assert br["from_snapshot"] is False
    assert br["benchmark_code"] == "000300.SH"
    assert br["benchmark_name"] == "沪深300"
    assert br["live_benchmark_code"] == "000905.SH"
    assert br["live_benchmark_name"] == "中证500"
    assert br["stored_benchmark_code"] == "000300.SH"
    assert br["stored_benchmark_name"] == "沪深300"
    assert br["mismatch"] is True
    row = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).one()
    assert row.benchmark_code == "000300.SH"
    assert row.benchmark_name == "沪深300"
    assert json.loads(row.payload_json)["benchmark_code"] == "000300.SH"


def test_recompute_snapshot_matching_live_reports_no_mismatch(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    _seed_ca_daily_snapshots(
        db_session, rec, ps, _ca_daily_points([0.0] + _CA_WEEKS_S))
    _seed_stored_ca_row(db_session, rec, "000300.SH", "沪深300")
    rec.params_snapshot = json.dumps({"benchmark_index": "000300.SH"})
    db_session.commit()
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(_ca_daily_points([0.0] + _CA_WEEKS_B))})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    br = out["benchmark_resolution"]
    assert br["from_snapshot"] is True
    assert br["benchmark_code"] == "000300.SH"
    assert br["live_benchmark_code"] == "000300.SH"
    assert br["mismatch"] is False


def test_resolve_crisis_benchmark_cases_without_db():
    ps = SimpleNamespace(benchmark_index="000300.SH")
    snap = SimpleNamespace(params_snapshot=json.dumps({"benchmark_index": "000905.SH"}))
    row = SimpleNamespace(benchmark_code="000300.SH", benchmark_name="沪深300")
    from_snap = svc.resolve_crisis_benchmark(snap, row, ps)
    assert from_snap["benchmark_code"] == "000905.SH"
    assert from_snap["benchmark_name"] == "中证500"
    assert from_snap["from_snapshot"] is True
    assert from_snap["mismatch"] is True
    assert from_snap["stored_benchmark_code"] == "000300.SH"
    assert from_snap["stored_benchmark_name"] == "沪深300"
    legacy = svc.resolve_crisis_benchmark(
        SimpleNamespace(params_snapshot=None), row, ps)
    assert legacy["benchmark_code"] == "000300.SH"
    assert legacy["from_snapshot"] is False
    assert legacy["mismatch"] is False
    assert legacy["live_benchmark_name"] == "沪深300"
    legacy_edited = svc.resolve_crisis_benchmark(
        SimpleNamespace(params_snapshot=None), row,
        SimpleNamespace(benchmark_index="000905.SH"))
    assert legacy_edited["benchmark_code"] == "000300.SH"
    assert legacy_edited["from_snapshot"] is False
    assert legacy_edited["mismatch"] is True
    assert legacy_edited["live_benchmark_code"] == "000905.SH"
    assert legacy_edited["live_benchmark_name"] == "中证500"
    assert legacy_edited["stored_benchmark_code"] == "000300.SH"
    fresh = svc.resolve_crisis_benchmark(SimpleNamespace(params_snapshot=None), None, ps)
    assert fresh["benchmark_code"] == "000300.SH"
    assert fresh["from_snapshot"] is False
    assert fresh["mismatch"] is False
    assert fresh["stored_benchmark_code"] is None
    broken = svc.resolve_crisis_benchmark(
        SimpleNamespace(params_snapshot="{not json"), row, ps)
    assert broken["benchmark_code"] == "000300.SH"
    assert broken["from_snapshot"] is False
    defaulted = svc.resolve_crisis_benchmark(
        SimpleNamespace(params_snapshot=None), None,
        SimpleNamespace(benchmark_index=None))
    assert defaulted["benchmark_code"] == "000300.SH"


# ---------------------------------------------------------------------------
# 信号预检：build_signal_warnings（零信号 / 输出线缺失 / 公式报错）
# 场景根因：30 分钟主图公式挂在 1d 策略上时公式不报错但全程零信号，
# 回测静默 completed，用户无法判断"为什么没有交易"。
# ---------------------------------------------------------------------------
def _seed_strat(db, period="1d", trigger=1, sig_name="open_sig"):
    """基于 _seed 改周期/触发值/信号名，返回 (ps, strat)。"""
    ps, strat_id = _seed(db)
    strat = db.get(Strategy, strat_id)
    strat.period = period
    sig = db.query(FormulaSignal).filter_by(formula_id=strat.formula_id).one()
    sig.signal_name = sig_name
    sig.trigger_value = trigger
    db.commit()
    return ps, strat


def test_build_signal_warnings_zero_signal(db_session):
    """信号在回测区间内从未触发 → 警告含策略名/信号名/触发值/周期。"""
    ps, strat = _seed_strat(db_session, period="30m", trigger=2, sig_name="最终强交易买")
    t = datetime(2026, 7, 29)
    # 输出线存在，但全程为 0（无信号常态值），永不等于 trigger=2
    cache = {(strat.id, "000001.SZ", t): [{"name": "最终强交易买", "value": 0}]}
    diagnostics = {"errors": {}, "output_lines": {strat.id: {"最终强交易买"}}}

    warnings = svc.build_signal_warnings(db_session, [strat], cache, diagnostics)

    assert len(warnings) == 1
    assert "s1" in warnings[0]
    assert "最终强交易买" in warnings[0]
    assert "2" in warnings[0]
    assert "30m" in warnings[0]
    assert "从未触发" in warnings[0]


def test_build_signal_warnings_triggered_no_warning(db_session):
    """区间内存在一次匹配触发 → 无警告。"""
    ps, strat = _seed_strat(db_session, trigger=2)
    t = datetime(2026, 7, 29)
    cache = {(strat.id, "000001.SZ", t): [{"name": "open_sig", "value": 2}]}
    diagnostics = {"errors": {}, "output_lines": {strat.id: {"open_sig"}}}

    assert svc.build_signal_warnings(db_session, [strat], cache, diagnostics) == []


def test_build_signal_warnings_missing_output_line(db_session):
    """配置的信号名不在公式实际输出线中 → 输出线缺失警告（线名写错/公式版本不同）。"""
    ps, strat = _seed_strat(db_session, sig_name="最终强交易买")
    diagnostics = {"errors": {}, "output_lines": {strat.id: {"MACD柱", "趋势"}}}

    warnings = svc.build_signal_warnings(db_session, [strat], {}, diagnostics)

    assert len(warnings) == 1
    assert "最终强交易买" in warnings[0]
    assert "输出线" in warnings[0]


def test_build_signal_warnings_formula_error_short_circuits(db_session):
    """公式执行报错 → 一条错误警告，不再逐信号报零触发（错误才是根因）。"""
    ps, strat = _seed_strat(db_session)
    diagnostics = {"errors": {strat.id: "ErrorId=3 Error=公式语法错误"}, "output_lines": {}}

    warnings = svc.build_signal_warnings(db_session, [strat], {}, diagnostics)

    assert len(warnings) == 1
    assert "公式执行失败" in warnings[0]
    assert "公式语法错误" in warnings[0]


def test_build_signal_warnings_empty_diagnostics_no_crash(db_session):
    """诊断信息缺失（如 build_signal_cache 被旁路）→ 不误报、不异常。"""
    ps, strat = _seed_strat(db_session)
    assert svc.build_signal_warnings(db_session, [strat], {}, {}) == []


def test_build_signal_cache_collects_error_diagnostics(db_session, monkeypatch):
    """ErrorId 异常 → diagnostics['errors'] 记录策略与错误，cache 为空。"""
    ps, strat = _seed_strat(db_session)
    klines = _mock_klines()

    class _FakeTQ:
        def compute(self, **kw):
            return {"ErrorId": 7, "Error": "公式语法错误"}

    monkeypatch.setattr(svc, "TQFormula", _FakeTQ)

    diagnostics = {}
    cache = svc.build_signal_cache(ps, klines, db_session, diagnostics=diagnostics)

    assert cache == {}
    assert strat.id in diagnostics["errors"]
    assert "ErrorId=7" in diagnostics["errors"][strat.id]


def test_build_signal_cache_collects_output_lines(db_session, monkeypatch):
    """正常返回 → diagnostics['output_lines'] 收集该公式全部真实输出线名。"""
    ps, strat = _seed_strat(db_session)
    klines = _mock_klines()

    class _FakeTQ:
        def compute(self, **kw):
            return {
                "ErrorId": 0,
                "000001.SZ": {
                    "open_sig": [{"Date": "20260729", "Value": "2.00"}],
                    "MACD柱": [{"Date": "20260729", "Value": "0.10"}],
                },
            }

    monkeypatch.setattr(svc, "TQFormula", _FakeTQ)

    diagnostics = {}
    cache = svc.build_signal_cache(ps, klines, db_session, diagnostics=diagnostics)

    assert diagnostics["errors"] == {}
    assert diagnostics["output_lines"][strat.id] == {"open_sig", "MACD柱"}
    # cache 正常转换不受影响
    key = (strat.id, "000001.SZ", datetime(2026, 7, 29))
    assert {"name": "open_sig", "value": 2} in cache[key]


def _patch_common_backtest_deps(monkeypatch):
    """run_backtest 周边依赖（信号/基准/危机Alpha走桩，引擎由各用例自行替换）。"""
    monkeypatch.setattr(svc, "build_klines", lambda ps, s, e, db=None, dividend_mode="front": _mock_klines())
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, s, e, db=None, dividend_mode="front": {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)


def _fake_engine_result():
    return {
        "trades": [],
        "snapshots": [
            {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
             "cash": Decimal("100000"), "market_value": Decimal("0")},
        ],
        "evaluations": {"total_return": Decimal("0")},
        "strategy_snapshots": {},
        "strategy_evaluations": {},
    }


def test_run_backtest_zero_signal_persists_warning(db_session, monkeypatch):
    """端到端：零信号回测 completed 但 warnings_json 落库，详情接口可读。"""
    ps, strat = _seed_strat(db_session, trigger=2, sig_name="最终强交易买")
    _patch_common_backtest_deps(monkeypatch)

    def _fake_cache(ps_, klines, db=None, start=None, diagnostics=None, dividend_mode="front"):
        if diagnostics is not None:
            diagnostics["errors"] = {}
            diagnostics["output_lines"] = {strat.id: {"最终强交易买"}}
        return {}  # 全程零信号

    monkeypatch.setattr(svc, "build_signal_cache", _fake_cache)
    monkeypatch.setattr(svc.BacktestEngine, "run",
                        lambda self, portfolio, **kw: _fake_engine_result())

    result = svc.run_backtest(db_session, ps, _req())

    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "completed"
    warnings = json.loads(rec.warnings_json)
    assert any("最终强交易买" in w and "从未触发" in w for w in warnings)

    detail = svc.get_record_detail(db_session, rec.id)
    assert any("最终强交易买" in w for w in detail["record"]["warnings"])


def test_run_backtest_triggered_signal_has_no_warning(db_session, monkeypatch):
    """信号正常触发时 warnings_json 为 NULL，详情 warnings 为空列表。"""
    ps, strat = _seed_strat(db_session, trigger=2)
    _patch_common_backtest_deps(monkeypatch)

    def _fake_cache(ps_, klines, db=None, start=None, diagnostics=None, dividend_mode="front"):
        if diagnostics is not None:
            diagnostics["errors"] = {}
            diagnostics["output_lines"] = {strat.id: {"open_sig"}}
        t = datetime(2026, 7, 29)
        return {(strat.id, "000001.SZ", t): [{"name": "open_sig", "value": 2}]}

    monkeypatch.setattr(svc, "build_signal_cache", _fake_cache)
    monkeypatch.setattr(svc.BacktestEngine, "run",
                        lambda self, portfolio, **kw: _fake_engine_result())

    result = svc.run_backtest(db_session, ps, _req())

    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.warnings_json is None
    detail = svc.get_record_detail(db_session, rec.id)
    assert detail["record"]["warnings"] == []


def test_run_backtest_missing_pool_stock_persists_data_warning(db_session, monkeypatch):
    """个别股票行情取不到（逐股兜底已跳过）→ 回测仍 completed，
    但 warnings_json 落库记录被跳过的股票，详情接口可读。"""
    ps, strat = _seed_strat(db_session, trigger=2, sig_name="open_sig")
    db_session.add(StockPoolStock(
        pool_id=ps.stock_pool_id, stock_code="920000.BJ", stock_name="坏数据"))
    db_session.commit()
    _patch_common_backtest_deps(monkeypatch)  # build_klines 只返回 000001.SZ

    def _fake_cache(ps_, klines, db=None, start=None, diagnostics=None, dividend_mode="front"):
        if diagnostics is not None:
            diagnostics["errors"] = {}
            diagnostics["output_lines"] = {strat.id: {"open_sig"}}
        t = datetime(2026, 7, 29)
        return {(strat.id, "000001.SZ", t): [{"name": "open_sig", "value": 2}]}

    monkeypatch.setattr(svc, "build_signal_cache", _fake_cache)
    monkeypatch.setattr(svc.BacktestEngine, "run",
                        lambda self, portfolio, **kw: _fake_engine_result())

    result = svc.run_backtest(db_session, ps, _req())

    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "completed"
    warnings = json.loads(rec.warnings_json)
    assert any("920000.BJ" in w for w in warnings)
    assert any("920000.BJ" in w for w in result["warnings"])

    detail = svc.get_record_detail(db_session, rec.id)
    assert any("920000.BJ" in w for w in detail["record"]["warnings"])
