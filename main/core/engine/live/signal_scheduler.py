"""实盘信号频率调度：何时算、数据截至哪天。"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional

from ..bar_poller import latest_completed_bar, parse_bar_time, to_ohlcv
from ..event import BarEvent
from ..live.timing import now_shanghai
from ...services.signal_schedule import (
    effective_signal_data_end, effective_signal_frequency, parse_signal_at,
)

logger = logging.getLogger(__name__)


@dataclass
class DueBatch:
    portfolio: object
    strategies: list
    frequency: str
    period: str
    mode: str
    bar_time: datetime
    data_end_date: date


class SignalScheduler:
    def __init__(
        self, ctx, market_data, calendar_provider: Callable,
        formula_by_strategy: Dict[int, str],
        formula_count_by_name: Dict[str, int],
        formula_id_by_strategy: Dict[int, int],
        monitor, alert, catch_up_minutes: int = 30,
        clock: Callable[[], datetime] = now_shanghai,
    ):
        self.ctx = ctx
        self.market_data = market_data
        self._calendar = calendar_provider
        self._formula_by_strategy = formula_by_strategy
        self._formula_count_by_name = formula_count_by_name
        self._formula_id_by_strategy = formula_id_by_strategy
        self.monitor = monitor
        self.alert = alert
        self.catch_up_minutes = catch_up_minutes
        self.clock = clock
        self._fired = set()

    def due(self, now: datetime) -> List[DueBatch]:
        batches: List[DueBatch] = []
        skipped = 0
        for portfolio in self.ctx.portfolios:
            groups = {}
            for st in portfolio.strategies:
                freq = effective_signal_frequency(st.period, st.signal_frequency)
                if freq not in ("1d", "1w", "1mon"):
                    continue
                # 归一化默认值：NULL 与显式默认值必须归入同一批，避免同窗口双发。
                at = parse_signal_at(st.signal_at, freq).strftime("%H:%M")
                weekday = st.signal_weekday or 1
                data_end = effective_signal_data_end(freq, st.signal_data_end)
                key = (freq, st.period, at, weekday, data_end)
                groups.setdefault(key, []).append(st)
            for (freq, period, at, weekday, data_end), strategies in groups.items():
                due_dt, window_end, bar_time = self._schedule(
                    now, freq, at, weekday, data_end
                )
                if due_dt is None or now < due_dt:
                    continue
                fired_key = (
                    portfolio.portfolio_id, freq, period, window_end,
                    at, weekday, data_end,
                )
                if fired_key in self._fired:
                    continue
                if now > due_dt + timedelta(minutes=self.catch_up_minutes):
                    self._fired.add(fired_key)
                    skipped += len(strategies)
                    # 执行审计（每天 grep [signal-schedule] 核对 14:20 是否跑过）：
                    # 超窗跳过是「当天没算信号」的最常见静默原因，必须留痕。
                    logger.info(
                        "[signal-schedule] skip session=%s portfolio=%s "
                        "freq=%s at=%s strategies=%d due=%s reason=catchup",
                        getattr(self.ctx, "session_id", None),
                        portfolio.portfolio_id, freq, at, len(strategies), due_dt,
                    )
                    continue
                self._fired.add(fired_key)
                logger.info(
                    "[signal-schedule] fire session=%s portfolio=%s freq=%s "
                    "period=%s at=%s strategies=%d bar_time=%s mode=%s",
                    getattr(self.ctx, "session_id", None),
                    portfolio.portfolio_id, freq, period, at,
                    len(strategies), bar_time,
                    "inject" if freq == "1d" else "compute",
                )
                batches.append(DueBatch(
                    portfolio=portfolio, strategies=strategies, frequency=freq,
                    period=period,
                    mode="inject" if freq == "1d" else "compute",
                    bar_time=bar_time, data_end_date=window_end,
                ))
        if skipped and self.alert is not None:
            try:
                self.alert.send_message(
                    "已跳过 %d 个过期信号（超过 %d 分钟补算窗口）"
                    % (skipped, self.catch_up_minutes)
                )
            except Exception:  # noqa: BLE001
                pass
        return batches

    def _schedule(self, now, freq, at, weekday, data_end):
        cal = self._calendar()
        at_time = parse_signal_at(at, freq)
        today = now.date()
        if freq == "1d":
            if not cal.is_trading_day(today):
                return None, None, None
            due_dt = datetime.combine(today, at_time)
            end = today
            if effective_signal_data_end(freq, data_end) == "completed":
                end = self._last_trading_before(today)
            return due_dt, end, due_dt
        if freq == "1w":
            monday = today - timedelta(days=today.weekday())
            due_date = monday + timedelta(days=(weekday or 1) - 1)
            for _ in range(7):
                if cal.is_trading_day(due_date):
                    break
                due_date += timedelta(days=1)
            else:
                return None, None, None
            # 周信号固定用上一个完整周（与 signal_weekday 无关）：以本周一为界
            # 回溯上一交易日，避免周三/周五触发时误取本周未完成 bar。
            end = self._last_trading_before(monday)
            return (
                datetime.combine(due_date, at_time), end,
                datetime.combine(end, time(15, 0)),
            )
        if freq == "1mon":
            first = date(today.year, today.month, 1)
            due_date = first
            for _ in range(31):
                if cal.is_trading_day(due_date):
                    break
                due_date += timedelta(days=1)
            else:
                return None, None, None
            end = self._last_trading_before(due_date)
            return (
                datetime.combine(due_date, at_time), end,
                datetime.combine(end, time(15, 0)),
            )
        return None, None, None

    def _last_trading_before(self, d: date) -> date:
        cal = self._calendar()
        cur = d - timedelta(days=1)
        for _ in range(40):
            if cal.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return d - timedelta(days=1)

    def execute(self, batch: DueBatch) -> None:
        started = self.clock()
        if batch.mode == "inject":
            codes = self._execute_inject(batch)
        else:
            codes = self._execute_compute(batch)
        logger.info(
            "[signal-schedule] execute session=%s portfolio=%s mode=%s "
            "strategies=%d codes=%d elapsed_ms=%d",
            getattr(self.ctx, "session_id", None), batch.portfolio.portfolio_id,
            batch.mode, len(batch.strategies), codes,
            round((self.clock() - started).total_seconds() * 1000),
        )

    def _codes(self):
        return list(self.market_data._bar_poller.stock_codes)

    def _quote(self, code, period, count):
        bars = self.ctx.dispatcher.query_quote(code, period=period, count=count)
        return self.market_data._sort_and_cap(bars or [], count)

    def _execute_inject(self, batch: DueBatch) -> int:
        bars_by_code = {}
        stocks = {}
        current = effective_signal_data_end(
            batch.frequency, batch.strategies[0].signal_data_end
        ) == "current"
        target = datetime.combine(batch.data_end_date, time(15, 0))
        for code in self._codes():
            count = self.ctx.code_period_count.get(
                (code, batch.period),
                self.market_data._period_count.get(
                    batch.period, self.market_data._formula_count),
            )
            bars = self._quote(code, batch.period, count)
            if not bars:
                continue
            if not current:
                capped = []
                for b in bars:
                    bt = parse_bar_time(b)
                    if bt is not None and bt <= target:
                        capped.append(b)
                if capped:
                    bars = capped
                else:
                    continue
            bars_by_code[code] = bars
            # 已按 data_end_date 截断（completed）或保留 forming（current），
            # 排序由 _quote→_sort_and_cap 保证，末根即目标 bar。
            stocks[code] = to_ohlcv(bars[-1])
        if not stocks:
            return 0
        bar = BarEvent(stocks=stocks, bar_time=batch.bar_time,
                       period=batch.period, bars_by_code=bars_by_code)
        self.market_data.fill_signal_cache(
            batch.portfolio, bar, bars_by_code=bars_by_code
        )
        if self.monitor is not None:
            self.monitor.evaluate(
                batch.portfolio, bar, self.market_data.signal_cache,
                strategies=batch.strategies, frequency=batch.frequency,
            )
        return len(stocks)

    def _execute_compute(self, batch: DueBatch) -> int:
        end_time = batch.data_end_date.strftime("%Y%m%d")
        evaluated = 0
        for st in batch.strategies:
            formula_name = self._formula_by_strategy.get(st.strategy_id)
            if not formula_name:
                continue
            count = self._formula_count_by_name.get(formula_name, 200)
            codes = self._codes()
            try:
                raw = self.market_data._tq_formula.compute(
                    formula_name, "", codes, period=batch.period,
                    count=count, end_time=end_time,
                )
            except Exception as exc:  # noqa: BLE001
                self._report_error(batch, st, formula_name, str(exc))
                continue
            if not isinstance(raw, dict) or str(raw.get("ErrorId", "0")) not in ("0", "19"):
                self._report_error(
                    batch, st, formula_name,
                    str((raw or {}).get("Error") or "ErrorId != 0"),
                )
                continue
            stocks = {}
            target = datetime.combine(batch.data_end_date, time(15, 0))
            for code in codes:
                bars = self._quote(code, batch.period, count)
                chosen = None
                for b in bars:
                    bt = parse_bar_time(b)
                    if bt is not None and bt <= target:
                        chosen = b
                if chosen is None:
                    fallback = latest_completed_bar(bars)
                    fb_time = parse_bar_time(fallback) if fallback else None
                    if fb_time is not None and fb_time <= target:
                        chosen = fallback
                if chosen is not None:
                    stocks[code] = to_ohlcv(chosen)
                outputs = self.market_data._extract_latest_signal(raw, code)
                if outputs:
                    self.market_data.signal_cache[
                        (st.strategy_id, code, batch.bar_time)
                    ] = outputs
            if not stocks:
                continue
            evaluated += len(stocks)
            bar = BarEvent(stocks=stocks, bar_time=batch.bar_time,
                           period=batch.period)
            if self.monitor is not None:
                self.monitor.evaluate(
                    batch.portfolio, bar, self.market_data.signal_cache,
                    strategies=[st], frequency=batch.frequency,
                )
        return evaluated

    def _report_error(self, batch, st, formula_name, message):
        text = "公式失败 %s/%s: %s" % (formula_name, st.strategy_id, message)
        if self.alert is not None:
            try:
                self.alert.send_message(text)
            except Exception:  # noqa: BLE001
                pass
        if self.monitor is not None and hasattr(
            self.monitor, "report_runtime_error"
        ):
            self.monitor.report_runtime_error(
                batch.portfolio, st, formula_name, message
            )
