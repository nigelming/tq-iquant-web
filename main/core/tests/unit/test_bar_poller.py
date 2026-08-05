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
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from core.engine.bar_poller import BarPoller, parse_bar_time
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
