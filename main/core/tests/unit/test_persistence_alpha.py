import datetime
from decimal import Decimal
from core.engine.persistence_alpha import compute_persistence, decide_verdict


def _mk_year(year, factors, base=Decimal("1.0")):
    """factors 为逐交易日乘子列表 → {date: Decimal} NAV（跳过周末）。"""
    out = {}
    cur = base
    d = datetime.date(year, 1, 1)
    i = 0
    while i < len(factors):
        if d.weekday() < 5:
            cur = cur * Decimal(str(factors[i]))
            out[d] = cur
            i += 1
        d += datetime.timedelta(days=1)
    return out


def _mk_closes(year, growth):
    """单一指数当年日收盘：日复利 → 年末精确到 1+growth。"""
    daily = (1.0 + growth) ** (1.0 / 240)
    return _mk_year(year, [daily] * 240)


def test_decide_insufficient():
    years = [{"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.1,
              "sharpe": 2.0, "calmar": 6.0, "win_rate": 0.6, "profit_factor": 1.8,
              "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.3},
              "bench_returns": {}}]
    assert decide_verdict(years, "000300.SH")["level"] == "insufficient"


def test_decide_alpha_decay():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.08,
         "sharpe": 2.5, "calmar": 7.0, "win_rate": 0.6, "profit_factor": 1.9,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.30}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.20,
         "sharpe": 1.0, "calmar": 1.5, "win_rate": 0.45, "profit_factor": 1.2,
         "turnover": 3.5, "cost_ratio": 0.002, "excess": {"000300.SH": 0.05}, "bench_returns": {}},
    ]
    v = decide_verdict(years, "000300.SH")
    assert v["level"] == "alpha_decay"
    assert "超额" in v["reasons"][0]


def test_decide_style_pendulum():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.12,
         "sharpe": 2.0, "calmar": 5.0, "win_rate": 0.55, "profit_factor": 1.7,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.20}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.13,
         "sharpe": 2.0, "calmar": 4.9, "win_rate": 0.55, "profit_factor": 1.7,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.22}, "bench_returns": {}},
    ]
    assert decide_verdict(years, "000300.SH")["level"] == "style_pendulum"


def test_decide_stable():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.10,
         "sharpe": 1.8, "calmar": 3.0, "win_rate": 0.5, "profit_factor": 1.5,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.05}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.32, "max_drawdown": 0.10,
         "sharpe": 1.9, "calmar": 3.2, "win_rate": 0.52, "profit_factor": 1.6,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.06}, "bench_returns": {}},
    ]
    assert decide_verdict(years, "000300.SH")["level"] == "stable"


def test_partial_year_marked_and_excluded():
    nav = dict(_mk_year(2024, [1.002] * 200))          # 满年
    nav.update(_mk_year(2026, [1.001] * 30))           # 部分年（<60 日）
    closes = {"000300.SH": _mk_closes(2024, 0.2)}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    years = out["years"]
    assert any(y["year"] == 2024 and not y["is_partial"] for y in years)
    assert any(y["year"] == 2026 and y["is_partial"] for y in years)
    assert out["verdict"]["level"] == "insufficient"   # 仅 1 个满年


def test_excess_and_benchmark_returns():
    nav = dict(_mk_year(2024, [1.0025] * 240))         # ~+81%
    closes = {"000300.SH": _mk_closes(2024, 0.3)}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    y = out["years"][0]
    assert abs(y["bench_returns"]["000300.SH"] - 0.3) < 0.01
    assert abs((y["nav_return"] or 0) - 0.3 - y["excess"]["000300.SH"]) < 0.01
    assert out["benchmarks"][0]["is_main"] is True


def test_rolling_annualized_and_nulls():
    nav = dict(_mk_year(2024, [1.001] * 240))
    nav.update(_mk_year(2025, [1.001] * 240))
    closes = {"000300.SH": dict(_mk_year(2024, [1.0005] * 240))}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    r = out["rolling"]
    assert r["dates"][0] and r["strat_12m"][0] is None      # 首个窗口历史不足
    assert any(v is not None for v in r["strat_12m"])       # 有足够历史后出现值
    assert len(r["dates"]) == len(r["strat_6m"]) == len(r["strat_12m"]) == len(r["bench_12m"])


def _row(year, nav_return=0.3, excess=0.05, sharpe=1.8, calmar=3.0,
         mdd=0.10, win=0.50, pf=1.5, turnover=2.0, cost=0.001):
    return {"year": year, "is_partial": False, "nav_return": nav_return,
            "max_drawdown": mdd, "sharpe": sharpe, "calmar": calmar,
            "win_rate": win, "profit_factor": pf, "turnover": turnover,
            "cost_ratio": cost, "excess": {"000300.SH": excess}, "bench_returns": {}}


def test_insufficient_when_main_benchmark_missing():
    """主基准在判定窗口缺数据时必须判 insufficient，不能默认「稳定」。"""
    years = [_row(2024), _row(2025)]
    for y in years:
        y["excess"] = {}
    v = decide_verdict(years, "000300.SH", "沪深300")
    assert v["level"] == "insufficient"
    assert "缺数据" in v["reasons"][0]


def test_small_excess_decline_is_mixed_not_decay():
    """3 年超额累计仅降 2pp（低于 3pp 门槛）→ 轻微收敛/信号混合，不判阿尔法衰减。"""
    years = [
        _row(2023, nav_return=0.30, excess=0.30),
        _row(2024, nav_return=0.32, excess=0.29),
        _row(2025, nav_return=0.34, excess=0.28),
    ]
    v = decide_verdict(years, "000300.SH", "沪深300")
    assert v["level"] == "mixed"
    assert v["signals"][0]["status"] == "mixed"
    assert v["signals"][0]["word"] == "轻微收敛"
    assert v["window_years"] == [2023, 2024, 2025]
    assert "超额 vs 沪深300" == v["signals"][0]["label"]


def test_excess_uses_common_trading_dates():
    """基准缺前 10 个交易日时，超额按共同交易日对齐，而不是基准自身首末日。"""
    nav = _mk_year(2024, [1.0] * 240)                      # 策略零收益
    closes = _mk_closes(2024, 0.3)
    keys = sorted(closes)
    closes = {d: closes[d] for d in keys[10:]}             # 基准缺前 10 日
    out = compute_persistence(nav, [], {"000300.SH": closes}, "000300.SH", "沪深300")
    y = out["years"][0]
    bench = y["bench_returns"]["000300.SH"]
    assert bench is not None and bench < 0.29              # 按共同区间少于全年 +30%
    # 策略零收益 → 超额 = -基准收益（excess 保留 4 位小数）
    assert abs(y["excess"]["000300.SH"] + bench) < 2e-4


def test_risk_within_noise_band_is_not_worsening():
    """夏普 -0.1 / 卡玛 -2% / 回撤 +0.5pp 均在噪声带内 → 风险不算恶化。"""
    years = [
        _row(2024, nav_return=0.50, excess=0.10, sharpe=2.0, calmar=5.0, mdd=0.10),
        _row(2025, nav_return=0.40, excess=0.11, sharpe=1.9, calmar=4.9, mdd=0.105),
    ]
    v = decide_verdict(years, "000300.SH")
    assert v["signals"][1]["status"] == "good"
    assert v["level"] == "style_pendulum"


def test_per_year_avg_position_utilization():
    """传入利用率序列 → 分年平均仓位利用率（口径同 Evaluator：tv>0 日的 mv/tv 均值）。"""
    nav = dict(_mk_year(2024, [1.001] * 240))
    util = {d: (Decimal("0.6") if i % 2 == 0 else Decimal("0.4"))
            for i, d in enumerate(sorted(nav))}
    out = compute_persistence(nav, [], {}, "000300.SH", "沪深300",
                              utilization_series=util)
    y = out["years"][0]
    assert y["avg_position_utilization"] is not None
    assert abs(y["avg_position_utilization"] - 0.5) < 1e-6


def test_per_year_utilization_missing_series_is_none():
    """未传利用率序列（老调用/老数据）→ 该字段 None，行为兼容。"""
    nav = dict(_mk_year(2024, [1.001] * 240))
    out = compute_persistence(nav, [], {}, "000300.SH", "沪深300")
    assert out["years"][0]["avg_position_utilization"] is None


def test_per_year_utilization_partial_days_skipped():
    """某几日缺利用率（停牌/无估值）→ 只对有值日均摊，不按 0 计入。"""
    nav = dict(_mk_year(2024, [1.001] * 240))
    dates = sorted(nav)
    util = {d: Decimal("0.25") for d in dates[:100]}   # 仅前 100 日有值，全部 0.25
    out = compute_persistence(nav, [], {}, "000300.SH", "沪深300",
                              utilization_series=util)
    y = out["years"][0]
    assert y["avg_position_utilization"] is not None
    assert abs(y["avg_position_utilization"] - 0.25) < 1e-6
