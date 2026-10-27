"""实盘 service 层 — 业务逻辑（从 core.api.live 提取）。

与 core.services.backtest_service 同一范式：路由层仅留 HTTP 壳（参数校验 +
守卫/调度 + 调 service + ok/err 包装），业务逻辑下沉到本模块。本模块承接：

- 引擎组装：build_engine（查 link/PS/strategy/股票池/Formula，算 code_period_count，
  建 dispatcher/poller/LiveEngine）
- 进程注册表：ENGINES（session_id -> LiveEngine）+ B6 全局限 1 个 session 守卫
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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.config import load_config
from core.db import SessionLocal
from core.models import (
    LiveSession, LiveSessionPortfolio, PortfolioStrategy, Strategy,
    StockPoolStock, Formula, FormulaSignal, LiveOrder, LiveTrade, LiveDecisionEvent,
    LiveSignal,
)
from core.engine.portfolio_builder import assemble_portfolio
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
try:
    from core.engine.qmt_dispatcher import QmtDispatcher
except Exception:
    QmtDispatcher = None  # type: ignore
from core.engine.bar_poller import BarPoller
from core.engine.live_engine import LiveEngine
from core.engine.position import Position
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

def bridge_config() -> dict:
    """读双桥地址：config.iquant_bridge 段，simulation(仿真/虚拟资金)/live(实盘/真实资金)
    各一个 base_url。桥绑 loopback，单用户本机部署，不鉴权（token 已移除）。

    session.mode = simulation|live 决定走哪个桥（账号差异，见 live/bridge/README.md）。
    模拟/实盘（信号 vs 真实下单）不由 Core 控制，由 iQuant 客户端启动按钮决定。
    """
    cfg = load_config()
    br = cfg.get("iquant_bridge", {}) if isinstance(cfg, dict) else {}
    sim = br.get("simulation", {}) if isinstance(br, dict) else {}
    liv = br.get("live", {}) if isinstance(br, dict) else {}
    return {
        "simulation": sim.get("base_url", "http://127.0.0.1:8790"),
        "live": liv.get("base_url", "http://127.0.0.1:8791"),
    }


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
    }


def qmt_config() -> dict:
    """读 miniQMT 交易配置（config.qmt 段）。"""
    cfg = load_config()
    qmt = cfg.get("qmt", {}) if isinstance(cfg, dict) else {}
    qmt = qmt if isinstance(qmt, dict) else {}
    return {
        "available": QmtDispatcher is not None,
        "path": qmt.get("path") or "",
        "account": qmt.get("account") or "",
        "account_type": qmt.get("account_type") or "STOCK",
        "price_mode": qmt.get("price_mode") or "fix",
        "xtquant_path": qmt.get("xtquant_path") or "",
    }


#: 实盘交易引擎：key -> (渠道标识, 中文名)。渠道标识是 Core 状态机/前端识别的通道名。
ENGINES_META = {
    "iquant": ("iquant_bridge", "iQuant HTTP 桥"),
    "tdx": ("tdx_tq", "通达信 TQ 交易"),
    "ths": ("ths_ui", "同花顺独立交易（UI 自动化）"),
    "qmt": ("qmt", "miniQMT 交易"),
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
    """
    explicit = getattr(session, "engine", None)
    return explicit if explicit in ENGINES_META else default_engine()


def make_dispatcher(engine: str, mode: str = "simulation", session_id: int = 0):
    """按引擎标识构造 dispatcher（三个可选引擎 + iQuant 桥）。

    依赖缺失/未配置时抛 RuntimeError，由 start_session 转成 503 明确告警，
    绝不静默回退到别的通道（避免"以为在通达信下单、实际打到 iQuant"）。
    """
    if engine == "ths":
        if ThsUiDispatcher is None:
            raise RuntimeError("未安装同花顺 UI 自动化依赖（pywinauto/pyperclip）")
        ths = ths_ui_config()
        return ThsUiDispatcher(
            window_title=ths["window_title"], client_exe_path=ths["client_exe_path"],
            captcha_ocr=ths["captcha_ocr"],
        )
    if engine == "tdx":
        if TdxTqDispatcher is None:
            raise RuntimeError("通达信 TQ 交易通道不可用")
        cfg = tq_trade_config()
        return TdxTqDispatcher(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
        )
    if engine == "qmt":
        if QmtDispatcher is None:
            raise RuntimeError("miniQMT 交易通道不可用")
        cfg = qmt_config()
        return QmtDispatcher(
            path=cfg["path"], account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], xtquant_path=cfg["xtquant_path"],
            session_id=session_id,
        )
    if HttpBridgeDispatcher is None:
        raise RuntimeError("HttpBridgeDispatcher not available")
    br = bridge_config()
    return HttpBridgeDispatcher(base_url=br.get(mode, br["simulation"]))


def engine_status(engine: Optional[str]) -> dict:
    """引擎清单 + 已配置情况（前端下拉与通道条共用）。"""
    ths = ths_ui_config()
    tq = tq_trade_config()
    qmt = qmt_config()
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
        elif key == "qmt":
            configured = qmt["available"] and bool(qmt["path"]) and bool(qmt["account"])
            if not qmt["available"]:
                detail = "未安装 xtquant"
            elif not qmt["path"] or not qmt["account"]:
                detail = "未配置 QMT 路径/资金账号"
            else:
                detail = qmt["account"]
        elif key == "iquant":
            br = bridge_config()
            detail = "%s / %s" % (br["simulation"], br["live"])
        items.append({
            "key": key, "channel": channel, "label": label,
            "configured": configured, "detail": detail,
        })
    current = engine or default_engine()
    channel, label = ENGINES_META.get(current, ENGINES_META["iquant"])
    return {
        "engine": current, "active": channel, "label": label, "engines": items,
        "ths_ui": ths, "tq_trade": tq, "qmt": qmt, "bridge": bridge_config(),
    }


def channel_status(engine: Optional[LiveEngine] = None,
                    engine_key: Optional[str] = None) -> dict:
    """当前生效的下单通道 + 在线状态（实盘页顶部通道条）。

    engine_key 为 session.engine；未传时沿用旧行为（ths_ui.enabled → ths，否则 iquant）。
    engine 为运行中的 LiveEngine 时取其 dispatcher 心跳，未运行 online=None（未知）。
    """
    data = engine_status(engine_key)
    online = None
    if engine is not None:
        try:
            online = bool(engine.dispatcher.heartbeat())
        except Exception:
            online = False
    data["online"] = online
    return data


def test_channel(channel: str = "ths_ui", mode: str = "simulation") -> dict:
    """只读连通性探测（实盘页/系统配置页「测试连接」按钮）。

    ths_ui：连接 xiadan.exe 窗口并读一次持仓（只读，不下单）；
    iquant_bridge：请求对应模式桥的 /ping；
    tdx_tq：连通达信 TQ 交易账户并读一次持仓（只读）；
    qmt：连 miniQMT 并读一次持仓（只读）。
    """
    if channel == "tdx_tq":
        if TdxTqDispatcher is None:
            return {"ok": False, "channel": "tdx_tq", "error": "通达信 TQ 交易通道不可用"}
        cfg = tq_trade_config()
        dispatcher = TdxTqDispatcher(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
        )
        try:
            if not dispatcher.heartbeat():
                return {"ok": False, "channel": "tdx_tq",
                        "error": dispatcher.last_error or "通达信交易未就绪"}
            rows = dispatcher.query_positions()
            if dispatcher.last_error:
                return {"ok": False, "channel": "tdx_tq", "error": dispatcher.last_error}
            return {"ok": True, "channel": "tdx_tq", "positions": len(rows)}
        finally:
            dispatcher.close()
    if channel == "qmt":
        if QmtDispatcher is None:
            return {"ok": False, "channel": "qmt", "error": "miniQMT 交易通道不可用"}
        cfg = qmt_config()
        dispatcher = QmtDispatcher(
            path=cfg["path"], account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], xtquant_path=cfg["xtquant_path"],
        )
        try:
            if not dispatcher.heartbeat():
                return {"ok": False, "channel": "qmt",
                        "error": dispatcher.last_error or "QMT 交易未就绪"}
            rows = dispatcher.query_positions()
            return {"ok": True, "channel": "qmt", "positions": len(rows)}
        finally:
            dispatcher.close()
    if channel == "ths_ui":
        if ThsUiDispatcher is None:
            return {"ok": False, "channel": "ths_ui", "error": "未安装 UI 自动化依赖（pywinauto/pyperclip）"}
        cfg = ths_ui_config()
        dispatcher = ThsUiDispatcher(
            window_title=cfg["window_title"], client_exe_path=cfg["client_exe_path"],
            captcha_ocr=cfg["captcha_ocr"],
        )
        try:
            if not dispatcher.connect():
                return {"ok": False, "channel": "ths_ui",
                        "error": dispatcher.last_error or "未找到同花顺交易客户端窗口"}
            if not cfg["captcha_ocr"]:
                return {"ok": False, "channel": "ths_ui",
                        "error": "同花顺复制表格会触发验证码，请先在系统配置启用验证码 OCR"}
            rows = dispatcher.query_positions()
            if dispatcher.last_error:
                return {"ok": False, "channel": "ths_ui",
                        "error": dispatcher.last_error}
            return {"ok": True, "channel": "ths_ui", "window_title": cfg["window_title"],
                    "positions": len(rows)}
        finally:
            dispatcher.close()
    br = bridge_config()
    base_url = br.get(mode, br["simulation"])
    try:
        dispatcher = HttpBridgeDispatcher(base_url=base_url)
        online = dispatcher.heartbeat()
        return {"ok": bool(online), "channel": "iquant_bridge", "base_url": base_url,
                "error": None if online else "iQuant 桥未响应（客户端未启动或桥策略未加载）"}
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
    if execution_mode not in ("alert", "auto"):
        return (400, "execution_mode 必须为 alert/auto")
    for pid, st in _session_strategies(db, session_id):
        if st.formula_id is None:
            if execution_mode == "alert":
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
    if execution_mode == "alert":
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
) -> LiveEngine:
    """组装一个 session 的 LiveEngine：各组合 Portfolio + dispatcher + BarPoller。

    公式注入（0010）：遍历各组合策略，按 Strategy.formula_id → Formula.name 预加载
    {strategy_id: formula_name}，连同 TQFormula 实例传入 LiveEngine，供 _fill_signal_cache
    逐 bar 内存注入算公式信号。无公式的策略不入映射（_fill_signal_cache 跳过）。

    dispatcher_cls：注入 dispatcher 类（测试 monkeypatch 用）；None 时按 engine 选：
    engine=None → 沿用旧行为（ths_ui.enabled → 同花顺，否则 iQuant 桥）；
    engine=tdx/qmt → 通达信 TQ / miniQMT。

    execution_mode=alert 时固定使用 AlertDataDispatcher（TQ 行情、禁止下单），
    优先于 dispatcher_cls/engine 选择；auto 保持既有下单通道行为。
    """
    if execution_mode == "alert":
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
        )
    elif dispatcher_cls is TdxTqDispatcher and TdxTqDispatcher is not None:
        cfg = tq_trade_config()
        dispatcher = dispatcher_cls(
            account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], client_path=cfg["client_path"],
        )
    elif dispatcher_cls is QmtDispatcher and QmtDispatcher is not None:
        cfg = qmt_config()
        dispatcher = dispatcher_cls(
            path=cfg["path"], account=cfg["account"], account_type=cfg["account_type"],
            price_mode=cfg["price_mode"], xtquant_path=cfg["xtquant_path"],
            session_id=session_id,
        )
    else:
        br = bridge_config()
        dispatcher = dispatcher_cls(base_url=br.get(mode, br["simulation"]))
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
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
    for link in links:
        ps = db.query(PortfolioStrategy).filter_by(id=link.portfolio_strategy_id).first()
        if ps is None:
            continue
        strategies = db.query(Strategy).filter_by(portfolio_id=ps.id).order_by(Strategy.id).all()
        port = assemble_portfolio(ps, strategies, db)
        portfolios.append(port)
        benchmark_codes[ps.id] = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
        pool_codes = resolve_stock_codes(db, ps.id)
        stock_codes.update(pool_codes)
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
    try:
        poller = BarPoller(
            dispatcher, sorted(stock_codes) or ["000001.SZ"],
            period="1m", count=10,
        )
    except Exception:
        poller = None  # type: ignore
    return LiveEngine(
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
        regime_provider=_regime_provider,
        benchmark_codes=benchmark_codes,
        execution_mode=execution_mode,
        formula_id_by_strategy=strategy_formula,
        enable_signal_monitor=True,
    )


# ---------------------------------------------------------------------------
# session CRUD
# ---------------------------------------------------------------------------

def list_sessions(db: Session) -> list:
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
            "status": s.status,
            "started_at": s.started_at,
            "stopped_at": s.stopped_at,
            "portfolio_ids": sorted(p["portfolio_id"] for p in by_session.get(s.id, [])),
            "portfolios": by_session.get(s.id, []),
        }
        for s in sessions
    ]


def create_session(db: Session, data: dict, mode: str) -> dict:
    """创建 session。新会话默认 alert；老会话由迁移保持 auto。"""
    execution_mode = data.get("execution_mode") or "alert"
    session = LiveSession(
        name=data["name"], mode=mode, status="stopped",
        engine=data.get("engine") or default_engine(),
        execution_mode=execution_mode,
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
    # bridge_online 为实时心跳（/ping），pending_orders 在途单计数，last_backfill_time
    # 最近一次成交回报回填时点；未运行返回空值。
    if engine is not None:
        data["bridge_online"] = engine.dispatcher.heartbeat()
        data["pending_orders"] = engine.pending_orders_count
        data["last_backfill_time"] = (
            engine.last_backfill_time.strftime("%Y-%m-%d %H:%M:%S")
            if engine.last_backfill_time is not None else None
        )
    else:
        data["bridge_online"] = None
        data["pending_orders"] = 0
        data["last_backfill_time"] = None
    return data


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
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None, None, (404, "资源不存在")
    if session_id in engines:
        # 幂等：已在跑，直接返回（无 engine 新建，路由据此返回 ok running）
        return None, session, None
    if engines:
        # B6：全局限 1 个实盘 session（防多引擎争抢桥单线程 + 持仓归属混乱，Q1 未解）。
        running_id = next(iter(engines))
        return None, session, (409, "已有实盘会话 %d 运行中，全局限 1 个" % running_id)

    execution_mode = getattr(session, "execution_mode", None) or "auto"
    engine = build_engine(
        session_id, db, mode,
        db_session_factory=db_session_factory, dispatcher_cls=dispatcher_cls,
        engine=session_engine(session),
        execution_mode=execution_mode,
    )
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
        if channel == "qmt":
            return None, session, (
                503,
                "QMT 交易未就绪：请先启动 QMT 客户端并开启极简模式（%s）"
                % (detail or "未连接"),
            )
        mode_label = "实盘" if mode == "live" else "仿真"
        br = bridge_config()
        base_url = br.get(mode, br["simulation"])
        return None, session, (
            503,
            "桥未启动：%s（%s）— 请先在 iQuant 客户端加载并运行对应桥策略"
            % (mode_label, base_url),
        )
    if getattr(engine.dispatcher, "channel", "") == "ths_ui" and not ths_ui_config()["captcha_ocr"]:
        return None, session, (
            503,
            "同花顺复制表格会触发验证码：请先在系统配置启用「验证码 OCR」再启动实盘",
        )
    preflight_err = preflight_signal_config(db, session_id, execution_mode)
    if preflight_err is not None:
        return None, session, preflight_err
    if execution_mode != "alert":
        # 重启恢复：从 live_trades 重放虚拟持仓/虚拟现金（alert 无下单/成交）
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
    """桥在线状态（§11 切片5 并入）。引擎运行中取 dispatcher.heartbeat()；未运行返回 not_running。"""
    if engine is None:
        return {"online": None, "status": "not_running"}
    return {"online": engine.dispatcher.heartbeat(), "status": status}


# ---------------------------------------------------------------------------
# 历史查询 + 序列化
# ---------------------------------------------------------------------------

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
        "bar_time": o.bar_time.isoformat() if o.bar_time else None,
        "order_ref": o.order_ref,
        "bridge_order_id": o.bridge_order_id,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
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
        "trade_time": t.trade_time.isoformat() if t.trade_time else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


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
    rows = [
        {
            "portfolio_id": pid,
            "strategy_id": sid,
            "stock_code": code,
            "quantity": pos.quantity,
            "avg_cost": float(pos.avg_cost),
            "market_value": float(pos.market_value),
        }
        for (pid, sid, code), pos in agg.items()
        if pos.quantity != 0
    ]
    return sorted(rows, key=lambda r: (r["stock_code"], r["portfolio_id"], r["strategy_id"]))


def engine_virtual_positions(engine: LiveEngine) -> List[dict]:
    """运行中：读引擎内存态虚拟持仓，一行一个 (组合策略, 子策略, 股票)。

    只读引擎内存态，不修改 Position 对象；含当日已成交未落库的变动（更实时）。
    同票被多个子策略持有时各成一行（不再按 code 汇总净仓）。按 (股票, 组合, 子策略) 排序。
    """
    rows = []
    for port in engine.portfolios:
        for ctx in port.strategies:
            for code, pos in ctx.positions.items():
                if pos.quantity == 0:
                    continue
                rows.append({
                    "portfolio_id": port.portfolio_id,
                    "strategy_id": ctx.strategy_id,
                    "stock_code": code,
                    "quantity": pos.quantity,
                    "avg_cost": float(pos.avg_cost),
                    "market_value": float(pos.market_value),
                })
    return sorted(rows, key=lambda r: (r["stock_code"], r["portfolio_id"], r["strategy_id"]))


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
    """虚拟持仓。运行中读引擎内存态（含未落库当日变动）；停止后从 live_trades 重放聚合。

    session 不存在返回 None。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    if engine is not None:
        return engine_virtual_positions(engine)
    trades = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time, LiveTrade.id)
        .all()
    )
    return aggregate_positions_from_trades(trades)


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
        "bar_time": d.bar_time.isoformat() if d.bar_time else None,
        "param_name": d.param_name,
        "param_value": d.param_value,
        "actual_value": d.actual_value,
        "requested_qty": d.requested_qty,
        "final_qty": d.final_qty,
        "message": d.message,
        "created_at": d.created_at.isoformat() if d.created_at else None,
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
        "bar_time": s.bar_time.isoformat() if s.bar_time else None,
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
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "alerted_at": s.alerted_at.isoformat() if s.alerted_at else None,
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
