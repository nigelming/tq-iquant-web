"""BarPoller 单测(0009 切片3)— httpx MockTransport 模拟桥 /quote 响应。

验证行情通道核心逻辑(**相对变化判定 bar 完成,不依赖任何绝对时钟**):
  - bar 完成 = 它不再是「最新一根」(下次拉取出现更新的 bar,它退居第二)
  - 首次拉取建立基线,不回放触发历史 bar
  - 只触发 > last_completed_stime 的新完成 bar(防重复)
  - 多股票同一 bar 时间合并为一根 BarEvent.stocks
  - 桥离线抛 BridgeUnavailableError(不吞异常,交上层暂停交易)
  - parse_bar_time 兼容 stime(14位串)/time(毫秒/秒时间戳)

桥 /quote 返回格式(live/bridge/iquant_bridge.py get_quote):
  {"ok": True, "data": {code: [bar, ...]}}
每 bar = DataFrame reset_index().to_dict("records")。
"""
from datetime import datetime, time
from decimal import Decimal

import httpx
import pytest

from core.engine.bar_poller import (
    DEFAULT_QUOTE_POLL_PAUSE,
    DEFAULT_QUOTE_POLL_RESUME,
    BarPoller,
    QuotePollingWindow,
    parse_bar_time,
)
from core.engine.event import BarEvent
from core.engine.http_bridge_dispatcher import BridgeUnavailableError


# ---------------- 工具 ----------------
def _bar(stime, close, open_=None, high=None, low=None, volume=10000, **extra):
    """构造桥 /quote 返回的单根 bar dict(stime = yyyymmddHHMMSS bar 结束时间)。"""
    c = float(close)
    bar = {
        "stime": stime,
        "open": float(open_) if open_ is not None else c,
        "high": float(high) if high is not None else c,
        "low": float(low) if low is not None else c,
        "close": c,
        "volume": volume,
    }
    bar.update(extra)
    return bar


def _make_quote_response(bars_by_code):
    """构造桥 /quote 的 JSON 响应体:{code: [bar,...]}。"""
    return httpx.Response(200, json={"ok": True, "data": bars_by_code})


class _QuoteRecorder:
    """MockTransport handler:按 code 维护独立响应队列。

    per_code: dict{code: list[list[bar]]},每只 code 有自己的 bar 列表序列,
    每次该 code 的 /quote 请求 pop 队列首元素。天然处理多股票、不依赖轮次计数。
    """

    def __init__(self, per_code=None, fail=False):
        self._per_code = {c: list(seq) for c, seq in (per_code or {}).items()}
        self.fail = fail
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/quote":
            code = self._extract_code(request)
            seq = self._per_code.get(code, [[]])
            bars = seq.pop(0) if seq else []
            return _make_quote_response({code: bars})
        if request.url.path == "/ping":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    @staticmethod
    def _extract_code(request):
        """从 /quote?code=XXX&... 取 code 参数。"""
        import urllib.parse
        q = urllib.parse.urlparse(str(request.url)).query
        for pair in q.split("&"):
            if pair.startswith("code="):
                return urllib.parse.unquote(pair[5:])
        return ""


def _make_poller(recorder, codes=None, period="1m", count=10):
    """recorder 按 code 维护独立响应队列(每只 code 一次请求 pop 一个)。"""
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=client)
    return BarPoller(dispatcher=disp, stock_codes=codes or ["600000.SH"],
                     period=period, count=count), recorder


# ---------------- parse_bar_time ----------------
def test_parse_bar_time_stime_14digit():
    """stime '20260805100800' → datetime(2026,8,5,10,8,0)。"""
    assert parse_bar_time({"stime": "20260805100800"}) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_time_ms_timestamp():
    """time 为 13 位毫秒时间戳(北京时间 +8 显式转,不依赖本机时区)。"""
    # 2026-08-05 10:08:00 CST 的毫秒时间戳(动态算,避免硬编码错)
    dt = datetime(2026, 8, 5, 10, 8, 0)
    from datetime import timezone, timedelta
    cst = timezone(timedelta(hours=8))
    ms = int(dt.replace(tzinfo=cst).timestamp()) * 1000
    assert parse_bar_time({"time": ms}) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_time_sec_timestamp():
    """time 为 10 位秒时间戳。"""
    dt = datetime(2026, 8, 5, 10, 8, 0)
    from datetime import timezone, timedelta
    cst = timezone(timedelta(hours=8))
    sec = int(dt.replace(tzinfo=cst).timestamp())
    assert parse_bar_time({"time": sec}) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_stime_preferred_over_time():
    """stime 优先于 time(两者都存在时用 stime)。"""
    bar = {"stime": "20260805100800", "time": 1788432480000}
    assert parse_bar_time(bar) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_index_14digit_fallback():
    """index 14 位时间戳兜底(无 stime/time 时)。"""
    assert parse_bar_time({"index": "20260805100800"}) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_index_8digit_date():
    """index 8 位日期(日线)兜底。"""
    assert parse_bar_time({"index": "20260805"}) == datetime(2026, 8, 5, 0, 0, 0)


def test_parse_bar_time_stime_still_preferred_over_index():
    """stime 仍优先于 index(两者都存在时用 stime)。"""
    bar = {"stime": "20260805100800", "index": "20260805100900"}
    assert parse_bar_time(bar) == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_stime_8digit_daily_bar():
    """日线 bar 的 stime 是 8 位日期 "20261106" → 当日 00:00（回归：曾解析成 1970）。"""
    assert parse_bar_time({"stime": "20261106"}) == datetime(2026, 11, 6, 0, 0, 0)


def test_parse_bar_time_numeric_8digit_not_treated_as_epoch():
    """数值型 8 位日期不得当 epoch 秒（20261106 → 1970-08-23 的真机 bug）。"""
    assert parse_bar_time({"time": 20261106}) == datetime(2026, 11, 6, 0, 0, 0)


def test_parse_bar_time_out_of_range_year_warns_and_returns_none(caplog):
    """年份不在 [2000, 2100] → WARN 带原始值并返回 None，绝不静默按 1970 处理。"""
    import logging

    with caplog.at_level(logging.WARNING):
        assert parse_bar_time({"stime": "19700823"}) is None
    assert any("out of range" in r.getMessage() and "19700823" in r.getMessage()
               for r in caplog.records)


def test_parse_bar_time_invalid_returns_none():
    assert parse_bar_time({}) is None
    assert parse_bar_time({"stime": "abc"}) is None
    assert parse_bar_time({"stime": ""}) is None
    assert parse_bar_time(None) is None


# ---------------- 相对变化判定 bar 完成 ----------------
def test_first_poll_baseline_no_trigger():
    """首次拉取建立基线,不触发任何 bar(不回放历史 bar)。"""
    # 第 1 轮:[10:08, 10:09],10:09 最新(进行中),10:08 已完成 —— 但首次不触发
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
    ]})
    poller, _ = _make_poller(rec)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    result = poller.poll()

    assert fired == []
    assert result == []
    assert poller._initialized is True
    # 基线:last_completed_stime = 最高完成 bar(10:08)
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 8, 0)


def test_new_bar_completed_when_newer_bar_appears():
    """核心:10:09 在新 bar 10:10 出现后退居第二 → 10:09 完成,触发。"""
    # 第 1 轮:基线 [10:08, 10:09](10:09 最新)
    # 第 2 轮:[10:08, 10:09, 10:10](10:10 最新,10:09 退居第二 → 完成)
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
    ]})
    poller, _ = _make_poller(rec)
    poller.on_bar = lambda bar: fired.append(bar)
    fired = []

    poller.poll()                       # 基线
    assert fired == []
    result = poller.poll()              # 10:10 出现 → 10:09 完成

    assert len(fired) == 1
    assert result == fired
    bar = fired[0]
    assert bar.bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert "600000.SH" in bar.stocks
    assert bar.stocks["600000.SH"]["close"] == Decimal("9.32")
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 9, 0)


def test_in_progress_latest_bar_never_triggers():
    """最新 bar(进行中)永远不触发,无论拉多少次。"""
    # 持续只返回 [10:08, 10:09](10:09 始终最新,无新 bar 出现)
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    poller.poll()
    poller.poll()

    assert fired == []
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 8, 0)


def test_multiple_new_bars_triggered_in_order():
    """跳 bar(拉取间隔漏中间):区间内新完成 bar 按时间顺序全部触发。"""
    # 第 1 轮:基线 [10:08, 10:09]
    # 第 2 轮:[10:08, 10:09, 10:10, 10:11, 10:12](10:12 最新,10:09/10:10/10:11 都新完成)
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
         _bar("20260805101000", 9.40), _bar("20260805101100", 9.45),
         _bar("20260805101200", 9.50)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    result = poller.poll()

    # 触发 10:09, 10:10, 10:11(10:12 是最新不触发),按时间顺序
    assert [b.bar_time for b in result] == [
        datetime(2026, 8, 5, 10, 9, 0),
        datetime(2026, 8, 5, 10, 10, 0),
        datetime(2026, 8, 5, 10, 11, 0),
    ]
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 11, 0)


def test_no_duplicate_trigger_for_same_bar():
    """同一根 bar 不重复触发(last_completed_stime 推进后,旧 bar 被过滤)。"""
    # 第 1 轮:基线 [10:08, 10:09]
    # 第 2 轮:出现 10:10 → 10:09 触发
    # 第 3 轮:仍是 [.., 10:10](10:10 最新)→ 10:09 不再触发
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    poller.poll()                       # 10:09 触发
    poller.poll()                       # 无新完成 bar

    assert len(fired) == 1
    assert fired[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)


def test_only_bars_beyond_last_completed_trigger():
    """基线后,只触发 > last_completed_stime 的已完成 bar。"""
    # 第 1 轮:基线 [10:08, 10:09] → last_completed=10:08
    # 第 2 轮:[10:08, 10:09, 10:10] → 只触发 10:09(>10:08),10:08 旧不触发
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    result = poller.poll()

    assert len(result) == 1
    assert result[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)


def test_poll_attaches_fetched_bars_for_triggered_bar():
    """BarPoller 把本轮该 code 拉到的原始 bars 挂到 BarEvent.bars_by_code。

    注入复用（消除 1m 双拉）：BarPoller 本轮已拉 count=10 根，注入若再走
    _get_bars_with_increment 增量拉（同样 count=10）是双份冗余。透传后注入
    把本轮 bars 并入预热缓存直接复用。挂在事件上而非改 on_bar 签名（保持
    1 参回调兼容）。
    """
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                       # 基线
    poller.poll()                       # 10:09 完成 → 触发

    assert len(fired) == 1
    bar = fired[0]
    assert bar.bars_by_code is not None
    assert set(bar.bars_by_code.keys()) == {"600000.SH"}
    raw = bar.bars_by_code["600000.SH"]
    # 本轮拉到的原始 bars（含最新 forming 10:10），供注入并入缓存复用
    assert [parse_bar_time(b) for b in raw] == [
        datetime(2026, 8, 5, 10, 8, 0),
        datetime(2026, 8, 5, 10, 9, 0),
        datetime(2026, 8, 5, 10, 10, 0),
    ]


# ---------------- 多股票合并 ----------------
def test_multiple_stocks_same_time_merged_into_one_bar_event():
    """多股票同一 bar 时间 → 合并为一根 BarEvent.stocks。"""
    # 第 1 轮:基线,两股都有 [10:08, 10:09]
    # 第 2 轮:两股都出现 10:10 → 10:09(两股同时间)合并为一根 BarEvent
    rec = _QuoteRecorder(per_code={
        "600000.SH": [
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
        ],
        "000001.SZ": [
            [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50)],
            [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50), _bar("20260805101000", 18.60)],
        ],
    })
    poller, _ = _make_poller(rec, codes=["600000.SH", "000001.SZ"])
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    result = poller.poll()

    assert len(result) == 1
    bar = result[0]
    assert bar.bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert set(bar.stocks.keys()) == {"600000.SH", "000001.SZ"}
    assert bar.stocks["600000.SH"]["close"] == Decimal("9.32")
    assert bar.stocks["000001.SZ"]["close"] == Decimal("18.50")


def test_multiple_stocks_different_times_trigger_separate_bars():
    """多股票 bar 时间不同步 → 按各自完成时间分别触发。"""
    # 第 1 轮:基线。A 最新 10:09,B 最新 10:09
    # 第 2 轮:A 出现 10:10,B 仍只到 10:09 → A 的 10:09 完成;B 的 10:09 仍最新不触发
    # 第 3 轮:B 出现 10:10 → B 的 10:09 完成
    rec = _QuoteRecorder(per_code={
        "600000.SH": [
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32), _bar("20260805101000", 9.40)],
        ],
        "000001.SZ": [
            [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50)],
            [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50)],
            [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50), _bar("20260805101000", 18.60)],
        ],
    })
    poller, _ = _make_poller(rec, codes=["600000.SH", "000001.SZ"])
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                       # 基线
    r1 = poller.poll()                  # A 的 10:09 完成
    r2 = poller.poll()                  # B 的 10:09 完成

    assert len(r1) == 1 and r1[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert set(r1[0].stocks.keys()) == {"600000.SH"}
    assert len(r2) == 1 and r2[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert set(r2[0].stocks.keys()) == {"000001.SZ"}


# ---------------- E8 离线恢复重建基线 ----------------
def test_reset_baseline_after_offline_drops_gap_then_triggers_normally():
    """E8:离线→在线转场 reset_baseline 后,首次 poll 只建基线不触发(离线期间错过的
    bar 直接丢弃,不补触发——补触发=过时信号+现价成交,价格错位);再 poll 正常触发。"""
    # 第 1 轮:基线 [10:08, 10:09]
    # reset_baseline()(模拟离线恢复)
    # 第 2 轮:[10:08, 10:09, 10:10, 10:11](离线期间新出现 10:10/10:11)
    #   → 重建基线,last_completed=10:10,**不触发任何 bar**
    # 第 3 轮:[.., 10:12] 出现 → 10:11 完成,正常触发
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
         _bar("20260805101000", 9.40), _bar("20260805101100", 9.45)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
         _bar("20260805101000", 9.40), _bar("20260805101100", 9.45),
         _bar("20260805101200", 9.50)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                       # 基线建立
    assert fired == []
    assert poller._initialized is True
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 8, 0)

    # 模拟离线→在线转场
    poller.reset_baseline()
    assert poller._initialized is False
    assert poller._last_completed == {}

    # 首次 poll:只重建基线,离线期间错过的 10:09/10:10 不补触发
    result = poller.poll()
    assert fired == []
    assert result == []
    assert poller._initialized is True
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 10, 0)

    # 再 poll:10:12 出现 → 10:11 完成,正常触发
    result2 = poller.poll()
    assert len(fired) == 1
    assert result2 == fired
    assert fired[0].bar_time == datetime(2026, 8, 5, 10, 11, 0)
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 11, 0)


def test_reset_baseline_dropped_completed_bar_not_triggered_later():
    """E8:离线期间已完成但被基线吞掉的 bar,恢复后不因水位推进而补触发。"""
    # 第 1 轮:基线 [10:08, 10:09]
    # reset_baseline()
    # 第 2 轮:[10:08, 10:09, 10:10, 10:11] → 基线推到 10:10,10:09/10:10 被吞
    # 第 3 轮:[.., 10:10, 10:11, 10:12] → 只触发 10:11(10:09/10:10 已落基线)
    rec = _QuoteRecorder(per_code={"600000.SH": [
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
         _bar("20260805101000", 9.40), _bar("20260805101100", 9.45)],
        [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
         _bar("20260805101000", 9.40), _bar("20260805101100", 9.45),
         _bar("20260805101200", 9.50)],
    ]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    poller.reset_baseline()
    poller.poll()                       # 重建基线(吞掉 10:09/10:10)
    result = poller.poll()              # 只触发 10:11

    assert [b.bar_time for b in result] == [datetime(2026, 8, 5, 10, 11, 0)]
    assert len(fired) == 1


# ---------------- 桥离线 / 空数据 ----------------
def test_bridge_offline_raises():
    """桥不可用 → BridgeUnavailableError,不吞异常。"""
    rec = _QuoteRecorder(fail=True)
    poller, _ = _make_poller(rec)
    with pytest.raises(BridgeUnavailableError):
        poller.poll()


def test_empty_quote_no_trigger():
    """桥返回空 bar 列表 → 不触发,且不建立基线(下次仍按首次处理)。"""
    rec = _QuoteRecorder(per_code={"600000.SH": [[]]})
    poller, _ = _make_poller(rec)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    result = poller.poll()
    assert fired == []
    assert result == []
    # 空数据不建立基线
    assert poller._initialized is False


# ---------------- TQ 1.3.1 批量行情路径 ----------------
class BatchDispatcher:
    """支持批量行情的假 dispatcher(TdxTqDispatcher 同形状)。

    batches:每次 query_quotes_batch 依次弹出的返回值(Exception 元素 → 抛出模拟故障);
    per_code:回退路径下每 code 的 query_quote 响应队列(同 _QuoteRecorder 语义)。
    """

    supports_batch_quote = True

    def __init__(self, batches=None, per_code=None):
        self._batches = list(batches or [])
        self._per_code = {c: list(seq) for c, seq in (per_code or {}).items()}
        self.batch_calls = []
        self.quote_calls = []

    def query_quotes_batch(self, codes, period="1m", count=10):
        self.batch_calls.append(list(codes))
        item = self._batches.pop(0) if self._batches else {}
        if isinstance(item, Exception):
            raise item
        return item

    def query_quote(self, code, period="1m", count=10):
        self.quote_calls.append(code)
        seq = self._per_code.get(code) or []
        return seq.pop(0) if seq else []


def test_batch_path_triggers_same_as_per_code():
    """批量命中:每轮一次批量拉取,多股合并触发行为与逐只路径一致。"""
    base = {
        "600000.SH": [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
        "000001.SZ": [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50)],
    }
    nxt = {
        "600000.SH": [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
                      _bar("20260805101000", 9.40)],
        "000001.SZ": [_bar("20260805100800", 18.40), _bar("20260805100900", 18.50),
                      _bar("20260805101000", 18.60)],
    }
    disp = BatchDispatcher(batches=[base, nxt])
    poller = BarPoller(disp, ["600000.SH", "000001.SZ"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                       # 基线
    result = poller.poll()              # 两股的 10:09 均完成 → 合并触发

    # 每轮恰好一次批量调用,零逐只调用
    assert disp.batch_calls == [["600000.SH", "000001.SZ"], ["600000.SH", "000001.SZ"]]
    assert disp.quote_calls == []
    assert len(result) == 1
    assert result[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert set(result[0].stocks.keys()) == {"600000.SH", "000001.SZ"}
    assert result[0].stocks["600000.SH"]["close"] == Decimal("9.32")
    assert result[0].stocks["000001.SZ"]["close"] == Decimal("18.50")
    # bars_by_code 透传与逐只路径一致
    assert set(result[0].bars_by_code.keys()) == {"600000.SH", "000001.SZ"}


def test_batch_empty_result_falls_back_to_per_code():
    """批量返回空({})→ 回退逐只路径,触发行为与逐只完全一致。"""
    disp = BatchDispatcher(
        batches=[{}, {}],
        per_code={"600000.SH": [
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
             _bar("20260805101000", 9.40)],
        ]})
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()
    result = poller.poll()

    assert disp.quote_calls == ["600000.SH", "600000.SH"]
    assert len(result) == 1
    assert result[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert result[0].stocks["600000.SH"]["close"] == Decimal("9.32")


def test_batch_exception_falls_back_and_warns_once(caplog):
    """批量抛异常 → 回退逐只;回退 warning 只记一次(防刷屏)。"""
    import logging
    disp = BatchDispatcher(
        batches=[RuntimeError("batch boom"), RuntimeError("batch boom")],
        per_code={"600000.SH": [
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32)],
            [_bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
             _bar("20260805101000", 9.40)],
        ]})
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    with caplog.at_level(logging.WARNING, logger="core.engine.bar_poller"):
        poller.poll()
        poller.poll()

    assert len(fired) == 1
    assert fired[0].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    assert disp.quote_calls == ["600000.SH", "600000.SH"]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1


# ---------------- 显式闭合数据源（快照合成） ----------------
class ClosedBarDispatcher(BatchDispatcher):
    """声明「返回的每一根都已是闭合 bar」的假数据源（SnapshotQuoteDispatcher 同形）。"""

    bars_explicitly_completed = True


def test_explicit_closed_source_triggers_latest_bar_immediately():
    """数据源声明全闭合时，最新一根闭合 bar 本轮立即触发（不再等下一根）。"""
    base = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32)]}
    nxt = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
        _bar("20260805101000", 9.40)]}
    disp = ClosedBarDispatcher(batches=[base, nxt])
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                        # 基线：last_completed=10:09，不触发
    assert fired == []
    result = poller.poll()               # 10:10 已闭合 → 本轮立即触发

    assert len(result) == 1
    # bar_time 语义不变：仍为区间终点（10:10:00），不是区间起点
    assert result[0].bar_time == datetime(2026, 8, 5, 10, 10, 0)
    assert result[0].stocks["600000.SH"]["close"] == Decimal("9.40")


def test_explicit_closed_source_no_duplicate_when_window_repeats():
    """同一闭合 bar 在后续轮次重复返回 → 不重复触发（(周期, bar_time) 幂等）。"""
    base = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32)]}
    nxt = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
        _bar("20260805101000", 9.40)]}
    disp = ClosedBarDispatcher(batches=[base, nxt, nxt, nxt])
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                        # 基线
    poller.poll()                        # 10:10 触发一次
    poller.poll()                        # 窗口重复，不触发
    poller.poll()                        # 窗口重复，不触发

    assert [b.bar_time for b in fired] == [datetime(2026, 8, 5, 10, 10, 0)]
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 10, 0)


def test_explicit_closed_source_empty_round_does_not_trigger_or_advance():
    """某轮取不到数据（空/批量异常回退空）→ 不触发、水位不变，下一轮正常。"""
    base = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32)]}
    nxt = {"600000.SH": [
        _bar("20260805100800", 9.31), _bar("20260805100900", 9.32),
        _bar("20260805101000", 9.40)]}
    disp = ClosedBarDispatcher(batches=[base, {}, nxt])
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.poll()                        # 基线
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 9, 0)

    poller.poll()                        # 空轮次：无 bar → 不触发、水位不变
    assert fired == []
    assert poller.last_completed_stime == datetime(2026, 8, 5, 10, 9, 0)

    poller.poll()                        # 恢复：10:10 正常触发
    assert [b.bar_time for b in fired] == [datetime(2026, 8, 5, 10, 10, 0)]


# ---------------- 行情轮询窗口(收盘后停拉行情) ----------------
def test_quote_polling_window_defaults_when_config_missing():
    """缺键/None → 安全默认 09:15–15:05(左闭右开):收盘后停轮询。"""
    w = QuotePollingWindow.from_config(None)
    assert w.enabled is True
    assert w.resume_time == DEFAULT_QUOTE_POLL_RESUME == time(9, 15)
    assert w.pause_time == DEFAULT_QUOTE_POLL_PAUSE == time(15, 5)
    assert w.is_open(datetime(2026, 8, 5, 9, 14, 59)) is False
    assert w.is_open(datetime(2026, 8, 5, 9, 15)) is True
    assert w.is_open(datetime(2026, 8, 5, 15, 4, 59)) is True
    assert w.is_open(datetime(2026, 8, 5, 15, 5)) is False
    assert w.is_open(datetime(2026, 8, 5, 23, 0)) is False


def test_quote_polling_window_can_be_disabled():
    """enabled=false → 永远开放(旧行为:全天轮询)。"""
    w = QuotePollingWindow.from_config({"enabled": False})
    assert w.is_open(datetime(2026, 8, 8, 3, 0)) is True


def test_quote_polling_window_parses_times_and_falls_back():
    """兼容 '0915' / '15:05' 写法;非法值回落安全默认。"""
    w = QuotePollingWindow.from_config({"resume_time": "0915", "pause_time": "15:06"})
    assert w.resume_time == time(9, 15)
    assert w.pause_time == time(15, 6)
    bad = QuotePollingWindow.from_config({"resume_time": "abc", "pause_time": ""})
    assert bad.resume_time == DEFAULT_QUOTE_POLL_RESUME
    assert bad.pause_time == DEFAULT_QUOTE_POLL_PAUSE


def test_quote_polling_next_resume_skips_weekend():
    """暂停日志的"下次恢复"提示:周五收盘后 → 下一交易日(周一)09:15。"""
    w = QuotePollingWindow.from_config(None)
    nxt = w.next_resume_at(
        datetime(2026, 8, 7, 15, 6), lambda d: d.weekday() < 5)
    assert nxt == datetime(2026, 8, 10, 9, 15)


class _SeqDispatcher:
    """按顺序返回固定 bar 窗口的最小 dispatcher(无批量能力)。"""

    def __init__(self, bars_by_code):
        self.bars_by_code = bars_by_code
        self.calls = 0

    def query_quote(self, code, period="1m", count=10, **kwargs):
        self.calls += 1
        return list(self.bars_by_code.get(code) or [])


def test_resume_guard_baselines_new_code_and_drops_overnight_bars():
    """暂停→恢复:无水位新代码只建基线;挂水位代码的隔夜 bar 不触发。"""
    # 昨日尾盘:14:58 / 14:59 / 15:00(最新一根按相对变化规则算进行中)
    yday = [
        _bar("20260804145800", 9.1),
        _bar("20260804145900", 9.2),
        _bar("20260804150000", 9.3),
    ]
    disp = _SeqDispatcher({"600000.SH": yday, "000001.SZ": yday})
    poller = BarPoller(disp, ["600000.SH", "000001.SZ"], period="1m", count=10)
    poller._initialized = True
    poller._last_completed = {"600000.SH": datetime(2026, 8, 4, 14, 57),
                              "000001.SZ": None}
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.mark_resume_boundary(datetime(2026, 8, 5).date())
    poller.poll()   # 恢复首轮:窗口里全是昨日 bar
    assert fired == []                                        # 隔夜 bar 未当盘中触发
    assert poller._last_completed["000001.SZ"] == datetime(2026, 8, 4, 14, 59)  # 建基线

    poller.poll()   # 恢复后第二轮:昨日 bar 仍不触发(_resume_day 过滤保留)
    assert fired == []

    # 今日首根完成(10:00，被 10:01 顶成已完成) → 正常触发
    today_bars = yday[1:] + [_bar("20260805100000", 9.4), _bar("20260805100100", 9.5)]
    disp.bars_by_code = {"600000.SH": today_bars, "000001.SZ": today_bars}
    poller.poll()
    assert [b.bar_time for b in fired] == [datetime(2026, 8, 5, 10, 0, 0)]


def test_resume_guard_survives_empty_first_poll():
    """恢复首轮拉空（09:15 开盘前 1m 窗口为空）→ 守护保留到首批真实 bar。

    若空轮次就清守护，09:31 首批 bar 到来时「暂停期扩池、无水位」的代码会把整个
    count 窗口（09:22–09:30）当盘中补触发——旧价格 + 现价成交，正是要防的副作用。
    """
    disp = _SeqDispatcher({"600000.SH": []})
    poller = BarPoller(disp, ["600000.SH"], period="1m", count=10)
    poller._initialized = True
    fired = []
    poller.on_bar = lambda bar: fired.append(bar)

    poller.mark_resume_boundary(datetime(2026, 8, 5).date())
    poller.poll()                        # 09:15 空窗口：无任何数据
    assert fired == []
    assert poller._resume_guard is True  # 空轮次不清守护

    today_bars = [_bar("20260805092200", 9.1), _bar("20260805092300", 9.1),
                  _bar("20260805093000", 9.2), _bar("20260805093100", 9.2)]
    disp.bars_by_code = {"600000.SH": today_bars}
    poller.poll()                        # 09:31 首批真实 bar：新代码只建基线
    assert fired == []
    assert poller._last_completed["600000.SH"] == datetime(2026, 8, 5, 9, 30)
    assert poller._resume_guard is False  # 有数据后守护一次性清除

