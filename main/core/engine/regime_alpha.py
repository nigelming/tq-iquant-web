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

from core.engine.crisis_alpha import CRISIS_BENCHMARKS
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

# ---------------------------------------------------------------------------
# 风格轮动（FR-13 ~ FR-16）常量
# ---------------------------------------------------------------------------
# 6 宽基 → 风格组映射（复用 crisis_alpha.CRISIS_BENCHMARKS 的 6 只指数）
STYLE_GROUPS: Dict[str, List[str]] = {
    "大盘价值": ["000300.SH"],
    "中小盘": ["000905.SH", "000852.SH"],
    "科技成长": ["399006.SZ", "000688.SH"],
}
# 中证全指仅作普涨/普跌参考，不归属任何主导风格
STYLE_BROAD_INDEX = "000985.CSI"
STYLE_LABELS = ("大盘价值", "中小盘", "科技成长", "普涨", "普跌", "无主线")

STYLE_ROLL_WINDOW = 60        # 风格领导主窗口（60 周累计收益排名，稳定）
STYLE_FAST_WINDOW = 20        # 副窗口（20 周，用于近段确认/短样本回退）
STYLE_LOW_DISPERSION = 0.05   # 普涨判定：60 周累计收益截面 std < 5%
STYLE_NOLEADER_EDGE = 0.5     # 无主线判定：榜首与次席差距 < 0.5×截面 std

DISPERSION_WINDOW = 1         # 分化度 = 当期 6 指数周收益截面标准差（无需滚动）
ROLLING_EXPOSURE_WINDOW = 52  # 滚动相关/OLS beta 窗口（周）

# 巨潮四象限风格指数（规模×成长/价值，纯风格口径；TQ 可取，spike 见计划 0014 同批验证）
# 与 6 宽基的区别：宽基是规模+板块的混合载体，四象限把规模与成长/价值两维拆开，
# 用于隐含风格暴露的第二组回归（FR-16 增强），不参与风格领导标签判定。
QUADRANT_STYLE_INDICES: Tuple[Dict[str, str], ...] = (
    {"code": "399372.SZ", "name": "大盘成长"},
    {"code": "399373.SZ", "name": "大盘价值"},
    {"code": "399374.SZ", "name": "中盘成长"},
    {"code": "399375.SZ", "name": "中盘价值"},
    {"code": "399376.SZ", "name": "小盘成长"},
    {"code": "399377.SZ", "name": "小盘价值"},
)


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
        benchmark_closes: Optional[Dict[str, pd.Series]] = None,
        quadrant_closes: Optional[Dict[str, pd.Series]] = None,
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

        # 风格轮动 / 隐含暴露所需 6 宽基周频收益（FR-13 ~ FR-16）
        self.style_available_codes: List[str] = []
        self.style_skipped: List[str] = []
        self.style_week_rets: Dict[str, pd.Series] = {}
        self._prepare_style_benchmarks(benchmark_closes)

        # 巨潮四象限纯风格周频收益（FR-16 增强：第二组隐含暴露）
        self.quadrant_available_codes: List[str] = []
        self.quadrant_skipped: List[str] = []
        self.quadrant_week_rets: Dict[str, pd.Series] = {}
        (self.quadrant_available_codes, self.quadrant_skipped,
         self.quadrant_week_rets) = self._prepare_weekly_rets(
            quadrant_closes, QUADRANT_STYLE_INDICES)

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
    # 风格轮动：6 宽基周频收益对齐
    # ------------------------------------------------------------------
    def _prepare_style_benchmarks(
        self, benchmark_closes: Optional[Dict[str, pd.Series]]
    ) -> None:
        """把 6 宽基日线收盘价转成与策略对齐的周频收益。"""
        (self.style_available_codes, self.style_skipped,
         self.style_week_rets) = self._prepare_weekly_rets(
            benchmark_closes, CRISIS_BENCHMARKS)

    def _prepare_weekly_rets(
        self,
        closes: Optional[Dict[str, pd.Series]],
        specs,
    ) -> Tuple[List[str], List[str], Dict[str, pd.Series]]:
        """把 specs 指定的指数日线收盘价转成与策略对齐的周频收益。

        不可得指数记入 skipped（名称）；仅保留同时存在于策略周收益索引上的周。
        closes 为 None（未接线）时全部安全跳过且 skipped 不诬报；
        closes 为显式空 dict（已请求但全部缺失）时 specs 全部记入 skipped。
        """
        available: List[str] = []
        skipped: List[str] = []
        week_rets: Dict[str, pd.Series] = {}
        if closes is None:
            return available, skipped, week_rets
        ref_index = self.s_week_ret.index
        for spec in specs:
            code, name = spec["code"], spec["name"]
            series = closes.get(code)
            if series is None or not isinstance(series, pd.Series) or series.empty:
                skipped.append(name)
                continue
            wk = series.copy().sort_index().astype(float)
            wk.index = pd.to_datetime(wk.index).normalize()
            wk = wk.resample("W-FRI").last().dropna()
            wk_ret = wk.pct_change().dropna()
            common = wk_ret.index.intersection(ref_index)
            wk_ret = wk_ret.loc[common]
            if len(wk_ret) < 2:
                skipped.append(name)
                continue
            week_rets[code] = wk_ret
            available.append(code)
        return available, skipped, week_rets

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
            "style": self._style(labeled),
            "dispersion": self._dispersion(),
            "exposure": self._exposure(),
            "quadrant_exposure": self._quadrant_exposure(),
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

    # ------------------------------------------------------------------
    # FR-13 ~ FR-15：风格领导分段 + 风格×绩效矩阵 + 分化度
    # ------------------------------------------------------------------
    def _style_week_label(
        self, cum_rets: Dict[str, float]
    ) -> Optional[str]:
        """根据某周 6 宽基滚动累计收益截面判定风格标签。

        规则（见 spec FR-13）：
        - 全部为负 → 普跌
        - 全部为正 且 截面 std < STYLE_LOW_DISPERSION → 普涨
        - 否则取榜首指数所属风格组；榜首与次席差距 < 0.5×std → 无主线
        """
        if not cum_rets:
            return None
        vals = list(cum_rets.values())
        vmax = max(vals)
        vmin = min(vals)
        std = float(pd.Series(vals).std(ddof=0)) if len(vals) >= 2 else 0.0
        if vmax < 0:
            return "普跌"
        if vmin > 0 and std < STYLE_LOW_DISPERSION:
            return "普涨"
        # 找榜首
        leader_code = max(cum_rets, key=lambda k: cum_rets[k])
        leader_style = None
        for grp, codes in STYLE_GROUPS.items():
            if leader_code in codes:
                leader_style = grp
                break
        if leader_style is None:
            # 榜首是中证全指：若其明显领先则无主线，否则随次席风格
            return "无主线"
        # 无主线判定
        sorted_vals = sorted(vals, reverse=True)
        edge = sorted_vals[0] - sorted_vals[1] if len(sorted_vals) >= 2 else sorted_vals[0]
        if std > 1e-9 and edge < STYLE_NOLEADER_EDGE * std:
            return "无主线"
        return leader_style

    def _style(self, labeled) -> dict:
        """风格×绩效矩阵 + 每周风格标签序列。"""
        n_codes = len(self.style_available_codes)
        base = {
            "available_codes": self.style_available_codes,
            "skipped": self.style_skipped,
            "weeks": [],           # [{date, label, leader_code, leader_return}]
            "matrix": {
                lbl: {
                    "label": lbl,
                    "weeks": 0,
                    "ratio": 0.0,
                    "strategy_return": None,
                    "leader_return": None,
                    "excess_return": None,
                    "win_rate": None,
                    "max_drawdown": None,
                } for lbl in STYLE_LABELS
            },
        }
        if n_codes < 2 or not labeled:
            return base
        # 取所有可用指数周收益的共同周索引
        ref_index = self.s_week_ret.index
        common = ref_index
        for code in self.style_available_codes:
            common = common.intersection(self.style_week_rets[code].index)
        common = common.intersection(pd.Index(x[0] for x in labeled))
        if len(common) < STYLE_FAST_WINDOW:
            # 样本不足以做滚动，直接返回空矩阵
            return base

        # 每周的 6 指数滚动累计收益（FR-13 双窗口：主 60W 稳定，副 20W 近段确认）
        weekly_labels: List[Tuple[pd.Timestamp, str, Optional[str], float,
                                  str, Optional[str]]] = []
        for i, ts in enumerate(common):
            label, leader_code, leader_week_ret = self._style_label_at(
                common, i, ts, STYLE_ROLL_WINDOW)
            fast_label, fast_leader_code, _ = self._style_label_at(
                common, i, ts, STYLE_FAST_WINDOW)
            weekly_labels.append(
                (ts, label, leader_code, leader_week_ret,
                 fast_label, fast_leader_code))

        base["weeks"] = [
            {"date": _dstr(ts), "label": lbl,
             "leader_code": lc, "leader_return": lr,
             "fast_label": fl, "fast_leader_code": flc}
            for ts, lbl, lc, lr, fl, flc in weekly_labels
        ]

        # 风格分组聚合（以主窗口 60W 标签为准）
        s_by_date = {x[0]: x[2] for x in labeled}
        total = len(weekly_labels)
        for lbl in STYLE_LABELS:
            rows = [(ts, lc, lr) for ts, l, lc, lr, *_ in weekly_labels
                    if l == lbl]
            blk = base["matrix"][lbl]
            blk["weeks"] = len(rows)
            blk["ratio"] = len(rows) / total if total else 0.0
            if rows:
                s_rets = [s_by_date.get(ts, 0.0) for ts, _, _ in rows]
                l_rets = [lr for _, _, lr in rows]
                s_cum = _compound(s_rets)
                l_cum = _compound(l_rets)
                blk["strategy_return"] = s_cum
                blk["leader_return"] = l_cum
                blk["excess_return"] = s_cum - l_cum
                blk["win_rate"] = sum(1 for r in s_rets if r > 0) / len(s_rets)
                blk["max_drawdown"] = self._style_max_drawdown(
                    [ts for ts, _, _ in rows])
        # 双窗口一致性统计：20W 与 60W 标签一致的周占比（衡量主线稳定性）
        agree = sum(1 for t in weekly_labels if t[1] == t[4])
        base["fast_window"] = STYLE_FAST_WINDOW
        base["label_agree_ratio"] = agree / total if total else None
        return base

    def _style_label_at(
        self, common, i: int, ts: pd.Timestamp, window: int
    ) -> Tuple[str, Optional[str], float]:
        """单周风格标签（window 周滚动累计收益排名）；样本不足退化为可用窗口。"""
        win = min(window, i + 1)
        start = common[max(0, i + 1 - win)]
        cum: Dict[str, float] = {}
        for code in self.style_available_codes:
            seg = self.style_week_rets[code].loc[start:ts]
            if len(seg) < 2:
                continue
            cum[code] = float((1 + seg).prod() - 1.0)
        label = self._style_week_label(cum)
        if label is None:
            return "无主线", None, 0.0
        leader_code = max(cum, key=lambda k: cum[k]) if cum else None
        leader_week_ret = (
            float(self.style_week_rets[leader_code].loc[ts])
            if leader_code and ts in self.style_week_rets[leader_code].index
            else 0.0
        )
        return label, leader_code, leader_week_ret

    def _style_max_drawdown(self, week_ts_list: List[pd.Timestamp]) -> Optional[float]:
        """风格段最大回撤：取该风格涉及的周对应的交易日，净值对段内高点的最大回撤。"""
        if not week_ts_list:
            return None
        fri_set = {self._friday(ts) for ts in week_ts_list}
        days = [d for d in self.daily_index if self._friday(d) in fri_set]
        if not days:
            return None
        peak: Optional[float] = None
        dd = 0.0
        for d in days:
            v = float(self.s_nav.loc[d])
            peak = v if peak is None else max(peak, v)
            if peak > 0:
                dd = min(dd, v / peak - 1.0)
        return dd

    def _dispersion(self) -> dict:
        """宽基截面分化度时间序列 + 策略超额按分化度分桶对比。"""
        base = {
            "available_codes": self.style_available_codes,
            "skipped": self.style_skipped,
            "series": [],          # [{date, dispersion}]
            "buckets": {
                "low": {"count": 0, "mean_excess": None, "win_rate": None,
                        "threshold": None},
                "mid": {"count": 0, "mean_excess": None, "win_rate": None,
                        "threshold": None},
                "high": {"count": 0, "mean_excess": None, "win_rate": None,
                         "threshold": None},
            },
        }
        n_codes = len(self.style_available_codes)
        if n_codes < 2:
            return base
        ref_index = self.s_week_ret.index
        common = ref_index
        for code in self.style_available_codes:
            common = common.intersection(self.style_week_rets[code].index)
        common = common.intersection(self.b_week_ret.index)
        if len(common) < 6:
            return base

        series: List[Tuple[pd.Timestamp, float, float]] = []
        for ts in common:
            rets = [float(self.style_week_rets[c].loc[ts])
                    for c in self.style_available_codes
                    if ts in self.style_week_rets[c].index]
            if len(rets) < 2:
                continue
            disp = float(pd.Series(rets).std(ddof=0))
            s_ret = float(self.s_week_ret.loc[ts])
            b_ret = float(self.b_week_ret.loc[ts])
            series.append((ts, disp, s_ret - b_ret))
        if not series:
            return base
        disps = sorted(x[1] for x in series)
        n = len(disps)
        q1 = disps[n // 3]
        q2 = disps[(2 * n) // 3]
        base["series"] = [
            {"date": _dstr(ts), "dispersion": d} for ts, d, _ in series
        ]
        buckets = {"low": [], "mid": [], "high": []}
        for _, d, exc in series:
            if d <= q1:
                buckets["low"].append(exc)
            elif d <= q2:
                buckets["mid"].append(exc)
            else:
                buckets["high"].append(exc)
        for name, vals in buckets.items():
            base["buckets"][name]["count"] = len(vals)
            base["buckets"][name]["threshold"] = q1 if name == "low" else (
                q2 if name == "mid" else None)
            if vals:
                base["buckets"][name]["mean_excess"] = _mean(vals)
                base["buckets"][name]["win_rate"] = (
                    sum(1 for v in vals if v > 0) / len(vals))
        return base

    # ------------------------------------------------------------------
    # FR-16：隐含风格暴露（滚动相关热力图 + OLS beta）
    # ------------------------------------------------------------------
    def _exposure(self) -> dict:
        """策略对 6 宽基的滚动相关与 OLS beta（52 周窗口，v1 不约束权重）。"""
        name_by_code = {spec["code"]: spec["name"] for spec in CRISIS_BENCHMARKS}
        return self._exposure_block(
            self.style_available_codes, self.style_skipped,
            self.style_week_rets, name_by_code)

    def _quadrant_exposure(self) -> dict:
        """策略对巨潮四象限风格指数的滚动相关与 OLS beta（纯风格口径）。"""
        name_by_code = {s["code"]: s["name"] for s in QUADRANT_STYLE_INDICES}
        return self._exposure_block(
            self.quadrant_available_codes, self.quadrant_skipped,
            self.quadrant_week_rets, name_by_code)

    def _exposure_block(
        self,
        available_codes: List[str],
        skipped: List[str],
        week_rets: Dict[str, pd.Series],
        name_by_code: Dict[str, str],
    ) -> dict:
        base = {
            "available_codes": available_codes,
            "available_names": [name_by_code.get(c, c)
                                for c in available_codes],
            "skipped": skipped,
            "dates": [],
            "corr": {},       # code -> [float or None] 对齐 dates
            "beta": {},       # code -> [float or None]
            "window": ROLLING_EXPOSURE_WINDOW,
        }
        if len(available_codes) < 1:
            return base
        ref_index = self.s_week_ret.index
        common = ref_index
        for code in available_codes:
            common = common.intersection(week_rets[code].index)
        common = common.intersection(self.b_week_ret.index)
        if len(common) < 4:
            return base
        base["dates"] = [_dstr(ts) for ts in common]
        w = min(ROLLING_EXPOSURE_WINDOW, len(common))
        s_arr = [float(self.s_week_ret.loc[ts]) for ts in common]
        for code in available_codes:
            b_arr = [float(week_rets[code].loc[ts]) for ts in common]
            corr_list: List[Optional[float]] = []
            beta_list: List[Optional[float]] = []
            for end in range(len(common)):
                start = max(0, end + 1 - w)
                if end - start + 1 < 4:
                    corr_list.append(None)
                    beta_list.append(None)
                    continue
                sx = pd.Series(s_arr[start:end + 1])
                bx = pd.Series(b_arr[start:end + 1])
                corr_list.append(float(sx.corr(bx)))
                # 分子 cov 默认 ddof=1，分母 var 也须 ddof=1（Minor-1 修复：
                # 混用 ddof=0 会把 beta 系统性放大 n/(n-1)，n=4 时高估 33%）
                var_b = float(bx.var())
                if var_b < 1e-15:
                    beta_list.append(None)
                else:
                    cov = float(sx.cov(bx))
                    beta_list.append(cov / var_b)
            base["corr"][code] = corr_list
            base["beta"][code] = beta_list
        return base

