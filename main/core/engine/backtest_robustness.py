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


def _round_ret(v) -> Optional[float]:
    """None 安全的四位小数：首末净值为 0 时 _return_ratio 返回 None，
    直接 round(None, 4) 会抛 TypeError（稳健性分析整块丢失）。"""
    return round(v, 4) if v is not None else None


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


def _share(v, base) -> Optional[float]:
    """占比：分母 <= 0（毛利/净利为负或为零）时返回 None，避免负占比误导。"""
    if not base or base <= 0:
        return None
    return round(v / base, 4)


def _date_str(v) -> Optional[str]:
    """payload 走 json.dumps（无 default=str），日期须转字符串。"""
    if v is None:
        return None
    if hasattr(v, "date") and not isinstance(v, str):
        return v.date().isoformat() if hasattr(v, "hour") else v.isoformat()
    return str(v)


def _empty_concentration(total_stocks: int = 0) -> dict:
    return {
        "total_realized_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        "unrealized": {"total_pnl": 0.0, "cost": 0.0, "market_value": 0.0, "rows": []},
        "top": [], "top20_share": None, "top20_share_gross": None,
        "profitable_stocks": 0, "total_stocks": total_stocks,
        "lots": {"count": 0, "winners": 0, "top": [], "cumulative": [],
                 "hhi": None, "effective_sources": None},
    }


def _concentration(trades, last_positions=None) -> dict:
    """盈利贡献分布：已实现（FIFO 逐笔）/ 未实现（期末持仓）分离 + 股本与单笔两个粒度。

    口径修正（审计）：
    - 旧实现按现金流汇总（买入 -amount-fee、卖出 +amount-fee），期末未平仓持仓
      的买入成本被当成流出且无对应卖出收入 → 被记为负贡献、总净利被低估。
      现改为 FIFO 逐笔匹配算已实现 P&L（与 Evaluator._compute_trade_metrics 一致，
      双边费用按股分摊），未平仓持仓单列到 unrealized。
    - 占比同时给出两个分母：gross_profit（毛利，衡量"利润来自哪里"）与
      net_profit（净利，含亏损抵扣），避免单一口径误读集中度。
    - 新增 lots 块：单笔（FIFO 批次）贡献分布（前 N 笔占毛利/净利、HHI、等效来源数）。

    trades: [{stock_code, trade_type, price, quantity, amount, commission,
              stamp_duty, bar_time}]；缺 quantity/price 的旧入参按 amount 当单价、
    qty=1 兼容（整笔视为一个批次，结果与旧现金流口径一致）。
    last_positions: 末期快照 positions（[{stock_code, quantity, market_value,
    cost_value}]，字符串数值）。
    """
    fifo: Dict[str, list] = defaultdict(list)
    lots: List[dict] = []
    realized: Dict[str, float] = defaultdict(float)

    for t in trades:
        code = t.get("stock_code")
        ttype = t.get("trade_type")
        amount = _f(t.get("amount")) or 0.0
        qty = _f(t.get("quantity")) or 0.0
        price = _f(t.get("price"))
        if qty <= 0:
            qty, price = 1.0, (price if price is not None else amount)
        elif price is None:
            price = amount / qty
        fee_ps = ((_f(t.get("commission")) or 0.0)
                  + (_f(t.get("stamp_duty")) or 0.0)) / qty
        if ttype == "BUY":
            fifo[code].append([price, qty, fee_ps, t.get("bar_time")])
            continue
        if ttype != "SELL":
            continue
        remaining = qty
        while remaining > 0 and fifo[code]:
            bp, bq, bcp, bt = fifo[code][0]
            taken = min(remaining, bq)
            pnl = (price - bp - bcp - fee_ps) * taken
            lots.append({"code": code, "pnl": pnl, "qty": taken,
                         "buy_time": bt, "sell_time": t.get("bar_time")})
            realized[code] += pnl
            remaining -= taken
            if taken >= bq:
                fifo[code].pop(0)
            else:
                fifo[code][0][1] = bq - taken

    if not lots and not last_positions:
        return _empty_concentration(len(realized))

    gross_profit = sum(l["pnl"] for l in lots if l["pnl"] > 0)
    gross_loss = abs(sum(l["pnl"] for l in lots if l["pnl"] < 0))
    net = gross_profit - gross_loss

    ranked = sorted(realized.items(), key=lambda kv: -kv[1])
    top = [{"code": c, "pnl": round(p, 2),
            "share": _share(p, net),
            "share_gross": _share(p, gross_profit),
            "share_net": _share(p, net)}
           for c, p in ranked[:10]]
    top20 = sum(p for _, p in ranked[:20])
    # 毛利口径只累计正贡献（负贡献股票不属于"利润来源"，否则会低估集中度）
    top20_gross = sum(p for _, p in ranked[:20] if p > 0)

    # 未实现：期末仍持仓的市值 - 成本（成本含买入费用由 positions 的 cost_value 口径）
    unreal_rows = []
    u_cost = u_mv = 0.0
    for p in (last_positions or []):
        mv = _f(p.get("market_value")) or 0.0
        cost = _f(p.get("cost_value")) or 0.0
        if mv == 0.0 and cost == 0.0:
            continue
        unreal_rows.append({"code": p.get("stock_code"),
                            "quantity": _f(p.get("quantity")),
                            "cost": round(cost, 2), "market_value": round(mv, 2),
                            "pnl": round(mv - cost, 2)})
        u_cost += cost
        u_mv += mv
    unreal_rows.sort(key=lambda r: -r["pnl"])
    u_total = u_mv - u_cost
    for r in unreal_rows:
        r["share_net"] = _share(r["pnl"], net)

    # 单笔（FIFO 批次）贡献分布
    lots_sorted = sorted(lots, key=lambda l: -l["pnl"])
    n_lots = len(lots_sorted)
    winners = sum(1 for l in lots_sorted if l["pnl"] > 0)
    top_lots = [{"rank": i + 1, "code": l["code"],
                 "buy_date": _date_str(l["buy_time"]),
                 "sell_date": _date_str(l["sell_time"]),
                 "quantity": l["qty"], "pnl": round(l["pnl"], 2),
                 "share_gross": _share(l["pnl"], gross_profit),
                 "share_net": _share(l["pnl"], net)}
                for i, l in enumerate(lots_sorted[:10])]
    milestones = sorted({1, 3, 5, 10, max(1, n_lots // 100),
                         max(1, n_lots // 20), max(1, n_lots // 10)})
    cumulative = []
    for k in milestones:
        s = sum(l["pnl"] for l in lots_sorted[:k])
        cumulative.append({"n": k, "pct_of_lots": round(k / n_lots, 4),
                           "pnl": round(s, 2),
                           "share_gross": _share(s, gross_profit),
                           "share_net": _share(s, net)})
    pos_pnls = [l["pnl"] for l in lots_sorted if l["pnl"] > 0]
    hhi = None
    effective = None
    if pos_pnls:
        tot = sum(pos_pnls)
        hhi = sum((p / tot) ** 2 for p in pos_pnls) if tot else None
        hhi = round(hhi, 4) if hhi is not None else None
        effective = round(1 / hhi, 1) if hhi else None

    return {
        "total_realized_pnl": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "unrealized": {"total_pnl": round(u_total, 2), "cost": round(u_cost, 2),
                       "market_value": round(u_mv, 2), "rows": unreal_rows},
        "top": top,
        "top20_share": _share(top20, net),
        "top20_share_gross": _share(top20_gross, gross_profit),
        "profitable_stocks": sum(1 for _, p in ranked if p > 0),
        "total_stocks": len(ranked),
        "lots": {"count": n_lots, "winners": winners, "top": top_lots,
                 "cumulative": cumulative, "hhi": hhi,
                 "effective_sources": effective},
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
        "benchmark_return": _round_ret(_return_ratio(bench[0], bench[-1])) if len(bench) > 1 else None,
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
    last_positions = snaps[-1].get("positions") if snaps else None
    if not vals:
        return {
            "total_return": None, "benchmark_return": None, "years_positive": "0/0",
            "annual": [], "rolling_3y": [],
            "concentration": _concentration(trades or [], last_positions),
            "oos": None,
        }
    yearly = _yearly_returns(snaps)
    years = sorted(yearly)
    bench = [_f(s.get("benchmark_value")) for s in snaps]
    bench = [b for b in bench if b is not None]
    pos_years = sum(1 for y in years if (yearly[y]["return"] or 0) > 0)
    return {
        "total_return": _round_ret(_return_ratio(vals[0], vals[-1])),
        "benchmark_return": _round_ret(_return_ratio(bench[0], bench[-1])) if len(bench) > 1 else None,
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
        "concentration": _concentration(trades or [], last_positions),
        "oos": _oos_split(snaps, yearly, years, oos_ratio),
    }
