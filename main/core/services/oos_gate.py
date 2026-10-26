"""C7 IS/OOS 采纳门槛（验证纪律层，默认关）。

新参数入库纪律：OOS CAGR ≥ min_ratio × IS CAGR，且 OOS 区间收益不跑输基准，
否则标记 rejected（记录保留，仅不被采纳——审计友好）。

输入为稳健性检验 payload 的 oos 块（backtest_robustness.analyze_robustness 产出）：
    {"split_year", "in_sample": {cagr, return, benchmark_return, ...},
     "out_of_sample": {...}}
输出 {"status": "passed" | "rejected" | None, "reasons": [str]}：
- None：数据不足未评估（oos 缺失或 cagr 缺失），不算通过也不算拒绝；
- reasons 为人读原因列表（rejected 时逐条列出未过子项；None 时说明为何未评估）。

纯函数，可单测；服务层 run_backtest 在 safe_compute_robustness 之后调用并落库。
"""
from typing import Optional


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def evaluate_oos_gate(oos: Optional[dict], min_ratio: float = 0.5) -> dict:
    """评估 IS/OOS 门槛。oos 为 None 或结构缺失时返回 status=None。

    W5-O4b：样本内年化 ≤0 → 直接 rejected（样本内收益为负无采纳意义）；
    基准缺失 → status=None（未评估），不再静默跳过通过。
    """
    if not oos:
        return {"status": None,
                "reasons": ["样本外数据不足（回测需跨越至少 2 个日历年），未评估"]}
    in_seg = oos.get("in_sample") or {}
    out_seg = oos.get("out_of_sample") or {}
    in_cagr = in_seg.get("cagr")
    out_cagr = out_seg.get("cagr")
    if in_cagr is None or out_cagr is None:
        return {"status": None, "reasons": ["样本内/外年化收益缺失，未评估"]}

    # W5-O4b：样本内年化 ≤0 → 不采纳（无论 OOS 如何，无参照意义）
    if in_cagr <= 0:
        return {"status": "rejected", "reasons": ["样本内年化为负，不采纳"]}

    reasons = []
    # 子项 1：OOS CAGR ≥ min_ratio × IS CAGR
    threshold = min_ratio * in_cagr
    if out_cagr < threshold:
        reasons.append(
            f"OOS 年化 {_pct(out_cagr)} < {_pct(min_ratio)} × "
            f"IS 年化 {_pct(in_cagr)}（门槛 {_pct(threshold)}）"
        )
    # 子项 2：OOS 不跑输基准。基准缺失 → 无法验证，未评估（不算通过）。
    out_ret = out_seg.get("return")
    bench = out_seg.get("benchmark_return")
    if bench is None:
        return {"status": None, "reasons": ["基准数据缺失，无法验证 OOS 相对表现，未评估"]}
    if out_ret is not None and out_ret < bench:
        reasons.append(f"OOS 收益 {_pct(out_ret)} 跑输基准 {_pct(bench)}")

    return {"status": "rejected" if reasons else "passed", "reasons": reasons}
