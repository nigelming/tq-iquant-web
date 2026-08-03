"""build_klines / build_signal_cache / build_open_prices 数据对接层测试。

这些函数桥接 TQ 原始格式与回测引擎内部格式。TQ 调用本身依赖通达信进程，
不可单测；故将逻辑拆为纯转换函数（_convert_market_data / _convert_formula_output）
+ 编排函数（build_*，monkeypatch TQ 调用）。纯函数用合成输入直接单测。
"""
from datetime import datetime, date
from decimal import Decimal

import polars as pl
import pytest

import core.api.backtest as bt_api


# ---------------------------------------------------------------------------
# _convert_market_data：TQ 原始行情 → 引擎 klines
# TQ 原始格式：dict[str, pandas.DataFrame]，键 "Open"/"High"/"Low"/"Close"/"Volume"/"Amount"，
#   index=时间戳，columns=股票代码（一次调用 = 多股票单周期）。
# 引擎格式：{stock_code: {period: pl.DataFrame}}，列 datetime + Open/High/Low/Close/Volume/Amount。
# ---------------------------------------------------------------------------
def _tq_raw_market_data():
    """构造 TQ 原始日线行情（多股票单周期），用 polars 模拟 pandas DataFrame。
    TQ 返回的是 pandas，但转换函数只依赖 .index / [col].loc[t, code] 接口，
    polars 不支持该接口，故测试用真实 pandas 构造。"""
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31)]
    codes = ["000001.SZ", "600519.SH"]
    return {
        "Open": pd.DataFrame(
            {"000001.SZ": [10.0, 10.2, 9.0], "600519.SH": [1600.0, 1610.0, 1595.0]},
            index=ts,
        ),
        "High": pd.DataFrame(
            {"000001.SZ": [10.3, 10.5, 9.2], "600519.SH": [1610.0, 1620.0, 1605.0]},
            index=ts,
        ),
        "Low": pd.DataFrame(
            {"000001.SZ": [9.9, 8.9, 8.8], "600519.SH": [1590.0, 1600.0, 1590.0]},
            index=ts,
        ),
        "Close": pd.DataFrame(
            {"000001.SZ": [10.2, 9.0, 9.1], "600519.SH": [1605.0, 1615.0, 1600.0]},
            index=ts,
        ),
        "Volume": pd.DataFrame(
            {"000001.SZ": [1000, 1000, 1000], "600519.SH": [200, 210, 180]},
            index=ts,
        ),
        "Amount": pd.DataFrame(
            {"000001.SZ": [10200.0, 9000.0, 9100.0], "600519.SH": [321000.0, 339150.0, 288000.0]},
            index=ts,
        ),
    }


def test_convert_market_data_basic():
    """单周期多股票转换：得到 {stock: {period: polars}}，列齐全，时间升序。"""
    raw = _tq_raw_market_data()
    klines = bt_api._convert_market_data(raw, ["000001.SZ", "600519.SH"], ["1d"])

    assert set(klines.keys()) == {"000001.SZ", "600519.SH"}
    for code in klines:
        assert "1d" in klines[code]
        df = klines[code]["1d"]
        assert isinstance(df, pl.DataFrame)
        # 列齐全（datetime + 6 行情列）
        for col in ("datetime", "Open", "High", "Low", "Close", "Volume", "Amount"):
            assert col in df.columns
        assert df.height == 3
        # 时间升序
        times = df["datetime"].to_list()
        assert times == sorted(times)


def test_convert_market_data_decimal_preserved():
    """价格列应保留为 Decimal（引擎依赖 Decimal 做金额计算，float 会引入误差）。"""
    raw = _tq_raw_market_data()
    klines = bt_api._convert_market_data(raw, ["000001.SZ"], ["1d"])
    df = klines["000001.SZ"]["1d"]
    # 第一行 open 应为 Decimal("10")，而非 float 10.0
    assert isinstance(df["Open"][0], Decimal)
    assert df["Open"][0] == Decimal("10")
    assert df["Close"][2] == Decimal("9.1")


def test_convert_market_data_missing_stock_skipped():
    """请求的股票在 TQ 返回中缺失 → 该股票不出现在结果中（不报错）。"""
    raw = _tq_raw_market_data()
    # 请求一只不存在的股票
    klines = bt_api._convert_market_data(raw, ["000001.SZ", "999999.XX"], ["1d"])
    assert "000001.SZ" in klines
    assert "999999.XX" not in klines


def test_convert_market_data_volume_as_string():
    """真机 bug：TQ 返回的 Volume 可能是 str 而非 int（如 "1000"），
    polars 构造 DataFrame 时会 panic 'str object cannot be interpreted as integer'。
    转换层应将 Volume/Amount 数值列统一规整为可数值类型，不依赖调用方传干净数据。"""
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 10.2]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, 10.5]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 8.9]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, 9.0]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": ["1000", "1500"]}, index=ts),  # 字符串！
        "Amount": pd.DataFrame({"000001.SZ": ["10200", "9000"]}, index=ts),  # 字符串！
    }
    # 不应抛异常
    klines = bt_api._convert_market_data(raw, ["000001.SZ"], ["1d"])
    df = klines["000001.SZ"]["1d"]
    assert df.height == 2
    # Volume 列可被引擎当数值用（持仓快照市值计算虽不直接读 Volume，但下游不能是 str）
    vols = df["Volume"].to_list()
    assert vols[0] == 1000 and isinstance(vols[0], (int, float, Decimal))


def test_convert_market_data_nan_values():
    """真机 bug：停牌/无交易的日子 TQ 返回 NaN（float）。
    _to_int(NaN) 直接 int(NaN) 抛 ValueError；_to_decimal(NaN) 产 Decimal('nan') 毒化计算。
    NaN 应规整为 0（无成交量）/ 价列也应容忍 NaN 不报错。"""
    import math
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, float("nan")]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, float("nan")]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, float("nan")]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, float("nan")]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000, float("nan")]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0, float("nan")]}, index=ts),
    }
    # 不应抛异常
    klines = bt_api._convert_market_data(raw, ["000001.SZ"], ["1d"])
    df = klines["000001.SZ"]["1d"]
    assert df.height == 2
    # NaN Volume 规整为 0（非 NaN）
    vols = df["Volume"].to_list()
    assert vols[1] == 0, f"NaN Volume 应为 0，实得 {vols[1]!r}"
    # 价列 NaN 不毒化（Decimal nan 会破坏下游金额计算）— 规整为 0
    opens = df["Open"].to_list()
    assert not (isinstance(opens[1], Decimal) and opens[1].is_nan()), \
        f"NaN 价应规整为 0，实得 {opens[1]!r}"


def test_convert_market_data_multi_period():
    """多周期：每个 period 独立一份 DataFrame（TQ 一次调用 = 单周期，故 raw 按 period 分组）。"""
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw_1d = _tq_raw_market_data()
    # 构造一个 5m 周期的 raw（TQ 实际返回完整 6 字段，这里同步构造）
    raw_5m = {
        "Open": pd.DataFrame({"000001.SZ": [10.5, 9.5]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.6, 9.6]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [10.4, 9.4]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.5, 9.5]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [500, 600]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [5250.0, 5700.0]}, index=ts),
    }
    # 多周期 raw 结构：{period: raw_dict}
    raw_by_period = {"1d": raw_1d, "5m": raw_5m}
    klines = bt_api._convert_market_data_multi(raw_by_period, ["000001.SZ"])

    assert "1d" in klines["000001.SZ"]
    assert "5m" in klines["000001.SZ"]
    assert klines["000001.SZ"]["1d"].height == 3
    assert klines["000001.SZ"]["5m"].height == 2


# ---------------------------------------------------------------------------
# build_open_prices：klines → {stock: {bar_time: Decimal open}}
# ---------------------------------------------------------------------------
def test_build_open_prices_from_klines():
    """open 价表直接从 klines 的 Open 列提取，key 为 datetime。"""
    df = pl.DataFrame({
        "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31)],
        "Open": [Decimal("10"), Decimal("10.2"), Decimal("9.0")],
        "High": [Decimal("10.3"), Decimal("10.5"), Decimal("9.2")],
        "Low": [Decimal("9.9"), Decimal("8.9"), Decimal("8.8")],
        "Close": [Decimal("10.2"), Decimal("9.0"), Decimal("9.1")],
        "Volume": [1000, 1000, 1000],
    })
    klines = {"000001.SZ": {"1d": df}}
    prices = bt_api.build_open_prices(None, klines)

    assert "000001.SZ" in prices
    stock_prices = prices["000001.SZ"]
    assert stock_prices[datetime(2026, 7, 29)] == Decimal("10")
    assert stock_prices[datetime(2026, 7, 30)] == Decimal("10.2")
    assert stock_prices[datetime(2026, 7, 31)] == Decimal("9.0")
    # 值为 Decimal
    for v in stock_prices.values():
        assert isinstance(v, Decimal)


def test_build_open_prices_empty_klines():
    """空 klines → 空 open_prices。"""
    assert bt_api.build_open_prices(None, {}) == {}


# ---------------------------------------------------------------------------
# _convert_formula_output：TQ 公式输出 → signal_cache 条目
# TQ 公式输出（formula_process_mul_zb）：{stock_code: {var_name: [{"Date":"YYYYMMDD","Value":float}, ...]}}
#   顶层可能有 "ErrorId"。日期串 "YYYYMMDD" 需转 datetime 对齐 klines 时间轴。
# signal_cache value: [{name: str, value: int}]（trigger_value 是 int 比较）
# ---------------------------------------------------------------------------
def test_convert_formula_output_date_format():
    """日期格式输出：{code: {var: [{Date, Value}, ...]}} → 按 datetime 索引的 cache 条目。"""
    raw = {
        "ErrorId": "0",
        "000001.SZ": {
            "open_sig": [
                {"Date": "20260729", "Value": 1},
                {"Date": "20260730", "Value": -1},
                {"Date": "20260731", "Value": 1},
            ],
        },
    }
    entries = bt_api._convert_formula_output(raw, 1, ["000001.SZ"])

    # 条目：{(strategy_id, stock_code, bar_time): [{"name", "value"}]}
    assert (1, "000001.SZ", datetime(2026, 7, 29)) in entries
    assert (1, "000001.SZ", datetime(2026, 7, 30)) in entries
    assert (1, "000001.SZ", datetime(2026, 7, 31)) in entries
    assert entries[(1, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "open_sig", "value": 1}]
    assert entries[(1, "000001.SZ", datetime(2026, 7, 31))] == [{"name": "open_sig", "value": 1}]


def test_convert_formula_output_multi_var():
    """多输出变量：同股票同 bar 多变量合并到一个 list。"""
    raw = {
        "000001.SZ": {
            "open_sig": [{"Date": "20260729", "Value": 1}],
            "close_sig": [{"Date": "20260729", "Value": 1}],
        },
    }
    entries = bt_api._convert_formula_output(raw, 1, ["000001.SZ"])
    val = entries[(1, "000001.SZ", datetime(2026, 7, 29))]
    assert {"name": "open_sig", "value": 1} in val
    assert {"name": "close_sig", "value": 1} in val
    assert len(val) == 2


def test_convert_formula_output_skips_metadata_keys():
    """跳过 Date/ErrorId/Time 等非变量键。"""
    raw = {
        "ErrorId": "0",
        "000001.SZ": {
            "Date": [{"Date": "20260729", "Value": "20260729"}],
            "open_sig": [{"Date": "20260729", "Value": 1}],
        },
    }
    entries = bt_api._convert_formula_output(raw, 1, ["000001.SZ"])
    # 只应有 open_sig，不含 Date
    val = entries[(1, "000001.SZ", datetime(2026, 7, 29))]
    assert [o["name"] for o in val] == ["open_sig"]


def test_convert_formula_output_empty_raw():
    """空输出 → 空条目。"""
    assert bt_api._convert_formula_output(None, 1, ["000001.SZ"]) == {}
    assert bt_api._convert_formula_output({}, 1, ["000001.SZ"]) == {}


def test_convert_formula_output_error_id_nonzero():
    """ErrorId 非 0/19 → 视为公式出错，返回空（不抛异常）。"""
    raw = {"ErrorId": "99", "Error": "formula not found", "000001.SZ": {}}
    assert bt_api._convert_formula_output(raw, 1, ["000001.SZ"]) == {}


# ---------------------------------------------------------------------------
# 分钟级对齐（5m/15m/30m/60m）：TQ 公式输出 Date 只标到日（丢时分），
# 但输出条目按 bar 顺序排列。传 bar_times 时间轴时，按索引对齐：
# 第 i 条输出 → bar_times[i] 的 datetime。日线不传 bar_times，走 Date 匹配。
# ---------------------------------------------------------------------------
def test_convert_formula_output_minute_aligns_by_index():
    """5m：TQ 输出 96 条（Date 只有 2 个唯一日），传 96 根 bar 的 bar_times，
    第 i 条输出对齐到 bar_times[i]。每根 bar 用自己计算出的信号值。"""
    # 模拟真机：2 天 × 4 根 5m bar = 8 条输出，但 Date 只有 2 个唯一日
    bar_times = [
        datetime(2026, 7, 28, 9, 35), datetime(2026, 7, 28, 9, 40),
        datetime(2026, 7, 28, 9, 45), datetime(2026, 7, 28, 9, 50),
        datetime(2026, 7, 29, 9, 35), datetime(2026, 7, 29, 9, 40),
        datetime(2026, 7, 29, 9, 45), datetime(2026, 7, 29, 9, 50),
    ]
    raw = {
        "ErrorId": "0",
        "000001.SZ": {
            # 8 条输出，Date 全是日粒度（重复），但 Value 逐条变（模拟逐 bar 计算）
            "open_sig": [
                {"Date": "20260728", "Value": 1},   # bar0 09:35
                {"Date": "20260728", "Value": -1},  # bar1 09:40
                {"Date": "20260728", "Value": -1},  # bar2 09:45
                {"Date": "20260728", "Value": 1},   # bar3 09:50
                {"Date": "20260729", "Value": -1},  # bar4 09:35
                {"Date": "20260729", "Value": 1},   # bar5 09:40
                {"Date": "20260729", "Value": -1},  # bar6 09:45
                {"Date": "20260729", "Value": -1},  # bar7 09:50
            ],
        },
    }
    bar_times_by_code = {"000001.SZ": bar_times}
    entries = bt_api._convert_formula_output(
        raw, 1, ["000001.SZ"], bar_times_by_code=bar_times_by_code
    )

    # 每根 bar 对齐到自己的信号值（不是全用末值，不是全用首值）
    assert entries[(1, "000001.SZ", datetime(2026, 7, 28, 9, 35))] == [{"name": "open_sig", "value": 1}]
    assert entries[(1, "000001.SZ", datetime(2026, 7, 28, 9, 40))] == [{"name": "open_sig", "value": -1}]
    assert entries[(1, "000001.SZ", datetime(2026, 7, 28, 9, 50))] == [{"name": "open_sig", "value": 1}]
    assert entries[(1, "000001.SZ", datetime(2026, 7, 29, 9, 40))] == [{"name": "open_sig", "value": 1}]
    # 8 根 bar 全覆盖
    assert len(entries) == 8


def test_convert_formula_output_minute_multi_var_merges_per_bar():
    """分钟级多变量：同股票同 bar 的多变量合并到同一 bar_time 的 list。"""
    bar_times = [datetime(2026, 7, 28, 9, 35), datetime(2026, 7, 28, 9, 40)]
    raw = {
        "000001.SZ": {
            "open_sig": [{"Date": "20260728", "Value": 1}, {"Date": "20260728", "Value": -1}],
            "close_sig": [{"Date": "20260728", "Value": -1}, {"Date": "20260728", "Value": 1}],
        },
    }
    entries = bt_api._convert_formula_output(
        raw, 1, ["000001.SZ"], bar_times_by_code={"000001.SZ": bar_times}
    )
    val0 = entries[(1, "000001.SZ", datetime(2026, 7, 28, 9, 35))]
    assert {"name": "open_sig", "value": 1} in val0
    assert {"name": "close_sig", "value": -1} in val0
    val1 = entries[(1, "000001.SZ", datetime(2026, 7, 28, 9, 40))]
    assert {"name": "open_sig", "value": -1} in val1
    assert {"name": "close_sig", "value": 1} in val1


def test_convert_formula_output_minute_count_mismatch_clamps():
    """输出条数 < bar 数（TQ 少算了几根）→ 多余 bar 无信号（不报错，不越界）。
    输出条数 > bar 数 → 多余输出丢弃（不越界）。"""
    bar_times = [datetime(2026, 7, 28, 9, 35), datetime(2026, 7, 28, 9, 40),
                 datetime(2026, 7, 28, 9, 45)]
    # 只有 2 条输出，但有 3 根 bar
    raw = {
        "000001.SZ": {
            "open_sig": [{"Date": "20260728", "Value": 1}, {"Date": "20260728", "Value": -1}],
        },
    }
    entries = bt_api._convert_formula_output(
        raw, 1, ["000001.SZ"], bar_times_by_code={"000001.SZ": bar_times}
    )
    # 前 2 根 bar 对齐，第 3 根无信号
    assert (1, "000001.SZ", datetime(2026, 7, 28, 9, 35)) in entries
    assert (1, "000001.SZ", datetime(2026, 7, 28, 9, 40)) in entries
    assert (1, "000001.SZ", datetime(2026, 7, 28, 9, 45)) not in entries


def test_convert_formula_output_daily_without_bar_times_unchanged():
    """日线不传 bar_times → 走原 Date 匹配（向后兼容，现有日线测试不破）。"""
    raw = {
        "ErrorId": "0",
        "000001.SZ": {
            "open_sig": [
                {"Date": "20260729", "Value": 1},
                {"Date": "20260730", "Value": -1},
            ],
        },
    }
    entries = bt_api._convert_formula_output(raw, 1, ["000001.SZ"])
    assert entries[(1, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "open_sig", "value": 1}]
    assert entries[(1, "000001.SZ", datetime(2026, 7, 30))] == [{"name": "open_sig", "value": -1}]


# ---------------------------------------------------------------------------
# 编排层：build_klines / build_signal_cache 的 DB 读取 + TQ 调用接线
# monkeypatch TQ 调用，用真实内存 DB 验证：股票池读取、周期去重、公式名读取
# ---------------------------------------------------------------------------
def _seed_db(db):
    """建依赖链：StockPool + 股票 → Formula + FormulaSignal → PortfolioStrategy + Strategy。"""
    from decimal import Decimal
    from core.models import (
        StockPool, StockPoolStock, Formula, FormulaSignal,
        PortfolioStrategy, Strategy,
    )
    pool = StockPool(code="TEST", name="test_pool")
    db.add(pool); db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    db.add(StockPoolStock(pool_id=pool.id, stock_code="600519.SH"))
    db.flush()
    formula = Formula(name="OPEN_FORMULA", content="REF(CLOSE,1)")
    db.add(formula); db.flush()
    db.add(FormulaSignal(
        formula_id=formula.id, signal_name="open_sig",
        signal_type="OPEN", trigger_value=1,
    ))
    db.flush()
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool.id,
        initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps); db.flush()
    strat = Strategy(
        portfolio_id=ps.id, name="s1", formula_id=formula.id,
        period="1d", role="master",
        capital_ratio=Decimal("0.6"), max_positions=5,
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    db.add(strat); db.commit()
    return ps, strat, formula


def test_build_klines_orchestration(db_session, monkeypatch):
    """build_klines 读股票池 + 策略周期，调 get_history_raw，转 polars。"""
    import pandas as pd
    db = db_session
    ps, strat, formula = _seed_db(db)

    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 10.2], "600519.SH": [1600.0, 1610.0]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, 10.5], "600519.SH": [1610.0, 1620.0]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 8.9], "600519.SH": [1590.0, 1600.0]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, 9.0], "600519.SH": [1605.0, 1615.0]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000, 1000], "600519.SH": [200, 210]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0, 9000.0], "600519.SH": [321000.0, 339150.0]}, index=ts),
    }
    captured = {}
    def fake_get_history_raw(self, stocks, periods, start, end, dividend_type, count):
        captured["stocks"] = stocks
        captured["periods"] = periods
        captured["start"] = start
        captured["end"] = end
        return {"1d": raw}
    monkeypatch.setattr(bt_api.TQData, "get_history_raw", fake_get_history_raw)

    klines = bt_api.build_klines(ps, date(2026, 7, 29), date(2026, 7, 31), db)

    # 接线校验：股票池 2 只股票、周期 1d、日期 YYYYMMDD
    assert captured["stocks"] == ["000001.SZ", "600519.SH"]
    assert captured["periods"] == ["1d"]
    assert captured["start"] == "20260729"
    assert captured["end"] == "20260731"
    # 输出校验：两股票 polars DataFrame 含 datetime
    assert set(klines.keys()) == {"000001.SZ", "600519.SH"}
    assert "datetime" in klines["000001.SZ"]["1d"].columns
    assert klines["000001.SZ"]["1d"].height == 2


def test_build_signal_cache_orchestration(db_session, monkeypatch):
    """build_signal_cache 读 Strategy+Formula，调 compute，转 cache。"""
    db = db_session
    ps, strat, formula = _seed_db(db)
    klines = {"000001.SZ": {"1d": pl.DataFrame({
        "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30)],
        "Open": [Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.9"), Decimal("8.9")],
        "Close": [Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000],
    })}}

    captured = {}
    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                      start_time="", end_time="", return_count=-1, return_date=True):
        captured["formula_name"] = formula_name
        captured["stocks"] = stocks
        captured["period"] = period
        captured["start_time"] = start_time
        captured["end_time"] = end_time
        captured["return_date"] = return_date
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260729", "Value": 1},
                    {"Date": "20260730", "Value": -1},
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db)

    # 接线校验：读到了 Formula.name=OPEN_FORMULA，period=1d，时间范围从 klines 提取
    assert captured["formula_name"] == "OPEN_FORMULA"
    assert captured["period"] == "1d"
    assert captured["stocks"] == ["000001.SZ"]
    assert captured["start_time"] == "20260729"
    assert captured["end_time"] == "20260730"
    assert captured["return_date"] is True
    # cache key = (strategy_id, stock, bar_time)，strategy_id = strat.id
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29)) in cache
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "open_sig", "value": 1}]


def test_build_signal_cache_minute_orchestration(db_session, monkeypatch):
    """5m 编排：TQ 输出 Date 只标到日但逐条计算，build_signal_cache 按 bar_times 索引对齐。
    cache key 应为带时分的 5m bar_time，而非午夜 datetime（否则引擎逐 bar 查不到信号）。"""
    from core.models import Strategy
    db = db_session
    ps, strat, formula = _seed_db(db)
    # 改策略周期为 5m
    db.query(Strategy).filter_by(id=strat.id).update({"period": "5m"})
    db.commit()

    # 2 根 5m bar：09:35 / 09:40
    klines = {"000001.SZ": {"5m": pl.DataFrame({
        "datetime": [datetime(2026, 7, 29, 9, 35), datetime(2026, 7, 29, 9, 40)],
        "Open": [Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.9"), Decimal("8.9")],
        "Close": [Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000],
    })}}

    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        # TQ 真机：2 条输出，Date 都是 20260729（日粒度），但 Value 逐条变
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260729", "Value": 1},   # → bar 09:35
                    {"Date": "20260729", "Value": -1},  # → bar 09:40
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db)

    # 关键：cache key 是带时分的 5m bar_time，不是午夜 datetime
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40)) in cache
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35))] == [{"name": "open_sig", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40))] == [{"name": "open_sig", "value": -1}]
    # 午夜 key 不应存在（那是对齐失败的旧表现）
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 0, 0)) not in cache

