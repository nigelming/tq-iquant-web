import math
from collections import deque
from decimal import Decimal
from typing import List, Optional, Tuple


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

        daily_returns = []
        for i in range(1, n):
            dr = (values[i] - values[i - 1]) / values[i - 1]
            daily_returns.append(float(dr))

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

        rf = 0.02
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
        }
        return result

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
