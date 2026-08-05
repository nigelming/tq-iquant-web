"""BarPoller 单测（0009 切片3）— httpx MockTransport 模拟桥 /quote 响应。

验证行情通道核心逻辑：
  - 拉 /quote → 解析 stime（yyyymmddHHMMSS，bar 结束时间）
  - bar 完成检测：stime <= now 的已完成 bar 才触发；stime > now 的进行中 bar 忽略
  - 只触发 > last_bar_time 的新已完成 bar（避免重复触发同一 bar）
  - 多股票同一 bar 时间合并为一根 BarEvent.stocks
  - 桥离线抛 BridgeUnavailableError（不吞异常，交上层暂停交易）

桥 /quote 返回格式（live/bridge/iquant_bridge.py get_quote）：
  {"ok": True, "data": {code: [bar, ...]}}
每 bar = DataFrame reset_index().to_dict("records")，字段含
stime/open/high/low/close/volume/amount 等。
"""
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from core.engine.bar_poller import BarPoller, parse_bar_time
from core.engine.event import BarEvent
from core.engine.http_bridge_dispatcher import BridgeUnavailableError
from tq_iquant_shared.constants import SignalType, TradeType


# ---------------- 工具 ----------------
def _bar(stime, close, open_=None, high=None, low=None, volume=10000):
    """构造桥 /quote 返回的单根 bar dict（stime = yyyymmddHHMMSS bar 结束时间）。"""
    c = float(close)
    return {
        "stime": stime,
        "open": float(open_) if open_ is not None else c,
        "high": float(high) if high is not None else c,
        "low": float(low) if low is not None else c,
        "close": c,
        "volume": volume,
    }


def _make_quote_response(bars_by_code):
    """构造桥 /quote 的 JSON 响应体：{code: [bar,...]}。"""
    return httpx.Response(200, json={"ok": True, "data": bars_by_code})


class _QuoteRecorder:
    """MockTransport handler：按配置返回 /quote，记录请求，可选失败。"""

    def __init__(self, bars_by_code=None, fail=False):
        self.bars_by_code = bars_by_code or {}
        self.fail = fail
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/quote":
            return _make_quote_response(self.bars_by_code)
        if request.url.path == "/ping":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


def _make_poller(recorder, codes=None, period="1m", count=10):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    disp_client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=disp_client)
    return BarPoller(dispatcher=disp, stock_codes=codes or ["600000.SH"],
                     period=period, count=count), recorder


# ---------------- parse_bar_time ----------------
def test_parse_bar_time_yyyymmddhhmmss():
    """stime '20260805100800' → datetime(2026,8,5,10,8,0)。"""
    assert parse_bar_time("20260805100800") == datetime(2026, 8, 5, 10, 8, 0)


def test_parse_bar_time_invalid_returns_none():
    assert parse_bar_time("") is None
    assert parse_bar_time("abc") is None
    assert parse_bar_time(None) is None


# ---------------- bar 完成检测 ----------------
def test_new_bar_triggered():
    """已完成 bar（stime <= now）且 > last_bar_time → 触发回调，BarEvent 含 OHLCV。"""
    # 已完成 bar：10:08:00 结束，当前 10:09:30 → stime <= now
    bars = [_bar("20260805100800", 9.32, open_=9.30, high=9.35, low=9.28, volume=12000)]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars})
    poller, _ = _make_poller(rec)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    assert len(fired) == 1
    bar = fired[0]
    assert isinstance(bar, BarEvent)
    assert bar.bar_time == datetime(2026, 8, 5, 10, 8, 0)
    assert "600000.SH" in bar.stocks
    s = bar.stocks["600000.SH"]
    assert s["open"] == Decimal("9.30")
    assert s["close"] == Decimal("9.32")
    assert s["high"] == Decimal("9.35")
    assert s["low"] == Decimal("9.28")
    assert s["volume"] == 12000
    # last_bar_time 推进到该 bar
    assert poller.last_bar_time == datetime(2026, 8, 5, 10, 8, 0)


def test_in_progress_bar_ignored():
    """进行中 bar（stime > now）→ 不触发回调，last_bar_time 不变。"""
    # 最新 bar 10:10:00 结束，但当前才 10:09:30 → 还在进行中
    bars = [_bar("20260805101000", 9.40)]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars})
    poller, _ = _make_poller(rec)
    poller.last_bar_time = datetime(2026, 8, 5, 10, 8, 0)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    assert fired == []
    assert poller.last_bar_time == datetime(2026, 8, 5, 10, 8, 0)


def test_only_beyond_last_bar_triggered():
    """多根已完成 bar，只触发 > last_bar_time 的；已触发的 bar 不重复。"""
    bars = [
        _bar("20260805100700", 9.31),   # <= last_bar_time，旧 bar，跳过
        _bar("20260805100800", 9.32),   # > last_bar_time，新已完成 bar，触发
        _bar("20260805100900", 9.35),   # > last_bar_time，新已完成 bar，触发
        _bar("20260805101000", 9.40),   # > now，进行中，跳过
    ]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars})
    poller, _ = _make_poller(rec)
    poller.last_bar_time = datetime(2026, 8, 5, 10, 7, 0)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    # 触发 2 根（10:08、10:09），10:07 旧、10:10 进行中
    assert len(fired) == 2
    assert fired[0].bar_time == datetime(2026, 8, 5, 10, 8, 0)
    assert fired[1].bar_time == datetime(2026, 8, 5, 10, 9, 0)
    # last_bar_time 推进到最后一根已完成 bar
    assert poller.last_bar_time == datetime(2026, 8, 5, 10, 9, 0)


def test_no_trigger_when_all_bars_old():
    """所有 bar 都 <= last_bar_time → 不触发。"""
    bars = [_bar("20260805100700", 9.31), _bar("20260805100800", 9.32)]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars})
    poller, _ = _make_poller(rec)
    poller.last_bar_time = datetime(2026, 8, 5, 10, 8, 0)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    assert fired == []


# ---------------- 多股票合并 ----------------
def test_multiple_stocks_merged_into_one_bar_event():
    """多股票同一 bar 时间 → 合并为一根 BarEvent.stocks（多 code）。"""
    bars_a = [_bar("20260805100800", 9.32)]
    bars_b = [_bar("20260805100800", 18.50, open_=18.40, high=18.60, low=18.35)]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars_a, "000001.SZ": bars_b})
    poller, _ = _make_poller(rec, codes=["600000.SH", "000001.SZ"])

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    assert len(fired) == 1
    bar = fired[0]
    assert bar.bar_time == datetime(2026, 8, 5, 10, 8, 0)
    assert set(bar.stocks.keys()) == {"600000.SH", "000001.SZ"}
    assert bar.stocks["600000.SH"]["close"] == Decimal("9.32")
    assert bar.stocks["000001.SZ"]["close"] == Decimal("18.50")


def test_multiple_stocks_different_times_trigger_separate_bars():
    """多股票 bar 时间不一致 → 按各自时间分别触发（时间轴合并去重）。"""
    bars_a = [_bar("20260805100800", 9.32)]
    bars_b = [_bar("20260805100900", 18.50)]
    rec = _QuoteRecorder(bars_by_code={"600000.SH": bars_a, "000001.SZ": bars_b})
    poller, _ = _make_poller(rec, codes=["600000.SH", "000001.SZ"])

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    # 两根 bar，时间不同，各自触发
    assert len(fired) == 2
    times = sorted(b.bar_time for b in fired)
    assert times == [datetime(2026, 8, 5, 10, 8, 0), datetime(2026, 8, 5, 10, 9, 0)]
    # 10:08 那根只含 600000，10:09 那根只含 000001
    by_time = {b.bar_time: set(b.stocks.keys()) for b in fired}
    assert by_time[datetime(2026, 8, 5, 10, 8, 0)] == {"600000.SH"}
    assert by_time[datetime(2026, 8, 5, 10, 9, 0)] == {"000001.SZ"}


# ---------------- 桥离线 ----------------
def test_bridge_offline_raises():
    """桥不可用 → BridgeUnavailableError，不吞异常。"""
    rec = _QuoteRecorder(fail=True)
    poller, _ = _make_poller(rec)

    with pytest.raises(BridgeUnavailableError):
        poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))


# ---------------- 空数据 ----------------
def test_empty_quote_no_trigger():
    """桥返回空 bar 列表 → 不触发，last_bar_time 不变。"""
    rec = _QuoteRecorder(bars_by_code={"600000.SH": []})
    poller, _ = _make_poller(rec)

    fired = []
    poller.on_bar = lambda bar: fired.append(bar)
    poller.poll(now=datetime(2026, 8, 5, 10, 9, 30))

    assert fired == []
