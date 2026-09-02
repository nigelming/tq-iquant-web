from datetime import date
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.models import PortfolioStrategy

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# 并发保护：同一时刻全局只允许 1 个回测在跑（回测是同步内联执行，双并发会抢 TQ
# 资源 + 重复写库）。与实盘互不互斥——实盘有自己的 B6 单 session 守卫（live.py）。
_BACKTEST_LOCK = Lock()


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
    warmup_counts_by_period,
    _convert_market_data,
    _convert_market_data_multi,
    _convert_formula_output,
    _merge_raw_by_period,
    _slice_klines_from,
    _MINUTE_PERIODS,
    list_records as _svc_list_records,
    get_record_detail as _svc_get_record_detail,
    delete_record as _svc_delete_record,
    run_backtest as _svc_run_backtest,
)
from core.tq.data import TQData  # noqa: E402  re-export（测试 monkeypatch bt_api.TQData）
from core.tq.formula import TQFormula  # noqa: E402  re-export（同上）


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
    主链路在 core.services.backtest_service.run_backtest（P1 #9 阶段 3 迁出）。
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
        return ok(_svc_run_backtest(db, ps, req))
    finally:
        _BACKTEST_LOCK.release()
