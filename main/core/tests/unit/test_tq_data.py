"""TQData 板块/成分股解析单测（TDD）。

打 SDK 层（core.tq.data.get_tq 返回 fake tq），覆盖 _get_pools/_get_stocks 对
tqcenter 真实返回 dict 的解析——v1 因 monkeypatch 高层 TQData 导致这层从没被测，
线上 TypeError: unhashable type: 'dict'。

SDK 真实格式（探查实证）：
- get_user_sector() → [{"Code":..., "Name":...}]
- get_stock_list_in_sector(code, block_type=1, list_type=1) → [{"Code":..,"Name":..}]
"""
from datetime import datetime

import pytest

from core.tq.data import TQData
import core.tq.data as tq_data


class FakeTq:
    """模拟 tqcenter.tq 的板块相关方法，返回真实 SDK dict 结构。"""

    def __init__(self, sectors, stocks_map):
        self._sectors = sectors          # [{"Code","Name"}]
        self._stocks = stocks_map        # {block_code: [{"Code","Name"}]}
        self.last_call = None            # 记录 get_stock_list_in_sector 调用参数

    def get_user_sector(self):
        return self._sectors

    def get_stock_list_in_sector(self, block_code, block_type=0, list_type=0):
        self.last_call = {
            "block_code": block_code,
            "block_type": block_type,
            "list_type": list_type,
        }
        return self._stocks.get(block_code, [])


FIELDS = ["Open", "High", "Low", "Close", "Volume", "Amount"]


class FakeMarketTq:
    """模拟 tqcenter get_market_data：批量调用固定抛非法日期异常（复现线上 20040000）。

    逐只调用时，坏股票（bad_codes）仍抛错，好股票返回单列 DataFrame。
    batch_raises=False 时批量调用正常返回（用于验证不触发兜底）。
    """

    def __init__(self, good, bad_codes=(), batch_raises=True):
        self._good = good                # {code: {field: [v1, v2]}}
        self._bad = set(bad_codes)
        self._batch_raises = batch_raises
        self.batch_calls = 0
        self.single_calls = []

    def get_market_data(self, field_list, stock_list, period, start_time="",
                        end_time="", count=100, dividend_type="front",
                        fill_data=False):
        import pandas as pd
        if len(stock_list) > 1:
            self.batch_calls += 1
            if self._batch_raises:
                raise ValueError(
                    "time data '20040000' does not match format '%Y%m%d'")
            idx = pd.to_datetime([datetime(2026, 7, 29), datetime(2026, 7, 30)])
            out = {}
            for f in field_list:
                cols = {c: self._good[c][f] for c in stock_list if c in self._good}
                out[f] = pd.DataFrame(cols, index=idx)
            return out
        code = stock_list[0]
        self.single_calls.append(code)
        if code in self._bad:
            raise ValueError(
                "time data '20040000' does not match format '%Y%m%d'")
        idx = pd.to_datetime([datetime(2026, 7, 29), datetime(2026, 7, 30)])
        return {f: pd.DataFrame({code: self._good[code][f]}, index=idx)
                for f in field_list}


def _good_data():
    return {
        "000001.SZ": {f: [10.0, 10.2] for f in FIELDS},
        "600519.SH": {f: [1600.0, 1610.0] for f in FIELDS},
    }


@pytest.fixture
def patch_tq(monkeypatch):
    """返回一个工厂：设置 fake tq 并 patch core.tq.data.get_tq。"""
    def _setup(sectors, stocks_map=None):
        stocks_map = stocks_map or {}
        fake = FakeTq(sectors, stocks_map)
        monkeypatch.setattr(tq_data, "get_tq", lambda: fake)
        return fake
    return _setup


# ---------------------------------------------------------------------------
# get_stock_pools — 解析 get_user_sector 的 {Code,Name} dict
# ---------------------------------------------------------------------------
def test_get_pools_parses_user_sector(patch_tq):
    """get_user_sector 返 {Code,Name} dict 列表 → get_stock_pools 归一化为 [{code,name}]。"""
    patch_tq(sectors=[
        {"Code": "TQCS", "Name": "tq自选"},
        {"Code": "DEGP", "Name": "第二股票"},
    ])

    pools = TQData().get_stock_pools()

    assert pools == [
        {"code": "TQCS", "name": "tq自选"},
        {"code": "DEGP", "name": "第二股票"},
    ]


def test_get_pools_empty(patch_tq):
    """通达信无用户板块 → 返 []。"""
    patch_tq(sectors=[])
    assert TQData().get_stock_pools() == []


# ---------------------------------------------------------------------------
# get_pool_stocks — 传板块 Code，解析成分股 {Code,Name}
# ---------------------------------------------------------------------------
def test_get_stocks_parses_sector_stocks(patch_tq):
    """成分股返 {Code,Name} → 归一化为 [{stock_code,stock_name}]，调参 block_type=1,list_type=1。"""
    fake = patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]},
    )

    stocks = TQData().get_pool_stocks("TQCS")

    assert stocks == [
        {"stock_code": "600000.SH", "stock_name": "浦发银行"},
        {"stock_code": "000001.SZ", "stock_name": "平安银行"},
    ]
    # 确认调用 SDK 时传了正确的 block_type / list_type
    assert fake.last_call == {
        "block_code": "TQCS",
        "block_type": 1,
        "list_type": 1,
    }


def test_get_stocks_passes_block_code_not_name(patch_tq):
    """get_pool_stocks 入参是板块 Code（如 TQCS），不是 Name。"""
    fake = patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [{"Code": "600000.SH", "Name": "浦发银行"}]},
    )

    TQData().get_pool_stocks("TQCS")

    assert fake.last_call["block_code"] == "TQCS"


def test_get_stocks_empty(patch_tq):
    """板块存在但无成分股 → 返 []（不报错）。"""
    patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": []},
    )
    assert TQData().get_pool_stocks("TQCS") == []


def test_get_stocks_unknown_code(patch_tq):
    """板块 Code 不在 stocks_map → 返 []（SDK 对未知板块返空）。"""
    patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [{"Code": "600000.SH", "Name": "浦发银行"}]},
    )
    assert TQData().get_pool_stocks("NOPE") == []


# ---------------------------------------------------------------------------
# get_history_raw 逐股兜底：批量 get_market_data 抛异常（通达信返回非法日期）
# 时不整单失败——逐只重试、跳过取不到的股票，其余正常返回
# ---------------------------------------------------------------------------
def test_get_history_raw_batch_ok_no_fallback(monkeypatch):
    """批量调用成功 → 不触发逐股兜底，skipped_stocks 为空。"""
    fake = FakeMarketTq(_good_data(), batch_raises=False)
    monkeypatch.setattr(tq_data, "get_tq", lambda: fake)

    td = TQData()
    res = td.get_history_raw(["000001.SZ", "600519.SH"], ["1d"],
                             start="20260101", end="20261010", count=-1)

    assert fake.batch_calls == 1
    assert fake.single_calls == []
    assert set(res["1d"]["Close"].columns) == {"000001.SZ", "600519.SH"}
    assert td.skipped_stocks == []


def test_get_history_raw_batch_error_falls_back_per_stock(monkeypatch):
    """批量抛非法日期异常 → 逐股拉取；坏股票被跳过并记录，好股票照常返回。"""
    fake = FakeMarketTq(_good_data(), bad_codes=["920000.BJ"])
    monkeypatch.setattr(tq_data, "get_tq", lambda: fake)

    td = TQData()
    res = td.get_history_raw(["000001.SZ", "920000.BJ", "600519.SH"], ["1d"],
                             start="20260101", end="20261010", count=-1)

    # 批量失败过，逐股尝试了全部 3 只
    assert fake.batch_calls == 1
    assert set(fake.single_calls) == {"000001.SZ", "920000.BJ", "600519.SH"}
    # 好股票正常合并，坏股票被剔除
    assert set(res["1d"]["Close"].columns) == {"000001.SZ", "600519.SH"}
    assert td.skipped_stocks == ["920000.BJ"]


def test_get_history_raw_all_bad_returns_empty_period(monkeypatch):
    """整批股票都取不到 → 该周期不出现在返回值（由上层空行情保护处理）。"""
    fake = FakeMarketTq({}, bad_codes=["920000.BJ", "920001.BJ"])
    monkeypatch.setattr(tq_data, "get_tq", lambda: fake)

    td = TQData()
    res = td.get_history_raw(["920000.BJ", "920001.BJ"], ["1d"],
                             start="20260101", end="20261010", count=-1)

    assert res == {}
    assert set(td.skipped_stocks) == {"920000.BJ", "920001.BJ"}


# ---------------------------------------------------------------------------
# strip_lunch_phantom — 午休幽灵 bar（13:00）剔除（TDD，回测 30m 信号错位根因）
# 批量 get_market_data(fill_data=True) 会把午休空档用「上一根 bar 收盘」填充：
# 在 11:30 后重复一根同样的数据并打 13:00 时间戳（1m/5m/15m/30m/1h 皆如此，
# 实测 30m = 9 根/日，13:00 与 11:30 OHLC 完全一致）。TQ 公式/图表返回逐 bar 真实
# 序列无此 bar；且 A 股分钟 bar 下午首根合法时间是 13:01/13:05/13:15/13:30/14:00，
# 13:00 永远不可能真实存在，整根剔除安全。不清掉会让分钟级公式输出按索引对齐
# 整体前移（隐性未来函数：后 N 根 bar 信号全部提前 N 根触发）。
# ---------------------------------------------------------------------------
_LUNCH_BARS = [
    (10, 0), (10, 30), (11, 0), (11, 30), (13, 0), (13, 30),
    (14, 0), (14, 30), (15, 0),
]


def _phantom_raw(day=datetime(2026, 1, 7), codes=("000001.SZ",)):
    import pandas as pd
    idx = pd.to_datetime([day.replace(hour=h, minute=m) for h, m in _LUNCH_BARS])
    return {
        "Close": pd.DataFrame({c: list(range(len(idx))) for c in codes}, index=idx),
        "Open": pd.DataFrame({c: list(range(len(idx))) for c in codes}, index=idx),
    }


@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h"])
def test_strip_lunch_phantom_drops_1300(period):
    """分钟周期批量数据含 13:00 幽灵 bar → 剔除；11:30/13:30 保留且时序不乱。"""
    out = tq_data.strip_lunch_phantom(_phantom_raw(), period)
    times = out["Close"].index
    assert len(times) == 8
    assert all(t.strftime("%H:%M") != "13:00" for t in times)
    assert times[3].strftime("%H:%M") == "11:30"
    assert times[4].strftime("%H:%M") == "13:30"


def test_strip_lunch_phantom_keeps_daily_and_clean_minute():
    """日线（无此问题）与不含 13:00 的分钟数据原样透传（对象同一，零拷贝）。"""
    import pandas as pd
    daily = {"Close": pd.DataFrame(
        {"000001.SZ": [1.0, 1.2]},
        index=pd.to_datetime([datetime(2026, 1, 7), datetime(2026, 1, 8)]))}
    assert tq_data.strip_lunch_phantom(daily, "1d") is daily
    clean = _phantom_raw()
    for k in list(clean):
        clean[k] = clean[k].loc[clean[k].index.strftime("%H:%M") != "13:00"]
    assert tq_data.strip_lunch_phantom(clean, "30m") is clean


def test_strip_lunch_phantom_empty_none():
    """None / 空 dict 透传，不崩。"""
    assert tq_data.strip_lunch_phantom(None, "30m") is None
    assert tq_data.strip_lunch_phantom({}, "30m") == {}


class FakePhantomBatch:
    """批量 get_market_data 返回含 13:00 午休幽灵 bar（fill_data=True 真实行为）。"""

    def __init__(self, period="30m"):
        self._period = period

    def get_market_data(self, field_list, stock_list, period, start_time="",
                        end_time="", count=100, dividend_type="front",
                        fill_data=False):
        import pandas as pd
        assert period == self._period
        idx = pd.to_datetime([
            datetime(2026, 1, 7, h, m) for h, m in _LUNCH_BARS])
        return {f: pd.DataFrame(
            {c: [float(i) for i in range(len(idx))] for c in stock_list}, index=idx)
            for f in field_list}


def test_get_history_raw_strips_phantom_30m(monkeypatch):
    """get_history_raw 批量结果剔除 13:00 → 剩 8 根/日（回测 klines 时间轴干净）。"""
    monkeypatch.setattr(tq_data, "get_tq", lambda: FakePhantomBatch("30m"))
    td = TQData()
    res = td.get_history_raw(["000001.SZ"], ["30m"],
                             start="20260101", end="20260201", count=-1)
    times = res["30m"]["Close"].index
    assert len(times) == 8
    assert all(t.strftime("%H:%M") != "13:00" for t in times)


def test_get_history_strips_phantom_30m(monkeypatch):
    """get_history（逐股 polars）同样不受幽灵 bar 污染（防御同源修复）。"""
    monkeypatch.setattr(tq_data, "get_tq", lambda: FakePhantomBatch("30m"))
    td = TQData()
    res = td.get_history(["000001.SZ"], ["30m"],
                         start="20260101", end="20260201", count=-1)
    assert res["000001.SZ"]["30m"].height == 8
