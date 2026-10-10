# -*- coding: utf-8 -*-
"""Task 16 行业归因纯计算引擎单测（TR-16.1 / TR-16.2）"""
from datetime import date, timedelta

import pandas as pd

from core.engine.industry_attribution import (
    compute_industry_attribution, INDUSTRY_TOP_N,
)


def _trading_days(start: date, n: int):
    """连续 n 个自然日（合成数据用，跳过周末近似交易日）。"""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _price_series(days, daily_rets):
    """按日收益序列生成收盘价（起点 1000）。"""
    price = 1000.0
    vals = []
    for r in daily_rets:
        price *= (1 + r)
        vals.append(price)
    return pd.Series(vals, index=pd.DatetimeIndex(days), dtype=float)


def _holdings_daily(days, total=100.0, positions=None):
    return [
        {"snap_date": d, "total_value": total,
         "positions": positions(d) if callable(positions) else positions}
        for d in days
    ]


def test_tr16_1_exposure_weights_and_hit_rate_direction():
    days = _trading_days(date(2024, 1, 1), 28)  # 约 4 个周（W-FRI）
    n = len(days)
    # 组合始终满仓持有 S_A（行业 IA）
    daily = _holdings_daily(
        days, total=100.0,
        positions=[{"stock_code": "000001.SZ", "market_value": 100.0}])
    stock_industry = {"000001.SZ": {"code": "881001.SH", "name": "测试行业A"}}
    names = {"881001.SH": "测试行业A", "881002.SH": "测试行业B",
             "881003.SH": "测试行业C"}
    # A 每天 +0.8%（周涨幅约 +4%），B/C 每天 -0.1%
    closes = {
        "881001.SH": _price_series(days, [0.008] * n),
        "881002.SH": _price_series(days, [-0.001] * n),
        "881003.SH": _price_series(days, [-0.002] * n),
    }

    out = compute_industry_attribution(daily, stock_industry, names, closes)

    assert out["status"] == "ok"
    # --- 暴露权重和 = 总仓位（容差）---
    exp = out["exposure"]
    ia = next(s for s in exp["series"] if s["code"] == "881001.SH")
    assert len(exp["dates"]) == n
    for w in ia["weights"]:
        assert abs(w - 1.0) < 1e-9
    for c in exp["cash"]:
        assert abs(c - 0.0) < 1e-9

    # --- 命中率：领涨行业 A 恰为持仓行业 → 命中 ---
    wk = out["hit_rate"]["weekly"]
    assert wk["top_n"] == INDUSTRY_TOP_N
    assert wk["periods_holding"] >= 3
    assert wk["hits"] == wk["periods_holding"]
    assert wk["hit_rate"] == 1.0
    assert wk["details"][0]["leaders"][0] == "测试行业A"

    # --- 贡献：A 行业正贡献 ---
    contrib = out["contribution"]
    assert contrib["available"] is True
    row_a = next(r for r in contrib["rows"] if r["code"] == "881001.SH")
    assert row_a["contribution"] > 0
    assert row_a["index_return"] > 0
    assert abs(row_a["avg_weight"] - 1.0) < 1e-9
    assert abs(contrib["total_contribution"] - row_a["contribution"]) < 1e-12


def test_tr16_1_hit_rate_zero_when_holding_laggard():
    days = _trading_days(date(2024, 1, 1), 28)
    n = len(days)
    daily = _holdings_daily(
        days, total=100.0,
        positions=[{"stock_code": "000002.SZ", "market_value": 100.0}])
    stock_industry = {"000002.SZ": {"code": "881002.SH", "name": "测试行业B"}}
    names = {"881001.SH": "测试行业A", "881002.SH": "测试行业B",
             "881003.SH": "测试行业C", "881004.SH": "测试行业D"}
    # 持仓行业 B 下跌，A/C/D 上涨 → B 排在 Top3 之外
    closes = {
        "881001.SH": _price_series(days, [0.008] * n),
        "881002.SH": _price_series(days, [-0.005] * n),
        "881003.SH": _price_series(days, [0.006] * n),
        "881004.SH": _price_series(days, [0.004] * n),
    }
    out = compute_industry_attribution(daily, stock_industry, names, closes)
    wk = out["hit_rate"]["weekly"]
    assert wk["periods_holding"] >= 3
    assert wk["hits"] == 0
    assert wk["hit_rate"] == 0.0
    row_b = next(r for r in out["contribution"]["rows"]
                 if r["code"] == "881002.SH")
    assert row_b["contribution"] < 0


def test_tr16_1_unmapped_holdings_tracked_separately():
    days = _trading_days(date(2024, 1, 1), 10)
    daily = _holdings_daily(
        days, total=100.0,
        positions=[{"stock_code": "999999.XX", "market_value": 60.0}])
    out = compute_industry_attribution(
        daily, {"999999.XX": None}, {}, {})
    # 映射缺失不报错：未分类权重 0.6，现金 0.4
    un = next(s for s in out["exposure"]["series"] if s["code"] == "UNMAPPED")
    assert all(abs(w - 0.6) < 1e-9 for w in un["weights"])
    assert all(abs(c - 0.4) < 1e-9 for c in out["exposure"]["cash"])
    assert "999999.XX" in out["unmapped_codes"]
    assert out["contribution"]["available"] is False


def test_tr16_2_positions_missing_means_needs_rerun():
    days = _trading_days(date(2024, 1, 1), 10)
    # 老记录：positions 键为 None
    daily = [{"snap_date": d, "total_value": 100.0, "positions": None}
             for d in days]
    out = compute_industry_attribution(daily, {}, {}, {})
    assert out["status"] == "needs_rerun"
    assert "exposure" not in out


def test_tr16_2_empty_positions_all_cash_is_ok():
    days = _trading_days(date(2024, 1, 1), 10)
    # 新记录全程空仓：positions=[] 是有效负载
    daily = [{"snap_date": d, "total_value": 100.0, "positions": []}
             for d in days]
    out = compute_industry_attribution(daily, {}, {}, {})
    assert out["status"] == "ok"
    assert all(abs(c - 1.0) < 1e-9 for c in out["exposure"]["cash"])
    assert out["hit_rate"]["weekly"]["periods_holding"] == 0
