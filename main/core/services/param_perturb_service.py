"""P2b 参数微扰敏感性分析（验证纪律层，新功能，默认不使用）。

对组合下各子策略的数值参数逐一做 ±perturb_pct 扰动（乘性因子，各策略
按自身基准值缩放），每参数低/高方向各跑一轮子回测，外加 1 轮无扰动
基准；子回测经 backtest_records.param_perturb_run_id 关联（列表接口
默认过滤）。扰动值只作用于内存装配（run_backtest param_overrides），
不回写 strategies 表。

summary_json：{"baseline": {...}, "rounds": [{param, direction, factor,
record_id, status, annual_return, ..., error?}, ...],
"params": [{param, low: {...}|None, high: {...}|None, delta_cagr,
sensitivity, asymmetric}],
"most_sensitive": 敏感度（每单位扰动幅度 CAGR 变化）最大的参数名}。

并发：路由层持回测并发槽一次（整轮微扰互斥单跑回测）；内部逐轮串行调
run_backtest（服务层本身不持锁，锁在 API 路由层）。
"""
import json
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import List, Optional

from sqlalchemy.orm import Session

from core.models import (
    BacktestEvaluation, BacktestRecord, ParamPerturbRun, PortfolioStrategy, Strategy,
)
from core.services.backtest_service import clamp_perturbed_value, run_backtest

logger = logging.getLogger(__name__)

# 参与微扰的子策略层数值参数：(字段名, 是否整型)。
# rotation_*/trend_* 等条件开关参数不在其列（默认关闭，扰动无意义）；
# 组合层参数（max_drawdown 等）不扰动——微扰定位为策略参数体检。
PERTURB_PARAM_FIELDS = (
    ("capital_ratio", False),
    ("max_positions", True),
    ("single_open_ratio", False),
    ("stop_loss_ratio", False),
    ("take_profit_ratio", False),
    ("trailing_stop_ratio", False),
    ("add_position_threshold", False),
    ("max_add_count", True),
    ("add_position_ratio", False),
    ("reduce_position_ratio", False),
)

# 落 summary 的单轮指标（从 BacktestEvaluation 读）
_ROUND_METRIC_FIELDS = (
    "annual_return", "total_return", "max_drawdown",
    "win_rate", "profit_factor", "sharpe_ratio", "total_trades",
)


def scaled_value(base, factor: float, is_int: bool, field: Optional[str] = None):
    """基准值 × 因子。整型按因子方向 floor/ceil 且 ≥1；结果等于基准、
    基准为 None 或（非整型）基准为 0 → 返回 None（该方向不可扰动）。
    W5-O1：含特殊语义 -1（如 add_position_threshold = -1）→ 不可扰动。
    W5-O1b(M4)：field 传入时按 PERTURB_BOUNDS 钳制；钳制后仍等于基准
    （如 capital_ratio=1.0 再放大）→ None（该方向无实际扰动，丢弃）。
    """
    if base is None:
        return None
    try:
        b_dec = Decimal(str(base))
    except Exception:
        b_dec = Decimal(str(base))
    # -1 语义字段不可扰动（跳过加仓回撤检查等特殊模式）
    if b_dec == Decimal("-1"):
        return None
    if is_int:
        b = int(base)
        raw = b * factor
        v = math.floor(raw) if factor < 1 else math.ceil(raw)
        v = max(1, v)
        return v if v != b else None
    b = float(base)
    if b == 0:
        return None
    v = b * factor
    if field is not None:
        v = clamp_perturbed_value(field, v)
    return v if v != b else None


def build_perturb_rounds(strategies: List, pct: float) -> List[dict]:
    """按 PERTURB_PARAM_FIELDS 生成扰动轮次：[{param, direction, factor}]。

    某参数的某方向仅当至少一个子策略可扰动（scaled_value 非 None）才收。
    """
    rounds: List[dict] = []
    for field, is_int in PERTURB_PARAM_FIELDS:
        for direction, factor in (("low", 1 - pct), ("high", 1 + pct)):
            if any(
                scaled_value(getattr(s, field, None), factor, is_int, field) is not None
                for s in strategies
            ):
                rounds.append({"param": field, "direction": direction, "factor": factor})
    return rounds


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _run_one(db: Session, ps: PortfolioStrategy, req, run: ParamPerturbRun,
             overrides: Optional[dict], tag: str) -> dict:
    """跑一轮子回测并采集评估指标；失败记 error 不拖垮整轮。"""
    child_req = SimpleNamespace(
        name=f"{req.name}#{tag}",
        start_date=req.start_date,
        end_date=req.end_date,
        dividend_mode=req.dividend_mode,
        evaluation_profile=req.evaluation_profile,
        # 微扰子回测不过 C7 门槛（非参数采纳场景）
        is_oos_gate_enabled=False,
    )
    entry = {"record_id": None, "status": None, "error": None}
    try:
        result = run_backtest(db, ps, child_req, param_overrides=overrides)
        child_id = result["record_id"]
        child = db.get(BacktestRecord, child_id)
        if child is not None:
            child.param_perturb_run_id = run.id
            db.commit()
            entry["status"] = child.status
        entry["record_id"] = child_id
        ev = (
            db.query(BacktestEvaluation)
            .filter_by(backtest_record_id=child_id)
            .first()
        )
        for f in _ROUND_METRIC_FIELDS:
            v = getattr(ev, f, None) if ev is not None else None
            entry[f] = int(v) if f == "total_trades" and v is not None else _f(v)
    except Exception as exc:  # 单轮失败不拖垮整轮：记录错误继续下一轮
        db.rollback()
        logger.exception("[PP#%s] 轮次 %s 子回测失败", run.id, tag)
        entry["error"] = str(exc) or repr(exc)
    return entry


def run_param_perturb(db: Session, ps: PortfolioStrategy, req) -> dict:
    """参数微扰主链路：基准轮 + 逐参数 ±pct 轮，落 run 与 summary。

    前置：路由层已保证 ps 非 None、参数合法、已持回测并发槽。
    req 需含：name/start_date/end_date/perturb_pct/dividend_mode/
              evaluation_profile。
    返回纯 dict（路由层统一 ok()）：{run_id, status, rounds_run,
    most_sensitive, error}。
    """
    run = ParamPerturbRun(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        perturb_pct=Decimal(str(req.perturb_pct)),
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    strategies = db.query(Strategy).filter_by(portfolio_id=ps.id).order_by(Strategy.id).all()
    pct = float(req.perturb_pct)
    rounds = build_perturb_rounds(strategies, pct)

    baseline = _run_one(db, ps, req, run, overrides=None, tag="base")
    round_entries: List[dict] = []
    for r in rounds:
        entry = _run_one(
            db, ps, req, run,
            overrides={r["param"]: r["factor"]},
            tag=f"{r['param']}_{r['direction']}",
        )
        entry.update({"param": r["param"], "direction": r["direction"],
                      "factor": r["factor"]})
        round_entries.append(entry)

    # 按参数聚合：delta_cagr = 高方向 CAGR − 低方向 CAGR（按实际扰动幅度归一化）。
    # W5-M3：单方向可扰动参数也计入 most_sensitive（delta_cagr 置 None、标 asymmetric），
    # 敏感度用「每单位扰动幅度」的 CAGR 变化归一化，避免幅度不同的参数不可比。
    baseline_cagr = baseline.get("annual_return")
    param_summaries: List[dict] = []
    for field, _ in PERTURB_PARAM_FIELDS:
        low = next((e for e in round_entries
                    if e["param"] == field and e["direction"] == "low"), None)
        high = next((e for e in round_entries
                     if e["param"] == field and e["direction"] == "high"), None)
        if low is None and high is None:
            continue
        delta = None
        sensitivity = None
        asymmetric = False
        if (low is not None and high is not None
                and low.get("annual_return") is not None
                and high.get("annual_return") is not None):
            delta = high["annual_return"] - low["annual_return"]
            if pct > 0:
                sensitivity = abs(delta) / (2 * pct)
        else:
            asymmetric = True
            present = low if low is not None else high
            if (present is not None and present.get("annual_return") is not None
                    and baseline_cagr is not None and pct > 0):
                sensitivity = abs(present["annual_return"] - baseline_cagr) / pct
        param_summaries.append({
            "param": field, "low": low, "high": high, "delta_cagr": delta,
            "sensitivity": sensitivity, "asymmetric": asymmetric,
        })

    most_sensitive = None
    ranked = [p for p in param_summaries if p.get("sensitivity") is not None]
    if ranked:
        most_sensitive = max(ranked, key=lambda p: p["sensitivity"])["param"]

    run.status = "completed"
    run.summary_json = json.dumps({
        "baseline": baseline,
        "rounds": round_entries,
        "params": param_summaries,
        "most_sensitive": most_sensitive,
    }, ensure_ascii=False)
    run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    n_ok = sum(1 for e in round_entries if e.get("status") == "completed")
    return {
        "run_id": run.id,
        "status": "completed",
        "rounds_run": f"{n_ok}/{len(round_entries)}",
        "most_sensitive": most_sensitive,
        "error": None,
    }


def _serialize_run(r: ParamPerturbRun) -> dict:
    return {
        "id": r.id,
        "portfolio_strategy_id": r.portfolio_strategy_id,
        "name": r.name,
        "start_date": _iso(r.start_date),
        "end_date": _iso(r.end_date),
        "perturb_pct": float(r.perturb_pct) if r.perturb_pct is not None else None,
        "status": r.status,
        "error_message": r.error_message,
        "summary": json.loads(r.summary_json) if r.summary_json else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


def list_param_perturb_runs(db: Session) -> list:
    """列出全部参数微扰运行（按创建时间倒序）。"""
    runs = db.query(ParamPerturbRun).order_by(ParamPerturbRun.created_at.desc()).all()
    return [_serialize_run(r) for r in runs]


def get_param_perturb_run(db: Session, run_id: int) -> Optional[dict]:
    """单个参数微扰运行详情（不存在返回 None，路由转 404）。"""
    r = db.get(ParamPerturbRun, run_id)
    if r is None:
        return None
    return _serialize_run(r)



def delete_param_perturb_run(db: Session, run_id: int):
    """删除参数微扰运行 + 其子回测记录（级联清快照/成交/评估/决策）。

    返回 (ok, error_message)：不存在 → (False, "...不存在")；
    running → (False, "...运行中...")；成功 → (True, None)。
    子回测先经 backtest_service.delete_record 级联清理，再删 run 本身。
    """
    from core.services.backtest_service import delete_record
    run = db.get(ParamPerturbRun, run_id)
    if run is None:
        return False, "参数微扰运行不存在"
    if run.status == "running":
        return False, "参数微扰运行中，请先停止再删除"
    children = db.query(BacktestRecord).filter_by(param_perturb_run_id=run_id).all()
    for child in children:
        delete_record(db, child.id)
    db.delete(run)
    db.commit()
    return True, None
