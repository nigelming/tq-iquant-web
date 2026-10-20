# -*- coding: utf-8 -*-
"""行业归因（regime-alpha-suite Task 16）

纯计算层（无 TQ / DB 依赖，便于单测）：
- 行业暴露时间序列：每日持仓市值按个股所属通达信二级行业（881）聚合，
  权重 = 行业市值 / 组合总资产；未映射个股单列 UNMAPPED。
- 领涨行业命中率：周/月行业涨幅 Top-N 与期初持仓行业集合的重合度。
- 行业收益贡献：Σ 期初行业权重 × 行业日收益（Brinson 行业配置项，
  不含行业内选股 alpha）。

positions_json 缺失（老记录）→ status="needs_rerun"，不返回伪装结果。
"""
from typing import Dict, List, Optional

import pandas as pd

INDUSTRY_TOP_N = 3
INDUSTRY_CHART_TOP = 8
INDUSTRY_CONTRIB_TOP = 15
TAXONOMY = "通达信二级行业(881)"
UNMAPPED_CODE = "UNMAPPED"
UNMAPPED_NAME = "未分类"

NEEDS_RERUN_REASON = "该记录回测时未保存持仓明细，重新回测后可用"


def needs_rerun_block() -> dict:
    return {
        "status": "needs_rerun",
        "taxonomy": TAXONOMY,
        "reason": NEEDS_RERUN_REASON,
    }


def compute_industry_attribution(
    daily: List[dict],
    stock_industry: Dict[str, Optional[dict]],
    industry_names: Dict[str, str],
    industry_closes: Dict[str, pd.Series],
) -> dict:
    """计算行业归因块。

    daily: [{"snap_date": date, "total_value": number,
             "positions": [{"stock_code", "market_value"}] | None}]
    stock_industry: 持仓个股 → {"code","name"}（None/缺失=未映射）
    industry_names: 全量行业代码 → 名称（领涨行业可能不在持仓中）
    industry_closes: 行业指数代码 → 收盘价日序列
    """
    if not daily or not any(s.get("positions") is not None for s in daily):
        return needs_rerun_block()

    weights, unmapped = _daily_industry_weights(daily, stock_industry)
    if weights is None:
        return needs_rerun_block()

    exposure = _exposure_block(weights, industry_names)
    closes_df = _closes_frame(industry_closes)
    hit_rate = _hit_rate_block(weights, closes_df, industry_names)
    contribution = _contribution_block(weights, closes_df, industry_names)

    return {
        "status": "ok",
        "taxonomy": TAXONOMY,
        "n_days": len(weights.index),
        "unmapped_codes": sorted(unmapped),
        "exposure": exposure,
        "hit_rate": hit_rate,
        "contribution": contribution,
    }


# --------------------------------------------------------------------------
# 日度行业权重
# --------------------------------------------------------------------------

def _daily_industry_weights(
    daily: List[dict],
    stock_industry: Dict[str, Optional[dict]],
) -> "tuple[Optional[pd.DataFrame], set]":
    """返回 (日度权重 DataFrame, 未映射代码集合)；无有效快照日返回 None。"""
    rows: List[dict] = []
    unmapped: set = set()
    for snap in daily:
        d = snap.get("snap_date")
        positions = snap.get("positions")
        if d is None or positions is None:
            continue
        if isinstance(d, pd.Timestamp):
            d = d.date()
        total = float(snap.get("total_value") or 0.0)
        if total <= 0:
            continue
        row = {"_date": d}
        for pos in positions:
            code = pos.get("stock_code")
            mv = float(pos.get("market_value") or 0.0)
            if not code or mv <= 0:
                continue
            ind = stock_industry.get(code)
            key = ind["code"] if ind else UNMAPPED_CODE
            if not ind:
                unmapped.add(code)
            row[key] = row.get(key, 0.0) + mv / total
        rows.append(row)
    if not rows:
        return None, unmapped
    df = pd.DataFrame(rows).set_index("_date").sort_index()
    df = df.fillna(0.0)
    df.index = pd.DatetimeIndex(df.index)
    if UNMAPPED_CODE in df.columns:
        cols = [c for c in df.columns if c != UNMAPPED_CODE] + [UNMAPPED_CODE]
        df = df[cols]
    return df, unmapped


# --------------------------------------------------------------------------
# 暴露块（Top 行业 + 其他 + 未分类 + 现金）
# --------------------------------------------------------------------------

def _exposure_block(weights: pd.DataFrame, names: Dict[str, str]) -> dict:
    real_cols = [c for c in weights.columns if c != UNMAPPED_CODE]
    order = (weights[real_cols].mean(axis=0) if real_cols
             else pd.Series(dtype=float)).sort_values(ascending=False)
    top = list(order.index[:INDUSTRY_CHART_TOP])
    rest = [c for c in real_cols if c not in top]

    series: List[dict] = []
    for code in top:
        series.append(_exposure_series(
            code, names.get(code, code), weights[code]))
    if rest:
        series.append(_exposure_series(
            "__OTHER__", "其他行业", weights[rest].sum(axis=1)))
    if UNMAPPED_CODE in weights.columns:
        series.append(_exposure_series(
            UNMAPPED_CODE, UNMAPPED_NAME, weights[UNMAPPED_CODE]))

    invested = weights.sum(axis=1) if len(weights.columns) else \
        pd.Series(0.0, index=weights.index)
    cash = (1.0 - invested).clip(lower=0.0)
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in weights.index],
        "series": series,
        "cash": [round(float(x), 6) for x in cash],
    }


def _exposure_series(code: str, name: str, col: pd.Series) -> dict:
    return {
        "code": code,
        "name": name,
        "avg_weight": float(col.mean()),
        "weights": [round(float(x), 6) for x in col],
    }


# --------------------------------------------------------------------------
# 领涨命中率（周 / 月）
# --------------------------------------------------------------------------

def _closes_frame(industry_closes: Dict[str, pd.Series]) -> pd.DataFrame:
    if not industry_closes:
        return pd.DataFrame()
    df = pd.DataFrame(industry_closes).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.DatetimeIndex(df.index)
    return df


def _period_hit(weights: pd.DataFrame, closes: pd.DataFrame,
                names: Dict[str, str], rule: str) -> dict:
    held_cols = [c for c in weights.columns if c != UNMAPPED_CODE]
    if closes.empty or not held_cols:
        return {"top_n": INDUSTRY_TOP_N, "periods": 0,
                "periods_holding": 0, "hits": 0, "hit_rate": None,
                "details": []}
    period_close = closes.resample(rule).last()
    period_ret = period_close.pct_change()
    # 期初持仓 = 上一期末（季末/周末）权重
    period_w = weights.reindex(period_close.index, method="ffill").shift(1)
    details = []
    periods = 0
    periods_holding = 0
    hits = 0
    for ts in period_ret.index:
        rets = period_ret.loc[ts].dropna()
        if rets.empty:
            continue
        periods += 1
        wrow = period_w.loc[ts]
        held = [c for c in held_cols
                if c in wrow.index and float(wrow[c]) > 0]
        if not held:
            continue
        periods_holding += 1
        leaders = list(rets.sort_values(ascending=False).head(
            INDUSTRY_TOP_N).index)
        hit = bool(set(held) & set(leaders))
        hits += int(hit)
        details.append({
            "period": ts.strftime("%Y-%m-%d"),
            "held": [names.get(c, c) for c in held],
            "leaders": [names.get(c, c) for c in leaders],
            "hit": hit,
        })
    return {
        "top_n": INDUSTRY_TOP_N,
        "periods": periods,
        "periods_holding": periods_holding,
        "hits": hits,
        "hit_rate": (hits / periods_holding) if periods_holding else None,
        "details": details,
    }


def _hit_rate_block(weights: pd.DataFrame, closes: pd.DataFrame,
                    names: Dict[str, str]) -> dict:
    return {
        "weekly": _period_hit(weights, closes, names, "W-FRI"),
        "monthly": _period_hit(weights, closes, names, "ME"),
    }


# --------------------------------------------------------------------------
# 行业收益贡献（Σ 期初权重 × 行业日收益）
# --------------------------------------------------------------------------

def _contribution_block(weights: pd.DataFrame, closes: pd.DataFrame,
                        names: Dict[str, str]) -> dict:
    held_cols = [c for c in weights.columns
                 if c != UNMAPPED_CODE and c in closes.columns]
    if closes.empty or not held_cols:
        return {"available": False, "rows": [], "total_contribution": None}
    daily_ret = closes[held_cols].pct_change()
    # 快照日权重视为当日收盘权重，对次日收益为期初权重
    begin_w = weights[held_cols].shift(1)
    contrib = (begin_w * daily_ret).sum(axis=0)
    rows = []
    for code in held_cols:
        avg_w = float(begin_w[code].mean())
        s = closes[code].dropna()
        index_ret = float(s.iloc[-1] / s.iloc[0] - 1) if len(s) >= 2 else None
        rows.append({
            "code": code,
            "name": names.get(code, code),
            "avg_weight": round(avg_w, 6),
            "index_return": (round(index_ret, 6)
                             if index_ret is not None else None),
            "contribution": round(float(contrib[code]), 6),
        })
    rows.sort(key=lambda r: r["contribution"], reverse=True)
    rows = rows[:INDUSTRY_CONTRIB_TOP]
    return {
        "available": True,
        "rows": rows,
        "total_contribution": round(float(contrib.sum()), 6),
    }
