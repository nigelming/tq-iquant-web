"""进程内引擎存活监控（EngineWatchdog）单测。

覆盖：启动/停止、空注册表、引擎卡死告警去重与恢复、启动宽限、盘外不误报、
ths 文件交接层积压与恢复、桥不可达、tdx bar 停滞、配置非法回退默认。
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.engine.live.engine_watchdog import (
    EngineWatchdog, config_seconds, current_watchdog,
)

NOW = datetime(2026, 11, 5, 10, 30, 0)


class _Dispatcher:
    def __init__(self, channel):
        self.channel = channel


class _Engine:
    def __init__(self, channel="tdx_tq", started=None, tick=None, running=True):
        self.session_id = 1
        self._running = running
        self._started_at = started
        self._last_tick_at = tick
        self._dispatcher = _Dispatcher(channel)
        self._bridge_online = True
        self._available_bar = None
        self._snapshot_synth = None
        self._file_handoff = None
        self.events = []

    def _emit(self, event_type, payload):
        self.events.append({"type": event_type, **payload})


def _watchdog(engines, **kw):
    kw.setdefault("now_fn", lambda: NOW)
    kw.setdefault("mono_fn", lambda: 0.0)
    kw.setdefault("snapshot_fn", lambda sid: None)
    kw.setdefault("in_session_fn", lambda dt: True)
    # 默认关掉引擎层阈值，避免干扰通道探针断言
    kw.setdefault("timeout_seconds", 10 ** 9)
    kw.setdefault("execution_stall_seconds", 180)
    kw.setdefault("bridge_timeout_seconds", 90)
    kw.setdefault("market_stall_seconds", 180)
    kw.setdefault("grace_seconds", 120)
    return EngineWatchdog(lambda: engines, **kw)


def _risk(eng, rule):
    return [e for e in eng.events if e["type"] == "risk" and e.get("rule") == rule]


# ---------------- 生命周期 / 空注册表 ----------------

def test_start_stop_no_exception():
    wd = _watchdog({})
    wd.start()
    wd.stop()


def test_empty_registry_scan_is_safe():
    wd = _watchdog({})
    wd.scan()  # 不抛异常
    assert wd._states == {}


def test_engine_not_running_is_not_probed():
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900),
                  tick=NOW - timedelta(seconds=900), running=False)
    eng._bridge_online = False
    wd = _watchdog({1: eng}, bridge_timeout_seconds=1)
    wd.scan()
    assert eng.events == []


# ---------------- 引擎层心跳 ----------------

def test_stale_engine_alerts_once_then_recovers():
    eng = _Engine(channel="unknown",
                  started=NOW - timedelta(seconds=900),
                  tick=NOW - timedelta(seconds=200))
    wd = _watchdog({1: eng}, timeout_seconds=90)
    wd.scan()
    alerts = _risk(eng, "engine_stale")
    assert len(alerts) == 1
    assert alerts[0]["seconds"] >= 200
    assert "已静默" in alerts[0]["message"]

    wd.scan()  # 同一轮卡死不重复告警
    assert len(_risk(eng, "engine_stale")) == 1

    eng._last_tick_at = NOW  # 恢复
    wd.scan()
    recovered = [e for e in eng.events if e["type"] == "engine_recovered"]
    assert len(recovered) == 1
    assert _risk(eng, "engine_stale") == [alerts[0]]  # 无新增告警


def test_startup_grace_silences_alerts():
    eng = _Engine(channel="unknown", started=NOW - timedelta(seconds=30), tick=None)
    wd = _watchdog({1: eng}, timeout_seconds=5, grace_seconds=120)
    wd.scan()
    assert eng.events == []


def test_out_of_session_does_not_alert_engine_stale():
    eng = _Engine(channel="unknown",
                  started=NOW - timedelta(seconds=900),
                  tick=NOW - timedelta(seconds=300))
    wd = _watchdog({1: eng}, timeout_seconds=90,
                   in_session_fn=lambda dt: False)
    wd.scan()
    assert eng.events == []


# ---------------- 通道探针：ths ----------------

def _write_handoff(tmp_path, lines, offset=0):
    orders = tmp_path / "orders.jsonl"
    orders.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"orders_offset": offset}), encoding="utf-8")
    return SimpleNamespace(orders_path=str(orders), cursor_path=str(cursor))


def test_ths_backlog_stall_alerts_and_recovers(tmp_path):
    eng = _Engine(channel="ths_ui", started=NOW - timedelta(seconds=900), tick=NOW)
    old = (NOW - timedelta(seconds=600)).isoformat(timespec="seconds")
    handoff = _write_handoff(tmp_path, [
        {"order_id": "a", "ts": old},
        {"order_id": "b", "ts": old},
    ], offset=0)
    eng._file_handoff = handoff
    wd = _watchdog({1: eng})
    wd.scan()
    alerts = _risk(eng, "execution_stalled")
    assert len(alerts) == 1
    assert "积压 2 行" in alerts[0]["message"]
    assert alerts[0]["backlog"] == 2

    # cursor 推进到 2 → 不再积压 → 恢复事件
    Path(handoff.cursor_path).write_text(
        json.dumps({"orders_offset": 2}), encoding="utf-8")
    wd.scan()
    recovered = [e for e in eng.events if e["type"] == "execution_recovered"]
    assert len(recovered) == 1


def test_ths_backlog_alerts_out_of_session(tmp_path):
    """ths 积压不受交易时段限制：盘外仍告警。"""
    eng = _Engine(channel="ths_ui", started=NOW - timedelta(seconds=900), tick=NOW)
    old = (NOW - timedelta(seconds=600)).isoformat(timespec="seconds")
    eng._file_handoff = _write_handoff(tmp_path, [{"order_id": "a", "ts": old}])
    wd = _watchdog({1: eng}, in_session_fn=lambda dt: False)
    wd.scan()
    assert len(_risk(eng, "execution_stalled")) == 1


# ---------------- 通道探针：桥 ----------------

def test_bridge_unreachable_alerts_after_timeout():
    mono = [0.0]
    eng = _Engine(channel="goldminer_bridge", started=NOW - timedelta(seconds=900))
    eng._last_tick_at = NOW
    eng._bridge_online = False
    wd = _watchdog({1: eng}, bridge_timeout_seconds=90,
                   mono_fn=lambda: mono[0])
    wd.scan()                     # 起点，wait=0
    assert eng.events == []
    mono[0] = 100.0
    wd.scan()                     # wait=100 > 90
    alerts = _risk(eng, "bridge_unreachable")
    assert len(alerts) == 1
    assert alerts[0]["engine"] == "goldminer_bridge"


def test_tdx_bar_stall_alerts():
    """1m 快照合成真停 200s → 告警（基准为最近已闭合 1m bar）。"""
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=NOW - timedelta(seconds=200))
    wd = _watchdog({1: eng}, market_stall_seconds=180)
    wd.scan()
    alerts = _risk(eng, "market_data_stalled")
    assert len(alerts) == 1
    assert "未推进" in alerts[0]["message"]
    assert alerts[0]["basis"] == "snapshot_1m"


def test_tdx_daily_strategy_with_fresh_1m_snapshot_no_alert():
    """日线策略的 00:00:00 不参与判定：1m 采样新鲜 → 不报（2026-11-05 假警回归）。"""
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._available_bar = SimpleNamespace(
        period="1d", bar_time=NOW.replace(hour=0, minute=0, second=0))
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=NOW - timedelta(seconds=10))
    wd = _watchdog({1: eng}, market_stall_seconds=180)
    wd.scan()
    assert eng.events == []


def test_tdx_daily_bar_time_never_used_as_basis():
    """无合成器时，日线 bar_time=00:00:00 仍不得成为停滞判定基准。"""
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._available_bar = SimpleNamespace(
        period="1d", bar_time=NOW.replace(hour=0, minute=0, second=0))
    wd = _watchdog({1: eng}, market_stall_seconds=180)
    wd.scan()
    assert eng.events == []


def test_tdx_minute_bar_stall_still_alerts_without_synth():
    """无合成器（非 tdx 盘官方路径）：分钟级 bar 停滞照旧告警。"""
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._available_bar = SimpleNamespace(
        period="1m", bar_time=NOW - timedelta(seconds=600))
    wd = _watchdog({1: eng}, market_stall_seconds=180)
    wd.scan()
    assert len(_risk(eng, "market_data_stalled")) == 1


def test_tdx_bar_stall_not_reported_out_of_session():
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=NOW - timedelta(seconds=600))
    wd = _watchdog({1: eng}, in_session_fn=lambda dt: False)
    wd.scan()
    assert eng.events == []


def test_lunch_break_gap_not_counted_as_stall():
    """13:00 开盘首分钟：午休 11:30–13:00 不算停滞（2026-11-05 13:00 假警回归）。"""
    lunch_now = datetime(2026, 11, 5, 13, 0, 2)
    eng = _Engine(channel="tdx_tq",
                  started=lunch_now - timedelta(hours=8), tick=lunch_now)
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=datetime(2026, 11, 5, 11, 41, 0))
    wd = _watchdog({1: eng}, now_fn=lambda: lunch_now)
    wd.scan()
    assert eng.events == []


def test_overnight_gap_not_counted_as_stall():
    """09:30 开盘首分钟：隔夜静默从当日开盘起算，不报。"""
    morning = datetime(2026, 11, 5, 9, 31, 30)
    eng = _Engine(channel="tdx_tq",
                  started=morning - timedelta(hours=1), tick=morning)
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=datetime(2026, 11, 4, 14, 59, 0))
    wd = _watchdog({1: eng}, now_fn=lambda: morning)
    wd.scan()
    assert eng.events == []


def test_market_data_recovery_clears_active_alarms():
    """误报解除后 active_alarms 必须清空（面板不挂红标）。"""
    eng = _Engine(channel="tdx_tq", started=NOW - timedelta(seconds=900), tick=NOW)
    eng._snapshot_synth = SimpleNamespace(
        last_completed_at=NOW - timedelta(seconds=600))
    wd = _watchdog({1: eng}, market_stall_seconds=180)
    wd.scan()
    assert wd.status()["active_alarms"] == [
        {"session_id": 1, "rule": "market_data_stalled"}]

    eng._snapshot_synth.last_completed_at = NOW
    wd.scan()
    assert wd.status()["active_alarms"] == []
    assert any(e["type"] == "market_data_recovered" for e in eng.events)


# ---------------- 自身可观测性 ----------------

def test_scan_advances_status_counters(caplog):
    now = [NOW]
    eng = _Engine(channel="unknown",
                  started=NOW - timedelta(seconds=900), tick=NOW)
    wd = _watchdog({1: eng}, now_fn=lambda: now[0])
    assert wd.status()["scan_count"] == 0

    wd.scan()
    st = wd.status()
    assert st["scan_count"] == 1
    assert st["last_scan_at"] == NOW.isoformat()
    assert st["running"] is False  # 未 start()，线程不在跑

    now[0] = NOW + timedelta(seconds=30)
    with caplog.at_level(logging.DEBUG):
        wd.scan()
    st2 = wd.status()
    assert st2["scan_count"] == 2
    assert st2["last_scan_at"] != st["last_scan_at"]
    assert any("engine watchdog scan: sessions=1" in r.getMessage()
               for r in caplog.records)


def test_current_watchdog_cleared_after_stop():
    wd = _watchdog({})
    wd.start()
    assert current_watchdog() is wd
    assert wd.status()["running"] is True
    wd.stop()
    assert current_watchdog() is None
    assert wd.status()["running"] is False


def test_watchdog_status_endpoint_without_watchdog():
    """watchdog 未启动时接口返回 running=false，不抛 500。"""
    from core.api.live import watchdog_status
    assert current_watchdog() is None
    resp = watchdog_status()
    assert resp["code"] == 0
    assert resp["data"]["running"] is False
    assert resp["data"]["scan_count"] == 0
    assert resp["data"]["active_alarms"] == []


def test_config_seconds_invalid_falls_back_to_default():
    assert config_seconds(
        "heartbeat_timeout_seconds", 90,
        {"live": {"heartbeat_timeout_seconds": "abc"}}) == 90
    assert config_seconds(
        "heartbeat_timeout_seconds", 90,
        {"live": {"heartbeat_timeout_seconds": -5}}) == 90
    assert config_seconds(
        "heartbeat_timeout_seconds", 90,
        {"live": {"heartbeat_timeout_seconds": 45}}) == 45
