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

import json
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd
import polars as pl
from sqlalchemy.orm import Session

from core.models import (
    StockPoolStock, Strategy, Formula, PortfolioStrategy,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
    BacktestDecisionEvent, BacktestCrisisAlpha,
)
from core.engine.backtest_engine import BacktestEngine
from core.engine.evaluator import Evaluator
from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, DEFAULT_BENCHMARK_CODE, build_crisis_alpha_payload,
)
from core.engine.decision import summarize_decisions
from core.engine.portfolio_builder import (
    assemble_portfolio as _assemble_portfolio,
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


def warmup_counts_by_period(ps, db: Session = None) -> Dict[str, int]:
    """各周期公式预热根数：{period: max(formula_count)}。

    取该周期所有策略引用公式的 formula_count 最大值（与实盘 code_period_count
    同语义：凑满 formula_count 根数据，公式输出有效值）。formula_count<=0 或
    公式缺失的策略跳过。返回空 dict 表示无需预热。

    回测主区间首根 bar 前需 formula_count-1 根历史即可让公式生效
    （有效值条件含当前 bar 本身），按 formula_count 拉取多 1 根富余。
    """
    if db is None:
        return {}
    counts: Dict[str, int] = {}
    for strat in _portfolio_strategies(ps, db):
        if strat.formula_id is None:
            continue
        formula = db.get(Formula, strat.formula_id)
        if formula is None:
            continue
        cnt = formula.formula_count or 0
        if cnt <= 0:
            continue
        prev = counts.get(strat.period, 0)
        counts[strat.period] = max(prev, cnt)
    return counts


def _merge_raw_by_period(main_raw: dict, warm_raw: dict) -> dict:
    """预热段 + 主段原始行情合并：{period: {field: pandas.DataFrame}}。

    逐周期逐字段 pandas.concat → index 去重（保留主段值）→ 按时间升序。
    预热段缺周期/缺字段/非 dict 时退化为仅主段。
    """
    if not isinstance(main_raw, dict):
        return main_raw
    if not isinstance(warm_raw, dict):
        return main_raw
    merged: dict = {}
    for period, fields in main_raw.items():
        warm_fields = warm_raw.get(period)
        if not isinstance(fields, dict) or not isinstance(warm_fields, dict):
            merged[period] = fields
            continue
        mf: dict = {}
        for field, df in fields.items():
            wdf = warm_fields.get(field)
            if not isinstance(df, pd.DataFrame) or not isinstance(wdf, pd.DataFrame):
                mf[field] = df
                continue
            comb = pd.concat([wdf, df])
            # 去重保留后出现的（= 主段值），再按时间升序
            comb = comb[~comb.index.duplicated(keep="last")]
            mf[field] = comb.sort_index()
        merged[period] = mf
    return merged


def _slice_klines_from(klines: dict, start: date) -> dict:
    """引擎视野切片：只保留 datetime >= start 的 bar（预热段不进引擎时间轴）。

    预热 bar 只喂公式（build_signal_cache 用全量 klines），引擎的
    时间轴/成交/快照/熔断/评估全部只看 [start, end]。
    """
    start_ts = datetime(start.year, start.month, start.day)
    result: dict = {}
    for code, periods in klines.items():
        per: dict = {}
        for period, df in periods.items():
            if "datetime" not in df.columns:
                continue
            sliced = df.filter(pl.col("datetime") >= start_ts)
            if sliced.height > 0:
                per[period] = sliced
        if per:
            result[code] = per
    return result


def build_klines(ps, start: date, end: date, db: Session = None) -> dict:
    """从 TQ 取历史 K 线：{stock_code: {period: pl.DataFrame}}（含公式预热段）。

    股票来自 ps.stock_pool_id 对应的 stock_pool_stocks；周期取所有策略的 period 去重。
    需要 db 查股票池；若未传 db，返回空（无法定位股票）。
    流程：主段 get_history_raw（start~end，count=-1）→ 每周期预热段 count 直拉
    （start 空 + end=start 前一天 + count=formula_count，TDX 消化交易日/停牌差异）→
    _merge_raw_by_period 合并 → _convert_market_data_multi（转引擎 polars）。

    返回值含预热 bar（早于 start）；引擎视野由调用方 _slice_klines_from 切回主区间，
    预热段只喂 build_signal_cache。
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
    warmup_counts = warmup_counts_by_period(ps, db)
    if warmup_counts:
        # 预热段：每周期一次 count 直拉，end=start 前一天（重叠 bar 合并时去重）
        warm_end = (start - timedelta(days=1)).strftime("%Y%m%d")
        warm_raw: dict = {}
        for period, cnt in warmup_counts.items():
            if period not in periods:
                continue
            part = tq.get_history_raw(
                stocks=stocks, periods=[period],
                start="", end=warm_end, dividend_type="front", count=cnt,
            )
            if isinstance(part, dict):
                warm_raw.update(part)
        raw_by_period = _merge_raw_by_period(raw_by_period, warm_raw)
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


def build_index_close_series(
    codes: List[str], start: date, end: date
) -> Dict[str, Dict[date, Decimal]]:
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return {}
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    try:
        raw = TQData().get_history_raw(
            stocks=wanted,
            periods=["1d"],
            start=start_str,
            end=end_str,
            dividend_type="front",
            count=-1,
        )
    except Exception:
        return {}
    if not isinstance(raw, dict) or not raw:
        return {}
    period = raw.get("1d")
    if not isinstance(period, dict):
        return {}
    close_df = period.get("Close")
    if close_df is None:
        return {}
    columns = getattr(close_df, "columns", [])
    out: Dict[str, Dict[date, Decimal]] = {}
    for code in wanted:
        if code not in columns:
            continue
        series: Dict[date, Decimal] = {}
        for ts in close_df.index:
            val = close_df.loc[ts, code]
            if _is_nan(val):
                continue
            d = ts.date() if hasattr(ts, "date") else ts
            if isinstance(d, datetime):
                d = d.date()
            series[d] = _to_decimal(val)
        if series:
            out[code] = series
    return out


def build_benchmark_data(
    ps, start: date, end: date, db: Session = None
) -> Dict[date, Decimal]:
    index_code = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
    return build_index_close_series([index_code], start, end).get(index_code, {})


def _crisis_benchmark_code(ps) -> str:
    return getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE


def _crisis_benchmark_name(code: str) -> str:
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == code:
            return spec["name"]
    return code


PARAMS_SNAPSHOT_FIELDS = (
    "benchmark_index",
    "initial_capital",
    "max_drawdown",
    "daily_loss_limit",
    "max_holdings",
    "min_commission",
    "buy_commission_rate",
    "sell_commission_rate",
    "stamp_duty_rate",
    "slippage",
    "trading_session",
    "stock_pool_id",
)


def _snapshot_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return repr(value)
    return str(value)


def build_params_snapshot(ps) -> str:
    payload = {
        field: _snapshot_value(getattr(ps, field, None))
        for field in PARAMS_SNAPSHOT_FIELDS
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def snapshot_benchmark_code(params_snapshot) -> Optional[str]:
    if not params_snapshot or not isinstance(params_snapshot, (str, bytes, bytearray)):
        return None
    try:
        data = json.loads(params_snapshot)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("benchmark_index")
    if isinstance(code, str) and code:
        return code
    return None


def resolve_crisis_benchmark(rec, row, ps) -> dict:
    live_code = _crisis_benchmark_code(ps)
    stored_code = getattr(row, "benchmark_code", None) if row is not None else None
    stored_name = getattr(row, "benchmark_name", None) if row is not None else None
    snap_code = snapshot_benchmark_code(getattr(rec, "params_snapshot", None))
    from_snapshot = snap_code is not None
    if from_snapshot:
        used_code = snap_code
    elif stored_code:
        used_code = stored_code
    else:
        used_code = live_code
    return {
        "from_snapshot": from_snapshot,
        "mismatch": used_code != live_code,
        "benchmark_code": used_code,
        "benchmark_name": _crisis_benchmark_name(used_code),
        "live_benchmark_code": live_code,
        "live_benchmark_name": _crisis_benchmark_name(live_code),
        "stored_benchmark_code": stored_code,
        "stored_benchmark_name": stored_name,
    }


def _crisis_nav_from_snapshots(snapshots: List[dict]) -> pd.Series:
    points = {}
    for snap in snapshots:
        d = snap.get("snap_date")
        v = snap.get("total_value")
        if d is None or v is None:
            continue
        if isinstance(d, datetime):
            d = d.date()
        points[d] = float(v)
    if not points:
        return pd.Series(dtype=float)
    return pd.Series(points, dtype=float).sort_index()


def _to_close_series(points: Dict[date, Decimal]) -> pd.Series:
    return pd.Series(
        {d: float(v) for d, v in points.items()}, dtype=float
    ).sort_index()


def _upsert_crisis_alpha(
    db: Session,
    record_id: int,
    benchmark_code: str,
    benchmark_name: str,
    status: str,
    error: Optional[str] = None,
    skipped: Optional[List[str]] = None,
    payload: Optional[dict] = None,
) -> BacktestCrisisAlpha:
    row = (
        db.query(BacktestCrisisAlpha)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    if row is None:
        row = BacktestCrisisAlpha(backtest_record_id=record_id)
        db.add(row)
    row.benchmark_code = benchmark_code
    row.benchmark_name = benchmark_name
    row.status = status
    row.error = error
    row.skipped_benchmarks_json = json.dumps(skipped or [], ensure_ascii=False)
    row.payload_json = (
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if payload is not None
        else None
    )
    row.computed_at = datetime.now()
    db.commit()
    return row


def _merge_crisis_skipped(base: List[str], extra: List[str]) -> List[str]:
    out = list(base)
    for name in extra:
        if name not in out:
            out.append(name)
    return out


def compute_and_store_crisis_alpha(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    start: date,
    end: date,
    benchmark_code: Optional[str] = None,
) -> BacktestCrisisAlpha:
    benchmark_code = benchmark_code or _crisis_benchmark_code(ps)
    benchmark_name = _crisis_benchmark_name(benchmark_code)
    codes = [benchmark_code] + [spec["code"] for spec in CRISIS_BENCHMARKS]
    closes = build_index_close_series(codes, start, end)
    skipped = [
        spec["name"] for spec in CRISIS_BENCHMARKS if spec["code"] not in closes
    ]
    main_closes = closes.get(benchmark_code) or {}
    payload_skipped: List[str] = []
    if not main_closes:
        return _upsert_crisis_alpha(
            db, record_id, benchmark_code, benchmark_name,
            status="failed",
            error="主基准指数无数据（TQ 取数失败或代码不被支持）",
            skipped=_merge_crisis_skipped(skipped, payload_skipped),
        )
    benchmark_closes = {}
    for spec in CRISIS_BENCHMARKS:
        got = closes.get(spec["code"])
        if got:
            benchmark_closes[spec["code"]] = _to_close_series(got)
    try:
        payload, payload_skipped = build_crisis_alpha_payload(
            _crisis_nav_from_snapshots(snapshots),
            benchmark_code,
            benchmark_name,
            _to_close_series(main_closes),
            benchmark_closes,
            getattr(ps, "name", "组合"),
        )
    except ValueError as exc:
        return _upsert_crisis_alpha(
            db, record_id, benchmark_code, benchmark_name,
            status="insufficient", error=str(exc),
            skipped=_merge_crisis_skipped(skipped, payload_skipped),
        )
    return _upsert_crisis_alpha(
        db, record_id, benchmark_code, benchmark_name,
        status="completed", payload=payload,
        skipped=_merge_crisis_skipped(skipped, payload_skipped),
    )


def safe_compute_crisis_alpha(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    start: date,
    end: date,
    benchmark_code: Optional[str] = None,
) -> Optional[BacktestCrisisAlpha]:
    try:
        return compute_and_store_crisis_alpha(
            db, record_id, ps, snapshots, start, end,
            benchmark_code=benchmark_code,
        )
    except Exception as exc:
        db.rollback()
        code = benchmark_code or _crisis_benchmark_code(ps)
        try:
            return _upsert_crisis_alpha(
                db, record_id, code, _crisis_benchmark_name(code),
                status="failed", error=str(exc),
            )
        except Exception:
            db.rollback()
            return None


def recompute_crisis_alpha(db: Session, record_id: int) -> Optional[dict]:
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return None
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    if not snaps:
        return {"status": "failed", "error": "该回测无组合日快照，无法计算"}
    ps = db.get(PortfolioStrategy, rec.portfolio_strategy_id)
    if ps is None:
        return {"status": "failed", "error": "组合策略已删除，无法确定基准指数"}
    existing = (
        db.query(BacktestCrisisAlpha)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    resolution = resolve_crisis_benchmark(rec, existing, ps)
    row = safe_compute_crisis_alpha(
        db, record_id, ps,
        [{"snap_date": s.snap_date, "total_value": s.total_value} for s in snaps],
        rec.start_date, rec.end_date,
        benchmark_code=resolution["benchmark_code"],
    )
    if row is None:
        return {"status": "failed", "error": "危机Alpha 结果落库失败"}
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        "benchmark_resolution": resolution,
    }


# 评估指标落库字段（运行落库与离线重算共用，避免两处漂移）
_EVALUATION_FIELDS = (
    "total_return", "annual_return", "max_drawdown", "volatility",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio", "win_rate",
    "profit_factor", "total_trades", "benchmark_return", "avg_holding_days",
    "var_95", "cvar_95", "avg_recovery_days", "max_recovery_days",
    "ulcer_index", "return_stability",
    "beta", "jensen_alpha", "excess_annual_return", "tracking_error",
    "information_ratio", "up_capture_ratio", "down_capture_ratio",
    "excess_max_drawdown",
    "annual_turnover", "total_cost_ratio", "avg_position_utilization",
    "empty_days_ratio", "max_single_weight", "hhi",
    "max_consecutive_loss_days", "worst_daily_return", "worst_weekly_return",
    "return_skewness", "return_kurtosis",
)


def recompute_evaluation(db: Session, record_id: int) -> Optional[dict]:
    """从 DB 快照 + 成交离线重算组合级评估并 upsert（老记录补新增指标用）。

    不重跑回测、不触碰行情；相对指标依赖快照 benchmark_value（老记录已落该列）。
    策略层评估无基准值，重算不会带来新字段，故只更新组合级行。
    记录不存在返回 None（路由 404）；快照不足返回 failed 状态。
    """
    from types import SimpleNamespace

    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return None

    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    if len(snaps) < 2:
        return {"status": "failed", "error": "组合日快照不足 2 条，无法重算评估"}

    snap_dicts = [
        {
            "snap_date": s.snap_date,
            "total_value": s.total_value,
            "cash": s.cash,
            "market_value": s.market_value,
            "benchmark_value": s.benchmark_value,
            # 仅新回测有 positions_json；"[]" 也是有效负载（空仓日计入 HHI）
            "positions": json.loads(s.positions_json) if s.positions_json else None,
        }
        for s in snaps
    ]
    trades = (
        db.query(BacktestTrade)
        .filter_by(backtest_record_id=record_id)
        .order_by(BacktestTrade.bar_time)
        .all()
    )
    trade_objs = [
        SimpleNamespace(
            strategy_id=t.strategy_id,
            stock_code=t.stock_code,
            # Evaluator FIFO 只读 trade_type.value
            trade_type=SimpleNamespace(value=t.trade_type),
            price=t.price,
            quantity=t.quantity,
            amount=t.amount,
            commission=t.commission,
            stamp_duty=t.stamp_duty,
            trade_time=t.bar_time,
        )
        for t in trades
    ]
    ev = Evaluator().evaluate(snap_dicts, trades=trade_objs)
    if not ev:
        return {"status": "failed", "error": "快照序列无法产出评估结果"}

    row = (
        db.query(BacktestEvaluation)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .one_or_none()
    )
    if row is None:
        row = BacktestEvaluation(
            backtest_record_id=record_id,
            target_type="portfolio",
            target_id=rec.portfolio_strategy_id,
        )
        db.add(row)
    for f in _EVALUATION_FIELDS:
        setattr(row, f, ev.get(f))
    db.commit()

    return {"status": "ok", "evaluations": _serialize_evaluation(row)}



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


def build_signal_cache(ps, klines: dict, db: Session = None, start: date = None) -> dict:
    """预计算公式信号：{(strategy_id, stock_code, bar_time): [{name, value}]}。

    对每个策略：读 Formula（公式名）+ FormulaSignal（信号配置），
    调 TQFormula.compute 跑公式，转 signal_cache。cache-first，TQ 兜底。

    分钟级（5m/15m/30m/60m）：TQ 公式输出 Date 只标到日（丢时分），按输出条目顺序
    对齐 klines 时间轴（_convert_formula_output 的 bar_times_by_code）。
    日线：按 Date 匹配（1:1 对齐）。

    start（回测开始日期）：klines 若含公式预热段（早于 start 的 bar），分钟级
    索引对齐必须基于全量时间轴（先对齐再过滤，否则索引串位）；转换完成后过滤掉
    bar_time < start 的预热条目，cache 只留主区间。start=None 不过滤（兼容单测）。
    """
    if db is None:
        return {}
    stocks = list(klines.keys())
    if not stocks:
        return {}
    strategies = _portfolio_strategies(ps, db)
    tq_formula = TQFormula()
    # 分钟级时间轴（所有策略共用同一 klines 时间轴，含预热段——与喂入公式引擎的数据同源）
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
    if start is not None:
        start_ts = datetime(start.year, start.month, start.day)
        cache = {k: v for k, v in cache.items() if k[2] >= start_ts}
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
            # 新回测才有 positions 键；空仓写 "[]"（有效负载），老引擎结果为 None
            positions_json=(
                json.dumps(snap["positions"], default=str)
                if snap.get("positions") is not None else None
            ),
        ))
    ev = result.get("evaluations") or {}
    db.add(BacktestEvaluation(
        backtest_record_id=record_id,
        target_type="portfolio",
        target_id=ps_id,
        **{f: ev.get(f) for f in _EVALUATION_FIELDS},
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
                positions_json=(
                    json.dumps(snap["positions"], default=str)
                    if snap.get("positions") is not None else None
                ),
            ))
    for sid, sev in strategy_evals.items():
        db.add(BacktestEvaluation(
            backtest_record_id=record_id,
            target_type="strategy",
            target_id=sid,
            **{f: sev.get(f) for f in _EVALUATION_FIELDS},
        ))

    # ---- 决策闸门事件（止损/止盈/熔断/资金不足/超仓…逐触发一条，调参可观测性）----
    for d in result.get("decisions") or []:
        db.add(BacktestDecisionEvent(
            backtest_record_id=record_id,
            gate=d["gate"],
            layer=d["layer"],
            action=d["action"],
            portfolio_id=d.get("portfolio_id"),
            strategy_id=d.get("strategy_id"),
            stock_code=d.get("stock_code"),
            bar_time=d.get("bar_time"),
            param_name=d.get("param_name"),
            param_value=d.get("param_value"),
            actual_value=d.get("actual_value"),
            requested_qty=d.get("requested_qty"),
            final_qty=d.get("final_qty"),
            message=(d.get("message") or "")[:200],
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


def _serialize_decision(d: BacktestDecisionEvent) -> dict:
    """决策闸门事件 → dict（详情页下钻明细）。"""
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
    }


def _serialize_decision_summary(s: dict) -> dict:
    """聚合统计行 → dict（bar_time 转 ISO 字符串）。"""
    return {
        "gate": s["gate"],
        "layer": s["layer"],
        "action": s["action"],
        "param_name": s["param_name"],
        "param_value": s["param_value"],
        "count": s["count"],
        "first_bar_time": s["first_bar_time"].isoformat() if s.get("first_bar_time") else None,
        "last_bar_time": s["last_bar_time"].isoformat() if s.get("last_bar_time") else None,
        "stock_count": s["stock_count"],
        "requested_qty_sum": s["requested_qty_sum"],
        "final_qty_sum": s["final_qty_sum"],
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
        "beta": _f(e.beta),
        "jensen_alpha": _f(e.jensen_alpha),
        "excess_annual_return": _f(e.excess_annual_return),
        "tracking_error": _f(e.tracking_error),
        "information_ratio": _f(e.information_ratio),
        "up_capture_ratio": _f(e.up_capture_ratio),
        "down_capture_ratio": _f(e.down_capture_ratio),
        "excess_max_drawdown": _f(e.excess_max_drawdown),
        "annual_turnover": _f(e.annual_turnover),
        "total_cost_ratio": _f(e.total_cost_ratio),
        "avg_position_utilization": _f(e.avg_position_utilization),
        "empty_days_ratio": _f(e.empty_days_ratio),
        "max_single_weight": _f(e.max_single_weight),
        "hhi": _f(e.hhi),
        "max_consecutive_loss_days": e.max_consecutive_loss_days,
        "worst_daily_return": _f(e.worst_daily_return),
        "worst_weekly_return": _f(e.worst_weekly_return),
        "return_skewness": _f(e.return_skewness),
        "return_kurtosis": _f(e.return_kurtosis),
    }


def build_return_buckets(snaps: list) -> dict:
    """从组合级每日快照构建月度矩阵 + 年度统计，供热力图/箱线图/年柱使用。

    snaps: BacktestDailySnapshot ORM 列表（按 snap_date 升序）。
    返回: {
        "monthly_matrix": {"years": [...], "months": [...], "matrix": [[...]], "text": [[...]]},
        "yearly": [{"year": int, "daily_returns": [...], "monthly_returns": [...], "annual_return": float}, ...],
    }
    每日净值取同日最后一条（日内多 bar 回测取日收盘），与 Evaluator._daily_returns 口径一致。
    """
    from collections import OrderedDict

    # --- 日净值序列（同日末条去重）---
    daily_map: OrderedDict = OrderedDict()
    for s in snaps:
        d = s.snap_date
        v = float(s.total_value) if s.total_value is not None else 0.0
        daily_map[d] = v  # 同日覆盖，保留最后一条
    dates = list(daily_map.keys())
    values = list(daily_map.values())
    if len(dates) < 2:
        return {"monthly_matrix": {"years": [], "months": [], "matrix": [], "text": []}, "yearly": []}

    # --- 日收益序列 ---
    daily_returns = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        daily_returns.append((values[i] - prev) / prev if prev != 0 else 0.0)
    daily_dates = dates[1:]

    # --- 月度收益：按月末净值 pct_change ---
    # 按 (year, month) 分组，取组内最后一个日净值
    monthly_vals: OrderedDict = OrderedDict()  # (year, month) → 日净值
    for d, v in daily_map.items():
        key = (d.year, d.month)
        monthly_vals[key] = v  # 同月覆盖，保留月末最后一条
    month_keys = list(monthly_vals.keys())
    month_vals = list(monthly_vals.values())
    monthly_returns = {}
    for i in range(1, len(month_keys)):
        key = month_keys[i]
        prev_val = month_vals[i - 1]
        cur_val = month_vals[i]
        monthly_returns[key] = (cur_val - prev_val) / prev_val if prev_val != 0 else 0.0

    # 构建年/月矩阵
    all_years = sorted(set(k[0] for k in month_keys))
    all_months = list(range(1, 13))
    matrix = []  # matrix[i][j] = 百分比数值或 None
    text = []
    for y in all_years:
        row_z = []
        row_t = []
        for m in all_months:
            val = monthly_returns.get((y, m))
            if val is not None:
                row_z.append(round(val * 100, 2))
                row_t.append(f"{val * 100:+.2f}%")
            else:
                row_z.append(None)
                row_t.append("")
        matrix.append(row_z)
        text.append(row_t)

    monthly_matrix = {
        "years": all_years,
        "months": [f"{m}月" for m in all_months],
        "matrix": matrix,
        "text": text,
    }

    # --- 年度统计：日收益按年分组（覆盖净值存在的每一年，含无收益的起点/残年）---
    yearly_map: OrderedDict = OrderedDict()
    for d in dates:
        yearly_map.setdefault(d.year, [])
    for d, dr in zip(daily_dates, daily_returns):
        yearly_map[d.year].append(dr)

    yearly = []
    for year in sorted(yearly_map.keys()):
        rets = yearly_map[year]
        # 月收益按月分组（从 daily_returns + dates）
        monthly_by_year: dict = {}
        for d, dr in zip(daily_dates, daily_returns):
            if d.year == year:
                monthly_by_year.setdefault(d.month, []).append(dr)
        monthly_rets = []
        for m in sorted(monthly_by_year.keys()):
            mr = 1.0
            for r in monthly_by_year[m]:
                mr *= (1 + r)
            monthly_rets.append(mr - 1.0)
        # 年度累计收益（复利）
        annual = 1.0
        for r in rets:
            annual *= (1 + r)
        annual_return = annual - 1.0 if rets else None
        yearly.append({
            "year": year,
            "daily_returns": rets,
            "monthly_returns": monthly_rets,
            "annual_return": annual_return,
        })

    return {"monthly_matrix": monthly_matrix, "yearly": yearly}


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

    # ---- 决策闸门事件（调参可观测性）：聚合统计 + 原始明细 ----
    decision_rows = (
        db.query(BacktestDecisionEvent)
        .filter_by(backtest_record_id=record_id)
        .order_by(BacktestDecisionEvent.bar_time, BacktestDecisionEvent.id)
        .all()
    )
    # summarize_decisions 吃 ORM 行（getattr 取字段），bar_time 按 datetime 比较
    decision_summary = [
        _serialize_decision_summary(s) for s in summarize_decisions(decision_rows)
    ]

    # ---- 收益分布桶（月度热力图 / 年度箱线图）----
    returns_buckets = build_return_buckets(snaps)

    ca = (
        db.query(BacktestCrisisAlpha)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    crisis_alpha = None
    if ca is not None:
        crisis_alpha = {
            "status": ca.status,
            "error": ca.error,
            "skipped_benchmarks": (
                json.loads(ca.skipped_benchmarks_json)
                if ca.skipped_benchmarks_json else []
            ),
            "computed_at": ca.computed_at.isoformat() if ca.computed_at else None,
            "benchmark_code": ca.benchmark_code,
            "benchmark_name": ca.benchmark_name,
            "data": json.loads(ca.payload_json) if ca.payload_json else None,
        }

    return {
        "record": _serialize_record(rec),
        "snapshots": [_serialize_snapshot(s) for s in snaps],
        "trades": [_serialize_trade_with_name(t, strat_name_map) for t in trades],
        "evaluations": _serialize_evaluation(evals) if evals else None,
        "strategy_evaluations": strategy_evaluations,
        "strategy_snapshots": strategy_snapshots,
        "decision_summary": decision_summary,
        "decisions": [_serialize_decision(d) for d in decision_rows],
        "returns_buckets": returns_buckets,
        "crisis_alpha": crisis_alpha,
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
    db.query(BacktestDecisionEvent).filter_by(backtest_record_id=record_id).delete()
    db.delete(rec)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------
def _log_timing(msg: str) -> None:
    """打印耗时日志，带时间戳。同步端点（uvicorn 单线程），print 安全。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def _log_kline_summary(record_id: int, klines: dict) -> None:
    """打印 K 线数据量摘要：股票数、各周期 bar 数。"""
    if not klines:
        _log_timing(f"[回测#{record_id}] K线数据为空")
        return
    stock_count = len(klines)
    first_code = next(iter(klines))
    period_bars = {}
    for period, df in klines[first_code].items():
        period_bars[period] = len(df) if df is not None else 0
    _log_timing(
        f"[回测#{record_id}] K线: {stock_count}只股票, "
        + ", ".join(f"{p}={n}bar" for p, n in period_bars.items())
    )


def run_backtest(db: Session, ps: PortfolioStrategy, req) -> dict:
    """回测主链路（从路由 _run_backtest_locked 迁入）。

    前置：路由层已保证 ps 非 None、日期合法、已持并发锁。
    内部：写 record(running) → assemble → build_klines → build_signal_cache →
          build_open_prices → build_benchmark_data → 空行情保护 → engine.run →
          _persist_result → 标 completed → 返回 {record_id, trades_count,
          snapshots_count, evaluations}。
    异常：标 record(failed) + re-raise（路由层全局异常处理器兜底转 {code:500}，
          与现状一致——原 _run_backtest_locked 也是 raise 出去）。

    返回纯 dict（不经 ok() 包装，由路由层统一 ok()）。
    """
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
        params_snapshot=build_params_snapshot(ps),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    record_id = rec.id

    try:
        t0 = datetime.now()
        portfolio = _assemble_portfolio(ps, strategies, db)
        t1 = datetime.now()
        # klines 含公式预热段（早于 start，保证首根 bar 公式有效值）
        klines_full = build_klines(ps, req.start_date, req.end_date, db)
        t2 = datetime.now()
        # 数据量摘要：各周期多少根 bar、多少只股票，帮助判断瓶颈
        _log_kline_summary(record_id, klines_full)
        # 信号计算喂全量（含预热段，分钟级索引对齐与喂入数据同源），start 起的条目才进 cache
        signal_cache = build_signal_cache(ps, klines_full, db, start=req.start_date)
        t3 = datetime.now()
        # 引擎视野切回主区间：预热段不进时间轴/成交/快照/熔断
        klines = _slice_klines_from(klines_full, req.start_date)
        open_prices = build_open_prices(ps, klines)
        t4 = datetime.now()
        benchmark_data = build_benchmark_data(ps, req.start_date, req.end_date, db)
        t5 = datetime.now()

        # 各阶段耗时日志（定位回测慢的根因）
        _log_timing(f"[回测#{record_id}] 组装组合: {(t1-t0).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ K线拉取: {(t2-t1).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 公式计算: {(t3-t2).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 提取开盘价: {(t4-t3).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 基准指数: {(t5-t4).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 数据准备合计: {(t5-t0).total_seconds():.1f}s")

        # 空行情保护：TQ 在该区间/股票池拉不到任何 K 线 → 标 failed 而非静默 completed。
        # 这正是"启动了但没运行直接完成"的根因（日期填反/未来日/股票池空等）。
        if not klines:
            rec.status = "failed"
            rec.error_message = (
                f"未取到任何行情数据：区间 {req.start_date}~{req.end_date}，"
                f"股票池 id={ps.stock_pool_id}。请检查日期区间与股票池成分。"
            )
            db.commit()
            return {"record_id": record_id, "trades_count": 0,
                    "snapshots_count": 0, "evaluations": {}}

        t6 = datetime.now()
        engine = BacktestEngine()
        result = engine.run(
            portfolio,
            klines=klines,
            signal_cache=signal_cache,
            open_prices=open_prices,
            benchmark_data=benchmark_data,
        )
        t7 = datetime.now()
        _log_timing(f"[回测#{record_id}] 引擎逐bar回测: {(t7-t6).total_seconds():.1f}s")

        _persist_result(db, record_id, ps.id, result, strategies)
        t8 = datetime.now()
        _log_timing(f"[回测#{record_id}] 结果持久化: {(t8-t7).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 总耗时: {(t8-t0).total_seconds():.1f}s")

        rec.status = "completed"
        rec.progress = 100
        rec.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC，与原 utcnow() 语义一致（Python 3.13 已弃用 utcnow）
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

    safe_compute_crisis_alpha(
        db, rec.id, ps, result["snapshots"], req.start_date, req.end_date
    )

    return {
        "record_id": record_id,
        "trades_count": len(result["trades"]),
        "snapshots_count": len(result["snapshots"]),
        "evaluations": result.get("evaluations") or {},
    }
