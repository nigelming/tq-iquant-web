# -*- coding: utf-8 -*-
"""市场状态×绩效分析器（FR-7 ~ FR-10）：机遇Alpha / 中性Alpha / 反弹弹性。

与 ``crisis_alpha.py`` 同层同风格。输入策略/基准日频净值、成交流水
（trades）与 ``regime.segment_regime`` 的周频四态标签（或其完整返回
dict），``analyze()`` 输出四块 JSON 安全 payload：

- ``regime_table``  市场状态×绩效总表，四态齐全，每态：周数/占比/策略
  累计收益/基准累计收益/超额/周胜率/态内最大回撤/年化换手率/成本占比
- ``opportunity``   机遇Alpha（趋势上涨）：上涨捕获率、大涨周条件检验
  （+3%/+5%，镜像危机阈值）、踏空周占比、上涨跟涨四档（同步涨/涨得少/
  踏空/逆势跌）计数与均值
- ``rebound``       反弹弹性：反弹段列表（自近端低点的段内基准/策略反弹
  幅、弹性=策略反弹幅/基准反弹幅、基准与策略各自收复前高的自然日数，
  未收复 recovered=False）
- ``neutral``       中性Alpha（震荡）：震荡周数/占比/累计/年化/胜率、
  月均交易笔数与年化换手、成本/毛利比、假突破磨损次数、趋势 vs 震荡
  平均单笔盈亏

口径约定（固化，TR-7.2 交叉断言依据，勿随手感改）
------------------------------------------------
- 周收益 = W-FRI 周收盘 ``pct_change``；状态按周的周五日期查 regime 标签，
  regime_weeks 未覆盖的周不参与任何状态统计。
- 状态累计收益 = 该态各周 (1+周收益) 连乘 - 1（跨段拼接近似）。
- 态内最大回撤 = 仅取标签为该态的交易日，日净值对态内运行高点的最大回撤。
- 年化换手率（分态/震荡）= 该态交易日双边成交额（BUY+SELL amount 全计）
  / 该态日均总净值 / 自然年数（该态首末交易日天数差 / 365.25）。
- 成本占比（分态）= 该态交易日（佣金+印花税）/ 该态首日总净值。
- 成本/毛利比（震荡）= 震荡日（佣金+印花税）/ 震荡日平仓盈利交易的毛
  PnL 之和（FIFO 配对价差，未扣费用）；毛利 ≤ 0 时为 None。
- 假突破磨损 = 买入开仓后 ≤ ``WHIPSAW_DAYS`` 个交易日内同票平仓且毛
  PnL ≤ 0，按平仓日所处状态归属；卖出超过持仓部分忽略。
- 踏空周 = 基准周涨（>0）而策略周收益 ≤ 0（含持平，近似空仓未参与）。
- 上涨四档（基准周涨 >0 的 bull 周，与危机四档 0.5 比例镜像对称）：
  s ≥ 0.5b 同步涨；0 < s < 0.5b 涨得少；-0.5b < s ≤ 0 踏空；s ≤ -0.5b 逆势跌。

样本不足安全性：无某态样本 → 该态周数 0、比率/收益类字段 None；共同
交易日不足或输入为空 → 全 payload 安全空值，不抛错。纯 numpy/pandas。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.engine.regime import (
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_LABELS_CN,
    REGIME_RANGE,
    REGIME_REBOUND,
)

WEEKS_PER_YEAR = 52
DAYS_PER_YEAR = 365.25
MONTH_DAYS = 30.4375          # 自然月平均天数

BIG_UP_TESTS = (0.03, 0.05)   # 大涨周条件检验阈值（镜像危机 -3%/-5%）
SYNC_RATIO = 0.5              # 同步涨/涨得少 与 踏空/逆势跌 的分界比例
WHIPSAW_DAYS = 5              # 假突破磨损：开仓后 N 个交易日内小亏/止损平仓

STATE_ORDER = ("bull", "bear", "range", "rebound")
TREND_STATES = ("bull", "bear", "rebound")   # 单笔盈亏对比中的「趋势」组
OPP_CATS = ("同步涨", "涨得少", "踏空", "逆势跌")


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _compound(rets: List[float]) -> float:
    r = 1.0
    for x in rets:
        r *= 1.0 + x
    return r - 1.0


def _dstr(d) -> Optional[str]:
    if d is None:
        return None
    return str(pd.Timestamp(d).date())


def _classify_opp(s: float, b: float) -> str:
    """上涨周四档分类（b > 0），见模块 docstring 口径。"""
    if s > 0:
        return "同步涨" if s >= SYNC_RATIO * b else "涨得少"
    return "踏空" if s > -SYNC_RATIO * b else "逆势跌"


class RegimeAlphaAnalyzer:
    """市场状态×绩效分析器：机遇 / 反弹 / 中性 + 状态总表。"""

    def __init__(
        self,
        strategy_nav: pd.Series,
        benchmark_price: pd.Series,
        trades: Optional[List[Any]] = None,
        regime_weeks: Optional[Any] = None,
        strategy_name: str = "策略",
        benchmark_name: str = "基准",
    ):
        if not isinstance(strategy_nav, pd.Series) or not isinstance(
                benchmark_price, pd.Series):
            raise TypeError("strategy_nav 与 benchmark_price 必须是 pd.Series")
        self.strategy_name = strategy_name
        self.benchmark_name = benchmark_name

        s = strategy_nav.copy().sort_index().astype(float)
        b = benchmark_price.copy().sort_index().astype(float)
        s.index = pd.to_datetime(s.index).normalize()
        b.index = pd.to_datetime(b.index).normalize()
        common = s.index.intersection(b.index)
        self.s_nav = s.loc[common]
        self.b_nav = b.loc[common]
        self.daily_index = self.s_nav.index
        self.day_idx = {ts: i for i, ts in enumerate(self.daily_index)}

        weeks = regime_weeks
        if isinstance(weeks, dict) and "weeks" in weeks:
            weeks = weeks["weeks"]
        self.regime_weeks = self._normalize_weeks(weeks or [])

        # 周五日期 → 状态标签；日频日期 → 状态标签
        self.label_by_friday = {
            w["date"]: w["label"] for w in self.regime_weeks if w["label"]
        }
        self.label_of_day = {
            d: self.label_by_friday.get(self._friday(d))
            for d in self.daily_index
        }

        # 周频净值与周收益（W-FRI 与 regime 分段器对齐）
        self.s_w = self.s_nav.resample("W-FRI").last().dropna()
        self.b_w = self.b_nav.resample("W-FRI").last().dropna()
        common_w = self.s_w.index.intersection(self.b_w.index)
        self.s_w = self.s_w.loc[common_w]
        self.b_w = self.b_w.loc[common_w]
        self.s_week_ret = self.s_w.pct_change().dropna()
        self.b_week_ret = self.b_w.pct_change().dropna()
        common_r = self.s_week_ret.index.intersection(self.b_week_ret.index)
        self.s_week_ret = self.s_week_ret.loc[common_r]
        self.b_week_ret = self.b_week_ret.loc[common_r]

        self.trades = self._normalize_trades(trades or [])
        self.events = self._realize()   # FIFO 平仓事件（毛 PnL / 磨损）

    # ------------------------------------------------------------------
    # 输入归一化
    # ------------------------------------------------------------------
    @staticmethod
    def _friday(ts) -> pd.Timestamp:
        """ISO 周的周五（与 regime._to_weekly、W-FRI 标签对齐）。"""
        ts = pd.Timestamp(ts)
        return (ts + pd.Timedelta(days=5 - ts.isoweekday())).normalize()

    @staticmethod
    def _normalize_weeks(weeks) -> List[dict]:
        out = []
        for w in weeks:
            d = w.get("date")
            if d is None:
                continue
            out.append({
                "date": pd.Timestamp(d).normalize(),
                "close": float(w.get("close") or 0.0),
                "label": w.get("label"),
            })
        out.sort(key=lambda x: x["date"])
        return out

    @staticmethod
    def _tget(t: Any, key: str, default=None):
        if isinstance(t, dict):
            return t.get(key, default)
        return getattr(t, key, default)

    def _normalize_trades(self, trades) -> List[dict]:
        out = []
        for t in trades:
            bt = self._tget(t, "bar_time")
            if bt is None:
                continue
            out.append({
                "date": pd.Timestamp(bt).normalize(),
                "type": str(self._tget(t, "trade_type") or "").lower(),
                "price": float(self._tget(t, "price") or 0.0),
                "quantity": float(self._tget(t, "quantity") or 0.0),
                "amount": float(self._tget(t, "amount") or 0.0),
                "commission": float(self._tget(t, "commission") or 0.0),
                "stamp_duty": float(self._tget(t, "stamp_duty") or 0.0),
                "stock_code": str(self._tget(t, "stock_code") or ""),
            })
        out.sort(key=lambda x: x["date"])
        return out

    # ------------------------------------------------------------------
    # FIFO 平仓配对（毛 PnL，未扣费用）
    # ------------------------------------------------------------------
    def _realize(self) -> List[dict]:
        events: List[dict] = []
        lots: Dict[str, List[dict]] = {}
        for tr in self.trades:
            di = self.day_idx.get(tr["date"])
            if di is None:
                continue  # 不在共同交易日内的成交不参与配对与归属
            if tr["type"] == "buy":
                lots.setdefault(tr["stock_code"], []).append({
                    "price": tr["price"], "qty": tr["quantity"], "idx": di,
                })
            elif tr["type"] == "sell":
                need = tr["quantity"]
                queue = lots.get(tr["stock_code"], [])
                pnl = 0.0
                first_idx: Optional[int] = None
                while need > 1e-9 and queue:
                    lot = queue[0]
                    if first_idx is None:
                        first_idx = lot["idx"]
                    matched = min(lot["qty"], need)
                    pnl += (tr["price"] - lot["price"]) * matched
                    lot["qty"] -= matched
                    need -= matched
                    if lot["qty"] <= 1e-9:
                        queue.pop(0)
                if first_idx is None:
                    continue  # 无持仓可平（卖超部分忽略）
                events.append({
                    "pnl": pnl,
                    "close_idx": di,
                    "close_date": tr["date"],
                    "span": di - first_idx,   # 交易日距离
                    "code": tr["stock_code"],
                })
        return events

    # ------------------------------------------------------------------
    # 公共查询
    # ------------------------------------------------------------------
    def _week_label(self, ts) -> Optional[str]:
        return self.label_by_friday.get(self._friday(ts))

    def _labeled_week_rets(self) -> List[Tuple[pd.Timestamp, str, float, float]]:
        out = []
        for ts in self.s_week_ret.index:
            lbl = self._week_label(ts)
            if lbl is None:
                continue
            out.append((ts, lbl, float(self.s_week_ret.loc[ts]),
                        float(self.b_week_ret.loc[ts])))
        return out

    def _days_of_state(self, state: str) -> List[pd.Timestamp]:
        return [d for d in self.daily_index if self.label_of_day.get(d) == state]

    def _state_turnover_cost(
            self, days: List[pd.Timestamp]) -> Tuple[Optional[float], Optional[float]]:
        """（年化换手率, 成本占比），口径见模块 docstring。"""
        if not days:
            return None, None
        day_set = set(days)
        amount = cost = 0.0
        for tr in self.trades:
            if tr["date"] in day_set:
                amount += tr["amount"]
                cost += tr["commission"] + tr["stamp_duty"]
        navs = [float(self.s_nav.loc[d]) for d in days]
        avg_total = _mean(navs)
        first_total = navs[0]
        years = (days[-1] - days[0]).days / DAYS_PER_YEAR
        turnover = (
            amount / avg_total / years
            if avg_total > 0 and years > 0 else None
        )
        cost_ratio = cost / first_total if first_total > 0 else None
        return turnover, cost_ratio

    # ------------------------------------------------------------------
    # 四块输出
    # ------------------------------------------------------------------
    def analyze(self) -> dict:
        labeled = self._labeled_week_rets()
        return {
            "meta": self._meta(labeled),
            "regime_table": self._regime_table(labeled),
            "opportunity": self._opportunity(labeled),
            "rebound": self._rebound(),
            "neutral": self._neutral(labeled),
        }

    def _meta(self, labeled) -> dict:
        counts = {st: 0 for st in STATE_ORDER}
        for _, lbl, _, _ in labeled:
            counts[lbl] += 1
        return {
            "strategy_name": self.strategy_name,
            "benchmark_name": self.benchmark_name,
            "n_weeks_labeled": len(labeled),
            "n_weeks_total": int(len(self.s_week_ret)),
            "state_week_counts": counts,
        }

    @staticmethod
    def _empty_state(state: str) -> dict:
        return {
            "label": state,
            "label_cn": REGIME_LABELS_CN.get(state, state),
            "weeks": 0,
            "ratio": 0.0,
            "strategy_return": None,
            "benchmark_return": None,
            "excess_return": None,
            "win_rate": None,
            "max_drawdown": None,
            "turnover": None,
            "cost_ratio": None,
        }

    def _regime_table(self, labeled) -> dict:
        total = len(labeled)
        table = {}
        for state in STATE_ORDER:
            rows = [x for x in labeled if x[1] == state]
            blk = self._empty_state(state)
            blk["weeks"] = len(rows)
            blk["ratio"] = len(rows) / total if total else 0.0
            if rows:
                s_rets = [x[2] for x in rows]
                b_rets = [x[3] for x in rows]
                s_cum = _compound(s_rets)
                b_cum = _compound(b_rets)
                blk["strategy_return"] = s_cum
                blk["benchmark_return"] = b_cum
                blk["excess_return"] = s_cum - b_cum
                blk["win_rate"] = sum(1 for r in s_rets if r > 0) / len(s_rets)
                blk["max_drawdown"] = self._state_max_drawdown(state)
                blk["turnover"], blk["cost_ratio"] = (
                    self._state_turnover_cost(self._days_of_state(state)))
            table[state] = blk
        return table

    def _state_max_drawdown(self, state: str) -> float:
        peak: Optional[float] = None
        dd = 0.0
        for d in self._days_of_state(state):
            v = float(self.s_nav.loc[d])
            peak = v if peak is None else max(peak, v)
            if peak > 0:
                dd = min(dd, v / peak - 1.0)
        return dd

    def _opportunity(self, labeled) -> dict:
        out = {
            "n_bull_weeks": 0,
            "up_capture": None,
            "miss_ratio": 0.0,
            "big_up_tests": [],
            "cats": {c: {"count": 0, "avg_strategy": None} for c in OPP_CATS},
        }
        rows = [x for x in labeled if x[1] == REGIME_BULL]
        out["n_bull_weeks"] = len(rows)
        if not rows:
            for thr in BIG_UP_TESTS:
                out["big_up_tests"].append({
                    "threshold": thr, "n": 0, "avg_strategy": None,
                    "avg_benchmark": None, "win_rate": None,
                })
            return out
        s_vals = [x[2] for x in rows]
        b_vals = [x[3] for x in rows]
        mean_b = _mean(b_vals)
        if abs(mean_b) > 1e-12:
            out["up_capture"] = _mean(s_vals) / mean_b

        up_b = [(sv, bv) for sv, bv in zip(s_vals, b_vals) if bv > 0]
        if up_b:
            out["miss_ratio"] = (
                sum(1 for sv, _ in up_b if sv <= 0) / len(up_b))

        for thr in BIG_UP_TESTS:
            sel = [(sv, bv) for sv, bv in zip(s_vals, b_vals) if bv >= thr]
            out["big_up_tests"].append({
                "threshold": thr,
                "n": len(sel),
                "avg_strategy": _mean([sv for sv, _ in sel]) if sel else None,
                "avg_benchmark": _mean([bv for _, bv in sel]) if sel else None,
                "win_rate": (
                    sum(1 for sv, _ in sel if sv > 0) / len(sel)
                    if sel else None),
            })

        buckets: Dict[str, List[float]] = {c: [] for c in OPP_CATS}
        for sv, bv in zip(s_vals, b_vals):
            if bv <= 0:
                continue  # 四档仅对基准周涨 >0 的 bull 周
            buckets[_classify_opp(sv, bv)].append(sv)
        for cat, vals in buckets.items():
            out["cats"][cat] = {
                "count": len(vals),
                "avg_strategy": _mean(vals) if vals else None,
            }
        return out

    def _rebound(self) -> dict:
        out: dict = {"n_segments": 0, "avg_elasticity": None, "segments": []}
        weeks = self.regime_weeks
        runs: List[Tuple[int, int]] = []
        cur: Optional[List[int]] = None
        for i, w in enumerate(weeks):
            if w["label"] == REGIME_REBOUND:
                if cur is None:
                    cur = [i, i]
                else:
                    cur[1] = i
            elif cur is not None:
                runs.append((cur[0], cur[1]))
                cur = None
        if cur is not None:
            runs.append((cur[0], cur[1]))
        if not runs:
            return out

        def s_at(i: int) -> Optional[float]:
            v = self.s_w.get(weeks[i]["date"])
            return float(v) if v is not None and pd.notna(v) else None

        elasticities: List[float] = []
        for a, z in runs:
            # 近端低点：自反弹段起点向前扫描连续 bear 周，取最低收盘
            trough_idx, bmin = a, weeks[a]["close"]
            t = a - 1
            while t >= 0 and weeks[t]["label"] == REGIME_BEAR:
                if weeks[t]["close"] < bmin:
                    bmin = weeks[t]["close"]
                    trough_idx = t
                t -= 1
            trough_date = weeks[trough_idx]["date"]

            seg = {
                "start_date": _dstr(weeks[a]["date"]),
                "end_date": _dstr(weeks[z]["date"]),
                "trough_date": _dstr(trough_date),
                "weeks": z - a + 1,
                "benchmark_rebound_pct": (
                    weeks[z]["close"] / bmin - 1.0 if bmin > 0 else None),
                "strategy_rebound_pct": None,
                "elasticity": None,
                "benchmark_recover_days": None,
                "benchmark_recovered": False,
                "strategy_recover_days": None,
                "strategy_recovered": False,
            }
            s_trough, s_end = s_at(trough_idx), s_at(z)
            if s_trough is not None and s_end is not None and s_trough > 0:
                seg["strategy_rebound_pct"] = s_end / s_trough - 1.0
            if (seg["benchmark_rebound_pct"] is not None
                    and abs(seg["benchmark_rebound_pct"]) > 1e-12
                    and seg["strategy_rebound_pct"] is not None):
                seg["elasticity"] = (
                    seg["strategy_rebound_pct"] / seg["benchmark_rebound_pct"])
                elasticities.append(seg["elasticity"])

            seg["benchmark_recover_days"], seg["benchmark_recovered"] = (
                self._recover_days(weeks, trough_idx,
                                   lambda i: weeks[i]["close"]))
            seg["strategy_recover_days"], seg["strategy_recovered"] = (
                self._recover_days(weeks, trough_idx, s_at))
            out["segments"].append(seg)

        out["n_segments"] = len(runs)
        out["avg_elasticity"] = _mean(elasticities) if elasticities else None
        return out

    @staticmethod
    def _recover_days(weeks, trough_idx: int, value_at) -> Tuple[Optional[int], bool]:
        """收复前高：前高 = 低点周（含）之前各周值最大值；返回
        (自低点周起收复所历自然日数, 是否收复)。"""
        pre = [value_at(i) for i in range(trough_idx + 1)]
        pre = [v for v in pre if v is not None]
        if not pre:
            return None, False
        peak = max(pre)
        trough_date = weeks[trough_idx]["date"]
        for j in range(trough_idx + 1, len(weeks)):
            v = value_at(j)
            if v is not None and v >= peak:
                return (weeks[j]["date"] - trough_date).days, True
        return None, False

    def _neutral(self, labeled) -> dict:
        out = {
            "n_range_weeks": 0,
            "range_ratio": 0.0,
            "cumulative_return": None,
            "annualized_return": None,
            "win_rate": None,
            "n_range_days": 0,
            "monthly_trades": None,
            "annual_turnover": None,
            "cost_gross_ratio": None,
            "whipsaw_count": 0,
            "whipsaw_days": WHIPSAW_DAYS,
            "avg_trade_pnl": {"trend": None, "trend_count": 0,
                              "range": None, "range_count": 0},
        }
        total = len(labeled)
        rows = [x for x in labeled if x[1] == REGIME_RANGE]
        out["n_range_weeks"] = len(rows)
        out["range_ratio"] = len(rows) / total if total else 0.0
        if rows:
            s_rets = [x[2] for x in rows]
            cum = _compound(s_rets)
            out["cumulative_return"] = cum
            n = len(s_rets)
            out["annualized_return"] = (
                (1.0 + cum) ** (WEEKS_PER_YEAR / n) - 1.0
                if n > 0 and (1.0 + cum) > 0 else None)
            out["win_rate"] = sum(1 for r in s_rets if r > 0) / n

        days = self._days_of_state(REGIME_RANGE)
        out["n_range_days"] = len(days)
        if not days:
            return out
        day_set = set(days)
        rng_trades = [tr for tr in self.trades if tr["date"] in day_set]
        amount = sum(tr["amount"] for tr in rng_trades)
        cost = sum(tr["commission"] + tr["stamp_duty"] for tr in rng_trades)

        navs = [float(self.s_nav.loc[d]) for d in days]
        avg_total = _mean(navs)
        span_days = (days[-1] - days[0]).days
        months = max(span_days, 1) / MONTH_DAYS
        out["monthly_trades"] = len(rng_trades) / months
        years = span_days / DAYS_PER_YEAR
        out["annual_turnover"] = (
            amount / avg_total / years
            if avg_total > 0 and years > 0 else None)

        # 平仓事件按平仓日状态归属
        rng_pnl: List[float] = []
        trend_pnl: List[float] = []
        whipsaw = 0
        for ev in self.events:
            lbl = self.label_of_day.get(ev["close_date"])
            if lbl == REGIME_RANGE:
                rng_pnl.append(ev["pnl"])
                if ev["span"] <= WHIPSAW_DAYS and ev["pnl"] <= 0:
                    whipsaw += 1
            elif lbl in TREND_STATES:
                trend_pnl.append(ev["pnl"])
        out["whipsaw_count"] = whipsaw
        gross = sum(p for p in rng_pnl if p > 0)
        out["cost_gross_ratio"] = cost / gross if gross > 1e-9 else None
        out["avg_trade_pnl"] = {
            "trend": _mean(trend_pnl) if trend_pnl else None,
            "trend_count": len(trend_pnl),
            "range": _mean(rng_pnl) if rng_pnl else None,
            "range_count": len(rng_pnl),
        }
        return out
