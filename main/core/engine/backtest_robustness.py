"""回测稳健性检验（纯后处理分析，不重跑回测）。

输入已落库的组合级日快照（净值曲线）与成交（backtest_trades），输出：
- 年度收益 / 年内最大回撤（annual）
- 3 年滚动 CAGR / 窗口内最大回撤（rolling_3y）
- 个股已实现净 P&L 集中度（concentration：前10/前20占比、盈利个股占比）
- 样本内 / 样本外子区间对比（oos：默认按年数 75/25 自动切分，含各段与基准）

与复权模式（front/back/dual）无关——直接消费已落库的净值与成交。
纯函数 analyze_robustness 可单测；服务层（backtest_service.safe_compute_robustness）
负责查询落库、upsert、自动/手动计算。

返回全部为 JSON 安全值（float/int/str/None），供 payload_json 落库。
"""
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional


def _f(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _return_ratio(first_v, last_v) -> Optional[float]:
    if not first_v or not last_v:
        return None
    return last_v / first_v - 1


def _max_drawdown(series) -> float:
    # W5-L2：返回正号回撤（与 robustness.py 口径统一）；0 表示无回撤。
    peak = None
    mdd = 0.0
    for v in series:
        if peak is None or v > peak:
            peak = v
        if peak:
            mdd = max(mdd, 1 - v / peak)
    return mdd


def _yearly_returns(snaps) -> Dict[str, dict]:
    """{year: {"return", "max_drawdown", "first_value", "last_value", "first_date", "last_date"}}"""
    by_year = defaultdict(list)
    for s in snaps:
        by_year[s["snap_date"].year].append(s)
    out = {}
    for y, pts in sorted(by_year.items()):
        vals = [_f(s["total_value"]) for s in pts]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        out[str(y)] = {
            "return": _return_ratio(vals[0], vals[-1]),
            "max_drawdown": _max_drawdown(vals),
            "first_value": vals[0],
            "last_value": vals[-1],
            "first_date": pts[0]["snap_date"],
            "last_date": pts[-1]["snap_date"],
        }
    return out


def _rolling_cagr(snaps, yearly, years, width=3) -> List[dict]:
    out = []
    if len(years) < width:
        return out
    for i in range(len(years) - width + 1):
        w = years[i:i + width]
        start_v = yearly[w[0]]["first_value"]
        end_v = yearly[w[2]]["last_value"]
        # W5-O3：按窗口实际首末日期跨度年化（自然日/365.25），对齐 _segment。
        start_d = yearly[w[0]]["first_date"]
        end_d = yearly[w[2]]["last_date"]
        span_years = (end_d - start_d).days / 365.25
        cagr = _return_ratio(start_v, end_v)
        if cagr is not None and cagr > -1 and span_years > 0:
            cagr = (1 + cagr) ** (1 / span_years) - 1
        inw = [s for s in snaps if int(w[0]) <= s["snap_date"].year <= int(w[2])]
        out.append({
            "start": w[0],
            "end": w[2],
            "cagr": round(cagr, 4) if cagr is not None else None,
            "max_drawdown": round(_max_drawdown([_f(s["total_value"]) for s in inw]), 4),
        })
    return out


def _concentration(trades) -> dict:
    net: Dict[str, float] = defaultdict(float)
    for t in trades:
        amount = _f(t.get("amount")) or 0.0
        commission = _f(t.get("commission")) or 0.0
        stamp = _f(t.get("stamp_duty")) or 0.0
        if t.get("trade_type") == "BUY":
            net[t["stock_code"]] -= amount + commission + stamp
        else:
            net[t["stock_code"]] += amount - commission - stamp
    ranked = sorted(net.items(), key=lambda kv: -kv[1])
    total = sum(net.values())
    if not ranked or not total:
        return {
            "total_realized_pnl": round(total, 2) if total else 0.0,
            "top": [], "top20_share": 0.0,
            "profitable_stocks": 0, "total_stocks": len(ranked),
        }
    top = [{"code": c, "pnl": round(p, 2), "share": round(p / total, 4)}
           for c, p in ranked[:10]]
    top20 = sum(p for _, p in ranked[:20])
    return {
        "total_realized_pnl": round(total, 2),
        "top": top,
        "top20_share": round(top20 / total, 4),
        "profitable_stocks": sum(1 for _, p in ranked if p > 0),
        "total_stocks": len(ranked),
    }


def _segment(snaps, lo, hi):
    sub = [s for s in snaps if lo <= s["snap_date"].year <= hi]
    if not sub:
        return None
    vals = [_f(s["total_value"]) for s in sub]
    bench = [_f(s.get("benchmark_value")) for s in sub]
    bench = [b for b in bench if b is not None]
    # W5-O3：CAGR 年化用实际首末日期跨度（自然日/365.25），而非 hi-lo+1 整年。
    # 子区间可能不足/超过整年（如 1 月~12 月仅 11 个月），整年计会低估年化。
    span_years = (sub[-1]["snap_date"] - sub[0]["snap_date"]).days / 365.25
    ret = _return_ratio(vals[0], vals[-1])
    cagr = None
    if ret is not None and ret > -1 and span_years > 0:
        cagr = (1 + ret) ** (1 / span_years) - 1
    return {
        "start": sub[0]["snap_date"].isoformat()[:10],
        "end": sub[-1]["snap_date"].isoformat()[:10],
        "return": round(ret, 4) if ret is not None else None,
        "cagr": round(cagr, 4) if cagr is not None else None,
        "max_drawdown": round(_max_drawdown(vals), 4),
        "benchmark_return": round(_return_ratio(bench[0], bench[-1]), 4) if len(bench) > 1 else None,
    }


def _oos_split(snaps, yearly, years, oos_ratio: float) -> Optional[dict]:
    if len(years) < 2:
        return None
    n = len(years)
    oos_count = max(1, min(n - 1, round(n * oos_ratio)))
    split_idx = n - oos_count
    split_year = years[split_idx]
    in_hi = int(split_year) - 1
    in_seg = _segment(snaps, int(years[0]), in_hi)
    out_seg = _segment(snaps, int(split_year), int(years[-1]))
    if in_seg is None or out_seg is None:
        return None
    return {
        "split_year": split_year,
        "in_sample": in_seg,
        "out_of_sample": out_seg,
        # W5-O2：伪 OOS 明示——固定切分防时段崩塌，不防全样本调参
        "caveat": "OOS 仅防时段崩塌，不防全样本调参",
    }


def analyze_robustness(
    snaps: List[dict],
    trades: List[dict],
    oos_ratio: float = 0.25,
) -> dict:
    """输入：组合级快照 [{snap_date: date, total_value, benchmark_value}] +
    成交 [{stock_code, trade_type, amount, commission, stamp_duty}]。
    输出：JSON 安全的分析 payload（见模块 docstring）。
    """
    snaps = sorted(snaps, key=lambda s: s["snap_date"])
    vals = [_f(s["total_value"]) for s in snaps]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {
            "total_return": None, "benchmark_return": None, "years_positive": "0/0",
            "annual": [], "rolling_3y": [], "concentration": _concentration(trades or []),
            "oos": None,
        }
    yearly = _yearly_returns(snaps)
    years = sorted(yearly)
    bench = [_f(s.get("benchmark_value")) for s in snaps]
    bench = [b for b in bench if b is not None]
    pos_years = sum(1 for y in years if (yearly[y]["return"] or 0) > 0)
    return {
        "total_return": round(_return_ratio(vals[0], vals[-1]), 4),
        "benchmark_return": round(_return_ratio(bench[0], bench[-1]), 4) if len(bench) > 1 else None,
        "years_positive": f"{pos_years}/{len(years)}",
        "annual": [
            {
                "year": y,
                "return": round(yearly[y]["return"], 4) if yearly[y]["return"] is not None else None,
                "max_drawdown": round(yearly[y]["max_drawdown"], 4),
            }
            for y in years
        ],
        "rolling_3y": _rolling_cagr(snaps, yearly, years),
        "concentration": _concentration(trades or []),
        "oos": _oos_split(snaps, yearly, years, oos_ratio),
    }
