import math
from collections import deque
from decimal import Decimal
from typing import List, Optional, Tuple

# 无风险年化利率，Sharpe/Sortino 计算用。提为常量消除魔法数字（审计 #33）。
RISK_FREE_RATE = 0.02

# 基准相对指标所需最少对齐日收益样本数（< 20 不出数）
MIN_REL_SAMPLE_DAYS = 20

# FR-1 八项基准相对指标键序（evaluator/robustness 共用，Minor-5 合并）
REL_METRIC_KEYS = (
    "beta", "jensen_alpha", "excess_annual_return", "tracking_error",
    "information_ratio", "up_capture_ratio", "down_capture_ratio",
    "excess_max_drawdown",
)


def compute_relative_metrics_core(
    rs: List[float], rb: List[float],
    s_first: float, s_last: float, b_first: float, b_last: float,
    days: int,
) -> dict:
    """FR-1 八项基准相对指标唯一实现（Minor-5：evaluator/robustness 共用核心）。

    输入：对齐日收益序列 rs/rb（等长）、策略与基准首末净值、跨度天数。
    输出：raw float dict（None 安全）；样本 < MIN_REL_SAMPLE_DAYS → 全 None。

    口径（固化，勿随手感改）：
    - beta：日频总体协方差 / 基准总体方差（分子分母同 ddof=0，比值精确）
    - Jensen α：对齐窗口 CAGR 口径，α = CAGR_s − rf − β(CAGR_b − rf)
    - 年化超额/跟踪误差/IR：active 日收益算术口径，IR = 年化超额 / TE
    - 上/下行捕获率：基准上涨/下跌日中双方日均收益之比；无对应日样本 → None
    - 超额回撤：cumprod(1+active) 曲线的最大回撤（≥0）
    """
    none_result = {k: None for k in REL_METRIC_KEYS}
    n = len(rs)
    if n < MIN_REL_SAMPLE_DAYS:
        return none_result

    mean_s = sum(rs) / n
    mean_b = sum(rb) / n
    var_b = sum((r - mean_b) ** 2 for r in rb) / n
    cov = sum((s - mean_s) * (b - mean_b) for s, b in zip(rs, rb)) / n
    beta = cov / var_b if var_b > 0 else None

    years = max(days / 365.25, 1 / 365.25)
    cagr_s = (s_last / s_first) ** (1 / years) - 1
    cagr_b = (b_last / b_first) ** (1 / years) - 1
    jensen = (
        cagr_s - RISK_FREE_RATE - beta * (cagr_b - RISK_FREE_RATE)
        if beta is not None else None
    )

    active = [s - b for s, b in zip(rs, rb)]
    mean_a = sum(active) / n
    excess_annual = mean_a * 252
    te = math.sqrt(sum((a - mean_a) ** 2 for a in active) / n * 252)
    ir = excess_annual / te if te > 0 else None

    up_s = [s for s, b in zip(rs, rb) if b > 0]
    up_b = [b for b in rb if b > 0]
    down_s = [s for s, b in zip(rs, rb) if b < 0]
    down_b = [b for b in rb if b < 0]
    up_mean_b = sum(up_b) / len(up_b) if up_b else 0
    down_mean_b = sum(down_b) / len(down_b) if down_b else 0
    up_capture = (
        (sum(up_s) / len(up_s)) / up_mean_b
        if up_b and up_mean_b != 0 else None
    )
    down_capture = (
        (sum(down_s) / len(down_s)) / down_mean_b
        if down_b and down_mean_b != 0 else None
    )

    equity = 1.0
    peak = 1.0
    excess_dd = 0.0
    for a in active:
        equity *= 1 + a
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > excess_dd:
            excess_dd = dd

    return {
        "beta": beta,
        "jensen_alpha": jensen,
        "excess_annual_return": excess_annual,
        "tracking_error": te,
        "information_ratio": ir,
        "up_capture_ratio": up_capture,
        "down_capture_ratio": down_capture,
        "excess_max_drawdown": excess_dd,
    }


class Evaluator:
    def evaluate(
        self,
        snapshots: List[dict],
        benchmark_data: Optional[List[dict]] = None,
        trades: Optional[List] = None,
    ) -> dict:
        if len(snapshots) < 2:
            return {}

        n = len(snapshots)
        values = [Decimal(str(s["total_value"])) for s in snapshots]

        start_val = values[0]
        end_val = values[-1]
        total_return = (end_val - start_val) / start_val

        # 日频聚合行（每日最后一根 bar）：日收益/仓位/持仓结构/路径指标统一从这里出
        daily_rows = self._daily_rows(snapshots)
        daily_returns = self._returns_from_rows(daily_rows)

        days = (snapshots[-1]["snap_date"] - snapshots[0]["snap_date"]).days
        years = max(days / 365.25, 1 / 365.25)
        annual_return = (1 + float(total_return)) ** (1 / years) - 1 if total_return > -1 else -1

        peak = values[0]
        max_dd = Decimal("0")
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

        mean_dr = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_dr) ** 2 for r in daily_returns) / len(daily_returns)
        volatility = math.sqrt(variance * 252)

        rf = RISK_FREE_RATE
        sharpe = (annual_return - rf) / volatility if volatility > 0 else 0

        neg_returns = [r for r in daily_returns if r < 0]
        downside_var = sum(r ** 2 for r in neg_returns) / len(neg_returns) if neg_returns else 0
        downside_dev = math.sqrt(downside_var * 252)
        sortino = (annual_return - rf) / downside_dev if downside_dev > 0 else 0

        calmar = annual_return / float(max_dd) if max_dd > 0 else 0

        daily_sorted = sorted(daily_returns)
        idx = max(0, int(len(daily_sorted) * 0.05) - 1)
        var_95 = daily_sorted[idx] if len(daily_sorted) >= 20 else None

        if var_95 is not None:
            cvar = sum(r for r in daily_returns if r <= var_95) / max(
                len([r for r in daily_returns if r <= var_95]), 1
            )
        else:
            cvar = None

        peak_u = values[0]
        dd_sq_sum = Decimal("0")
        for v in values:
            if v > peak_u:
                peak_u = v
            drawdown = (peak_u - v) / peak_u
            dd_sq_sum += drawdown ** 2
        ulcer = math.sqrt(float(dd_sq_sum) / n)

        if n > 2:
            xs = list(range(n))
            ys = [float(v) for v in values]
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            den = math.sqrt(sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys))
            r2 = (num / den) ** 2 if den > 0 else 0
        else:
            r2 = 0

        # ---- 交易指标 ----
        trade_metrics = self._compute_trade_metrics(trades or [])

        # ---- 基准收益 ----
        benchmark_return = self._compute_benchmark_return(benchmark_data)

        # ---- 回撤恢复天数 ----
        recovery = self._compute_recovery_days(snapshots, values)

        # ---- 基准相对指标（基于快照内 benchmark_value 按日对齐）----
        relative = self._compute_relative_metrics(snapshots)

        # ---- 交易磨损 / 仓位利用（FR-2）----
        cost_metrics = self._compute_cost_metrics(trades or [], daily_rows, years)
        utilization = self._compute_utilization(daily_rows)
        position_structure = self._compute_position_structure(daily_rows)

        # ---- 路径 / 分布（FR-3）----
        path_metrics = self._compute_path_metrics(daily_rows, daily_returns)

        result = {
            "total_return": total_return,
            "annual_return": Decimal(str(annual_return)).quantize(Decimal("0.0001")),
            "max_drawdown": max_dd,
            "volatility": Decimal(str(volatility)).quantize(Decimal("0.0001")),
            "sharpe_ratio": Decimal(str(sharpe)).quantize(Decimal("0.0001")),
            "sortino_ratio": Decimal(str(sortino)).quantize(Decimal("0.0001")),
            "calmar_ratio": Decimal(str(calmar)).quantize(Decimal("0.0001")),
            "win_rate": trade_metrics["win_rate"],
            "profit_factor": trade_metrics["profit_factor"],
            "total_trades": trade_metrics["total_trades"],
            "benchmark_return": benchmark_return,
            "avg_holding_days": trade_metrics["avg_holding_days"],
            "var_95": (
                Decimal(str(var_95)).quantize(Decimal("0.0001"))
                if var_95 is not None else None
            ),
            "cvar_95": (
                Decimal(str(cvar)).quantize(Decimal("0.0001"))
                if cvar is not None else None
            ),
            "avg_recovery_days": recovery["avg_recovery_days"],
            "max_recovery_days": recovery["max_recovery_days"],
            "ulcer_index": Decimal(str(ulcer)).quantize(Decimal("0.0001")),
            "return_stability": Decimal(str(r2)).quantize(Decimal("0.0001")),
            **relative,
            **cost_metrics,
            **utilization,
            **position_structure,
            **path_metrics,
        }
        return result

    # ========================================================================
    # 日频聚合（bar 频回测按日期统计）
    # ========================================================================
    @staticmethod
    def _daily_rows(snapshots: List[dict]) -> List[dict]:
        """按日期聚合，每日保留最后一根 bar（日收盘口径）。

        返回升序 [{date, total_value, market_value, positions}, ...]。
        - 30m 等日内周期一天多根 bar，per-bar 收益不能直接当日记收益（vol 低估
          ~√每日bar数、Sharpe 虚高，2026-09 回测"30"实测根因），故统一按日聚合。
        - positions 为该日持仓明细负载（Task 3 产出；老快照无此键 → None）。
        """
        daily: dict = {}
        for s in snapshots:
            d = s["snap_date"]
            daily[d] = {
                "date": d,
                "total_value": Decimal(str(s["total_value"])),
                "market_value": Decimal(str(s.get("market_value") or 0)),
                "positions": s.get("positions"),
            }
        return [daily[d] for d in sorted(daily)]

    @staticmethod
    def _returns_from_rows(rows: List[dict]) -> List[float]:
        returns: List[float] = []
        for i in range(1, len(rows)):
            prev = rows[i - 1]["total_value"]
            cur = rows[i]["total_value"]
            returns.append(float((cur - prev) / prev))
        return returns

    @staticmethod
    def _daily_returns(snapshots: List[dict], values: List[Decimal] = None) -> List[float]:
        """兼容保留：日收益序列（内部已统一走 _daily_rows）。values 入参忽略。"""
        return Evaluator._returns_from_rows(Evaluator._daily_rows(snapshots))

    # ========================================================================
    # 交易指标：FIFO 匹配计算 P&L
    # ========================================================================
    @staticmethod
    def _compute_trade_metrics(trades: List) -> dict:
        """从 TradeEvent 列表计算 win_rate / profit_factor / total_trades / avg_holding_days。

        FIFO 队列按 (strategy_id, stock_code) 分组，每笔 SELL 按先进先出消耗 BUY
        仓位，逐笔计算 P&L。部分平仓时按比例拆分。
        """
        total_trades = len(trades)
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": None,
                "profit_factor": None,
                "avg_holding_days": None,
            }

        # FIFO 队列：{key: deque([(buy_price, qty, buy_time)])}
        fifo: dict = {}
        lot_pnls: List[Tuple[Decimal, int]] = []  # [(pnl, holding_days)]

        for tr in trades:
            key = (tr.strategy_id, tr.stock_code)
            if key not in fifo:
                fifo[key] = deque()

            if tr.trade_type.value == "BUY":
                fifo[key].append((tr.price, tr.quantity, tr.trade_time))
            elif tr.trade_type.value == "SELL":
                remaining = tr.quantity
                matched_qty = 0
                while remaining > 0 and fifo[key]:
                    buy_price, buy_qty, buy_time = fifo[key][0]
                    taken = min(remaining, buy_qty)
                    # P&L = (sell_price - buy_price) * taken
                    pnl = (tr.price - buy_price) * taken
                    holding_days = (tr.trade_time - buy_time).days
                    lot_pnls.append((pnl, holding_days))
                    remaining -= taken
                    matched_qty += taken
                    if taken >= buy_qty:
                        fifo[key].popleft()
                    else:
                        fifo[key][0] = (buy_price, buy_qty - taken, buy_time)
                # 剩余 unmatched（卖超了）忽略

        # win_rate
        if lot_pnls:
            winners = sum(1 for pnl, _ in lot_pnls if pnl > 0)
            win_rate = Decimal(winners) / Decimal(len(lot_pnls))
            win_rate = win_rate.quantize(Decimal("0.0001"))
        else:
            win_rate = None

        # profit_factor
        if lot_pnls:
            gross_profit = sum(pnl for pnl, _ in lot_pnls if pnl > 0)
            gross_loss = abs(sum(pnl for pnl, _ in lot_pnls if pnl < 0))
            if gross_loss > 0:
                profit_factor = (gross_profit / gross_loss).quantize(Decimal("0.0001"))
            else:
                profit_factor = None  # 从无亏损
        else:
            profit_factor = None

        # avg_holding_days
        if lot_pnls:
            holding_days = [d for _, d in lot_pnls]
            avg_hold = Decimal(sum(holding_days)) / Decimal(len(holding_days))
            avg_hold = avg_hold.quantize(Decimal("0.0001"))
        else:
            avg_hold = None

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_holding_days": avg_hold,
        }

    # ========================================================================
    # 基准收益
    # ========================================================================
    @staticmethod
    def _compute_benchmark_return(benchmark_data: Optional[List[dict]]) -> Decimal:
        if not benchmark_data or len(benchmark_data) < 2:
            return Decimal("0")
        start_val = Decimal(str(benchmark_data[0]["value"]))
        end_val = Decimal(str(benchmark_data[-1]["value"]))
        if start_val == 0:
            return Decimal("0")
        result = (end_val - start_val) / start_val
        return result.quantize(Decimal("0.0001"))

    # ========================================================================
    # 基准相对指标
    # beta / Jensen α / 年化超额 / 跟踪误差 / IR / 上下行捕获率 / 超额回撤
    # ========================================================================
    @staticmethod
    def _aligned_daily_pairs(snapshots: List[dict]) -> List[tuple]:
        """按日聚合（每日取最后一根 bar）策略与基准收盘净值，仅保留两者皆有的日期。

        返回 [(date, strategy_close: float, benchmark_close: float), ...]（升序）。
        基准值取自快照自身 benchmark_value（引擎按 bar 日期与策略净值同日填充），
        故离线从 DB 快照重算时口径一致；策略层快照无 benchmark_value 自然被排除。
        """
        daily: dict = {}
        for s in snapshots:
            d = s["snap_date"]
            bv = s.get("benchmark_value")
            daily[d] = (
                float(Decimal(str(s["total_value"]))),
                float(Decimal(str(bv))) if bv is not None else None,
            )
        return [
            (d, sv, bv)
            for d, (sv, bv) in sorted(daily.items())
            if bv is not None
        ]

    @staticmethod
    def _compute_relative_metrics(snapshots: List[dict]) -> dict:
        """从快照序列计算 8 个基准相对指标（Decimal 量化输出）。

        计算核心下沉到模块级 ``compute_relative_metrics_core``（与
        robustness._relative_metrics 共用，Minor-5 消除双实现漂移）；
        此处只负责快照→日收益对齐与 Decimal 量化。
        """
        pairs = Evaluator._aligned_daily_pairs(snapshots)
        rs: List[float] = []
        rb: List[float] = []
        for i in range(1, len(pairs)):
            _, s0, b0 = pairs[i - 1]
            _, s1, b1 = pairs[i]
            if s0 > 0 and b0 > 0:
                rs.append(s1 / s0 - 1)
                rb.append(b1 / b0 - 1)

        def q(x):
            return Decimal(str(x)).quantize(Decimal("0.0001")) if x is not None else None

        if len(rs) < MIN_REL_SAMPLE_DAYS:
            return {k: None for k in REL_METRIC_KEYS}
        days = (pairs[-1][0] - pairs[0][0]).days
        core = compute_relative_metrics_core(
            rs, rb, pairs[0][1], pairs[-1][1], pairs[0][2], pairs[-1][2], days)
        return {k: q(v) for k, v in core.items()}

    # ========================================================================
    # 交易磨损 / 仓位利用 / 持仓结构（FR-2）
    #
    # 口径约定（固化，勿随手感改）：
    # - 年化换手率 = 双边成交额（BUY+SELL amount 全计）/ 日均总净值 / 自然年数
    # - 成本占比 =（佣金 + 印花税）/ 首日总净值（≈初始资金）
    # - 仓位利用率 = mean(market_value / total_value)，按日收盘
    # - 空仓日 = 当日 market_value <= 0
    # - 单票权重 = 个股市值 / 组合总净值（含现金口径）；HHI = 日 Σw² 再按日均值
    # ========================================================================
    @staticmethod
    def _compute_cost_metrics(trades: List, daily_rows: List[dict], years: float) -> dict:
        total_amount = Decimal("0")
        total_cost = Decimal("0")
        for t in trades:
            total_amount += Decimal(str(getattr(t, "amount", 0) or 0))
            total_cost += Decimal(str(getattr(t, "commission", 0) or 0))
            total_cost += Decimal(str(getattr(t, "stamp_duty", 0) or 0))

        n_rows = len(daily_rows)
        avg_total = (
            sum((r["total_value"] for r in daily_rows), Decimal("0")) / n_rows
            if n_rows else Decimal("0")
        )
        start_value = daily_rows[0]["total_value"] if daily_rows else Decimal("0")

        turnover = (
            float(total_amount / avg_total) / years if avg_total > 0 else None
        )
        cost_ratio = (
            total_cost / start_value if start_value > 0 else None
        )
        return {
            "annual_turnover": (
                Decimal(str(turnover)).quantize(Decimal("0.0001"))
                if turnover is not None else None
            ),
            "total_cost_ratio": (
                cost_ratio.quantize(Decimal("0.000001"))
                if cost_ratio is not None else None
            ),
        }

    @staticmethod
    def _compute_utilization(daily_rows: List[dict]) -> dict:
        ratios: List[Decimal] = []
        empty = 0
        for r in daily_rows:
            tv = r["total_value"]
            mv = r["market_value"]
            if tv > 0:
                ratios.append(mv / tv)
                if mv <= 0:
                    empty += 1
        if not ratios:
            return {
                "avg_position_utilization": None,
                "empty_days_ratio": None,
            }
        return {
            "avg_position_utilization": (
                sum(ratios, Decimal("0")) / len(ratios)
            ).quantize(Decimal("0.0001")),
            "empty_days_ratio": (
                Decimal(empty) / Decimal(len(ratios))
            ).quantize(Decimal("0.0001")),
        }

    @staticmethod
    def _compute_position_structure(daily_rows: List[dict]) -> dict:
        """从每日持仓明细算最大单票权重 / HHI；全程无持仓负载 → 均 None。"""
        payload_days = [r for r in daily_rows if r.get("positions") is not None]
        if not payload_days:
            return {"max_single_weight": None, "hhi": None}

        max_weight = 0.0
        hhis: List[float] = []
        for r in payload_days:
            tv = float(r["total_value"])
            if tv <= 0:
                continue
            weights = []
            for h in r["positions"]:
                mv = h.get("market_value")
                if mv is None:
                    continue
                w = float(Decimal(str(mv))) / tv
                if w > 0:
                    weights.append(w)
            if weights:
                max_weight = max(max_weight, max(weights))
            # 空仓日 HHI=0（持仓负载为 []），计入日均值
            hhis.append(sum(w * w for w in weights))
        hhi = sum(hhis) / len(hhis) if hhis else None
        return {
            "max_single_weight": Decimal(str(max_weight)).quantize(Decimal("0.0001")),
            "hhi": Decimal(str(hhi)).quantize(Decimal("0.0001")) if hhi is not None else None,
        }

    # ========================================================================
    # 路径 / 分布指标（FR-3）
    # 最长连亏日 / 最差日 / 最差周（ISO 周桶，周末值/周首值-1）/
    # 偏度峰度（总体矩，Fisher 超额峰度，正态=0；<20 日收益不出数）
    # ========================================================================
    @staticmethod
    def _compute_path_metrics(daily_rows: List[dict], daily_returns: List[float]) -> dict:
        streak = best_streak = 0
        for r in daily_returns:
            if r < 0:
                streak += 1
                if streak > best_streak:
                    best_streak = streak
            else:
                streak = 0

        worst_daily = min(daily_returns) if daily_returns else None

        weekly_returns: List[float] = []
        week_key = None
        first_v = last_v = 0.0
        for r in daily_rows:
            key = r["date"].isocalendar()[:2]  # (ISO year, ISO week)
            v = float(r["total_value"])
            if key != week_key:
                if week_key is not None and first_v > 0:
                    weekly_returns.append(last_v / first_v - 1)
                first_v = v
                week_key = key
            last_v = v
        if week_key is not None and first_v > 0:
            weekly_returns.append(last_v / first_v - 1)
        worst_weekly = min(weekly_returns) if weekly_returns else None

        n = len(daily_returns)
        skewness = kurtosis = None
        if n >= MIN_REL_SAMPLE_DAYS:
            mean_r = sum(daily_returns) / n
            m2 = sum((r - mean_r) ** 2 for r in daily_returns) / n
            if m2 > 0:
                m3 = sum((r - mean_r) ** 3 for r in daily_returns) / n
                m4 = sum((r - mean_r) ** 4 for r in daily_returns) / n
                skewness = m3 / m2 ** 1.5
                kurtosis = m4 / m2 ** 2 - 3  # Fisher 超额峰度

        return {
            "max_consecutive_loss_days": best_streak,
            "worst_daily_return": (
                Decimal(str(worst_daily)).quantize(Decimal("0.0001"))
                if worst_daily is not None else None
            ),
            "worst_weekly_return": (
                Decimal(str(worst_weekly)).quantize(Decimal("0.0001"))
                if worst_weekly is not None else None
            ),
            "return_skewness": (
                Decimal(str(skewness)).quantize(Decimal("0.0001"))
                if skewness is not None else None
            ),
            "return_kurtosis": (
                Decimal(str(kurtosis)).quantize(Decimal("0.0001"))
                if kurtosis is not None else None
            ),
        }

    # ========================================================================
    # 回撤恢复天数
    # ========================================================================
    @staticmethod
    def _compute_recovery_days(snapshots: List[dict], values: List[Decimal]) -> dict:
        """从快照序列计算平均/最大回撤恢复天数。

        恢复定义：价值回升至超越上一个峰值（> peak，不含等值）。
        回撤区间：[peak_date, 超越峰值的第一个日期)。
        """
        recovery_periods: List[int] = []
        peak_val = values[0]
        peak_date = snapshots[0]["snap_date"]  # 峰值对应日期
        in_drawdown = False

        for i in range(1, len(values)):
            val = values[i]
            snap_date = snapshots[i]["snap_date"]
            if val > peak_val:
                if in_drawdown:
                    # 恢复：从 peak_date 到当前日期的天数
                    recovery_days = (snap_date - peak_date).days
                    recovery_periods.append(recovery_days)
                    in_drawdown = False
                peak_val = val
                peak_date = snap_date
            elif val < peak_val:
                in_drawdown = True
            # val == peak_val: 不变状态

        if recovery_periods:
            avg_days = Decimal(sum(recovery_periods)) / Decimal(len(recovery_periods))
            avg_days = avg_days.quantize(Decimal("0.0001"))
            max_days = max(recovery_periods)
        else:
            avg_days = Decimal("0")
            max_days = 0

        return {"avg_recovery_days": avg_days, "max_recovery_days": max_days}
