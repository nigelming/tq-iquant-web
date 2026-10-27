"""TQ 1.3.1 包装层单测（TDD）。

打 SDK 层：两包装模块均以 `from .utils import get_tq` 引入名字，
须 patch `core.tq.formula.get_tq` / `core.tq.data.get_tq`
（patch core.tq.utils.get_tq 不生效——模块导入时名字已绑定）。

SDK 实证（tqcenter.py 1.3.1 源码核对）：
- formula_by_src 内部自动分批合并（无需平台分批），校验失败打印返回 {}
- get_trading_dates(market='SH') → ["YYYYMMDD", ...]，失败打印返回 []
- get_zdt_data → json_res['Value']（仅状态无价格），空列表打印返回 {}
- create_sector 返回 JSON 字符串（失败 None），不抛异常
- send_user_block 返回 dict（失败 {}）
- unsubscribe_hq 空列表 raise ValueError（wrapper 必须空列表短路不触 SDK）
- get_divid_factors 空代码返回空 DataFrame
"""
import pandas as pd
import pytest

import core.tq.data as tq_data
import core.tq.formula as tq_formula
from core.tq.data import TQData
from core.tq.formula import TQFormula


class FakeTq:
    """记录调用参数并返回可配置结果的 tqcenter.tq 替身。"""

    def __init__(self):
        self.calls = {}
        self.formula_by_src_result = {"ErrorId": "0"}
        self.trading_dates_result = ["20240102", "20240103"]
        self.trading_dates_raise = False
        self.zdt_result = {}
        self.create_sector_result = '{"ErrorId": "0"}'
        self.send_user_block_result = {"ErrorId": "0"}
        self.divid_result = pd.DataFrame()
        self.batch_result = {}

    def formula_by_src(self, **kwargs):
        self.calls["formula_by_src"] = kwargs
        return self.formula_by_src_result

    def get_trading_dates(self, market, start_time, end_time, count=-1):
        self.calls["get_trading_dates"] = {
            "market": market,
            "start_time": start_time,
            "end_time": end_time,
        }
        if self.trading_dates_raise:
            raise RuntimeError("SDK boom")
        return self.trading_dates_result

    def get_zdt_data(self, stock_list):
        self.calls["get_zdt_data"] = list(stock_list)
        return self.zdt_result

    def create_sector(self, block_code="", block_name=""):
        self.calls["create_sector"] = {"block_code": block_code, "block_name": block_name}
        return self.create_sector_result

    def send_user_block(self, block_code="", stock_list=[], show=False):
        self.calls["send_user_block"] = {
            "block_code": block_code,
            "stock_list": list(stock_list),
            "show": show,
        }
        return self.send_user_block_result

    def unsubscribe_hq(self, stock_list):
        self.calls["unsubscribe_hq"] = list(stock_list)

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        self.calls["get_divid_factors"] = {
            "stock_code": stock_code,
            "start_time": start_time,
            "end_time": end_time,
        }
        return self.divid_result

    def get_market_snapshot_batch(self, stock_list=[], field_list=[]):
        self.calls["get_market_snapshot_batch"] = {
            "stock_list": list(stock_list), "field_list": list(field_list)}
        return self.batch_result

    def get_stock_info_batch(self, stock_list=[], field_list=[]):
        self.calls["get_stock_info_batch"] = {
            "stock_list": list(stock_list), "field_list": list(field_list)}
        return self.batch_result

    def get_more_info_batch(self, stock_list=[], field_list=[]):
        self.calls["get_more_info_batch"] = {
            "stock_list": list(stock_list), "field_list": list(field_list)}
        return self.batch_result


@pytest.fixture
def fake_tq(monkeypatch):
    tq = FakeTq()
    monkeypatch.setattr(tq_formula, "get_tq", lambda: tq)
    monkeypatch.setattr(tq_data, "get_tq", lambda: tq)
    return tq


# --- compute_by_src：试算公式源码 ---

def test_compute_by_src_passes_params_and_cleans_meta(fake_tq):
    fake_tq.formula_by_src_result = {
        "ErrorId": "0", "Error": "", "Date": "20240102", "Time": "150000",
        "000001.SZ": {"CLOSE": [10.0]},
        "000002.SZ": {"CLOSE": [20.0]},
    }
    out = TQFormula().compute_by_src(
        "CLOSE>5;", ["000001.SZ", "000002.SZ"], period="5m",
        count=-1, dividend_type=2, formula_type=1,
        return_count=3, return_date=False,
    )
    call = fake_tq.calls["formula_by_src"]
    assert call["formula_src"] == "CLOSE>5;"
    assert call["stock_list"] == ["000001.SZ", "000002.SZ"]
    assert call["stock_period"] == "5m"
    assert call["count"] == -1
    assert call["dividend_type"] == 2
    assert call["formula_type"] == 1
    assert call["return_count"] == 3
    assert call["return_date"] is False
    # meta 键清洗 + 成功标记，与 compute 输出同构
    assert out == {
        "ErrorId": "0",
        "000001.SZ": {"CLOSE": [10.0]},
        "000002.SZ": {"CLOSE": [20.0]},
    }


def test_compute_by_src_error_kept(fake_tq):
    fake_tq.formula_by_src_result = {"ErrorId": 19, "Error": "数据过多"}
    out = TQFormula().compute_by_src("X;", ["000001.SZ"])
    assert out == {"ErrorId": "19", "Error": "数据过多"}


def test_compute_by_src_empty_result_is_error(fake_tq):
    fake_tq.formula_by_src_result = {}
    out = TQFormula().compute_by_src("X;", ["000001.SZ"])
    assert out["ErrorId"] != "0"
    assert out["Error"]


def test_compute_by_src_empty_stocks_no_sdk(fake_tq):
    out = TQFormula().compute_by_src("X;", [])
    assert out == {"ErrorId": "0"}
    assert "formula_by_src" not in fake_tq.calls


# --- get_trading_dates：交易日历 ---

def test_get_trading_dates_passthrough_and_normalized(fake_tq):
    fake_tq.trading_dates_result = ["20240102", "20240103"]
    out = TQData().get_trading_dates("SH", "20240101", "20240131")
    assert out == ["20240102", "20240103"]
    call = fake_tq.calls["get_trading_dates"]
    assert call == {"market": "SH", "start_time": "20240101", "end_time": "20240131"}


def test_get_trading_dates_int_and_space_normalized(fake_tq):
    fake_tq.trading_dates_result = [20240102, " 20240103 "]
    assert TQData().get_trading_dates() == ["20240102", "20240103"]


def test_get_trading_dates_empty(fake_tq):
    fake_tq.trading_dates_result = []
    assert TQData().get_trading_dates() == []


def test_get_trading_dates_exception_to_empty(fake_tq):
    fake_tq.trading_dates_raise = True
    assert TQData().get_trading_dates() == []


# --- get_zdt_data：涨跌停状态 ---

def test_get_zdt_data_passthrough(fake_tq):
    fake_tq.zdt_result = {"000001.SZ": {"ZDTStatusNow": 1, "ZDTStatusOri": 0}}
    out = TQData().get_zdt_data(["000001.SZ"])
    assert out == {"000001.SZ": {"ZDTStatusNow": 1, "ZDTStatusOri": 0}}
    assert fake_tq.calls["get_zdt_data"] == ["000001.SZ"]


def test_get_zdt_data_empty_no_sdk(fake_tq):
    assert TQData().get_zdt_data([]) == {}
    assert "get_zdt_data" not in fake_tq.calls


# --- ensure_sector / send_user_block：自选股板块 ---

def test_ensure_sector_parses_json(fake_tq):
    fake_tq.create_sector_result = '{"ErrorId": "0", "Msg": "ok"}'
    out = TQData().ensure_sector("MYBK", "我的板块")
    assert out == {"ErrorId": "0", "Msg": "ok"}
    assert fake_tq.calls["create_sector"] == {"block_code": "MYBK", "block_name": "我的板块"}


def test_ensure_sector_existing_not_error(fake_tq):
    # SDK 已存在板块时不抛异常，返回带 ErrorId 的 JSON → wrapper 原样解析返回
    fake_tq.create_sector_result = '{"ErrorId": "3", "Error": "板块已存在"}'
    assert TQData().ensure_sector("MYBK", "我的板块") == {
        "ErrorId": "3", "Error": "板块已存在"}


def test_ensure_sector_none_result_to_empty(fake_tq):
    fake_tq.create_sector_result = None
    assert TQData().ensure_sector("MYBK", "我的板块") == {}


def test_send_user_block_show_passthrough(fake_tq):
    fake_tq.send_user_block_result = {"ErrorId": "0", "N": 1}
    out = TQData().send_user_block("MYBK", ["000001.SZ"], show=True)
    assert out == {"ErrorId": "0", "N": 1}
    assert fake_tq.calls["send_user_block"] == {
        "block_code": "MYBK", "stock_list": ["000001.SZ"], "show": True}


# --- unsubscribe_bars：退订 ---

def test_unsubscribe_bars_empty_no_sdk(fake_tq):
    TQData().unsubscribe_bars([])
    assert "unsubscribe_hq" not in fake_tq.calls


def test_unsubscribe_bars_calls_sdk(fake_tq):
    TQData().unsubscribe_bars(["000001.SZ", "000002.SZ"])
    assert fake_tq.calls["unsubscribe_hq"] == ["000001.SZ", "000002.SZ"]


# --- get_divid_factors：除权除息 ---

def test_get_divid_factors_passthrough(fake_tq):
    df = pd.DataFrame({"fhr": [0.1]})
    fake_tq.divid_result = df
    out = TQData().get_divid_factors("000001.SZ", "20200101", "20240101")
    assert out is df
    assert fake_tq.calls["get_divid_factors"] == {
        "stock_code": "000001.SZ", "start_time": "20200101", "end_time": "20240101"}


def test_get_divid_factors_empty_code_no_sdk(fake_tq):
    out = TQData().get_divid_factors("")
    assert out.empty
    assert "get_divid_factors" not in fake_tq.calls


# --- 三个批量 wrapper：透传 field_list ---

@pytest.mark.parametrize("method", [
    "get_market_snapshot_batch",
    "get_stock_info_batch",
    "get_more_info_batch",
])
def test_batch_wrappers_pass_field_list(fake_tq, method):
    fake_tq.batch_result = {"000001.SZ": {"Open": 10.0}}
    out = getattr(TQData(), method)(["000001.SZ"], ["Open", "Close"])
    assert out == {"000001.SZ": {"Open": 10.0}}
    assert fake_tq.calls[method] == {
        "stock_list": ["000001.SZ"], "field_list": ["Open", "Close"]}
