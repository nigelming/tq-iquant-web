import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

CRISIS_THRESHOLD = 0.0
EXTREME_TOP_N = 10
DEEP_DD_THRESHOLD = -0.05
ROLLING_CORR_WINDOW = 52
DEEP_WEEK_TESTS = (-0.03, -0.05)
MONTH_BIG_MOVE = 0.05
WEEKS_PER_YEAR = 52
MIN_COMMON_DAYS = 30
FULL_YEAR_WEEKS = 40

DEFAULT_BENCHMARK_CODE = "000300.SH"

CRISIS_CATS = ["逆势上涨", "跌得少", "跟着跌", "跌更多"]

CRISIS_COLORS = {
    "逆势上涨": "#d6403a",
    "跌得少": "#f0a429",
    "跟着跌": "#9dc88d",
    "跌更多": "#1f6e4f",
}

CRISIS_BENCHMARKS = (
    {"key": "csi_all", "name": "中证全指", "code": "000985.CSI"},
    {"key": "hs300", "name": "沪深300", "code": "000300.SH"},
    {"key": "csi500", "name": "中证500", "code": "000905.SH"},
    {"key": "csi1000", "name": "中证1000", "code": "000852.SH"},
    {"key": "cyb", "name": "创业板指", "code": "399006.SZ"},
    {"key": "kc50", "name": "科创50", "code": "000688.SH"},
)

_HEDGE_CATS = ("逆势上涨", "跌得少")


def _mean(values: Sequence[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _clip(value: float) -> float:
    return float(max(0.0, min(100.0, value)))


def _classify_crisis(b_values, s_values) -> List[str]:
    labels: List[str] = []
    for b, s in zip(b_values, s_values):
        if s >= 0:
            labels.append("逆势上涨")
            continue
        denom = abs(b)
        ratio = abs(s) / denom if denom > 1e-12 else math.inf
        if ratio < 0.5:
            labels.append("跌得少")
        elif ratio <= 1.0 + 1e-12:
            labels.append("跟着跌")
        else:
            labels.append("跌更多")
    return labels


def _rating(score: float) -> str:
    if score >= 85:
        return "卓越"
    if score >= 70:
        return "优秀"
    return "一般"


def alpha_rating(ratio: float) -> str:
    if ratio >= 0.80:
        return "卓越"
    if ratio >= 0.60:
        return "优秀"
    if ratio >= 0.40:
        return "一般"
    return "较弱"


class CrisisAlphaAnalyzer:
    def __init__(
        self,
        strategy_nav: pd.Series,
        benchmark_price: pd.Series,
        strategy_name: str = "策略",
        benchmark_name: str = "基准",
    ):
        if not isinstance(strategy_nav, pd.Series) or not isinstance(benchmark_price, pd.Series):
            raise TypeError("strategy_nav 与 benchmark_price 必须是 pd.Series")
        s = strategy_nav.copy().sort_index()
        b = benchmark_price.copy().sort_index()
        s.index = pd.to_datetime(s.index).normalize()
        b.index = pd.to_datetime(b.index).normalize()
        self.s_orig_nav = s.copy()
        self.b_orig_nav = b.copy()
        common = s.index.intersection(b.index)
        if len(common) < MIN_COMMON_DAYS:
            raise ValueError(
                f"策略与基准共同交易日不足{MIN_COMMON_DAYS}天（{len(common)}），无法分析"
            )
        s = s.loc[common]
        b = b.loc[common]
        self.s_daily_ret = s.pct_change().dropna()
        self.b_daily_ret = b.pct_change().dropna()
        common_r = self.s_daily_ret.index.intersection(self.b_daily_ret.index)
        self.s_daily_ret = self.s_daily_ret.loc[common_r]
        self.b_daily_ret = self.b_daily_ret.loc[common_r]
        s_w = s.resample("W-FRI").last().dropna()
        b_w = b.resample("W-FRI").last().dropna()
        common_w = s_w.index.intersection(b_w.index)
        self.s_week_ret = s_w.loc[common_w].pct_change().dropna()
        self.b_week_ret = b_w.loc[common_w].pct_change().dropna()
        common_wr = self.s_week_ret.index.intersection(self.b_week_ret.index)
        self.s_week_ret = self.s_week_ret.loc[common_wr]
        self.b_week_ret = self.b_week_ret.loc[common_wr]
        s_m = s.resample("ME").last().dropna()
        b_m = b.resample("ME").last().dropna()
        common_m = s_m.index.intersection(b_m.index)
        self.s_month_ret = s_m.loc[common_m].pct_change().dropna()
        self.b_month_ret = b_m.loc[common_m].pct_change().dropna()
        common_mr = self.s_month_ret.index.intersection(self.b_month_ret.index)
        self.s_month_ret = self.s_month_ret.loc[common_mr]
        self.b_month_ret = self.b_month_ret.loc[common_mr]
        self.strategy_name = strategy_name
        self.benchmark_name = benchmark_name
        self.s_full_nav = s
        self.b_full_nav = b
        self.week_labels = pd.Series(
            _classify_crisis(self.b_week_ret.values, self.s_week_ret.values),
            index=self.b_week_ret.index,
        )

    def _nav(self):
        return (
            self.s_orig_nav / self.s_orig_nav.iloc[0],
            self.b_orig_nav / self.b_orig_nav.iloc[0],
        )

    def drawdown_series(self, which: str = "b") -> pd.Series:
        nav = self.b_full_nav if which == "b" else self.s_full_nav
        nav = nav / nav.iloc[0]
        return nav / nav.cummax() - 1

    def quadrant_analysis(self, crisis_threshold: Optional[float] = None) -> dict:
        thr = CRISIS_THRESHOLD if crisis_threshold is None else crisis_threshold
        s = self.s_week_ret
        b = self.b_week_ret
        down = b < thr
        n_down = int(down.sum())
        n_total = int(len(b))
        labels = self.week_labels[down]
        s_c = s[down]
        b_c = b[down]
        counts = {cat: int((labels == cat).sum()) for cat in CRISIS_CATS}
        avg_ret = {}
        for cat in CRISIS_CATS:
            v = s_c[labels == cat]
            avg_ret[cat] = float(v.mean()) if len(v) > 0 else 0.0
        n_up = counts["逆势上涨"]
        n_alpha = counts["逆势上涨"] + counts["跌得少"]
        n_follow = counts["跟着跌"] + counts["跌更多"]
        up_rate = n_up / n_down if n_down else 0.0
        alpha_ratio = n_alpha / n_down if n_down else 0.0
        follow_ratio = n_follow / n_down if n_down else 0.0
        win_avg = float(s_c[s_c > 0].mean()) if n_up > 0 else 0.0
        loss_vals = s_c[s_c <= 0]
        loss_avg = float(abs(loss_vals.mean())) if len(loss_vals) > 0 else 0.0
        odds = (win_avg / loss_avg) if loss_avg > 1e-12 else 0.0
        return {
            "n_crisis": n_down,
            "n_down": n_down,
            "n_total": n_total,
            "hit_rate": up_rate,
            "up_rate": up_rate,
            "alpha_ratio": alpha_ratio,
            "follow_ratio": follow_ratio,
            "odds": odds,
            "counts": counts,
            "avg_ret": avg_ret,
            "down_avg_s": float(s_c.mean()) if n_down else 0.0,
            "down_avg_b": float(b_c.mean()) if n_down else 0.0,
            "win_avg": win_avg,
            "loss_avg": loss_avg,
        }

    def condition_tests(self) -> List[dict]:
        out = []
        for thr in DEEP_WEEK_TESTS:
            mask = self.b_week_ret < thr
            n = int(mask.sum())
            if n == 0:
                out.append(
                    {"threshold": thr, "n": 0, "b_avg": 0.0, "s_avg": 0.0, "up_rate": 0.0}
                )
                continue
            out.append(
                {
                    "threshold": thr,
                    "n": n,
                    "b_avg": float(self.b_week_ret[mask].mean()),
                    "s_avg": float(self.s_week_ret[mask].mean()),
                    "up_rate": float((self.s_week_ret[mask] > 0).mean()),
                }
            )
        return out

    def extreme_drops_top_n(self, n: Optional[int] = None) -> pd.DataFrame:
        count = n or EXTREME_TOP_N
        df = pd.DataFrame({"基准": self.b_week_ret, "策略": self.s_week_ret})
        df = df.nsmallest(count, "基准").sort_values("基准", ascending=False)
        df["超额"] = df["策略"] - df["基准"]
        df["分类"] = self.week_labels.loc[df.index]
        return df

    def weekly_detail_df(self) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "基准收益": self.b_week_ret,
                "策略收益": self.s_week_ret,
                "超额": self.s_week_ret - self.b_week_ret,
            }
        )
        df["分类"] = self.week_labels
        return df

    def down_week_df(self) -> pd.DataFrame:
        df = self.weekly_detail_df()
        return df[df["基准收益"] < 0].sort_index(ascending=False)

    def deep_drawdown_events(self, threshold: Optional[float] = None) -> List[dict]:
        thr = DEEP_DD_THRESHOLD if threshold is None else threshold
        b_nav = self.b_full_nav / self.b_full_nav.iloc[0]
        dd = b_nav / b_nav.cummax() - 1
        in_dd = dd < -1e-10
        events = []
        idx = b_nav.index
        arr = in_dd.values
        i = 0
        n_arr = len(arr)
        while i < n_arr:
            if not arr[i]:
                i += 1
                continue
            j = i
            while j + 1 < n_arr and arr[j + 1]:
                j += 1
            peak_pos = i - 1
            if peak_pos < 0:
                i = j + 1
                continue
            peak_date = idx[peak_pos]
            seg = b_nav.iloc[i : j + 1]
            trough_date = seg.idxmin()
            min_dd = float(seg.loc[trough_date] / b_nav.loc[peak_date] - 1)
            if min_dd < thr:
                b_ret = float(
                    self.b_full_nav.loc[trough_date] / self.b_full_nav.loc[peak_date] - 1
                )
                s_ret = float(
                    self.s_full_nav.loc[trough_date] / self.s_full_nav.loc[peak_date] - 1
                )
                hedged = s_ret > 0
                events.append(
                    {
                        "start": peak_date,
                        "end": trough_date,
                        "benchmark_ret": b_ret,
                        "strategy_ret": s_ret,
                        "excess": s_ret - b_ret,
                        "hedged": hedged,
                        "verdict": "逆势对冲" if hedged else "同步下跌",
                        "max_dd": min_dd,
                        "days": int((trough_date - peak_date).days),
                    }
                )
            i = j + 1
        return events

    def drawdown_shade_spans(self, threshold: Optional[float] = None) -> List[tuple]:
        thr = DEEP_DD_THRESHOLD if threshold is None else threshold
        dd = self.drawdown_series("b")
        mask = dd < thr
        spans = []
        if not mask.any():
            return spans
        idx = mask.index
        arr = mask.values
        i = 0
        n_arr = len(arr)
        while i < n_arr:
            if not arr[i]:
                i += 1
                continue
            j = i
            while j + 1 < n_arr and arr[j + 1]:
                j += 1
            spans.append((idx[i], idx[j]))
            i = j + 1
        return spans

    def rolling_correlation(self, window: Optional[int] = None) -> pd.Series:
        w = window or ROLLING_CORR_WINDOW
        return self.s_week_ret.rolling(w).corr(self.b_week_ret).dropna()

    def expanding_correlation(self, min_periods: int = 8) -> pd.Series:
        return self.s_week_ret.expanding(min_periods=min_periods).corr(
            self.b_week_ret
        ).dropna()

    def cost_analysis(self) -> dict:
        s = self.s_week_ret
        b = self.b_week_ret
        up = b > 0
        n_up = int(up.sum())
        if n_up:
            drag = float((s[up] < 0).mean())
            sync_up = float((s[up] > 0).mean())
            b_up_mean = float(b[up].mean())
            elasticity = float(s[up].mean() / b_up_mean) if abs(b_up_mean) > 1e-12 else 0.0
        else:
            drag = 0.0
            sync_up = 0.0
            elasticity = 0.0
        down = b < 0
        if int(down.sum()) >= 5:
            tail_corr = float(s[down].corr(b[down]))
        else:
            tail_corr = 0.0
        q1_cut = b.quantile(0.25)
        q1_mask = b <= q1_cut
        if int(q1_mask.sum()) >= 5:
            q1_corr = float(s[q1_mask].corr(b[q1_mask]))
        else:
            q1_corr = 0.0
        return {
            "drag": drag,
            "sync_up": sync_up,
            "elasticity": elasticity,
            "tail_corr": tail_corr,
            "q1_corr": q1_corr,
            "n_up": n_up,
        }

    def performance_stats(self) -> dict:
        s_nav, b_nav = self._nav()
        days = max((s_nav.index[-1] - s_nav.index[0]).days, 1)
        years = days / 365.25

        def _stats(nav: pd.Series, week_ret: pd.Series) -> dict:
            cum = float(nav.iloc[-1] / nav.iloc[0] - 1)
            ann = (
                float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1)
                if years > 0
                else 0.0
            )
            vol = float(week_ret.std() * math.sqrt(WEEKS_PER_YEAR))
            dd = nav / nav.cummax() - 1
            mdd = float(dd.min())
            return {"cum": cum, "ann": ann, "vol": vol, "mdd": mdd}

        return {
            "s": _stats(s_nav, self.s_week_ret),
            "b": _stats(b_nav, self.b_week_ret),
            "corr": float(self.s_week_ret.corr(self.b_week_ret)),
            "start": s_nav.index[0],
            "end": s_nav.index[-1],
        }

    def monthly_analysis(self) -> dict:
        s_m = self.s_month_ret
        b_m = self.b_month_ret
        big = MONTH_BIG_MOVE
        states = pd.Series(index=b_m.index, dtype="object")
        states[b_m <= -big] = "大跌"
        states[(b_m > -big) & (b_m < 0)] = "小跌"
        states[(b_m >= 0) & (b_m < big)] = "小涨"
        states[b_m >= big] = "大涨"
        out = {}
        for state in ("大涨", "小涨", "小跌", "大跌"):
            mask = states == state
            n = int(mask.sum())
            if n == 0:
                out[state] = {"n": 0, "s_avg": 0.0, "b_avg": 0.0, "win_rate": 0.0}
            else:
                out[state] = {
                    "n": n,
                    "s_avg": float(s_m[mask].mean()),
                    "b_avg": float(b_m[mask].mean()),
                    "win_rate": float((s_m[mask] > 0).mean()),
                }
        m_labels = _classify_crisis(b_m.values, s_m.values)
        down_mask = (b_m.values < 0).tolist()
        hedged = [lbl in _HEDGE_CATS for lbl, is_down in zip(m_labels, down_mask) if is_down]
        hedge_ratio = _mean([1.0 if h else 0.0 for h in hedged]) if hedged else 0.0
        big_mask = (b_m <= -big).tolist()
        big_down = pd.DataFrame(
            {
                "日期": [i for i, flag in zip(s_m.index, big_mask) if flag],
                "基准": [float(v) for v, flag in zip(b_m.values, big_mask) if flag],
                "策略": [float(v) for v, flag in zip(s_m.values, big_mask) if flag],
            }
        )
        if len(big_down):
            big_down = big_down.sort_values("日期", ascending=False)
        return {
            "states": out,
            "hedge_ratio": hedge_ratio,
            "n_down_month": int(sum(down_mask)),
            "big_down": big_down,
        }

    def annual_stats(self) -> List[dict]:
        rows = []
        s_g = self.s_week_ret.groupby(self.s_week_ret.index.year)
        b_g = self.b_week_ret.groupby(self.b_week_ret.index.year)
        lab_g = self.week_labels.groupby(self.week_labels.index.year)
        years = sorted(set(s_g.groups.keys()) & set(b_g.groups.keys()))
        for yr in years:
            s_y = s_g.get_group(yr)
            b_y = b_g.get_group(yr)
            lab_y = lab_g.get_group(yr)
            down = (b_y < 0).tolist()
            n_down = int(sum(down))
            if n_down:
                flags = [
                    1.0 if lbl in _HEDGE_CATS else 0.0
                    for lbl, is_down in zip(lab_y.tolist(), down)
                    if is_down
                ]
                alpha = _mean(flags)
            else:
                alpha = 1.0
            rows.append(
                {
                    "年份": int(yr),
                    "下跌周": n_down,
                    "危机Alpha": alpha,
                    "策略收益": float((1 + s_y).prod() - 1),
                    "基准收益": float((1 + b_y).prod() - 1),
                    "完整": len(b_y) >= FULL_YEAR_WEEKS,
                }
            )
        return rows

    def composite_score(self, multi_bench_alpha_ratios: Optional[List[float]] = None) -> dict:
        q = self.quadrant_analysis()
        cost = self.cost_analysis()
        events = self.deep_drawdown_events()
        top = self.extreme_drops_top_n()
        mo = self.monthly_analysis()
        tests = self.condition_tests()
        corr = float(self.s_week_ret.corr(self.b_week_ret))
        r = q["alpha_ratio"]
        s_alpha = _clip(50 + (r - 0.5) * 118)
        deep_items = [t for t in tests if t["n"] > 0]
        if deep_items:
            up_part = _mean([t["up_rate"] for t in deep_items])
            alpha_part_vals = []
            for t in deep_items:
                mask = self.b_week_ret < t["threshold"]
                lab = self.week_labels[mask]
                alpha_part_vals.append(
                    _mean([1.0 if lbl in _HEDGE_CATS else 0.0 for lbl in lab.tolist()])
                )
            alpha_part = _mean(alpha_part_vals)
            s_deep = _clip(30 + up_part * 45 + alpha_part * 30)
        else:
            s_deep = 50.0
        s_tail = _clip(50 - cost["tail_corr"] * 175)
        if events:
            s_dd = _clip(
                100 * _mean([1.0 if e["hedged"] else 0.0 for e in events])
            )
        else:
            s_dd = 50.0
        if multi_bench_alpha_ratios:
            m = _mean(multi_bench_alpha_ratios)
            s_multi = _clip(40 + (m - 0.5) * 150)
        else:
            s_multi = _clip(40 + (r - 0.5) * 150)
        s_month = _clip(100 * mo["hedge_ratio"])
        if len(top):
            beat = float((top["策略"] > top["基准"]).mean())
            upr = float((top["策略"] > 0).mean())
            s_extreme = _clip(60 * beat + 40 * upr)
        else:
            s_extreme = 50.0
        sc_sync = _clip((cost["sync_up"] - 0.3) / 0.6 * 100)
        sc_elas = _clip(cost["elasticity"] / 0.3 * 100)
        s_cost = _clip(0.4 * sc_sync + 0.6 * sc_elas)
        s_low = _clip(50 - corr * 50)
        dims_raw = [
            ("危机Alpha占比", "核心", 0.20, s_alpha),
            ("深跌周抗跌性", "核心", 0.15, s_deep),
            ("下尾负相关", "对冲", 0.12, s_tail),
            ("回撤独立性", "对冲", 0.12, s_dd),
            ("多宽基一致性", "稳健", 0.10, s_multi),
            ("月度危机Alpha", "稳健", 0.10, s_month),
            ("极端事件保护", "核心", 0.10, s_extreme),
            ("上涨周代价", "代价", 0.06, s_cost),
            ("整体低相关", "对冲", 0.05, s_low),
        ]
        dimensions = []
        for name, typ, weight, score in dims_raw:
            score_r = round(score, 1)
            dimensions.append(
                {
                    "维度": name,
                    "类型": typ,
                    "权重": weight,
                    "得分": score_r,
                    "评级": _rating(score_r),
                    "贡献": round(score_r * weight, 1),
                }
            )
        total = round(sum(d["得分"] * d["权重"] for d in dimensions), 1)
        return {"total_score": total, "rating": _rating(total), "dimensions": dimensions}

    def summary(self, multi_bench_alpha_ratios: Optional[List[float]] = None) -> dict:
        return {
            "strategy": self.strategy_name,
            "benchmark": self.benchmark_name,
            "n_weeks": int(len(self.b_week_ret)),
            "quadrant": self.quadrant_analysis(),
            "extreme_top": self.extreme_drops_top_n(),
            "deep_dd_events": self.deep_drawdown_events(),
            "rolling_corr": self.rolling_correlation(),
            "monthly": self.monthly_analysis(),
            "annual": self.annual_stats(),
            "cost": self.cost_analysis(),
            "performance": self.performance_stats(),
            "conditions": self.condition_tests(),
            "score": self.composite_score(multi_bench_alpha_ratios),
            "weekly_detail": self.weekly_detail_df(),
        }


def _frame_records(df: pd.DataFrame) -> List[dict]:
    out = []
    for idx, row in zip(df.index, df.to_dict(orient="records")):
        rec = {"日期": idx}
        rec.update(row)
        out.append(rec)
    return out


def sanitize_for_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return sanitize_for_json(float(value))
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Series):
        return [
            {"date": sanitize_for_json(idx), "value": sanitize_for_json(v)}
            for idx, v in value.items()
        ]
    if isinstance(value, pd.DataFrame):
        return sanitize_for_json(_frame_records(value))
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize_for_json(item())
        except (TypeError, ValueError):
            pass
    return value


def build_crisis_alpha_payload(
    strategy_nav: pd.Series,
    main_code: str,
    main_name: str,
    main_closes: pd.Series,
    benchmark_closes: Dict[str, pd.Series],
    strategy_name: str,
) -> dict:
    an = CrisisAlphaAnalyzer(
        strategy_nav, main_closes,
        strategy_name=strategy_name, benchmark_name=main_name,
    )
    q_main = an.quadrant_analysis()
    multi_rows = [
        {
            "key": "main",
            "name": main_name,
            "code": main_code,
            "alpha_ratio": q_main["alpha_ratio"],
            "n_down": q_main["n_down"],
            "up_rate": q_main["up_rate"],
            "is_main": True,
        }
    ]
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == main_code:
            continue
        series = benchmark_closes.get(spec["code"])
        if series is None or len(series) == 0:
            continue
        try:
            an_b = CrisisAlphaAnalyzer(
                strategy_nav, series,
                strategy_name=strategy_name, benchmark_name=spec["name"],
            )
        except (TypeError, ValueError):
            continue
        q = an_b.quadrant_analysis()
        multi_rows.append(
            {
                "key": spec["key"],
                "name": spec["name"],
                "code": spec["code"],
                "alpha_ratio": q["alpha_ratio"],
                "n_down": q["n_down"],
                "up_rate": q["up_rate"],
                "is_main": False,
            }
        )
    summary = an.summary([row["alpha_ratio"] for row in multi_rows])
    s_nav, b_nav = an._nav()
    s_map = {idx: float(v) for idx, v in s_nav.items()}
    b_map = {idx: float(v) for idx, v in b_nav.items()}
    dates = sorted(set(s_map) | set(b_map))
    payload = {
        "strategy_name": strategy_name,
        "benchmark_code": main_code,
        "benchmark_name": main_name,
        "alpha_rating": alpha_rating(summary["quadrant"]["alpha_ratio"]),
        "n_weeks": summary["n_weeks"],
        "quadrant": summary["quadrant"],
        "conditions": summary["conditions"],
        "extreme_top": _frame_records(summary["extreme_top"]),
        "deep_dd_events": summary["deep_dd_events"],
        "rolling_corr": summary["rolling_corr"],
        "expanding_corr": an.expanding_correlation(),
        "monthly": {
            "states": summary["monthly"]["states"],
            "hedge_ratio": summary["monthly"]["hedge_ratio"],
            "n_down_month": summary["monthly"]["n_down_month"],
            "big_down": _frame_records(summary["monthly"]["big_down"]),
        },
        "annual": summary["annual"],
        "cost": summary["cost"],
        "performance": summary["performance"],
        "score": summary["score"],
        "weekly_detail": _frame_records(summary["weekly_detail"]),
        "down_weeks": _frame_records(an.down_week_df()),
        "nav": {
            "dates": dates,
            "strategy": [s_map.get(d) for d in dates],
            "benchmark": [b_map.get(d) for d in dates],
            "shade_spans": [[s, e] for s, e in an.drawdown_shade_spans()],
        },
        "multi_bench": multi_rows,
    }
    return sanitize_for_json(payload)
