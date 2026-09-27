"""轻量稳健性分析（FR-20 / AC-8）——纯 pandas 计算模块。

只消费已落库的每日净值快照与基准日线序列，**禁止触发回测重跑**
（service 层从快照与 TQ 基准取数后调用本模块，本模块不 import 回测链路）。

口径固化（阈值为命名常量，同输入必须同输出）：
- 对齐：策略净值与基准取交易日交集后各自排序。
- 逐半年矩阵：自然年 H1(1~6 月)/H2(7~12 月)；区间收益=末值/初值−1，
  超额=策略区间收益−基准区间收益（算术差），最大回撤取区间内峰谷回撤（正值），
  夏普=日收益均值/样本标准差×√252（不足 2 个日收益则 None）。
- 周频：按 W-FRI 取周末净值；滚动窗口收益=窗口内周收益几何累乘；
  滚动夏普=窗口周收益均值/样本标准差×√52；分布统计中位/最差/最好/负值占比；
  直方图固定 8 档边界。样本不足整窗时统计为 None、直方图全 0。
- 最长亏损窗口：净值低于历史峰值（水下）的最长连续区间——起点为峰值日，
  谷底为区间最低净值日，终点为收复日（末日仍未收复则取末日）；
  days 为峰值到终点的自然日数，drawdown 为正的谷底回撤幅度。
- 基准替换敏感性：FR-1 八项相对指标逐基独立对齐计算，口径与
  Evaluator._compute_relative_metrics 一致；对齐日收益 < 20 → available=false。
"""

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252
WEEKS_PER_YEAR = 52
RISK_FREE_RATE = 0.02
MIN_ALIGNED_DAYS = 30        # 与市场状态分析一致的共同交易日下限
MIN_REL_SAMPLE_DAYS = 20     # FR-1 相对指标最小对齐日收益样本
ROLLING_WINDOWS = (26, 52)   # 滚动窗口（周）

# 滚动窗口收益直方图固定分箱（右闭：首档 ≤-20%，末档 >20%）
HIST_EDGES = (-math.inf, -0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20, math.inf)
HIST_LABELS = (
    "≤-20%", "-20%~-10%", "-10%~-5%", "-5%~0%",
    "0%~5%", "5%~10%", "10%~20%", ">20%",
)

_REL_KEYS = (
    "beta", "jensen_alpha", "excess_annual_return", "tracking_error",
    "information_ratio", "up_capture_ratio", "down_capture_ratio",
    "excess_max_drawdown",
)


def _r4(x) -> Optional[float]:
    """转 float 并保留 4 位小数；NaN/Inf/None 一律归一为 None（JSON 安全）。"""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(xf) or math.isinf(xf):
        return None
    return round(xf, 4)


def _as_series(s: pd.Series) -> pd.Series:
    out = pd.Series(s.values, index=pd.DatetimeIndex(s.index), dtype=float)
    return out.sort_index()


def _aligned(nav: pd.Series, bench: pd.Series):
    idx = nav.index.intersection(bench.index)
    return nav.loc[idx].sort_index(), bench.loc[idx].sort_index()


def _half_year_matrix(s: pd.Series, b: pd.Series) -> List[dict]:
    labels = s.index.map(lambda d: f"{d.year}H{1 if d.month <= 6 else 2}")
    rows: List[dict] = []
    for label in sorted(set(labels)):
        ss = s[labels == label]
        bb = b[labels == label]
        if len(ss) < 2:
            continue
        daily = ss.pct_change().dropna()
        std = daily.std(ddof=1)
        sharpe = (
            daily.mean() / std * math.sqrt(TRADING_DAYS)
            if std is not None and std > 0 else None
        )
        sr = ss.iloc[-1] / ss.iloc[0] - 1
        br = bb.iloc[-1] / bb.iloc[0] - 1
        rows.append({
            "period": label,
            "start_date": ss.index[0].date().isoformat(),
            "end_date": ss.index[-1].date().isoformat(),
            "strategy_return": _r4(sr),
            "benchmark_return": _r4(br),
            "excess_return": _r4(sr - br),
            "max_drawdown": _r4(-(ss / ss.cummax() - 1).min()),
            "sharpe": _r4(sharpe),
        })
    return rows


def _dist(vals: pd.Series) -> dict:
    vals = vals.dropna()
    if len(vals) == 0:
        return {"median": None, "min": None, "max": None, "negative_ratio": None}
    return {
        "median": _r4(vals.median()),
        "min": _r4(vals.min()),
        "max": _r4(vals.max()),
        "negative_ratio": _r4((vals < 0).mean()),
    }


def _histogram(vals: pd.Series) -> List[dict]:
    if len(vals.dropna()) == 0:
        return [{"label": lab, "count": 0} for lab in HIST_LABELS]
    cats = pd.cut(vals.dropna(), bins=HIST_EDGES, labels=HIST_LABELS, right=True)
    counts = cats.value_counts(sort=False)
    return [{"label": lab, "count": int(counts.get(lab, 0))}
            for lab in HIST_LABELS]


def _rolling_windows(weekly_nav: pd.Series) -> List[dict]:
    wr = weekly_nav.pct_change().dropna()
    sqrt52 = math.sqrt(WEEKS_PER_YEAR)
    out: List[dict] = []
    for w in ROLLING_WINDOWS:
        if len(wr) < w:
            out.append({
                "window": w,
                "n_samples": 0,
                "returns": _dist(pd.Series(dtype=float)),
                "sharpe": _dist(pd.Series(dtype=float)),
                "histogram": _histogram(pd.Series(dtype=float)),
            })
            continue
        cum = (1 + wr).rolling(w).apply(np.prod, raw=True) - 1
        cum_vals = cum.dropna()

        def _win_sharpe(x):
            sd = x.std(ddof=1)
            return x.mean() / sd * sqrt52 if sd > 0 else np.nan

        shp = wr.rolling(w).apply(_win_sharpe, raw=True).dropna()
        out.append({
            "window": w,
            "n_samples": int(len(cum_vals)),
            "returns": _dist(cum_vals),
            "sharpe": _dist(shp),
            "histogram": _histogram(cum_vals),
        })
    return out


def _longest_loss_window(s: pd.Series) -> dict:
    dd = s / s.cummax() - 1
    water = dd < -1e-12
    empty = {
        "start_date": None, "trough_date": None, "end_date": None,
        "days": 0, "drawdown": None,
    }
    if not water.any():
        return empty

    run_id = (water != water.shift().fillna(False)).cumsum()
    best = None
    for gid in run_id[water].unique():
        seg_dates = run_id[(water) & (run_id == gid)].index
        first_pos = s.index.get_loc(seg_dates[0])
        peak_date = s.index[first_pos - 1] if first_pos > 0 else seg_dates[0]
        last_pos = s.index.get_loc(seg_dates[-1])
        # 收复日：水下去间结束后的首个交易日；末日仍未收复则取末日
        end_date = (
            s.index[last_pos + 1] if last_pos + 1 < len(s) else seg_dates[-1])
        trough_date = dd.loc[seg_dates].idxmin()
        days = (end_date - peak_date).days
        trough_dd = float(dd.loc[seg_dates].min())
        cand = {
            "start_date": peak_date.date().isoformat(),
            "trough_date": trough_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "days": int(days),
            "drawdown": _r4(-trough_dd),
            "_depth": trough_dd,
        }
        if best is None or days > best["days"] or (
            days == best["days"] and trough_dd < best["_depth"]
        ):
            best = cand
    best.pop("_depth", None)
    return best


def _relative_metrics(s: pd.Series, b: pd.Series) -> dict:
    """FR-1 八项指标；口径同 Evaluator._compute_relative_metrics。"""
    none_row = {k: None for k in _REL_KEYS}
    frame = pd.DataFrame({"s": s, "b": b}).dropna()
    rs = frame["s"].pct_change().dropna()
    rb = frame["b"].pct_change().dropna()
    rs, rb = rs.align(rb, join="inner")
    n = len(rs)
    if n < MIN_REL_SAMPLE_DAYS:
        return {
            "available": False, "n_days": n,
            "strategy_total_return": None, "benchmark_total_return": None,
            **none_row,
        }

    var_b = rb.var(ddof=0)
    cov = rs.cov(rb, ddof=0)
    beta = cov / var_b if var_b > 0 else None

    days = (frame.index[-1] - frame.index[0]).days
    years = max(days / 365.25, 1 / 365.25)
    cagr_s = (frame["s"].iloc[-1] / frame["s"].iloc[0]) ** (1 / years) - 1
    cagr_b = (frame["b"].iloc[-1] / frame["b"].iloc[0]) ** (1 / years) - 1
    jensen = (
        cagr_s - RISK_FREE_RATE - beta * (cagr_b - RISK_FREE_RATE)
        if beta is not None else None
    )

    active = rs - rb
    excess_annual = active.mean() * TRADING_DAYS
    te = active.std(ddof=0) * math.sqrt(TRADING_DAYS)
    ir = excess_annual / te if te > 0 else None

    up_s = rs[rb > 0]
    up_mean_b = rb[rb > 0].mean()
    down_s = rs[rb < 0]
    down_mean_b = rb[rb < 0].mean()
    up_capture = up_s.mean() / up_mean_b if up_mean_b else None
    down_capture = down_s.mean() / down_mean_b if down_mean_b else None

    equity = (1 + active).cumprod()
    excess_dd = -(equity / equity.cummax() - 1).min()

    return {
        "available": True,
        "n_days": int(n),
        "strategy_total_return": _r4(frame["s"].iloc[-1] / frame["s"].iloc[0] - 1),
        "benchmark_total_return": _r4(frame["b"].iloc[-1] / frame["b"].iloc[0] - 1),
        "beta": _r4(beta),
        "jensen_alpha": _r4(jensen),
        "excess_annual_return": _r4(excess_annual),
        "tracking_error": _r4(te),
        "information_ratio": _r4(ir),
        "up_capture_ratio": _r4(up_capture),
        "down_capture_ratio": _r4(down_capture),
        "excess_max_drawdown": _r4(excess_dd),
    }


def compute_robustness(
    nav: pd.Series,
    benchmark: pd.Series,
    benchmark_closes: Optional[Dict[str, pd.Series]] = None,
    benchmark_names: Optional[Dict[str, str]] = None,
    primary_code: Optional[str] = None,
) -> dict:
    """计算稳健性块。benchmark_closes 为替换敏感性用的宽基序列（不含主基准亦可）。"""
    benchmark_closes = benchmark_closes or {}
    benchmark_names = benchmark_names or {}

    nav = _as_series(nav)
    benchmark = _as_series(benchmark)
    s, b = _aligned(nav, benchmark)
    if len(s) < MIN_ALIGNED_DAYS:
        return {
            "status": "insufficient",
            "n_days": int(len(s)),
            "error": f"策略与基准共同交易日不足{MIN_ALIGNED_DAYS}天"
                     f"（{len(s)}），稳健性无法分析",
        }

    weekly_nav = s.resample("W-FRI").last()
    rows = [{
        "code": primary_code or "BENCH",
        "name": benchmark_names.get(primary_code, "主基准"),
        "is_primary": True,
        **_relative_metrics(s, b),
    }]
    for code, closes in benchmark_closes.items():
        if code == primary_code:
            continue
        cs, cb = _aligned(nav, _as_series(closes))
        row = {
            "code": code,
            "name": benchmark_names.get(code, code),
            "is_primary": False,
            **_relative_metrics(cs, cb),
        }
        rows.append(row)

    return {
        "status": "ok",
        "n_days": int(len(s)),
        "start_date": s.index[0].date().isoformat(),
        "end_date": s.index[-1].date().isoformat(),
        "half_year_matrix": _half_year_matrix(s, b),
        "rolling": {
            "weekly_points": int(len(weekly_nav)),
            "windows": _rolling_windows(weekly_nav),
        },
        "longest_loss_window": _longest_loss_window(s),
        "benchmark_sensitivity": rows,
    }
