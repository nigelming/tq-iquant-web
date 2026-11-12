import logging
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from core.engine.live.signal_scheduler import DueBatch, SignalScheduler
from core.engine.live.calendar import TradingCalendar


class FakeCalendar:
    def __init__(self, days):
        self.days = set(days)

    def is_trading_day(self, d):
        return d in self.days


class FakeAlert:
    def __init__(self):
        self.messages = []

    def send_message(self, msg):
        self.messages.append(msg)
        return {"ok": True}


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def evaluate(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeMarketData:
    pass


def _ctx(strategy):
    port = SimpleNamespace(portfolio_id=1, strategies=[strategy])
    return SimpleNamespace(session_id=7, portfolios=[port], code_period_count={})


def _strategy(**kw):
    base = dict(strategy_id=1, period="1d", signal_frequency="1w",
                signal_at="09:00", signal_weekday=1,
                signal_data_end="completed")
    base.update(kw)
    return SimpleNamespace(**base)


def _scheduler(now_days, strategy, alert=None, clock=None, catch_up=30):
    cal = FakeCalendar(now_days)
    return SignalScheduler(
        _ctx(strategy), FakeMarketData(), lambda: cal,
        {1: "momentum"}, {"momentum": 200}, {1: 10},
        FakeMonitor(), alert or FakeAlert(), catch_up_minutes=catch_up,
        clock=clock or (lambda: datetime(2026, 10, 26, 9, 0)),
    )


def test_weekly_due_at_monday_0900_uses_last_friday():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    s = _scheduler(days, _strategy())
    assert s.due(datetime(2026, 10, 26, 8, 59)) == []
    batches = s.due(datetime(2026, 10, 26, 9, 0))
    assert len(batches) == 1
    b = batches[0]
    assert b.mode == "compute"
    assert b.frequency == "1w"
    assert b.data_end_date == date(2026, 10, 23)
    assert b.bar_time == datetime(2026, 10, 23, 15, 0)
    assert s.due(datetime(2026, 10, 26, 9, 1)) == []


def test_weekly_due_once_per_portfolio():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    st = _strategy()
    ctx = SimpleNamespace(portfolios=[
        SimpleNamespace(portfolio_id=1, strategies=[st]),
        SimpleNamespace(portfolio_id=2, strategies=[st]),
    ], code_period_count={})
    s = SignalScheduler(
        ctx, FakeMarketData(), lambda: FakeCalendar(days),
        {1: "momentum"}, {"momentum": 200}, {1: 10},
        FakeMonitor(), FakeAlert(),
    )
    assert len(s.due(datetime(2026, 10, 26, 9, 0))) == 2


def test_weekly_holiday_shifts_to_next_trading_day():
    days = [date(2026, 10, 23), date(2026, 10, 27)]
    s = _scheduler(days, _strategy())
    assert s.due(datetime(2026, 10, 26, 9, 0)) == []
    batches = s.due(datetime(2026, 10, 27, 9, 0))
    assert batches[0].data_end_date == date(2026, 10, 23)


def test_weekly_wednesday_uses_previous_week_end():
    days = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21),
            date(2026, 10, 22), date(2026, 10, 23),
            date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28),
            date(2026, 10, 29), date(2026, 10, 30)]
    s = _scheduler(days, _strategy(signal_weekday=3))
    assert s.due(datetime(2026, 10, 26, 9, 0)) == []
    assert s.due(datetime(2026, 10, 27, 9, 0)) == []
    batches = s.due(datetime(2026, 10, 28, 9, 0))
    assert len(batches) == 1
    assert batches[0].data_end_date == date(2026, 10, 23)
    assert batches[0].bar_time == datetime(2026, 10, 23, 15, 0)


def test_daily_1430_is_inject():
    days = [date(2026, 10, 26)]
    s = _scheduler(days, _strategy(signal_frequency="1d", signal_at="14:30",
                                  signal_data_end="current"))
    batches = s.due(datetime(2026, 10, 26, 14, 30))
    assert len(batches) == 1
    assert batches[0].mode == "inject"
    assert batches[0].bar_time == datetime(2026, 10, 26, 14, 30)
    assert batches[0].data_end_date == date(2026, 10, 26)


def test_catch_up_window_and_skip_message():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    alert = FakeAlert()
    s = _scheduler(days, _strategy(), alert=alert)
    assert len(s.due(datetime(2026, 10, 26, 9, 20))) == 1
    alert2 = FakeAlert()
    s2 = _scheduler(days, _strategy(), alert=alert2)
    assert s2.due(datetime(2026, 10, 26, 9, 31)) == []
    assert alert2.messages and "跳过" in alert2.messages[0]


def test_monthly_due_first_trading_day_uses_prev_month_last_day():
    days = [date(2026, 9, 30), date(2026, 10, 9)]
    st = _strategy(signal_frequency="1mon", signal_at="09:00",
                   signal_data_end="completed")
    s = _scheduler(days, st)
    assert s.due(datetime(2026, 10, 1, 9, 0)) == []
    batches = s.due(datetime(2026, 10, 9, 9, 0))
    assert batches[0].data_end_date == date(2026, 9, 30)
    assert batches[0].bar_time == datetime(2026, 9, 30, 15, 0)


def test_grouping_normalizes_null_defaults_into_one_batch():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    st1 = _strategy(strategy_id=1, signal_at=None, signal_weekday=None,
                    signal_data_end=None)
    st2 = _strategy(strategy_id=2)
    ctx = SimpleNamespace(
        portfolios=[SimpleNamespace(portfolio_id=1, strategies=[st1, st2])],
        code_period_count={},
    )
    s = SignalScheduler(
        ctx, FakeMarketData(), lambda: FakeCalendar(days),
        {1: "momentum"}, {"momentum": 200}, {1: 10},
        FakeMonitor(), FakeAlert(),
    )
    batches = s.due(datetime(2026, 10, 26, 9, 0))
    assert len(batches) == 1
    assert [st.strategy_id for st in batches[0].strategies] == [1, 2]


class FakeFormula:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def compute(self, *a, **kw):
        self.calls.append((a, kw))
        return self.raw


class ExecuteMarketData:
    def __init__(self):
        self.signal_cache = {}
        self._period_count = {"1d": 200}
        self._formula_count = 200
        self._formula_count_by_name = {"momentum": 200}
        self._bar_poller = SimpleNamespace(stock_codes=["000001.SZ"])
        self._tq_formula = FakeFormula({
            "ErrorId": "0",
            "000001.SZ": {"buy": [{"Date": "20261023", "Value": 1}]},
        })
        self.fills = []

    def _sort_and_cap(self, bars, count):
        return bars

    def fill_signal_cache(self, portfolio, bar, bars_by_code=None, **kw):
        self.fills.append((bar, bars_by_code))
        self.signal_cache[(1, "000001.SZ", bar.bar_time)] = [
            {"name": "buy", "value": 1},
        ]

    @staticmethod
    def _extract_latest_signal(raw, code):
        return [{"name": "buy", "value": 1}]


def _exec_ctx(strategy, bars):
    disp = SimpleNamespace(
        query_quote=lambda code, period="1m", count=10: list(bars)
    )
    port = SimpleNamespace(portfolio_id=1, strategies=[strategy])
    return SimpleNamespace(session_id=7, portfolios=[port],
                           code_period_count={}, dispatcher=disp)


_BARS = [
    {"stime": "20261023150000", "open": 12, "high": 12.5, "low": 11.9,
     "close": 12.34, "volume": 100},
    {"stime": "20261026143000", "open": 12.5, "high": 13, "low": 12.4,
     "close": 12.9, "volume": 50},
]


def test_execute_inject_completed_caps_bars_and_price():
    strategy = _strategy(signal_frequency="1d", signal_at="14:30",
                         signal_data_end="completed")
    md = ExecuteMarketData()
    mon = FakeMonitor()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, mon, FakeAlert(),
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1d", period="1d", mode="inject",
        bar_time=datetime(2026, 10, 26, 14, 30),
        data_end_date=date(2026, 10, 23),
    )
    sched.execute(batch)
    bar, bars_by_code = md.fills[0]
    assert len(bars_by_code["000001.SZ"]) == 1
    assert bars_by_code["000001.SZ"][0]["stime"].startswith("20261023")
    assert bar.stocks["000001.SZ"]["close"] == Decimal("12.34")
    assert mon.calls


def test_execute_tolerates_none_monitor_and_alert():
    strategy = _strategy(signal_frequency="1d", signal_at="14:30",
                         signal_data_end="completed")
    md = ExecuteMarketData()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, None, None,
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1d", period="1d", mode="inject",
        bar_time=datetime(2026, 10, 26, 14, 30),
        data_end_date=date(2026, 10, 23),
    )
    sched.execute(batch)
    assert md.fills


def test_execute_compute_uses_window_end_price_not_forming_bar():
    strategy = _strategy()
    md = ExecuteMarketData()
    mon = FakeMonitor()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, mon, FakeAlert(),
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1w", period="1d", mode="compute",
        bar_time=datetime(2026, 10, 23, 15, 0),
        data_end_date=date(2026, 10, 23),
    )
    sched.execute(batch)
    portfolio, bar, _cache = mon.calls[0][0][:3]
    assert bar.stocks["000001.SZ"]["close"] == Decimal("12.34")
    assert md._tq_formula.calls
    assert md._tq_formula.calls[0][1]["end_time"] == "20261023"


# ---------------- 14:20 执行审计日志（[signal-schedule]，每天 grep 核对） ----------------

def test_due_fire_logs_signal_schedule(caplog):
    """调度触发时落一条 [signal-schedule] fire INFO（session/频率/策略数）。"""
    days = [date(2026, 10, 26)]
    s = _scheduler(days, _strategy(signal_frequency="1d", signal_at="14:20",
                                   signal_data_end="current"))
    with caplog.at_level(logging.INFO,
                         logger="core.engine.live.signal_scheduler"):
        batches = s.due(datetime(2026, 10, 26, 14, 20))
    assert batches
    rec = [r for r in caplog.records if "[signal-schedule] fire" in r.getMessage()]
    assert rec, "调度触发应有 [signal-schedule] fire 日志"
    msg = rec[0].getMessage()
    assert "session=7" in msg
    assert "portfolio=1" in msg
    assert "freq=1d" in msg
    assert "strategies=1" in msg


def test_due_skip_logs_signal_schedule(caplog):
    """超补算窗口被跳过时落 [signal-schedule] skip INFO（14:20 是否执行的核对项）。"""
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    s = _scheduler(days, _strategy())
    with caplog.at_level(logging.INFO,
                         logger="core.engine.live.signal_scheduler"):
        assert s.due(datetime(2026, 10, 26, 9, 31)) == []
    rec = [r for r in caplog.records if "[signal-schedule] skip" in r.getMessage()]
    assert rec, "超窗跳过应有 [signal-schedule] skip 日志"
    assert "strategies=1" in rec[0].getMessage()


def test_execute_logs_signal_schedule(caplog):
    """批次执行完成落 [signal-schedule] execute INFO（模式/公式/耗时/代码数）。"""
    strategy = _strategy(signal_frequency="1d", signal_at="14:30",
                         signal_data_end="completed")
    md = ExecuteMarketData()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, FakeMonitor(), FakeAlert(),
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1d", period="1d", mode="inject",
        bar_time=datetime(2026, 10, 26, 14, 30),
        data_end_date=date(2026, 10, 23),
    )
    with caplog.at_level(logging.INFO,
                         logger="core.engine.live.signal_scheduler"):
        sched.execute(batch)
    rec = [r for r in caplog.records
           if "[signal-schedule] execute" in r.getMessage()]
    assert rec, "批次执行完成应有 [signal-schedule] execute 日志"
    msg = rec[0].getMessage()
    assert "session=7" in msg
    assert "mode=inject" in msg
    assert "codes=1" in msg
