"""实盘引擎存活监控（进程内）+ 通道级停滞探针。

两层信号：

- **引擎层**：``engine._last_tick_at``（``_tick_main`` 每轮起始刷新）+ 
  ``live_account_snapshots`` 最新 ``captured_at``（deals 池节拍落库）。抓
  「进程还在、引擎却不动」（线程池饿死/死锁）。
- **通道级**（按 ``dispatcher.channel`` 注册探针）：
  - ``tdx_tq``：行情新鲜度停滞（优先用快照合成器最近一根已闭合 1m bar；
    日线/周线/月线 bar 的 ``00:00:00`` 不参与判定）/ ``_bridge_online=False``；
  - ``ths_ui``：文件交接层 orders 积压未被 cursor 消费、cursor 长时间不推进、
    results 回执未消费（**不受交易时段限制**）；
  - ``goldminer_bridge``：``engine._bridge_online=False`` 持续超过阈值。

**不覆盖进程级崩溃**——进程消失后本线程一并消失，需外部 watchdog 兜底
（``scripts/ops/watch_live_backend.ps1`` + Windows 计划任务）。

阈值（config.yaml ``live.*``，非法/非正回退默认）：
``heartbeat_timeout_seconds``=90、``execution_stall_seconds``=180、
``bridge_timeout_seconds``=90、``market_data_stall_seconds``=180。

告警节流：同一 (会话, 规则) 一轮停滞只报一次，持续停滞每 ``repeat_seconds``
（默认 300s）最多重复一次；恢复后发 ``{rule}_recovered`` 事件并重置。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as _dtime
from typing import Callable, Dict, Optional

from sqlalchemy import func

from core.config import load_config
from core.db import SessionLocal
from core.models import LiveAccountSnapshot

from .snapshot_bars import default_in_session
from .timing import now_shanghai

logger = logging.getLogger(__name__)

# 进程内当前启动的 watchdog（只读接口用；未启动/已停止为 None）
_ACTIVE_WATCHDOG = None


def current_watchdog():
    """返回当前进程内启动的 EngineWatchdog（无则 None）。"""
    return _ACTIVE_WATCHDOG

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 90.0
DEFAULT_EXECUTION_STALL_SECONDS = 180.0
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 90.0
DEFAULT_MARKET_STALL_SECONDS = 180.0
DEFAULT_SCAN_INTERVAL_SECONDS = 30.0
DEFAULT_GRACE_SECONDS = 120.0
DEFAULT_REPEAT_SECONDS = 300.0

#: 规则 → 恢复事件名（``xxx_recovered``）
_RECOVERY_EVENT = {
    "engine_stale": "engine_recovered",
    "execution_stalled": "execution_recovered",
    "bridge_unreachable": "bridge_recovered",
    "market_data_stalled": "market_data_recovered",
}

#: 日线及以上周期（bar_time 为 00:00:00，不具备分钟级行情新鲜度语义）
_LONG_PERIODS = frozenset({"1d", "1w", "1mon", "day", "week", "month"})

#: 连续竞价边界（用于把「墙钟静默」折算成「有效交易静默」）
_AM_OPEN = _dtime(9, 30)
_LUNCH_START = _dtime(11, 30)
_LUNCH_END = _dtime(13, 0)


def _tradable_seconds(start: datetime, end: datetime) -> float:
    """start→end 的有效交易时长：扣除午休 11:30–13:00；隔夜从当日开盘起算。

    午休/隔夜不是行情停滞（13:00 开盘首根 pm 1m bar 要到 13:01 才闭合，
    09:30 开盘首根同样如此），直接用墙钟相减会在每天开盘后必报假警
    （2026-11-05 13:00:02 实证：last bar=11:41:18，静默 4724 秒）。
    """
    if start >= end:
        return 0.0
    if start.date() != end.date():
        start = datetime.combine(end.date(), _AM_OPEN)
        if start >= end:
            return 0.0
    total = (end - start).total_seconds()
    lunch_s = datetime.combine(end.date(), _LUNCH_START)
    lunch_e = datetime.combine(end.date(), _LUNCH_END)
    overlap = max(0.0, (min(end, lunch_e) - max(start, lunch_s)).total_seconds())
    return total - overlap


def config_seconds(key: str, default: float, cfg: Optional[dict] = None) -> float:
    """读 ``live.<key>`` 秒数；缺省/非法/非正数回退 ``default``。"""
    cfg = load_config() if cfg is None else cfg
    raw = (cfg.get("live") or {}).get(key) if isinstance(cfg, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def heartbeat_timeout_seconds(cfg: Optional[dict] = None) -> float:
    return config_seconds(
        "heartbeat_timeout_seconds", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, cfg)


def latest_snapshot_at(session_id: int) -> Optional[datetime]:
    """该会话 ``live_account_snapshots`` 最新 ``captured_at``；无记录/失败 → None。"""
    db = SessionLocal()
    try:
        return db.query(func.max(LiveAccountSnapshot.captured_at)).filter(
            LiveAccountSnapshot.live_session_id == session_id).scalar()
    finally:
        db.close()


@dataclass
class StallSignal:
    """通道探针命中结果。"""

    rule: str
    seconds: float
    message: str
    detail: dict = field(default_factory=dict)


class EngineWatchdog:
    """进程内引擎卡死 + 通道停滞检测（daemon 线程）。"""

    def __init__(
        self,
        sessions_provider: Callable[[], Dict[int, object]],
        *,
        timeout_seconds: Optional[float] = None,
        execution_stall_seconds: Optional[float] = None,
        bridge_timeout_seconds: Optional[float] = None,
        market_stall_seconds: Optional[float] = None,
        interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        repeat_seconds: float = DEFAULT_REPEAT_SECONDS,
        now_fn: Callable[[], datetime] = now_shanghai,
        mono_fn: Callable[[], float] = time.monotonic,
        snapshot_fn: Callable[[int], Optional[datetime]] = latest_snapshot_at,
        in_session_fn: Callable[[datetime], bool] = default_in_session,
    ):
        self._sessions_provider = sessions_provider
        self._timeout = (heartbeat_timeout_seconds() if timeout_seconds is None
                         else float(timeout_seconds))
        self._exec_stall = (
            config_seconds("execution_stall_seconds", DEFAULT_EXECUTION_STALL_SECONDS)
            if execution_stall_seconds is None else float(execution_stall_seconds))
        self._bridge_timeout = (
            config_seconds("bridge_timeout_seconds", DEFAULT_BRIDGE_TIMEOUT_SECONDS)
            if bridge_timeout_seconds is None else float(bridge_timeout_seconds))
        self._market_stall = (
            config_seconds("market_data_stall_seconds", DEFAULT_MARKET_STALL_SECONDS)
            if market_stall_seconds is None else float(market_stall_seconds))
        self._interval = max(float(interval_seconds), 1.0)
        self._grace = max(float(grace_seconds), 0.0)
        self._repeat = max(float(repeat_seconds), 0.0)
        self._now = now_fn
        self._mono = mono_fn
        self._snapshot = snapshot_fn
        self._in_session = in_session_fn
        # (session_id, rule) -> {"alerted": bool, "last_alert_mono": float}
        self._states: Dict[tuple, dict] = {}
        # 探针用的持续态：cursor 未推进起点 / 桥离线起点 / 回执积压起点
        self._probe_state: Dict[tuple, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # 自身可观测性（只读）：最近一次扫描时间/耗时/轮数 + 当轮统计
        self._scan_count = 0
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_duration_ms: Optional[float] = None
        self._scan_stats: Dict[str, int] = {
            "sessions": 0, "rules_checked": 0, "alarms": 0}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        global _ACTIVE_WATCHDOG
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                _ACTIVE_WATCHDOG = self
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="engine-watchdog", daemon=True)
            self._thread.start()
            _ACTIVE_WATCHDOG = self

    def stop(self, timeout: float = 2.0) -> None:
        global _ACTIVE_WATCHDOG
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        if _ACTIVE_WATCHDOG is self:
            _ACTIVE_WATCHDOG = None

    def status(self) -> dict:
        """只读状态快照（供 /api/live/watchdog-status）。

        ``last_scan_at`` 停止前进 = 扫描线程自身卡死/退出，这是外部可判据。
        """
        active = [
            {"session_id": sid, "rule": rule}
            for (sid, rule), state in list(self._states.items())
            if state.get("alerted")
        ]
        thread = self._thread
        return {
            "running": thread is not None and thread.is_alive(),
            "last_scan_at": (self._last_scan_at.isoformat()
                             if self._last_scan_at else None),
            "last_scan_duration_ms": self._last_scan_duration_ms,
            "scan_count": self._scan_count,
            "active_alarms": active,
            "interval_seconds": self._interval,
            "thresholds": {
                "heartbeat_timeout_seconds": self._timeout,
                "execution_stall_seconds": self._exec_stall,
                "bridge_timeout_seconds": self._bridge_timeout,
                "market_data_stall_seconds": self._market_stall,
            },
        }

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.scan()
            except Exception:  # noqa: BLE001 — 监控自身绝不能拖垮进程
                logger.exception("engine watchdog scan error")

    # ---------------------------------------------------------------- 检测
    def scan(self) -> None:
        """扫一轮：引擎卡死 + 各通道探针；恢复发对应 recovered 事件。"""
        t0 = time.perf_counter()
        now = self._now()
        self._scan_stats = {"sessions": 0, "rules_checked": 0, "alarms": 0}
        sessions = dict(self._sessions_provider() or {})
        for session_id, engine in sessions.items():
            try:
                self._scan_one(now, session_id, engine)
            except Exception:  # noqa: BLE001 — 单会话异常不影响其它会话
                logger.exception(
                    "engine watchdog session scan error (session %s)", session_id)
        self._scan_count += 1
        self._last_scan_at = now
        self._last_scan_duration_ms = (time.perf_counter() - t0) * 1000.0
        # DEBUG 心跳：默认级别不刷屏，排查时临时调级别即可看到扫描线程在转
        logger.debug(
            "engine watchdog scan: sessions=%d rules_checked=%d alarms=%d duration_ms=%.1f",
            self._scan_stats["sessions"], self._scan_stats["rules_checked"],
            self._scan_stats["alarms"], self._last_scan_duration_ms)

    def _scan_one(self, now: datetime, session_id: int, engine: object) -> None:
        if not getattr(engine, "_running", False):
            self._clear_session(session_id)
            return
        started = getattr(engine, "_started_at", None)
        if started is not None and (now - started).total_seconds() < self._grace:
            return  # 预热宽限

        self._scan_stats["sessions"] += 1
        channel = self._channel(engine)
        # 引擎层卡死：仅交易时段判定（无 bar/无快照在盘外是正常）
        if self._in_session(now):
            self._scan_stats["rules_checked"] += 1
            self._check_engine_heartbeat(now, session_id, engine, started, channel)

        # 通道级探针：ths 积压不受时段限制；tdx/bridge 由探针内部各自判定
        self._scan_stats["rules_checked"] += 1
        signal = self._probe(channel, now, session_id, engine)
        if signal is None:
            self._recover_if_alerted(engine, session_id, channel, channel_probe=True)
        else:
            self._maybe_alert(engine, session_id, channel, signal)

    @staticmethod
    def _channel(engine: object) -> str:
        dispatcher = getattr(engine, "_dispatcher", None)
        return str(getattr(dispatcher, "channel", "") or "unknown")

    # ---------------------------------------------------------------- 引擎层
    def _check_engine_heartbeat(self, now: datetime, session_id: int, engine: object,
                                started: Optional[datetime], channel: str) -> None:
        last = self._last_activity(session_id, engine, started)
        if last is None:
            return
        silence = (now - last).total_seconds()
        if silence <= self._timeout:
            self._recover_rule(engine, session_id, channel, "engine_stale")
            return
        self._maybe_alert(engine, session_id, channel, StallSignal(
            rule="engine_stale",
            seconds=silence,
            message=(
                "实盘引擎卡死：会话 %s 已静默 %.0f 秒（最后活动 %s，阈值 %.0f 秒）"
                % (session_id, silence, last.strftime("%H:%M:%S"), self._timeout)),
            detail={"last_activity": last.strftime("%Y-%m-%d %H:%M:%S"),
                    "threshold": int(self._timeout)},
        ))

    def _last_activity(self, session_id: int, engine: object,
                       started: Optional[datetime]) -> Optional[datetime]:
        tick = getattr(engine, "_last_tick_at", None)
        snap = None
        try:
            snap = self._snapshot(session_id)
        except Exception:  # noqa: BLE001 — DB 抖动不应误判，另一信号仍可用
            logger.warning(
                "engine watchdog: 读取账户快照时间失败 (session %s)", session_id,
                exc_info=True)
        candidates = [v for v in (tick, snap) if v is not None]
        return max(candidates) if candidates else started

    # ---------------------------------------------------------------- 通道探针
    def _probe(self, channel: str, now: datetime, session_id: int,
               engine: object) -> Optional[StallSignal]:
        if channel == "tdx_tq":
            return self._probe_tdx(now, session_id, engine)
        if channel == "ths_ui":
            return self._probe_ths(now, session_id, engine)
        if channel in ("goldminer_bridge", "iquant_bridge"):
            return self._probe_bridge(now, session_id, engine)
        return None  # 未知通道：仅引擎层监控

    def _probe_tdx(self, now: datetime, session_id: int,
                   engine: object) -> Optional[StallSignal]:
        """tdx：行情新鲜度停滞；或行情链路已标离线。

        新鲜度基准优先取快照合成器「最近一根已闭合 1m bar」（tdx/ths/alert 的
        盘中数据源）。**策略周期 bar（1d/1w/1mon）的 00:00:00 不参与判定**——
        直接与墙钟相减会在每天开盘后必报假警（2026-11-05 14:31 实证：
        last bar=00:00:00、静默 52278 秒，实际 5s 账户采样与 1m bar 一直在跑）。
        """
        if not self._in_session(now):
            return None  # 盘外无 bar 正常
        if not getattr(engine, "_bridge_online", True):
            started = getattr(engine, "_started_at", None)
            last = getattr(engine, "_last_tick_at", None) or started
            seconds = (_tradable_seconds(last, now) if last
                       else self._market_stall)
            return StallSignal(
                rule="market_data_stalled", seconds=seconds,
                message=("行情链路不可用（bridge_online=False）：会话 %s，"
                         "已持续约 %.0f 秒" % (session_id, seconds)),
                detail={"reason": "market_offline"})
        basis = None
        basis_name = ""
        synth = getattr(engine, "_snapshot_synth", None)
        if synth is not None:
            basis = getattr(synth, "last_completed_at", None)
            basis_name = "snapshot_1m"
            if basis is None:
                # 合成器在跑但尚无已闭合 1m bar：以启动时刻计（宽限已在外层扣）
                basis = getattr(engine, "_started_at", None)
                basis_name = "uptime"
        else:
            bar = getattr(engine, "_available_bar", None)
            period = str(getattr(bar, "period", "") or "").lower()
            if period in _LONG_PERIODS:
                # 日线及以上：bar_time 为 00:00:00，无分钟级新鲜度语义 → 不判定，
                # 由引擎层心跳/桥探针兜底（不臆造告警）。
                return None
            basis = getattr(bar, "bar_time", None)
            basis_name = "bar_%s" % (period or "1m")
            if basis is None:
                basis = getattr(engine, "_started_at", None)
                basis_name = "uptime"
        if basis is None:
            return None
        # 午休/隔夜不计入停滞（否则 13:00 与 09:30 开盘首分钟必报假警）
        silence = _tradable_seconds(basis, now)
        if silence <= self._market_stall:
            return None
        return StallSignal(
            rule="market_data_stalled", seconds=silence,
            message=("tdx 行情停滞：会话 %s 最后行情基准=%s（%s），已 %.0f 秒未推进"
                     "（阈值 %.0f 秒）"
                     % (session_id, basis.strftime("%H:%M:%S"), basis_name, silence,
                        self._market_stall)),
            detail={"last_bar_time": basis.strftime("%Y-%m-%d %H:%M:%S"),
                    "basis": basis_name,
                    "threshold": int(self._market_stall)})

    def _probe_ths(self, now: datetime, session_id: int,
                   engine: object) -> Optional[StallSignal]:
        """ths：orders 积压未消费 / cursor 不推进 / results 回执未消费。

        **不受交易时段限制**——任何时段积压都是异常。
        """
        handoff = getattr(engine, "_file_handoff", None)
        if handoff is None:
            return None
        try:
            lines = self._read_lines(handoff.orders_path)
        except OSError:
            return None
        total = len(lines)
        offset = self._cursor_offset(handoff.cursor_path, total)
        backlog = max(total - offset, 0)
        mono = self._mono()
        cursor_key = (session_id, "ths_cursor")
        if backlog > 0:
            since = self._probe_state.setdefault(cursor_key, mono)
            no_advance = mono - since
        else:
            self._probe_state.pop(cursor_key, None)
            no_advance = 0.0
        oldest_wait = self._oldest_pending_seconds(lines, offset, now)
        # 回执积压（results 新增但引擎未 execution_receipts 弹出）
        dispatcher = getattr(engine, "_dispatcher", None)
        pending_receipts = int(getattr(dispatcher, "pending_receipt_count", 0) or 0)
        receipt_key = (session_id, "ths_receipts")
        if pending_receipts > 0:
            since = self._probe_state.setdefault(receipt_key, mono)
            receipts_wait = mono - since
        else:
            self._probe_state.pop(receipt_key, None)
            receipts_wait = 0.0
        stalled = backlog > 0 and (
            (oldest_wait is not None and oldest_wait > self._exec_stall)
            or no_advance > self._exec_stall)
        if stalled:
            seconds = max(oldest_wait or 0.0, no_advance)
            return StallSignal(
                rule="execution_stalled", seconds=seconds,
                message=("ths 执行停滞：会话 %s orders 积压 %d 行未消费，最老待消费"
                         " %.0f 秒，cursor=%d/%d，%.0f 秒无推进"
                         % (session_id, backlog, oldest_wait or 0.0, offset, total,
                            no_advance)),
                detail={"engine": "ths", "backlog": backlog,
                        "oldest_pending_seconds": int(oldest_wait or 0),
                        "cursor_offset": offset, "orders_total": total})
        if pending_receipts > 0 and receipts_wait > self._exec_stall:
            return StallSignal(
                rule="execution_stalled", seconds=receipts_wait,
                message=("ths 回执积压：会话 %s results 回执 %d 条未消费，已 %.0f 秒"
                         "（cursor=%d/%d）"
                         % (session_id, pending_receipts, receipts_wait, offset, total)),
                detail={"engine": "ths", "backlog": pending_receipts,
                        "reason": "results_unconsumed",
                        "cursor_offset": offset, "orders_total": total})
        return None

    def _probe_bridge(self, now: datetime, session_id: int,
                      engine: object) -> Optional[StallSignal]:
        """HTTP 桥：引擎在跑但心跳已标离线并持续超过阈值。"""
        if not self._in_session(now):
            return None
        if getattr(engine, "_bridge_online", True):
            self._probe_state.pop((session_id, "bridge_offline"), None)
            return None
        mono = self._mono()
        key = (session_id, "bridge_offline")
        since = self._probe_state.setdefault(key, mono)
        wait = mono - since
        if wait <= self._bridge_timeout:
            return None
        return StallSignal(
            rule="bridge_unreachable", seconds=wait,
            message=("交易桥不可达：会话 %s 引擎运行中但 bridge_online=False，"
                     "已持续 %.0f 秒（阈值 %.0f 秒）"
                     % (session_id, wait, self._bridge_timeout)),
            detail={"engine": self._channel(engine), "threshold": int(self._bridge_timeout)})

    # ---------------------------------------------------------------- 告警/恢复
    def _maybe_alert(self, engine: object, session_id: int, channel: str,
                     signal: StallSignal) -> None:
        key = (session_id, signal.rule)
        state = self._states.setdefault(
            key, {"alerted": False, "last_alert_mono": 0.0})
        mono = self._mono()
        if state["alerted"] and mono - state["last_alert_mono"] < self._repeat:
            return
        state["alerted"] = True
        state["last_alert_mono"] = mono
        self._scan_stats["alarms"] += 1
        payload = {
            "session_id": session_id,
            "engine": channel,
            "rule": signal.rule,
            "seconds": int(signal.seconds),
            "count": int(signal.detail.get("backlog", 1) or 1),
            "message": signal.message,
        }
        payload.update(signal.detail)
        logger.error("%s", signal.message)
        self._safe_emit(engine, "risk", payload)

    def _recover_rule(self, engine: object, session_id: int, channel: str,
                      rule: str) -> None:
        key = (session_id, rule)
        state = self._states.pop(key, None)
        if not state or not state.get("alerted"):
            return
        name = "%s（%s）" % (session_id, channel)
        message = "实盘监控恢复：会话 %s 的 %s 已恢复正常" % (name, rule)
        logger.info("%s", message)
        self._safe_emit(engine, _RECOVERY_EVENT.get(rule, rule + "_recovered"), {
            "session_id": session_id,
            "engine": channel,
            "rule": _RECOVERY_EVENT.get(rule, rule + "_recovered"),
            "message": message,
        })

    def _recover_if_alerted(self, engine: object, session_id: int, channel: str,
                            channel_probe: bool) -> None:
        """通道探针未命中：清该会话所有通道类规则的告警态并各发一次恢复。"""
        for rule in ("execution_stalled", "bridge_unreachable",
                     "market_data_stalled"):
            self._recover_rule(engine, session_id, channel, rule)

    def _clear_session(self, session_id: int) -> None:
        for key in [k for k in self._states if k[0] == session_id]:
            self._states.pop(key, None)
        for key in [k for k in self._probe_state if k[0] == session_id]:
            self._probe_state.pop(key, None)

    @staticmethod
    def _safe_emit(engine: object, event_type: str, payload: dict) -> None:
        emit = getattr(engine, "_emit", None)
        if emit is None:
            return
        try:
            emit(event_type, payload)
        except Exception:  # noqa: BLE001 — SSE 队列异常不影响监控与交易
            logger.exception("engine watchdog emit error (%s)", event_type)

    # ---------------------------------------------------------------- 文件读取
    @staticmethod
    def _read_lines(path: str) -> list:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()

    @staticmethod
    def _cursor_offset(path: str, total: int) -> int:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            offset = max(int(data.get("orders_offset") or 0), 0)
        except (OSError, ValueError, TypeError):
            return 0
        return 0 if offset > total else offset

    @staticmethod
    def _oldest_pending_seconds(lines: list, offset: int,
                                now: datetime) -> Optional[float]:
        for raw in lines[offset:]:
            text = raw.strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except (ValueError, TypeError):
                continue
            ts = entry.get("ts")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            return max((now - dt).total_seconds(), 0.0)
        return None
