"""B6 考核口径验收计算（默认 absolute，不改变任何回测行为）。

策略定位决定考核口径：
- absolute（绝对收益型，默认）：年化收益 / 最大回撤 / 卡玛比率。
- relative（相对收益型）：信息比率 / 超额年化 / 下行捕获率。

compute_acceptance 输入评估指标 dict（backtest_evaluations 序列化值），
输出验收结论：pass=True 全过 / False 有不过 / None 数据不足（任一指标缺失
且无不过项）。该模块只做口径判定，不回写指标、不影响回测执行。
"""
from typing import Dict, List, Optional

# 口径 → [(指标键, 比较符, 阈值, 中文标签)]
ACCEPTANCE_PROFILES: Dict[str, List[tuple]] = {
    "absolute": [
        ("annual_return", ">=", 0.08, "年化收益率 ≥ 8%"),
        ("max_drawdown", "<=", 0.20, "最大回撤 ≤ 20%"),
        ("calmar_ratio", ">=", 1.0, "卡玛比率 ≥ 1.0"),
    ],
    "relative": [
        ("information_ratio", ">=", 0.5, "信息比率 ≥ 0.5"),
        ("excess_annual_return", ">=", 0.0, "超额年化收益 ≥ 0"),
        ("down_capture_ratio", "<=", 1.0, "下行捕获率 ≤ 100%"),
    ],
}

_OPS = {
    ">=": lambda v, t: v >= t,
    "<=": lambda v, t: v <= t,
}


def compute_acceptance(evals: Optional[dict], profile: str) -> dict:
    """按口径计算验收结论。

    :param evals: 评估指标 dict（值可为 None/str(Decimal)，自动转 float）。
    :param profile: "absolute" | "relative"（未知值回落 absolute）。
    :returns: {"profile", "pass": True/False/None, "checks": [
        {"metric", "op", "threshold", "value", "pass", "label"}]}
        单指标 value 为 None（数据不足）→ 该 check pass=None；
        overall：有 False → False，否则有 None → None，否则 True。
    """
    checks_conf = ACCEPTANCE_PROFILES.get(profile) or ACCEPTANCE_PROFILES["absolute"]
    evals = evals or {}
    checks = []
    has_fail = False
    has_unknown = False
    for metric, op, threshold, label in checks_conf:
        raw = evals.get(metric)
        value: Optional[float] = None
        if raw is not None:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = None
        passed: Optional[bool] = None
        if value is not None:
            passed = _OPS[op](value, threshold)
        if passed is False:
            has_fail = True
        elif passed is None:
            has_unknown = True
        checks.append({
            "metric": metric,
            "op": op,
            "threshold": threshold,
            "value": value,
            "pass": passed,
            "label": label,
        })
    overall: Optional[bool]
    if has_fail:
        overall = False
    elif has_unknown:
        overall = None
    else:
        overall = True
    return {"profile": profile if profile in ACCEPTANCE_PROFILES else "absolute",
            "pass": overall, "checks": checks}
