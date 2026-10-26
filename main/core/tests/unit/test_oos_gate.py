"""C7 IS/OOS 门槛纯函数单测。

新参数采纳纪律：OOS CAGR ≥ min_ratio × IS CAGR 且 OOS 不跑输基准，否则 rejected。
默认关（is_oos_gate_enabled=False 时完全不评估）；数据不足 → status=None（未评估），
不算通过也不算拒绝。
"""
from core.services.oos_gate import evaluate_oos_gate


def _oos(in_cagr, out_cagr, out_return=0.30, bench=0.10):
    return {
        "split_year": "2024",
        "in_sample": {"cagr": in_cagr, "return": 0.80, "benchmark_return": 0.20},
        "out_of_sample": {"cagr": out_cagr, "return": out_return, "benchmark_return": bench},
    }


class TestEvaluateOosGate:
    def test_healthy_passes(self):
        # IS 24.8% / OOS 16.6% ≥ 50%×24.8%=12.4%，且 OOS 收益跑赢基准
        v = evaluate_oos_gate(_oos(0.248, 0.166), 0.5)
        assert v["status"] == "passed"
        assert v["reasons"] == []

    def test_oos_collapse_rejected(self):
        # #1 案例：IS 24.8% → OOS 6.6% < 12.4% → 拒绝
        v = evaluate_oos_gate(_oos(0.248, 0.066), 0.5)
        assert v["status"] == "rejected"
        assert len(v["reasons"]) == 1
        assert "6.6%" in v["reasons"][0] and "24.8%" in v["reasons"][0]

    def test_underperform_benchmark_rejected(self):
        # CAGR 门槛过了，但 OOS 区间收益跑输基准 → 拒绝
        v = evaluate_oos_gate(_oos(0.248, 0.20, out_return=0.05, bench=0.10), 0.5)
        assert v["status"] == "rejected"
        assert any("跑输基准" in r for r in v["reasons"])

    def test_both_failures_two_reasons(self):
        v = evaluate_oos_gate(_oos(0.248, 0.066, out_return=0.05, bench=0.10), 0.5)
        assert v["status"] == "rejected"
        assert len(v["reasons"]) == 2

    def test_benchmark_missing_not_evaluated(self):
        # W5-O4b：基准缺失 → 无法验证相对表现，未评估（不再静默通过）
        v = evaluate_oos_gate(_oos(0.248, 0.20, bench=None), 0.5)
        assert v["status"] is None
        assert any("基准数据缺失" in r for r in v["reasons"])

    def test_oos_none_not_evaluated(self):
        v = evaluate_oos_gate(None, 0.5)
        assert v["status"] is None
        assert v["reasons"]  # 人读原因：数据不足

    def test_cagr_missing_not_evaluated(self):
        v = evaluate_oos_gate(_oos(None, 0.10), 0.5)
        assert v["status"] is None
        v2 = evaluate_oos_gate(_oos(0.10, None), 0.5)
        assert v2["status"] is None

    def test_non_positive_is_cagr_rejected(self):
        # W5-O4b：样本内年化 ≤0 → 直接 rejected（无论 OOS 如何）
        v = evaluate_oos_gate(_oos(-0.10, -0.03), 0.5)
        assert v["status"] == "rejected"
        assert any("样本内年化为负" in r for r in v["reasons"])
        assert evaluate_oos_gate(_oos(0.0, 0.20), 0.5)["status"] == "rejected"

    def test_custom_ratio(self):
        # 30% 门槛：50%×30% = 15%... 用 0.3×24.8%=7.44%，OOS 8% → 过
        assert evaluate_oos_gate(_oos(0.248, 0.08), 0.3)["status"] == "passed"
        # 80% 门槛：0.8×24.8%=19.84%，OOS 16.6% → 拒
        assert evaluate_oos_gate(_oos(0.248, 0.166), 0.8)["status"] == "rejected"
