from datetime import datetime, date, timezone
from decimal import Decimal
from threading import Lock
from typing import Dict, List, Optional

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.models import (
    PortfolioStrategy, Strategy, Formula, FormulaSignal, StockPoolStock,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
)
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio_builder import (
    assemble_portfolio as _assemble_portfolio,
    signal_type_from_str as _signal_type_from_str,
)
from tq_iquant_shared.constants import SignalType
from core.tq.data import TQData
from core.tq.formula import TQFormula

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# 并发保护：同一时刻全局只允许 1 个回测在跑（回测是同步内联执行，双并发会抢 TQ
# 资源 + 重复写库）。与实盘互不互斥——实盘有自己的 B6 单 session 守卫（live.py）。
_BACKTEST_LOCK = Lock()


def _log_timing(msg: str) -> None:
    """打印耗时日志，带时间戳。同步端点（uvicorn 单线程），print 安全。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def _log_kline_summary(record_id: int, klines: dict) -> None:
    """打印 K 线数据量摘要：股票数、各周期 bar 数。"""
    if not klines:
        _log_timing(f"[回测#{record_id}] K线数据为空")
        return
    stock_count = len(klines)
    first_code = next(iter(klines))
    period_bars = {}
    for period, df in klines[first_code].items():
        period_bars[period] = len(df) if df is not None else 0
    _log_timing(
        f"[回测#{record_id}] K线: {stock_count}只股票, "
        + ", ".join(f"{p}={n}bar" for p, n in period_bars.items())
    )




class BacktestRequest(BaseModel):
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date


# ---------------------------------------------------------------------------
# 业务逻辑已下沉到 core.services.backtest_service（P1 #9）。
# 下方 re-export 供测试 bt_api.xxx 兼容（test_backtest_data.py / test_backtest_api.py
# 直接 import core.api.backtest 引用 build_klines 等数据层符号 + monkeypatch TQData/TQFormula）。
# 类方法 monkeypatch 与 import 路径无关，re-export 后仍生效。
# ---------------------------------------------------------------------------
from core.services.backtest_service import (  # noqa: E402
    build_klines,
    build_open_prices,
    build_benchmark_data,
    build_signal_cache,
    _convert_market_data,
    _convert_market_data_multi,
    _convert_formula_output,
    _MINUTE_PERIODS,
    _persist_result,          # 阶段 2：迁 service；阶段 3 _run_backtest_locked 迁走后可从 re-export 删除
    list_records as _svc_list_records,
    get_record_detail as _svc_get_record_detail,
    delete_record as _svc_delete_record,
)
from core.tq.data import TQData  # noqa: E402  re-export（测试 monkeypatch bt_api.TQData）
from core.tq.formula import TQFormula  # noqa: E402  re-export（同上）


# ---------------------------------------------------------------------------
# 组装 Portfolio
# ---------------------------------------------------------------------------
# _signal_type_from_str / _assemble_portfolio 已抽取到 core.engine.portfolio_builder
# （回测/实盘共用），在此模块顶部 import 为 _signal_type_from_str / _assemble_portfolio。


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/records")
def list_records(db: Session = Depends(get_db)):
    return ok(_svc_list_records(db))


@router.get("/records/{record_id}")
def get_record(record_id: int, db: Session = Depends(get_db)):
    detail = _svc_get_record_detail(db, record_id)
    if detail is None:
        return err(404, "回测记录不存在")
    return ok(detail)


@router.delete("/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    """删除回测记录 + 级联子表（trades/snapshots/evaluations）。
    子表 FK 虽配 ondelete=CASCADE，但显式删更稳妥（不依赖连接级 PRAGMA）。"""
    if not _svc_delete_record(db, record_id):
        return err(404, "回测记录不存在")
    return ok()


def _validate_backtest_request(req: BacktestRequest) -> Optional[str]:
    """回测请求基础校验，返回错误消息或 None（通过）。
    校验日期区间：start < end，且 start 不在未来（TQ 拉不到未来行情）。"""
    if req.start_date >= req.end_date:
        return f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}"
    today = date.today()
    if req.start_date > today:
        return f"开始日期不可在未来，收到 {req.start_date}（今天 {today}）"
    return None


@router.post("")
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    """启动回测（同步内联执行）。并发保护：同一时刻全局最多 1 个回测，已有在跑则 409。

    校验（404/400）在锁外，只对合法请求抢锁；锁由 finally 保证异常路径也释放。
    """
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        return err(404, "组合策略不存在")

    err_msg = _validate_backtest_request(req)
    if err_msg:
        return err(400, err_msg)

    if not _BACKTEST_LOCK.acquire(blocking=False):
        # 有意保留真实 HTTP 409（非 body-code）：并发契约 + 测试断言 status_code==409
        # （test_post_backtest_409_when_already_running）。body-code 会破坏前端对并发冲突
        # 的 HTTP 状态码判断。与统一 envelope 的模式 A 不同——此为刻意的模式 B 例外。
        raise HTTPException(
            status_code=409,
            detail="回测正在进行中，请等待当前回测完成后再启动（同一时刻仅允许 1 个回测）",
        )
    try:
        return _run_backtest_locked(req, db, ps)
    finally:
        _BACKTEST_LOCK.release()


def _run_backtest_locked(req: BacktestRequest, db: Session, ps: PortfolioStrategy) -> dict:
    """持锁执行回测主链路。锁由 run_backtest 获取，finally 保证异常路径也释放。"""
    strategies = (
        db.query(Strategy)
        .filter_by(portfolio_id=ps.id)
        .all()
    )

    # 写 record（running）
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        status="running",
        progress=0,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    record_id = rec.id

    try:
        t0 = datetime.now()
        portfolio = _assemble_portfolio(ps, strategies, db)
        t1 = datetime.now()
        klines = build_klines(ps, req.start_date, req.end_date, db)
        t2 = datetime.now()
        # 数据量摘要：各周期多少根 bar、多少只股票，帮助判断瓶颈
        _log_kline_summary(record_id, klines)
        signal_cache = build_signal_cache(ps, klines, db)
        t3 = datetime.now()
        open_prices = build_open_prices(ps, klines)
        t4 = datetime.now()
        benchmark_data = build_benchmark_data(ps, req.start_date, req.end_date, db)
        t5 = datetime.now()

        # 各阶段耗时日志（定位回测慢的根因）
        _log_timing(f"[回测#{record_id}] 组装组合: {(t1-t0).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ K线拉取: {(t2-t1).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 公式计算: {(t3-t2).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 提取开盘价: {(t4-t3).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 基准指数: {(t5-t4).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 数据准备合计: {(t5-t0).total_seconds():.1f}s")

        # 空行情保护：TQ 在该区间/股票池拉不到任何 K 线 → 标 failed 而非静默 completed。
        # 这正是"启动了但没运行直接完成"的根因（日期填反/未来日/股票池空等）。
        if not klines:
            rec.status = "failed"
            rec.error_message = (
                f"未取到任何行情数据：区间 {req.start_date}~{req.end_date}，"
                f"股票池 id={ps.stock_pool_id}。请检查日期区间与股票池成分。"
            )
            db.commit()
            return ok({"record_id": record_id, "trades_count": 0,
                       "snapshots_count": 0, "evaluations": {}})

        t6 = datetime.now()
        engine = BacktestEngine()
        result = engine.run(
            portfolio,
            klines=klines,
            signal_cache=signal_cache,
            open_prices=open_prices,
            benchmark_data=benchmark_data,
        )
        t7 = datetime.now()
        _log_timing(f"[回测#{record_id}] 引擎逐bar回测: {(t7-t6).total_seconds():.1f}s")

        _persist_result(db, record_id, ps.id, result, strategies)
        t8 = datetime.now()
        _log_timing(f"[回测#{record_id}] 结果持久化: {(t8-t7).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 总耗时: {(t8-t0).total_seconds():.1f}s")

        rec.status = "completed"
        rec.progress = 100
        rec.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC，与原 utcnow() 语义一致（Python 3.13 已弃用 utcnow）
        db.commit()
    except Exception as e:
        # 异常时把 record 标 failed 并落库。session 可能因异常处于脏状态，
        # 先 rollback 再改字段重提交，确保状态写进去（否则前端会看到永久 running）。
        db.rollback()
        rec = db.get(BacktestRecord, record_id)
        if rec is not None:
            rec.status = "failed"
            rec.error_message = str(e) or repr(e)
            db.commit()
        raise

    return ok({
        "record_id": record_id,
        "trades_count": len(result["trades"]),
        "snapshots_count": len(result["snapshots"]),
        "evaluations": result.get("evaluations") or {},
    })
