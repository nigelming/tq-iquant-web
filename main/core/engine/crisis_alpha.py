import math
from typing import List, Optional, Sequence

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
