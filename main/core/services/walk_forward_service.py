"""C8 walk-forward 滚动验证（验证纪律层，新功能）。

滚动 train_years 年训练 + test_years 年验证逐段前移，替代一次性 75/25 切分，
更能暴露参数时效性。每窗：
1. 调现有 run_backtest 跑窗口子回测（backtest_records.walk_forward_run_id 关联，
   列表接口默认过滤）；
2. safe_compute_robustness(oos_ratio=test/(train+test)) 切该窗 IS/OOS；
3. 对该窗 oos 块过 C7 门槛（evaluate_oos_gate）。

summary_json：{"windows": [{index, train, test, record_id, is_cagr, oos_cagr,
oos_return, oos_benchmark_return, gate, error?}, ...],
"windows_passed": "k/n", "windows_failed": n, "windows_errored": n,
"overall_pass": 全窗 passed 才 True}。

并发：路由层持回测并发槽一次（整轮 WF 互斥单跑回测）；内部逐窗串行调
run_backtest（服务层本身不持锁，锁在 API 路由层）。
"""
import json
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import List, Optional

from sqlalchemy.orm import Session

from core.models import BacktestRecord, PortfolioStrategy, WalkForwardRun
from core.services.backtest_service import run_backtest, safe_compute_robustness
from core.services.oos_gate import evaluate_oos_gate

logger = logging.getLogger(__name__)


def _add_years(d: date, years: int) -> date:
    """按日历年加 n 年；2 月 29 日落到非闰年时回退 28 日。"""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def build_walk_forward_windows(
    start: date, end: date, train_years: int, test_years: int,
) -> List[dict]:
    """生成滚动窗口：[start + i*test_years 年, +train+test 年)，右端 ≤ end 才收。

    每窗 {index(1 起), start, end, train: {start, end}, test: {start, end}}；
    train/test 切分点 = 窗口起点 + train_years 年。不足一个完整窗口返回 []。
    """
    windows: List[dict] = []
    i = 0
    while True:
        w_start = _add_years(start, i * test_years)
        w_end = _add_years(w_start, train_years + test_years)
        if w_end > end:
            break
        split = _add_years(w_start, train_years)
        windows.append({
            "index": i + 1,
            "start": w_start,
            "end": w_end,
            "train": {"start": w_start, "end": split},
            "test": {"start": split, "end": w_end},
        })
        i += 1
    return windows


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _segment_payload(oos: Optional[dict], key: str) -> dict:
    return (oos or {}).get(key) or {}


def run_walk_forward(db: Session, ps: PortfolioStrategy, req) -> dict:
    """walk-forward 主链路：逐窗跑子回测 + IS/OOS 门槛，落 run 与 summary。

    前置：路由层已保证 ps 非 None、参数合法、已持回测并发槽。
    req 需含：name/start_date/end_date/train_years/test_years/
              oos_gate_min_ratio/dividend_mode/evaluation_profile。
    返回纯 dict（路由层统一 ok()）：{run_id, status, windows_passed,
    overall_pass, error}。
    """
    run = WalkForwardRun(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        train_years=req.train_years,
        test_years=req.test_years,
        oos_gate_min_ratio=req.oos_gate_min_ratio,
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    windows = build_walk_forward_windows(
        req.start_date, req.end_date, req.train_years, req.test_years,
    )
    if not windows:
        run.status = "failed"
        run.error_message = (
            f"区间不足一个完整窗口（需 ≥ {req.train_years + req.test_years} 年："
            f"train {req.train_years} 年 + test {req.test_years} 年），"
            f"收到 {req.start_date} ~ {req.end_date}"
        )
        run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return {"run_id": run.id, "status": "failed",
                "windows_passed": "0/0", "overall_pass": False,
                "error": run.error_message}

    min_ratio = float(req.oos_gate_min_ratio)
    oos_ratio = req.test_years / (req.train_years + req.test_years)
    window_summaries: List[dict] = []
    passed = 0

    for w in windows:
        entry = {
            "index": w["index"],
            "train": {"start": _iso(w["train"]["start"]), "end": _iso(w["train"]["end"])},
            "test": {"start": _iso(w["test"]["start"]), "end": _iso(w["test"]["end"])},
            "record_id": None,
            "is_cagr": None, "oos_cagr": None,
            "oos_return": None, "oos_benchmark_return": None,
            "gate": {"status": None, "reasons": []},
        }
        child_req = SimpleNamespace(
            name=f"{req.name}#w{w['index']}",
            start_date=w["start"],
            end_date=w["end"],
            dividend_mode=req.dividend_mode,
            evaluation_profile=req.evaluation_profile,
            # 子回测不重复过 C7 门槛：WF 在此统一按窗评估
            is_oos_gate_enabled=False,
        )
        try:
            result = run_backtest(db, ps, child_req)
            child_id = result["record_id"]
            child = db.get(BacktestRecord, child_id)
            if child is not None:
                child.walk_forward_run_id = run.id
                db.commit()
            entry["record_id"] = child_id

            rb_row = safe_compute_robustness(db, child_id, oos_ratio=oos_ratio)
            payload = (
                json.loads(rb_row.payload_json)
                if rb_row and rb_row.payload_json else {}
            )
            oos = payload.get("oos")
            in_seg = _segment_payload(oos, "in_sample")
            out_seg = _segment_payload(oos, "out_of_sample")
            entry["is_cagr"] = in_seg.get("cagr")
            entry["oos_cagr"] = out_seg.get("cagr")
            entry["oos_return"] = out_seg.get("return")
            entry["oos_benchmark_return"] = out_seg.get("benchmark_return")
            entry["gate"] = evaluate_oos_gate(oos, min_ratio)
        except Exception as exc:  # 单窗失败不拖垮整轮：记录错误继续下一窗
            db.rollback()
            logger.exception("[WF#%s] 窗口 %s 子回测失败", run.id, w["index"])
            entry["error"] = str(exc) or repr(exc)
        window_summaries.append(entry)
        if entry["gate"]["status"] == "passed":
            passed += 1

    n = len(window_summaries)
    overall_pass = passed == n and n > 0
    # W5-L5：暴露失败/出错窗口计数，避免"k/n 通过"掩盖个别窗口异常。
    n_failed = sum(1 for e in window_summaries if e["gate"]["status"] == "failed")
    n_errored = sum(1 for e in window_summaries if e.get("error"))
    run.status = "completed"
    run.summary_json = json.dumps({
        "windows": window_summaries,
        "windows_passed": f"{passed}/{n}",
        "windows_failed": n_failed,
        "windows_errored": n_errored,
        "overall_pass": overall_pass,
    }, ensure_ascii=False)
    run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    return {
        "run_id": run.id,
        "status": "completed",
        "windows_passed": f"{passed}/{n}",
        "overall_pass": overall_pass,
        "error": None,
    }


def _serialize_run(r: WalkForwardRun) -> dict:
    return {
        "id": r.id,
        "portfolio_strategy_id": r.portfolio_strategy_id,
        "name": r.name,
        "start_date": _iso(r.start_date),
        "end_date": _iso(r.end_date),
        "train_years": r.train_years,
        "test_years": r.test_years,
        "oos_gate_min_ratio": float(r.oos_gate_min_ratio) if r.oos_gate_min_ratio is not None else None,
        "status": r.status,
        "error_message": r.error_message,
        "summary": json.loads(r.summary_json) if r.summary_json else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


def list_walk_forward_runs(db: Session) -> list:
    """列出全部 walk-forward 运行（按创建时间倒序）。"""
    runs = db.query(WalkForwardRun).order_by(WalkForwardRun.created_at.desc()).all()
    return [_serialize_run(r) for r in runs]


def get_walk_forward_run(db: Session, run_id: int) -> Optional[dict]:
    """单个 walk-forward 运行详情；不存在返回 None（路由层 404）。"""
    run = db.get(WalkForwardRun, run_id)
    if run is None:
        return None
    return _serialize_run(run)


def delete_walk_forward_run(db: Session, run_id: int):
    """删除 walk-forward 运行 + 其子回测记录（级联清快照/成交/评估/决策）。

    返回 (ok, error_message)：不存在 → (False, "...不存在")；
    running → (False, "...运行中...")；成功 → (True, None)。
    子回测先经 backtest_service.delete_record 级联清理，再删 run 本身。
    """
    from core.services.backtest_service import delete_record
    run = db.get(WalkForwardRun, run_id)
    if run is None:
        return False, "walk-forward 运行不存在"
    if run.status == "running":
        return False, "walk-forward 运行中，请先停止再删除"
    children = db.query(BacktestRecord).filter_by(walk_forward_run_id=run_id).all()
    for child in children:
        delete_record(db, child.id)
    db.delete(run)
    db.commit()
    return True, None
