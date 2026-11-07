import asyncio
from datetime import datetime
from types import SimpleNamespace

from core.engine.event import BarEvent
from core.engine.live_engine import LiveEngine


class FakePoller:
    def __init__(self):
        self.calls = 0

    def poll(self):
        self.calls += 1


class FakeDispatcher:
    channel = "alert"

    def __init__(self):
        self.calls = []

    def heartbeat(self):
        self.calls.append("heartbeat")
        return True

    def query_quote(self, *a, **kw):
        self.calls.append("quote")
        return []

    def query_calendar(self):
        return []

    def get_positions(self):
        self.calls.append("positions")
        return []

    def query_orders(self):
        self.calls.append("orders")
        return []

    def query_deals(self):
        self.calls.append("deals")
        return []

    def close(self):
        self.calls.append("close")


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def evaluate(self, *a, **kw):
        self.calls.append((a, kw))


class FakeScheduler:
    def __init__(self):
        self.calls = 0

    def due(self, now):
        self.calls += 1
        return []

    def execute(self, batch):
        return None


def _engine(monitor=None, scheduler=None):
    disp = FakeDispatcher()
    eng = LiveEngine(
        session_id=1, portfolios=[], dispatcher=disp,
        bar_poller=FakePoller(), db_session_factory=lambda: None,
        execution_mode="alert", signal_monitor=monitor,
        signal_scheduler=scheduler or FakeScheduler(),
    )
    return eng, disp


def test_tick_alert_does_not_touch_broker_or_orders():
    mon = FakeMonitor()
    sched = FakeScheduler()
    eng, disp = _engine(mon, sched)
    eng._tick_alert(datetime(2026, 10, 26, 9, 30))
    assert "positions" not in disp.calls
    assert "orders" not in disp.calls
    assert "deals" not in disp.calls
    assert sched.calls == 1


def test_handle_alert_bar_fills_and_notifies_without_orders(monkeypatch):
    mon = FakeMonitor()
    eng, _ = _engine(mon)
    called = {"fill": 0, "on_bar": 0}

    def fake_fill(portfolio, bar, **kw):
        called["fill"] += 1

    def fake_on_bar(*a, **kw):
        called["on_bar"] += 1
        raise AssertionError("alert must not call Portfolio.on_bar")

    monkeypatch.setattr(eng.market_data, "fill_signal_cache", fake_fill)
    port = SimpleNamespace(portfolio_id=1, strategies=[], on_bar=fake_on_bar)
    bar = BarEvent(stocks={"000001.SZ": {"close": 12.3}},
                   bar_time=datetime(2026, 10, 26, 14, 30), period="1d")
    eng._handle_alert_bar(port, bar)
    assert called == {"fill": 1, "on_bar": 0}
    assert mon.calls


# ---------------- TQ 1.3.1：stop() 释放 dispatcher 资源 ----------------
def test_stop_calls_dispatcher_close():
    """stop() 在 executor shutdown 后调用 dispatcher.close()（退订对账入口）。"""
    eng, disp = _engine()
    asyncio.run(eng.stop())
    assert "close" in disp.calls


def test_stop_swallows_dispatcher_close_error():
    """close() 抛异常 → 吞并记 warning，不阻断停止流程。"""
    eng, disp = _engine()

    def boom():
        raise RuntimeError("close failed")

    disp.close = boom
    asyncio.run(eng.stop())  # 不抛


def test_stop_tolerates_dispatcher_without_close():
    """无 close 的通道（如 QMT HttpBridgeDispatcher）→ 跳过，不抛。"""
    disp = FakeDispatcher()
    disp.close = None  # 模拟无 close 的通道
    eng = LiveEngine(
        session_id=1, portfolios=[], dispatcher=disp,
        bar_poller=FakePoller(), db_session_factory=lambda: None,
        execution_mode="alert", signal_scheduler=FakeScheduler(),
    )
    asyncio.run(eng.stop())  # 不抛
