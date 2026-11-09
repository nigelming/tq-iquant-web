"""SnapshotBarSynthesizer 单元测试。

TQ 盘中 K 线缓存只到上一交易日（实测+手册 refresh_kline 限制），
实盘 bar 流改由 get_market_snapshot_batch 实时快照按分钟聚合合成。
"""
from datetime import datetime

import pytest

from core.engine.bar_poller import BarPoller
from core.engine.live.snapshot_bars import (
    SnapshotBarSynthesizer,
    SnapshotQuoteDispatcher,
    default_in_session,
)


D1 = "20261103"


def _fetcher(snaps):
    """snaps: {code: {"Now": "10.0", "Volume": "100"}} → fetch_snapshot 闭包。"""
    def fetch(codes):
        return {c: dict(snaps[c]) for c in codes if c in snaps}
    return fetch


def _mk(snaps, codes=("000001.SZ",), gate=None):
    s = SnapshotBarSynthesizer(
        list(codes),
        fetch_snapshot=_fetcher(snaps),
        in_session=gate or (lambda dt: True),
    )
    s._test_snaps = snaps      # 测试钩子：直接改快照表模拟下一次采样
    return s


# --- 合成：分钟边界与 OHLCV ---

def test_first_sample_only_forming_no_completed():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    assert s.completed_bars(["000001.SZ"]) == {"000001.SZ": []}


def test_minute_rollover_completes_bar_ohlc_volume_delta():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))          # 基线采样
    s._test_snaps["000001.SZ"] = {"Now": "10.5", "Volume": "1100"}
    s.sample(datetime(2026, 11, 3, 10, 50, 40))
    s._test_snaps["000001.SZ"] = {"Now": "9.9", "Volume": "1200"}
    s.sample(datetime(2026, 11, 3, 10, 50, 58))
    s._test_snaps["000001.SZ"] = {"Now": "10.2", "Volume": "1600"}
    s.sample(datetime(2026, 11, 3, 10, 51, 10))          # 跨分钟 → 10:50 bar 完成

    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert len(bars) == 1
    b = bars[0]
    # stime = 分钟结束时刻（10:50 分钟 → 10:51:00）
    assert b["stime"] == D1 + "105100"
    assert b["open"] == pytest.approx(10.0)
    assert b["high"] == pytest.approx(10.5)
    assert b["low"] == pytest.approx(9.9)
    assert b["close"] == pytest.approx(9.9)     # 分钟内末采样
    assert b["volume"] == 200                   # 1200 - 1000（10.2/1600 归下一分钟）


def test_zero_volume_minute_still_produces_bar():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s.sample(datetime(2026, 11, 3, 10, 51, 10))   # 无成交：Volume 不变
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert len(bars) == 1
    assert bars[0]["volume"] == 0
    assert bars[0]["close"] == pytest.approx(10.0)


def test_volume_decrease_clamped_to_zero():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s._test_snaps["000001.SZ"] = {"Now": "10.1", "Volume": "900"}   # 异常回退
    s.sample(datetime(2026, 11, 3, 10, 51, 10))
    assert s.completed_bars(["000001.SZ"])["000001.SZ"][0]["volume"] == 0


def test_fetch_failure_keeps_state_and_rollover():
    snaps = {"000001.SZ": {"Now": "10.0", "Volume": "1000"}}
    calls = {"n": 0}

    def flaky(codes):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("snapshot boom")
        return {c: dict(snaps[c]) for c in codes}

    s = SnapshotBarSynthesizer(["000001.SZ"], fetch_snapshot=flaky,
                               in_session=lambda dt: True)
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s.sample(datetime(2026, 11, 3, 10, 51, 5))    # fetch 失败 → 仅滚动完成
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert len(bars) == 1 and bars[0]["close"] == pytest.approx(10.0)

    snaps["000001.SZ"] = {"Now": "10.2", "Volume": "1600"}
    s.sample(datetime(2026, 11, 3, 10, 51, 30))
    s.sample(datetime(2026, 11, 3, 10, 52, 10))   # 10:51 bar 完成
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert bars[-1]["stime"] == D1 + "105200"
    assert bars[-1]["volume"] == 600              # 1600 - 1000（故障未破坏基线）


def test_multi_minute_accumulates_and_count_window():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "0"}})
    vols = [0, 600, 1300, 1900]
    for i, v in enumerate(vols):
        s._test_snaps["000001.SZ"] = {"Now": str(10.0 + i), "Volume": str(v)}
        s.sample(datetime(2026, 11, 3, 10, 50 + i, 30))
    # 完成 bar：105100(vol0) 105200(600) 105300(700)；count=2 取末两根
    bars = s.completed_bars(["000001.SZ"], count=2)["000001.SZ"]
    assert [b["stime"][-6:] for b in bars] == ["105200", "105300"]
    assert bars[0]["volume"] == 600 and bars[1]["volume"] == 700


def test_completed_bars_filters_unknown_codes():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s.sample(datetime(2026, 11, 3, 10, 51, 10))
    assert s.completed_bars(["999999.SZ"]) == {"999999.SZ": []}


def test_cross_day_resets_volume_baseline():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "500000"}})
    s.sample(datetime(2026, 11, 3, 14, 59, 50))
    s._test_snaps["000001.SZ"] = {"Now": "10.1", "Volume": "300"}
    s.sample(datetime(2026, 11, 4, 9, 30, 10))
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    # 昨日 14:59 bar 完成（stime=昨日 15:00）
    assert bars[0]["stime"] == "20261103150000"
    s._test_snaps["000001.SZ"] = {"Now": "10.2", "Volume": "500"}
    s.sample(datetime(2026, 11, 4, 9, 31, 10))    # 200 记入 09:31 forming
    s.sample(datetime(2026, 11, 4, 9, 32, 10))    # 滚动完成 09:31 bar
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert bars[-1]["stime"] == "20261104093200"
    assert bars[-1]["volume"] == 200              # 新交易日差分基线已重置


def test_reset_clears_state():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s.sample(datetime(2026, 11, 3, 10, 51, 10))
    assert len(s.completed_bars(["000001.SZ"])["000001.SZ"]) == 1
    s.reset()
    assert s.completed_bars(["000001.SZ"]) == {"000001.SZ": []}
    s.sample(datetime(2026, 11, 3, 10, 52, 10))   # 重置后首采样仅建基线
    assert s.completed_bars(["000001.SZ"]) == {"000001.SZ": []}


# --- 交易时段门控 ---

@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 11, 3, 9, 15), False),    # 集合竞价前
    (datetime(2026, 11, 3, 9, 30), True),
    (datetime(2026, 11, 3, 10, 0), True),
    (datetime(2026, 11, 3, 11, 29, 59), True),
    (datetime(2026, 11, 3, 11, 30), False),   # 午休
    (datetime(2026, 11, 3, 12, 30), False),
    (datetime(2026, 11, 3, 13, 0), True),
    (datetime(2026, 11, 3, 14, 59, 59), True),
    (datetime(2026, 11, 3, 15, 0), False),
    (datetime(2026, 11, 7, 10, 0), False),    # 周六
])
def test_default_in_session(dt, expected):
    assert default_in_session(dt) is expected


def test_out_of_session_sample_ignored_but_rollover_completes():
    def gate(dt):
        # 午休关闭（模拟默认门控拦截采样数据）
        return not (dt.hour == 12)

    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}}, gate=gate)
    s.sample(datetime(2026, 11, 3, 11, 29, 50))   # 盘中 → 建 forming
    s._test_snaps["000001.SZ"] = {"Now": "10.9", "Volume": "9999"}
    s.sample(datetime(2026, 11, 3, 12, 0, 0))     # 午休 → 样本忽略，但 11:29 bar 完成
    bars = s.completed_bars(["000001.SZ"])["000001.SZ"]
    assert len(bars) == 1
    assert bars[0]["stime"] == D1 + "113000"
    assert bars[0]["close"] == pytest.approx(10.0)   # 12:00 的 10.9 未混入


# --- BarPoller 适配器 ---

def test_adapter_batch_and_single_and_period_guard():
    s = _mk({"000001.SZ": {"Now": "10.0", "Volume": "1000"}})
    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    s.sample(datetime(2026, 11, 3, 10, 51, 10))
    ada = SnapshotQuoteDispatcher(s)
    assert ada.supports_batch_quote is True
    # 显式闭合声明：completed_bars 只吐已闭合 bar，BarPoller 可立即处理最新一根
    assert ada.bars_explicitly_completed is True
    out = ada.query_quotes_batch(["000001.SZ", "999999.SZ"], "1m", 10)
    assert set(out) == {"000001.SZ", "999999.SZ"}
    assert len(out["000001.SZ"]) == 1
    assert out["999999.SZ"] == []
    assert ada.query_quotes_batch(["000001.SZ"], "5m", 10) == {}
    one = ada.query_quote("000001.SZ", period="1m", count=10)
    assert len(one) == 1 and one[0]["stime"] == D1 + "105100"


def test_bar_poller_triggers_synthesized_bar_end_to_end():
    """闭合声明下：分钟一结束（合成器 rollover）即触发，不再白等下一根。

    原用例断言「10:50 bar 已 rollover 但仍是最新 → 不触发，要等 10:52 出现」，
    这正是本轮要消除的 ~60s 延迟（把「已闭合」误当「进行中」）。合成器只在
    采样时刻 >= 分钟终点才 rollover，且完成列表永不含 forming，因此立即处理
    最新一根仍是「只用已闭合数据」。
    """
    snaps = {"000001.SZ": {"Now": "10.0", "Volume": "1000"}}
    s = SnapshotBarSynthesizer(["000001.SZ"], fetch_snapshot=_fetcher(snaps),
                               in_session=lambda dt: True)
    s._test_snaps = snaps
    poller = BarPoller(SnapshotQuoteDispatcher(s), ["000001.SZ"],
                       period="1m", count=10)

    s.sample(datetime(2026, 11, 3, 10, 50, 30))
    assert poller.poll() == []                    # 无闭合 bar：未建基线

    s._test_snaps["000001.SZ"] = {"Now": "10.2", "Volume": "1600"}
    s.sample(datetime(2026, 11, 3, 10, 51, 10))   # 10:51 label 闭合（close 10.0）
    assert poller.poll() == []                    # 首根闭合 bar 建基线，不回放

    s._test_snaps["000001.SZ"] = {"Now": "10.3", "Volume": "2000"}
    s.sample(datetime(2026, 11, 3, 10, 52, 10))   # 10:52 label 闭合（close 10.2）
    events = poller.poll()
    assert len(events) == 1                       # 本轮立即触发，不再等下一根
    ev = events[0]
    assert ev.bar_time.strftime("%Y%m%d%H%M%S") == D1 + "105200"
    assert float(ev.stocks["000001.SZ"]["close"]) == pytest.approx(10.2)

    assert poller.poll() == []                    # 窗口重复 → 不重复触发
