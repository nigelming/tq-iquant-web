"""回测 service 层 — 业务逻辑（从 core.api.backtest 提取）。

P1 #9（审计）第一块：把回测路由的业务逻辑下沉到 service，路由层仅留 HTTP 壳
（参数校验 + 并发锁 + 调 service + 响应包装）。本模块承接：

- 数据获取层：TQ 行情/公式/基准对接（build_klines / build_signal_cache / ...）
- 持久化：回测结果写库（_persist_result）
- 序列化：record/snapshot/trade/evaluation → dict
- 查询：list / get detail / delete
- 主链路：run_backtest（assemble → 数据准备 → engine.run → 持久化）

兼容：core.api.backtest 顶部 re-export 本模块被测试引用的符号（bt_api.xxx），
故测试零改动。纯重构，行为不变。
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional

import polars as pl
from sqlalchemy.orm import Session

from core.models import (
    StockPoolStock, Strategy, Formula,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
)
from core.tq.data import TQData
from core.tq.formula import TQFormula


# ---------------------------------------------------------------------------
# 数据获取层 — TQ 对接
# 拆为「TQ 调用（依赖通达信进程，不可单测）」+「纯转换（可单测）」两层。
# ---------------------------------------------------------------------------
# TQ 行情字段（首字母大写）→ 引擎 polars 列
_OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")


def _convert_market_data(
    raw: dict, stocks: list, periods: list
) -> dict:
    """单周期 TQ 原始行情 → 引擎 klines（单周期入口）。

    raw: {field: pandas.DataFrame}，field ∈ Open/High/Low/Close/Volume/Amount，
         DataFrame.index=时间戳，columns=股票代码。
    返回: {stock_code: {period: pl.DataFrame}}（列 datetime + 6 行情列，Decimal 价）。
    缺失股票静默跳过。
    """
    if not isinstance(raw, dict):
        return {}
    # 以 Close 的 index 为时间轴基准
    close_df = raw.get("Close")
    if close_df is None:
        return {}
    timestamps = list(close_df.index)
    result: dict = {}
    for code in stocks:
        per_period: dict = {}
        for period in periods:
            df = _build_polars_kline(raw, code, timestamps)
            if df is not None:
                per_period[period] = df
        if per_period:
            result[code] = per_period
    return result


def _build_polars_kline(raw: dict, code: str, timestamps: list):
    """从 TQ raw 抽取单股票 polars DataFrame；该股票无数据返回 None。

    TQ 真机返回的 Volume/Amount 可能是 str（如 "1000"），polars 构造时若混入
    str 会 panic。故数值列统一规整：价/金额→Decimal，成交量→int。
    """
    col_data: dict = {"datetime": []}
    has_any = False
    for field in _OHLCV_FIELDS:
        col_data[field] = []
    for ts in timestamps:
        row_ok = True
        for field in _OHLCV_FIELDS:
            df = raw.get(field)
            if df is None or code not in getattr(df, "columns", []):
                row_ok = False
                break
        if not row_ok:
            continue
        has_any = True
        col_data["datetime"].append(ts)
        for field in _OHLCV_FIELDS:
            val = raw[field].loc[ts, code]
            if field == "Volume":
                # 成交量统一规整为 int（TQ 可能返回 str）
                col_data[field].append(_to_int(val))
            else:
                # 价/金额列转 Decimal
                col_data[field].append(_to_decimal(val))
    if not has_any:
        return None
    return pl.DataFrame(col_data)


def _is_nan(val) -> bool:
    """识别 NaN/None/空（TQ 停牌/无交易日返回 NaN float 或 None）。"""
    if val is None:
        return True
    if isinstance(val, float) and val != val:  # NaN 唯一不等于自身的特性
        return True
    return False


def _to_decimal(val) -> Decimal:
    """数值转 Decimal，容忍 float/int/str/Decimal；NaN/None → Decimal("0")。
    NaN 会毒化下游金额计算（Decimal('nan') 参与运算全 NaN），故规整为 0。"""
    if _is_nan(val):
        return Decimal("0")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    if isinstance(val, str):
        return Decimal(val)
    return Decimal(str(val))


def _to_int(val) -> int:
    """数值转 int（Volume/公式 trigger_value）；NaN/None/无法解析 → 0。
    注意：int(NaN) 直接抛 ValueError，故先 _is_nan 拦截。"""
    if _is_nan(val):
        return 0
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(val)
    try:
        return int(Decimal(str(val)))
    except (ValueError, ArithmeticError):
        return 0


def _convert_market_data_multi(raw_by_period: dict, stocks: list) -> dict:
    """多周期 TQ 原始行情 → 引擎 klines。

    raw_by_period: {period: {field: pandas.DataFrame}}（每个周期一次 TQ 调用）。
    返回: {stock_code: {period: pl.DataFrame}}。
    """
    result: dict = {}
    for period, raw in raw_by_period.items():
        single = _convert_market_data(raw, stocks, [period])
        for code, per_period in single.items():
            if code not in result:
                result[code] = {}
            result[code].update(per_period)
    return result


def build_klines(ps, start: date, end: date, db: Session = None) -> dict:
    """从 TQ 取历史 K 线：{stock_code: {period: pl.DataFrame}}。

    股票来自 ps.stock_pool_id 对应的 stock_pool_stocks；周期取所有策略的 period 去重。
    需要 db 查股票池；若未传 db，返回空（无法定位股票）。
    流程：get_history_raw（原始 TQ 行情）→ _convert_market_data_multi（转引擎 polars）。
    """
    stocks = _pool_stocks(ps, db)
    if not stocks:
        return {}
    periods = _strategy_periods(ps, db)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    tq = TQData()
    raw_by_period = tq.get_history_raw(
        stocks=stocks, periods=periods,
        start=start_str, end=end_str, dividend_type="front", count=-1,
    )
    return _convert_market_data_multi(raw_by_period, stocks)


def build_open_prices(ps, klines: dict) -> dict:
    """从 klines 提取 open 价表：{stock_code: {bar_time: Decimal}}。

    供 SimulatedDispatcher 在下一 bar open 成交。取每只股票首个周期的 Open 列。
    """
    prices: dict = {}
    for code, periods in klines.items():
        if not periods:
            continue
        # 取任一周期（日线回测只有一个周期）
        df = next(iter(periods.values()))
        if "datetime" not in df.columns or "Open" not in df.columns:
            continue
        stock_prices: dict = {}
        times = df["datetime"].to_list()
        opens = df["Open"].to_list()
        for t, o in zip(times, opens):
            stock_prices[t] = o if isinstance(o, Decimal) else _to_decimal(o)
        prices[code] = stock_prices
    return prices


def build_benchmark_data(
    ps, start: date, end: date, db: Session = None
) -> Dict[date, Decimal]:
    """拉基准指数收盘价：{snap_date: close_value}。

    基准代码取 ps.benchmark_index（默认 000300.SH）。用 TQ 拉指数日线 Close，
    按日期（date）建索引，供 BacktestEngine 逐 bar 填入快照 benchmark_value。
    拉取失败/无数据返回空 dict（前端据此隐藏基准线，Evaluator 退化为 0）。
    """
    index_code = getattr(ps, "benchmark_index", None) or "000300.SH"
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    tq = TQData()
    raw = tq.get_history_raw(
        stocks=[index_code], periods=["1d"],
        start=start_str, end=end_str, dividend_type="front", count=-1,
    )
    if not isinstance(raw, dict) or not raw:
        return {}
    raw_1d = raw.get("1d")
    if not isinstance(raw_1d, dict):
        return {}
    close_df = raw_1d.get("Close")
    if close_df is None or index_code not in getattr(close_df, "columns", []):
        return {}
    result: Dict[date, Decimal] = {}
    for ts in close_df.index:
        val = close_df.loc[ts, index_code]
        if _is_nan(val):
            continue
        # ts 可能是 Timestamp/datetime/date，统一取 .date()
        d = ts.date() if hasattr(ts, "date") else ts
        if isinstance(d, datetime):
            d = d.date()
        result[d] = _to_decimal(val)
    return result



# --- 公式信号 ---
# TQ 公式输出中需跳过的非变量键
_FORMULA_META_KEYS = ("Date", "ErrorId", "Error", "Time")


def _convert_formula_output(
    raw: dict, strategy_id: int, stocks: list,
    bar_times_by_code: Optional[Dict[str, List[datetime]]] = None,
) -> dict:
    """TQ 公式输出（formula_process_mul_zb）→ signal_cache 条目。

    raw: {stock_code: {var_name: [{"Date":"YYYYMMDD","Value":float}, ...]}}，
         顶层可能有 ErrorId。
    返回: {(strategy_id, stock_code, bar_time): [{"name": str, "value": int}]}。
    ErrorId 非 0/19 → 视为出错，返回空。

    时间轴对齐（两种模式）：
    - 日线（bar_times_by_code=None）：公式输出 Date=YYYYMMDD 转午夜 datetime 作 key。
      日线 bar 也是日粒度，1:1 对齐。
    - 分钟级（bar_times_by_code 传入）：TQ 输出 Date 只标到日（丢时分），但输出条目
      按 bar 顺序排列。按索引对齐：第 i 条输出 → bar_times_by_code[code][i]。
      输出条数 < bar 数 → 多余 bar 无信号；> bar 数 → 多余输出丢弃。
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    err = raw.get("ErrorId")
    if err is not None and str(err) not in ("0", "19"):
        return {}
    entries: dict = {}
    for code in stocks:
        stock_data = raw.get(code)
        if not isinstance(stock_data, dict):
            continue
        bar_times = bar_times_by_code.get(code) if bar_times_by_code else None
        # 按 bar_time 聚合该股票所有变量
        by_time: dict = {}
        for var_name, val_list in stock_data.items():
            if var_name in _FORMULA_META_KEYS:
                continue
            if not isinstance(val_list, list):
                continue
            for i, entry in enumerate(val_list):
                if not isinstance(entry, dict):
                    continue
                v = entry.get("Value")
                if v is None:
                    continue
                if bar_times is not None:
                    # 分钟级：按索引对齐 bar_times；越界（i >= len）丢弃
                    if i >= len(bar_times):
                        break
                    bar_time = bar_times[i]
                else:
                    # 日线：按 Date 转午夜 datetime
                    d = entry.get("Date")
                    if not d:
                        continue
                    bar_time = _parse_date_str(d)
                    if bar_time is None:
                        continue
                key = (strategy_id, code, bar_time)
                by_time.setdefault(key, []).append({"name": var_name, "value": _to_int(v)})
        entries.update(by_time)
    return entries


def _parse_date_str(d: str):
    """YYYYMMDD 或 YYYY-MM-DD → datetime。无法解析返回 None。"""
    s = str(d).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# 索引对齐周期：TQ 公式输出 Date 只标到日（丢时分），但 bar 时间带时分，
# 无法按 Date 1:1 匹配，须按 bar_times 索引对齐（第 i 条输出 → bar_times[i]）。
# 含 1h（小时级 bar 时间带时分，与分钟级同需索引对齐）；1d bar 是日粒度走 Date 匹配。
# 取值与 VALID_PERIODS 的「带时分 bar」子集一致（open-questions Q4）。
_MINUTE_PERIODS = {"1m", "5m", "15m", "30m", "1h"}


def _bar_times_by_code(klines: dict) -> Dict[str, List[datetime]]:
    """从 klines 提取每只股票的时间轴（升序去重），供分钟级公式输出按索引对齐。"""
    result: Dict[str, List[datetime]] = {}
    for code, periods in klines.items():
        times: List[datetime] = []
        for df in periods.values():
            if "datetime" in df.columns:
                times.extend(df["datetime"].to_list())
        if times:
            # 去重保序（多周期合并时可能重复）
            seen = set()
            uniq = []
            for t in sorted(times):
                if t not in seen:
                    seen.add(t)
                    uniq.append(t)
            result[code] = uniq
    return result


def build_signal_cache(ps, klines: dict, db: Session = None) -> dict:
    """预计算公式信号：{(strategy_id, stock_code, bar_time): [{name, value}]}。

    对每个策略：读 Formula（公式名）+ FormulaSignal（信号配置），
    调 TQFormula.compute 跑公式，转 signal_cache。cache-first，TQ 兜底。

    分钟级（5m/15m/30m/60m）：TQ 公式输出 Date 只标到日（丢时分），按输出条目顺序
    对齐 klines 时间轴（_convert_formula_output 的 bar_times_by_code）。
    日线：按 Date 匹配（1:1 对齐）。
    """
    if db is None:
        return {}
    stocks = list(klines.keys())
    if not stocks:
        return {}
    strategies = _portfolio_strategies(ps, db)
    tq_formula = TQFormula()
    # 分钟级时间轴（所有策略共用同一 klines 时间轴）
    bar_times_by_code = _bar_times_by_code(klines)
    cache: dict = {}
    for strat in strategies:
        formula = db.get(Formula, strat.formula_id)
        if formula is None:
            continue
        period = strat.period
        start_str, end_str = _kline_time_range(klines)
        raw = tq_formula.compute(
            formula_name=formula.name, formula_arg="",
            stocks=stocks, period=period,
            count=-1, dividend_type=1,
            start_time=start_str, end_time=end_str,
        )
        # 分钟级传 bar_times 按索引对齐；日线不传走 Date 匹配
        bt = bar_times_by_code if period in _MINUTE_PERIODS else None
        entries = _convert_formula_output(raw, strat.id, stocks, bar_times_by_code=bt)
        cache.update(entries)
    return cache


# --- 辅助：从 DB 读组装所需数据 ---
def _pool_stocks(ps, db: Session) -> list:
    """股票池股票代码列表。db 为 None 时返回空。"""
    if db is None:
        return []
    rows = db.query(StockPoolStock).filter_by(pool_id=ps.stock_pool_id).all()
    return [r.stock_code for r in rows]


def _strategy_periods(ps, db: Session) -> list:
    """所有策略的 period 去重（保持顺序）。"""
    if db is None:
        return ["1d"]
    strats = _portfolio_strategies(ps, db)
    seen: list = []
    for s in strats:
        if s.period not in seen:
            seen.append(s.period)
    return seen or ["1d"]


def _portfolio_strategies(ps, db: Session) -> list:
    if db is None:
        return []
    return db.query(Strategy).filter_by(portfolio_id=ps.id).all()


def _kline_time_range(klines: dict):
    """从 klines 取最早/最晚时间，返回 (start_str, end_str) YYYYMMDD。"""
    times = []
    for periods in klines.values():
        for df in periods.values():
            if "datetime" in df.columns:
                times.extend(df["datetime"].to_list())
    if not times:
        return "", ""
    t_min, t_max = min(times), max(times)
    return t_min.strftime("%Y%m%d"), t_max.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 持久化引擎产出
# ---------------------------------------------------------------------------
def _persist_result(
    db: Session, record_id: int, ps_id: int, result: dict, strategies: list
) -> None:
    strat_by_id = {s.id: s for s in strategies}
    for trade in result["trades"]:
        db.add(BacktestTrade(
            backtest_record_id=record_id,
            strategy_id=trade.strategy_id,
            formula_signal_id=None,
            signal_name=trade.signal_name or "",
            signal_type=trade.signal_type.value,
            stock_code=trade.stock_code,
            trade_type=trade.trade_type.value,
            price=trade.price,
            quantity=trade.quantity,
            amount=trade.amount,
            commission=trade.commission,
            stamp_duty=trade.stamp_duty,
            bar_time=trade.trade_time,
        ))
    for snap in result["snapshots"]:
        db.add(BacktestDailySnapshot(
            backtest_record_id=record_id,
            target_type="portfolio",
            target_id=ps_id,
            snap_date=snap["snap_date"],
            total_value=snap["total_value"],
            cash=snap["cash"],
            market_value=snap.get("market_value", Decimal("0")),
            daily_return=snap.get("daily_return"),
            cumulative_return=snap.get("cumulative_return"),
            benchmark_value=snap.get("benchmark_value"),
        ))
    ev = result.get("evaluations") or {}
    db.add(BacktestEvaluation(
        backtest_record_id=record_id,
        target_type="portfolio",
        target_id=ps_id,
        total_return=ev.get("total_return"),
        annual_return=ev.get("annual_return"),
        max_drawdown=ev.get("max_drawdown"),
        volatility=ev.get("volatility"),
        sharpe_ratio=ev.get("sharpe_ratio"),
        sortino_ratio=ev.get("sortino_ratio"),
        calmar_ratio=ev.get("calmar_ratio"),
        win_rate=ev.get("win_rate"),
        profit_factor=ev.get("profit_factor"),
        total_trades=ev.get("total_trades"),
        benchmark_return=ev.get("benchmark_return"),
        avg_holding_days=ev.get("avg_holding_days"),
        var_95=ev.get("var_95"),
        cvar_95=ev.get("cvar_95"),
        avg_recovery_days=ev.get("avg_recovery_days"),
        max_recovery_days=ev.get("max_recovery_days"),
        ulcer_index=ev.get("ulcer_index"),
        return_stability=ev.get("return_stability"),
    ))

    # ---- 策略层快照 + 评估 ----
    strategy_snaps = result.get("strategy_snapshots") or {}
    strategy_evals = result.get("strategy_evaluations") or {}
    for sid, snaps in strategy_snaps.items():
        for snap in snaps:
            db.add(BacktestDailySnapshot(
                backtest_record_id=record_id,
                target_type="strategy",
                target_id=sid,
                snap_date=snap["snap_date"],
                total_value=snap["total_value"],
                cash=snap.get("cash", Decimal("0")),
                market_value=snap.get("market_value", Decimal("0")),
            ))
    for sid, sev in strategy_evals.items():
        db.add(BacktestEvaluation(
            backtest_record_id=record_id,
            target_type="strategy",
            target_id=sid,
            total_return=sev.get("total_return"),
            annual_return=sev.get("annual_return"),
            max_drawdown=sev.get("max_drawdown"),
            volatility=sev.get("volatility"),
            sharpe_ratio=sev.get("sharpe_ratio"),
            sortino_ratio=sev.get("sortino_ratio"),
            calmar_ratio=sev.get("calmar_ratio"),
            win_rate=sev.get("win_rate"),
            profit_factor=sev.get("profit_factor"),
            total_trades=sev.get("total_trades"),
            benchmark_return=sev.get("benchmark_return"),
            avg_holding_days=sev.get("avg_holding_days"),
            var_95=sev.get("var_95"),
            cvar_95=sev.get("cvar_95"),
            avg_recovery_days=sev.get("avg_recovery_days"),
            max_recovery_days=sev.get("max_recovery_days"),
            ulcer_index=sev.get("ulcer_index"),
            return_stability=sev.get("return_stability"),
        ))


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _serialize_record(r: BacktestRecord) -> dict:
    return {
        "id": r.id,
        "portfolio_strategy_id": r.portfolio_strategy_id,
        "name": r.name,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "status": r.status,
        "progress": r.progress,
        "error_message": r.error_message,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


def _f(v) -> float | None:
    """Decimal → float，None 透传（沿用 _serialize_strategy 模式）。"""
    return float(v) if v is not None else None


def _serialize_snapshot(s: BacktestDailySnapshot) -> dict:
    return {
        "snap_date": s.snap_date.isoformat() if s.snap_date else None,
        "total_value": _f(s.total_value),
        "cash": _f(s.cash),
        "market_value": _f(s.market_value),
        "daily_return": _f(s.daily_return),
        "cumulative_return": _f(s.cumulative_return),
        "benchmark_value": _f(s.benchmark_value),
    }


def _serialize_trade(t: BacktestTrade) -> dict:
    return {
        "id": t.id,
        "strategy_id": t.strategy_id,
        "signal_name": t.signal_name,
        "signal_type": t.signal_type,
        "stock_code": t.stock_code,
        "trade_type": t.trade_type,
        "price": _f(t.price),
        "quantity": t.quantity,
        "amount": _f(t.amount),
        "commission": _f(t.commission),
        "stamp_duty": _f(t.stamp_duty),
        "bar_time": t.bar_time.isoformat() if t.bar_time else None,
    }


def _serialize_trade_with_name(t: BacktestTrade, name_map: dict) -> dict:
    """带策略名的交易序列化（详情页策略列展示）。"""
    d = _serialize_trade(t)
    d["strategy_name"] = name_map.get(t.strategy_id, f"策略{t.strategy_id}")
    return d


def _serialize_evaluation(e: BacktestEvaluation) -> dict:
    return {
        "total_return": _f(e.total_return),
        "annual_return": _f(e.annual_return),
        "max_drawdown": _f(e.max_drawdown),
        "volatility": _f(e.volatility),
        "sharpe_ratio": _f(e.sharpe_ratio),
        "sortino_ratio": _f(e.sortino_ratio),
        "calmar_ratio": _f(e.calmar_ratio),
        "win_rate": _f(e.win_rate),
        "profit_factor": _f(e.profit_factor),
        "total_trades": e.total_trades,
        "benchmark_return": _f(e.benchmark_return),
        "avg_holding_days": _f(e.avg_holding_days),
        "var_95": _f(e.var_95),
        "cvar_95": _f(e.cvar_95),
        "avg_recovery_days": _f(e.avg_recovery_days),
        "max_recovery_days": e.max_recovery_days,
        "ulcer_index": _f(e.ulcer_index),
        "return_stability": _f(e.return_stability),
    }


# ---------------------------------------------------------------------------
# 查询（路由层调，返回纯 dict；不存在返回 None / False 由路由转 err(404)）
# ---------------------------------------------------------------------------
def list_records(db: Session) -> list:
    """列出全部回测记录（按创建时间倒序），序列化为 dict 列表。"""
    recs = db.query(BacktestRecord).order_by(BacktestRecord.created_at.desc()).all()
    return [_serialize_record(r) for r in recs]


def get_record_detail(db: Session, record_id: int) -> Optional[dict]:
    """回测记录详情：record + snapshots + trades + evaluations + 策略层。
    不存在返回 None（路由层据此 err(404)）。"""
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return None
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    trades = (
        db.query(BacktestTrade)
        .filter_by(backtest_record_id=record_id)
        .order_by(BacktestTrade.bar_time)
        .all()
    )
    evals = (
        db.query(BacktestEvaluation)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .first()
    )

    # ---- 策略层 ----
    strategy_evals_rows = (
        db.query(BacktestEvaluation)
        .filter_by(backtest_record_id=record_id, target_type="strategy")
        .all()
    )
    strategy_snaps_rows = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="strategy")
        .order_by(BacktestDailySnapshot.target_id, BacktestDailySnapshot.snap_date)
        .all()
    )
    # strategy_id → name 映射（trades/snapshots 都带 strategy_id，前端展示需名字）
    strat_ids = {t.strategy_id for t in trades} | {r.target_id for r in strategy_evals_rows}
    strat_name_map = {
        s.id: s.name for s in db.query(Strategy).filter(Strategy.id.in_(strat_ids)).all()
    } if strat_ids else {}

    # 按策略聚合快照曲线
    curves_by_sid: dict = {}
    for s in strategy_snaps_rows:
        curves_by_sid.setdefault(s.target_id, []).append({
            "snap_date": s.snap_date.isoformat() if s.snap_date else None,
            "total_value": _f(s.total_value),
        })

    strategy_evaluations = [
        {"strategy_id": r.target_id, "strategy_name": strat_name_map.get(r.target_id, f"策略{r.target_id}"),
         **_serialize_evaluation(r)}
        for r in strategy_evals_rows
    ]
    strategy_snapshots = [
        {"strategy_id": sid, "strategy_name": strat_name_map.get(sid, f"策略{sid}"), "curve": curve}
        for sid, curve in curves_by_sid.items()
    ]

    return {
        "record": _serialize_record(rec),
        "snapshots": [_serialize_snapshot(s) for s in snaps],
        "trades": [_serialize_trade_with_name(t, strat_name_map) for t in trades],
        "evaluations": _serialize_evaluation(evals) if evals else None,
        "strategy_evaluations": strategy_evaluations,
        "strategy_snapshots": strategy_snapshots,
    }


def delete_record(db: Session, record_id: int) -> bool:
    """删除回测记录 + 级联子表（trades/snapshots/evaluations）。
    子表 FK 虽配 ondelete=CASCADE，但显式删更稳妥（不依赖连接级 PRAGMA）。
    不存在返回 False（路由层据此 err(404)）。"""
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return False
    db.query(BacktestTrade).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestEvaluation).filter_by(backtest_record_id=record_id).delete()
    db.delete(rec)
    db.commit()
    return True
