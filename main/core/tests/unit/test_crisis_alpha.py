import math

import pandas as pd
import pytest

from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, CRISIS_CATS, CRISIS_COLORS, CrisisAlphaAnalyzer,
    _classify_crisis, _rating, alpha_rating,
)

WEEKS_B = [-0.10, -0.10, -0.10, -0.10, 0.05, 0.05, 0.05, 0.05, -0.04, 0.04, -0.08, 0.02]
WEEKS_S = [0.02, -0.02, -0.06, -0.12, 0.03, 0.06, -0.01, 0.04, -0.03, 0.02, -0.09, 0.01]
WEEKS_B_DISTINCT = [-0.10, -0.09, -0.08, -0.07, 0.05, 0.06, 0.05, 0.04, -0.04, 0.03, -0.11, 0.02]
WEEKS_S_DISTINCT = [0.01, -0.01, -0.02, -0.03, 0.04, 0.05, 0.03, 0.02, -0.01, 0.01, -0.05, 0.01]


def _series_from_weekly(weekly_returns, start="2024-01-01", days_per_week=5):
    dates = pd.bdate_range(start=start, periods=len(weekly_returns) * days_per_week)
    values = []
    level = 100.0
    for r in weekly_returns:
        base = level
        for d in range(days_per_week):
            level = base * (1 + r * (d + 1) / days_per_week)
            values.append(level)
    return pd.Series(values, index=dates, dtype=float)


def _analyzer(b_weeks, s_weeks, **kw):
    return CrisisAlphaAnalyzer(
        _series_from_weekly([0.0] + s_weeks),
        _series_from_weekly([0.0] + b_weeks),
        **kw,
    )


def test_week_labels_follow_source_boundaries():
    b = [0.0, -0.10, -0.10, -0.10, -0.10]
    s = [0.01, -0.04, -0.05, -0.06, -0.11]
    assert _classify_crisis(b, s) == ["逆势上涨", "跌得少", "跟着跌", "跟着跌", "跌更多"]


def test_classify_crisis_zero_benchmark_with_negative_strategy_is_worst():
    assert _classify_crisis([0.0], [-0.01]) == ["跌更多"]


def test_rating_and_alpha_rating_thresholds():
    assert _rating(85) == "卓越" and _rating(84.9) == "优秀"
    assert _rating(70) == "优秀" and _rating(69.9) == "一般"
    assert alpha_rating(0.80) == "卓越" and alpha_rating(0.60) == "优秀"
    assert alpha_rating(0.40) == "一般" and alpha_rating(0.39) == "较弱"


def test_cats_colors_and_benchmarks_are_complete():
    assert CRISIS_CATS == ["逆势上涨", "跌得少", "跟着跌", "跌更多"]
    assert CRISIS_COLORS == {
        "逆势上涨": "#d6403a",
        "跌得少": "#f0a429",
        "跟着跌": "#9dc88d",
        "跌更多": "#1f6e4f",
    }
    codes = [b["code"] for b in CRISIS_BENCHMARKS]
    assert codes == ["000985.CSI", "000300.SH", "000905.SH", "000852.SH", "399006.SZ", "000688.SH"]
    assert [b["name"] for b in CRISIS_BENCHMARKS] == [
        "中证全指", "沪深300", "中证500", "中证1000", "创业板指", "科创50",
    ]


def test_quadrant_counts_alpha_ratio_and_odds():
    an = _analyzer(WEEKS_B, WEEKS_S)
    q = an.quadrant_analysis()
    assert q["n_total"] == 12
    assert q["n_down"] == 6
    assert q["counts"] == {"逆势上涨": 1, "跌得少": 1, "跟着跌": 2, "跌更多": 2}
    assert q["alpha_ratio"] == pytest.approx(2 / 6)
    assert q["up_rate"] == pytest.approx(1 / 6)
    assert q["follow_ratio"] == pytest.approx(4 / 6)
    assert q["down_avg_s"] == pytest.approx(-0.05)
    assert q["down_avg_b"] == pytest.approx(-0.52 / 6)
    assert q["win_avg"] == pytest.approx(0.02)
    assert q["loss_avg"] == pytest.approx(0.064)
    assert q["odds"] == pytest.approx(0.02 / 0.064)
    assert q["avg_ret"]["逆势上涨"] == pytest.approx(0.02)
    assert q["avg_ret"]["跌得少"] == pytest.approx(-0.02)
    assert q["avg_ret"]["跟着跌"] == pytest.approx(-0.045)
    assert q["avg_ret"]["跌更多"] == pytest.approx(-0.105)


def test_quadrant_on_flat_upward_benchmark_has_no_down_weeks():
    an = _analyzer([0.01] * 12, [0.02] * 12)
    q = an.quadrant_analysis()
    assert q["n_down"] == 0
    assert q["alpha_ratio"] == 0.0
    assert q["up_rate"] == 0.0
    assert q["odds"] == 0.0
    assert q["down_avg_s"] == 0.0
    assert q["down_avg_b"] == 0.0


def test_quadrant_zero_crisis_week_return_keeps_source_nan_quirk():
    an = _analyzer(
        [-0.10, -0.10, 0.05, 0.05, 0.05, 0.05],
        [0.0, -0.02, 0.03, 0.03, 0.03, 0.03],
    )
    q = an.quadrant_analysis()
    assert q["n_down"] == 2
    assert q["counts"]["逆势上涨"] > 0
    assert q["up_rate"] == pytest.approx(0.5)
    assert math.isnan(q["win_avg"])
    assert math.isnan(q["odds"])


def test_condition_tests_two_deep_thresholds():
    an = _analyzer(WEEKS_B, WEEKS_S)
    rows = an.condition_tests()
    assert [r["threshold"] for r in rows] == [-0.03, -0.05]
    assert rows[0]["n"] == 6
    assert rows[0]["b_avg"] == pytest.approx(-0.52 / 6)
    assert rows[0]["s_avg"] == pytest.approx(-0.05)
    assert rows[0]["up_rate"] == pytest.approx(1 / 6)
    assert rows[1]["n"] == 5
    assert rows[1]["b_avg"] == pytest.approx(-0.48 / 5)
    assert rows[1]["s_avg"] == pytest.approx(-0.27 / 5)
    assert rows[1]["up_rate"] == pytest.approx(1 / 5)


def test_condition_tests_zero_sample_returns_zeros():
    an = _analyzer([0.01] * 12, [0.01] * 12)
    for row in an.condition_tests():
        assert row["n"] == 0
        assert row["b_avg"] == 0.0
        assert row["s_avg"] == 0.0
        assert row["up_rate"] == 0.0


def test_extreme_drops_top_n_picks_worst_benchmark_weeks():
    an = _analyzer(WEEKS_B_DISTINCT, WEEKS_S_DISTINCT)
    top = an.extreme_drops_top_n(n=3)
    assert [str(d.date()) for d in top.index] == ["2024-01-19", "2024-01-12", "2024-03-22"]
    assert list(top["基准"]) == pytest.approx([-0.09, -0.10, -0.11])
    assert list(top["策略"]) == pytest.approx([-0.01, 0.01, -0.05])
    assert list(top["超额"]) == pytest.approx([0.08, 0.11, 0.06])
    assert list(top["分类"]) == ["跌得少", "逆势上涨", "跌得少"]


def test_weekly_detail_and_down_weeks():
    an = _analyzer(WEEKS_B, WEEKS_S)
    detail = an.weekly_detail_df()
    assert list(detail.columns) == ["基准收益", "策略收益", "超额", "分类"]
    assert len(detail) == 12
    down = an.down_week_df()
    assert len(down) == 6
    assert (down["基准收益"] < 0).all()
    assert down.index.is_monotonic_decreasing


def test_drawdown_series_is_zero_at_peak():
    an = _analyzer(WEEKS_B, WEEKS_S)
    dd = an.drawdown_series("b")
    assert dd.iloc[0] == pytest.approx(0.0)
    assert dd.min() < 0


def test_insufficient_common_days_raises():
    s = _series_from_weekly([0.0, 0.01, 0.01])
    b = _series_from_weekly([0.0, 0.01, 0.01])
    with pytest.raises(ValueError, match="共同交易日不足"):
        CrisisAlphaAnalyzer(s, b)


def test_non_series_input_raises_type_error():
    s = _series_from_weekly([0.0] * 13)
    with pytest.raises(TypeError):
        CrisisAlphaAnalyzer(s, [1, 2, 3])


def test_nav_normalizes_each_series_to_one():
    an = _analyzer(WEEKS_B, WEEKS_S)
    s_nav, b_nav = an._nav()
    assert s_nav.iloc[0] == pytest.approx(1.0)
    assert b_nav.iloc[0] == pytest.approx(1.0)
    assert len(s_nav) == len(an.s_orig_nav)
    assert len(b_nav) == len(an.b_orig_nav)


def test_deep_drawdown_events_peak_to_trough_single_event():
    an = _analyzer(WEEKS_B, WEEKS_S)
    events = an.deep_drawdown_events()
    assert len(events) == 1
    ev = events[0]
    assert str(ev["start"].date()) == "2024-01-05"
    assert str(ev["end"].date()) == "2024-02-02"
    assert ev["benchmark_ret"] == pytest.approx(-0.3439)
    assert ev["strategy_ret"] == pytest.approx(-0.17313088)
    assert ev["excess"] == pytest.approx(ev["strategy_ret"] - ev["benchmark_ret"])
    assert ev["hedged"] is False
    assert ev["verdict"] == "同步下跌"
    assert ev["max_dd"] == pytest.approx(-0.3439)
    assert ev["days"] == 28


def test_deep_drawdown_events_flags_hedged_event():
    b = [-0.10] * 6
    s = [0.02, 0.02, 0.02, 0.02, 0.02, 0.02]
    an = _analyzer(b, s)
    events = an.deep_drawdown_events()
    assert len(events) == 1
    assert events[0]["hedged"] is True
    assert events[0]["verdict"] == "逆势对冲"
    assert events[0]["excess"] > 0


def test_deep_drawdown_events_empty_when_shallow():
    an = _analyzer([-0.002] * 12, [0.0] * 12)
    dd = an.b_full_nav / an.b_full_nav.cummax() - 1
    assert float(dd.min()) > -0.05
    assert an.deep_drawdown_events() == []


def test_drawdown_shade_spans_merges_contiguous_region():
    an = _analyzer(WEEKS_B, WEEKS_S)
    spans = an.drawdown_shade_spans()
    assert len(spans) == 1
    dd = an.b_full_nav / an.b_full_nav.cummax() - 1
    deep_days = dd[dd < -0.05]
    assert str(deep_days.index[0].date()) == "2024-01-10"
    assert str(deep_days.index[-1].date()) == "2024-03-29"
    assert spans == [(deep_days.index[0], deep_days.index[-1])]


def test_drawdown_shade_spans_empty_when_no_deep_drawdown():
    an = _analyzer([-0.002] * 12, [0.0] * 12)
    dd = an.b_full_nav / an.b_full_nav.cummax() - 1
    assert float(dd.min()) > -0.05
    assert an.drawdown_shade_spans() == []


def test_rolling_correlation_needs_full_window():
    an = _analyzer(WEEKS_B_DISTINCT, WEEKS_S_DISTINCT)
    assert len(an.rolling_correlation()) == 0
    assert len(an.rolling_correlation(window=4)) == 9
    assert len(an.expanding_correlation()) == 5
    assert len(an.expanding_correlation(min_periods=2)) == 11


def test_cost_analysis_up_week_price_and_tail_correlation():
    b_weeks = [
        -0.10, -0.08, -0.06, -0.05, -0.04, -0.03, -0.02, -0.015,
        0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12,
    ]
    s_weeks = [
        0.02, -0.01, -0.03, 0.01, -0.05, 0.04, 0.03, -0.02,
        0.02, 0.01, -0.04, 0.05, 0.03, 0.06, -0.01, 0.02, 0.04, 0.07, -0.02, 0.03,
    ]
    an = _analyzer(b_weeks, s_weeks)
    cost = an.cost_analysis()
    assert cost["n_up"] == 12
    assert cost["drag"] == pytest.approx(0.25)
    assert cost["sync_up"] == pytest.approx(0.75)
    b_up = [r for r in b_weeks if r > 0]
    s_up = [s for b, s in zip(b_weeks, s_weeks) if b > 0]
    assert cost["elasticity"] == pytest.approx(
        (sum(s_up) / len(s_up)) / (sum(b_up) / len(b_up))
    )
    b_down = [r for r in b_weeks if r < 0]
    s_down = [s for b, s in zip(b_weeks, s_weeks) if b < 0]
    assert cost["tail_corr"] == pytest.approx(
        float(pd.Series(s_down).corr(pd.Series(b_down)))
    )
    assert cost["q1_corr"] == pytest.approx(-0.6742671940326725)


def test_cost_analysis_zero_when_no_up_weeks():
    b = [-0.01, -0.02, -0.01, -0.03, -0.02, -0.01, -0.02, -0.01, -0.03, -0.02, -0.01, -0.02]
    s = [0.01, -0.01, 0.02, -0.02, 0.01, 0.03, -0.01, 0.02, -0.03, 0.01, 0.02, -0.01]
    an = _analyzer(b, s)
    cost = an.cost_analysis()
    assert cost["n_up"] == 0
    assert cost["drag"] == 0.0
    assert cost["sync_up"] == 0.0
    assert cost["elasticity"] == 0.0


def test_performance_stats_cumulative_annual_vol_mdd_and_corr():
    an = _analyzer(WEEKS_B, WEEKS_S)
    perf = an.performance_stats()
    assert set(perf) == {"s", "b", "corr", "start", "end"}
    for side in ("s", "b"):
        assert set(perf[side]) == {"cum", "ann", "vol", "mdd"}
    assert perf["s"]["cum"] == pytest.approx(
        float(an.s_orig_nav.iloc[-1] / an.s_orig_nav.iloc[0] - 1)
    )
    assert perf["b"]["cum"] == pytest.approx(
        float(an.b_orig_nav.iloc[-1] / an.b_orig_nav.iloc[0] - 1)
    )
    assert perf["s"]["cum"] == pytest.approx(-0.15475410705679027)
    assert perf["s"]["ann"] == pytest.approx(-0.5023336802119625)
    assert perf["s"]["vol"] == pytest.approx(0.39271780291613057)
    assert perf["s"]["mdd"] == pytest.approx(-0.18934400000000007)
    assert perf["b"]["cum"] == pytest.approx(-0.25282934712029415)
    assert perf["b"]["ann"] == pytest.approx(-0.7017226526258493)
    assert perf["b"]["vol"] == pytest.approx(0.5069576516890347)
    assert perf["b"]["mdd"] == pytest.approx(-0.3438999999999999)
    assert -1.0 <= perf["corr"] <= 1.0


def test_monthly_analysis_state_buckets_and_hedge_ratio():
    an = _analyzer(WEEKS_B, WEEKS_S)
    mo = an.monthly_analysis()
    assert set(mo["states"]) == {"大涨", "小涨", "小跌", "大跌"}
    assert mo["states"]["大涨"]["n"] == 1
    assert mo["states"]["大跌"]["n"] == 1
    assert mo["states"]["小涨"]["n"] == 0
    assert mo["states"]["小跌"]["n"] == 0
    assert mo["states"]["大涨"]["win_rate"] == pytest.approx(1.0)
    assert mo["states"]["大跌"]["win_rate"] == pytest.approx(0.0)
    assert mo["n_down_month"] == 1
    assert mo["hedge_ratio"] == pytest.approx(0.0)
    assert len(mo["big_down"]) == 1


def test_monthly_analysis_flat_upward_benchmark_lands_in_small_up():
    an = _analyzer([0.001] * 12, [0.001] * 12)
    mo = an.monthly_analysis()
    assert mo["states"]["小涨"]["n"] == 2
    assert mo["states"]["大涨"]["n"] == 0
    assert mo["states"]["小跌"]["n"] == 0
    assert mo["states"]["大跌"]["n"] == 0
    assert mo["n_down_month"] == 0
    assert mo["hedge_ratio"] == 0.0
    assert len(mo["big_down"]) == 0


def test_annual_stats_single_partial_year():
    an = _analyzer(WEEKS_B, WEEKS_S)
    rows = an.annual_stats()
    assert len(rows) == 1
    row = rows[0]
    assert row["年份"] == 2024
    assert row["下跌周"] == 6
    assert row["危机Alpha"] == pytest.approx(2 / 6)
    assert row["完整"] is False
    assert row["策略收益"] == pytest.approx(math.prod(1 + r for r in WEEKS_S) - 1)
    assert row["基准收益"] == pytest.approx(math.prod(1 + r for r in WEEKS_B) - 1)


def test_annual_stats_marks_full_year_when_40_plus_weeks():
    weeks = 41
    b = ([-0.01] * (weeks // 2)) + ([0.01] * (weeks - weeks // 2))
    s = ([0.01] * (weeks // 2)) + ([0.01] * (weeks - weeks // 2))
    an = _analyzer(b, s)
    rows = an.annual_stats()
    assert rows[0]["完整"] is True
