"""启动恢复：僵尸 running 实盘会话清理（2026-11-04 部署缺陷回归测试）。

背景：后端重启不会恢复实盘引擎，但 `live_sessions.status` 保留 `running`，
工作台显示「运行中」而 `bridge-status` 返回 `not_running`（当天实证）。
启动扫描在引擎注册表为空时把残留 running 统一置 stopped。
"""
from datetime import datetime
from types import SimpleNamespace

from core.models.live_session import LiveSession
from core.services.live_service import (
    list_sessions,
    recover_stale_running_live_sessions,
)


def _make(db, name: str, status: str, engine: str = "tdx") -> LiveSession:
    s = LiveSession(name=name, mode="live", engine=engine, status=status)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_stale_running_marked_stopped(db_session):
    running = _make(db_session, "重启前残留", "running")
    running.started_at = datetime(2026, 11, 4, 14, 11, 39)
    db_session.commit()

    n = recover_stale_running_live_sessions(db_session, engines={})

    assert n == 1
    db_session.refresh(running)
    assert running.status == "stopped"
    assert running.stopped_at is not None
    # started_at 保留原始真实启动时间（不被恢复扫描改写）
    assert running.started_at == datetime(2026, 11, 4, 14, 11, 39)


def test_non_running_sessions_untouched(db_session):
    stopped = _make(db_session, "已停止", "stopped")
    stopped.stopped_at = datetime(2026, 11, 3, 15, 0, 0)
    db_session.commit()

    n = recover_stale_running_live_sessions(db_session, engines={})

    assert n == 0
    db_session.refresh(stopped)
    assert stopped.status == "stopped"
    assert stopped.stopped_at == datetime(2026, 11, 3, 15, 0, 0)


def test_recovery_is_idempotent(db_session):
    _make(db_session, "残留", "running")

    assert recover_stale_running_live_sessions(db_session, engines={}) == 1
    # 第二次：已无 running，返回 0，状态不被再次改写
    assert recover_stale_running_live_sessions(db_session, engines={}) == 0
    sessions = db_session.query(LiveSession).all()
    assert [s.status for s in sessions] == ["stopped"]


def test_noop_when_engine_registry_not_empty(db_session):
    """非冷启动（注册表有引擎）→ 绝不改写状态，避免误伤真实运行会话。"""
    running = _make(db_session, "真在跑", "running")

    n = recover_stale_running_live_sessions(
        db_session, engines={running.id: object()})

    assert n == 0
    db_session.refresh(running)
    assert running.status == "running"


def test_list_sessions_reports_engine_running(db_session):
    """工作台据此识别「状态陈旧」：status=running 但 engine_running=false。"""
    s = _make(db_session, "残留", "running")
    rows = list_sessions(db_session, engines={})
    row = [r for r in rows if r["id"] == s.id][0]
    assert row["engine_running"] is False

    s.status = "running"
    rows = list_sessions(
        db_session, engines={s.id: SimpleNamespace(reconcile_mismatches=[])})
    row = [r for r in rows if r["id"] == s.id][0]
    assert row["engine_running"] is True
