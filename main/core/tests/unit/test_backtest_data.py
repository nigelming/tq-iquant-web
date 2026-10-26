"""build_klines / build_signal_cache 数据对接层测试。

这些函数桥接 TQ 原始格式与回测引擎内部格式。TQ 调用本身依赖通达信进程，
不可单测；故将逻辑拆为纯转换函数（_convert_market_data / _convert_formula_output）
+ 编排函数（build_*，monkeypatch TQ 调用）。纯函数用合成输入直接单测。
"""
from datetime import datetime, date
from decimal import Decimal

import polars as pl
import pytest

import core.api.backtest as bt_api
import core.services.backtest_service as svc


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
    NaN Volume 规整为 0（无成交量）；价列 NaN → Open/Close=0 → 整 bar 作为伪影丢弃（不进引擎）。"""
    import math
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 10.1, float("nan")]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, 10.4, float("nan")]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 10.0, float("nan")]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, 10.3, float("nan")]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000, float("nan"), float("nan")]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0, 10403.0, float("nan")]}, index=ts),
    }
    # 不应抛异常
    klines = bt_api._convert_market_data(raw, ["000001.SZ"], ["1d"])
    df = klines["000001.SZ"]["1d"]
    # 全 NaN bar（Open/Close=0）被丢弃；价合法但 Volume NaN 的 bar 保留
    assert df.height == 2
    # NaN Volume 规整为 0（非 NaN）
    vols = df["Volume"].to_list()
    assert vols[1] == 0, f"NaN Volume 应为 0，实得 {vols[1]!r}"
    # 保留 bar 的价列不毒化（Decimal nan 会破坏下游金额计算）
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
    """ErrorId 非 0 → 视为公式出错，返回空（不抛异常）。"""
    raw = {"ErrorId": "99", "Error": "formula not found", "000001.SZ": {}}
    assert bt_api._convert_formula_output(raw, 1, ["000001.SZ"]) == {}


def test_convert_formula_output_error_id_19_rejected():
    """ErrorId=19 表示数据过多只返回了部分股票，不能静默使用部分结果。"""
    raw = {
        "ErrorId": "19", "Error": "数据过多无法完全加载",
        "000001.SZ": {"open_sig": [{"Date": "20260729", "Value": 1}]},
    }
    assert bt_api._convert_formula_output(raw, 1, ["000001.SZ"]) == {}


def test_formula_diagnostics_error_id_19_is_error():
    """ErrorId=19 必须在信号预检里报错，而不是当作正常输出。"""
    err, lines = svc._formula_diagnostics(
        {"ErrorId": "19", "Error": "数据过多无法完全加载"}, ["000001.SZ"],
    )
    assert err is not None
    assert "19" in err
    assert lines == set()


# ---------------------------------------------------------------------------
# 索引对齐（1m/5m/15m/30m/1h）：TQ 公式输出 Date 只标到日（丢时分），
# 但输出条目按 bar 顺序排列。传 bar_times 时间轴时，按索引对齐：
# 第 i 条输出 → bar_times[i] 的 datetime。1d 不传 bar_times，走 Date 匹配。
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
    """build_klines 读股票池 + 策略周期，调 get_history_raw（主段 + 预热段），转 polars。"""
    import pandas as pd
    db = db_session
    ps, strat, formula = _seed_db(db)  # formula_count 用模型默认 200

    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 10.2], "600519.SH": [1600.0, 1610.0]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, 10.5], "600519.SH": [1610.0, 1620.0]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 8.9], "600519.SH": [1590.0, 1600.0]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, 9.0], "600519.SH": [1605.0, 1615.0]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000, 1000], "600519.SH": [200, 210]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0, 9000.0], "600519.SH": [321000.0, 339150.0]}, index=ts),
    }
    calls = []
    def fake_get_history_raw(self, stocks, periods, start, end, dividend_type, count):
        calls.append({
            "stocks": list(stocks), "periods": list(periods),
            "start": start, "end": end, "count": count,
        })
        return {"1d": raw}
    monkeypatch.setattr(bt_api.TQData, "get_history_raw", fake_get_history_raw)

    klines = bt_api.build_klines(ps, date(2026, 7, 29), date(2026, 7, 31), db)

    # W-A：单次拉取（主段 start 前推覆盖公式预热窗口），不再单独预热段
    assert len(calls) == 1
    assert calls[0]["stocks"] == ["000001.SZ", "600519.SH"]
    assert calls[0]["periods"] == ["1d"]
    # formula_count=200（1d）→ 前推 430 天：2026-07-29 - 430 = 2025-05-25
    assert calls[0]["start"] == "20250525"
    assert calls[0]["end"] == "20260731"
    assert calls[0]["count"] == -1
    # 输出校验：单次拉取同数据 → 仍 2 根 bar（预热 bar 保留在 klines，引擎由调用方切片）
    assert set(klines.keys()) == {"000001.SZ", "600519.SH"}
    assert "datetime" in klines["000001.SZ"]["1d"].columns
    assert klines["000001.SZ"]["1d"].height == 2


def test_convert_market_data_drops_nonpositive_prices():
    """负价/零价/近零价（<0.01）行属前复权伪影，应被过滤，不进引擎。"""
    import pandas as pd
    ts = [datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 9.0, 8.0]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3, 9.5, 8.5]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 8.8, 7.9]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2, -1.0, 0.005]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000, 1000, 1000]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0, 9000.0, 8000.0]}, index=ts),
    }
    out = bt_api._convert_market_data(raw, ["000001.SZ"], ["1d"])
    df = out["000001.SZ"]["1d"]
    assert df.height == 1
    assert df["datetime"].to_list() == [ts[0]]


def test_build_klines_passes_dividend_mode_through(db_session, monkeypatch):
    """dividend_mode='back' → get_history_raw 主段/预热段均传 dividend_type='back'。"""
    import pandas as pd
    db = db_session
    ps, strat, formula = _seed_db(db)
    ts = [datetime(2026, 7, 29)]
    raw = {
        "Open": pd.DataFrame({"000001.SZ": [10.0]}, index=ts),
        "High": pd.DataFrame({"000001.SZ": [10.3]}, index=ts),
        "Low": pd.DataFrame({"000001.SZ": [9.9]}, index=ts),
        "Close": pd.DataFrame({"000001.SZ": [10.2]}, index=ts),
        "Volume": pd.DataFrame({"000001.SZ": [1000]}, index=ts),
        "Amount": pd.DataFrame({"000001.SZ": [10200.0]}, index=ts),
    }
    calls = []
    def fake_get_history_raw(self, stocks, periods, start, end, dividend_type, count):
        calls.append(dividend_type)
        return {"1d": raw}
    monkeypatch.setattr(bt_api.TQData, "get_history_raw", fake_get_history_raw)
    bt_api.build_klines(ps, date(2026, 7, 29), date(2026, 7, 31), db, dividend_mode="back")
    # W-A：单次拉取，dividend_type 透传
    assert calls == ["back"]


def test_build_signal_cache_back_dividend_passes_2(db_session, monkeypatch):
    """dividend_mode='back' → TQFormula.compute dividend_type=2（后复权）。"""
    db = db_session
    ps, strat, formula = _seed_db(db)
    klines = {"000001.SZ": {"1d": pl.DataFrame({
        "datetime": [datetime(2026, 7, 29)],
        "Open": [Decimal("10")], "High": [Decimal("10.3")],
        "Low": [Decimal("9.9")], "Close": [Decimal("10.2")],
        "Volume": [1000],
    })}}
    captured = {}
    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                      start_time="", end_time="", return_count=-1, return_date=True):
        captured["dividend_type"] = dividend_type
        return {}
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)
    bt_api.build_signal_cache(ps, klines, db, dividend_mode="back")
    assert captured["dividend_type"] == 2


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


# ---------------------------------------------------------------------------
# _MINUTE_PERIODS：索引对齐判定集（open-questions Q4）
# TQ 公式输出 Date 只标到日（丢时分），bar 时间带时分的周期无法按 Date 1:1 匹配，
# 须按 bar_times 索引对齐。含 1m/5m/15m/30m/1h（1h bar 带时分）；
# 1d bar 是日粒度，走 Date 匹配（1:1）。60m 已移除（两端都不认）。
# ---------------------------------------------------------------------------
def test_minute_periods_membership():
    """_MINUTE_PERIODS 恰好是 5 个「带时分 bar」周期，含 1h，不含 60m/1d。"""
    assert bt_api._MINUTE_PERIODS == {"1m", "5m", "15m", "30m", "1h"}
    assert "60m" not in bt_api._MINUTE_PERIODS
    assert "1d" not in bt_api._MINUTE_PERIODS
    assert "1w" not in bt_api._MINUTE_PERIODS
    assert "1mon" not in bt_api._MINUTE_PERIODS


def test_minute_periods_aligned_with_valid_periods():
    """_MINUTE_PERIODS 应是 VALID_PERIODS 的「带时分 bar」子集（两处白名单不可漂移）。"""
    from core.api.strategies import VALID_PERIODS
    assert bt_api._MINUTE_PERIODS.issubset(VALID_PERIODS)
    # VALID_PERIODS 的日粒度（1d/1w/1mon）不在 _MINUTE_PERIODS，走 Date 匹配
    non_minute = VALID_PERIODS - bt_api._MINUTE_PERIODS
    assert non_minute == {"1d", "1w", "1mon"}


def test_convert_formula_output_minute_alignment_phantom_drift():
    """午休幽灵 bar 回归：批量取数 fill_data=True 注入 13:00 bar（9 根/日 vs 公式 8 根/日）。

    公式输出按索引对齐到含幽灵的时间轴会整体前移一根——15:00 触发的信号被钉到
    14:30（隐性未来函数，30m 回测换手/胜率异常的根因）。剔除幽灵后的标准 8 根/日
    时间轴才映射到正确 bar。此测试固定两端行为，防止数据层修复后回退。
    """
    raw = {"000001.SZ": {"buy": [{"Date": "20260107", "Value": 0} for _ in range(7)]
                         + [{"Date": "20260107", "Value": 1}]}}
    phantom_axis = [
        datetime(2026, 1, 7, 10, 0), datetime(2026, 1, 7, 10, 30),
        datetime(2026, 1, 7, 11, 0), datetime(2026, 1, 7, 11, 30),
        datetime(2026, 1, 7, 13, 0), datetime(2026, 1, 7, 13, 30),
        datetime(2026, 1, 7, 14, 0), datetime(2026, 1, 7, 14, 30),
        datetime(2026, 1, 7, 15, 0),
    ]
    # 现状（BUG）：输出第 8 条（15:00 信号）落到含幽灵轴的 index[7]=14:30
    entries = bt_api._convert_formula_output(raw, 12, ["000001.SZ"],
                                             {"000001.SZ": phantom_axis})
    assert (12, "000001.SZ", datetime(2026, 1, 7, 14, 30)) in entries

    # 修复后：数据层剔除 13:00，时间轴恢复 8 根/日 → 信号正确落在 15:00
    import core.tq.data as tq_data
    import pandas as pd
    phantom_raw = {f: pd.DataFrame({"000001.SZ": list(range(9))}, index=pd.to_datetime(phantom_axis))
                   for f in ("Open", "High", "Low", "Close", "Volume", "Amount")}
    stripped = tq_data.strip_lunch_phantom(phantom_raw, "30m")
    # 真机 klines 的 bar_times 来自 polars datetime 列 → Python datetime，
    # 与 pandas Timestamp 不相等（dict key 比对），故转 pydatetime 对齐
    clean_axis = [t.to_pydatetime() for t in stripped["Close"].index]
    assert len(clean_axis) == 8
    entries = bt_api._convert_formula_output(raw, 12, ["000001.SZ"],
                                             {"000001.SZ": clean_axis})
    # 信号线（Value=1）应在 15:00，绝不应提前到 14:30（其他条目的 0 值无信号意义）
    assert ("buy", 1) in [(e["name"], e["value"]) for e in entries.get(
        (12, "000001.SZ", datetime(2026, 1, 7, 15, 0)), [])]
    assert ("buy", 1) not in [(e["name"], e["value"]) for e in entries.get(
        (12, "000001.SZ", datetime(2026, 1, 7, 14, 30)), [])]


def test_convert_formula_output_minute_perday_new_listing_no_drift():
    """新股对齐回归：批量取数 fill_data=True 会给上市前也补 carry-forward bar，
    klines 轴比公式输出多一段「上市前假 bar」。全局索引对齐会把上市后的信号
    错贴到上市前（漂移 N 根=上市前 bar 数，实测 688808.SH 漂 784 根）。
    必须按 Date 逐日对齐：公式某日的第 j 条 → 该日 klines 第 j 根。
    """
    axis = [
        datetime(2026, 1, 5, 10, 0), datetime(2026, 1, 5, 10, 30),  # 上市前（假）
        datetime(2026, 1, 7, 10, 0), datetime(2026, 1, 7, 10, 30),  # 上市后（真）
    ]
    raw = {"000001.SZ": {"buy": [
        {"Date": "20260107", "Value": 0},
        {"Date": "20260107", "Value": 1},   # 上市后 10:30 出信号
    ]}}
    entries = bt_api._convert_formula_output(raw, 12, ["000001.SZ"],
                                             {"000001.SZ": axis})
    # 信号必须落在上市后 2026-01-07 10:30
    assert (12, "000001.SZ", datetime(2026, 1, 7, 10, 30)) in entries
    # 全局索引会把 entry[1] 贴到 axis[1]=2026-01-05 10:30（漂移到上市前）→ 禁止
    assert (12, "000001.SZ", datetime(2026, 1, 5, 10, 30)) not in entries
    assert all(k[2].date() == datetime(2026, 1, 7).date() for k in entries)


def test_convert_formula_output_minute_perday_suspension_skip():
    """停牌日对齐回归：klines 停牌日有 carry-forward bar（fill_data），公式输出当天
    为 0 条。恢复日后信号必须落到正确的当天 bar，不被停牌日的多余 bar 前移，
    停牌日本身也不应产生任何 signal key。"""
    axis = [
        datetime(2026, 1, 5, 10, 0), datetime(2026, 1, 5, 10, 30),  # 正常日
        datetime(2026, 1, 6, 10, 0), datetime(2026, 1, 6, 10, 30),  # 停牌日（klines 有，公式无）
        datetime(2026, 1, 7, 10, 0), datetime(2026, 1, 7, 10, 30),  # 恢复日
    ]
    raw = {"000001.SZ": {"buy": [
        {"Date": "20260105", "Value": 0}, {"Date": "20260105", "Value": 0},
        {"Date": "20260107", "Value": 0}, {"Date": "20260107", "Value": 1},  # 恢复日 10:30 出信号
    ]}}
    entries = bt_api._convert_formula_output(raw, 12, ["000001.SZ"],
                                             {"000001.SZ": axis})
    assert (12, "000001.SZ", datetime(2026, 1, 7, 10, 30)) in entries
    # 全局索引会把恢复日 entry 贴到停牌日（01-06）→ 禁止
    assert (12, "000001.SZ", datetime(2026, 1, 6, 10, 30)) not in entries
    assert all(k[2].date() != datetime(2026, 1, 6).date() for k in entries)


def test_build_signal_cache_1h_uses_index_align(db_session, monkeypatch):
    """1h bar 时间带时分（10:00/11:00），TQ 输出 Date 只标到日，
    须按 bar_times 索引对齐（同 5m），否则 cache key 落午夜 → 引擎逐 bar 查不到信号。"""
    from core.models import Strategy
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Strategy).filter_by(id=strat.id).update({"period": "1h"})
    db.commit()

    klines = {"000001.SZ": {"1h": pl.DataFrame({
        "datetime": [datetime(2026, 7, 29, 10, 0), datetime(2026, 7, 29, 11, 0)],
        "Open": [Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.9"), Decimal("8.9")],
        "Close": [Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000],
    })}}

    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        # TQ 真机：2 条输出，Date 都是 20260729（日粒度），Value 逐条变
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260729", "Value": 1},   # → bar 10:00
                    {"Date": "20260729", "Value": -1},  # → bar 11:00
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db)

    # 关键：1h 走索引对齐，cache key 是带时分的 1h bar_time，不是午夜
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 10, 0)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 11, 0)) in cache
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 10, 0))] == [{"name": "open_sig", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 11, 0))] == [{"name": "open_sig", "value": -1}]
    # 午夜 key 不应存在（那是 Date 匹配的产物，1h 不该走那条）
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 0, 0)) not in cache


# ---------------------------------------------------------------------------
# 公式预热（回测优化）：回测区间首根 bar 上公式须有有效值。
# 预热根数 = 该周期所有策略引用公式的 formula_count 最大值（与实盘 code_period_count
# 同语义）；预热段用 TDX count 直拉（start 空 + end=start 前一天），只喂公式不进引擎。
# ---------------------------------------------------------------------------
def _seed_db_warmup(db):
    """预热测试依赖链：两个 1d 策略引用不同 formula_count 公式 + 30m 策略 + 5m 策略(count=0)。"""
    from decimal import Decimal
    from core.models import (
        StockPool, StockPoolStock, Formula, FormulaSignal,
        PortfolioStrategy, Strategy,
    )
    pool = StockPool(code="TESTW", name="warmup_pool")
    db.add(pool); db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    db.flush()
    f255 = Formula(name="F255", content="MA(CLOSE,255)", formula_count=255)
    f100 = Formula(name="F100", content="MA(CLOSE,100)", formula_count=100)
    f0 = Formula(name="F0", content="CLOSE", formula_count=0)
    db.add(f255); db.add(f100); db.add(f0); db.flush()
    ps = PortfolioStrategy(
        name="ps_warm", stock_pool_id=pool.id,
        initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps); db.flush()
    for name, period, fid in [
        ("s1", "1d", f255.id), ("s2", "1d", f100.id),
        ("s3", "30m", f255.id), ("s4", "5m", f0.id),
    ]:
        db.add(Strategy(
            portfolio_id=ps.id, name=name, formula_id=fid,
            period=period, role="master",
            capital_ratio=Decimal("0.6"), max_positions=5,
            stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
            trailing_stop_ratio=Decimal("0"),
        ))
    db.commit()
    return ps


def test_warmup_counts_by_period(db_session):
    """预热根数 = 该周期所有策略引用公式的 formula_count 最大值；count=0 不预热。"""
    db = db_session
    ps = _seed_db_warmup(db)
    counts = bt_api.warmup_counts_by_period(ps, db)
    assert counts == {"1d": 255, "30m": 255}


def test_warmup_counts_by_period_no_db(db_session):
    """db=None → 空 dict（build_klines 据此跳过预热拉取）。"""
    assert bt_api.warmup_counts_by_period(object(), None) == {}


def test_build_klines_warmup_merge(db_session, monkeypatch):
    """W-A：预热段并入主段——一次 get_history_raw(start 前推覆盖预热窗口) 取全量，
    klines 含预热 bar（引擎由调用方切片回主区间）。"""
    import pandas as pd
    db = db_session
    ps = _seed_db_warmup(db)

    def _raw(ts, values):
        return {
            "Open": pd.DataFrame({"000001.SZ": values}, index=ts),
            "High": pd.DataFrame({"000001.SZ": values}, index=ts),
            "Low": pd.DataFrame({"000001.SZ": values}, index=ts),
            "Close": pd.DataFrame({"000001.SZ": values}, index=ts),
            "Volume": pd.DataFrame({"000001.SZ": [1000] * len(values)}, index=ts),
            "Amount": pd.DataFrame({"000001.SZ": [10000.0] * len(values)}, index=ts),
        }

    ts_warm = [datetime(2026, 7, 28)]
    ts_main = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    raw = _raw(ts_warm + ts_main, [9.9, 10.2, 9.0])

    calls = []
    def fake_get_history_raw(self, stocks, periods, start, end, dividend_type, count):
        calls.append({"periods": list(periods), "start": start, "end": end, "count": count})
        # 单次拉取：返回 [前推 start, end] 全量（含预热 bar）
        return {p: raw for p in periods}
    monkeypatch.setattr(bt_api.TQData, "get_history_raw", fake_get_history_raw)

    klines = bt_api.build_klines(ps, date(2026, 7, 29), date(2026, 7, 31), db)

    # W-A：单次拉取（所有策略周期一次取全量），count=-1，start 前推早于回测开始日
    assert len(calls) == 1
    assert set(calls[0]["periods"]) == {"1d", "30m", "5m"}
    assert calls[0]["count"] == -1
    assert calls[0]["start"] < "20260729"
    assert calls[0]["end"] == "20260731"
    # 预热窗口 = max(formula_count)=255：1d 前推 545 天
    assert calls[0]["start"] == "20250130"

    # klines 含预热 bar：1d 有 3 根（07-28 预热 + 07-29/30 主段）
    df = klines["000001.SZ"]["1d"]
    assert df.height == 3
    assert df["datetime"].to_list()[0] == datetime(2026, 7, 28)


def test_merge_raw_by_period_keeps_main_on_overlap():
    """预热段 + 主段合并：按时间升序，重叠 bar 保留主段值。"""
    import pandas as pd
    ts_warm = [datetime(2026, 7, 28), datetime(2026, 7, 29)]
    ts_main = [datetime(2026, 7, 29), datetime(2026, 7, 30)]
    main = {"1d": {"Close": pd.DataFrame({"000001.SZ": [10.2, 9.0]}, index=ts_main)}}
    warm = {"1d": {"Close": pd.DataFrame({"000001.SZ": [9.9, 99.0]}, index=ts_warm)}}
    merged = bt_api._merge_raw_by_period(main, warm)
    df = merged["1d"]["Close"]
    assert list(df.index) == [datetime(2026, 7, 28), datetime(2026, 7, 29), datetime(2026, 7, 30)]
    assert df.loc[datetime(2026, 7, 29), "000001.SZ"] == 10.2  # 重叠 bar 主段值优先
    assert df.loc[datetime(2026, 7, 28), "000001.SZ"] == 9.9   # 预热 bar 保留


def test_merge_raw_by_period_fallbacks():
    """预热段非 dict / 缺周期 / 缺字段 → 退化为仅主段。"""
    import pandas as pd
    ts_main = [datetime(2026, 7, 29)]
    close = pd.DataFrame({"000001.SZ": [10.2]}, index=ts_main)
    main = {"1d": {"Close": close}}
    assert bt_api._merge_raw_by_period(main, None) is main
    assert bt_api._merge_raw_by_period(main, {})["1d"] is main["1d"]
    warm_missing_period = {"5m": {"Close": close}}
    assert bt_api._merge_raw_by_period(main, warm_missing_period)["1d"] is main["1d"]
    warm_missing_field = {"1d": {"Open": close}}
    merged = bt_api._merge_raw_by_period(main, warm_missing_field)
    assert merged["1d"]["Close"] is close


def test_slice_klines_from_start():
    """引擎视野切片：datetime >= start 保留；主区间无 bar 的股票整只剔除。"""
    df_keep = pl.DataFrame({
        "datetime": [datetime(2026, 7, 28), datetime(2026, 7, 29)],
        "Open": [Decimal("9.9"), Decimal("10.2")],
    })
    df_drop = pl.DataFrame({
        "datetime": [datetime(2026, 7, 27), datetime(2026, 7, 28)],
        "Open": [Decimal("9.8"), Decimal("9.9")],
    })
    klines = {"000001.SZ": {"1d": df_keep}, "600519.SH": {"1d": df_drop}}
    sliced = bt_api._slice_klines_from(klines, date(2026, 7, 29))
    assert set(sliced.keys()) == {"000001.SZ"}
    assert sliced["000001.SZ"]["1d"].height == 1
    assert sliced["000001.SZ"]["1d"]["datetime"].to_list() == [datetime(2026, 7, 29)]


def test_build_signal_cache_filters_warmup_entries(db_session, monkeypatch):
    """传 start：预热段（start 之前）的信号条目被过滤，cache 只留主区间；
    公式引擎仍拿到含预热段的全量时间范围。"""
    db = db_session
    ps, strat, formula = _seed_db(db)
    klines = {"000001.SZ": {"1d": pl.DataFrame({
        "datetime": [datetime(2026, 7, 28), datetime(2026, 7, 29), datetime(2026, 7, 30)],
        "Open": [Decimal("9.9"), Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10"), Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.8"), Decimal("9.9"), Decimal("8.9")],
        "Close": [Decimal("9.95"), Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000, 1000],
    })}}

    captured = {}
    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        captured["start_time"] = start_time
        captured["end_time"] = end_time
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260728", "Value": 1},   # 预热段 → 应被过滤
                    {"Date": "20260729", "Value": -1},
                    {"Date": "20260730", "Value": 1},
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db, start=date(2026, 7, 29))

    # 公式引擎拿到含预热段的全量时间范围（首 bar 起公式即有效）
    assert captured["start_time"] == "20260728"
    assert captured["end_time"] == "20260730"
    # start 之前的预热信号不进 cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 28)) not in cache
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "open_sig", "value": -1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 30))] == [{"name": "open_sig", "value": 1}]


def test_build_signal_cache_minute_warmup_align_then_filter(db_session, monkeypatch):
    """分钟级含预热：索引对齐必须基于全量时间轴（含预热 bar），过滤在转换后按 bar_time 做。
    若先切片再对齐，预热 bar 占用的索引会把主区间信号整体串位。"""
    from core.models import Strategy
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Strategy).filter_by(id=strat.id).update({"period": "5m"})
    db.commit()

    # 全量时间轴：07-28 14:55（预热）+ 07-29 09:35/09:40（主区间）
    klines = {"000001.SZ": {"5m": pl.DataFrame({
        "datetime": [
            datetime(2026, 7, 28, 14, 55),
            datetime(2026, 7, 29, 9, 35),
            datetime(2026, 7, 29, 9, 40),
        ],
        "Open": [Decimal("9.9"), Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10"), Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.8"), Decimal("9.9"), Decimal("8.9")],
        "Close": [Decimal("9.95"), Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000, 1000],
    })}}

    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        # TQ 真机语义：3 条输出按 bar 顺序，Date 只标到日
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260728", "Value": 9},   # → 预热 bar → 应被过滤
                    {"Date": "20260729", "Value": 1},   # → 09:35
                    {"Date": "20260729", "Value": -1},  # → 09:40
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db, start=date(2026, 7, 29))

    # 主区间信号值不串位（若对齐用了切片后时间轴，这里会拿到 9/-1 或错位值）
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35))] == [{"name": "open_sig", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40))] == [{"name": "open_sig", "value": -1}]
    assert (strat.id, "000001.SZ", datetime(2026, 7, 28, 14, 55)) not in cache


# ---------------------------------------------------------------------------
# 多周期信号对齐：klines 含 1d + 5m 时，5m 策略的 bar_times 必须只取 5m 时间轴，
# 不能混入 1d 午夜时间戳（否则索引串位，cache key 落在错误时间点）。
# ---------------------------------------------------------------------------
def test_build_signal_cache_multi_period_filters_by_strategy_period(db_session, monkeypatch):
    """多周期 klines（1d + 5m）：5m 策略 signal_cache key 全部在 5m 时间点，
    不含 1d 午夜时间——_bar_times_by_period 按策略周期过滤，避免并集串位。"""
    from core.models import Strategy
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Strategy).filter_by(id=strat.id).update({"period": "5m"})
    db.commit()

    # klines：1d 2 根（午夜）+ 5m 4 根（带时分）
    klines = {"000001.SZ": {
        "1d": pl.DataFrame({
            "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30)],
            "Open": [Decimal("10"), Decimal("10.2")],
            "High": [Decimal("10.3"), Decimal("10.5")],
            "Low": [Decimal("9.9"), Decimal("8.9")],
            "Close": [Decimal("10.2"), Decimal("9.0")],
            "Volume": [1000, 1000],
        }),
        "5m": pl.DataFrame({
            "datetime": [
                datetime(2026, 7, 29, 9, 35),
                datetime(2026, 7, 29, 9, 40),
                datetime(2026, 7, 30, 9, 35),
                datetime(2026, 7, 30, 9, 40),
            ],
            "Open": [Decimal("10"), Decimal("10.2"), Decimal("10.1"), Decimal("10.3")],
            "High": [Decimal("10.3"), Decimal("10.5"), Decimal("10.4"), Decimal("10.6")],
            "Low": [Decimal("9.9"), Decimal("8.9"), Decimal("10.0"), Decimal("10.1")],
            "Close": [Decimal("10.2"), Decimal("9.0"), Decimal("10.3"), Decimal("10.4")],
            "Volume": [1000, 1000, 1000, 1000],
        }),
    }}

    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        # 4 条 5m 输出（Date 日粒度，Value 逐条变）
        return {
            "000001.SZ": {
                "open_sig": [
                    {"Date": "20260729", "Value": 1},    # → 5m bar 09:35
                    {"Date": "20260729", "Value": -1},   # → 5m bar 09:40
                    {"Date": "20260730", "Value": 1},    # → 5m bar 09:35
                    {"Date": "20260730", "Value": -1},   # → 5m bar 09:40
                ],
            },
        }
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, klines, db)

    # 4 个 cache key 全部在 5m 时间点
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 30, 9, 35)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 30, 9, 40)) in cache
    # 1d 午夜时间不应出现在 5m 策略的 cache 中
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 0, 0)) not in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 30, 0, 0)) not in cache
    # 信号值不串位
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35))] == [{"name": "open_sig", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40))] == [{"name": "open_sig", "value": -1}]


def test_build_signal_cache_multi_period_daily_strategy_uses_date_match(db_session, monkeypatch):
    """同上 klines（1d + 5m），1d 策略走 Date 匹配（不传 bar_times）→
    cache key 在 1d 午夜时间点。"""
    db = db_session
    ps, strat, formula = _seed_db(db)  # 默认 1d 策略

    klines = {"000001.SZ": {
        "1d": pl.DataFrame({
            "datetime": [datetime(2026, 7, 29), datetime(2026, 7, 30)],
            "Open": [Decimal("10"), Decimal("10.2")],
            "High": [Decimal("10.3"), Decimal("10.5")],
            "Low": [Decimal("9.9"), Decimal("8.9")],
            "Close": [Decimal("10.2"), Decimal("9.0")],
            "Volume": [1000, 1000],
        }),
        "5m": pl.DataFrame({
            "datetime": [datetime(2026, 7, 29, 9, 35), datetime(2026, 7, 29, 9, 40)],
            "Open": [Decimal("10"), Decimal("10.2")],
            "High": [Decimal("10.3"), Decimal("10.5")],
            "Low": [Decimal("9.9"), Decimal("8.9")],
            "Close": [Decimal("10.2"), Decimal("9.0")],
            "Volume": [1000, 1000],
        }),
    }}

    def fake_compute(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
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

    # 1d 走 Date 匹配 → cache key 在午夜
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29)) in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 30)) in cache
    # 5m 时间点不应出现在 1d 策略的 cache 中
    assert (strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35)) not in cache


# ---------------------------------------------------------------------------
# engine=builtin：内置公式子集解释器分支（仅回测接线；engine 默认 tq 行为不变）
# ---------------------------------------------------------------------------
def _builtin_klines():
    """3 根日线合成行情：bar1 C>O、bar2 C<O、bar3 C>O。"""
    return {"000001.SZ": {"1d": pl.DataFrame({
        "datetime": [
            datetime(2026, 7, 29), datetime(2026, 7, 30), datetime(2026, 7, 31),
        ],
        "Open": [Decimal("10"), Decimal("10.2"), Decimal("10.0")],
        "High": [Decimal("10.3"), Decimal("10.5"), Decimal("10.4")],
        "Low": [Decimal("9.9"), Decimal("10.1"), Decimal("9.9")],
        "Close": [Decimal("10.2"), Decimal("9.0"), Decimal("10.2")],
        "Volume": [1000, 1000, 1000],
        "Amount": [Decimal("10200"), Decimal("9000"), Decimal("10200")],
    })}}


def test_build_signal_cache_builtin_engine_skips_tq(db_session, monkeypatch):
    """engine=builtin：不调 TQFormula.compute，对喂入 klines 求值出信号。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(C>O,1,0);",
    })
    db.commit()

    def fail_compute(*a, **kw):
        raise AssertionError("engine=builtin 不应调用 TQFormula.compute")
    monkeypatch.setattr(bt_api.TQFormula, "compute", fail_compute)

    cache = bt_api.build_signal_cache(ps, _builtin_klines(), db)

    # 与 TQ 口径一致：0（无信号常态值）也入 cache
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "SIG", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 30))] == [{"name": "SIG", "value": 0}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 31))] == [{"name": "SIG", "value": 1}]


def test_build_signal_cache_builtin_default_tq_unchanged(db_session, monkeypatch):
    """engine 默认 tq：仍走 TQ 按名调用（原有行为不变）。"""
    db = db_session
    ps, strat, formula = _seed_db(db)  # engine 默认 "tq"
    called = {}

    def fake_compute(self, formula_name, **kw):
        called["name"] = formula_name
        return {"000001.SZ": {"open_sig": [
            {"Date": "20260729", "Value": 1},
            {"Date": "20260730", "Value": 0},
            {"Date": "20260731", "Value": 1},
        ]}}
    monkeypatch.setattr(bt_api.TQFormula, "compute", fake_compute)

    cache = bt_api.build_signal_cache(ps, _builtin_klines(), db)

    assert called["name"] == "OPEN_FORMULA"
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "open_sig", "value": 1}]


def test_build_signal_cache_builtin_diagnostics_error(db_session):
    """builtin 求值报错 → diagnostics.errors 记录根因，cache 为空。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:C>FOO;",
    })
    db.commit()

    diagnostics = {}
    cache = bt_api.build_signal_cache(ps, _builtin_klines(), db, diagnostics=diagnostics)

    assert cache == {}
    assert strat.id in diagnostics["errors"]
    assert "FOO" in diagnostics["errors"][strat.id]


def test_build_signal_cache_builtin_diagnostics_output_lines(db_session):
    """正常求值 → diagnostics.output_lines 收集真实输出线名（信号预检用）。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(C>O,1,0);",
    })
    db.commit()

    diagnostics = {}
    bt_api.build_signal_cache(ps, _builtin_klines(), db, diagnostics=diagnostics)

    assert diagnostics["output_lines"][strat.id] == {"SIG"}


def test_build_signal_cache_builtin_minute_uses_df_datetime(db_session):
    """分钟级 builtin：df 行序即 bar 序列，key 直接用 df datetime（含时分）。"""
    from core.models import Formula, Strategy
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Strategy).filter_by(id=strat.id).update({"period": "5m"})
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(C>O,1,0);",
    })
    db.commit()

    klines = {"000001.SZ": {"5m": pl.DataFrame({
        "datetime": [datetime(2026, 7, 29, 9, 35), datetime(2026, 7, 29, 9, 40)],
        "Open": [Decimal("10"), Decimal("10.2")],
        "High": [Decimal("10.3"), Decimal("10.5")],
        "Low": [Decimal("9.9"), Decimal("10.1")],
        "Close": [Decimal("10.2"), Decimal("9.0")],
        "Volume": [1000, 1000],
        "Amount": [Decimal("10200"), Decimal("9000")],
    })}}

    cache = bt_api.build_signal_cache(ps, klines, db)

    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 35))] == [{"name": "SIG", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29, 9, 40))] == [{"name": "SIG", "value": 0}]


def test_build_signal_cache_builtin_start_filters_warmup(db_session):
    """builtin 信号同样受 start 过滤：预热段条目不进 cache。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(C>O,1,0);",
    })
    db.commit()

    cache = bt_api.build_signal_cache(
        ps, _builtin_klines(), db, start=date(2026, 7, 30))

    assert (strat.id, "000001.SZ", datetime(2026, 7, 29)) not in cache
    assert (strat.id, "000001.SZ", datetime(2026, 7, 30)) in cache


# ---------------------------------------------------------------------------
# 批次 9b 跨公式引用：resolver 闭包 + memo + 静态环检测（build_signal_cache 级）
# ---------------------------------------------------------------------------
def test_build_signal_cache_xref_base_period(db_session, monkeypatch):
    """9b 跨公式引用：SIG:IF(TESTA.A1=100,1,0)——resolver 在 base 周期求值 TESTA。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.add(Formula(name="TESTA", content="A1:IF(C>O,100,50);", engine="builtin"))
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(TESTA.A1=100,1,0);",
    })
    db.commit()

    def fail_compute(*a, **kw):
        raise AssertionError("跨公式引用不应调用 TQFormula.compute")
    monkeypatch.setattr(bt_api.TQFormula, "compute", fail_compute)

    cache = bt_api.build_signal_cache(ps, _builtin_klines(), db)

    # TESTA.A1=[100,50,100]（C>O 判定）：bar1/bar3 → 1，bar2 → 0
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "SIG", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 30))] == [{"name": "SIG", "value": 0}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 31))] == [{"name": "SIG", "value": 1}]


def test_build_signal_cache_xref_system_macd_week(db_session, monkeypatch):
    """系统公式兜底：库内未建 MACD 时 "MACD.DIF#WEEK" 仍可解析（自动取周线对齐）。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin",
        "content": 'SIG:IF("MACD.DIF#WEEK">0,1,0);',
    })
    db.commit()

    klines = _builtin_klines()
    klines["000001.SZ"]["1w"] = pl.DataFrame({
        "datetime": [datetime(2026, 7, 20), datetime(2026, 7, 27), datetime(2026, 8, 3)],
        "Open": [Decimal("5"), Decimal("10"), Decimal("20")],
        "High": [Decimal("6"), Decimal("11"), Decimal("21")],
        "Low": [Decimal("4"), Decimal("9"), Decimal("19")],
        "Close": [Decimal("5"), Decimal("10"), Decimal("20")],
        "Volume": [5000, 5000, 5000],
        "Amount": [Decimal("25000"), Decimal("50000"), Decimal("100000")],
    })

    def fail_compute(*a, **kw):
        raise AssertionError("跨公式引用不应调用 TQFormula.compute")
    monkeypatch.setattr(bt_api.TQFormula, "compute", fail_compute)

    diagnostics = {}
    cache = bt_api.build_signal_cache(ps, klines, db, diagnostics=diagnostics)

    assert not diagnostics.get("errors"), diagnostics
    assert cache, "系统 MACD 兜底后应产出信号"
    # 日线落在 7/27 那根周线上（周线收盘 5→10→20 上行，该根 DIF>0）→ 信号恒为 1
    assert all(v[0]["value"] == 1 for v in cache.values())


MOMENTUM_MODEL_SRC = """SHORT:=12;
LONG:=26;
MID:=9;

周DIF:="MACD.DIF#WEEK";
周DEA:="MACD.DEA#WEEK";
周MACD:="MACD.MACD#WEEK";

DIF:=周DIF;
DEA:=周DEA;
MACD:=周MACD;

金叉:=CROSS(周DIF,周DEA);
金叉后周期数:=BARSLAST(金叉);
金叉后最低DIF:=LLV(周DIF,金叉后周期数+1);
转强:=金叉后周期数>0 AND 金叉后最低DIF<-0.12 AND CROSS(周DIF,-0.12) AND 周MACD>0;
强势金叉:=金叉 AND 周DIF>=-0.12;
普通金叉:=金叉 AND 周DIF<-0.12;

普通金叉信号:IF(普通金叉,1,0);
强势金叉信号:IF(强势金叉,1,0);
转强信号:IF(转强,1,0);
"""


def test_build_signal_cache_momentum_model_builtin_e2e(db_session, monkeypatch):
    """用户实际粘贴的「创懿动量模型」原文：语法预检 + builtin 求值全链路，零 TQ 公式调用。

    覆盖此前只有合成公式验证过的部分：BARSLAST/LLV 动态周期、系统公式 MACD 兜底、
    `:=` 与中文标识符、三条 `:` 输出线。
    """
    from core.engine.tdx_formula import syntax_check
    from core.models import Formula

    checked = syntax_check(MOMENTUM_MODEL_SRC)
    assert checked["ok"], checked
    assert checked["outputs"] == ["普通金叉信号", "强势金叉信号", "转强信号"]

    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": MOMENTUM_MODEL_SRC,
    })
    db.commit()

    klines = _builtin_klines()
    # 60 根周线：先长期阴跌（DIF 下探 -0.12 以下）再强势反弹（制造金叉）
    closes = [20 - i * 0.2 for i in range(45)] + [11 + i * 0.8 for i in range(15)]
    from datetime import timedelta
    weekly_dates = [datetime(2025, 6, 2) + timedelta(days=7 * i) for i in range(60)]
    klines["000001.SZ"]["1w"] = pl.DataFrame({
        "datetime": weekly_dates,
        "Open": [Decimal(str(c)) for c in closes],
        "High": [Decimal(str(c * 1.01)) for c in closes],
        "Low": [Decimal(str(c * 0.99)) for c in closes],
        "Close": [Decimal(str(c)) for c in closes],
        "Volume": [5000] * 60,
        "Amount": [Decimal("50000")] * 60,
    })

    def fail_compute(*a, **kw):
        raise AssertionError("engine=builtin 不应调用 TQFormula.compute")
    monkeypatch.setattr(bt_api.TQFormula, "compute", fail_compute)

    diagnostics = {}
    cache = bt_api.build_signal_cache(ps, klines, db, diagnostics=diagnostics)

    assert not diagnostics.get("errors"), diagnostics
    assert cache, "内置求值应产出信号条目"
    assert diagnostics["output_lines"][strat.id] == {
        "普通金叉信号", "强势金叉信号", "转强信号"}
    names = {item["name"] for items in cache.values() for item in items}
    assert names <= {"普通金叉信号", "强势金叉信号", "转强信号"}
    assert all(item["value"] in (0, 1) for items in cache.values() for item in items)


def test_build_signal_cache_xref_missing_formula(db_session):
    """9b 被引公式不存在或非 builtin → diagnostics.errors，cache 空。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.add(Formula(name="TQ_ONLY", content="A1:CLOSE;", engine="tq"))
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(NOSUCH.A1>C,1,0);",
    })
    db.commit()

    diagnostics = {}
    cache = bt_api.build_signal_cache(
        ps, _builtin_klines(), db, diagnostics=diagnostics)

    assert cache == {}
    assert strat.id in diagnostics["errors"]
    assert "NOSUCH" in diagnostics["errors"][strat.id]


def test_build_signal_cache_xref_cycle(db_session):
    """9b 自引用 → 静态环检测报错（不进入求值，避免无限递归）。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:OPEN_FORMULA.A1;",
    })
    db.commit()

    diagnostics = {}
    cache = bt_api.build_signal_cache(
        ps, _builtin_klines(), db, diagnostics=diagnostics)

    assert cache == {}
    assert "环" in diagnostics["errors"][strat.id]


def test_build_signal_cache_xref_cross_period(db_session):
    """9b 组合语法 TESTA.A1#WEEK：在 1w 上求值 TESTA → 对齐折回日线。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.add(Formula(name="TESTA", content="A1:CLOSE;", engine="builtin"))
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": "SIG:IF(TESTA.A1#WEEK>C,1,0);",
    })
    db.commit()

    klines = _builtin_klines()
    klines["000001.SZ"]["1w"] = pl.DataFrame({
        "datetime": [datetime(2026, 7, 27)],
        "Open": [Decimal("9")], "High": [Decimal("11")], "Low": [Decimal("9")],
        "Close": [Decimal("10.1")],
        "Volume": [1000], "Amount": [Decimal("10000")],
    })

    cache = bt_api.build_signal_cache(ps, klines, db)

    # 周 A1=10.1 对齐三根日线：C=10.2→0；C=9.0→1；C=10.2→0
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 29))] == [{"name": "SIG", "value": 0}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 30))] == [{"name": "SIG", "value": 1}]
    assert cache[(strat.id, "000001.SZ", datetime(2026, 7, 31))] == [{"name": "SIG", "value": 0}]


# ---------------------------------------------------------------------------
# 批次 9a+9b 组合：跨周期(#WEEK) + 跨公式("MACD.DIF#WEEK") + CROSS/BARSLAST/LLV
# （用户实际案例：创懿动量模型，内置引擎，数据走 TQ K线）
# ---------------------------------------------------------------------------
_MOMENTUM_FORMULA = """SHORT:=12;
LONG:=26;
MID:=9;
周DIF:=\"MACD.DIF#WEEK\";
周DEA:=\"MACD.DEA#WEEK\";
周MACD:=\"MACD.MACD#WEEK\";
DIF:=周DIF;
DEA:=周DEA;
MACD:=周MACD;
金叉:=CROSS(周DIF,周DEA);
金叉后周期数:=BARSLAST(金叉);
金叉后最低DIF:=LLV(周DIF,金叉后周期数+1);
转强:=金叉后周期数>0 AND 金叉后最低DIF<-0.12 AND CROSS(周DIF,-0.12) AND 周MACD>0;
强势金叉:=金叉 AND 周DIF>=-0.12;
普通金叉:=金叉 AND 周DIF<-0.12;
普通金叉信号:IF(普通金叉,1,0);
强势金叉信号:IF(强势金叉,1,0);
转强信号:IF(转强,1,0);"""


def _macd_momentum_klines():
    """周线先跌后涨制造 DIF 金叉；日线由周线展开（每日同价）。"""
    from datetime import timedelta
    week_closes = [Decimal(str(round(10 - 1.0 * i / 19, 4))) for i in range(20)]
    week_closes += [Decimal(str(round(9 + 1.5 * i / 19, 4))) for i in range(20)]
    week_dt = [datetime(2022, 1, 3) + timedelta(days=7 * i) for i in range(40)]
    w_df = pl.DataFrame({
        "datetime": week_dt, "Open": week_closes,
        "High": [c + Decimal("0.2") for c in week_closes],
        "Low": [c - Decimal("0.2") for c in week_closes], "Close": week_closes,
        "Volume": [1000] * 40, "Amount": [Decimal("10000")] * 40,
    })
    d_dt, d_close = [], []
    for i, c in enumerate(week_closes):
        base = week_dt[i]
        for d in range(5):
            d_dt.append(base + timedelta(days=d))
            d_close.append(c)
    d_df = pl.DataFrame({
        "datetime": d_dt, "Open": d_close,
        "High": [c + Decimal("0.2") for c in d_close],
        "Low": [c - Decimal("0.2") for c in d_close], "Close": d_close,
        "Volume": [1000] * len(d_dt), "Amount": [Decimal("10000")] * len(d_dt),
    })
    return {"000001.SZ": {"1d": d_df, "1w": w_df}}


def test_build_signal_cache_builtin_macd_momentum_crossweek(db_session, monkeypatch):
    """创懿动量模型（内置引擎）跨周期跨公式：'MACD.DIF#WEEK' + CROSS/BARSLAST/LLV 可求值出信号。"""
    from core.models import Formula
    db = db_session
    ps, strat, formula = _seed_db(db)
    db.add(Formula(name="MACD", engine="builtin",
                   content="DIF:EMA(CLOSE,12)-EMA(CLOSE,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"))
    db.query(Formula).filter_by(id=formula.id).update({
        "engine": "builtin", "content": _MOMENTUM_FORMULA,
    })
    db.commit()

    def fail_compute(*a, **kw):
        raise AssertionError("内置引擎不应调用 TQFormula.compute")
    monkeypatch.setattr(bt_api.TQFormula, "compute", fail_compute)

    diagnostics = {}
    cache = bt_api.build_signal_cache(ps, _macd_momentum_klines(), db, diagnostics=diagnostics)

    assert not diagnostics.get("errors", {}).get(strat.id), diagnostics.get("errors")
    assert diagnostics["output_lines"][strat.id] == {"普通金叉信号", "强势金叉信号", "转强信号"}
    assert cache
    values = [v["value"] for lst in cache.values() for v in lst]
    assert any(v != 0 for v in values), "周 DIF 回升段应至少出现一次金叉信号"
