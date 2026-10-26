"""持续性检验：分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆。

- 阿尔法衰减：近 2+ 满年 超额（vs 组合基准）逐年收敛 且 夏普/卡玛下行、回撤走高
  且 胜率或盈亏比走低、换手或成本上升 —— 三条件同时成立。
- 风格钟摆：总收益逐年回落 但 超额未收敛 且 风险指标平稳。
- 不足 2 满年 → insufficient（仅展示数据）。
"""
import bisect
import datetime
import math
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from core.engine.crisis_alpha import CRISIS_BENCHMARKS

FULL_YEAR_MIN_DAYS = 200          # 满年门槛：≥200 个交易日（约 10 个月），否则仅展示不参与判定
ROLLING_6M_DAYS = 183
ROLLING_12M_DAYS = 365
TRADING_DAYS_YEAR = 252
MIN_SHARPE_OBS = 20
VERDICT_WINDOW_YEARS = 3          # 判定窗口：最近 3 个满年（不足 3 年时退化为 2 年，阈值更高）

# 判定阈值（超出噪声带才算方向性变化，避免 1bp 波动触发结论）
MIN_EXCESS_DECLINE_3Y = 0.03      # 3 年窗口累计超额降幅 ≥ 3pp
MIN_EXCESS_DECLINE_2Y = 0.05      # 2 年窗口累计超额降幅 ≥ 5pp
MIN_SHARPE_DROP = 0.3             # 夏普下降 ≥ 0.3
MIN_CALMAR_REL_DROP = 0.20        # 卡玛相对下降 ≥ 20%
MIN_DD_RISE = 0.02                # 最大回撤上升 ≥ 2pp
MIN_WIN_RATE_DROP = 0.03          # 胜率下降 ≥ 3pp
MIN_PF_REL_DROP = 0.20            # 盈亏比相对下降 ≥ 20%
MIN_TURNOVER_RISE = 0.10          # 换手相对上升 ≥ 10%
MIN_COST_REL_RISE = 0.20          # 成本占比相对上升 ≥ 20%

_CAVEAT = ("结论供参考，非自动判定：判定窗口为最近 3 个满年（不足 3 年时用 2 年并提高门槛），"
           "仅当超额趋势性收敛 + 风险指标同步恶化 + 交易结构退化同时成立才判为疑似衰减；"
           "若总收益随风格指数回落而超额稳定，则为风格钟摆（市场温度周期）。")


def _f(v: Decimal) -> float:
    return float(v)


def _ratio(a: Decimal, b: Decimal) -> Optional[float]:
    if b <= 0:
        return None
    return _f(a / b - 1)


def _max_drawdown(vals: List[Decimal]) -> float:
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        elif peak > 0:
            mdd = max(mdd, _f((peak - v) / peak))
    return round(mdd, 4)


def _sharpe(vals: List[Decimal]) -> Optional[float]:
    if len(vals) < MIN_SHARPE_OBS + 1:
        return None
    rets = []
    for i in range(1, len(vals)):
        prev = _f(vals[i - 1])
        if prev > 0:
            rets.append(_f(vals[i]) / prev - 1)
    if len(rets) < MIN_SHARPE_OBS:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    if std <= 0:
        return None
    return round(mean / std * math.sqrt(TRADING_DAYS_YEAR), 4)


def _aligned_year_return(nav: Dict[datetime.date, Decimal],
                         closes: Dict[datetime.date, Decimal],
                         year_dates: List[datetime.date]) -> Tuple[Optional[float], Optional[float]]:
    """同一年内按「策略与基准共同交易日」对齐后计算两者收益。

    避免基准停牌/缺数据时用各自首末日，导致超额被虚增或虚减。
    """
    common = [d for d in year_dates if d in closes]
    if len(common) < 2:
        return None, None
    a, b = common[0], common[-1]
    if nav[a] <= 0 or nav[b] <= 0 or closes[a] <= 0 or closes[b] <= 0:
        return None, None
    return _ratio(nav[b], nav[a]), _ratio(closes[b], closes[a])


def _slope(vals: List[float]) -> Optional[float]:
    """最小二乘斜率（x = 0,1,2...），用于超额趋势方向。"""
    n = len(vals)
    if n < 2:
        return None
    mean_x = (n - 1) / 2
    mean_y = sum(vals) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    if denom <= 0:
        return None
    return sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals)) / denom


def _pct_s(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:+.2f}%"


def _pct1(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v: Optional[float], nd: int = 2) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _fifo_lots(trades: List[dict]) -> List[Tuple[float, int]]:
    """FIFO 逐笔 P&L（复用 Evaluator 口径：净 P&L 扣双边费用），按 SELL 日期归年。"""
    fifo: dict = {}
    lots: List[Tuple[float, int]] = []
    for tr in sorted(trades, key=lambda t: t["bar_time"]):
        key = (tr["strategy_id"], tr["stock_code"])
        if tr["trade_type"] == "BUY":
            cost_ps = _f((tr["commission"] + tr["stamp_duty"]) / max(tr["quantity"], 1))
            fifo.setdefault(key, []).append([_f(tr["price"]), int(tr["quantity"]), cost_ps])
        elif tr["trade_type"] == "SELL":
            sell_cost_ps = _f((tr["commission"] + tr["stamp_duty"]) / max(tr["quantity"], 1))
            remaining = int(tr["quantity"])
            queue = fifo.get(key, [])
            while remaining > 0 and queue:
                lot = queue[0]
                taken = min(remaining, lot[1])
                pnl = (_f(tr["price"]) - lot[0] - lot[2] - sell_cost_ps) * taken
                lots.append((pnl, tr["bar_time"].year))
                remaining -= taken
                if taken >= lot[1]:
                    queue.pop(0)
                else:
                    lot[1] -= taken
    return lots


def _year_turnover_cost(trades: List[dict], year: int, nav_series) -> Tuple[Optional[float], Optional[float]]:
    """（当年换手倍数, 成本/当年日均净值）。

    口径与 Evaluator 一致：换手=当年双边成交额/日均净值；成本=(佣金+印花税)
    /日均净值（分母同换手，不用成交额或首日净值）。当年换手不年化（自然年）。
    """
    yr = [t for t in trades if t["bar_time"].year == year]
    if not yr:
        return None, None
    total_amount = sum(_f(t["amount"]) for t in yr)
    total_cost = sum(_f(t["commission"] + t["stamp_duty"]) for t in yr)
    dts = sorted(d for d in nav_series if d.year == year)
    if not dts:
        return None, None
    avg_nav = sum(_f(nav_series[d]) for d in dts) / len(dts)
    turnover = round(total_amount / avg_nav, 4) if avg_nav else None
    cost_ratio = round(total_cost / avg_nav, 6) if avg_nav else None
    return turnover, cost_ratio


def _prev_key(sorted_keys, d):
    i = bisect.bisect_right(sorted_keys, d) - 1
    return sorted_keys[i] if i >= 0 else None


def _rolling_annualized(nav, window_days):
    dates = sorted(nav)
    out_dates, out_vals = [], []
    for d in dates:
        s = _prev_key(dates, d - datetime.timedelta(days=window_days))
        if s is None or nav[s] <= 0 or nav[d] <= 0:
            out_dates.append(d.isoformat())
            out_vals.append(None)
            continue
        span = (d - s).days
        ann = (_f(nav[d]) / _f(nav[s])) ** (365.0 / span) - 1
        out_dates.append(d.isoformat())
        out_vals.append(round(ann, 4))
    return out_dates, out_vals


def _rolling_against(anchor_dates, closes, window_days):
    cs = sorted(closes)
    out = []
    for d in anchor_dates:
        s = _prev_key(cs, d - datetime.timedelta(days=window_days))
        cur = _prev_key(cs, d)
        if s is None or cur is None or s >= cur or closes[s] <= 0 or closes[cur] <= 0:
            out.append(None)
            continue
        span = (d - s).days
        ann = (_f(closes[cur]) / _f(closes[s])) ** (365.0 / span) - 1
        out.append(round(ann, 4))
    return out


def _bench_name(code: str, main_name: str, main_code: str) -> str:
    if code == main_code:
        return main_name
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == code:
            return spec["name"]
    return code


def _signal(key: str, label: str, detail: str, status: str, word: str) -> dict:
    return {"key": key, "label": label, "detail": detail, "status": status, "word": word}


def _insufficient(reason: str) -> dict:
    return {
        "level": "insufficient",
        "reasons": [reason],
        "summary": "样本不足，仅展示分年度数据",
        "signals": [],
        "window_years": [],
        "caveat": _CAVEAT,
    }


def decide_verdict(years: List[dict], benchmark_code: str,
                   benchmark_name: Optional[str] = None) -> dict:
    """判定阿尔法衰减 / 风格钟摆 / 信号混合 / 稳定。

    规则要点（P0 修正）：
    - 仅用 ≥200 交易日的满年；判定窗口取最近 3 个满年（不足则 2 年，门槛更高）。
    - 超额要求「趋势为负 + 累计降幅超阈值」，而不是任意相邻年下降。
    - 风险指标需超过噪声带（夏普 0.3 / 卡玛相对 20% / 回撤 2pp），且至少两项可评估。
    - 交易结构 = 质量指标显著走低 且 活跃度/成本上升（不再把共线的换手与成本当作两个独立证据）。
    - 主基准在窗口内缺数据 → insufficient，绝不默认「稳定」。
    """
    bench_label = benchmark_name or benchmark_code
    full = sorted((y for y in years if not y.get("is_partial")), key=lambda y: y["year"])
    if len(full) < 2:
        return _insufficient("有效满年不足 2 年，暂不判定阿尔法衰减/风格钟摆（仅展示分年度数据）")

    window = full[-VERDICT_WINDOW_YEARS:]
    win_years = [y["year"] for y in window]
    excess = [(y.get("excess") or {}).get(benchmark_code) for y in window]
    if any(v is None for v in excess):
        missing = [str(y["year"]) for y, v in zip(window, excess) if v is None]
        return _insufficient(
            f"主基准 {bench_label}({benchmark_code}) 在判定窗口 {win_years[0]}–{win_years[-1]} "
            f"缺数据（{'、'.join(missing)}），无法计算超额；请先重算或更换主基准")
    ex = [float(v) for v in excess]
    drop = ex[0] - ex[-1]
    slope = _slope(ex)
    min_drop = MIN_EXCESS_DECLINE_3Y if len(ex) >= 3 else MIN_EXCESS_DECLINE_2Y
    conv = slope is not None and slope < 0 and drop >= min_drop
    ex_status = "bad" if conv else ("mixed" if drop > 0 else "good")
    ex_word = "收敛" if conv else ("轻微收敛" if drop > 0 else "未收敛")
    ex_detail = " → ".join(f"{y['year']} {_pct_s(v)}" for y, v in zip(window, excess))

    last, prev = full[-1], full[-2]
    s_now, s_prev = last.get("sharpe"), prev.get("sharpe")
    c_now, c_prev = last.get("calmar"), prev.get("calmar")
    dd_now, dd_prev = last.get("max_drawdown"), prev.get("max_drawdown")

    risk_flags: List[bool] = []
    if s_now is not None and s_prev is not None:
        risk_flags.append(s_prev - s_now >= MIN_SHARPE_DROP)
    if c_now is not None and c_prev is not None and c_prev > 0:
        risk_flags.append((c_prev - c_now) / c_prev >= MIN_CALMAR_REL_DROP)
    if dd_now is not None and dd_prev is not None:
        risk_flags.append(dd_now - dd_prev >= MIN_DD_RISE)
    risk = len(risk_flags) >= 2 and all(risk_flags)
    risk_any = any(risk_flags) if risk_flags else False
    risk_status = "bad" if risk else ("mixed" if risk_any else "good")
    risk_word = "恶化" if risk else ("轻度恶化" if risk_any else "平稳")
    risk_detail = (f"夏普 {_num(s_prev)}→{_num(s_now)} · 卡玛 {_num(c_prev)}→{_num(c_now)}"
                   f" · 回撤 {_pct1(dd_prev)}→{_pct1(dd_now)}")

    wr_now, wr_prev = last.get("win_rate"), prev.get("win_rate")
    pf_now, pf_prev = last.get("profit_factor"), prev.get("profit_factor")
    tn_now, tn_prev = last.get("turnover"), prev.get("turnover")
    cs_now, cs_prev = last.get("cost_ratio"), prev.get("cost_ratio")

    wr_bad = wr_now is not None and wr_prev is not None and wr_prev - wr_now >= MIN_WIN_RATE_DROP
    pf_bad = (pf_now is not None and pf_prev is not None and pf_prev > 0
              and (pf_prev - pf_now) / pf_prev >= MIN_PF_REL_DROP)
    quality_bad = wr_bad or pf_bad
    turnover_up = tn_now is not None and tn_prev is not None and tn_prev > 0 and tn_now >= tn_prev * (1 + MIN_TURNOVER_RISE)
    cost_up = cs_now is not None and cs_prev is not None and cs_prev > 0 and cs_now >= cs_prev * (1 + MIN_COST_REL_RISE)
    activity_up = turnover_up or cost_up
    struct = quality_bad and activity_up
    struct_any = quality_bad or activity_up
    struct_status = "bad" if struct else ("mixed" if struct_any else "good")
    struct_word = "退化" if struct else ("结构性波动" if struct_any else "平稳")
    struct_detail = (f"胜率 {_pct1(wr_prev)}→{_pct1(wr_now)} · 盈亏比 {_num(pf_prev)}→{_num(pf_now)}"
                     f" · 换手 {_num(tn_prev)}→{_num(tn_now)}× · 成本/日均净值 {_pct1(cs_prev)}→{_pct1(cs_now)}")

    signals = [
        _signal("excess", f"超额 vs {bench_label}", ex_detail, ex_status, ex_word),
        _signal("risk", "风险指标", risk_detail, risk_status, risk_word),
        _signal("struct", "交易结构", struct_detail, struct_status, struct_word),
    ]
    reasons = [
        f"超额(vs {benchmark_code}) 近 {len(ex)} 个满年{ex_word}"
        f"（累计 {drop * 100:+.2f}pp，门槛 {min_drop * 100:.0f}pp）",
        f"风险指标（夏普/卡玛/回撤）近两年{risk_word}",
        f"交易结构（胜率/盈亏比/换手/成本）近两年{struct_word}",
    ]

    total_down = last.get("nav_return") is not None and prev.get("nav_return") is not None \
        and last["nav_return"] < prev["nav_return"]
    if conv and risk and struct:
        level = "alpha_decay"
        summary = "疑似阿尔法衰减：超额趋势收敛 + 风险恶化 + 结构退化同时成立，需进一步排查容量拥挤/因子衰减"
    elif total_down and not conv and not risk:
        level = "style_pendulum"
        summary = "风格钟摆（市场温度周期）：总收益随风格指数回落，但超额稳定、风险平稳，非策略能力衰减"
    elif risk or struct or conv or risk_any or struct_any or ex_status == "mixed":
        level = "mixed"
        summary = "信号混合：部分维度走坏但未形成三重共振，建议继续观察并延长判定窗口"
    else:
        level = "stable"
        summary = "稳定：超额未趋势性收敛，风险与交易结构均在噪声带内"

    return {
        "level": level,
        "reasons": reasons,
        "summary": summary,
        "signals": signals,
        "window_years": win_years,
        "caveat": _CAVEAT,
    }


def compute_persistence(
    nav_series: Dict[datetime.date, Decimal],
    trades: List[dict],
    benchmark_closes: Dict[str, Dict[datetime.date, Decimal]],
    benchmark_code: str,
    benchmark_name: str,
) -> dict:
    dates = sorted(nav_series)
    if not dates:
        return {"status": "failed", "error": "组合日净值为空，无法计算持续性检验"}
    by_year: Dict[int, List[datetime.date]] = {}
    for d in dates:
        by_year.setdefault(d.year, []).append(d)
    all_codes = [benchmark_code] + sorted(c for c in benchmark_closes if c != benchmark_code)
    lots = _fifo_lots(trades)
    years = []
    for y in sorted(by_year):
        dts = by_year[y]
        full = len(dts) >= FULL_YEAR_MIN_DAYS
        vals = [nav_series[d] for d in dts]
        nav_return = _ratio(vals[-1], vals[0])
        mdd = _max_drawdown(vals)
        sharpe = _sharpe(vals)
        calmar = round(nav_return / mdd, 4) if mdd > 0 and nav_return is not None else None
        yr_lots = [pnl for pnl, ly in lots if ly == y]
        win_rate = profit_factor = None
        if yr_lots:
            winners = sum(1 for pnl in yr_lots if pnl > 0)
            win_rate = round(winners / len(yr_lots), 4)
            gross_profit = sum(p for p in yr_lots if p > 0)
            gross_loss = abs(sum(p for p in yr_lots if p < 0))
            if gross_loss > 0:
                profit_factor = round(gross_profit / gross_loss, 4)
        turnover, cost_ratio = _year_turnover_cost(trades, y, nav_series)
        excess, bench_returns = {}, {}
        for code in all_codes:
            closes = benchmark_closes.get(code)
            nav_aligned, r = _aligned_year_return(nav_series, closes, dts) if closes else (None, None)
            if r is not None:
                bench_returns[code] = r
                if nav_aligned is not None:
                    excess[code] = round(nav_aligned - r, 4)
        years.append({
            "year": y, "is_partial": not full, "trading_days": len(dts),
            "nav_return": nav_return, "max_drawdown": mdd,
            "sharpe": sharpe, "calmar": calmar,
            "win_rate": win_rate, "profit_factor": profit_factor,
            "turnover": turnover, "cost_ratio": cost_ratio,
            "excess": excess, "bench_returns": bench_returns,
        })
    d6, v6 = _rolling_annualized(nav_series, ROLLING_6M_DAYS)
    _d12, v12 = _rolling_annualized(nav_series, ROLLING_12M_DAYS)
    bench_closes_main = benchmark_closes.get(benchmark_code)
    b12 = _rolling_against(dates, bench_closes_main, ROLLING_12M_DAYS) if bench_closes_main else [None] * len(d6)
    rolling = {"dates": d6, "strat_6m": v6, "strat_12m": v12, "bench_12m": b12}
    verdict = decide_verdict(years, benchmark_code, benchmark_name)
    benchmarks = [{
        "code": c, "name": _bench_name(c, benchmark_name, benchmark_code),
        "is_main": c == benchmark_code, "available": c in benchmark_closes,
    } for c in all_codes]
    return {
        "status": "completed", "error": None,
        "years": years, "rolling": rolling,
        "benchmarks": benchmarks, "verdict": verdict,
    }
