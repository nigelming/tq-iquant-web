"""实盘 service 层 — 业务逻辑（从 core.api.live 提取）。

与 core.services.backtest_service 同一范式：路由层仅留 HTTP 壳（参数校验 +
守卫/调度 + 调 service + ok/err 包装），业务逻辑下沉到本模块。本模块承接：

- 引擎组装：build_engine（查 link/PS/strategy/股票池/Formula，算 code_period_count，
  建 dispatcher/poller/LiveEngine）
- 进程注册表：ENGINES（session_id -> LiveEngine）+ B6 资源互斥守卫
  （M1 多会话并行：仅 THS 通道键鼠独占，tdx/iquant 可并行，组合重叠=复制交易）
- session CRUD：list/create/get/delete + start/stop 业务体
- 熔断手动恢复：recover_breaker（运行中双写 / 未运行只改 DB）
- 历史查询 + 序列化：orders/trades/positions（运行中读内存，停止后从 live_trades 重放）

SSE 流（_sse_line/_heartbeat_stream/session_stream）是 HTTP 传输层关注点，留路由。
asyncio 任务调度（create_task(engine.start/stop)）同样留 async 路由——本模块的
start_session 只返回构造好、heartbeat 通过、recover 已执行但尚未 start() 的引擎，
由路由负责调度与注册。

纯重构，行为不变。
"""

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.config import load_config
from core.db import SessionLocal
from core.models import (
    LiveSession, LiveSessionPortfolio, PortfolioStrategy, Strategy,
    StockPoolStock, Formula, FormulaSignal, LiveOrder, LiveTrade, LiveDecisionEvent,
    LiveSignal, LiveAccountSnapshot,
)
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.live.modes import is_signal_only
try:
    from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
except Exception:
    HttpBridgeDispatcher = None  # type: ignore
try:
    from core.engine.ths_ui_dispatcher import ThsUiDispatcher
except Exception:
    ThsUiDispatcher = None  # type: ignore
try:
    from core.engine.tdx_tq_dispatcher import TdxTqDispatcher
except Exception:
    TdxTqDispatcher = None  # type: ignore
from core.engine.bar_poller import BarPoller
from core.engine.live_engine import LiveEngine
from core.engine.position import Position
from core.engine.format import dec_money
from core.engine.decision import summarize_decisions
from core.engine.crisis_alpha import DEFAULT_BENCHMARK_CODE
from core.engine.event import TradeEvent
from core.tq.formula import TQFormula
from core.tq.alert import TQAlert
from core.engine.live.alert_dispatcher import AlertDataDispatcher
from core.engine.live.market_data import MarketDataService
from core.engine.live.timing import now_shanghai
from core.services.signal_schedule import (
    effective_signal_frequency, validate_signal_config,
)
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)

# 进程内运行中的 LiveEngine 注册表：session_id -> LiveEngine。
# 实盘引擎需长期持有 dispatcher/poller/portfolio 内存态，不能按请求建销，
# 故放进程级 dict（单用户系统，无需分布式协调）。
ENGINES: Dict[int, LiveEngine] = {}


# ---------------------------------------------------------------------------
# 桥配置 / 股票池 / 引擎组装
# ---------------------------------------------------------------------------

def iquant_config() -> dict:
    """读 QMT 通道桥配置（config.iquant_bridge 段）。

    simulation / live 两个模式位当前都指向 8790 的 QMT 仿真账户（410038214257 / STOCK，账户内为仿真资金），各一个
    base_url；桥绑 loopback、无鉴权。超时/慢告警阈值与掘金桥同构配置，
    缺省回退（本机 loopback 桥默认 10s / 5s / 300s），不写死在调用点。
    """
    cfg = load_config()
    br = cfg.get("iquant_bridge", {}) if isinstance(cfg, dict) else {}
    br = br if isinstance(br, dict) else {}
    sim = br.get("simulation", {}) if isinstance(br.get("simulation"), dict) else {}
    liv = br.get("live", {}) if isinstance(br.get("live"), dict) else {}
    return {
        "available": HttpBridgeDispatcher is not None,
        "simulation": (sim.get("base_url")
                       or "http://127.0.0.1:8790"),
        "live": (liv.get("base_url")
                 or "http://127.0.0.1:8791"),
        "request_timeout_seconds": _positive_float(
            br.get("request_timeout_seconds"), 10.0),
        "slow_response_seconds": _positive_float(
            br.get("slow_response_seconds"), 5.0),
        "slow_warn_interval_seconds": _positive_float(
            br.get("slow_warn_interval_seconds"), 300.0),
    }


def bridge_config() -> dict:
    """读桥地址：simulation/live 两个模式位当前都指向 8790 的 QMT 仿真账户。

    8791 信用腿（CREDIT）保留但非生产、未启用，见 live/bridge/README.md。
    模拟/实盘（信号 vs 真实下单）不由 Core 控制，由 QMT 客户端启动按钮决定。
    """
    q = iquant_config()
    return {"simulation": q["simulation"], "live": q["live"]}


def ths_ui_config() -> dict:
    """读同花顺 UI 通道配置（config.ths_ui 段），缺省值对齐 core.config._defaults()。"""
    cfg = load_config()
    ths = cfg.get("ths_ui", {}) if isinstance(cfg, dict) else {}
    ths = ths if isinstance(ths, dict) else {}
    return {
        "enabled": bool(ths.get("enabled")),
        "available": ThsUiDispatcher is not None,
        "window_title": ths.get("window_title") or "网上股票交易系统5.0",
        "client_exe_path": ths.get("client_exe_path") or "",
        "driver": ths.get("driver") or "clipboard",
        "captcha_ocr": bool(ths.get("captcha_ocr")),
        # 委托价模式（2026-11-07 修：此前该配置项从未透传给 dispatcher，
        # config.ths_ui.price_mode 是死配置；实盘恒走 approval）
        "price_mode": ths.get("price_mode") or "approval",
        # 文件交接层双模式（file_builtin=内置执行器 / direct=同步直呼）
        "execute_mode": ths.get("execute_mode") or "file_builtin",
        "execute_interval_seconds": int(ths.get("execute_interval_seconds") or 5),
        "work_dir": ths.get("work_dir") or "data/ths",
    }


def _positive_float(value, default: float) -> float:
    """配置数值安全解析：缺失/非法/<=0 一律回退默认（配置问题不得抛异常）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def goldminer_config() -> dict:
    """读掘金量化桥配置（config.goldminer 段）。

    桥策略（live/bridge/goldminer_bridge.py）跑在掘金终端内，本平台经
    HttpBridgeDispatcher 以 QMT 桥同构协议访问其本地 HTTP 服务。
    """
    cfg = load_config()
    g = cfg.get("goldminer", {}) if isinstance(cfg, dict) else {}
    g = g if isinstance(g, dict) else {}
    return {
        "available": HttpBridgeDispatcher is not None,
        "base_url": g.get("base_url") or "http://127.0.0.1:7801",
        "token_set": bool(g.get("token")),
        "account_id": g.get("account_id") or "",
        # 桥请求超时（默认 30s：≥30 且 < 主循环 60s；超时按在途处理不判拒单）
        "request_timeout_seconds": _positive_float(
            g.get("request_timeout_seconds"), 30.0),
        # 慢响应告警阈值（默认 15s，明显小于超时；超阈值 risk/bridge_slow）
        "slow_response_seconds": _positive_float(
            g.get("slow_response_seconds"), 15.0),
        # 慢告警窗口（默认 300s）：同一会话每窗口最多一条，窗口内后续慢请求
        # 累计成计数与最慢值，在窗口过期后的首条里汇总（防多端点各报一条）
        "slow_warn_interval_seconds": _positive_float(
            g.get("slow_warn_interval_seconds"), 300.0),
    }


def tq_trade_config() -> dict:
    """读通达信 TQ 交易配置（config.tq_trade 段）。"""
    cfg = load_config()
    tq = cfg.get("tq_trade", {}) if isinstance(cfg, dict) else {}
    tq = tq if isinstance(tq, dict) else {}
    return {
        "available": TdxTqDispatcher is not None,
        "account": tq.get("account") or "",
        "account_type": tq.get("account_type") or "stock",
        "price_mode": tq.get("price_mode") or "my",
        "client_path": tq.get("client_path") or "",
        "auto_confirm_dialog": bool(tq.get("auto_confirm_dialog", True)),
        # 「交易信号」自动确认单笔总时限（秒）：定位窗口+OCR+点击+等委托表认领。
        "confirm_timeout": float(tq.get("confirm_timeout") or 12.0),
    }


#: 实盘交易引擎：key -> (渠道标识, 中文名)。渠道标识是 Core 状态机/前端识别的通道名。
#: 四引擎：QMT 通道 / 掘金量化桥 / 通达信 TQ / 同花顺 UI 自动化。
ENGINES_META = {
    "goldminer": ("goldminer_bridge", "掘金量化（HTTP 桥）"),
    "tdx": ("tdx_tq", "通达信 TQ 交易"),
    "ths": ("ths_ui", "同花顺独立交易（UI 自动化）"),
    "iquant": ("iquant_bridge", "QMT 通道"),
}


def default_engine() -> str:
    """未显式选引擎时的默认值：tq_trade.default_engine 优先（A-6，用户主通道 tdx）；
    未配置/空/非法值时沿用旧行为（ths_ui.enabled → ths，否则 iquant）。"""
    cfg = load_config()
    tq = cfg.get("tq_trade", {}) if isinstance(cfg, dict) else {}
    value = str((tq.get("default_engine") if isinstance(tq, dict) else "") or "").strip()
    if value in ENGINES_META:
        return value
    ths = ths_ui_config()
    return "ths" if (ths["enabled"] and ths["available"]) else "iquant"


def session_engine(session) -> str:
    """session 生效引擎：显式值优先；NULL（升级前的历史会话）回退全局默认。

    全局默认自 A-6 起为 tq_trade.default_engine（SystemConfig 可配，默认 tdx）；
    未配置时沿用旧行为（ths_ui.enabled → ths，否则 iquant）。
    qmt 通道已移除：历史 qmt 会话的显式值不再进白名单，同样回退默认引擎。
    **不自动改指 iquant**——miniQMT 与 QMT 通道是不同终端与不同账户，
    静默改指可能把委托打到非预期账户；需要切换请显式修改会话 engine。
    """
    explicit = getattr(session, "engine", None)
    return explicit if explicit in ENGINES_META else default_engine()


def make_dispatcher(engine: str, mode: str = "simulation", session_id: int = 0):
    """按引擎标识构造 dispatcher（tdx/ths 两个独立通道 + QMT/掘金 HTTP 桥）。

    依赖缺失/未配置时抛 RuntimeError，由 start_session 转成 503 明确告警，
    绝不静默回退到别的通道（避免"以为在通达信下单、实际打到 QMT"）。

    QMT 双桥：mode == "live" 走真实资金桥（8791），其余一律走仿真桥
    （8790）——未知 mode 绝不落到真实资金桥。
    """
    if engine == "ths":
        if ThsUiDispatcher is None:
            raise RuntimeError("未安装同花顺 UI 自动化依赖（pywinauto/pyperclip）")
        ths = ths_ui_config()
        return ThsUiDispatcher(
            window_title=ths["window_title"], client_exe_path=ths["client_exe_path"],
            captcha_ocr=ths["captcha_ocr"],
            execute_mode=ths["execute_mode"],
            execute_interval_seconds=ths["execute_interval_seconds"],
            work_dir=ths["work_dir"],
            session_id=session_id,
            price_mode=ths["price_mode"],
        )
    if engine == "tdx":
        if TdxTqDispatcher is None:
            raise RuntimeError("通达信 TQ 交易通道不可用")
        cfg = tq_trade_config()
        return TdxTqDispatcher(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
            auto_confirm_dialog=cfg["auto_confirm_dialog"],
            confirm_timeout=cfg["confirm_timeout"],
        )
    if engine == "goldminer":
        if HttpBridgeDispatcher is None:
            raise RuntimeError("HttpBridgeDispatcher not available")
        g = goldminer_config()
        # 掘金桥 /quote 走 gm history_n（云端历史服务，首次 >10s）+ 桥内全局锁
        # 串行 → 超时/慢阈值读 config.goldminer（默认 30s / 15s）。
        return HttpBridgeDispatcher(
            base_url=g["base_url"],
            timeout=g["request_timeout_seconds"],
            slow_threshold=g["slow_response_seconds"],
            slow_warn_interval=g["slow_warn_interval_seconds"],
            channel="goldminer_bridge",
        )
    if engine == "iquant":
        q = iquant_config()
        if HttpBridgeDispatcher is None:
            raise RuntimeError("HttpBridgeDispatcher not available")
        # mode 语义显式：live=真实资金桥（8791），其余（simulation/未知）一律
        # 仿真桥（8790）——配置错误不得把请求打到真实资金桥。
        base_url = q["live"] if mode == "live" else q["simulation"]
        return HttpBridgeDispatcher(
            base_url=base_url,
            timeout=q["request_timeout_seconds"],
            slow_threshold=q["slow_response_seconds"],
            slow_warn_interval=q["slow_warn_interval_seconds"],
            channel="iquant_bridge",
        )
    # 未知引擎明确报错，绝不静默回退
    raise RuntimeError(
        "未知交易引擎：%s（可用：%s）" % (engine, "/".join(ENGINES_META)))


def bridge_probe_message(channel: str, reason: Optional[str], base_url: str) -> str:
    """桥启动闸提示（按探测失败类型分级，避免把人误导成「策略没加载」）。

    reason 来自 HttpBridgeDispatcher.last_probe_error：
    refused=端口无监听 / timeout=已监听但无响应 / protocol=应答格式不符。
    """
    label = ("掘金桥" if channel == "goldminer_bridge"
             else "QMT 通道" if channel == "iquant_bridge" else "桥")
    if reason == "refused":
        return ("%s进程未启动（%s 端口无监听）：桥策略可能未运行，"
                "或启动时卡在 SDK 查询" % (label, base_url))
    if reason == "timeout":
        return ("%s端口已监听但无响应（%s）：桥策略可能卡在 SDK 查询"
                "（终端显示“运行中”不代表查询可用）" % (label, base_url))
    if reason == "protocol":
        return "%s应答格式不符（%s，期望 HTTP 桥协议）" % (label, base_url)
    return "%s不可达（%s）：请检查桥策略是否运行及端口配置" % (label, base_url)


def engine_status(engine: Optional[str]) -> dict:
    """引擎清单 + 已配置情况（前端下拉与通道条共用）。"""
    ths = ths_ui_config()
    tq = tq_trade_config()
    items = []
    for key, (channel, label) in ENGINES_META.items():
        configured = True
        detail = ""
        if key == "ths":
            configured = ths["available"]
            detail = "依赖未安装（pywinauto/pyperclip）" if not configured else ths["window_title"]
        elif key == "tdx":
            configured = tq["available"]
            detail = "通达信 TQ（需客户端已登录资金账号）" if configured else "通道不可用"
        elif key == "goldminer":
            g = goldminer_config()
            configured = g["available"]
            detail = "%s（token%s）" % (g["base_url"], "已配" if g["token_set"] else "未配")
        elif key == "iquant":
            q = iquant_config()
            configured = q["available"]
            detail = "仿真 %s / 实盘 %s" % (q["simulation"], q["live"])
        else:
            continue
        items.append({
            "key": key, "channel": channel, "label": label,
            "configured": configured, "detail": detail,
        })
    current = engine or default_engine()
    # 未知引擎（如历史 qmt）回退 tdx 展示，避免前端 active 为 None
    channel, label = ENGINES_META.get(current, ENGINES_META["tdx"])
    return {
        "engine": current, "active": channel, "label": label, "engines": items,
        "ths_ui": ths, "tq_trade": tq, "bridge": bridge_config(),
    }


#: 通道探活 TTL：同一通道 30s 内复用上次结果，页面请求不重复打桥
_PROBE_TTL_SECONDS = 30.0
#: 单次探活硬超时：心跳最多等 1s，超时沿用上次已知值（绝不阻塞页面请求）
_PROBE_TIMEOUT_SECONDS = 1.0
#: {通道键: (单调时钟时点, 是否在线)}
_PROBE_CACHE: Dict[str, Tuple[float, bool]] = {}
_PROBE_CACHE_LOCK = threading.Lock()


def _known_engine_online(engine) -> Optional[bool]:
    """引擎内存里最近一次已知的在线状态（不新发探针；未知返回 None）。

    ``LiveEngine._tick_main`` 每轮都调 dispatcher.heartbeat() 并写
    ``_bridge_online``，页面请求只需读这个值，避免与引擎共用慢桥排队。
    """
    for attr in ("bridge_online", "_bridge_online"):
        val = getattr(engine, attr, None)
        if val is not None:
            return bool(val)
    return None


def _live_bridge_online(engine) -> bool:
    """运行中引擎的桥在线状态：优先引擎内存最近已知值（不新发 /ping）。

    兜底（引擎无 ``bridge_online`` 的替身/旧实现）才直接探活一次，保留旧语义。
    """
    known = _known_engine_online(engine)
    if known is not None:
        return known
    return bool(engine.dispatcher.heartbeat())


def _engine_state_freshness(engine) -> Tuple[Optional[str], bool]:
    """引擎状态新鲜度：(checked_at, stale)。

    ``checked_at`` 取引擎最近一次主循环 tick 时点（``_last_tick_at``，naive 上海
    时间）；超过 ``max(180s, 3×poll_interval)`` 未更新视为可能过期（引擎卡住/
    线程池饿死），置 ``stale=True``。取不到 tick 时点时返回 (None, False)。
    """
    last_tick = getattr(engine, "_last_tick_at", None)
    if not isinstance(last_tick, datetime):
        return None, False
    if last_tick.tzinfo is None:
        now = datetime.now(_SH_TZ).replace(tzinfo=None)
    else:
        now = datetime.now(_SH_TZ)
    try:
        poll = float(getattr(engine, "_poll_interval", 60) or 60)
    except (TypeError, ValueError):
        poll = 60.0
    stale = (now - last_tick).total_seconds() > max(180.0, 3.0 * poll)
    return last_tick.strftime("%Y-%m-%d %H:%M:%S"), stale


def _probe_online_quick(engine_key: str, dispatcher) -> Tuple[Optional[bool], bool]:
    """兜底探活：30s TTL 缓存 + 1s 硬超时，返回 (online, stale)。

    仅用于没有 ``bridge_online`` 的引擎替身/旧实现；真实 LiveEngine 走引擎状态，
    不会进入这里。心跳跑在守护线程里并 ``join(1s)``：超时/异常时沿用上一次已知
    值（无缓存则判离线）并置 ``stale=True``，保证调用方最坏 1s 返回。
    """
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(engine_key)
    if cached is not None and time.monotonic() - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1], False

    box: List[bool] = []

    def _run() -> None:
        try:
            box.append(bool(dispatcher.heartbeat()))
        except Exception:  # noqa: BLE001 - 探活失败只影响在线展示
            box.append(False)

    t = threading.Thread(target=_run, name="channel-probe", daemon=True)
    t.start()
    t.join(_PROBE_TIMEOUT_SECONDS)
    if t.is_alive():
        return (cached[1] if cached is not None else False), True
    online = box[0] if box else False
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[engine_key] = (time.monotonic(), online)
    return online, False


def channel_status(engine: Optional[LiveEngine] = None,
                   engine_key: Optional[str] = None,
                   engines: Optional[Dict[int, LiveEngine]] = None) -> dict:
    """当前生效的下单通道 + 在线状态（实盘页顶部通道条 / 状态总览通道卡）。

    在线语义（2026-11-04 修正，2026-11-05 探活脱钩）：
    - **有运行中的 LiveEngine**（``engine`` 直接传入，或按 ``engine_key`` 在当前
      注册表中找到同通道引擎）→ ``online`` 取引擎内存里**最近一次已知**的在线
      状态（``LiveEngine.bridge_online``，由主循环每 60s 心跳刷新），**不新发
      /ping**。掘金桥单次查询 4–40s（桥内全局锁串行），页面请求里现打心跳会把
      「状态总览」整页卡住十几到几十秒（实证 2026-11-05：channel?engine=goldminer
      耗时 7.7s，其余接口 2–86ms）。
    - 引擎没有已知状态（无 ``bridge_online`` 的替身/旧实现）才回退到探活：30s
      TTL 缓存 + 1s 硬超时，超时沿用上一次已知值并置 ``stale=True``。
    - **没有运行引擎** → ``online=None``（未检测/待启动），并置
      ``engine_running=False``。此前无引擎时会另建一个 dispatcher 做 TQ 心跳
      探测——只要通达信客户端开着就返回 True，工作台因此把「引擎未运行」误显示
      成「在线」（后端重启后尤其明显，实证 2026-11-04）。

    新增可选字段：``online_source``（engine/probe/none）、``checked_at``（引擎
    最近一次心跳时点，未知为 None）、``stale``（状态可能已过期）。

    ``engines`` 仅供测试注入注册表，缺省全局 ``ENGINES``。
    """
    data = engine_status(engine_key)
    registry = ENGINES if engines is None else engines
    running = engine if engine is not None else _running_engine_for(registry, engine_key)
    online = None
    source = "none"
    checked_at = None
    stale = False
    if running is not None:
        known = _known_engine_online(running)
        if known is not None:
            online = known
            source = "engine"
            checked_at, stale = _engine_state_freshness(running)
        else:
            # 兜底探活（仅无引擎状态的替身/旧实现）：TTL 缓存 + 1s 硬超时。
            online, stale = _probe_online_quick(engine_key or "", running.dispatcher)
            source = "probe"
    data["engine_running"] = running is not None
    data["online"] = online
    data["online_source"] = source
    data["checked_at"] = checked_at
    data["stale"] = stale
    return data


#: 引擎键 → dispatcher.channel（用于在注册表中找同通道的运行中引擎）
_ENGINE_CHANNEL = {
    "goldminer": "goldminer_bridge",
    "tdx": "tdx_tq",
    "ths": "ths_ui",
    "iquant": "iquant_bridge",
}


def _running_engine_for(engines: Dict[int, LiveEngine],
                        engine_key: Optional[str]) -> Optional[LiveEngine]:
    """引擎注册表中该通道的运行中实例（无则 None，多个时取第一个）。"""
    target = _ENGINE_CHANNEL.get(engine_key or "")
    if not target:
        return None
    for eng in list(engines.values()):
        if getattr(getattr(eng, "dispatcher", None), "channel", None) == target:
            return eng
    return None


def real_position_count(rows: list) -> int:
    """真实持仓条数：按「持仓数量」判定，0 股残行不计。

    通达信/同花顺卖出后持仓表仍保留该行（代码+名称），只是数量归零；按
    行数计数会把残行算成持仓。字段取 ``volume``（持仓数量/股票余额），
    **不取** ``available``（可卖数量）：当日买入 T+1 锁仓 ``available=0`` 但
    ``volume>0``，属真实持仓，不能用可卖数剔除。
    """
    count = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            volume = int(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if volume > 0:
            count += 1
    return count


def test_channel(channel: str = "ths_ui", mode: str = "simulation") -> dict:
    """只读连通性探测（实盘页/系统配置页「测试连接」按钮）。

    ths_ui：连接 xiadan.exe 窗口并读一次持仓（只读，不下单）；
    iquant_bridge：请求对应模式桥的 /ping；
    tdx_tq：连通达信 TQ 交易账户并读一次持仓（只读）。
    """
    if channel == "tdx_tq":
        if TdxTqDispatcher is None:
            return {"ok": False, "channel": "tdx_tq", "error": "通达信 TQ 交易通道不可用"}
        cfg = tq_trade_config()
        dispatcher = TdxTqDispatcher(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
            auto_confirm_dialog=cfg["auto_confirm_dialog"],
            confirm_timeout=cfg["confirm_timeout"],
        )
        try:
            if not dispatcher.heartbeat():
                return {"ok": False, "channel": "tdx_tq",
                        "error": dispatcher.last_error or "通达信交易未就绪"}
            try:
                rows = dispatcher.query_positions()
            except Exception as e:
                # 持仓查询失败会抛 BridgeUnavailableError（读失败≠零持仓）；
                # 探测接口必须转成 ok=False，不能漏成 500。
                return {"ok": False, "channel": "tdx_tq",
                        "error": str(e) or "通达信持仓读取失败"}
            if dispatcher.last_error:
                return {"ok": False, "channel": "tdx_tq", "error": dispatcher.last_error}
            return {"ok": True, "channel": "tdx_tq",
                    "positions": real_position_count(rows)}
        finally:
            dispatcher.close()
    if channel == "ths_ui":
        if ThsUiDispatcher is None:
            return {"ok": False, "channel": "ths_ui", "error": "未安装 UI 自动化依赖（pywinauto/pyperclip）"}
        cfg = ths_ui_config()
        dispatcher = ThsUiDispatcher(
            window_title=cfg["window_title"], client_exe_path=cfg["client_exe_path"],
            captcha_ocr=cfg["captcha_ocr"],
            # 只读探测：direct 模式不启文件交接执行器线程（否则每次「测试连接」
            # 都会在 data/ths/0 挂一个消费 orders 的后台线程）。
            execute_mode="direct",
        )
        try:
            if not dispatcher.connect():
                return {"ok": False, "channel": "ths_ui",
                        "error": dispatcher.last_error or "未找到同花顺交易客户端窗口"}
            if not cfg["captcha_ocr"]:
                return {"ok": False, "channel": "ths_ui",
                        "error": "同花顺复制表格会触发验证码，请先在系统配置启用验证码 OCR"}
            rows = dispatcher.query_positions()
            if rows is None or dispatcher.last_error:
                return {"ok": False, "channel": "ths_ui",
                        "error": dispatcher.last_error or "同花顺持仓读取失败（未登录/验证码/切页异常）"}
            return {"ok": True, "channel": "ths_ui", "window_title": cfg["window_title"],
                    "positions": real_position_count(rows)}
        finally:
            dispatcher.close()
    if channel == "goldminer_bridge":
        g = goldminer_config()
        base_url = g["base_url"]
        try:
            dispatcher = HttpBridgeDispatcher(base_url=base_url)
            online = dispatcher.heartbeat()
            if not online:
                return {"ok": False, "channel": "goldminer_bridge", "base_url": base_url,
                        "error": "掘金桥未响应（终端内桥策略未运行，日志应显示 [GM-BRIDGE] listening）"}
            return {"ok": True, "channel": "goldminer_bridge", "base_url": base_url, "error": None}
        except Exception as e:
            return {"ok": False, "channel": "goldminer_bridge", "base_url": base_url, "error": str(e)}
    q = iquant_config()
    # 与 make_dispatcher 同一 mode 语义：仅 live 走真实资金桥，其余兜底仿真桥
    base_url = q["live"] if mode == "live" else q["simulation"]
    try:
        dispatcher = HttpBridgeDispatcher(base_url=base_url)
        online = dispatcher.heartbeat()
        return {"ok": bool(online), "channel": "iquant_bridge", "base_url": base_url,
                "error": None if online else "QMT 桥未响应（客户端未启动或桥策略未加载）"}
    except Exception as e:
        return {"ok": False, "channel": "iquant_bridge", "base_url": base_url, "error": str(e)}


def resolve_stock_codes(db: Session, portfolio_strategy_id: int) -> list:
    """取某组合股票池的成分股代码列表（BarPoller 行情订阅范围）。"""
    ps = db.query(PortfolioStrategy).filter_by(id=portfolio_strategy_id).first()
    if ps is None:
        return []
    stocks = (
        db.query(StockPoolStock)
        .filter_by(pool_id=ps.stock_pool_id)
        .all()
    )
    return [s.stock_code for s in stocks if s.stock_code]


def _session_strategies(db: Session, session_id: int):
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    rows = []
    for link in links:
        strategies = (
            db.query(Strategy)
            .filter(Strategy.portfolio_id == link.portfolio_strategy_id)
            .order_by(Strategy.id)
            .all()
        )
        rows.extend((link.portfolio_strategy_id, st) for st in strategies)
    return rows


def _send_warn_enabled() -> bool:
    """live.alert_send_warn_enabled（0017）：alert 信号是否推通达信 send_warn。

    缺省 False（hermes 信号模式：只落库 + SSE + 接口拉取）；config.yaml 设
    true 恢复推送（同时恢复启动前的 send_warn 可用性探测）。
    """
    return bool((load_config().get("live") or {}).get(
        "alert_send_warn_enabled", False))


def _preflight_tq_alert():
    msg = TQAlert().preflight()
    return (503, msg) if msg else None


def _smoke_formula_outputs(db: Session, session_id: int):
    """alert 启动前用 1 只股票试算公式，确认输出线覆盖 FormulaSignal。"""
    formula = TQFormula()
    seen = set()
    for pid, st in _session_strategies(db, session_id):
        smoke_key = (st.formula_id, st.period)
        if st.formula_id is None or smoke_key in seen:
            continue
        seen.add(smoke_key)
        f = db.query(Formula).filter_by(id=st.formula_id).first()
        if f is None:
            return (503, "策略 %s 引用的公式不存在" % st.name)
        signals = db.query(FormulaSignal).filter_by(formula_id=f.id).all()
        if not signals:
            return (503, "公式 %s 未配置信号输出线" % f.name)
        codes = resolve_stock_codes(db, pid)
        if not codes:
            continue
        code = sorted(codes)[0]
        # 至少拉 200 根，避免周线 MACD 等长周期输出线因历史不足变 NaN
        count = max(f.formula_count or 200, 200)
        try:
            raw = formula.compute(
                f.name, "", [code], period=st.period,
                count=count, end_time=now_shanghai().strftime("%Y%m%d"),
            )
            if (not isinstance(raw, dict)
                    or str(raw.get("ErrorId", "0")) not in ("0", "19")):
                detail = raw.get("Error") if isinstance(raw, dict) else "无返回"
                return (503, "公式 %s 试算失败：%s" % (
                    f.name, detail or "无返回"))
            produced = MarketDataService._extract_latest_signal(raw, code)
            names = {o["name"] for o in produced}
        except Exception as exc:
            return (503, "公式 %s 试算失败：%s" % (f.name, exc))
        missing = [s.signal_name for s in signals if s.signal_name not in names]
        if missing:
            return (503, "公式 %s 输出缺少信号线：%s" % (
                f.name, "、".join(missing)))
    return None


def preflight_signal_config(db: Session, session_id: int, execution_mode: str):
    """启动前检查；返回 None 或 (http_status, message)。

    alert: TQ-only + 频率规则 + send_warn 可用 + 公式试算输出线。
    auto（P0 兼容）: 只查 TQ engine + 频率规则，不做 TQ 试算/告警探测，
    避免历史自动会话因客户端未就绪而无法启动。
    """
    if execution_mode not in ("alert", "auto", "hermes"):
        return (400, "execution_mode 必须为 alert/auto/hermes")
    for pid, st in _session_strategies(db, session_id):
        if st.formula_id is None:
            if is_signal_only(execution_mode):
                return (503, "策略 %s 未绑定公式" % st.name)
            continue
        f = db.query(Formula).filter_by(id=st.formula_id).first()
        if f is None:
            return (503, "策略 %s 引用的公式不存在" % st.name)
        if f.engine != "tq":
            return (503, "实盘只支持通达信导入公式（%s.engine=%s）" % (
                f.name, f.engine))
        msg = validate_signal_config(
            st.period, st.signal_frequency, st.signal_at,
            st.signal_weekday, st.signal_data_end,
        )
        if msg:
            return (503, msg)
        freq = effective_signal_frequency(st.period, st.signal_frequency)
        if execution_mode == "auto" and freq != st.period:
            return (503, "全自动 P0 仅支持 signal_frequency == period；"
                         "请改用 alert 或等待 P2（策略 %s）" % st.name)
    if is_signal_only(execution_mode):
        if _send_warn_enabled():
            err = _preflight_tq_alert()
            if err:
                return err
        err = _smoke_formula_outputs(db, session_id)
        if err:
            return err
    return None


def build_engine(
    session_id: int,
    db: Session,
    mode: str = "simulation",
    db_session_factory=SessionLocal,
    dispatcher_cls=None,
    engine: Optional[str] = None,
    execution_mode: str = "auto",
    signal_file_path: Optional[str] = None,
) -> LiveEngine:
    """组装一个 session 的 LiveEngine：各组合 Portfolio + dispatcher + BarPoller。

    公式注入（0010）：遍历各组合策略，按 Strategy.formula_id → Formula.name 预加载
    {strategy_id: formula_name}，连同 TQFormula 实例传入 LiveEngine，供 _fill_signal_cache
    逐 bar 内存注入算公式信号。无公式的策略不入映射（_fill_signal_cache 跳过）。

    dispatcher_cls：注入 dispatcher 类（测试 monkeypatch 用）；None 时按 engine 选：
    engine=None → 沿用旧行为（ths_ui.enabled → 同花顺，否则 QMT 桥）；
    engine=tdx → 通达信 TQ。

    execution_mode=alert/hermes（0018）时固定使用 AlertDataDispatcher（TQ 行情、
    禁止下单），优先于 dispatcher_cls/engine 选择；auto 保持既有下单通道行为。

    外部信号注入（0011）：signal_file_path 参数非空 → LiveEngine 建
    ExternalSignalFileSource（通达信条件预警文件时间窗口增量读取）；None 时回退读
    session.signal_file_path（会话级配置，start_session/api 组装路径无需显式传），
    两者皆空 → 不启用，行为不变。
    """
    if is_signal_only(execution_mode):
        dispatcher = AlertDataDispatcher()
    elif dispatcher_cls is None:
        dispatcher = make_dispatcher(engine or default_engine(), mode, session_id)
    elif dispatcher_cls is ThsUiDispatcher:
        ths_cfg = load_config().get("ths_ui", {})
        ths_cfg = ths_cfg if isinstance(ths_cfg, dict) else {}
        dispatcher = dispatcher_cls(
            window_title=ths_cfg.get("window_title") or "网上股票交易系统5.0",
            client_exe_path=ths_cfg.get("client_exe_path"),
            captcha_ocr=bool(ths_cfg.get("captcha_ocr")),
            execute_mode=ths_cfg.get("execute_mode") or "file_builtin",
            execute_interval_seconds=int(ths_cfg.get("execute_interval_seconds") or 5),
            work_dir=ths_cfg.get("work_dir") or "data/ths",
            session_id=session_id,
            price_mode=ths_cfg.get("price_mode") or "approval",
        )
    elif dispatcher_cls is TdxTqDispatcher and TdxTqDispatcher is not None:
        cfg = tq_trade_config()
        dispatcher = dispatcher_cls(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
            auto_confirm_dialog=cfg["auto_confirm_dialog"],
        )
    else:
        br = bridge_config()
        dispatcher = dispatcher_cls(base_url=br.get(mode, br["simulation"]))
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    # 会话级实盘资金基准（账户总资产口径）：非 None 时所有组合以该值起账
    # （比率配置等比缩放）；None=各组合用自身 initial_capital（现行为）。
    # 继承持仓的市值由 LiveEngine 从起账现金中扣除，避免与基准重复计入。
    session_row = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    live_capital = getattr(session_row, "live_capital", None)
    # 外部信号注入（0011）：参数优先；未传回退 session.signal_file_path（会话级配置）
    if signal_file_path is None and session_row is not None:
        signal_file_path = getattr(session_row, "signal_file_path", None)
    portfolios = []
    stock_codes: set = set()
    # B5 市场状态闸：组合 → 基准指数代码（供 LiveEngine 逐日算 regime）
    benchmark_codes: Dict[int, str] = {}
    formula_ids: set = set()
    strategy_formula: Dict[int, int] = {}  # strategy_id -> formula_id
    # 预热/分发按 (code, period) 取该股票该周期所有公式的 formula_count 最大值（跨组合跨策略）。
    # 无公式兜底 DEFAULT_COUNT(200)。预热只拉实际有策略的 (code,period)，按需不浪费。
    # 在此同一循环累加（已查 ps/strategies/股票池，不二次遍历）。
    DEFAULT_COUNT = 200
    code_period_count: Dict[tuple, int] = {}
    # 注：formula_map 在循环后批量查，此处先记 strategy_id -> period，待 formula_map 就绪再算 max。
    # 简化：先记 (code, period) -> [formula_id...]，formula_map 就绪后取 max(formula_count)。
    code_period_formula_ids: Dict[tuple, set] = {}
    pool_codes_by_portfolio: Dict[int, set] = {}
    for link in links:
        ps = db.query(PortfolioStrategy).filter_by(id=link.portfolio_strategy_id).first()
        if ps is None:
            continue
        strategies = db.query(Strategy).filter_by(portfolio_id=ps.id).order_by(Strategy.id).all()
        port = assemble_portfolio(ps, strategies, db, initial_capital_override=live_capital)
        if live_capital is not None:
            logger.info(
                "实盘资金基准 %s（组合 %s 配置初始资金 %s）",
                live_capital, ps.id, ps.initial_capital,
            )
        portfolios.append(port)
        benchmark_codes[ps.id] = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
        pool_codes = resolve_stock_codes(db, ps.id)
        stock_codes.update(pool_codes)
        pool_codes_by_portfolio[ps.id] = set(pool_codes)
        for strat in strategies:
            if strat.formula_id is not None:
                formula_ids.add(strat.formula_id)
                strategy_formula[strat.id] = strat.formula_id
            # 累加 (code, period) -> formula_id（跨组合跨策略，同 key 收集所有公式）
            for code in pool_codes:
                code_period_formula_ids.setdefault((code, strat.period), set()).add(strat.formula_id)

    # 批量查 Formula，建 {strategy_id: formula_name} + {formula_name: formula_count}
    # formula_count（Q4 决策4）为公式级注入根数：实盘注入 count 来自该字段，非全局 200。
    formula_by_strategy: Dict[int, str] = {}
    formula_count_by_name: Dict[str, int] = {}
    # formula_id -> formula_count（供 code_period_count 取 max）
    formula_count_by_id: Dict[int, int] = {}
    if formula_ids:
        formula_map = {
            f.id: f
            for f in db.query(Formula).filter(Formula.id.in_(formula_ids)).all()
        }
        for sid, fid in strategy_formula.items():
            f = formula_map.get(fid)
            if f:
                formula_by_strategy[sid] = f.name
                if f.formula_count:
                    formula_count_by_name[f.name] = f.formula_count
                    formula_count_by_id[fid] = f.formula_count

    for port in portfolios:
        for ctx in port.strategies:
            ctx.formula_name = formula_by_strategy.get(ctx.strategy_id, "")
            ctx.formula_id = strategy_formula.get(ctx.strategy_id)

    # (code, period) -> 该股票该周期所有公式的 formula_count 最大值（跨组合跨策略）。
    # formula_id 为 None 的策略（无公式）兜底 DEFAULT_COUNT。预热/分发按此拉，按需不浪费。
    for (code, period), fids in code_period_formula_ids.items():
        counts = [formula_count_by_id.get(fid, DEFAULT_COUNT) for fid in fids if fid is not None]
        # 无任何带公式的策略 → 兜底；有则取 max
        code_period_count[(code, period)] = max(counts) if counts else DEFAULT_COUNT

    def _regime_provider(code: str, end_date):
        """B5：拉基准收盘历史（end_date 前 ~620 天 ~ end_date），供因果 regime 跟踪。
        局部 import 避免 live_service ↔ backtest_service 模块级耦合；仅在有策略
        开启 regime_filter_enabled 时被 LiveEngine 调用（默认完全不触发）。"""
        from core.services.backtest_service import build_index_close_series
        return build_index_close_series(
            [code], end_date - timedelta(days=620), end_date,
        ).get(code, {})

    # 股票池为空时给一个占位，避免空列表；首期实盘一般有成分股
    poll_codes = sorted(stock_codes) or ["000001.SZ"]
    # 盘中 1m 数据源（快照合成）：TQ 通道盘中分钟 K 线只到上一交易日
    # （实测 + 手册 refresh_kline 限制），快照通道实时——tdx/ths/alert 的
    # poller 改读快照合成 bar（deals 节拍 5s 采样，_tick_deals 驱动）；
    # iquant 桥行情自带实时 bar，保持 K 线轮询。合成器构建失败回退 K 线
    # 轮询（盘中 1m 退化，行为同旧版）。
    snapshot_synth = None
    poller = None
    try:
        from core.tq.data import TQData
        from core.engine.live.snapshot_bars import (
            SnapshotBarSynthesizer, SnapshotQuoteDispatcher,
        )
        from core.engine.ths_ui_dispatcher import ThsUiDispatcher as _ThsDisp
        from core.engine.tdx_tq_dispatcher import TdxTqDispatcher as _TdxDisp
        _tq_quote_types = tuple(
            d for d in (_TdxDisp, _ThsDisp, AlertDataDispatcher)
            if d is not None
        )
        # goldminer 引擎：交易走掘金桥，**行情走通达信快照合成**（行情与交易
        # 分离——gm history_n 单只 ~13s 云端往返，120 只全池轮询会饿死主循环）
        _goldminer_quotes = engine == "goldminer"
        if _goldminer_quotes or isinstance(dispatcher, _tq_quote_types):
            snapshot_synth = SnapshotBarSynthesizer(
                poll_codes,
                fetch_snapshot=lambda codes: TQData().get_market_snapshot_batch(
                    list(codes), []),
            )
            poller = BarPoller(
                SnapshotQuoteDispatcher(snapshot_synth), poll_codes,
                period="1m", count=10,
            )
    except Exception:
        logger.exception("快照合成器构建失败，回退 K 线轮询（盘中 1m 将不可用）")
        snapshot_synth = None
        poller = None
    if poller is None:
        try:
            poller = BarPoller(dispatcher, poll_codes, period="1m", count=10)
        except Exception:
            poller = None  # type: ignore
    # 账户全貌快照采集节拍：THS UI 读表重 → 60s；goldminer 交易通道走掘金云
    # （gm RPC 每查询 ~10s）→ 60s；tdx/iquant/alert 本地快照 → 5s
    try:
        from core.engine.ths_ui_dispatcher import ThsUiDispatcher as _ThsDisp2
        from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher as _HttpDisp2
        if isinstance(dispatcher, _ThsDisp2) or engine == "goldminer":
            _snap_interval = 60.0
        elif isinstance(dispatcher, _HttpDisp2):
            _snap_interval = 60.0
        else:
            _snap_interval = 5.0
    except Exception:
        _snap_interval = 5.0
    # 引擎标识（快照表落库用）：signal-only 模式用模式名本身（alert/hermes）；
    # auto 按会话解析（None → 全局默认）
    if is_signal_only(execution_mode):
        _engine_key = execution_mode
    else:
        _engine_key = engine or default_engine()
    # 交易日历多源回退链：主源=当前通道 query_calendar；非掘金会话再挂掘金桥
    # /calendar 作备用源（内置官方休市表 BUILTIN_CLOSURES 为末级兜底）。
    from core.engine.live.calendar import TradingCalendar, goldminer_calendar_provider
    _calendar_fallbacks = []
    if _engine_key != "goldminer":
        _calendar_fallbacks.append(("goldminer_bridge", goldminer_calendar_provider))
    trading_calendar = TradingCalendar(
        dispatcher.query_calendar, fallback_providers=_calendar_fallbacks)
    eng = LiveEngine(
        session_id=session_id,
        portfolios=portfolios,
        dispatcher=dispatcher,
        bar_poller=poller,
        db_session_factory=db_session_factory,
        tq_formula=TQFormula(),
        formula_by_strategy=formula_by_strategy,
        formula_count=200,
        formula_count_by_name=formula_count_by_name,
        code_period_count=code_period_count,
        trading_calendar=trading_calendar,
        regime_provider=_regime_provider,
        benchmark_codes=benchmark_codes,
        execution_mode=execution_mode,
        signal_file_path=signal_file_path,
        formula_id_by_strategy=strategy_formula,
        enable_signal_monitor=True,
        send_warn_enabled=_send_warn_enabled(),
        snapshot_synth=snapshot_synth,
        engine_key=_engine_key,
        account_snapshot_interval=_snap_interval,
        pool_codes_by_portfolio=pool_codes_by_portfolio,
        # 行情轮询窗口（config.live.quote_polling）：缺键时引擎侧回落安全默认
        # （交易日 09:15–15:05 之外不轮询，收盘后停对桥/TQ 的行情拉取）
        quote_polling=(load_config().get("live") or {}).get("quote_polling"),
    )
    # 桥慢响应告警接引擎事件流（risk/bridge_slow，dispatcher 侧已按 endpoint
    # 冷却去重）；仅 HTTP 桥 dispatcher 有 on_slow 钩子，其它通道不受影响。
    if hasattr(dispatcher, "on_slow"):
        dispatcher.on_slow = eng._on_bridge_slow
    # 交易日历退化告警接引擎事件流（risk/trading_calendar_degraded，每自然年一次）
    cal = getattr(eng, "_calendar", None)
    if cal is not None and hasattr(cal, "on_degraded"):
        cal.on_degraded = lambda payload: eng._emit(
            "risk", dict(payload, session_id=session_id, engine=_engine_key))
    return eng


# ---------------------------------------------------------------------------
# session CRUD
# ---------------------------------------------------------------------------

def list_sessions(db: Session, engines: Optional[Dict[int, LiveEngine]] = None) -> list:
    """session 列表。engines 缺省取本模块注册表（I-7：并入引擎内存对账差异摘要）。"""
    if engines is None:
        engines = ENGINES
    sessions = db.query(LiveSession).all()
    # 一次取全部分组关系,避免 N+1(单用户规模可忽略,但分组写更干净)
    links = db.query(LiveSessionPortfolio).all()
    by_session: Dict[int, list] = {}
    for l in links:
        by_session.setdefault(l.session_id, []).append(
            {"portfolio_id": l.portfolio_strategy_id, "status": l.status}
        )
    return [
        {
            "id": s.id,
            "name": s.name,
            "mode": s.mode,
            "engine": session_engine(s),
            "execution_mode": s.execution_mode,
            # 会话级实盘资金基准：None=各组合用自身 initial_capital
            "live_capital": s.live_capital,
            # 外部信号注入（0011）：None=不启用
            "signal_file_path": s.signal_file_path,
            "status": s.status,
            # 引擎是否真的在本进程注册表中运行（工作台据此区分「状态陈旧」与
            # 真正运行中：status=running 但 engine_running=false = 后端重启后
            # 引擎未恢复的残留状态，见 recover_stale_running_live_sessions）。
            "engine_running": s.id in engines,
            "started_at": s.started_at,
            "stopped_at": s.stopped_at,
            "portfolio_ids": sorted(p["portfolio_id"] for p in by_session.get(s.id, [])),
            "portfolios": by_session.get(s.id, []),
            # I-7（audit）：运行中引擎最近一次对账差异摘要；未运行（无引擎）为 []
            "reconcile_mismatches": (
                list(engines[s.id].reconcile_mismatches) if s.id in engines else []
            ),
        }
        for s in sessions
    ]


def recover_stale_running_live_sessions(
    db: Session, engines: Optional[Dict[int, LiveEngine]] = None,
) -> int:
    """启动时清理僵尸 running 实盘会话（进程崩溃/重启残留）。

    与 ``recover_stale_running_backtests`` 同思路：引擎注册表 ``ENGINES`` 是
    进程内状态，重启后必然为空；DB 若仍留 ``status="running"``，工作台会显示
    「运行中」而实际没有任何引擎在跑（下单循环/行情轮询/成交回填全部停止，
    且 UI 无从分辨）。这里统一置 ``stopped`` 并逐条记日志（可读原因），由用户
    确认后手动重新启动。

    边界：``engines``（缺省全局注册表）非空时直接返回 0——存在运行中引擎说明
    不是冷启动，绝不能误改真实运行态。幂等：只匹配 ``status="running"``，重复
    调用返回 0。返回本次被清理的会话数。
    """
    registry = ENGINES if engines is None else engines
    if registry:
        return 0
    stale = db.query(LiveSession).filter(LiveSession.status == "running").all()
    if not stale:
        return 0
    now = datetime.now()
    for session in stale:
        session.status = "stopped"
        session.stopped_at = now
    db.commit()
    for session in stale:
        logger.warning(
            "启动恢复：实盘会话 #%s「%s」残留 running（引擎未随重启恢复，实际未运行），"
            "已置 stopped；如需继续交易请在界面重新启动",
            session.id, session.name,
        )
    return len(stale)


def create_session(db: Session, data: dict, mode: str) -> dict:
    """创建 session。新会话默认 hermes（0018，人工拉取信号执行）；老会话由迁移保持 auto。"""
    execution_mode = data.get("execution_mode") or "hermes"
    # 外部信号注入（0011）：空串/纯空白 → None（不启用）；非空原样存储（API 层同规则）
    signal_file_path = data.get("signal_file_path")
    if signal_file_path is not None:
        signal_file_path = str(signal_file_path).strip() or None
    session = LiveSession(
        name=data["name"], mode=mode, status="stopped",
        engine=data.get("engine") or default_engine(),
        execution_mode=execution_mode,
        live_capital=data.get("live_capital"),
        # 外部信号注入（0011）：None=不启用；非空=同花顺条件预警输出文件路径
        signal_file_path=signal_file_path,
    )
    db.add(session)
    db.flush()
    for pid in data.get("portfolio_ids", []):
        link = LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=pid)
        db.add(link)
    db.commit()
    return {"id": session.id, "status": session.status,
            "engine": session_engine(session),
            "execution_mode": session.execution_mode}


def get_session(db: Session, session_id: int, engine: Optional[LiveEngine] = None) -> Optional[dict]:
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    portfolios = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    data = {
        "id": session.id,
        "name": session.name,
        "mode": session.mode,
        "engine": session_engine(session),
        "execution_mode": session.execution_mode,
        # 会话级实盘资金基准：None=各组合用自身 initial_capital
        "live_capital": session.live_capital,
        # 外部信号注入（0011）：None=不启用
        "signal_file_path": session.signal_file_path,
        "status": session.status,
        "started_at": session.started_at,
        "stopped_at": session.stopped_at,
        "portfolio_ids": sorted(p.portfolio_strategy_id for p in portfolios),
        "portfolios": [
            {"portfolio_id": p.portfolio_strategy_id, "status": p.status}
            for p in portfolios
        ],
    }
    # G7（0011 §5.11）：桥状态并入 session API。运行中取引擎实时态：
    # bridge_online 为引擎内存最近已知心跳态（不新发 /ping，避免页面请求被慢桥阻塞），
    # pending_orders 在途单计数，last_backfill_time 最近一次成交回报回填时点；未运行返回空值。
    # I-7（audit）：reconcile_mismatches = 引擎内存最近一次对账差异摘要（未运行 []）。
    if engine is not None:
        data["bridge_online"] = _live_bridge_online(engine)
        data["pending_orders"] = engine.pending_orders_count
        data["last_backfill_time"] = (
            engine.last_backfill_time.strftime("%Y-%m-%d %H:%M:%S")
            if engine.last_backfill_time is not None else None
        )
        data["reconcile_mismatches"] = list(engine.reconcile_mismatches)
        # 交易日历来源/退化状态（"是否在用退化日历"必须可见；未加载年份为空）
        cal = getattr(engine, "_calendar", None)
        data["trading_calendar"] = (
            cal.status() if cal is not None and hasattr(cal, "status") else None)
    else:
        data["bridge_online"] = None
        data["pending_orders"] = 0
        data["last_backfill_time"] = None
        data["reconcile_mismatches"] = []
        data["trading_calendar"] = None
    # 账户全貌对齐：最新真实账户快照（总资产/可用资金/市值/持仓明细）。
    # 运行中优先取引擎内存最近一次快照组装（与采集同源），否则查表最近一行。
    data["account_snapshot"] = _latest_account_snapshot(db, session_id, engine)
    # 序列化兜底：任何路径混进来的 inf/-inf/nan 在这里归一为 None，
    # 保证 GET /api/live/sessions/{id} 不会因一个脏字段整接口 500。
    return _json_safe(data)


def _finite_or_none(value):
    """非有限浮点（inf / -inf / nan）→ None；其它类型原样返回。

    桥/引擎若混入非有限值（QMT 停牌持仓会返回 Infinity），float() 出来仍是
    inf，FastAPI 的 json.dumps 会抛 ValueError → 会话详情整接口 500。
    """
    if value is None or isinstance(value, bool):
        return value
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    return num if math.isfinite(num) else None


def _json_safe(value):
    """递归把非有限 float 换成 None（组装处兜底，不引入全局 JSON 编码器）。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _latest_account_snapshot(db: Session, session_id: int, engine) -> Optional[dict]:
    """最新账户快照 dict（工作台账户全貌卡）；无任何快照 → None。"""
    snap = (
        db.query(LiveAccountSnapshot)
        .filter(LiveAccountSnapshot.live_session_id == session_id)
        .order_by(LiveAccountSnapshot.captured_at.desc(),
                  LiveAccountSnapshot.id.desc())
        .first()
    )
    if snap is None:
        return None
    return {
        "engine": snap.engine,
        "captured_at": snap.captured_at.strftime("%Y-%m-%d %H:%M:%S")
        if snap.captured_at else None,
        "total_asset": _finite_or_none(snap.total_asset),
        "available_cash": _finite_or_none(snap.available_cash),
        "market_value": _finite_or_none(snap.market_value),
        "positions": [
            {
                "stock_code": p.stock_code,
                "quantity": p.quantity,
                "available_quantity": p.available_quantity,
                "cost_price": _finite_or_none(p.cost_price),
                "current_price": _finite_or_none(p.current_price),
                "market_value": _finite_or_none(p.market_value),
            }
            for p in (snap.positions or [])
        ],
    }


def delete_session(db: Session, session_id: int, engines: Dict[int, LiveEngine]) -> Tuple[bool, Optional[str]]:
    """删除 session。返回 (ok, error_message)。运行中拒绝。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return False, "资源不存在"
    if session.status == "running" or session_id in engines:
        return False, "session 运行中，请先停止再删除"
    db.query(LiveSessionPortfolio).filter(
        LiveSessionPortfolio.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()
    return True, None


# ---------------------------------------------------------------------------
# start / stop 业务体
# ---------------------------------------------------------------------------

def _check_channel_conflict(
    db: Session,
    session: LiveSession,
    engines: Dict[int, LiveEngine],
) -> Optional[Tuple[int, str]]:
    """同引擎互斥（M1 多会话并行 + 账户全貌对齐安全约束）。

    同一引擎（=同一交易通道/同一真实账户）同时仅允许一个运行中会话：
    - ths：UI 自动化独占键鼠；
    - tdx/iquant：同引擎双会话 = 两套虚拟账本交易同一真实账户（双份记账/
      互相吃单/快照对齐失真），一律 409；
    - 跨引擎（ths+tdx+iquant 各一）可并行。同组合挂多会话=复制交易语义，
      请用不同引擎，各会话独立虚拟账本（Per Portfolio）。

    遍历注册表（_START_LOCK 临界区内，注册即运行）中的会话，任一同引擎
    → 409 拒绝。返回 None 表示无冲突。
    """
    eng = session_engine(session)
    for running_id in engines:
        other = db.query(LiveSession).filter(LiveSession.id == running_id).first()
        if other is not None and session_engine(other) == eng:
            label = {
                "ths": "同花顺通道键鼠独占",
                "tdx": "通达信 TQ 账户独占（同引擎双会话会双份记账交易同一账户）",
                "iquant": "QMT 通道账户独占",
            }.get(eng, "通道独占")
            return (
                409,
                "%s：会话 %d 正在使用，请先停止" % (label, running_id),
            )
    return None


def start_session(
    db: Session,
    session_id: int,
    engines: Dict[int, LiveEngine],
    mode: str,
    db_session_factory=SessionLocal,
    dispatcher_cls=None,
) -> Tuple[Optional[LiveEngine], Optional[LiveSession], Optional[Tuple[int, str]]]:
    """启动 session 的业务体。

    返回 (engine, session, error)：
    - error 为 None 且 engine 非 None 表示成功：engine 已构造、heartbeat 通过、
      recover 已执行但尚未 start()，**未改 session 状态/未 commit**。由路由负责
      asyncio.create_task(engine.start())、engines[session_id]=engine、
      置 session.status=running + commit（注册先于 commit，闭合 B6 窗口）。
    - error 为 None 且 engine 为 None：幂等命中（session 已在跑），路由直接返回 ok running。
    - 否则 error = (http_status, message)。

    多会话并行（M1）：B6 全局限 1 已移除，改为资源互斥——仅 THS 通道键鼠独占
    （_check_channel_conflict），tdx/iquant 会话可并行；同组合挂多会话=复制交易
    （各会话独立虚拟账本，执行偏差用户自担）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None, None, (404, "资源不存在")
    if session_id in engines:
        # 幂等：已在跑，直接返回（无 engine 新建，路由据此返回 ok running）
        return None, session, None
    # B6 资源互斥（M1 多会话并行）：THS 通道键鼠独占，其余通道（tdx/iquant）
    # 可并行；组合重叠允许（复制交易语义，各会话独立虚拟账本）。
    conflict = _check_channel_conflict(db, session, engines)
    if conflict is not None:
        return None, session, conflict

    # 空股票池预检（audit ⑯）：空池会让 BarPoller 静默订阅占位 000001.SZ 空转，
    # 无任何告警；alert 与 auto 两条路径都在此拦截（位于 build_engine 之前，
    # 不依赖交易通道在线）。
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    pool_codes: set = set()
    for link in links:
        ps = db.query(PortfolioStrategy).filter_by(
            id=link.portfolio_strategy_id).first()
        if ps is not None:
            pool_codes.update(resolve_stock_codes(db, ps.id))
    if not pool_codes:
        return None, session, (
            503,
            "股票池为空：请先在组合策略中配置股票，或检查股票池同步",
        )

    execution_mode = getattr(session, "execution_mode", None) or "auto"
    try:
        engine = build_engine(
            session_id, db, mode,
            db_session_factory=db_session_factory, dispatcher_cls=dispatcher_cls,
            engine=session_engine(session),
            execution_mode=execution_mode,
            signal_file_path=getattr(session, "signal_file_path", None),
        )
    except RuntimeError as e:
        # make_dispatcher 对未知引擎键/依赖缺失（pywinauto 等）/路径账号未配置
        # 统一抛 RuntimeError；这里必须转成 503 可操作提示，不能漏成 500。
        return None, session, (503, str(e) or "交易通道未就绪")
    # 启动前探测通道在线：客户端未起/未登录直接拒绝，不建引擎、不置 running，
    # 避免「一点就成功、通道离线却无告警」。heartbeat() 本地 loopback 瞬时失败，
    # 不阻塞事件循环。
    if not engine.dispatcher.heartbeat():
        channel = getattr(engine.dispatcher, "channel", "")
        detail = getattr(engine.dispatcher, "last_error", "") or ""
        if channel == "alert":
            return None, session, (
                503,
                "通达信行情未就绪：请先启动并登录通达信客户端（%s）"
                % (detail or "未取到行情"),
            )
        if channel == "ths_ui":
            ths = ths_ui_config()
            return None, session, (
                503,
                "同花顺交易客户端未就绪：请先启动并登录「%s」（%s）"
                % (ths["window_title"], detail or "未找到交易窗口"),
            )
        if channel == "tdx_tq":
            return None, session, (
                503,
                "通达信交易未就绪：请先启动并登录通达信交易客户端（%s）"
                % (detail or "未获取到资金账号"),
            )
        channel = getattr(engine.dispatcher, "channel", "")
        base_url = getattr(engine.dispatcher, "_base_url", "") or "未知"
        if channel in ("goldminer_bridge", "iquant_bridge"):
            # 分级提示：端口无监听 / 已监听但无响应 / 应答格式不符，三档可区分，
            # 避免把「桥没跑」与「桥卡在查询」混成一句「桥未启动」误导排查方向。
            reason = getattr(engine.dispatcher, "last_probe_error", None)
            return None, session, (
                503, bridge_probe_message(channel, reason, base_url))
        return None, session, (
            503,
            "桥未启动：%s— 请先在对应客户端加载并运行对应桥策略" % base_url,
        )
    # OCR 启动闸仅 direct 模式：file_builtin 下单不做 OCR 前置拒单（OCR 守卫在
    # 执行器 _execute_via_ui 里，失败落 results rejected 回执），无需阻断启动。
    if (getattr(engine.dispatcher, "channel", "") == "ths_ui"
            and getattr(engine.dispatcher, "execute_mode", "direct") == "direct"
            and not ths_ui_config()["captcha_ocr"]):
        return None, session, (
            503,
            "同花顺复制表格会触发验证码：请先在系统配置启用「验证码 OCR」再启动实盘",
        )
    preflight_err = preflight_signal_config(db, session_id, execution_mode)
    if preflight_err is not None:
        return None, session, preflight_err
    if not is_signal_only(execution_mode):
        # 重启恢复：从 live_trades 重放虚拟持仓/虚拟现金（alert/hermes 无下单/成交）
        engine.recover(db)
    return engine, session, None


def stop_session(
    db: Session,
    session_id: int,
    engines: Dict[int, LiveEngine],
) -> Optional[LiveEngine]:
    """停止 session 业务体：从注册表弹出引擎、改状态、commit。

    返回弹出的 engine（供路由 asyncio.create_task(engine.stop())）；session 不存在返回 None。
    **引擎的异步 stop() 由路由调度**（本函数同步，不碰事件循环）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    engine = engines.pop(session_id, None)
    session.status = "stopped"
    session.stopped_at = datetime.now()
    db.commit()
    return engine


# ---------------------------------------------------------------------------
# 熔断手动恢复 / 桥状态
# ---------------------------------------------------------------------------

def recover_breaker(
    db: Session,
    session_id: int,
    portfolio_id: int,
    engines: Dict[int, LiveEngine],
) -> Tuple[bool, Optional[str]]:
    """手动恢复某组合熔断（3 次转手动恢复后的人工恢复入口）。

    引擎运行中：engine.recover_breaker 负责内存态 + DB 双写；
    引擎未运行：只改 DB（下次 start → recover 读回 count=0 不补挂，组合正常）。
    返回 (ok, error_message)。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return False, "资源不存在"
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=session_id, portfolio_strategy_id=portfolio_id,
    ).first()
    if link is None:
        return False, "该组合不在此 session 中"
    engine = engines.get(session_id)
    if engine is not None:
        # 运行中：引擎方法负责内存 + DB 双写
        if not engine.recover_breaker(portfolio_id):
            return False, "该组合不在此 session 引擎中"
    else:
        # 未运行：只改 DB（下次 start → recover 只在 count>=3 转手动；count=0 不补挂）
        link.circuit_breaker_count = 0
        link.status = "active"
        db.commit()
    return True, None


def bridge_status(session_id: int, status: str, engine: Optional[LiveEngine]) -> dict:
    """桥在线状态（§11 切片5 并入）。引擎运行中取最近已知在线态（不新发 /ping）；未运行返回 not_running。"""
    if engine is None:
        return {"online": None, "status": "not_running"}
    return {"online": _live_bridge_online(engine), "status": status}


# ---------------------------------------------------------------------------
# 历史查询 + 序列化
# ---------------------------------------------------------------------------

_SH_TZ = timezone(timedelta(hours=8))


def _iso_cst(dt: Optional[datetime]) -> Optional[str]:
    """上海墙钟 naive（bar_time/trade_time）→ 显式 +08:00。

    前端统一按 Asia/Shanghai 展示，依赖后端显式时区；naive 字符串不再猜测。
    """
    return dt.replace(tzinfo=_SH_TZ).isoformat() if dt else None


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """UTC naive（``server_default=func.now()`` 的 created_at/updated_at）→ +00:00。"""
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def serialize_order(o: LiveOrder) -> dict:
    return {
        "id": o.id,
        "live_session_id": o.live_session_id,
        "portfolio_strategy_id": o.portfolio_strategy_id,
        "strategy_id": o.strategy_id,
        "stock_code": o.stock_code,
        # 引擎写入 lower（buy/sell），统一大写对齐回测与前端标签（历史行兼容）。
        "trade_type": (o.trade_type or "").upper(),
        "order_type": o.order_type,
        "price": float(o.price) if o.price is not None else None,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "filled_price": float(o.filled_price) if o.filled_price is not None else None,
        "status": o.status,
        "error_message": o.error_message,
        "signal_name": o.signal_name,
        "signal_type": o.signal_type,
        "bar_time": _iso_cst(o.bar_time),
        "order_ref": o.order_ref,
        "bridge_order_id": o.bridge_order_id,
        "created_at": _iso_utc(o.created_at),
        "updated_at": _iso_utc(o.updated_at),
    }


def serialize_trade(t: LiveTrade) -> dict:
    return {
        "id": t.id,
        "live_session_id": t.live_session_id,
        "live_order_id": t.live_order_id,
        "portfolio_strategy_id": t.portfolio_strategy_id,
        "strategy_id": t.strategy_id,
        "stock_code": t.stock_code,
        "trade_type": (t.trade_type or "").upper(),
        "price": float(t.price) if t.price is not None else None,
        "quantity": t.quantity,
        "amount": float(t.amount) if t.amount is not None else None,
        "commission": float(t.commission) if t.commission is not None else None,
        "stamp_duty": float(t.stamp_duty) if t.stamp_duty is not None else None,
        "trade_time": _iso_cst(t.trade_time),
        "created_at": _iso_utc(t.created_at),
    }


_OVERSELL_WARNED: set = set()
_OVERSELL_LOCK = threading.Lock()


def _warn_replay_oversell(session_id, code, sell_qty, held_qty) -> None:
    """重放发现超卖流水告警（每 (session, code) 进程内只报一次，避免刷屏）。

    期初外部导入持仓未落 live_trades，重放时「只有卖出」会对空仓卖 ——
    ``Position.sell`` 把数量钳制为 0（不会产生负数），但账实不符必须暴露。
    """
    key = (session_id, code)
    with _OVERSELL_LOCK:
        if key in _OVERSELL_WARNED:
            return
        _OVERSELL_WARNED.add(key)
    logger.warning(
        "重放发现超卖流水（缺少买入/期初导入流水）：session=%s code=%s "
        "卖出=%s 当前持仓=%s——按 0 钳制、不展示负数",
        session_id, code, sell_qty, held_qty)


def _display_price(value) -> float:
    """成本价展示字段：收敛到 3 位小数（计算与存储仍全精度）。"""
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.001")))
    except (ArithmeticError, TypeError, ValueError):
        return 0.0


def _display_money(value) -> float:
    """金额展示字段：收敛到 2 位小数（分的精度；计算与存储仍全精度）。"""
    d = dec_money(value)
    return float(d) if d is not None else 0.0


def aggregate_positions_from_trades(trades: List[LiveTrade]) -> List[dict]:
    """从 live_trades 重放聚合虚拟持仓（与 recover/Position.apply_trade 同口径）。

    按 trade_time 顺序：BUY 加权累加持仓+均价，SELL 减仓均价不变。按 (组合策略, 子策略, 股票)
    分组，同票被多个子策略持有时各成一行。返回按 (股票, 组合, 子策略) 排序。
    """
    agg: Dict[tuple, Position] = {}
    for tr in trades:
        key = (tr.portfolio_strategy_id, tr.strategy_id, tr.stock_code)
        pos = agg.get(key)
        if pos is None:
            pos = Position(tr.stock_code)
            agg[key] = pos
        if (TradeType(tr.trade_type.upper()) == TradeType.SELL
                and pos.quantity <= 0):
            # 无买入/期初导入流水却出现卖出 → 脏数据，告警但不制造负数
            _warn_replay_oversell(
                tr.live_session_id, tr.stock_code, tr.quantity, pos.quantity)
        pos.apply_trade(TradeEvent(
            strategy_id=tr.strategy_id,
            portfolio_id=tr.portfolio_strategy_id,
            stock_code=tr.stock_code,
            trade_type=TradeType(tr.trade_type.upper()),
            price=Decimal(str(tr.price)),
            quantity=tr.quantity,
            amount=Decimal(str(tr.amount)),
            commission=Decimal(str(tr.commission)),
            stamp_duty=Decimal(str(tr.stamp_duty)),
            trade_time=tr.trade_time,
        ))
    rows = []
    for (pid, sid, code), pos in agg.items():
        if pos.quantity <= 0:
            continue  # 已清仓（含超卖被钳制为 0），不展示负数
        rows.append({
            "portfolio_id": pid,
            "strategy_id": sid,
            "stock_code": code,
            "quantity": pos.quantity,
            "avg_cost": _display_price(pos.avg_cost),
            "market_value": _display_money(pos.market_value),
        })
    return sorted(rows, key=lambda r: (r["stock_code"], r["portfolio_id"], r["strategy_id"]))


def engine_virtual_positions(engine: LiveEngine) -> List[dict]:
    """运行中：读引擎内存态虚拟持仓，一行一个 (组合策略, 子策略, 股票)。

    只读引擎内存态，不修改 Position 对象；含当日已成交未落库的变动（更实时）。
    同票被多个子策略持有时各成一行（不再按 code 汇总净仓）。按 (股票, 组合, 子策略) 排序。
    """
    # 组合→股票池代码集（兜底归属标注用）：不在池内却被 _strategy_ctx_for_code
    # 挂到首个组合的仓位，attribution=fallback（前端显示「非池内·兜底归属」）。
    pools = getattr(engine, "_pool_codes_by_portfolio", None) or {}
    rows = []
    for port in engine.portfolios:
        pool_codes = pools.get(port.portfolio_id)
        for ctx in port.strategies:
            for code, pos in ctx.positions.items():
                if pos.quantity <= 0:
                    continue  # 已清仓；负数不应出现（sell 已钳制），防御性跳过
                row = {
                    "portfolio_id": port.portfolio_id,
                    "strategy_id": ctx.strategy_id,
                    "stock_code": code,
                    "quantity": pos.quantity,
                    "avg_cost": _display_price(pos.avg_cost),
                    "market_value": _display_money(pos.market_value),
                }
                # 池信息缺失（旧引擎/测试）时不臆断，省略字段由前端按 pool 降级
                if pool_codes is not None:
                    row["attribution"] = "pool" if code in pool_codes else "fallback"
                rows.append(row)
    return sorted(rows, key=lambda r: (r["stock_code"], r["portfolio_id"], r["strategy_id"]))


# 外部持仓（真实账户原有持仓）读回缓存 TTL：THS UI 读回慢（约 3~10s，含验证码
# 流水线），60s 内复用上次读回，避免工作台每次刷新都重走 UI 自动化。
_EXTERNAL_POS_TTL_SECONDS = 60.0


def _normalize_external_symbol(code) -> str:
    """外部持仓代码归一：带后缀原样返回；6 位数字按首位推断交易所补后缀
    （92→BJ（北交所新股段）、6/5/9→SH、4/8→BJ、其余→SZ，与 tdx_tq_dispatcher._exchange_for 同口径）。

    无法归一（非 6 位数字且无后缀）返回空串，调用方丢弃该行。
    """
    text = str(code or "").strip()
    if "." in text:
        return text
    digits = re.sub(r"\D", "", text)[:6]
    if len(digits) != 6:
        return ""
    if digits.startswith("92"):
        suffix = "BJ"
    elif digits.startswith("11"):
        suffix = "SH"  # 沪市可转债 110/113/118
    elif digits.startswith(("6", "5", "9")):
        suffix = "SH"
    elif digits.startswith(("4", "8")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return "%s.%s" % (digits, suffix)


def engine_external_positions(engine: LiveEngine) -> List[dict]:
    """运行中：读 dispatcher 真实账户持仓，剔除策略虚拟持仓已持有的代码，
    剩余即「原有持仓」（非策略买入，仅供监视，不参与策略交易）。

    读回结果（dispatcher 原始行）带 60s TTL 缓存在引擎属性 _external_pos_cache
    （(monotonic_ts, rows)）上——THS UI 读回慢，TDX SDK 读快但同样走缓存统一口径。
    dispatcher 缺 get_positions（iquant 桥）/异常/None → 无外部行，不抛错。
    """
    dispatcher = getattr(engine, "dispatcher", None)
    if dispatcher is None:
        return []
    virtual_codes = {r["stock_code"] for r in engine_virtual_positions(engine)}
    now = time.monotonic()
    real = None
    cached_rows = None
    cache = getattr(engine, "_external_pos_cache", None)
    if cache is not None:
        try:
            ts, rows = cache
            if isinstance(rows, list):
                cached_rows = rows
            if cached_rows is not None and now - ts < _EXTERNAL_POS_TTL_SECONDS:
                real = cached_rows
        except Exception:
            real = None
    if real is None:
        try:
            fn = getattr(dispatcher, "get_positions", None)
            fresh = fn() if callable(fn) else []
        except Exception:
            # 读失败（如 TdxTqDispatcher 的 BridgeUnavailableError）不是「零持仓」：
            # 不刷新缓存、沿用上次成功结果，避免瞬时失败把外部持仓闪成空。
            logger.warning("外部持仓读回失败（dispatcher.get_positions）", exc_info=True)
            real = cached_rows or []
        else:
            if isinstance(fresh, list):
                real = fresh
                try:
                    engine._external_pos_cache = (now, real)
                except Exception:
                    pass
            else:
                # None（ThsUiDispatcher 读表失败契约）同样不是真空仓：
                # 不得写成空缓存，否则 60s TTL 内外部持仓会一直闪空。
                logger.warning("外部持仓读回失败（dispatcher.get_positions 返回 None）")
                real = cached_rows or []
    out = []
    for r in real:
        if not isinstance(r, dict):
            continue
        code = _normalize_external_symbol(r.get("symbol"))
        if not code or code in virtual_codes:
            continue
        try:
            quantity = int(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        try:
            avg_cost = float(r.get("cost") or 0)
            market_value = float(r.get("market_value") or 0)
        except (TypeError, ValueError):
            avg_cost, market_value = 0.0, 0.0
        out.append({
            "portfolio_id": None,
            "strategy_id": None,
            "stock_code": code,
            "quantity": quantity,
            "avg_cost": _display_price(avg_cost),
            "market_value": _display_money(market_value),
            "source": "external",
        })
    return out


def session_orders(db: Session, session_id: int, status: Optional[str] = None) -> Optional[List[dict]]:
    """委托历史。session 不存在返回 None。可选 status 过滤。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    q = db.query(LiveOrder).filter(LiveOrder.live_session_id == session_id)
    if status:
        q = q.filter(LiveOrder.status == status)
    rows = q.order_by(LiveOrder.created_at.desc(), LiveOrder.id.desc()).all()
    return [serialize_order(o) for o in rows]


def session_trades(db: Session, session_id: int) -> Optional[List[dict]]:
    """成交历史，按 trade_time 倒序。session 不存在返回 None。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    rows = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time.desc(), LiveTrade.id.desc())
        .all()
    )
    return [serialize_trade(t) for t in rows]


def session_positions(db: Session, session_id: int, engine: Optional[LiveEngine]) -> Optional[List[dict]]:
    """持仓分级（策略持仓/原有持仓）。运行中读引擎内存态虚拟持仓（含未落库当日
    变动，标 source=strategy）+ dispatcher 真实持仓差集（原有持仓，source=external）；
    停止后从 live_trades 重放聚合（无外部数据可得，全部标 source=strategy）。

    session 不存在返回 None。返回 List[dict]，前端按 source 分级渲染。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    if engine is not None:
        rows = engine_virtual_positions(engine)
        for r in rows:
            r["source"] = "strategy"
        rows.extend(engine_external_positions(engine))
        return rows
    trades = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time, LiveTrade.id)
        .all()
    )
    rows = aggregate_positions_from_trades(trades)
    for r in rows:
        r["source"] = "strategy"
    return rows


def serialize_decision(d: LiveDecisionEvent) -> dict:
    """决策闸门事件序列化（实盘/回测同字段口径）。"""
    return {
        "id": d.id,
        "gate": d.gate,
        "layer": d.layer,
        "action": d.action,
        "portfolio_id": d.portfolio_id,
        "strategy_id": d.strategy_id,
        "stock_code": d.stock_code,
        "bar_time": _iso_cst(d.bar_time),
        "param_name": d.param_name,
        "param_value": d.param_value,
        "actual_value": d.actual_value,
        "requested_qty": d.requested_qty,
        "final_qty": d.final_qty,
        "message": d.message,
        "created_at": _iso_utc(d.created_at),
    }


def session_decisions(db: Session, session_id: int) -> Optional[dict]:
    """决策闸门事件：聚合统计 + 原始事件。session 不存在返回 None。

    summary 复用 summarize_decisions（与回测详情同口径），按 (gate, param_name)
    分组计数；events 按 bar_time 正序返回（下钻看逐次触发）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    rows = (
        db.query(LiveDecisionEvent)
        .filter(LiveDecisionEvent.live_session_id == session_id)
        .order_by(LiveDecisionEvent.bar_time, LiveDecisionEvent.id)
        .all()
    )
    return {
        "summary": summarize_decisions(rows),
        "events": [serialize_decision(d) for d in rows],
    }


def serialize_signal(s: LiveSignal) -> dict:
    return {
        "id": s.id,
        "portfolio_id": s.portfolio_strategy_id,
        "strategy_id": s.strategy_id,
        "stock_code": s.stock_code,
        "period": s.period,
        "bar_time": _iso_cst(s.bar_time),
        "formula_name": s.formula_name,
        "signal_name": s.signal_name,
        "signal_type": s.signal_type,
        "trigger_value": s.trigger_value,
        "price": str(s.price) if s.price is not None else None,
        "execution_mode": s.execution_mode,
        "status": s.status,
        "alert_status": s.alert_status,
        "alert_error": s.alert_error,
        "tdx_run_id": s.tdx_run_id,
        "created_at": _iso_utc(s.created_at),
        # alerted_at 由 SignalMonitor 用 now_shanghai() 写入（非 server_default），
        # 按上海时区标注；标成 UTC 会让外部消费方偏移 8 小时。
        "alerted_at": _iso_cst(s.alerted_at),
    }


def session_signals(db, session_id, strategy_id=None, stock_code=None,
                    signal_type=None, execution_mode=None, alert_status=None,
                    start=None, end=None, limit=100, offset=0):
    if db.query(LiveSession).filter(LiveSession.id == session_id).first() is None:
        return None
    q = db.query(LiveSignal).filter(LiveSignal.live_session_id == session_id)
    if strategy_id is not None:
        q = q.filter(LiveSignal.strategy_id == strategy_id)
    if stock_code:
        q = q.filter(LiveSignal.stock_code == stock_code)
    if signal_type:
        q = q.filter(LiveSignal.signal_type == signal_type)
    if execution_mode:
        q = q.filter(LiveSignal.execution_mode == execution_mode)
    if alert_status:
        q = q.filter(LiveSignal.alert_status == alert_status)
    if start is not None:
        q = q.filter(LiveSignal.bar_time >= start)
    if end is not None:
        q = q.filter(LiveSignal.bar_time <= end)
    total = q.count()
    rows = (q.order_by(LiveSignal.bar_time.desc(), LiveSignal.id.desc())
            .offset(offset).limit(min(max(limit, 1), 500)).all())
    return {"items": [serialize_signal(r) for r in rows], "total": total}
