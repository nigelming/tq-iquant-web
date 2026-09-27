# -*- coding: utf-8 -*-
"""RegimeAlpha 分析器单测（TR-7.1 ~ TR-7.3，FR-7 ~ FR-10）。

构造「上涨段跟涨、反弹段不弹、震荡段高频磨损」场景，验证：
- 状态总表 regime_table 四态齐全、方向正确（TR-7.1）
- 换手率/成本/月均笔数与从 trades 独立聚合一致，容差 1e-2（TR-7.2）
- 缺态样本时 payload 结构完整、键不缺失、安全空值、不抛错（TR-7.3）

标签为手工给定（分段器正确性由 test_regime.py 覆盖），分析器只测
「标签×净值×trades」的聚合逻辑。
"""
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from core.engine.regime_alpha import (
    BIG_UP_TESTS,
    QUADRANT_STYLE_INDICES,
    ROLLING_EXPOSURE_WINDOW,
    WHIPSAW_DAYS,
    RegimeAlphaAnalyzer,
)

_START = pd.Timestamp("2023-01-06")  # 周五
STATES = ("bull", "bear", "range", "rebound")


def _compound(closes, a, b):
    """周收盘 closes[a..b]（含）的累计收益，周收益按 pct_change 口径。"""
    r = 1.0
    for i in range(a, b + 1):
        r *= closes[i] / closes[i - 1]
    return r - 1.0


def _build_market():
    """80 周场景：w0-29 预热（不给标签）/ w30-44 bull / w45-59 bear /
    w60-64 rebound / w65-79 range。日频周内恒定，W-FRI 周收盘=周给定值。

    返回 (s_daily, b_daily, sc, bc, fridays, regime_weeks)。
    """
    fridays = [_START + timedelta(weeks=i) for i in range(80)]
    labels = {}
    for i in range(30, 45):
        labels[i] = "bull"
    for i in range(45, 60):
        labels[i] = "bear"
    for i in range(60, 65):
        labels[i] = "rebound"
    for i in range(65, 80):
        labels[i] = "range"

    bc = [100.0] * 30
    for i in range(30, 45):  # bull：+2.2%/周；w35 +3.5%、w40 +5% 两个大涨周
        m = 1.022
        if i == 35:
            m = 1.035
        if i == 40:
            m = 1.05
        bc.append(bc[-1] * m)
    for _ in range(45, 60):  # bear：-3%/周
        bc.append(bc[-1] * 0.97)
    for _ in range(60, 65):  # rebound：+2%/周 ≈ +10.4%，未收复前高
        bc.append(bc[-1] * 1.02)
    for _ in range(65, 80):  # range：横盘
        bc.append(bc[-1])

    sc = [100.0] * 30
    for _ in range(30, 45):  # 跟涨 +2.4%
        sc.append(sc[-1] * 1.024)
    for _ in range(45, 60):  # 跌得少 -1.5%
        sc.append(sc[-1] * 0.985)
    for _ in range(60, 65):  # 反弹不弹 -0.5%/周
        sc.append(sc[-1] * 0.995)
    for _ in range(65, 80):  # 横盘
        sc.append(sc[-1])

    def _daily(closes):
        idx, vals = [], []
        for i, c in enumerate(closes):
            fri = fridays[i]
            for k in range(5):  # 周一..周五
                idx.append(fri - timedelta(days=4 - k))
                vals.append(c)
        return pd.Series(vals, index=pd.DatetimeIndex(idx), dtype=float)

    regime_weeks = [
        {"date": fridays[i], "close": bc[i], "label": labels[i]}
        for i in sorted(labels)
    ]
    return _daily(sc), _daily(bc), sc, bc, fridays, regime_weeks


def _trade(bar_time, ttype, price, amount=None, qty=1000, commission=5.0,
           stamp=0.0, code="000001.SZ"):
    return SimpleNamespace(
        bar_time=bar_time, trade_type=ttype, stock_code=code, price=price,
        quantity=qty, amount=amount if amount is not None else price * qty,
        commission=commission, stamp_duty=stamp,
    )


def _build_trades(fridays):
    """震荡段 6 笔假突破磨损（买后 2 个交易日小亏平仓）+ 1 笔跨 3 周
    盈利交易（>WHIPSAW_DAYS 不算磨损）；bull 段 1 笔盈利趋势交易。"""
    trades = []
    for w in range(65, 70):  # 5 笔磨损
        fri = fridays[w]
        trades.append(_trade(fri - timedelta(days=4), "buy", 10.0))
        trades.append(_trade(fri - timedelta(days=2), "sell", 9.9,
                             amount=9900.0, stamp=10.0))
    # 第 6 笔磨损 + 同周盈利建仓（周二，FIFO 顺序保证周三卖出配周一仓）
    fri70 = fridays[70]
    trades.append(_trade(fri70 - timedelta(days=4), "buy", 10.0))
    trades.append(_trade(fri70 - timedelta(days=3), "buy", 10.0))
    trades.append(_trade(fri70 - timedelta(days=2), "sell", 9.9,
                         amount=9900.0, stamp=10.0))
    trades.append(_trade(fridays[73], "sell", 10.5, amount=10500.0, stamp=10.5))
    # 趋势段盈利交易（bull w33 周一买、周五卖，毛 PnL +1000）
    fri33 = fridays[33]
    trades.append(_trade(fri33 - timedelta(days=4), "buy", 10.0, code="600000.SH"))
    trades.append(_trade(fri33, "sell", 11.0, amount=11000.0, stamp=11.0,
                         code="600000.SH"))
    return trades


@pytest.fixture(scope="module")
def market():
    s_daily, b_daily, sc, bc, fridays, regime_weeks = _build_market()
    trades = _build_trades(fridays)
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=trades, regime_weeks=regime_weeks,
        strategy_name="测试策略", benchmark_name="测试基准",
    )
    return an.analyze(), sc, bc, fridays, trades, s_daily


# ---------------------------------------------------------------------------
# TR-7.1 状态总表 + 三块方向
# ---------------------------------------------------------------------------

def test_tr71_regime_table_states(market):
    p, sc, bc, fridays, *_ = market
    table = p["regime_table"]
    assert set(table) == set(STATES)

    bull = table["bull"]
    assert bull["weeks"] == 15
    assert bull["ratio"] == pytest.approx(15 / 50)
    assert bull["strategy_return"] == pytest.approx(_compound(sc, 30, 44), rel=1e-9)
    assert bull["benchmark_return"] == pytest.approx(_compound(bc, 30, 44), rel=1e-9)
    assert bull["excess_return"] == pytest.approx(
        bull["strategy_return"] - bull["benchmark_return"], rel=1e-9)
    assert bull["win_rate"] == 1.0
    assert bull["max_drawdown"] == pytest.approx(0.0, abs=1e-12)
    assert bull["turnover"] is not None and bull["turnover"] > 0

    bear = table["bear"]
    assert bear["weeks"] == 15
    assert bear["strategy_return"] == pytest.approx(_compound(sc, 45, 59), rel=1e-9)
    assert bear["strategy_return"] < 0
    assert bear["benchmark_return"] < 0
    assert bear["max_drawdown"] == pytest.approx(sc[59] / sc[45] - 1, rel=1e-6)

    reb = table["rebound"]
    assert reb["weeks"] == 5
    assert reb["ratio"] == pytest.approx(5 / 50)
    assert reb["strategy_return"] < 0            # 反弹不弹
    assert reb["excess_return"] < 0              # 跑输反弹的基准
    assert -0.05 < reb["max_drawdown"] < -0.01

    rng = table["range"]
    assert rng["weeks"] == 15
    assert abs(rng["strategy_return"]) < 1e-9    # 横盘净值为真值
    assert rng["win_rate"] == 0.0
    assert rng["max_drawdown"] == pytest.approx(0.0, abs=1e-12)


def test_tr71_meta(market):
    p, *_ = market
    meta = p["meta"]
    assert meta["strategy_name"] == "测试策略"
    assert meta["benchmark_name"] == "测试基准"
    assert meta["n_weeks_labeled"] == 50
    assert meta["state_week_counts"] == {"bull": 15, "bear": 15,
                                         "range": 15, "rebound": 5}


def test_tr71_opportunity(market):
    p, sc, bc, fridays, *_ = market
    opp = p["opportunity"]
    assert opp["n_bull_weeks"] == 15

    s_bull = [sc[i] / sc[i - 1] - 1 for i in range(30, 45)]
    b_bull = [bc[i] / bc[i - 1] - 1 for i in range(30, 45)]
    assert opp["up_capture"] == pytest.approx(
        sum(s_bull) / len(s_bull) / (sum(b_bull) / len(b_bull)), rel=1e-9)
    assert opp["miss_ratio"] == 0.0              # 上涨段全程跟涨，无踏空

    tests = opp["big_up_tests"]
    assert [t["threshold"] for t in tests] == list(BIG_UP_TESTS)
    assert tests[0]["n"] == 2                    # 3.5% 与 5% 两个大涨周
    assert tests[0]["win_rate"] == 1.0
    assert tests[0]["avg_strategy"] == pytest.approx(0.024, rel=1e-9)
    assert tests[1]["n"] == 1
    assert tests[1]["avg_benchmark"] == pytest.approx(0.05, rel=1e-9)

    cats = opp["cats"]
    assert cats["同步涨"]["count"] == 14         # 5% 周策略 +2.4% < 0.5×5%
    assert cats["涨得少"]["count"] == 1
    assert cats["踏空"]["count"] == 0
    assert cats["逆势跌"]["count"] == 0
    assert cats["同步涨"]["avg_strategy"] is not None


def test_tr71_rebound(market):
    p, sc, bc, fridays, *_ = market
    reb = p["rebound"]
    assert reb["n_segments"] == 1
    assert reb["avg_elasticity"] is not None

    seg = reb["segments"][0]
    assert seg["start_date"] == str(fridays[60].date())
    assert seg["end_date"] == str(fridays[64].date())
    assert seg["trough_date"] == str(fridays[59].date())  # bear 段最低点
    assert seg["weeks"] == 5
    assert seg["benchmark_rebound_pct"] == pytest.approx(bc[64] / bc[59] - 1, rel=1e-9)
    assert seg["strategy_rebound_pct"] == pytest.approx(sc[64] / sc[59] - 1, rel=1e-9)
    assert seg["elasticity"] == pytest.approx(
        seg["strategy_rebound_pct"] / seg["benchmark_rebound_pct"], rel=1e-9)
    assert seg["elasticity"] < 0                 # 不弹
    # 前高（bull 末周）未被收复
    assert seg["benchmark_recovered"] is False
    assert seg["benchmark_recover_days"] is None
    assert seg["strategy_recovered"] is False
    assert seg["strategy_recover_days"] is None


def test_tr71_neutral(market):
    p, *_ = market
    neu = p["neutral"]
    assert neu["n_range_weeks"] == 15
    assert neu["range_ratio"] == pytest.approx(15 / 50)
    assert neu["cumulative_return"] == pytest.approx(0.0, abs=1e-12)
    assert neu["annualized_return"] == pytest.approx(0.0, abs=1e-12)
    assert neu["win_rate"] == 0.0
    assert neu["whipsaw_count"] == 6             # 高频磨损
    assert neu["whipsaw_days"] == WHIPSAW_DAYS
    pnl = neu["avg_trade_pnl"]
    assert pnl["trend_count"] == 1
    assert pnl["trend"] == pytest.approx(1000.0, rel=1e-9)
    assert pnl["range_count"] == 7
    assert pnl["range"] == pytest.approx((6 * -100.0 + 500.0) / 7, rel=1e-9)
    # 成本/毛利比：range 成本 140.5 / 毛利 500
    assert neu["cost_gross_ratio"] == pytest.approx(140.5 / 500.0, rel=1e-9)


# ---------------------------------------------------------------------------
# TR-7.2 与 trades 独立聚合交叉断言（容差 1e-2）
# ---------------------------------------------------------------------------

def _range_days(fridays):
    days = set()
    for w in range(65, 80):
        for k in range(5):
            days.add(fridays[w] - timedelta(days=4 - k))
    return days


def test_tr72_cross_aggregation(market):
    p, sc, bc, fridays, trades, s_daily = market
    rng_days = _range_days(fridays)
    rng_trades = [t for t in trades
                  if pd.Timestamp(t.bar_time) in rng_days]
    amount = sum(t.amount for t in rng_trades)
    cost = sum(t.commission + t.stamp_duty for t in rng_trades)

    s_rng = s_daily[s_daily.index.isin(rng_days)]
    avg_total = float(s_rng.mean())
    first_total = float(s_rng.iloc[0])
    span_days = (s_rng.index[-1] - s_rng.index[0]).days
    years = span_days / 365.25
    months = max(span_days, 1) / 30.4375

    expected_turnover = amount / avg_total / years
    expected_cost_ratio = cost / first_total
    expected_monthly = len(rng_trades) / months

    neu = p["neutral"]
    assert neu["annual_turnover"] == pytest.approx(expected_turnover, rel=1e-2)
    assert neu["monthly_trades"] == pytest.approx(expected_monthly, rel=1e-2)
    assert p["regime_table"]["range"]["turnover"] == pytest.approx(
        expected_turnover, rel=1e-2)
    assert p["regime_table"]["range"]["cost_ratio"] == pytest.approx(
        expected_cost_ratio, rel=1e-2)

    # bull 段换手交叉验证（1 买 1 卖）
    bull_days = set()
    for w in range(30, 45):
        for k in range(5):
            bull_days.add(fridays[w] - timedelta(days=4 - k))
    bull_trades = [t for t in trades if pd.Timestamp(t.bar_time) in bull_days]
    s_bull_d = s_daily[s_daily.index.isin(bull_days)]
    exp_bull_to = (
        sum(t.amount for t in bull_trades)
        / float(s_bull_d.mean())
        / ((s_bull_d.index[-1] - s_bull_d.index[0]).days / 365.25)
    )
    assert p["regime_table"]["bull"]["turnover"] == pytest.approx(
        exp_bull_to, rel=1e-2)


# ---------------------------------------------------------------------------
# TR-7.3 缺态样本：结构完整、安全空值、不抛错
# ---------------------------------------------------------------------------

def _build_flat(fridays_n=40):
    fridays = [_START + timedelta(weeks=i) for i in range(fridays_n)]
    bc = [100.0] * fridays_n
    sc = [100.0]
    for i in range(1, fridays_n):
        sc.append(sc[-1] * (1.01 if i % 2 == 1 else 0.995))

    def _daily(closes):
        idx, vals = [], []
        for i, c in enumerate(closes):
            fri = fridays[i]
            for k in range(5):
                idx.append(fri - timedelta(days=4 - k))
                vals.append(c)
        return pd.Series(vals, index=pd.DatetimeIndex(idx), dtype=float)

    regime_weeks = [{"date": fridays[i], "close": bc[i], "label": "range"}
                    for i in range(fridays_n)]
    return _daily(sc), _daily(bc), fridays, regime_weeks


def test_tr73_missing_states_structure():
    s_daily, b_daily, fridays, regime_weeks = _build_flat()
    # regime_weeks 以 segment_regime 完整返回 dict 形式传入（接口兼容）
    an = RegimeAlphaAnalyzer(s_daily, b_daily, trades=[],
                             regime_weeks={"weeks": regime_weeks,
                                           "segments": [], "counts": {}})
    p = an.analyze()

    assert set(p) == {"meta", "regime_table", "opportunity", "rebound",
                      "neutral", "style", "dispersion", "exposure",
                      "quadrant_exposure"}
    table = p["regime_table"]
    assert set(table) == set(STATES)
    for st in ("bull", "bear", "rebound"):
        blk = table[st]
        assert blk["weeks"] == 0
        assert blk["ratio"] == 0.0
        assert blk["strategy_return"] is None
        assert blk["benchmark_return"] is None
        assert blk["excess_return"] is None
        assert blk["win_rate"] is None
        assert blk["max_drawdown"] is None
        assert blk["turnover"] is None
        assert blk["cost_ratio"] is None

    opp = p["opportunity"]
    assert opp["n_bull_weeks"] == 0
    assert opp["up_capture"] is None
    assert opp["miss_ratio"] == 0.0
    assert len(opp["big_up_tests"]) == len(BIG_UP_TESTS)
    assert all(t["n"] == 0 and t["avg_strategy"] is None
               for t in opp["big_up_tests"])
    assert all(c["count"] == 0 and c["avg_strategy"] is None
               for c in opp["cats"].values())

    reb = p["rebound"]
    assert reb["segments"] == []
    assert reb["n_segments"] == 0
    assert reb["avg_elasticity"] is None

    neu = p["neutral"]
    assert neu["n_range_weeks"] == 39            # 首周无周收益
    assert neu["range_ratio"] == pytest.approx(1.0)
    assert neu["win_rate"] == pytest.approx(20 / 39)
    assert neu["monthly_trades"] == 0.0
    assert neu["annual_turnover"] == 0.0
    assert neu["whipsaw_count"] == 0
    assert neu["cost_gross_ratio"] is None
    assert neu["avg_trade_pnl"] == {"trend": None, "trend_count": 0,
                                    "range": None, "range_count": 0}


def test_tr73_empty_and_single_day_inputs():
    empty = pd.Series(dtype=float)
    p = RegimeAlphaAnalyzer(empty, empty).analyze()
    assert set(p) == {"meta", "regime_table", "opportunity", "rebound",
                      "neutral", "style", "dispersion", "exposure",
                      "quadrant_exposure"}
    assert all(p["regime_table"][st]["weeks"] == 0 for st in STATES)
    assert p["rebound"]["segments"] == []
    assert p["neutral"]["n_range_weeks"] == 0

    one = pd.Series([100.0], index=pd.DatetimeIndex([_START]))
    p2 = RegimeAlphaAnalyzer(one, one).analyze()  # 不抛错
    assert p2["neutral"]["n_range_weeks"] == 0


def test_tr73_dict_trades_whipsaw():
    s_daily, b_daily, fridays, regime_weeks = _build_flat()
    fri = fridays[10]
    trades = [
        {"bar_time": fri - timedelta(days=4), "trade_type": "buy",
         "stock_code": "000001.SZ", "price": 10.0, "quantity": 1000,
         "amount": 10000.0, "commission": 5.0, "stamp_duty": 0.0},
        {"bar_time": fri - timedelta(days=4), "trade_type": "sell",
         "stock_code": "000001.SZ", "price": 9.9, "quantity": 1000,
         "amount": 9900.0, "commission": 5.0, "stamp_duty": 10.0},
    ]
    an = RegimeAlphaAnalyzer(s_daily, b_daily, trades=trades,
                             regime_weeks=regime_weeks)
    p = an.analyze()
    assert p["neutral"]["whipsaw_count"] == 1    # 当日小亏平仓 → 磨损
    assert p["neutral"]["avg_trade_pnl"]["range_count"] == 1


# ---------------------------------------------------------------------------
# TR-11 / TR-12：风格轮动 + 分化度 + 隐含风格暴露（FR-13 ~ FR-16）
# ---------------------------------------------------------------------------

def _style_market(n_weeks=80, leader_code="399006.SZ", leader_weekly=0.03,
                  others_weekly=0.0, noise=0.003):
    """构造风格场景：指定指数持续领涨，其余横盘；策略≈领涨指数。

    返回 (s_daily, b_daily, benchmark_closes, fridays, regime_weeks)。
    regime_weeks 全部标 bull，让 labeled 周覆盖整个区间（风格计算需要
    labeled 周索引，见 _style 中 common 与 labeled 的交集）。
    noise 为周收益微扰（避免恒定收益→方差为零→相关系数 NaN）。
    """
    from core.engine.crisis_alpha import CRISIS_BENCHMARKS
    fridays = [_START + timedelta(weeks=i) for i in range(n_weeks)]

    def _week_closes(base_ret, phase=0):
        closes = [100.0]
        for i in range(n_weeks - 1):
            # 奇偶周微扰，保证序列有方差（相关/beta 可算）；phase 控制反相
            sign = 1 if (i + phase) % 2 == 0 else -1
            closes.append(closes[-1] * (1 + base_ret + sign * noise))
        return closes

    leader_closes = _week_closes(leader_weekly, phase=0)
    other_closes = _week_closes(others_weekly, phase=1)  # 反相，避免与领涨指数同涨同跌
    # 主基准用沪深300（横盘），策略≈领涨指数
    benchmark_closes = {}
    for spec in CRISIS_BENCHMARKS:
        code = spec["code"]
        closes = leader_closes if code == leader_code else other_closes
        idx, vals = [], []
        for i, c in enumerate(closes):
            fri = fridays[i]
            for k in range(5):
                idx.append(fri - timedelta(days=4 - k))
                vals.append(c)
        benchmark_closes[code] = pd.Series(
            vals, index=pd.DatetimeIndex(idx), dtype=float)
    # 策略日频 = 领涨指数（近似）
    s_daily = benchmark_closes[leader_code].copy()
    b_daily = benchmark_closes["000300.SH"].copy()
    regime_weeks = [
        {"date": fridays[i], "close": other_closes[i], "label": "bull"}
        for i in range(1, n_weeks)
    ]
    return s_daily, b_daily, benchmark_closes, fridays, regime_weeks


def test_tr11_1_tech_growth_leadership_labeled():
    """TR-11.1：创业板持续领涨 → 风格标签含科技成长，矩阵对应行有值。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="399006.SZ", leader_weekly=0.03,
        others_weekly=0.0)
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw,
        benchmark_closes=bc)
    p = an.analyze()
    style = p["style"]
    assert style["available_codes"]
    # 至少有一些周被判为科技成长
    labels = [w["label"] for w in style["weeks"]]
    assert "科技成长" in labels
    blk = style["matrix"]["科技成长"]
    assert blk["weeks"] > 0
    assert blk["strategy_return"] is not None
    assert blk["excess_return"] is not None
    # skipped 应为空（6 指数均构造）
    assert style["skipped"] == []


def test_tr11_2_dispersion_from_input_and_skipped():
    """TR-11.2：分化度序列由输入价格计算（无硬编码）；缺指数进入 skipped。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="399006.SZ", leader_weekly=0.03,
        others_weekly=-0.01)
    # 移除科创50（模拟不可得）
    del bc["000688.SH"]
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw, benchmark_closes=bc)
    p = an.analyze()
    disp = p["dispersion"]
    assert "科创50" in disp["skipped"]
    # 分化度序列非空且全为非负数
    assert len(disp["series"]) > 0
    assert all(s["dispersion"] >= 0 for s in disp["series"])
    # 分桶三档 count 之和≈序列长度
    total = sum(disp["buckets"][k]["count"] for k in ("low", "mid", "high"))
    assert total == len(disp["series"])
    # 分化度 = 截面 std，可由当周 5 个指数周收益独立复算
    import math
    sample = disp["series"][10]
    ts = pd.Timestamp(sample["date"])
    s_week_ret = an.s_week_ret
    if ts in s_week_ret.index:
        rets = []
        for code in an.style_available_codes:
            if ts in an.style_week_rets[code].index:
                rets.append(float(an.style_week_rets[code].loc[ts]))
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
            assert sample["dispersion"] == pytest.approx(expected, abs=1e-9)


def test_tr11_3_no_benchmark_closes_safe_empty():
    """不传 benchmark_closes → style/dispersion/exposure 安全空结构，不抛错。"""
    s_daily, b_daily, sc, bc, fridays, rw = _build_market()
    an = RegimeAlphaAnalyzer(s_daily, b_daily, trades=[], regime_weeks=rw)
    p = an.analyze()
    assert p["style"]["available_codes"] == []
    assert p["dispersion"]["series"] == []
    assert p["exposure"]["dates"] == []
    assert p["quadrant_exposure"]["dates"] == []
    assert p["quadrant_exposure"]["available_codes"] == []


def test_tr12_1_rolling_exposure_single_index_tracking():
    """TR-12.1：策略≈某单指数 → 该指数滚动相关接近 1、beta 接近 1。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="399006.SZ", leader_weekly=0.02,
        others_weekly=0.01)
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw, benchmark_closes=bc)
    p = an.analyze()
    exp = p["exposure"]
    assert "399006.SZ" in exp["corr"]
    cyb_corr = [v for v in exp["corr"]["399006.SZ"] if v is not None]
    cyb_beta = [v for v in exp["beta"]["399006.SZ"] if v is not None]
    assert cyb_corr, "应有非空滚动相关序列"
    # 末段 52 周窗口内，策略=创业板 → 相关≈1
    assert cyb_corr[-1] == pytest.approx(1.0, abs=0.05)
    assert cyb_beta[-1] == pytest.approx(1.0, abs=0.1)
    # 沪深300 与策略相关性应较低（横盘 vs 上涨）
    hs_corr = [v for v in exp["corr"]["000300.SH"] if v is not None]
    assert hs_corr[-1] < 0.5


def test_tr12_2_missing_index_skipped_no_nan_misalignment():
    """TR-12.2：缺指数 → skipped；exposure 的 corr/beta 仅含可得指数。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="000300.SH", leader_weekly=0.02,
        others_weekly=0.0)
    del bc["000688.SH"]   # 科创50 不可得
    del bc["000852.SH"]   # 中证1000 不可得
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw, benchmark_closes=bc)
    p = an.analyze()
    exp = p["exposure"]
    assert "科创50" in exp["skipped"]
    assert "中证1000" in exp["skipped"]
    # 可得指数的 corr/beta 数组长度一致且等于 dates 长度
    n = len(exp["dates"])
    for code in exp["available_codes"]:
        assert len(exp["corr"][code]) == n
        assert len(exp["beta"][code]) == n
    # 不可得指数不在 corr/beta 键中
    assert "000688.SH" not in exp["corr"]
    assert "000852.SH" not in exp["corr"]


# ---------------------------------------------------------------------------
# 巨潮四象限风格暴露（增强A：规模×成长/价值 纯风格口径，数据仅走 TQ）
# ---------------------------------------------------------------------------

def _quadrant_closes(n_weeks, fridays, leader_code, leader_weekly=0.02,
                     others_weekly=0.0, noise=0.003):
    """构造四象限指数日线：leader 持续上涨（phase=0 微扰），其余横盘反相。

    与 _style_market 相同公式/默认参数 → leader 日收盘价与
    _style_market(leader_weekly=0.02) 的 s_daily 完全一致，相关=1。
    """
    def _week_closes(base_ret, phase=0):
        closes = [100.0]
        for i in range(n_weeks - 1):
            sign = 1 if (i + phase) % 2 == 0 else -1
            closes.append(closes[-1] * (1 + base_ret + sign * noise))
        return closes

    leader = _week_closes(leader_weekly, phase=0)
    other = _week_closes(others_weekly, phase=1)
    out = {}
    for spec in QUADRANT_STYLE_INDICES:
        closes = leader if spec["code"] == leader_code else other
        idx, vals = [], []
        for i, c in enumerate(closes):
            fri = fridays[i]
            for k in range(5):
                idx.append(fri - timedelta(days=4 - k))
                vals.append(c)
        out[spec["code"]] = pd.Series(
            vals, index=pd.DatetimeIndex(idx), dtype=float)
    return out


def test_tr12_3_quadrant_exposure_pure_style_tracking():
    """策略≈小盘成长 → 该象限 corr≈1/beta≈1，大盘价值方向不显著。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="399006.SZ", leader_weekly=0.02,
        others_weekly=0.01)
    qc = _quadrant_closes(80, fridays, "399376.SZ", leader_weekly=0.02)
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw,
        benchmark_closes=bc, quadrant_closes=qc)
    p = an.analyze()
    qe = p["quadrant_exposure"]
    assert qe["window"] == ROLLING_EXPOSURE_WINDOW
    assert set(qe["available_codes"]) == {
        s["code"] for s in QUADRANT_STYLE_INDICES}
    assert qe["skipped"] == []
    corr = [v for v in qe["corr"]["399376.SZ"] if v is not None]
    beta = [v for v in qe["beta"]["399376.SZ"] if v is not None]
    assert corr, "应有非空滚动相关序列"
    assert corr[-1] == pytest.approx(1.0, abs=0.05)
    assert beta[-1] == pytest.approx(1.0, abs=0.1)
    dv_corr = [v for v in qe["corr"]["399373.SZ"] if v is not None]
    assert dv_corr[-1] < 0.5


def test_tr12_4_quadrant_missing_skipped_and_absent_safe():
    """缺象限指数 → skipped；不传 quadrant_closes → 安全空结构不抛错。"""
    s_daily, b_daily, bc, fridays, rw = _style_market(
        n_weeks=80, leader_code="399006.SZ", leader_weekly=0.02,
        others_weekly=0.01)
    qc = _quadrant_closes(80, fridays, "399376.SZ", leader_weekly=0.02)
    del qc["399376.SZ"]   # 小盘成长 不可得
    del qc["399377.SZ"]   # 小盘价值 不可得
    an = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw,
        benchmark_closes=bc, quadrant_closes=qc)
    qe = an.analyze()["quadrant_exposure"]
    assert "小盘成长" in qe["skipped"]
    assert "小盘价值" in qe["skipped"]
    assert "399376.SZ" not in qe["corr"]
    assert "399377.SZ" not in qe["corr"]
    n = len(qe["dates"])
    for code in qe["available_codes"]:
        assert len(qe["corr"][code]) == n
        assert len(qe["beta"][code]) == n

    # 完全不传 quadrant_closes → 安全空（skipped 不诬报，与 exposure 口径一致）
    an2 = RegimeAlphaAnalyzer(
        s_daily, b_daily, trades=[], regime_weeks=rw, benchmark_closes=bc)
    qe2 = an2.analyze()["quadrant_exposure"]
    assert qe2["available_codes"] == []
    assert qe2["dates"] == []
    assert qe2["skipped"] == []
    assert qe2["corr"] == {}

