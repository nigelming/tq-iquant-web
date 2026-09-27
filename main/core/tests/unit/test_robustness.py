"""TR-18.2 稳健性分析数值正确性（纯序列，不连 TQ、不连库）。

口径固化见 core/engine/robustness.py 模块文档。
"""
import json
import math

import numpy as np
import pandas as pd
import pytest

from core.engine.robustness import (
    compute_robustness, ROLLING_WINDOWS, MIN_ALIGNED_DAYS,
)


def _biz(start, end=None, periods=None):
    return pd.DatetimeIndex(pd.bdate_range(start, end, periods=periods))


def _close_series(dates, daily_returns, start_value=1_000_000.0):
    closes = start_value * np.cumprod(np.concatenate(([1.0], 1 + daily_returns)))
    return pd.Series(closes[: len(dates)], index=dates, dtype=float)


def _three_year_fixture():
    """3 年日线：基准收益正弦波动+正漂移；策略 r_s = 0.5·r_b + 0.0003。

    => 总体 beta≈0.5，上下行捕获≈0.5（含正 alpha 的微小偏移）。
    """
    dates = _biz("2023-01-02", "2025-12-31")
    n = len(dates)
    rb = np.sin(np.arange(n) / 7.0) * 0.006 + 0.0002
    rs = 0.5 * rb + 0.0003
    nav = _close_series(dates, rs)
    bench = _close_series(dates, rb, start_value=1000.0)
    return dates, nav, bench, rb, rs


def test_half_year_matrix_six_periods_and_values():
    dates, nav, bench, _, _ = _three_year_fixture()
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={}, benchmark_names={"000300.SH": "沪深300"})
    assert out["status"] == "ok"
    assert out["n_days"] == len(dates)
    hy = out["half_year_matrix"]
    assert [r["period"] for r in hy] == [
        "2023H1", "2023H2", "2024H1", "2024H2", "2025H1", "2025H2"]
    # 首半年策略收益独立复算
    h1 = nav[(nav.index >= "2023-01-01") & (nav.index <= "2023-06-30")]
    expected = h1.iloc[-1] / h1.iloc[0] - 1
    assert hy[0]["strategy_return"] == pytest.approx(expected, abs=1e-4)
    # 超额 = 策略 - 基准
    row = hy[2]
    assert row["excess_return"] == pytest.approx(
        row["strategy_return"] - row["benchmark_return"], abs=1e-9)
    assert row["max_drawdown"] >= 0
    assert isinstance(row["sharpe"], float)


def test_rolling_distribution_stats_and_histogram():
    dates, nav, bench, _, _ = _three_year_fixture()
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={}, benchmark_names={})
    weekly_returns = (
        nav.resample("W-FRI").last().pct_change().dropna())
    by_w = {w["window"]: w for w in out["rolling"]["windows"]}
    assert sorted(by_w) == list(ROLLING_WINDOWS)
    for w in ROLLING_WINDOWS:
        block = by_w[w]
        expect_n = max(len(weekly_returns) - w + 1, 0)
        assert block["n_samples"] == expect_n
        ret = block["returns"]
        assert ret["min"] <= ret["median"] <= ret["max"]
        assert 0.0 <= ret["negative_ratio"] <= 1.0
        sh = block["sharpe"]
        assert sh["min"] <= sh["median"] <= sh["max"]
        assert 0.0 <= sh["negative_ratio"] <= 1.0
        hist = block["histogram"]
        assert sum(b["count"] for b in hist) == expect_n


def test_benchmark_sensitivity_relative_metrics():
    dates, nav, bench, _, _ = _three_year_fixture()
    # 第二基准：余弦形态（验证逐基独立计算，beta 与主基准不同）
    rb2 = np.cos(np.arange(len(dates)) / 5.0) * 0.005
    other = _close_series(dates, rb2, start_value=2000.0)
    out = compute_robustness(
        nav, bench,
        primary_code="000300.SH",
        benchmark_closes={"000905.SH": other},
        benchmark_names={"000300.SH": "沪深300", "000905.SH": "中证500"})
    rows = out["benchmark_sensitivity"]
    assert [r["code"] for r in rows] == ["000300.SH", "000905.SH"]
    assert rows[0]["is_primary"] is True
    assert rows[1]["is_primary"] is False
    for r in rows:
        assert r["available"] is True
        assert r["beta"] is not None and r["information_ratio"] is not None
    # 策略对主基准 r_s = 0.5 r_b + α
    assert rows[0]["beta"] == pytest.approx(0.5, abs=0.03)
    # 捕获率≈0.5（正 alpha 使下行日偏移略大，放宽到 0.1）
    assert rows[0]["up_capture_ratio"] == pytest.approx(0.5, abs=0.1)
    assert rows[0]["down_capture_ratio"] == pytest.approx(0.5, abs=0.1)
    # 两基准 beta 不应相同
    assert abs(rows[0]["beta"] - rows[1]["beta"]) > 0.05


def test_benchmark_sensitivity_unavailable_row():
    dates, nav, bench, _, _ = _three_year_fixture()
    short = bench.iloc[:10].copy()
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={"399006.SZ": short},
        benchmark_names={"000300.SH": "沪深300", "399006.SZ": "创业板指"})
    rows = {r["code"]: r for r in out["benchmark_sensitivity"]}
    assert rows["000300.SH"]["available"] is True
    sec = rows["399006.SZ"]
    assert sec["available"] is False
    assert sec["beta"] is None and sec["information_ratio"] is None


def test_longest_loss_window_v_shape():
    dates = _biz("2024-01-02", "2024-09-30")
    n = len(dates)
    rets = np.zeros(n)
    # 第 40~59 个交易日持续下跌（累计约 -20%），之后 30 日涨回
    for i in range(40, 60):
        rets[i] = -0.011
    for i in range(60, 90):
        rets[i] = 0.008
    nav = _close_series(dates, rets)
    bench = _close_series(dates, np.full(n, 0.0002), start_value=3000.0)
    out = compute_robustness(
        nav, bench, primary_code="000985.CSI",
        benchmark_closes={}, benchmark_names={"000985.CSI": "中证全指"})
    w = out["longest_loss_window"]
    assert w["days"] > 0
    assert w["drawdown"] == pytest.approx(0.2, abs=0.02)
    assert w["trough_date"] >= w["start_date"]
    assert w["end_date"] >= w["trough_date"]


def test_short_history_insufficient():
    dates = _biz("2024-01-02", periods=15)
    nav = _close_series(dates, np.linspace(0, 0.01, 14))
    bench = _close_series(dates, np.linspace(0, -0.01, 14), 1000.0)
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={}, benchmark_names={})
    assert out["status"] == "insufficient"
    assert "共同交易日" in out["error"]
    assert out["n_days"] < MIN_ALIGNED_DAYS


def test_rolling_window_samples_zero_when_history_short():
    # 约 40 个交易日：ok 但 26 周滚动样本不足 → 统计为 None、直方图 0
    dates = _biz("2024-01-02", periods=40)
    nav = _close_series(dates, np.full(39, 0.001))
    bench = _close_series(dates, np.full(39, 0.0005), 1000.0)
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={}, benchmark_names={})
    assert out["status"] == "ok"
    for w in out["rolling"]["windows"]:
        assert w["n_samples"] == 0
        assert w["returns"]["median"] is None
        assert sum(b["count"] for b in w["histogram"]) == 0


def test_payload_is_json_safe_without_nan():
    dates, nav, bench, _, _ = _three_year_fixture()
    out = compute_robustness(
        nav, bench, primary_code="000300.SH",
        benchmark_closes={}, benchmark_names={})
    # allow_nan=False：存在 NaN/Infinity 会抛 ValueError
    json.dumps(out, ensure_ascii=False, allow_nan=False)
