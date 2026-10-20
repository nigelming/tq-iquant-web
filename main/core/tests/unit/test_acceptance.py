# -*- coding: utf-8 -*-
"""B6 考核口径验收计算单测（compute_acceptance）。

absolute（默认）：年化 ≥8% / 回撤 ≤20% / 卡玛 ≥1.0；
relative：IR ≥0.5 / 超额年化 ≥0 / 下行捕获 ≤100%。
overall：有不过 → False；无不过但有缺失 → None（数据不足）；全过 → True。
"""
from core.services.acceptance import ACCEPTANCE_PROFILES, compute_acceptance


def test_absolute_all_pass():
    evals = {"annual_return": 0.12, "max_drawdown": 0.10, "calmar_ratio": 1.5}
    res = compute_acceptance(evals, "absolute")
    assert res["pass"] is True
    assert res["profile"] == "absolute"
    assert all(c["pass"] for c in res["checks"])
    assert {c["metric"] for c in res["checks"]} == {
        "annual_return", "max_drawdown", "calmar_ratio",
    }


def test_absolute_fail_on_drawdown():
    evals = {"annual_return": 0.12, "max_drawdown": 0.25, "calmar_ratio": 1.5}
    res = compute_acceptance(evals, "absolute")
    assert res["pass"] is False
    dd = next(c for c in res["checks"] if c["metric"] == "max_drawdown")
    assert dd["pass"] is False
    assert dd["value"] == 0.25


def test_missing_metric_yields_none():
    """有缺失指标且无不过项 → 数据不足 None。"""
    res = compute_acceptance({"annual_return": 0.12}, "absolute")
    assert res["pass"] is None
    missing = next(c for c in res["checks"] if c["metric"] == "max_drawdown")
    assert missing["pass"] is None
    assert missing["value"] is None


def test_missing_plus_fail_yields_false():
    """不过项优先于缺失项 → False。"""
    res = compute_acceptance({"annual_return": 0.05}, "absolute")
    assert res["pass"] is False


def test_relative_profile():
    evals = {
        "information_ratio": 0.6,
        "excess_annual_return": 0.02,
        "down_capture_ratio": 0.5,
    }
    res = compute_acceptance(evals, "relative")
    assert res["pass"] is True
    assert res["profile"] == "relative"


def test_relative_down_capture_boundary():
    """下行捕获率 ≤1.0 边界值通过。"""
    evals = {
        "information_ratio": 0.5,
        "excess_annual_return": 0.0,
        "down_capture_ratio": 1.0,
    }
    assert compute_acceptance(evals, "relative")["pass"] is True
    evals["down_capture_ratio"] = 1.01
    assert compute_acceptance(evals, "relative")["pass"] is False


def test_unknown_profile_falls_back_absolute():
    evals = {"annual_return": 0.12, "max_drawdown": 0.1, "calmar_ratio": 1.2}
    res = compute_acceptance(evals, "weird")
    assert res["profile"] == "absolute"
    assert res["pass"] is True


def test_none_evals_yields_none():
    res = compute_acceptance(None, "absolute")
    assert res["pass"] is None
    assert len(res["checks"]) == len(ACCEPTANCE_PROFILES["absolute"])


def test_string_decimal_values_coerced():
    """序列化字符串/Decimal 值自动转 float。"""
    evals = {"annual_return": "0.12", "max_drawdown": "0.10", "calmar_ratio": "1.5"}
    assert compute_acceptance(evals, "absolute")["pass"] is True


def test_checks_carry_chinese_labels():
    res = compute_acceptance({}, "absolute")
    labels = {c["label"] for c in res["checks"]}
    assert any("年化" in lb for lb in labels)
    assert any("回撤" in lb for lb in labels)
