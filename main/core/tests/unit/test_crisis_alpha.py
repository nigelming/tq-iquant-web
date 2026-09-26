import json
import math
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, CRISIS_CATS, CRISIS_COLORS, CrisisAlphaAnalyzer,
    _classify_crisis, _frame_records, _rating, alpha_rating,
    build_crisis_alpha_payload, sanitize_for_json,
)

WEEKS_B = [-0.10, -0.10, -0.10, -0.10, 0.05, 0.05, 0.05, 0.05, -0.04, 0.04, -0.08, 0.02]
WEEKS_S = [0.02, -0.02, -0.06, -0.12, 0.03, 0.06, -0.01, 0.04, -0.03, 0.02, -0.09, 0.01]
WEEKS_B_DISTINCT = [-0.10, -0.09, -0.08, -0.07, 0.05, 0.06, 0.05, 0.04, -0.04, 0.03, -0.11, 0.02]
WEEKS_S_DISTINCT = [0.01, -0.01, -0.02, -0.03, 0.04, 0.05, 0.03, 0.02, -0.01, 0.01, -0.05, 0.01]
WEEKS_B_SHALLOW = [-0.02, 0.015, -0.025, 0.018, -0.012, 0.008, -0.022, 0.026, -0.016, 0.011, 0.024, -0.007]
WEEKS_S_SHALLOW = [0.011, -0.006, 0.014, -0.009, 0.019, -0.004, 0.007, -0.013, 0.016, -0.005, 0.010, -0.008]


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


def test_composite_score_shape_weights_and_contributions():
    an = _analyzer(WEEKS_B, WEEKS_S)
    sc = an.composite_score()
    assert len(sc["dimensions"]) == 9
    assert [d["维度"] for d in sc["dimensions"]] == [
        "危机Alpha占比", "深跌周抗跌性", "下尾负相关", "回撤独立性",
        "多宽基一致性", "月度危机Alpha", "极端事件保护", "上涨周代价", "整体低相关",
    ]
    assert [d["类型"] for d in sc["dimensions"]] == [
        "核心", "核心", "对冲", "对冲", "稳健", "稳健", "核心", "代价", "对冲",
    ]
    assert [d["权重"] for d in sc["dimensions"]] == [
        0.20, 0.15, 0.12, 0.12, 0.10, 0.10, 0.10, 0.06, 0.05,
    ]
    assert sum(d["权重"] for d in sc["dimensions"]) == pytest.approx(1.0)
    for d in sc["dimensions"]:
        assert 0.0 <= d["得分"] <= 100.0
        assert d["评级"] in {"卓越", "优秀", "一般"}
    assert [d["得分"] for d in sc["dimensions"]] == pytest.approx(
        [30.3, 49.2, 38.6, 0.0, 15.0, 0.0, 50.0, 95.6, 14.2]
    )
    assert [d["贡献"] for d in sc["dimensions"]] == pytest.approx(
        [6.1, 7.4, 4.6, 0.0, 1.5, 0.0, 5.0, 5.7, 0.7]
    )
    assert sc["total_score"] == pytest.approx(31.0)
    assert sc["rating"] in {"卓越", "优秀", "一般"}
    b_w = [-0.08, -0.06, -0.05, -0.04, 0.02, 0.04, 0.06, 0.10, 0.12, -0.07, 0.08, 0.03]
    s_w = [0.02, -0.01, -0.03, -0.02, 0.04, 0.03, 0.02, 0.015, -0.004, 0.01, -0.008, -0.012]
    cost_dims = {
        d["维度"]: d["得分"] for d in _analyzer(b_w, s_w).composite_score()["dimensions"]
    }
    assert cost_dims["上涨周代价"] == pytest.approx(54.1)
    assert cost_dims["回撤独立性"] == pytest.approx(50.0)


def test_composite_score_neutral_fallbacks_without_deep_weeks_or_events():
    an = _analyzer(WEEKS_B_SHALLOW, WEEKS_S_SHALLOW)
    assert float(an.drawdown_series("b").min()) > -0.05
    assert float(an.b_week_ret.min()) > -0.03
    assert an.b_week_ret.std() > 0.0 and an.s_week_ret.std() > 0.0
    dims = {d["维度"]: d for d in an.composite_score()["dimensions"]}
    assert dims["深跌周抗跌性"]["得分"] == pytest.approx(50.0)
    assert dims["回撤独立性"]["得分"] == pytest.approx(50.0)


def test_composite_score_multi_benchmark_ratios_drive_fifth_dimension():
    an = _analyzer(WEEKS_B, WEEKS_S)
    base = {d["维度"]: d["得分"] for d in an.composite_score()["dimensions"]}
    boosted = {
        d["维度"]: d["得分"]
        for d in an.composite_score(multi_bench_alpha_ratios=[1.0] * 6)["dimensions"]
    }
    assert base["多宽基一致性"] == pytest.approx(15.0)
    assert boosted["多宽基一致性"] == pytest.approx(100.0)
    assert base["深跌周抗跌性"] == pytest.approx(boosted["深跌周抗跌性"])


def test_summary_exposes_all_module_inputs():
    an = _analyzer(WEEKS_B, WEEKS_S)
    s = an.summary()
    assert set(s) == {
        "strategy", "benchmark", "n_weeks", "quadrant", "extreme_top",
        "deep_dd_events", "rolling_corr", "monthly", "annual", "cost",
        "performance", "conditions", "score", "weekly_detail",
    }
    assert s["n_weeks"] == 12


def test_frame_records_puts_index_into_date_key():
    df = pd.DataFrame(
        {"基准": [-0.1, -0.2]},
        index=pd.to_datetime(["2024-01-12", "2024-01-19"]),
    )
    assert _frame_records(df) == [
        {"日期": pd.Timestamp("2024-01-12"), "基准": -0.1},
        {"日期": pd.Timestamp("2024-01-19"), "基准": -0.2},
    ]


def test_build_payload_aligns_nav_and_flags_main_benchmark():
    s_nav = _series_from_weekly([0.0] + WEEKS_S)
    b_nav = _series_from_weekly([0.0] + WEEKS_B)
    payload = build_crisis_alpha_payload(
        s_nav,
        "000300.SH",
        "沪深300",
        b_nav,
        {"000300.SH": b_nav, "000905.SH": b_nav * 1.01},
        "组合A",
    )
    assert payload["strategy_name"] == "组合A"
    assert payload["benchmark_code"] == "000300.SH"
    assert payload["benchmark_name"] == "沪深300"
    assert payload["n_weeks"] == 12
    assert payload["alpha_rating"] == "较弱"
    nav = payload["nav"]
    assert len(nav["dates"]) == 65
    assert len(nav["strategy"]) == 65
    assert len(nav["benchmark"]) == 65
    assert nav["strategy"][0] == pytest.approx(1.0)
    assert nav["benchmark"][0] == pytest.approx(1.0)
    assert nav["dates"][0] == "2024-01-01"
    assert nav["dates"][-1] == "2024-03-29"
    assert len(nav["shade_spans"]) == 1
    assert len(payload["multi_bench"]) == 2
    assert payload["multi_bench"][0]["is_main"] is True
    assert payload["multi_bench"][0]["name"] == "沪深300"
    assert payload["multi_bench"][1]["is_main"] is False
    assert payload["multi_bench"][1]["name"] == "中证500"
    assert len(payload["down_weeks"]) == 6
    assert payload["down_weeks"][0]["日期"] > payload["down_weeks"][-1]["日期"]
    assert len(payload["weekly_detail"]) == 12
    assert set(payload["weekly_detail"][0]) == {"日期", "基准收益", "策略收益", "超额", "分类"}
    assert payload["score"]["total_score"] > 0
    json.dumps(payload, allow_nan=False)


def test_build_payload_ignores_unusable_extra_benchmarks():
    s_nav = _series_from_weekly([0.0] + WEEKS_S)
    b_nav = _series_from_weekly([0.0] + WEEKS_B)
    short = b_nav.iloc[:10]
    payload = build_crisis_alpha_payload(
        s_nav, "000300.SH", "沪深300", b_nav, {"000905.SH": short}, "组合A"
    )
    assert [r["name"] for r in payload["multi_bench"]] == ["沪深300"]


def test_sanitize_converts_non_json_types():
    np_float = pd.DataFrame({"a": [1.5]})["a"].values[0]
    np_int = pd.DataFrame({"a": [3]})["a"].values[0]
    out = sanitize_for_json(
        {
            "nan": float("nan"),
            "inf": float("inf"),
            "ninf": float("-inf"),
            "np_f": np_float,
            "np_i": np_int,
            "ts": pd.Timestamp("2024-03-22"),
            "nat": pd.NaT,
            "dec": Decimal("2.50"),
            "dec_nan": Decimal("NaN"),
            "dec_inf": Decimal("Infinity"),
            "d": date(2024, 1, 2),
            "series": pd.Series(
                [1.0, 2.0], index=pd.to_datetime(["2024-01-05", "2024-01-12"])
            ),
            "frame": pd.DataFrame(
                {"基准": [-0.1, -0.2]},
                index=pd.to_datetime(["2024-01-05", "2024-01-12"]),
            ),
            "nested": [{"x": float("nan")}, [float("inf")]],
            "flag": True,
        }
    )
    assert out["nan"] is None
    assert out["inf"] is None
    assert out["ninf"] is None
    assert out["np_f"] == 1.5 and isinstance(out["np_f"], float)
    assert out["np_i"] == 3 and isinstance(out["np_i"], int)
    assert out["ts"] == "2024-03-22"
    assert out["nat"] is None
    assert out["dec"] == pytest.approx(2.5)
    assert out["dec_nan"] is None
    assert out["dec_inf"] is None
    assert out["d"] == "2024-01-02"
    assert out["flag"] is True
    assert out["series"] == [
        {"date": "2024-01-05", "value": 1.0},
        {"date": "2024-01-12", "value": 2.0},
    ]
    assert out["frame"] == [
        {"日期": "2024-01-05", "基准": pytest.approx(-0.1)},
        {"日期": "2024-01-12", "基准": pytest.approx(-0.2)},
    ]
    assert out["nested"] == [{"x": None}, [None]]
    json.dumps(out, allow_nan=False)


def test_sanitize_passes_through_plain_values():
    assert sanitize_for_json({"a": 1, "b": "x", "c": [1, 2]}) == {
        "a": 1, "b": "x", "c": [1, 2],
    }
    assert sanitize_for_json(None) is None
