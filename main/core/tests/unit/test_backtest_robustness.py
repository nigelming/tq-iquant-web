from datetime import date, datetime
from decimal import Decimal

from core.engine.backtest_robustness import analyze_robustness


def _curve(days_values):
    return [{"snap_date": d, "total_value": v, "benchmark_value": None}
            for d, v in days_values]


def _trades(rows):
    return [{"stock_code": c, "trade_type": t, "amount": a,
             "commission": com, "stamp_duty": st} for c, t, a, com, st in rows]


def test_annual_returns_and_mdd():
    snaps = _curve([
        (date(2024, 1, 2), 100),
        (date(2024, 1, 3), 110),
        (date(2024, 1, 4), 99),   # 年内回撤 -10%
        (date(2024, 12, 31), 121),
        (date(2025, 1, 2), 121),
        (date(2025, 12, 31), 133.1),
    ])
    r = analyze_robustness(snaps, [], oos_ratio=0.5)
    annual = {a["year"]: a for a in r["annual"]}
    assert round(annual["2024"]["return"], 4) == 0.21   # 100 -> 121
    assert round(annual["2024"]["max_drawdown"], 4) == 0.1   # W5-L2：正号回撤
    assert round(annual["2025"]["return"], 4) == 0.1    # 121 -> 133.1
    assert r["total_return"] == round(133.1 / 100 - 1, 4)
    assert r["years_positive"] == "2/2"


def test_zero_net_value_returns_none_instead_of_crashing():
    """首末净值为 0 时 _return_ratio 返回 None，不能 round(None) 抛 TypeError。"""
    snaps = _curve([
        (date(2024, 1, 2), 0.0),
        (date(2024, 1, 3), 0.0),
        (date(2025, 1, 2), 0.0),
    ])
    r = analyze_robustness(snaps, [], oos_ratio=0.25)
    assert r["total_return"] is None
    assert r["benchmark_return"] is None


def test_rolling_3y_cagr():
    # 每年年末值：2014=100, 2015=110, 2016=121, 2017=133.1（每年 +10%）
    snaps = _curve([
        (date(2014, 1, 2), 100), (date(2014, 12, 31), 100),
        (date(2015, 1, 2), 100), (date(2015, 12, 31), 110),
        (date(2016, 1, 2), 110), (date(2016, 12, 31), 121),
        (date(2017, 1, 2), 121), (date(2017, 12, 31), 133.1),
    ])
    r = analyze_robustness(snaps, [], oos_ratio=0.25)
    r3 = r["rolling_3y"]
    # 4 个年份 → 2 个 3 年窗口
    assert len(r3) == 2
    assert r3[0]["start"] == "2014" and r3[0]["end"] == "2016"
    # W5-O3：CAGR 按窗口实际首末日期跨度年化（2014-01-02→2016-12-31 ≈ 2.995 年）
    assert round(r3[0]["cagr"], 4) == round(0.06571055110497115, 4)
    assert r3[1]["start"] == "2015" and r3[1]["end"] == "2017"
    assert round(r3[1]["cagr"], 4) == 0.1002                          # 100->133.1


def test_oos_split_75_25():
    # 4 个日历年：oos_ratio 0.25 → round(4*0.25)=1 → 样本内前3年，样本外后1年
    snaps = _curve([
        (date(2020, 1, 2), 100), (date(2020, 12, 31), 110),
        (date(2021, 1, 4), 110), (date(2021, 12, 31), 121),
        (date(2022, 1, 3), 121), (date(2022, 12, 30), 133.1),
        (date(2023, 1, 3), 133.1), (date(2023, 12, 29), 146.41),
    ])
    r = analyze_robustness(snaps, [], oos_ratio=0.25)
    oos = r["oos"]
    assert oos["split_year"] == "2023"
    assert oos["in_sample"]["start"][:4] == "2020"
    assert oos["in_sample"]["end"][:4] == "2022"
    assert oos["out_of_sample"]["start"][:4] == "2023"
    assert oos["out_of_sample"]["end"][:4] == "2023"
    # 样本内 100->133.1 = +33.1%，CAGR 按实际跨度年化（2020-01-02→2022-12-30）
    assert round(oos["in_sample"]["cagr"], 4) == 0.1003
    # 样本外 133.1->146.41 = +10%
    assert round(oos["out_of_sample"]["return"], 4) == 0.1


def test_oos_split_min_one_year():
    # 只有 2 个年份 → 样本内 1 年、样本外 1 年
    snaps = _curve([
        (date(2024, 1, 2), 100), (date(2024, 12, 31), 110),
        (date(2025, 1, 2), 110), (date(2025, 12, 31), 121),
    ])
    r = analyze_robustness(snaps, [], oos_ratio=0.25)
    oos = r["oos"]
    assert oos["split_year"] == "2025"
    assert oos["in_sample"]["start"][:4] == oos["in_sample"]["end"][:4] == "2024"
    assert oos["out_of_sample"]["start"][:4] == oos["out_of_sample"]["end"][:4] == "2025"


def test_concentration_from_trades():
    trades = _trades([
        ("A.SZ", "BUY", 1000, 5, 0),
        ("A.SZ", "SELL", 1500, 5, 0),     # 净 +490
        ("B.SZ", "BUY", 2000, 5, 0),
        ("B.SZ", "SELL", 1800, 5, 0),     # 净 -210
        ("C.SZ", "BUY", 3000, 5, 0),
        ("C.SZ", "SELL", 3900, 5, 0),     # 净 +890
    ])
    snaps = _curve([(date(2024, 1, 2), 100), (date(2024, 12, 31), 200)])
    r = analyze_robustness(snaps, trades, oos_ratio=0.5)
    conc = r["concentration"]
    assert conc["total_stocks"] == 3
    assert conc["profitable_stocks"] == 2
    top = conc["top"]
    assert top[0]["code"] == "C.SZ"          # 最高净利
    assert top[0]["pnl"] == 890
    total = 490 + (-210) + 890
    assert abs(conc["top20_share"] - total / total) < 1e-6  # 3 只全在前20 → 100%


def test_concentration_separates_unrealized_from_realized():
    """期末未平仓不得按现金流口径计入已实现净利（旧实现会把这笔买入成本算成负贡献）。"""
    trades = [
        {"stock_code": "A.SZ", "trade_type": "BUY", "amount": 1000,
         "commission": 0, "stamp_duty": 0, "price": 10, "quantity": 100},
        {"stock_code": "A.SZ", "trade_type": "SELL", "amount": 720,
         "commission": 0, "stamp_duty": 0, "price": 12, "quantity": 60},
    ]
    last_positions = [{"stock_code": "A.SZ", "quantity": "40",
                       "cost_value": "400", "market_value": "480"}]
    snaps = ([{"snap_date": date(2024, 1, 2), "total_value": 100, "benchmark_value": None,
               "positions": None},
              {"snap_date": date(2024, 12, 31), "total_value": 200, "benchmark_value": None,
               "positions": last_positions}])
    conc = analyze_robustness(snaps, trades, oos_ratio=0.5)["concentration"]
    # 已实现只认已平仓的 60 股：(12-10)*60 = 120；剩余 400 元成本不进已实现
    assert conc["total_realized_pnl"] == 120
    assert conc["gross_profit"] == 120
    assert conc["gross_loss"] == 0
    assert conc["unrealized"]["total_pnl"] == 80      # 480 - 400
    assert conc["unrealized"]["cost"] == 400
    assert [r["code"] for r in conc["unrealized"]["rows"]] == ["A.SZ"]
    assert conc["lots"]["count"] == 1
    assert conc["lots"]["top"][0]["pnl"] == 120


def test_concentration_shares_report_both_denominators():
    """占比同时给毛利与净利两个分母：净利受亏损抵扣，占比可 >100%。"""
    trades = [
        {"stock_code": "W.SZ", "trade_type": "BUY", "amount": 1000, "commission": 0,
         "stamp_duty": 0, "price": 10, "quantity": 100},
        {"stock_code": "W.SZ", "trade_type": "SELL", "amount": 1100, "commission": 0,
         "stamp_duty": 0, "price": 11, "quantity": 100},
        {"stock_code": "L.SZ", "trade_type": "BUY", "amount": 1000, "commission": 0,
         "stamp_duty": 0, "price": 10, "quantity": 100},
        {"stock_code": "L.SZ", "trade_type": "SELL", "amount": 950, "commission": 0,
         "stamp_duty": 0, "price": 9.5, "quantity": 100},
    ]
    snaps = _curve([(date(2024, 1, 2), 100), (date(2024, 12, 31), 200)])
    conc = analyze_robustness(snaps, trades, oos_ratio=0.5)["concentration"]
    assert conc["gross_profit"] == 100 and conc["gross_loss"] == 50
    assert conc["total_realized_pnl"] == 50
    top = {t["code"]: t for t in conc["top"]}
    assert top["W.SZ"]["share_gross"] == 1.0        # 100/100
    assert top["W.SZ"]["share_net"] == 2.0          # 100/50
    assert conc["top20_share"] == 1.0               # 50/50
    assert conc["top20_share_gross"] == 1.0         # 50/100


def test_concentration_lots_hhi_and_cumulative():
    """单笔贡献分布：4 笔等额盈利 → HHI=0.25、等效来源 4 笔；累计占比单调递增。"""
    trades = []
    for i, code in enumerate(["A.SZ", "B.SZ", "C.SZ", "D.SZ"]):
        trades += [
            {"stock_code": code, "trade_type": "BUY", "amount": 1000, "commission": 0,
             "stamp_duty": 0, "price": 10, "quantity": 100,
             "bar_time": datetime(2024, 1, 2)},
            {"stock_code": code, "trade_type": "SELL", "amount": 1025, "commission": 0,
             "stamp_duty": 0, "price": 10.25, "quantity": 100,
             "bar_time": datetime(2024, 6, 3)},
        ]
    snaps = _curve([(date(2024, 1, 2), 100), (date(2024, 12, 31), 200)])
    lots = analyze_robustness(snaps, trades, oos_ratio=0.5)["concentration"]["lots"]
    assert lots["count"] == 4 and lots["winners"] == 4
    assert lots["hhi"] == 0.25
    assert lots["effective_sources"] == 4.0
    cum = lots["cumulative"]
    assert cum[0]["n"] == 1 and abs(cum[0]["share_gross"] - 0.25) < 1e-9
    assert cum[-1]["share_gross"] == 1.0
    assert lots["top"][0]["share_gross"] == 0.25
    assert lots["top"][0]["buy_date"] == "2024-01-02"
    assert lots["top"][0]["sell_date"] == "2024-06-03"


def test_benchmark_return_tracked():
    snaps = [
        {"snap_date": date(2023, 1, 2), "total_value": 100, "benchmark_value": 200},
        {"snap_date": date(2023, 12, 31), "total_value": 110, "benchmark_value": 210},
        {"snap_date": date(2024, 1, 2), "total_value": 110, "benchmark_value": 210},
        {"snap_date": date(2024, 12, 31), "total_value": 121, "benchmark_value": 220.5},
    ]
    r = analyze_robustness(snaps, [], oos_ratio=0.5)
    assert round(r["benchmark_return"], 4) == 0.1025  # 200 -> 220.5
    assert round(r["oos"]["in_sample"]["benchmark_return"], 4) == 0.05  # 200 -> 210
    assert round(r["oos"]["out_of_sample"]["benchmark_return"], 4) == 0.05  # 210 -> 220.5


def test_insufficient_snapshots_returns_minimal():
    r = analyze_robustness([], [], oos_ratio=0.25)
    assert r["annual"] == []
    assert r["rolling_3y"] == []
    assert r["oos"] is None
