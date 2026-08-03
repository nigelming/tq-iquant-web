from datetime import datetime, date
from decimal import Decimal
from typing import Dict, Optional

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import (
    PortfolioStrategy, Strategy, Formula, FormulaSignal, StockPoolStock,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
)
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.backtest_engine import BacktestEngine
from tq_iquant_shared.constants import SignalType
from core.tq.data import TQData
from core.tq.formula import TQFormula

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date


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


def build_klines(ps: PortfolioStrategy, start: date, end: date, db: Session = None) -> dict:
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


def build_open_prices(ps: PortfolioStrategy, klines: dict) -> dict:
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


# --- 公式信号 ---
# TQ 公式输出中需跳过的非变量键
_FORMULA_META_KEYS = ("Date", "ErrorId", "Error", "Time")


def _convert_formula_output(
    raw: dict, strategy_id: int, stocks: list
) -> dict:
    """TQ 公式输出（formula_process_mul_zb）→ signal_cache 条目。

    raw: {stock_code: {var_name: [{"Date":"YYYYMMDD","Value":float}, ...]}}，
         顶层可能有 ErrorId。日期串需转 datetime 对齐 klines 时间轴。
    返回: {(strategy_id, stock_code, bar_time): [{"name": str, "value": int}]}。
    ErrorId 非 0/19 → 视为出错，返回空。
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
        # 按 bar_time 聚合该股票所有变量
        by_time: dict = {}
        for var_name, val_list in stock_data.items():
            if var_name in _FORMULA_META_KEYS:
                continue
            if not isinstance(val_list, list):
                continue
            for entry in val_list:
                if not isinstance(entry, dict):
                    continue
                d = entry.get("Date")
                v = entry.get("Value")
                if not d or v is None:
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


def build_signal_cache(ps: PortfolioStrategy, klines: dict, db: Session = None) -> dict:
    """预计算公式信号：{(strategy_id, stock_code, bar_time): [{name, value}]}。

    对每个策略：读 Formula（公式名）+ FormulaSignal（信号配置），
    调 TQFormula.compute 跑公式，转 signal_cache。cache-first，TQ 兜底。
    """
    if db is None:
        return {}
    stocks = list(klines.keys())
    if not stocks:
        return {}
    strategies = _portfolio_strategies(ps, db)
    tq_formula = TQFormula()
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
        entries = _convert_formula_output(raw, strat.id, stocks)
        cache.update(entries)
    return cache


# --- 辅助：从 DB 读组装所需数据 ---
def _pool_stocks(ps: PortfolioStrategy, db: Session) -> list:
    """股票池股票代码列表。db 为 None 时返回空。"""
    if db is None:
        return []
    rows = db.query(StockPoolStock).filter_by(pool_id=ps.stock_pool_id).all()
    return [r.stock_code for r in rows]


def _strategy_periods(ps: PortfolioStrategy, db: Session) -> list:
    """所有策略的 period 去重（保持顺序）。"""
    if db is None:
        return ["1d"]
    strats = _portfolio_strategies(ps, db)
    seen: list = []
    for s in strats:
        if s.period not in seen:
            seen.append(s.period)
    return seen or ["1d"]


def _portfolio_strategies(ps: PortfolioStrategy, db: Session) -> list:
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
# 组装 Portfolio
# ---------------------------------------------------------------------------
def _signal_type_from_str(s: str) -> SignalType:
    for st in SignalType:
        if st.value == s:
            return st
    raise ValueError(f"unknown signal_type: {s}")


def _assemble_portfolio(ps: PortfolioStrategy, strategies: list, db: Session) -> Portfolio:
    pm = PortfolioRiskManager(
        max_drawdown=Decimal(str(ps.max_drawdown)),
        daily_loss_limit=Decimal(str(ps.daily_loss_limit)),
    )
    port = Portfolio(
        portfolio_id=ps.id,
        initial_capital=Decimal(str(ps.initial_capital)),
        risk_manager=pm,
        cost_params={
            "min_commission": Decimal(str(ps.min_commission)),
            "buy_commission_rate": Decimal(str(ps.buy_commission_rate)),
            "sell_commission_rate": Decimal(str(ps.sell_commission_rate)),
            "stamp_duty_rate": Decimal(str(ps.stamp_duty_rate)),
            "slippage": Decimal(str(ps.slippage)),
        },
    )
    for strat in strategies:
        ctx = StrategyContext(
            strategy_id=strat.id,
            period=strat.period,
            capital_ratio=Decimal(str(strat.capital_ratio)),
            max_positions=strat.max_positions,
            single_open_ratio=Decimal(str(strat.single_open_ratio)),
            add_position_threshold=Decimal(str(strat.add_position_threshold)),
            max_add_count=strat.max_add_count,
            add_position_ratio=Decimal(str(strat.add_position_ratio)),
            reduce_position_ratio=Decimal(str(strat.reduce_position_ratio)),
            role=strat.role,
            master_strategy_id=strat.master_strategy_id,
        )
        # 从 formula_signals 表读信号配置
        sigs = db.query(FormulaSignal).filter_by(formula_id=strat.formula_id).all()
        ctx.formula_signals = [
            {
                "signal_name": s.signal_name,
                "signal_type": _signal_type_from_str(s.signal_type),
                "trigger_value": s.trigger_value,
            }
            for s in sigs
        ]
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal(str(strat.stop_loss_ratio)),
            take_profit_ratio=Decimal(str(strat.take_profit_ratio)),
            trailing_stop_ratio=Decimal(str(strat.trailing_stop_ratio)),
        )
        port.strategies.append(ctx)
    return port


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
            signal_name="",
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
# 路由
# ---------------------------------------------------------------------------
@router.get("/records")
def list_records(db: Session = Depends(get_db)):
    recs = db.query(BacktestRecord).order_by(BacktestRecord.created_at.desc()).all()
    return {"code": 0, "data": [_serialize_record(r) for r in recs]}


@router.get("/records/{record_id}")
def get_record(record_id: int, db: Session = Depends(get_db)):
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return {"code": 404, "message": "回测记录不存在"}
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id)
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
        .filter_by(backtest_record_id=record_id)
        .first()
    )
    return {
        "code": 0,
        "data": {
            "record": _serialize_record(rec),
            "snapshots": [_serialize_snapshot(s) for s in snaps],
            "trades": [_serialize_trade(t) for t in trades],
            "evaluations": _serialize_evaluation(evals) if evals else None,
        },
    }


@router.delete("/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    """删除回测记录 + 级联子表（trades/snapshots/evaluations）。
    子表 FK 虽配 ondelete=CASCADE，但显式删更稳妥（不依赖连接级 PRAGMA）。"""
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return {"code": 404, "message": "回测记录不存在"}
    db.query(BacktestTrade).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestEvaluation).filter_by(backtest_record_id=record_id).delete()
    db.delete(rec)
    db.commit()
    return {"code": 0, "data": None}


def _validate_backtest_request(req: BacktestRequest) -> Optional[str]:
    """回测请求基础校验，返回错误消息或 None（通过）。
    校验日期区间：start < end，且 start 不在未来（TQ 拉不到未来行情）。"""
    if req.start_date >= req.end_date:
        return f"开始日期必须早于结束日期，收到 {req.start_date} ~ {req.end_date}"
    today = date.today()
    if req.start_date > today:
        return f"开始日期不可在未来，收到 {req.start_date}（今天 {today}）"
    return None


@router.post("")
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        raise HTTPException(status_code=404, detail="portfolio strategy not found")

    err = _validate_backtest_request(req)
    if err:
        return {"code": 400, "message": err}

    strategies = (
        db.query(Strategy)
        .filter_by(portfolio_id=ps.id)
        .all()
    )

    # 写 record（running）
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        status="running",
        progress=0,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    record_id = rec.id

    try:
        portfolio = _assemble_portfolio(ps, strategies, db)
        klines = build_klines(ps, req.start_date, req.end_date, db)
        signal_cache = build_signal_cache(ps, klines, db)
        open_prices = build_open_prices(ps, klines)

        # 空行情保护：TQ 在该区间/股票池拉不到任何 K 线 → 标 failed 而非静默 completed。
        # 这正是"启动了但没运行直接完成"的根因（日期填反/未来日/股票池空等）。
        if not klines:
            rec.status = "failed"
            rec.error_message = (
                f"未取到任何行情数据：区间 {req.start_date}~{req.end_date}，"
                f"股票池 id={ps.stock_pool_id}。请检查日期区间与股票池成分。"
            )
            db.commit()
            return {
                "code": 0,
                "message": "ok",
                "data": {"record_id": record_id, "trades_count": 0,
                         "snapshots_count": 0, "evaluations": {}},
            }

        def on_progress(p: int):
            rec.progress = p
            db.commit()

        engine = BacktestEngine()
        result = engine.run(
            portfolio,
            klines=klines,
            signal_cache=signal_cache,
            open_prices=open_prices,
            progress_callback=on_progress,
        )

        _persist_result(db, record_id, ps.id, result, strategies)

        rec.status = "completed"
        rec.progress = 100
        rec.completed_at = datetime.now()
        db.commit()
    except Exception as e:
        # 异常时把 record 标 failed 并落库。session 可能因异常处于脏状态，
        # 先 rollback 再改字段重提交，确保状态写进去（否则前端会看到永久 running）。
        db.rollback()
        rec = db.get(BacktestRecord, record_id)
        if rec is not None:
            rec.status = "failed"
            rec.error_message = str(e) or repr(e)
            db.commit()
        raise

    return {
        "code": 0,
        "message": "ok",
        "data": {
            "record_id": record_id,
            "trades_count": len(result["trades"]),
            "snapshots_count": len(result["snapshots"]),
            "evaluations": result.get("evaluations") or {},
        },
    }
