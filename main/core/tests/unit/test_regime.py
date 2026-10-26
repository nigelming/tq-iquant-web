# -*- coding: utf-8 -*-
"""市场状态分段器 regime.py 单测（TR-4.1 ~ TR-4.3）。

四态：趋势上涨 bull / 趋势下跌 bear / 震荡 range / 下跌反弹 rebound。
规则口径（与实现常量一致）：周线 MA10/MA30 排列 + 26 周 Donchian 突破 +
连续 2 周确认；反弹=下跌段中自近端低点反弹≥8%、收盘站上 MA10 且 MA10 拐头向上。
标签 t 仅依赖 ≤t 数据（纯因果，转折点确认滞后 2 周属规则内滞后）。
"""
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from core.engine.regime import (
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_LABELS_CN,
    REGIME_RANGE,
    REGIME_REBOUND,
    segment_regime,
)

_FIRST_FRIDAY = date(2023, 1, 6)


def _fridays(n):
    return [_FIRST_FRIDAY + timedelta(weeks=i) for i in range(n)]


def _series(vals):
    """dict[date, float]，每个 ISO 周一个周五收盘点。"""
    return dict(zip(_fridays(len(vals)), vals))


def _labels(res):
    return [w["label"] for w in res["weeks"]]


# ---------------------------------------------------------------------------
# TR-4.1 四种构造序列
# ---------------------------------------------------------------------------
def test_single_sided_uptrend_labeled_bull():
    """30 周平台后每周 +2% 单边上涨：出现趋势上涨段，末周 bull，无熊/反弹标签。"""
    vals = [100.0] * 30 + [100.0 * (1.02 ** i) for i in range(1, 31)]
    res = segment_regime(_series(vals))
    labels = _labels(res)

    assert labels[-1] == REGIME_BULL
    assert REGIME_BEAR not in labels
    assert REGIME_REBOUND not in labels
    bull_segs = [s for s in res["segments"] if s["label"] == REGIME_BULL]
    assert bull_segs and bull_segs[-1]["change_pct"] > 0
    assert bull_segs[-1]["end_date"] == _fridays(60)[-1]
    assert res["counts"][REGIME_BULL] == bull_segs[-1]["weeks"]


def test_single_sided_downtrend_labeled_bear():
    """30 周平台后每周 -2% 单边下跌：末周 bear，无牛/反弹标签。"""
    vals = [100.0] * 30 + [100.0 * (0.98 ** i) for i in range(1, 31)]
    res = segment_regime(_series(vals))
    labels = _labels(res)

    assert labels[-1] == REGIME_BEAR
    assert REGIME_BULL not in labels
    assert REGIME_REBOUND not in labels
    bear_segs = [s for s in res["segments"] if s["label"] == REGIME_BEAR]
    assert bear_segs and bear_segs[-1]["change_pct"] < 0


def test_box_range_never_breaks_out():
    """60 周在 99~101 箱体内循环（首 26 周已覆盖全部高低点）：全程震荡。"""
    pattern = [100.0, 100.5, 101.0, 100.5, 100.0, 99.5, 99.0, 99.5]
    vals = (pattern * 8)[:60]
    res = segment_regime(_series(vals))
    labels = set(_labels(res))
    assert labels == {REGIME_RANGE}
    assert len(res["segments"]) == 1
    assert res["counts"] == {REGIME_BULL: 0, REGIME_BEAR: 0,
                             REGIME_RANGE: 60, REGIME_REBOUND: 0}


def test_v_shape_bear_then_rebound_then_bull():
    """平台→急跌 15 周(-2%/周)→强反弹 16 周(+3%/周)：bear→rebound→bull 有序出现。"""
    flat = [100.0] * 30
    decline = [100.0 * (0.98 ** i) for i in range(1, 16)]          # 15 周
    trough = 100.0 * (0.98 ** 15)
    recovery = [trough * (1.03 ** i) for i in range(1, 17)]       # 16 周
    vals = flat + decline + recovery
    assert len(vals) == 61

    res = segment_regime(_series(vals))
    labels = _labels(res)
    first_bear = labels.index(REGIME_BEAR)
    first_rebound = labels.index(REGIME_REBOUND)
    first_bull = labels.index(REGIME_BULL)

    assert first_bear < first_rebound < first_bull
    assert labels[-1] == REGIME_BULL
    # 反弹段：起点紧接下跌段、幅度为正、起点自低点涨幅已≥阈值
    rebound_seg = next(s for s in res["segments"] if s["label"] == REGIME_REBOUND)
    assert rebound_seg["change_pct"] > 0
    assert rebound_seg["start_close"] / trough - 1 >= 0.08 - 1e-9


def test_segments_are_contiguous_and_sorted():
    """段列表按时间排序、相邻段首尾相接（周频日期连续）。"""
    vals = [100.0] * 30 + [100.0 * (0.98 ** i) for i in range(1, 31)]
    res = segment_regime(_series(vals))
    segs = res["segments"]
    assert [s["start_date"] for s in segs] == sorted(s["start_date"] for s in segs)
    for prev, nxt in zip(segs, segs[1:]):
        assert nxt["start_date"] - prev["end_date"] == timedelta(weeks=1)
        assert prev["label"] != nxt["label"]
        # change_pct = end/start - 1
        assert nxt["change_pct"] == pytest.approx(
            nxt["end_close"] / nxt["start_close"] - 1)


# ---------------------------------------------------------------------------
# TR-4.2 确定性 + 无未来函数
# ---------------------------------------------------------------------------
def test_deterministic_same_input_same_output():
    vals = [100.0] * 30 + [100.0 * (0.98 ** i) for i in range(1, 31)]
    assert segment_regime(_series(vals)) == segment_regime(_series(vals))


def test_truncation_prefix_matches_full_series():
    """截断到第 k 周的子序列，其全部周标签与全量序列对应周完全一致（纯因果）。"""
    flat = [100.0] * 30
    decline = [100.0 * (0.98 ** i) for i in range(1, 16)]
    trough = 100.0 * (0.98 ** 15)
    recovery = [trough * (1.03 ** i) for i in range(1, 17)]
    vals = flat + decline + recovery
    full = _labels(segment_regime(_series(vals)))
    for k in (40, 50, 60):
        truncated = _labels(segment_regime(_series(vals[:k])))
        assert truncated == full[:k]


# ---------------------------------------------------------------------------
# TR-4.3 极短/异常输入安全
# ---------------------------------------------------------------------------
def test_short_series_returns_safe_range():
    """不足 MA30 窗口（30 周）：全部震荡标签，不抛错。"""
    vals = [100.0 + i for i in range(10)]
    res = segment_regime(_series(vals))
    assert len(res["weeks"]) == 10
    assert set(_labels(res)) == {REGIME_RANGE}
    assert len(res["segments"]) == 1


def test_empty_series_returns_empty_payload():
    res = segment_regime({})
    assert res["weeks"] == []
    assert res["segments"] == []
    assert res["counts"] == {REGIME_BULL: 0, REGIME_BEAR: 0,
                             REGIME_RANGE: 0, REGIME_REBOUND: 0}


# ---------------------------------------------------------------------------
# 输入兼容
# ---------------------------------------------------------------------------
def test_accepts_pandas_series_and_decimal():
    vals = [100.0] * 30 + [100.0 * (1.02 ** i) for i in range(1, 31)]
    idx = pd.to_datetime(_fridays(60))
    res_pd = segment_regime(pd.Series(vals, index=idx))
    res_dec = segment_regime(_series([Decimal(str(v)) for v in vals]))
    assert _labels(res_pd) == _labels(res_dec)
    assert _labels(res_pd)[-1] == REGIME_BULL


def test_weekly_date_is_friday_of_iso_week():
    """非周五的日频点按 ISO 周聚合，周末标签日为该周周五（对齐 crisis W-FRI 口径）。"""
    # 同一 ISO 周内两个交易日（周一、周四），取最后一个（周四）为周收盘
    res = segment_regime({
        date(2023, 1, 2): 100.0,   # 周一
        date(2023, 1, 5): 101.0,   # 周四
    })
    assert len(res["weeks"]) == 1
    assert res["weeks"][0]["date"] == date(2023, 1, 6)
    assert res["weeks"][0]["close"] == pytest.approx(101.0)


def test_chinese_label_map_covers_four_states():
    assert set(REGIME_LABELS_CN) == {
        REGIME_BULL, REGIME_BEAR, REGIME_RANGE, REGIME_REBOUND}


# ===========================================================================
# B5：CausalRegimeTracker —— 逐日因果跟踪（引擎运行时注入 portfolio.current_regime）
# ===========================================================================
from core.engine.regime import CausalRegimeTracker


def _daily_from_weekly(vals):
    """每周五收盘展开成 周一~周五 日频（周内从上周五值线性渐变到本周五值）。"""
    out = {}
    for i, v in enumerate(vals):
        friday = _FIRST_FRIDAY + timedelta(weeks=i)
        prev = vals[i - 1] if i > 0 else vals[0]
        for offset in range(5):
            d = friday - timedelta(days=4 - offset)
            out[d] = prev + (v - prev) * (offset + 1) / 5
    return out


def test_tracker_prefix_labels_match_batch():
    """纯因果：逐日喂入时任一前缀的 tracker.label == segment_regime(前缀) 末周标签。

    这是禁未来函数的关键性质：标签只依赖 ≤当日的数据，且不随后续数据回改。
    """
    vals = [100.0] * 30 + [100.0 * (1.02 ** i) for i in range(1, 31)]
    series = _daily_from_weekly(vals)
    dates = sorted(series)
    tracker = CausalRegimeTracker()
    for i, d in enumerate(dates):
        tracker.update(d, series[d])
        prefix = {k: series[k] for k in dates[: i + 1]}
        batch_label = segment_regime(prefix)["weeks"][-1]["label"]
        assert tracker.label == batch_label, (
            "前缀 %d 天（%s）：tracker=%s batch=%s" % (i + 1, d, tracker.label, batch_label)
        )


def test_tracker_feed_history_matches_batch_final_label():
    """feed_history 批量预喂与 segment_regime 末周标签一致。"""
    series = _series([100.0] * 30 + [100.0 * (1.02 ** i) for i in range(1, 31)])
    tracker = CausalRegimeTracker()
    assert tracker.feed_history(series) == segment_regime(series)["weeks"][-1]["label"]


def test_tracker_same_day_update_idempotent():
    """同日重复 update 幂等：直接返回当前标签，不吸收新值、不改状态。"""
    tracker = CausalRegimeTracker()
    d = date(2024, 1, 5)
    tracker.update(d, 100.0)
    label = tracker.label
    assert tracker.update(d, 999.0) == label
    assert tracker.label == label


def test_tracker_out_of_order_raises():
    """日期必须单调不减：乱序 update 抛 ValueError。"""
    tracker = CausalRegimeTracker()
    tracker.update(date(2024, 1, 5), 100.0)
    with pytest.raises(ValueError):
        tracker.update(date(2024, 1, 4), 101.0)
