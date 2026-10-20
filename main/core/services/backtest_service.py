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
import math
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pandas as pd
import polars as pl
from sqlalchemy.orm import Session

from core.models import (
    StockPool, StockPoolStock, Strategy, Formula, FormulaSignal, PortfolioStrategy,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
    BacktestDecisionEvent, BacktestCrisisAlpha, BacktestMarketRegimeAnalysis,
    BacktestRobustnessAnalysis, WalkForwardRun,
)
from core.engine.backtest_engine import BacktestEngine
from core.engine.evaluator import Evaluator
from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, DEFAULT_BENCHMARK_CODE, build_crisis_alpha_payload,
)
from core.engine.backtest_robustness import analyze_robustness
from core.engine.regime import segment_regime, CausalRegimeTracker
from core.services.acceptance import ACCEPTANCE_PROFILES, compute_acceptance
from core.engine.regime_alpha import (
    QUADRANT_STYLE_INDICES,
    RegimeAlphaAnalyzer,
)
from core.engine.industry_attribution import (
    compute_industry_attribution, needs_rerun_block, TAXONOMY as INDUSTRY_TAXONOMY,
)
from core.engine.robustness import compute_robustness
from core.engine.decision import summarize_decisions, summarize_decisions_yearly
from core.services.oos_gate import evaluate_oos_gate
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
        # 伪影过滤：Open/Close ≤0（负价/零价）或 Close 近零（<1分）——前复权加性压缩产物，
        # 会放大收益/巨量股数，直接丢弃该 bar（不进信号/成交/估值）。
        o = _to_decimal(raw["Open"].loc[ts, code])
        c = _to_decimal(raw["Close"].loc[ts, code])
        if o <= 0 or c <= 0 or c < _MIN_TRADE_PRICE:
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


# 最低可交易价：A股无低于 1 分钱的真实成交价，前复权"加性压缩"产物可能逼近 0/转负，
# 用于在取数层过滤伪影行（防近零价 → 巨量股数 / 负价成交现金倒增）。
_MIN_TRADE_PRICE = Decimal("0.01")


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


# W-A：预热窗口周期→自然日换算（bar/交易日近似，含缓冲）。取保守上限，
# 宁多拉勿少（覆盖停牌/节假日导致 bar 数不足），start 前推只多取几根预热 bar。
_PERIOD_BARS_PER_TRADING_DAY = {
    "1m": 240, "5m": 48, "15m": 16, "30m": 8, "1h": 4, "1d": 1,
    "1w": 1 / 5, "1mon": 1 / 22,
}


def _warmup_span_days(period: str, count: int) -> int:
    """估算覆盖 count 根该周期 bar 所需自然日（交易日→自然日 ×7/5，加缓冲）。"""
    per_day = _PERIOD_BARS_PER_TRADING_DAY.get(period, 1)
    trading_days = count / per_day if per_day else count
    calendar_days = trading_days * 7 / 5
    return int(calendar_days * 1.5) + 10


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


def build_klines(ps, start: date, end: date, db: Session = None,
                 dividend_mode: str = "front") -> dict:
    """从 TQ 取历史 K 线：{stock_code: {period: pl.DataFrame}}（含公式预热段）。

    股票来自 ps.stock_pool_id 对应的 stock_pool_stocks；周期取所有策略的 period 去重。
    需要 db 查股票池；若未传 db，返回空（无法定位股票）。
    流程：主段 start 前推覆盖公式预热窗口 → 一次 get_history_raw(count=-1) 取全量
    （含预热 bar）→ _convert_market_data_multi（转引擎 polars）。

    W-A：预热段不再单独逐周期拉取（原 1+N 次往返），改为把主段 start 前推一个
    覆盖 max(formula_count) 的自然日窗口，一次取全量后由调用方 _slice_klines_from
    切回主区间（预热 bar 只喂 build_signal_cache，不进引擎时间轴）。
    dividend_mode：复权模式（"front" 前复权 / "back" 后复权），K线取数透传给 TQ。
    """
    stocks = _pool_stocks(ps, db)
    if not stocks:
        return {}
    periods = _strategy_periods(ps, db)
    warmup_counts = warmup_counts_by_period(ps, db)
    # W-A：start 前推覆盖预热窗口（按周期估算自然日，保守上限）
    fetch_start = start
    if warmup_counts:
        span = max(
            (_warmup_span_days(p, cnt) for p, cnt in warmup_counts.items()
             if p in periods),
            default=0,
        )
        if span > 0:
            fetch_start = start - timedelta(days=span)
    start_str = fetch_start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    tq = TQData()
    raw_by_period = tq.get_history_raw(
        stocks=stocks, periods=periods,
        start=start_str, end=end_str, dividend_type=dividend_mode, count=-1,
    )
    return _convert_market_data_multi(raw_by_period, stocks)


def build_index_close_series(
    codes: List[str], start: date, end: date, batch_size: int = 0,
    dividend_mode: str = "front",
) -> Dict[str, Dict[date, Decimal]]:
    """取指数日线收盘价。batch_size>0 时分批取数（行业指数 128 个用）。"""
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return {}
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    chunks = ([wanted[i:i + batch_size]
               for i in range(0, len(wanted), batch_size)]
              if batch_size else [wanted])
    out: Dict[str, Dict[date, Decimal]] = {}
    for chunk in chunks:
        try:
            raw = TQData().get_history_raw(
                stocks=chunk,
                periods=["1d"],
                start=start_str,
                end=end_str,
                dividend_type=dividend_mode,
                count=-1,
            )
        except Exception:
            continue
        if not isinstance(raw, dict) or not raw:
            continue
        period = raw.get("1d")
        if not isinstance(period, dict):
            continue
        close_df = period.get("Close")
        if close_df is None:
            continue
        columns = getattr(close_df, "columns", [])
        for code in chunk:
            if code not in columns or code in out:
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
    ps, start: date, end: date, db: Session = None, dividend_mode: str = "front"
) -> Dict[date, Decimal]:
    index_code = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
    return build_index_close_series(
        [index_code], start, end, dividend_mode=dividend_mode,
    ).get(index_code, {})


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
    "position_sizing",
    # #1/#2/W2 新执行约束：影响回测成交/资金行为，冻结以保证结果可复现。
    # （此前缺失——11-fair 的成交量约束仅存于组合表、未进快照，无法仅凭记录复现）
    "price_limit_check_enabled",
    "volume_limit_enabled",
    "volume_limit_ratio",
    "adaptive_slippage_enabled",
    "slippage_impact_coef",
    "cash_enhance_enabled",
    "cash_enhance_equity_threshold",
    "cash_enhance_position_ratio_threshold",
    "equity_cap",
)

# 复权模式：K线/指数传 dividend_type 字符串（"front"/"back"，通达信 SDK 原样透传）；
# 公式信号传整数（front=1/back=2）。缺省前复权，保持历史行为。
# "dual" = 信号前复权 + 执行/记账后复权（模拟实盘口径：公式按前复权出信号、真实价成交）。
DIVIDEND_MODES = ("front", "back", "dual")
_FORMULA_DIVIDEND_TYPE = {"front": 1, "back": 2}

# W5-O1b(M4) 扰动值域：策略数值参数的有效区间，越界钳制（装配入口统一生效）。
# capital_ratio 为持仓上限占比 ≤1；比率类 (0,1]；整型 ≥1 在装配处 max(1,·) 已保。
PERTURB_BOUNDS: dict = {
    "capital_ratio": (None, 1.0),
    "single_open_ratio": (0.0, 1.0),
    "add_position_ratio": (0.0, 1.0),
    "reduce_position_ratio": (0.0, 1.0),
    "stop_loss_ratio": (0.0, 1.0),
    "take_profit_ratio": (0.0, 1.0),
    "trailing_stop_ratio": (0.0, 1.0),
}


def clamp_perturbed_value(field: str, value):
    """按 PERTURB_BOUNDS 钳制扰动后的参数值；越界取边界值（丢弃越界方向由
    微扰层 build_perturb_rounds 判定）。无界字段原样返回。"""
    if value is None:
        return value
    lo, hi = PERTURB_BOUNDS.get(field, (None, None))
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return value


def _snapshot_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _json_safe(obj):
    """递归清洗分析 payload：NaN/±Inf 浮点 → None。

    分析器在样本不足或零方差场景可能产出非有限浮点（如相关系数 0/0），
    落库用 allow_nan=False 严格 JSON，故在序列化边界统一转 null（与前端
    「安全空值」约定一致），不逐个分析点特判。非 float 类型原样返回。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def build_params_snapshot(ps, dividend_mode: str = "front",
                          fee_reserve_enabled: bool = False,
                          bond_t0_enabled: bool = False,
                          bond_lot_enabled: bool = False,
                          adaptive_slippage_enabled: bool | None = None,
                          slippage_impact_coef=None,
                          volume_limit_enabled: bool | None = None,
                          volume_limit_ratio=None,
                          equity_cap=None) -> str:
    payload = {
        field: _snapshot_value(getattr(ps, field, None))
        for field in PARAMS_SNAPSHOT_FIELDS
    }
    payload["dividend_mode"] = dividend_mode
    # run 级可选开关/覆盖冻结入快照，保证结果可复现：
    # 费用预留/可转债 T+0/转债 10 张（均默认关）；自适应滑点/成交量/容量 run 级覆盖（None=用组合默认）
    payload["fee_reserve_enabled"] = bool(fee_reserve_enabled)
    payload["bond_t0_enabled"] = bool(bond_t0_enabled)
    payload["bond_lot_enabled"] = bool(bond_lot_enabled)
    for name, val in (
        ("adaptive_slippage_enabled", adaptive_slippage_enabled),
        ("slippage_impact_coef", slippage_impact_coef),
        ("volume_limit_enabled", volume_limit_enabled),
        ("volume_limit_ratio", volume_limit_ratio),
        ("equity_cap", equity_cap),
    ):
        if val is not None:
            payload[name] = _snapshot_value(val)
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
        json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False)
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
        return {"status": "failed", "error": "该回测无组合日快照，无法计算", "computed_at": None, "skipped_benchmarks": []}
    ps = db.get(PortfolioStrategy, rec.portfolio_strategy_id)
    if ps is None:
        return {"status": "failed", "error": "组合策略已删除，无法确定基准指数", "computed_at": None, "skipped_benchmarks": []}
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
        return {"status": "failed", "error": "危机Alpha 结果落库失败", "computed_at": None, "skipped_benchmarks": []}
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        "skipped_benchmarks": (
            json.loads(row.skipped_benchmarks_json)
            if row.skipped_benchmarks_json else []
        ),
        "benchmark_resolution": resolution,
    }


# ---------------------------------------------------------------------------
# 市场状态×绩效分析（FR-11）：regime 分段 + RegimeAlphaAnalyzer，沿用危机模式
# ---------------------------------------------------------------------------
def _upsert_market_regime(
    db: Session,
    record_id: int,
    benchmark_code: str,
    benchmark_name: str,
    status: str,
    error: Optional[str] = None,
    skipped: Optional[List[str]] = None,
    payload: Optional[dict] = None,
) -> BacktestMarketRegimeAnalysis:
    row = (
        db.query(BacktestMarketRegimeAnalysis)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    if row is None:
        row = BacktestMarketRegimeAnalysis(backtest_record_id=record_id)
        db.add(row)
    row.benchmark_code = benchmark_code
    row.benchmark_name = benchmark_name
    row.status = status
    row.error = error
    row.skipped_benchmarks_json = json.dumps(skipped or [], ensure_ascii=False)
    row.payload_json = (
        json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False)
        if payload is not None
        else None
    )
    row.computed_at = datetime.now()
    db.commit()
    return row


# 与危机一致的共同交易日下限（不足标 insufficient，不落 payload）
_MARKET_REGIME_MIN_DAYS = 30
_INDUSTRY_FETCH_BATCH = 40


def _build_industry_attribution(
    snapshots: List[dict], start: date, end: date
) -> dict:
    """行业归因块（TQ I/O 层）。任何异常不影响市场态主链路。

    老记录（快照无 positions_json）→ needs_rerun；
    TQ 行业数据整体不可得 → failed 块；部分不可得则按可得集合计算。
    """
    if not any(s.get("positions") is not None for s in snapshots):
        return needs_rerun_block()
    try:
        tq_data = TQData()
        industries = tq_data.get_industry_list()
        names = {ind["code"]: ind["name"] for ind in industries}
        held_codes = set()
        for snap in snapshots:
            for pos in snap.get("positions") or []:
                code = pos.get("stock_code")
                if code:
                    held_codes.add(code)
        stock_industry: Dict[str, Optional[dict]] = {}
        for code in held_codes:
            try:
                stock_industry[code] = tq_data.get_stock_industry(code)
            except Exception:
                stock_industry[code] = None
        # 命中率排名需要全量行业指数；分批取数
        all_codes = [ind["code"] for ind in industries]
        raw_closes = (build_index_close_series(
            all_codes, start, end, batch_size=_INDUSTRY_FETCH_BATCH)
            if all_codes else {})
        industry_closes = {code: _to_close_series(pts)
                           for code, pts in raw_closes.items()}
        return compute_industry_attribution(
            snapshots, stock_industry, names, industry_closes)
    except Exception as exc:
        return {
            "status": "failed",
            "taxonomy": INDUSTRY_TAXONOMY,
            "error": f"行业归因取数失败：{exc}",
        }


def compute_and_store_market_regime(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    trades: List[BacktestTrade],
    start: date,
    end: date,
    benchmark_code: Optional[str] = None,
) -> BacktestMarketRegimeAnalysis:
    benchmark_code = benchmark_code or _crisis_benchmark_code(ps)
    benchmark_name = _crisis_benchmark_name(benchmark_code)
    # 主基准 + 6 宽基 + 巨潮四象限（风格轮动/分化度/隐含暴露复用 crisis 取数）
    codes = ([benchmark_code]
             + [spec["code"] for spec in CRISIS_BENCHMARKS]
             + [spec["code"] for spec in QUADRANT_STYLE_INDICES])
    closes = build_index_close_series(codes, start, end)
    main_closes = closes.get(benchmark_code) or {}
    if not main_closes:
        return _upsert_market_regime(
            db, record_id, benchmark_code, benchmark_name,
            status="failed",
            error="主基准指数无数据（TQ 取数失败或代码不被支持）",
        )
    benchmark = _to_close_series(main_closes)
    benchmark_closes: Dict[str, pd.Series] = {}
    for spec in CRISIS_BENCHMARKS:
        got = closes.get(spec["code"])
        if got:
            benchmark_closes[spec["code"]] = _to_close_series(got)
    quadrant_closes: Dict[str, pd.Series] = {}
    for spec in QUADRANT_STYLE_INDICES:
        got = closes.get(spec["code"])
        if got:
            quadrant_closes[spec["code"]] = _to_close_series(got)
    nav = _crisis_nav_from_snapshots(snapshots)
    common = nav.index.intersection(benchmark.index)
    if len(common) < _MARKET_REGIME_MIN_DAYS:
        return _upsert_market_regime(
            db, record_id, benchmark_code, benchmark_name,
            status="insufficient",
            error=(f"策略与基准共同交易日不足{_MARKET_REGIME_MIN_DAYS}天"
                   f"（{len(common)}），无法分析"),
        )
    regime = segment_regime(benchmark)
    analyzer = RegimeAlphaAnalyzer(
        nav, benchmark,
        trades=trades, regime_weeks=regime,
        strategy_name=getattr(ps, "name", "组合"),
        benchmark_name=benchmark_name,
        benchmark_closes=benchmark_closes,
        quadrant_closes=quadrant_closes,
    )
    payload = analyzer.analyze()
    payload["industry_attribution"] = _build_industry_attribution(
        snapshots, start, end)
    # FR-20 稳健性：纯快照+基准计算，不触发回测重跑；失败隔离不影响主链路
    names = {spec["code"]: spec["name"] for spec in CRISIS_BENCHMARKS}
    names[benchmark_code] = benchmark_name
    try:
        payload["robustness"] = compute_robustness(
            nav, benchmark,
            benchmark_closes=benchmark_closes,
            benchmark_names=names, primary_code=benchmark_code)
    except Exception as exc:
        payload["robustness"] = {
            "status": "failed", "error": f"稳健性分析计算失败：{exc}"}
    return _upsert_market_regime(
        db, record_id, benchmark_code, benchmark_name,
        status="completed", payload=payload,
    )


def safe_compute_market_regime(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    trades: List[BacktestTrade],
    start: date,
    end: date,
    benchmark_code: Optional[str] = None,
) -> Optional[BacktestMarketRegimeAnalysis]:
    try:
        return compute_and_store_market_regime(
            db, record_id, ps, snapshots, trades, start, end,
            benchmark_code=benchmark_code,
        )
    except Exception as exc:
        db.rollback()
        code = benchmark_code or _crisis_benchmark_code(ps)
        try:
            return _upsert_market_regime(
                db, record_id, code, _crisis_benchmark_name(code),
                status="failed", error=str(exc),
            )
        except Exception:
            db.rollback()
            return None


def recompute_market_regime(db: Session, record_id: int) -> Optional[dict]:
    """离线重算市场状态分析（不重跑回测）：组合快照净值 + 主基准日线 +
    成交流水 → regime 分段 + RegimeAlphaAnalyzer → upsert。"""
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
        return {"status": "failed", "error": "该回测无组合日快照，无法计算", "computed_at": None, "skipped_benchmarks": []}
    ps = db.get(PortfolioStrategy, rec.portfolio_strategy_id)
    if ps is None:
        return {"status": "failed", "error": "组合策略已删除，无法确定基准指数", "computed_at": None, "skipped_benchmarks": []}
    existing = (
        db.query(BacktestMarketRegimeAnalysis)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    resolution = resolve_crisis_benchmark(rec, existing, ps)
    trades = (
        db.query(BacktestTrade)
        .filter_by(backtest_record_id=record_id)
        .order_by(BacktestTrade.bar_time, BacktestTrade.id)
        .all()
    )
    row = safe_compute_market_regime(
        db, record_id, ps,
        [{"snap_date": s.snap_date, "total_value": s.total_value,
          "positions": (json.loads(s.positions_json)
                        if s.positions_json is not None else None)}
         for s in snaps],
        trades,
        rec.start_date, rec.end_date,
        benchmark_code=resolution["benchmark_code"],
    )
    if row is None:
        return {"status": "failed", "error": "市场状态分析结果落库失败", "computed_at": None, "skipped_benchmarks": []}
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        "skipped_benchmarks": (
            json.loads(row.skipped_benchmarks_json)
            if row.skipped_benchmarks_json else []
        ),
        "benchmark_resolution": resolution,
    }


# ---------------------------------------------------------------------------
# 回测稳健性检验（年度/3年滚动/个股集中度/样本内外对比，纯后处理不重跑）
# ---------------------------------------------------------------------------
def _upsert_robustness(
    db: Session, record_id: int, status: str,
    error: Optional[str] = None, payload: Optional[dict] = None,
) -> BacktestRobustnessAnalysis:
    row = (
        db.query(BacktestRobustnessAnalysis)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    if row is None:
        row = BacktestRobustnessAnalysis(backtest_record_id=record_id)
        db.add(row)
    row.status = status
    row.error = error
    row.payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False) if payload else None
    db.commit()
    db.refresh(row)
    return row


def compute_and_store_robustness(
    db: Session, record_id: int, oos_ratio: float = 0.25,
) -> BacktestRobustnessAnalysis:
    """读已落库快照+成交 → analyze_robustness → upsert。

    纯后处理：直接消费 DB 数据（与复权模式无关），不重跑回测。
    """
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    trades = (
        db.query(BacktestTrade)
        .filter_by(backtest_record_id=record_id)
        .all()
    )
    payload = analyze_robustness(
        snaps=[
            {"snap_date": s.snap_date, "total_value": s.total_value,
             "benchmark_value": s.benchmark_value}
            for s in snaps
        ],
        trades=[
            {"stock_code": t.stock_code, "trade_type": t.trade_type, "amount": t.amount,
             "commission": t.commission, "stamp_duty": t.stamp_duty}
            for t in trades
        ],
        oos_ratio=oos_ratio,
    )
    return _upsert_robustness(db, record_id, status="completed", payload=payload)


def safe_compute_robustness(
    db: Session, record_id: int, oos_ratio: float = 0.25,
) -> Optional[BacktestRobustnessAnalysis]:
    """回测完成后的自动稳健性分析：异常不中断主链路，落 status=failed。"""
    try:
        return compute_and_store_robustness(db, record_id, oos_ratio=oos_ratio)
    except Exception as exc:
        db.rollback()
        try:
            return _upsert_robustness(db, record_id, status="failed", error=str(exc))
        except Exception:
            db.rollback()
            return None


def recompute_robustness(db: Session, record_id: int, oos_ratio: float = 0.25) -> Optional[dict]:
    """离线重算稳健性检验（详情页手动触发）。记录不存在返回 None（路由 404）。"""
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
        return {"status": "failed", "error": "该回测无组合日快照，无法计算", "computed_at": None}
    row = safe_compute_robustness(db, record_id, oos_ratio=oos_ratio)
    if row is None:
        return {"status": "failed", "error": "稳健性分析结果落库失败", "computed_at": None}
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


# ---------------------------------------------------------------------------
# 换池稳健性对比（Tier2）：克隆组合（同参数仅换池）逐个重跑回测并对比
# ---------------------------------------------------------------------------
def ensure_pool_clone(
    db: Session, source_ps_id: int, pool_id: int, name: str,
) -> PortfolioStrategy:
    """克隆组合：照抄源组合全部字段 + 逐策略克隆（参数一致），仅把组合级/策略级
    股票池都指向目标池。已存在同名组合则复用（幂等）。"""
    existing = db.query(PortfolioStrategy).filter_by(name=name).one_or_none()
    if existing is not None:
        return existing
    src = db.get(PortfolioStrategy, source_ps_id)
    if src is None:
        raise ValueError(f"源组合策略不存在（id={source_ps_id}）")
    clone = PortfolioStrategy(
        name=name, stock_pool_id=pool_id,
        benchmark_index=src.benchmark_index,
        initial_capital=src.initial_capital, max_drawdown=src.max_drawdown,
        daily_loss_limit=src.daily_loss_limit, max_holdings=src.max_holdings,
        min_commission=src.min_commission, buy_commission_rate=src.buy_commission_rate,
        sell_commission_rate=src.sell_commission_rate, stamp_duty_rate=src.stamp_duty_rate,
        slippage=src.slippage, trading_session=src.trading_session,
    )
    db.add(clone)
    db.flush()
    for s in _portfolio_strategies(src, db):
        db.add(Strategy(
            portfolio_id=clone.id, name=s.name, formula_id=s.formula_id,
            period=s.period, role=s.role, master_strategy_id=s.master_strategy_id,
            capital_ratio=s.capital_ratio, max_positions=s.max_positions,
            single_open_ratio=s.single_open_ratio,
            stop_loss_ratio=s.stop_loss_ratio, take_profit_ratio=s.take_profit_ratio,
            trailing_stop_ratio=s.trailing_stop_ratio,
            add_position_threshold=s.add_position_threshold,
            max_add_count=s.max_add_count, add_position_ratio=s.add_position_ratio,
            reduce_position_ratio=s.reduce_position_ratio,
            stock_pool_id=pool_id, slave_exit_mode=s.slave_exit_mode,
        ))
    db.commit()
    db.refresh(clone)
    return clone


class _PoolCompareReq:
    """run_backtest 请求替身（只读 name/start_date/end_date/dividend_mode）。"""

    def __init__(self, name, start, end, dividend_mode):
        self.name = name
        self.start_date = start
        self.end_date = end
        self.dividend_mode = dividend_mode


def run_pool_comparison(
    db: Session, source_ps_id: int, pool_ids: List[int],
    start: date, end: date, name_prefix: str = "换池对比",
    dividend_mode: str = "dual",
) -> list:
    """为每个备选池克隆组合并跑一遍回测（同参数仅换池），返回可并排比较的结果。

    逐个同步执行（路由层应持有回测并发锁）；每次 run_backtest 会自动算
    稳健性检验，故每个对比记录带 metrics + robustness。"""
    pools = {
        p.id: p.name for p in db.query(StockPool).filter(StockPool.id.in_(pool_ids)).all()
    }
    results = []
    for pid in pool_ids:
        pname = pools.get(pid, f"pool-{pid}")
        clone = ensure_pool_clone(db, source_ps_id, pid, f"{name_prefix}-{pname}")
        try:
            r = run_backtest(
                db, clone,
                _PoolCompareReq(
                    f"{name_prefix}-{pname}-{start.year}-{end.year}",
                    start, end, dividend_mode,
                ),
            )
            rid = r["record_id"]
            ev = db.query(BacktestEvaluation).filter_by(
                backtest_record_id=rid, target_type="portfolio").first()
            metrics = None
            if ev is not None:
                metrics = {
                    "total_return": _f_val(ev.total_return),
                    "annual_return": _f_val(ev.annual_return),
                    "max_drawdown": _f_val(ev.max_drawdown),
                    "sharpe_ratio": _f_val(ev.sharpe_ratio),
                    "win_rate": _f_val(ev.win_rate),
                    "profit_factor": _f_val(ev.profit_factor),
                    "total_trades": ev.total_trades,
                    "avg_position_utilization": _f_val(ev.avg_position_utilization),
                    "benchmark_return": _f_val(ev.benchmark_return),
                }
            rb = db.query(BacktestRobustnessAnalysis).filter_by(
                backtest_record_id=rid).one_or_none()
            robustness = json.loads(rb.payload_json) if (rb and rb.payload_json) else None
            results.append({
                "pool_id": pid, "pool_name": pname,
                "record_id": rid, "status": "completed",
                "metrics": metrics, "robustness": robustness,
            })
        except Exception as exc:
            db.rollback()
            results.append({
                "pool_id": pid, "pool_name": pname,
                "record_id": None, "status": "failed", "error": str(exc),
            })
    return results


def _f_val(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 评估指标落库字段（运行落库与离线重算共用，避免两处漂移）
_EVALUATION_FIELDS = (
    "total_return", "annual_return", "max_drawdown", "volatility",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio", "win_rate",
    "profit_factor", "total_trades", "closed_trades", "benchmark_return", "avg_holding_days",
    "var_95", "cvar_95", "avg_recovery_days", "max_recovery_days",
    "ulcer_index", "return_stability",
    "beta", "jensen_alpha", "excess_annual_return", "tracking_error",
    "information_ratio", "up_capture_ratio", "down_capture_ratio",
    "excess_max_drawdown",
    "annual_turnover", "total_cost_ratio", "avg_position_utilization",
    "empty_days_ratio", "max_single_weight", "hhi",
    "max_consecutive_loss_days", "max_consecutive_profit_days",
    "worst_daily_return", "worst_weekly_return",
    "return_skewness", "return_kurtosis",
)


def recompute_evaluation(db: Session, record_id: int) -> Optional[dict]:
    """从 DB 快照 + 成交离线重算评估并 upsert（老记录补新增指标用）。

    不重跑回测、不触碰行情；相对指标依赖快照 benchmark_value（老记录已落该列）。
    组合级：直接重算。策略级：策略快照未落 benchmark_value（历史），按日注入组合
    快照的基准值后重算——相对/磨损/路径指标可补；持仓结构仍依赖 positions_json，
    老快照无此列时保持 None（前端提示「仅影响老记录」）。
    记录不存在返回 None（路由 404）；快照不足返回 failed 状态。
    """
    from types import SimpleNamespace
    from collections import defaultdict

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

    # ---- 策略层离线重算（注入组合基准值后补相对/磨损/路径指标） ----
    strat_snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="strategy")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    # 按 (date, strategy_id) 索引，合并时注入 portfolio 基准值
    benchmark_by_date = {s.snap_date: s.benchmark_value for s in snaps}
    strategy_snap_groups: dict = defaultdict(list)
    for s in strat_snaps:
        strategy_snap_groups[s.target_id].append(s)
    for sid, srows in strategy_snap_groups.items():
        s_dicts = [
            {
                "snap_date": s.snap_date,
                "total_value": s.total_value,
                "cash": s.cash,
                "market_value": s.market_value,
                "benchmark_value": benchmark_by_date.get(s.snap_date),
                "positions": json.loads(s.positions_json) if s.positions_json else None,
            }
            for s in srows
        ]
        s_trades = [t for t in trade_objs if t.strategy_id == sid]
        sev = Evaluator().evaluate(s_dicts, trades=s_trades)
        if not sev:
            continue
        ser = (
            db.query(BacktestEvaluation)
            .filter_by(backtest_record_id=record_id, target_type="strategy", target_id=sid)
            .one_or_none()
        )
        if ser is None:
            ser = BacktestEvaluation(
                backtest_record_id=record_id,
                target_type="strategy",
                target_id=sid,
            )
            db.add(ser)
        for f in _EVALUATION_FIELDS:
            setattr(ser, f, sev.get(f))
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
      按 bar 顺序排列。**按 Date 逐日对齐**：公式某日的第 j 条 → 该日 klines 第 j 根 bar
      （bar_times_by_code[code] 中同一 date 的有序子集）。不能用全局索引——批量取数
      fill_data=True 会给新股上市前/停牌日也补 carry-forward bar，全局索引会把公式
      输出整体前移（漂移=上市前/停牌日多余的 klines bar 数，实测新股 688808.SH 漂 784 根）。
      某日 klines 无真实 bar（停牌/上市前）→ 该日公式输出直接丢弃；输出条数超过
      该日 bar 数 → 多余输出丢弃。
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
        if bar_times is not None:
            # 分钟级：预构建 date → [当日 bar 时间（升序）]
            bars_by_date: Dict[date, List[datetime]] = {}
            for bt in bar_times:
                bars_by_date.setdefault(bt.date(), []).append(bt)
            for var_name, val_list in stock_data.items():
                if var_name in _FORMULA_META_KEYS or not isinstance(val_list, list):
                    continue
                day_counter: Dict[date, int] = {}
                for entry in val_list:
                    if not isinstance(entry, dict):
                        continue
                    v = entry.get("Value")
                    if v is None:
                        continue
                    d = _parse_date_str(entry.get("Date"))
                    if d is None:
                        continue
                    bars = bars_by_date.get(d.date())
                    if not bars:
                        # 该日 klines 无真实 bar（停牌/新股上市前）→ 公式输出丢弃
                        continue
                    j = day_counter.get(d.date(), 0)
                    day_counter[d.date()] = j + 1
                    if j >= len(bars):
                        # 输出条数超过该日 bar 数 → 多余丢弃
                        continue
                    key = (strategy_id, code, bars[j])
                    by_time.setdefault(key, []).append(
                        {"name": var_name, "value": _to_int(v)})
        else:
            # 日线：按 Date 转午夜 datetime
            for var_name, val_list in stock_data.items():
                if var_name in _FORMULA_META_KEYS or not isinstance(val_list, list):
                    continue
                for entry in val_list:
                    if not isinstance(entry, dict):
                        continue
                    v = entry.get("Value")
                    if v is None:
                        continue
                    d = entry.get("Date")
                    if not d:
                        continue
                    bar_time = _parse_date_str(d)
                    if bar_time is None:
                        continue
                    key = (strategy_id, code, bar_time)
                    by_time.setdefault(key, []).append(
                        {"name": var_name, "value": _to_int(v)})
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
    """从 klines 提取每只股票的时间轴（升序去重），供分钟级公式输出按索引对齐。

    注意：此函数合并所有周期的时间戳。多周期组合回测时，5m 策略的公式输出
    会与 1d 午夜时间戳混合对齐导致串位。新代码应改用 _bar_times_by_period。
    """
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


def _bar_times_by_period(klines: dict, period: str) -> Dict[str, List[datetime]]:
    """从 klines 提取每只股票指定周期的时间轴（升序），供分钟级公式输出按索引对齐。

    与 _bar_times_by_code 的区别：只取本策略周期的时间戳，避免多周期并集导致
    索引串位（5m 输出对齐到 [1d 00:00, 5m 09:30, ...] 混合时间轴）。
    """
    result: Dict[str, List[datetime]] = {}
    for code, periods in klines.items():
        df = periods.get(period)
        if df is not None and "datetime" in df.columns:
            result[code] = sorted(df["datetime"].to_list())
    return result


def _formula_diagnostics(raw, stocks: list):
    """从 TQ 原始返回提取诊断：(error_msg|None, output_lines:set)。

    ErrorId 非 0/19 → 公式执行错误（语法错误/引用公式缺失等）；
    正常时收集所有股票的输出线名并集（过滤 ErrorId 等元信息键）。
    """
    if not isinstance(raw, dict) or not raw:
        return "公式执行无返回（通达信未返回数据，请确认公式名与通达信内一致）", set()
    err = raw.get("ErrorId")
    if err is not None and str(err) not in ("0", "19"):
        return f"ErrorId={err} Error={raw.get('Error')}", set()
    lines: set = set()
    for code in stocks:
        stock_data = raw.get(code)
        if isinstance(stock_data, dict):
            lines.update(k for k in stock_data.keys() if k not in _FORMULA_META_KEYS)
    return None, lines


def build_signal_cache(
    ps, klines: dict, db: Session = None, start: date = None,
    diagnostics: Optional[dict] = None, dividend_mode: str = "front",
) -> dict:
    """预计算公式信号：{(strategy_id, stock_code, bar_time): [{name, value}]}。

    对每个策略：读 Formula（公式名）+ FormulaSignal（信号配置），
    调 TQFormula.compute 跑公式，转 signal_cache。cache-first，TQ 兜底。

    分钟级（5m/15m/30m/60m）：TQ 公式输出 Date 只标到日（丢时分），按输出条目顺序
    对齐 klines 时间轴（_convert_formula_output 的 bar_times_by_code）。
    日线：按 Date 匹配（1:1 对齐）。

    start（回测开始日期）：klines 若含公式预热段（早于 start 的 bar），分钟级
    索引对齐必须基于全量时间轴（先对齐再过滤，否则索引串位）；转换完成后过滤掉
    bar_time < start 的预热条目，cache 只留主区间。start=None 不过滤（兼容单测）。

    diagnostics（可选，调用方传入的 dict）：回填每策略公式执行诊断——
    {"errors": {strategy_id: msg}, "output_lines": {strategy_id: set(线名)}}，
    供 build_signal_warnings 做回测前信号预检（零信号/线名错误/公式报错）。
    """
    if db is None:
        return {}
    stocks = list(klines.keys())
    if not stocks:
        return {}
    strategies = _portfolio_strategies(ps, db)
    tq_formula = TQFormula()
    cache: dict = {}
    if diagnostics is not None:
        # 两键始终初始化，消费方可直接索引（成功时 errors 为空 dict）
        diagnostics.setdefault("errors", {})
        diagnostics.setdefault("output_lines", {})

    # W-C 按 (formula_id, period) 分组：同组一次 compute（stocks=并集），
    # 转换/诊断按策略拆分——多策略共享公式+周期时把 N 次 TQ 往返降为 1 次
    # （formula_process_mul_zb 的 stock_list 批量与逐策略调用等价）。
    start_str, end_str = _kline_time_range(klines)
    groups: Dict[tuple, List[tuple]] = {}
    for strat in strategies:
        formula = db.get(Formula, strat.formula_id)
        if formula is None:
            continue
        period = strat.period
        # 策略级股票池：非空 → 仅本池股票跑公式；空 → 全部 klines 股票（继承组合级）
        if strat.stock_pool_id is not None:
            strat_stocks = [
                code for code in stocks
                if code in _pool_stock_set(db, strat.stock_pool_id)
            ]
        else:
            strat_stocks = list(stocks)
        if not strat_stocks:
            continue
        groups.setdefault((strat.formula_id, period), []).append(
            (strat, strat_stocks)
        )
    for (formula_id, period), members in groups.items():
        formula = db.get(Formula, formula_id)
        if formula is None:
            continue
        # 并集股票（保序去重）
        union: List[str] = []
        seen: set = set()
        for _, ss in members:
            for code in ss:
                if code not in seen:
                    seen.add(code)
                    union.append(code)
        raw = tq_formula.compute(
            formula_name=formula.name, formula_arg="",
            stocks=union, period=period,
            count=-1, dividend_type=_FORMULA_DIVIDEND_TYPE.get(dividend_mode, 1),
            start_time=start_str, end_time=end_str,
        )
        # 分钟级传 bar_times 按索引对齐；日线不传走 Date 匹配
        # 按策略周期过滤时间轴，避免多周期并集导致索引串位
        bt = _bar_times_by_period(klines, period) if period in _MINUTE_PERIODS else None
        for strat, strat_stocks in members:
            if diagnostics is not None:
                err_msg, lines = _formula_diagnostics(raw, strat_stocks)
                if err_msg:
                    diagnostics.setdefault("errors", {})[strat.id] = err_msg
                else:
                    diagnostics.setdefault("output_lines", {})[strat.id] = lines
            entries = _convert_formula_output(
                raw, strat.id, strat_stocks, bar_times_by_code=bt
            )
            cache.update(entries)
    if start is not None:
        start_ts = datetime(start.year, start.month, start.day)
        cache = {k: v for k, v in cache.items() if k[2] >= start_ts}
    return cache


# 信号类型 → 中文标签（预检警告文案用）
_SIGNAL_TYPE_LABELS = {"OPEN": "开仓", "ADD": "加仓", "REDUCE": "减仓", "CLOSE": "清仓"}


def build_signal_warnings(
    db: Session, strategies: list, signal_cache: dict, diagnostics: Optional[dict]
) -> List[str]:
    """回测信号预检：返回非致命警告列表（空列表=一切正常）。

    三类问题（公式能编译不代表信号有效，典型坑：30 分钟主图公式挂到日线策略上，
    ErrorId=0 但区间内零信号，回测静默 completed）：
    1. 公式执行报错（ErrorId 异常）——直接给根因，不再逐信号重复报；
    2. 配置的信号输出线名在公式真实输出中不存在（线名写错/通达信公式版本不同）；
    3. 输出线存在但回测区间内从未等于触发值（周期不匹配/触发值配错/市场无信号）。

    diagnostics 为空/缺键（build_signal_cache 被旁路，如旧调用方）时不产生警告。
    """
    if not diagnostics or not strategies:
        return []
    errors = diagnostics.get("errors") or {}
    output_lines = diagnostics.get("output_lines") or {}
    warnings: List[str] = []
    for strat in strategies:
        period = getattr(strat, "period", "?")
        if strat.id in errors:
            warnings.append(
                f"策略「{strat.name}」({period}) 公式执行失败：{errors[strat.id]}。"
                f"请检查通达信公式语法及其引用的副图公式是否存在。"
            )
            continue
        lines = output_lines.get(strat.id)
        if lines is None:
            # 该策略未产生诊断信息（非错误，可能被测试桩旁路）——跳过避免误报
            continue
        formula = db.get(Formula, strat.formula_id) if db is not None else None
        formula_name = formula.name if formula is not None else f"id={strat.formula_id}"
        sigs = (
            db.query(FormulaSignal).filter_by(formula_id=strat.formula_id).all()
            if db is not None else []
        )
        for sig in sigs:
            type_label = _SIGNAL_TYPE_LABELS.get(sig.signal_type, sig.signal_type)
            if sig.signal_name not in lines:
                warnings.append(
                    f"策略「{strat.name}」({period}) 的{type_label}信号"
                    f"「{sig.signal_name}」在公式「{formula_name}」的输出线中不存在。"
                    f"请检查信号名称是否与通达信公式输出线完全一致（当前输出线："
                    f"{'、'.join(sorted(lines)) or '无'}）。"
                )
                continue
            triggered = any(
                key[0] == strat.id
                and any(
                    o.get("name") == sig.signal_name
                    and o.get("value") == sig.trigger_value
                    for o in outs
                )
                for key, outs in signal_cache.items()
            )
            if not triggered:
                warnings.append(
                    f"策略「{strat.name}」({period}) 的{type_label}信号"
                    f"「{sig.signal_name}」（触发值={sig.trigger_value}）"
                    f"在回测区间内从未触发，本次回测不会产生该信号的交易。"
                    f"请检查策略周期是否与公式主图周期一致、触发值配置是否正确。"
                )
    return warnings


# --- 辅助：从 DB 读组装所需数据 ---
_POOL_STOCK_CACHE: dict = {}  # pool_id → set(stock_code)，进程级缓存避免回测中重复查询


def _pool_stock_set(db: Session, pool_id: int) -> set:
    """股票池成分集合（带进程级缓存，避免 build_signal_cache 中逐策略重复查询）。"""
    if pool_id in _POOL_STOCK_CACHE:
        return _POOL_STOCK_CACHE[pool_id]
    codes = {
        row.stock_code
        for row in db.query(StockPoolStock).filter_by(pool_id=pool_id).all()
    }
    _POOL_STOCK_CACHE[pool_id] = codes
    return codes


def _pool_stocks(ps, db: Session) -> list:
    """回测所需全部股票：组合级池 ∪ 各策略级独立池（去重保序）。

    策略 stock_pool_id 为空的继承组合级（已在组合级中），非空才并入并集。
    db 为 None 时返回空。
    """
    if db is None:
        return []
    pool_ids = {ps.stock_pool_id}
    for s in _portfolio_strategies(ps, db):
        if s.stock_pool_id is not None:
            pool_ids.add(s.stock_pool_id)
    if not pool_ids:
        return []
    rows = db.query(StockPoolStock).filter(
        StockPoolStock.pool_id.in_(pool_ids)
    ).all()
    seen: list = []
    for r in rows:
        if r.stock_code not in seen:
            seen.append(r.stock_code)
    return seen


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
    # ORDER BY id：策略处理顺序确定化（master/slave 同 bar 现金审批与 follow_master
    # 镜像依赖遍历顺序，未排序会被 SQLite 物理顺序抖动导致非确定性）。
    return db.query(Strategy).filter_by(portfolio_id=ps.id).order_by(Strategy.id).all()


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
def _dedup_daily_by_date(snaps: list) -> list:
    """bar 级快照 → 日级：每日只保留最后一根 bar（日收盘）。

    引擎快照按 30m 等周期逐 bar 生成（一天多行）。落库/计数统一按日去重，
    与 Evaluator._daily_rows / build_return_buckets 口径一致（否则前端
    净值曲线一天多个点、逐行算"日收益"会混入日内收益）。快照按时间有序，
    同日覆盖即可取到最后一根 bar，返回结果保持日期升序。
    """
    by_date: dict = {}
    for snap in snaps:
        by_date[snap["snap_date"]] = snap
    return list(by_date.values())


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
    for snap in _dedup_daily_by_date(result["snapshots"]):
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
        for snap in _dedup_daily_by_date(snaps):
            db.add(BacktestDailySnapshot(
                backtest_record_id=record_id,
                target_type="strategy",
                target_id=sid,
                snap_date=snap["snap_date"],
                total_value=snap["total_value"],
                cash=snap.get("cash", Decimal("0")),
                market_value=snap.get("market_value", Decimal("0")),
                # 引擎已注入与组合同日的基准值（FR-1 策略层相对指标数据源）
                benchmark_value=snap.get("benchmark_value"),
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
        "error_message": r.error_message,
        "warnings": json.loads(r.warnings_json) if r.warnings_json else [],
        # B6 考核口径（历史记录无该列值时回落 absolute）
        "evaluation_profile": getattr(r, "evaluation_profile", None) or "absolute",
        # C7 IS/OOS 门槛（默认关；gate_status None=未启用/未评估）
        "is_oos_gate_enabled": bool(getattr(r, "is_oos_gate_enabled", False)),
        "oos_gate_min_ratio": _f(getattr(r, "oos_gate_min_ratio", None)),
        "gate_status": getattr(r, "gate_status", None),
        "gate_reasons": (
            json.loads(r.gate_reasons) if getattr(r, "gate_reasons", None) else []
        ),
        "validation": (
            json.loads(r.validation_json) if getattr(r, "validation_json", None) else None
        ),
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
        "created_at": d.created_at.isoformat() if d.created_at else None,
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
        "closed_trades": e.closed_trades,
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
        "max_consecutive_profit_days": e.max_consecutive_profit_days,
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
    """列出全部回测记录（按创建时间倒序），序列化为 dict 列表。
    C8：过滤 walk-forward 窗口子回测（walk_forward_run_id 非空），
    P2b：过滤参数微扰子回测（param_perturb_run_id 非空），
    子记录仅在各自 run 详情内可见；普通回测（NULL）行为不变。"""
    recs = (
        db.query(BacktestRecord)
        .filter(BacktestRecord.walk_forward_run_id.is_(None))
        .filter(BacktestRecord.param_perturb_run_id.is_(None))
        .order_by(BacktestRecord.created_at.desc())
        .all()
    )
    return [_serialize_record(r) for r in recs]


def get_record_detail(
    db: Session, record_id: int, profile_override: Optional[str] = None,
) -> Optional[dict]:
    """回测记录详情：record + snapshots + trades + evaluations + 策略层。
    不存在返回 None（路由层据此 err(404)）。
    profile_override：B6 验收口径临时切换（?profile=relative），仅影响
    acceptance 块，不改记录存储的 evaluation_profile。"""
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
    # C9 年度归因：按年聚合风控触发率与槽位拦截量（纯展示块，不改任何行为）
    decision_yearly = summarize_decisions_yearly(decision_rows)

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

    mr = (
        db.query(BacktestMarketRegimeAnalysis)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    market_regime = None
    if mr is not None:
        market_regime = {
            "status": mr.status,
            "error": mr.error,
            "computed_at": mr.computed_at.isoformat() if mr.computed_at else None,
            "skipped_benchmarks": (
                json.loads(mr.skipped_benchmarks_json)
                if mr.skipped_benchmarks_json else []
            ),
            "benchmark_code": mr.benchmark_code,
            "benchmark_name": mr.benchmark_name,
            "data": json.loads(mr.payload_json) if mr.payload_json else None,
        }

    rb = (
        db.query(BacktestRobustnessAnalysis)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    robustness = None
    if rb is not None:
        robustness = {
            "status": rb.status,
            "error": rb.error,
            "computed_at": rb.computed_at.isoformat() if rb.computed_at else None,
            "data": json.loads(rb.payload_json) if rb.payload_json else None,
        }

    # ---- B6 考核口径验收（默认 absolute；?profile= 可临时切换查看另一口径）----
    evals_dict = _serialize_evaluation(evals) if evals else None
    profile = profile_override or rec.evaluation_profile or "absolute"
    if profile not in ACCEPTANCE_PROFILES:
        profile = "absolute"
    acceptance = compute_acceptance(evals_dict, profile)

    return {
        "record": _serialize_record(rec),
        "snapshots": [_serialize_snapshot(s) for s in snaps],
        "trades": [_serialize_trade_with_name(t, strat_name_map) for t in trades],
        "evaluations": evals_dict,
        "strategy_evaluations": strategy_evaluations,
        "strategy_snapshots": strategy_snapshots,
        "decision_summary": decision_summary,
        "decision_yearly": decision_yearly,
        "decisions": [_serialize_decision(d) for d in decision_rows],
        "returns_buckets": returns_buckets,
        "crisis_alpha": crisis_alpha,
        "market_regime": market_regime,
        "robustness": robustness,
        "acceptance": acceptance,
    }


def delete_record(db: Session, record_id: int) -> Tuple[bool, Optional[str]]:
    """删除回测记录 + 级联子表（trades/snapshots/evaluations/decisions）。
    子表 FK 虽配 ondelete=CASCADE，但显式删更稳妥（不依赖连接级 PRAGMA）。

    返回 (ok, error_message)：
    - 不存在 → (False, "回测记录不存在")，路由据此 404
    - status=running → (False, "回测运行中，请先停止再删除")，路由据此 409
      （#4: 回测同步内联执行，运行中即代表另一请求正在跑回测；并发删除会引发
      FK IntegrityError 500 或破坏其写入——对照 live_service.delete_session 守卫）
    - 删除成功 → (True, None)
    """
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return False, "回测记录不存在"
    if rec.status == "running":
        return False, "回测运行中，请先停止再删除"
    db.query(BacktestTrade).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestDailySnapshot).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestEvaluation).filter_by(backtest_record_id=record_id).delete()
    db.query(BacktestDecisionEvent).filter_by(backtest_record_id=record_id).delete()
    db.delete(rec)
    db.commit()
    return True, None


def recover_stale_running_backtests(db: Session) -> int:
    """#5: 应用启动时清理僵尸 running 回测记录。

    回测是同步内联执行，正常运行时 status=running 只在请求线程内短暂存在，
    进程正常退出时不可能残留 running。残留即意味着上次进程崩溃/被强杀——
    前端会永久显示"运行中"且无法重新发起回测（_BACKTEST_LOCK 是进程内锁，
    重启后已释放，但 DB 状态仍是 running 误导用户）。

    扫描所有 status=running 的记录，统一标记 failed 并附 error_message 说明，
    让用户识别后重新发起。返回受影响记录数。
    """
    stale = db.query(BacktestRecord).filter_by(status="running").all()
    if not stale:
        return 0
    msg = "应用启动时检测到中断状态（进程崩溃/重启），已自动标记为失败"
    for rec in stale:
        rec.status = "failed"
        rec.error_message = msg
    db.commit()
    return len(stale)


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


# 容量预警阈值：单笔成交 ≥ 该股日均成交额的 30% → 提示不可成交风险（默认无限
# 流动性假设下，实盘按回测价成交不现实）；最大单票权重 > 25% → 集中度/权重膨胀。
_CAP_PARTICIPATION_WARN = 0.30
_CAP_MAX_WEIGHT_WARN = 0.25


def _fmt_yuan(v: float) -> str:
    """金额人读格式化：按量级选单位（亿/万/元），避免小金额被 /1e8 显示成
    '0.00亿' 无法分辨来源（如单笔 20 万 + 日均成交额 1.4 万 → 1420%）。"""
    if v >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f} 万"
    return f"{v:,.0f} 元"

# 危机期窗口（历史锁定、未来定义标准）：固定常量，非按策略表现回填，
# 保证"危机防御判定"的标准先于结果确定（无前视）。key 用于稳定标识。
CRISIS_WINDOWS: tuple = (
    {"key": "2015_crash", "name": "2015 股灾", "start": "2015-06-01", "end": "2015-09-30"},
    {"key": "2016_circuit_breaker", "name": "2016 熔断", "start": "2016-01-01", "end": "2016-02-29"},
    {"key": "2018_tradewar", "name": "2018 贸易战/去杠杆", "start": "2018-01-01", "end": "2018-12-31"},
    {"key": "2020_covid", "name": "2020 疫情", "start": "2020-01-01", "end": "2020-04-30"},
    {"key": "2024_liquidity", "name": "2023底-24初流动性危机", "start": "2023-11-01", "end": "2024-02-29"},
    {"key": "2025_tradewar", "name": "2025 贸易战", "start": "2025-04-01", "end": "2025-07-31"},
    {"key": "2026_techcrash", "name": "2026-07 科技崩盘", "start": "2026-07-01", "end": "2026-07-31"},
)

# 危机防御判定：危机期正收益/持平 → 防御；负收益但最低现金占比 ≥70%（高现金避险）→ 防御。
_CRISIS_DEFENSIVE_CASH = 0.70


def build_crisis_verification(snapshots: list) -> list:
    """危机防御机制自动验证：对固定危机窗口逐窗计算
    策略收益/基准收益/窗口最大回撤/最低现金占比 → 防御✓ / 暴露✗ 判定。

    snapshots：回测组合日快照（snap_date/total_value/cash/benchmark_value）。
    窗口未覆盖（回测区间未触及）→ 跳过；数据不足 → verdict=n/a。
    """
    if not snapshots:
        return []
    by_date = {}
    for s in snapshots:
        d = s.get("snap_date")
        if d is not None:
            by_date[d] = s
    sorted_dates = sorted(by_date)
    if not sorted_dates:
        return []
    result: list = []
    for w in CRISIS_WINDOWS:
        try:
            w_start = date.fromisoformat(w["start"])
            w_end = date.fromisoformat(w["end"])
        except ValueError:
            continue
        in_window = [d for d in sorted_dates if w_start <= d <= w_end]
        if not in_window:
            continue
        # 基准：窗口前一日 last value，否则窗口首日
        prior = [d for d in sorted_dates if d < w_start]
        base_day = prior[-1] if prior else in_window[0]
        base_total = float(by_date[base_day]["total_value"])
        end_total = float(by_date[in_window[-1]]["total_value"])
        strategy_return = (end_total / base_total - 1.0) if base_total else None
        # 基准收益
        bench_vals = []
        for d in in_window:
            bv = by_date[d].get("benchmark_value")
            if bv is not None:
                bench_vals.append(float(bv))
        if len(bench_vals) >= 2:
            benchmark_return = bench_vals[-1] / bench_vals[0] - 1.0
        else:
            benchmark_return = None
        # 窗口内最大回撤（total_value 峰谷）
        peak = None
        max_dd = None
        for d in in_window:
            v = float(by_date[d]["total_value"])
            if peak is None or v > peak:
                peak = v
            if peak:
                dd = (peak - v) / peak
                if max_dd is None or dd > max_dd:
                    max_dd = dd
        # 最低现金占比
        cash_ratios = []
        for d in in_window:
            cash = by_date[d].get("cash")
            tv = by_date[d].get("total_value")
            if cash is not None and tv and float(tv) > 0:
                cash_ratios.append(float(cash) / float(tv))
        min_cash_ratio = min(cash_ratios) if cash_ratios else None
        # 判定
        if strategy_return is None:
            verdict, reason = "n/a", "窗口内净值数据不足"
        elif strategy_return >= 0:
            verdict, reason = "defensive", "危机期仍取得正收益/持平"
        elif min_cash_ratio is not None and min_cash_ratio >= _CRISIS_DEFENSIVE_CASH:
            verdict, reason = "defensive", f"高现金避险（最低现金占比 {min_cash_ratio:.0%}）"
        else:
            verdict, reason = "exposed", f"危机期受损且现金占比低（最低 {min_cash_ratio:.0%}）"
        result.append({
            "key": w["key"], "name": w["name"],
            "start": w["start"], "end": w["end"],
            "strategy_return": strategy_return,
            "benchmark_return": benchmark_return,
            "max_drawdown": max_dd,
            "min_cash_ratio": min_cash_ratio,
            "verdict": verdict, "reason": reason,
        })
    return result


def build_validation_verdict(result: dict, klines: dict, ps, db: Session) -> dict:
    """回测"验证结论"聚合：容量健康 + 危机防御 + 样本外引用，随结果冻结落库。

    - capacity：build_capacity_warnings 的告警列表（空=健康）；
    - crisis：固定窗口危机防御验证（build_crisis_verification）；
    - oos：该组合最近的 walk-forward 运行摘要（None=未做过样本外验证）。
    """
    snapshots = result.get("snapshots") or []
    trades = result.get("trades") or []
    warnings = build_capacity_warnings(
        trades, klines,
        result.get("evaluations") or {},
        getattr(ps, "position_sizing", "dynamic"),
        getattr(ps, "equity_cap", None),
    )
    crisis = build_crisis_verification(snapshots)
    oos: Optional[dict] = None
    if db is not None:
        wf = (
            db.query(WalkForwardRun)
            .filter(WalkForwardRun.portfolio_strategy_id == ps.id)
            .order_by(WalkForwardRun.id.desc())
            .first()
        )
        if wf is not None:
            oos = {
                "run_id": wf.id, "name": wf.name,
                "completed_at": wf.completed_at,
                "windows_passed": (json.loads(wf.summary_json or "{}")
                                   .get("windows_passed")),
                "overall_pass": (json.loads(wf.summary_json or "{}")
                                 .get("overall_pass")),
            }
    return {
        "capacity": warnings,
        "crisis": crisis,
        "oos": oos,
    }


def build_capacity_warnings(
    trades: list,
    klines: dict,
    evaluations: Optional[dict] = None,
    position_sizing: str = "dynamic",
    equity_cap=None,
) -> list:
    """容量健康度告警（自动）：即使未设 equity_cap，也在结果里明确提示
    "该结果是否含不可实现仓位"。

    - 单笔成交 vs 该股日均成交额（ADV = mean(Volume_手×100×Close)，从 klines 估算）：
      最大占比 ≥ 30% → 该笔实盘按回测价成交不现实；
    - 最大单票权重 max_single_weight > 25% → 集中度/权重随涨幅过度膨胀；
    - dynamic 且未设 equity_cap 时命中任一 → 附设置容量上限/成交量约束的建议。

    仅在有成交且可估算 ADV 时生效；无数据静默跳过（不误报）。
    """
    if not trades:
        return []
    warns: list = []
    # 1. 每股日均成交额（元）。TQ 单位已实测确认：Volume=股、Amount=万元。
    #    故优先用 量(股)×价(元) 直接得元口径；Amount(万元)×10000 兜底。
    adv_by_code: Dict[str, float] = {}
    for code, periods in (klines or {}).items():
        df = periods.get("1d") if isinstance(periods, dict) else None
        if df is None:
            continue
        try:
            cols = df.columns
            amounts = df["Amount"].to_list() if "Amount" in cols else None
            vols = df["Volume"].to_list() if "Volume" in cols else None
            closes = df["Close"].to_list() if "Close" in cols else None
        except Exception:
            continue
        day_amts = []
        if vols is not None and closes is not None:
            day_amts = [
                float(v) * float(c)
                for v, c in zip(vols, closes)
                if v is not None and c is not None and float(v) > 0 and float(c) > 0
            ]
        if not day_amts and amounts is not None:
            day_amts = [float(a) * 10000.0 for a in amounts
                        if a is not None and float(a) > 0]
        if day_amts:
            adv_by_code[code] = sum(day_amts) / len(day_amts)
    # 2. 最大单笔成交 vs ADV 占比
    worst = None  # (amount, code, adv, ratio)
    for t in trades:
        code = getattr(t, "stock_code", None)
        adv = adv_by_code.get(code)
        if not adv or adv <= 0:
            continue
        amt = float(getattr(t, "amount", 0) or 0)
        if amt <= 0:
            continue
        ratio = amt / adv
        if worst is None or ratio > worst[3]:
            worst = (amt, code, adv, ratio)
    if worst is not None and worst[3] >= _CAP_PARTICIPATION_WARN:
        amt, code, adv, ratio = worst
        warns.append(
            f"容量预警：最大单笔成交 {_fmt_yuan(amt)}（{code}）≈ 该股日均成交额 "
            f"{_fmt_yuan(adv)}的 {ratio:.0%}（≥30%）。回测按无限流动性成交，"
            f"实盘按此价成交不现实，该结果可能含不可实现仓位"
        )
    # 3. 最大单票权重
    msw = (evaluations or {}).get("max_single_weight")
    if msw is not None and float(msw) > _CAP_MAX_WEIGHT_WARN:
        warns.append(
            f"集中度预警：最大单票权重 {float(msw):.1%} 超过 {_CAP_MAX_WEIGHT_WARN:.0%}，"
            f"组合过度集中于单只股票（仓位随涨幅膨胀）"
        )
    # 4. 修复建议
    if warns and position_sizing == "dynamic" and not equity_cap:
        warns.append(
            "容量建议：为组合设置 equity_cap（容量上限，仓位封顶）或开启 "
            "volume_limit 成交量约束，让仓位落在真实可成交规模；否则本结果仅作上界参考"
        )
    return warns


def run_backtest(db: Session, ps: PortfolioStrategy, req,
                 param_overrides: Optional[dict] = None) -> dict:
    """回测主链路（从路由 _run_backtest_locked 迁入）。

    前置：路由层已保证 ps 非 None、日期合法、已持并发锁。
    内部：写 record(running) → assemble → build_klines → build_signal_cache →
          build_benchmark_data → 空行情保护 → engine.run →
          _persist_result → 标 completed → 返回 {record_id, trades_count,
          snapshots_count, evaluations}。
    异常：标 record(failed) + re-raise（路由层全局异常处理器兜底转 {code:500}，
          与现状一致——原 _run_backtest_locked 也是 raise 出去）。

    param_overrides（P2b 参数微扰，可选，默认 None 行为不变）：
    {子策略数值字段: 乘性因子}，各子策略按自身基准值缩放（int 字段按因子
    方向 floor/ceil 且 ≥1）。先 expunge 脱管再改值——db.commit() 不会把
    扰动值 flush 回 strategies 表，扰动仅作用于本轮内存装配。

    返回纯 dict（不经 ok() 包装，由路由层统一 ok()）。
    """
    strategies = (
        db.query(Strategy)
        .filter_by(portfolio_id=ps.id)
        .order_by(Strategy.id)
        .all()
    )
    if param_overrides:
        for s in strategies:
            db.expunge(s)
            for field, factor in param_overrides.items():
                base = getattr(s, field, None)
                if base is None:
                    continue
                if isinstance(base, bool):
                    continue
                if isinstance(base, int):
                    scaled = base * factor
                    v = math.floor(scaled) if factor < 1 else math.ceil(scaled)
                    setattr(s, field, max(1, int(v)))
                elif isinstance(base, Decimal):
                    setattr(s, field, clamp_perturbed_value(field, base * Decimal(str(factor))))
                else:
                    setattr(s, field, clamp_perturbed_value(field, base * factor))

    # 复权模式：读请求参数，非法值回退前复权（保持历史行为 + 防脏数据）
    dividend_mode = getattr(req, "dividend_mode", "front") or "front"
    if dividend_mode not in DIVIDEND_MODES:
        dividend_mode = "front"
    # dual：公式信号走前复权（与通达信口径一致），引擎执行/记账走后复权（真实价、跨除权正确）
    if dividend_mode == "dual":
        accounting_mode, signal_mode = "back", "front"
    else:
        accounting_mode = signal_mode = dividend_mode

    # B6 考核口径：读请求参数，非法值回退 absolute（保持默认行为 + 防脏数据）
    evaluation_profile = getattr(req, "evaluation_profile", "absolute") or "absolute"
    if evaluation_profile not in ACCEPTANCE_PROFILES:
        evaluation_profile = "absolute"

    # C7 IS/OOS 采纳门槛（默认关）：读请求参数，非法 ratio 回退 0.5
    is_oos_gate_enabled = bool(getattr(req, "is_oos_gate_enabled", False))
    try:
        oos_gate_min_ratio = float(getattr(req, "oos_gate_min_ratio", 0.5) or 0.5)
    except (TypeError, ValueError):
        oos_gate_min_ratio = 0.5
    # 费用预留（run 级可选，默认关）：买入审批含佣金防现金为负
    fee_reserve_enabled = bool(getattr(req, "fee_reserve_enabled", False))
    # 可转债 run 级开关（默认关，仅回测生效）：T+0 回转 / 10 张取整
    bond_t0_enabled = bool(getattr(req, "bond_t0_enabled", False))
    bond_lot_enabled = bool(getattr(req, "bond_lot_enabled", False))
    # 自适应滑点 run 级覆盖（None=用组合默认；显式 bool/coef 临时覆盖不改组合）
    adaptive_slippage_enabled = getattr(req, "adaptive_slippage_enabled", None)
    slippage_impact_coef = getattr(req, "slippage_impact_coef", None)
    # 成交量约束 run 级覆盖（None=用组合默认）
    volume_limit_enabled = getattr(req, "volume_limit_enabled", None)
    volume_limit_ratio = getattr(req, "volume_limit_ratio", None)
    # 容量上限 run 级覆盖（None=用组合默认）
    equity_cap = getattr(req, "equity_cap", None)
    # 快照用的生效值：组合默认 or run 级覆盖（fee_reserve 仅 run 级）
    eff_adaptive = (
        bool(adaptive_slippage_enabled)
        if adaptive_slippage_enabled is not None
        else bool(getattr(ps, "adaptive_slippage_enabled", False))
    )
    eff_impact_coef = (
        slippage_impact_coef
        if slippage_impact_coef is not None
        else getattr(ps, "slippage_impact_coef", None)
    )

    # 写 record（running）
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        status="running",
        params_snapshot=build_params_snapshot(
            ps, dividend_mode=dividend_mode,
            fee_reserve_enabled=fee_reserve_enabled,
            bond_t0_enabled=bond_t0_enabled,
            bond_lot_enabled=bond_lot_enabled,
            adaptive_slippage_enabled=eff_adaptive,
            slippage_impact_coef=eff_impact_coef,
            volume_limit_enabled=volume_limit_enabled,
            volume_limit_ratio=volume_limit_ratio,
            equity_cap=equity_cap,
        ),
        evaluation_profile=evaluation_profile,
        is_oos_gate_enabled=is_oos_gate_enabled,
        oos_gate_min_ratio=oos_gate_min_ratio,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    record_id = rec.id

    try:
        t0 = datetime.now()
        portfolio = _assemble_portfolio(ps, strategies, db)
        # run 级覆盖装配：费用预留 + 可转债开关 + 自适应滑点 + 成交量约束 + 容量上限（不改组合表，仅本轮内存生效）
        portfolio.fee_reserve_enabled = fee_reserve_enabled
        portfolio.bond_t0_enabled = bond_t0_enabled
        portfolio.bond_lot_enabled = bond_lot_enabled
        if adaptive_slippage_enabled is not None:
            portfolio.adaptive_slippage_enabled = bool(adaptive_slippage_enabled)
        if slippage_impact_coef is not None:
            portfolio.slippage_impact_coef = Decimal(str(slippage_impact_coef))
        if volume_limit_enabled is not None:
            portfolio.volume_limit_enabled = bool(volume_limit_enabled)
        if volume_limit_ratio is not None:
            portfolio.volume_limit_ratio = Decimal(str(volume_limit_ratio))
        if equity_cap is not None:
            portfolio.equity_cap = Decimal(str(equity_cap))
        t1 = datetime.now()
        # klines 含公式预热段（早于 start，保证首根 bar 公式有效值）
        klines_full = build_klines(
            ps, req.start_date, req.end_date, db, dividend_mode=accounting_mode,
        )
        t2 = datetime.now()
        # 数据量摘要：各周期多少根 bar、多少只股票，帮助判断瓶颈
        _log_kline_summary(record_id, klines_full)
        # 信号计算喂全量（含预热段，分钟级索引对齐与喂入数据同源），start 起的条目才进 cache
        # diagnostics 收集公式执行错误与真实输出线名，供信号预检（零信号静默坑）
        signal_diagnostics: dict = {}
        signal_cache = build_signal_cache(
            ps, klines_full, db, start=req.start_date, diagnostics=signal_diagnostics,
            dividend_mode=signal_mode,
        )
        signal_warnings = build_signal_warnings(
            db, strategies, signal_cache, signal_diagnostics,
        )
        # 数据告警：个别股票行情取不到（通达信返回非法日期被逐股兜底跳过）→
        # 该股票不会出现在 klines_full 中。非致命，回测仍 completed 并落库告警。
        pool_codes = _pool_stocks(ps, db)
        skipped_stocks = [c for c in pool_codes if c not in klines_full]
        data_warnings: List[str] = []
        if skipped_stocks:
            shown = "、".join(skipped_stocks[:20])
            more = f" 等 {len(skipped_stocks)} 只" if len(skipped_stocks) > 20 else ""
            data_warnings.append(
                f"股票池中 {len(skipped_stocks)} 只股票行情数据异常或缺失，已跳过："
                f"{shown}{more}。请在通达信中补齐数据后重跑。"
            )
        warnings = data_warnings + signal_warnings
        t3 = datetime.now()
        # 引擎视野切回主区间：预热段不进时间轴/成交/快照/熔断
        klines = _slice_klines_from(klines_full, req.start_date)
        # W-B 基准取数去重：任一策略启用 regime_filter_enabled 时，本就会拉取
        # [start-620d, end] 全区间基准收盘（regime_closes）；benchmark_data 直接
        # 从其中切片主区间，不再单独调 build_benchmark_data（同一指数、区间重叠，
        # 避免二次 TQ 往返）。未启用 regime 维持原 build_benchmark_data。
        regime_enabled = any(
            getattr(s, "regime_filter_enabled", False) for s in strategies
        )
        benchmark_code = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
        regime_closes: Dict[date, Decimal] = {}
        if regime_enabled:
            regime_closes = build_index_close_series(
                [benchmark_code],
                req.start_date - timedelta(days=620),
                req.end_date,
                dividend_mode=accounting_mode,
            ).get(benchmark_code, {})
            benchmark_data = {
                d: c for d, c in regime_closes.items()
                if req.start_date <= d <= req.end_date
            }
        else:
            benchmark_data = build_benchmark_data(
                ps, req.start_date, req.end_date, db, dividend_mode=accounting_mode,
            )
        t4 = datetime.now()

        # 各阶段耗时日志（定位回测慢的根因）
        _log_timing(f"[回测#{record_id}] 组装组合: {(t1-t0).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ K线拉取: {(t2-t1).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 公式计算: {(t3-t2).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] TQ 基准指数: {(t4-t3).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 数据准备合计: {(t4-t0).total_seconds():.1f}s")

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
        # B5 市场状态闸（默认关）：任一策略开启 regime_filter_enabled 时构建因果
        # 状态跟踪器。regime_closes 已在上面（W-B 基准去重处）一次拉取 [start-620d, end]
        # 全区间基准收盘；这里只用 start 前的历史预喂（区间内由引擎逐 bar 喂入，
        # 纯因果，禁未来函数）。未开启传 None。
        regime_tracker = None
        if regime_enabled:
            regime_tracker = CausalRegimeTracker()
            regime_tracker.feed_history(
                {d: c for d, c in regime_closes.items() if d < req.start_date}
            )
        engine = BacktestEngine()
        result = engine.run(
            portfolio,
            klines=klines,
            signal_cache=signal_cache,
            benchmark_data=benchmark_data,
            regime_tracker=regime_tracker,
        )
        t7 = datetime.now()
        _log_timing(f"[回测#{record_id}] 引擎逐bar回测: {(t7-t6).total_seconds():.1f}s")

        # W5-O5：样本不足一年（< 252 交易日）→ 告警年化收益仅供参考
        n_trading_days = len(_dedup_daily_by_date(result.get("snapshots") or []))
        if n_trading_days < 252:
            warnings.append(
                f"回测区间仅 {n_trading_days} 个交易日（< 252），"
                f"年化收益基于不足一年样本，仅供参考"
            )

        # 容量健康度自动预警：即使未设 equity_cap，命中"单笔>30%日均成交额 /
        # 单票权重>25%"时，在结果里明确提示"该结果含不可实现仓位"并附修复建议。
        warnings.extend(build_capacity_warnings(
            result.get("trades") or [],
            klines,
            result.get("evaluations") or {},
            getattr(ps, "position_sizing", "dynamic"),
            getattr(ps, "equity_cap", None),
        ))

        _persist_result(db, record_id, ps.id, result, strategies)
        t8 = datetime.now()
        _log_timing(f"[回测#{record_id}] 结果持久化: {(t8-t7).total_seconds():.1f}s")
        _log_timing(f"[回测#{record_id}] 总耗时: {(t8-t0).total_seconds():.1f}s")

        rec.status = "completed"
        # 告警：行情跳过 + 信号预检（公式报错/输出线缺失/区间零信号）：非致命，回测仍 completed
        rec.warnings_json = (
            json.dumps(warnings, ensure_ascii=False) if warnings else None
        )
        # 验证结论冻结：容量健康 + 危机防御 + 样本外引用（随记录审计）
        rec.validation_json = json.dumps(
            build_validation_verdict(result, klines, ps, db),
            ensure_ascii=False, default=str,
        )
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
    rb_row = safe_compute_robustness(db, rec.id)

    # 机遇/中性Alpha（市场状态×绩效）随回测同时计算（与危机Alpha 同点，非手动重算）：
    # 用已落库成交（_persist_result 已写入 backtest_trades）走与「离线重算」完全一致的
    # 路径；失败由 safe_ 包装标 failed，不阻断回测主链路。
    trades_orm = (
        db.query(BacktestTrade)
        .filter_by(backtest_record_id=rec.id)
        .order_by(BacktestTrade.bar_time, BacktestTrade.id)
        .all()
    )
    safe_compute_market_regime(
        db, rec.id, ps, result["snapshots"], trades_orm,
        req.start_date, req.end_date,
    )

    # C7 IS/OOS 采纳门槛（默认关）：仅启用时按稳健性 oos 块评估并落库；
    # rejected 只标记不删记录（审计友好）；数据不足 gate_status=None。
    gate = None
    if rec.is_oos_gate_enabled:
        payload = json.loads(rb_row.payload_json) if rb_row and rb_row.payload_json else None
        verdict = evaluate_oos_gate(
            (payload or {}).get("oos"), float(rec.oos_gate_min_ratio or 0.5),
        )
        rec.gate_status = verdict["status"]
        rec.gate_reasons = (
            json.dumps(verdict["reasons"], ensure_ascii=False)
            if verdict["reasons"] else None
        )
        db.commit()
        gate = {
            "status": verdict["status"],
            "reasons": verdict["reasons"],
            "min_ratio": float(rec.oos_gate_min_ratio),
        }

    return {
        "record_id": record_id,
        "trades_count": len(result["trades"]),
        "snapshots_count": len(_dedup_daily_by_date(result["snapshots"])),
        "evaluations": result.get("evaluations") or {},
        "warnings": warnings,
        "gate": gate,
    }
