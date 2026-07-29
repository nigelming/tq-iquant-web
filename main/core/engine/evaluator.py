import math
from decimal import Decimal
from typing import List, Optional


class Evaluator:
    def evaluate(
        self,
        snapshots: List[dict],
        benchmark_data: Optional[List[dict]] = None,
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

        result = {
            "total_return": total_return,
            "annual_return": Decimal(str(annual_return)).quantize(Decimal("0.0001")),
            "max_drawdown": max_dd,
            "volatility": Decimal(str(volatility)).quantize(Decimal("0.0001")),
            "sharpe_ratio": Decimal(str(sharpe)).quantize(Decimal("0.0001")),
            "sortino_ratio": Decimal(str(sortino)).quantize(Decimal("0.0001")),
            "calmar_ratio": Decimal(str(calmar)).quantize(Decimal("0.0001")),
            "win_rate": Decimal("0"),
            "profit_factor": Decimal("0"),
            "total_trades": 0,
            "benchmark_return": Decimal("0"),
            "avg_holding_days": Decimal("0"),
            "var_95": (
                Decimal(str(var_95)).quantize(Decimal("0.0001"))
                if var_95 is not None else None
            ),
            "cvar_95": (
                Decimal(str(cvar)).quantize(Decimal("0.0001"))
                if cvar is not None else None
            ),
            "avg_recovery_days": Decimal("0"),
            "max_recovery_days": 0,
            "ulcer_index": Decimal(str(ulcer)).quantize(Decimal("0.0001")),
            "return_stability": Decimal(str(r2)).quantize(Decimal("0.0001")),
        }
        return result
