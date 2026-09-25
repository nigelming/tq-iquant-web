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
