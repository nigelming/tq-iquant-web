from datetime import date
from threading import Lock
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.config import load_config
from core.db import get_db
from core.models import PortfolioStrategy
from core.services.html_report import build_html_report

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# 并发保护：回测为同步内联执行，同时运行数上限由 config.max_concurrent_backtest
# 控制（默认 1）。超限时新请求返回 HTTP 409。与实盘互不互斥——实盘有自己的单
# session 守卫（live.py）。
_BACKTEST_GUARD = Lock()
_running_backtests = 0


def _max_concurrent_backtest() -> int:
    try:
        n = int(load_config().get("max_concurrent_backtest", 1) or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def _try_acquire_slot() -> bool:
    global _running_backtests
    with _BACKTEST_GUARD:
        if _running_backtests >= _max_concurrent_backtest():
            return False
        _running_backtests += 1
        return True


def _release_slot() -> None:
    global _running_backtests
    with _BACKTEST_GUARD:
        if _running_backtests > 0:
            _running_backtests -= 1



class BacktestRequest(BaseModel):
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date
    # 复权模式：front 前复权（默认，历史行为） / back 后复权 / dual 前复权信号+后复权记账
    dividend_mode: str = "front"
    # B6 考核口径：absolute 绝对收益（默认） / relative 相对收益；仅影响详情 acceptance 验收块
    evaluation_profile: str = "absolute"
    # C7 IS/OOS 采纳门槛（默认关）：启用后回测完成时评估 OOS CAGR ≥ min_ratio × IS CAGR
    # 且 OOS 不跑输基准，否则 gate_status=rejected（记录保留，仅标记不被采纳）
    is_oos_gate_enabled: bool = False
    oos_gate_min_ratio: float = 0.5
    # 费用预留（默认关）：开启后买入审批含佣金，避免现金为负；关闭为原行为
    fee_reserve_enabled: bool = False
    # 可转债 run 级开关（默认关，仅回测生效；关闭时与原逻辑完全一致）：
    # bond_t0_enabled：转债 T+0 回转（当日买入当日可卖，股票仍强制 T+1）；
    # bond_lot_enabled：转债最小交易单位 10 张（股票仍 100 股/手）
    bond_t0_enabled: bool = False
    bond_lot_enabled: bool = False
    # 自适应滑点 run 级覆盖（None=用组合默认）：临时开启/调整市场冲击成本，
    # 不改组合配置，便于同一组合 A/B 对比
    adaptive_slippage_enabled: bool | None = None
    slippage_impact_coef: float | None = None
    # 成交量约束 run 级覆盖（None=用组合默认）：临时开启/调整单笔成交量占比封顶
    volume_limit_enabled: bool | None = None
    volume_limit_ratio: float | None = None
    # 容量上限 run 级覆盖（None=用组合默认）：临时封顶 dynamic 仓位资金基座
    equity_cap: float | None = None


class PoolCompareRequest(BaseModel):
    portfolio_strategy_id: int
    pool_ids: List[int]
    start_date: date
    end_date: date
    name_prefix: str = "换池对比"
    dividend_mode: str = "dual"


class WalkForwardRequest(BaseModel):
    """C8 walk-forward 滚动验证：滚动 train_years 年训练 + test_years 年验证逐段前移，
    每窗子回测过 IS/OOS 门槛（C7 同一判定），全窗 passed 才 overall_pass。"""
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date
    train_years: int = 3
    test_years: int = 1
    oos_gate_min_ratio: float = 0.5
    dividend_mode: str = "front"
    evaluation_profile: str = "absolute"


class ParamPerturbRequest(BaseModel):
    """P2b 参数微扰敏感性分析（可选，默认不使用）：对子策略数值参数逐一
    ±perturb_pct 扰动各跑一轮子回测 + 1 轮基准，汇总各参数 CAGR 敏感度。"""
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date
    perturb_pct: float = 0.10
    dividend_mode: str = "front"
    evaluation_profile: str = "absolute"


# ---------------------------------------------------------------------------
# 业务逻辑已下沉到 core.services.backtest_service（P1 #9）。
# 下方 re-export 供测试 bt_api.xxx 兼容（test_backtest_data.py / test_backtest_api.py
# 直接 import core.api.backtest 引用 build_klines 等数据层符号 + monkeypatch TQData/TQFormula）。
# 类方法 monkeypatch 与 import 路径无关，re-export 后仍生效。
# ---------------------------------------------------------------------------
from core.services.backtest_service import (  # noqa: E402
    build_klines,
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
    recompute_crisis_alpha as _svc_recompute_crisis_alpha,
    recompute_market_regime as _svc_recompute_market_regime,
    recompute_evaluation as _svc_recompute_evaluation,
    recompute_robustness as _svc_recompute_robustness,
    run_pool_comparison as _svc_run_pool_comparison,
)
from core.services.walk_forward_service import (  # noqa: E402
    run_walk_forward as _svc_run_walk_forward,
    list_walk_forward_runs as _svc_list_walk_forward_runs,
    get_walk_forward_run as _svc_get_walk_forward_run,
    delete_walk_forward_run as _svc_delete_walk_forward_run,
)
from core.services.param_perturb_service import (  # noqa: E402
    run_param_perturb as _svc_run_param_perturb,
    list_param_perturb_runs as _svc_list_param_perturb_runs,
    get_param_perturb_run as _svc_get_param_perturb_run,
    delete_param_perturb_run as _svc_delete_param_perturb_run,
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
def get_record(record_id: int, profile: Optional[str] = None, db: Session = Depends(get_db)):
    # B6：?profile=absolute|relative 临时切换验收口径查看（不改记录存储口径）
    detail = _svc_get_record_detail(db, record_id, profile_override=profile)
    if detail is None:
        return err(404, "回测记录不存在")
    return ok(detail)


@router.delete("/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    """删除回测记录 + 级联子表（trades/snapshots/evaluations/decisions）。
    子表 FK 虽配 ondelete=CASCADE，但显式删更稳妥（不依赖连接级 PRAGMA）。

    #4: status=running 时拒绝删除（409），防止并发删除运行中的回测引发
    FK IntegrityError 500 或破坏其写入——对照 live 删除守卫。
    """
    ok_flag, msg = _svc_delete_record(db, record_id)
    if not ok_flag:
        status = 409 if (msg and "运行中" in msg) else 404
        return err(status, msg)
    return ok()


@router.post("/records/{record_id}/crisis-alpha/recompute")
def recompute_record_crisis_alpha(record_id: int, db: Session = Depends(get_db)):
    result = _svc_recompute_crisis_alpha(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)


@router.post("/records/{record_id}/market-regime/recompute")
def recompute_record_market_regime(record_id: int, db: Session = Depends(get_db)):
    """离线重算市场状态×绩效分析（机遇/中性Alpha，不重跑回测）。"""
    result = _svc_recompute_market_regime(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)


@router.post("/records/{record_id}/evaluations/recompute")
def recompute_record_evaluation(record_id: int, db: Session = Depends(get_db)):
    """离线重算组合级评估（不重跑回测），供老记录补算基准相对等新增指标。"""
    result = _svc_recompute_evaluation(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)


@router.post("/records/{record_id}/robustness/recompute")
def recompute_record_robustness(record_id: int, db: Session = Depends(get_db)):
    """离线重算稳健性检验（年度/3年滚动/集中度/样本内外，不重跑回测）。"""
    result = _svc_recompute_robustness(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)


@router.post("/robustness/compare")
def compare_pool_robustness(req: PoolCompareRequest, db: Session = Depends(get_db)):
    """换池稳健性对比：同参数仅换池逐个重跑回测并返回对比结果。

    同步内联执行（每池分钟级），需持回测并发锁（与单跑回测互斥）。
    """
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        return err(404, "组合策略不存在")
    if not req.pool_ids:
        return err(400, "请至少选择一个备选股票池")
    if req.start_date >= req.end_date:
        return err(400, f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}")
    if req.dividend_mode not in ("front", "back", "dual"):
        return err(400, f"复权模式仅支持 front/back/dual，收到 {req.dividend_mode!r}")

    if not _try_acquire_slot():
        raise HTTPException(
            status_code=409,
            detail="回测正在进行中，请等待当前回测完成后再启动（已达并发上限）",
        )
    try:
        results = _svc_run_pool_comparison(
            db, req.portfolio_strategy_id, req.pool_ids,
            req.start_date, req.end_date,
            req.name_prefix, req.dividend_mode,
        )
        return ok({"comparisons": results})
    finally:
        _release_slot()


@router.get("/records/{record_id}/export/html")
def export_record_html(record_id: int, db: Session = Depends(get_db)):
    """导出单文件离线 HTML 分析报告（FR-21/22，Task 19）。

    例外于统一 envelope：文件下载直接返回 text/html + Content-Disposition，
    前端以直链/a 标签触发浏览器下载（不经 axios 拦截器）。数字与详情接口同源。
    """
    detail = _svc_get_record_detail(db, record_id)
    if detail is None:
        return err(404, "回测记录不存在")
    report = build_html_report(detail)
    rec = detail.get("record") or {}
    fname = quote(f"回测报告-{rec.get('name') or record_id}.html")
    return HTMLResponse(
        content=report,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


def _validate_backtest_request(req: BacktestRequest) -> Optional[str]:
    """回测请求基础校验，返回错误消息或 None（通过）。
    校验日期区间：start < end，且 start 不在未来（TQ 拉不到未来行情）。
    校验复权模式取值。"""
    if req.start_date >= req.end_date:
        return f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}"
    today = date.today()
    if req.start_date > today:
        return f"开始日期不可在未来，收到 {req.start_date}（今天 {today}）"
    if req.dividend_mode not in ("front", "back", "dual"):
        return f"复权模式仅支持 front/back/dual，收到 {req.dividend_mode!r}"
    # C7：门槛比例须在 [0.1, 2]（<0.1 过松失意义，>2 要求 OOS 两倍于 IS 已失合理性）
    if not (0.1 <= req.oos_gate_min_ratio <= 2):
        return f"IS/OOS 门槛比例须在 [0.1, 2] 区间，收到 {req.oos_gate_min_ratio}"
    # 自适应滑点 run 级覆盖：冲击系数须在 (0,1]
    if req.slippage_impact_coef is not None and not (0 < req.slippage_impact_coef <= 1):
        return f"slippage_impact_coef 须在 (0,1]，收到 {req.slippage_impact_coef}"
    # 成交量约束 run 级覆盖：占比须在 (0,1]
    if req.volume_limit_ratio is not None and not (0 < req.volume_limit_ratio <= 1):
        return f"volume_limit_ratio 须在 (0,1]，收到 {req.volume_limit_ratio}"
    # 容量上限 run 级覆盖：必须 > 0
    if req.equity_cap is not None and req.equity_cap <= 0:
        return f"equity_cap 必须 > 0（None=不设上限/用组合默认），收到 {req.equity_cap}"
    return None


@router.post("")
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    """启动回测（同步内联执行）。并发保护：并发上限由 config.max_concurrent_backtest
    控制（默认 1），达到上限则 409。

    校验（404/400）在抢槽外，只对合法请求抢槽；槽由 finally 保证异常路径也释放。
    主链路在 core.services.backtest_service.run_backtest（P1 #9 阶段 3 迁出）。
    """
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        return err(404, "组合策略不存在")

    err_msg = _validate_backtest_request(req)
    if err_msg:
        return err(400, err_msg)

    if not _try_acquire_slot():
        # 有意保留真实 HTTP 409（非 body-code）：并发契约 + 测试断言 status_code==409
        # （test_post_backtest_409_when_already_running）。body-code 会破坏前端对并发冲突
        # 的 HTTP 状态码判断。与统一 envelope 的模式 A 不同——此为刻意的模式 B 例外。
        raise HTTPException(
            status_code=409,
            detail="回测正在进行中，请等待当前回测完成后再启动（已达并发上限）",
        )
    try:
        return ok(_svc_run_backtest(db, ps, req))
    finally:
        _release_slot()


# ---------------------------------------------------------------------------
# C8 walk-forward 滚动验证
# ---------------------------------------------------------------------------
def _validate_walk_forward_request(req: WalkForwardRequest) -> Optional[str]:
    """WF 请求校验，返回错误消息或 None（通过）。"""
    if req.start_date >= req.end_date:
        return f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}"
    today = date.today()
    if req.start_date > today:
        return f"开始日期不可在未来，收到 {req.start_date}（今天 {today}）"
    if req.dividend_mode not in ("front", "back", "dual"):
        return f"复权模式仅支持 front/back/dual，收到 {req.dividend_mode!r}"
    if req.train_years < 1:
        return f"训练窗口须 ≥ 1 年，收到 {req.train_years}"
    if req.test_years < 1:
        return f"验证窗口须 ≥ 1 年，收到 {req.test_years}"
    if not (0.1 <= req.oos_gate_min_ratio <= 2):
        return f"IS/OOS 门槛比例须在 [0.1, 2] 区间，收到 {req.oos_gate_min_ratio}"
    return None


@router.post("/walk-forward")
def run_walk_forward(req: WalkForwardRequest, db: Session = Depends(get_db)):
    """启动 walk-forward 滚动验证（同步内联，逐窗串行子回测）。

    整轮持一个回测并发槽（与单跑回测/换池对比互斥）；内部逐窗调
    run_backtest（服务层不持锁，锁只在本路由层）。
    """
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        return err(404, "组合策略不存在")
    err_msg = _validate_walk_forward_request(req)
    if err_msg:
        return err(400, err_msg)

    if not _try_acquire_slot():
        raise HTTPException(
            status_code=409,
            detail="回测正在进行中，请等待当前回测完成后再启动（已达并发上限）",
        )
    try:
        return ok(_svc_run_walk_forward(db, ps, req))
    finally:
        _release_slot()


@router.get("/walk-forward")
def list_walk_forward_runs(db: Session = Depends(get_db)):
    return ok(_svc_list_walk_forward_runs(db))


@router.get("/walk-forward/{run_id}")
def get_walk_forward_run(run_id: int, db: Session = Depends(get_db)):
    run = _svc_get_walk_forward_run(db, run_id)
    if run is None:
        return err(404, "walk-forward 运行不存在")
    return ok(run)


@router.delete("/walk-forward/{run_id}")
def delete_walk_forward_run(run_id: int, db: Session = Depends(get_db)):
    """删除 walk-forward 运行 + 其子回测记录（级联清子表）。running → 409。"""
    ok_flag, msg = _svc_delete_walk_forward_run(db, run_id)
    if not ok_flag:
        status = 409 if "运行中" in msg else 404
        return err(status, msg)
    return ok()


# ---------------------------------------------------------------------------
# P2b 参数微扰敏感性分析（可选，默认不使用）
# ---------------------------------------------------------------------------
def _validate_param_perturb_request(req: ParamPerturbRequest) -> Optional[str]:
    """微扰请求校验，返回错误消息或 None（通过）。"""
    if req.start_date >= req.end_date:
        return f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}"
    today = date.today()
    if req.start_date > today:
        return f"开始日期不可在未来，收到 {req.start_date}（今天 {today}）"
    if req.dividend_mode not in ("front", "back", "dual"):
        return f"复权模式仅支持 front/back/dual，收到 {req.dividend_mode!r}"
    # 扰动幅度上限 50%：再大已不是"微"扰，且 int 参数缩放失真严重
    if not (0 < req.perturb_pct <= 0.5):
        return f"扰动幅度须在 (0, 0.5] 区间，收到 {req.perturb_pct}"
    return None


@router.post("/param-perturb")
def run_param_perturb(req: ParamPerturbRequest, db: Session = Depends(get_db)):
    """启动参数微扰敏感性分析（同步内联，逐轮串行子回测）。

    整轮持一个回测并发槽（与单跑回测/walk-forward 互斥）；内部逐轮调
    run_backtest（服务层不持锁，锁只在本路由层）。
    """
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        return err(404, "组合策略不存在")
    err_msg = _validate_param_perturb_request(req)
    if err_msg:
        return err(400, err_msg)

    if not _try_acquire_slot():
        raise HTTPException(
            status_code=409,
            detail="回测正在进行中，请等待当前回测完成后再启动（已达并发上限）",
        )
    try:
        return ok(_svc_run_param_perturb(db, ps, req))
    finally:
        _release_slot()


@router.get("/param-perturb")
def list_param_perturb_runs(db: Session = Depends(get_db)):
    return ok(_svc_list_param_perturb_runs(db))


@router.get("/param-perturb/{run_id}")
def get_param_perturb_run(run_id: int, db: Session = Depends(get_db)):
    run = _svc_get_param_perturb_run(db, run_id)
    if run is None:
        return err(404, "参数微扰运行不存在")
    return ok(run)


@router.delete("/param-perturb/{run_id}")
def delete_param_perturb_run(run_id: int, db: Session = Depends(get_db)):
    """删除参数微扰运行 + 其子回测记录（级联清子表）。running → 409。"""
    ok_flag, msg = _svc_delete_param_perturb_run(db, run_id)
    if not ok_flag:
        status = 409 if "运行中" in msg else 404
        return err(status, msg)
    return ok()
