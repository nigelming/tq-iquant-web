"""auto_shutdown 单元测试：关闭信号 + 心跳取消 + 显式退出（monkeypatch os._exit）。"""
import time

import core.services.auto_shutdown as asd


def test_not_armed_all_noop(monkeypatch):
    monkeypatch.setattr(asd, "_armed", False)
    calls: list = []
    monkeypatch.setattr(asd.os, "_exit", lambda code: calls.append(code))
    asd.heartbeat()
    asd.request_close()
    asd.shutdown_now()
    time.sleep(0.3)
    assert calls == []


def test_close_exits_when_no_heartbeat(monkeypatch):
    monkeypatch.setattr(asd, "_armed", True)
    monkeypatch.setattr(asd, "_CLOSE_GRACE", 0.05)
    calls: list = []
    monkeypatch.setattr(asd.os, "_exit", lambda code: calls.append(code))
    asd.heartbeat()
    asd.request_close()
    time.sleep(0.3)
    assert calls == [0]


def test_heartbeat_cancels_pending_close(monkeypatch):
    monkeypatch.setattr(asd, "_armed", True)
    monkeypatch.setattr(asd, "_CLOSE_GRACE", 0.1)
    calls: list = []
    monkeypatch.setattr(asd.os, "_exit", lambda code: calls.append(code))
    asd.heartbeat()
    asd.request_close()
    asd.heartbeat()  # 新标签页心跳 → 取消退出
    time.sleep(0.3)
    assert calls == []


def test_shutdown_now_exits(monkeypatch):
    monkeypatch.setattr(asd, "_armed", True)
    calls: list = []
    monkeypatch.setattr(asd.os, "_exit", lambda code: calls.append(code))
    asd.shutdown_now()
    time.sleep(0.8)
    assert calls == [0]
