"""TQ REST API 集成测试（试算 / 自选股板块 / 除权除息 / 交易日历）。

TQ 层以类方法级 monkeypatch 替换（TQFormula.compute_by_src / TQData 各方法），
不发真实 DLL 调用。覆盖：
- POST /api/formulas/test-run：摘要形状（n/ones/last_values）、参数透传与默认值、
  参数校验 400、ErrorId→503、部分股票缺失 warning、TDXConnectionError→503
- POST /api/tdx/user-block：ensure→send 顺序断言、block_name 缺省回退 block_code、
  ErrorId→503（ensure 失败不 send）、SDK 空 dict（失败）→503、校验 400、连接错误 503；
  mode=replace（默认）delete→ensure→send（delete 失败 best-effort 不阻断）、
  mode=append 无 delete、mode 非法 400
- GET /api/tdx/divid-factors：Date 索引 DataFrame → events 记录（NaN→None）、
  stock_code 必填 400、连接错误 503
- GET /api/tdx/trading-dates：透传、start/end 必填 400、连接错误 503
"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from core.main import app
from core.tq.data import TQData
from core.tq.formula import TQFormula
from core.tq.utils import TDXConnectionError


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _patch_compute_by_src(monkeypatch, result=None, error=None):
    """替换 TQFormula.compute_by_src，记录调用参数；error 非空时抛出。"""
    calls = {}

    def fake(self, formula_src, stocks, **kwargs):
        calls["content"] = formula_src
        calls["stocks"] = list(stocks)
        calls["kwargs"] = kwargs
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(TQFormula, "compute_by_src", fake)
    return calls


def _patch_data(monkeypatch, name, fn):
    monkeypatch.setattr(TQData, name, fn)


def _patch_delete_noop(monkeypatch):
    """replace 模式默认先 delete_sector：不关心 delete 的用例补 no-op，避免触真实 SDK。"""
    _patch_data(monkeypatch, "delete_sector", lambda self, c: {"ErrorId": "0"})


# ---------------------------------------------------------------------------
# POST /api/formulas/test-run — 公式源码试算摘要
# ---------------------------------------------------------------------------

def test_test_run_success_summary_and_params(client, monkeypatch):
    result = {
        "ErrorId": "0",
        "000001.SZ": {"MACD": [
            {"Date": "2024-01-02", "Value": "0"},
            {"Date": "2024-01-03", "Value": "1"},
            {"Date": "2024-01-04", "Value": "1"},
            {"Date": "2024-01-05", "Value": "-1"},
            {"Date": "2024-01-08", "Value": None},
        ]},
        "000002.SZ": {"MACD": [{"Value": "2"}, {"Value": "1"}]},
    }
    calls = _patch_compute_by_src(monkeypatch, result=result)
    resp = client.post("/api/formulas/test-run", json={
        "content": "C>MA(C,5);",
        "stock_codes": ["000001.SZ", "000002.SZ"],
        "period": "30m", "count": 100, "dividend_type": 2, "formula_type": 1,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["results"] == {
        "000001.SZ": {"MACD": {"n": 5, "ones": 2,
                               "last_values": ["1", "-1", "None"]}},
        "000002.SZ": {"MACD": {"n": 2, "ones": 1, "last_values": ["2", "1"]}},
    }
    assert "warning" not in body["data"]
    assert calls["content"] == "C>MA(C,5);"
    assert calls["stocks"] == ["000001.SZ", "000002.SZ"]
    assert calls["kwargs"] == {"period": "30m", "count": 100,
                               "dividend_type": 2, "formula_type": 1}


def test_test_run_default_params(client, monkeypatch):
    calls = _patch_compute_by_src(
        monkeypatch, result={"ErrorId": "0",
                             "000001.SZ": {"X": [{"Value": "1"}]}})
    resp = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ"]})
    assert resp.json()["code"] == 0
    assert calls["kwargs"] == {"period": "1d", "count": 20,
                               "dividend_type": 1, "formula_type": 0}


def test_test_run_empty_content_400(client, monkeypatch):
    _patch_compute_by_src(monkeypatch)
    body = client.post("/api/formulas/test-run", json={
        "content": "  ", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 400


@pytest.mark.parametrize("stock_codes", [[], [f"300{i:03d}.SZ" for i in range(21)]])
def test_test_run_stock_count_400(client, monkeypatch, stock_codes):
    _patch_compute_by_src(monkeypatch)
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": stock_codes}).json()
    assert body["code"] == 400
    assert body["message"] == "试算股票数须 1-20"


def test_test_run_invalid_period_400(client, monkeypatch):
    _patch_compute_by_src(monkeypatch)
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ"], "period": "2d"}).json()
    assert body["code"] == 400
    assert "period" in body["message"]


@pytest.mark.parametrize("count", [0, 501])
def test_test_run_invalid_count_400(client, monkeypatch, count):
    _patch_compute_by_src(monkeypatch)
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ"], "count": count}).json()
    assert body["code"] == 400
    assert "count" in body["message"]


def test_test_run_error_id_503(client, monkeypatch):
    _patch_compute_by_src(
        monkeypatch, result={"ErrorId": "19", "Error": "数据过多无法加载"})
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert body["message"] == "数据过多无法加载"


def test_test_run_partial_stocks_warning(client, monkeypatch):
    # 请求 2 只只返回 1 只（SDK ErrorId=0 但部分股票失败的形态）
    _patch_compute_by_src(
        monkeypatch, result={"ErrorId": "0",
                             "000001.SZ": {"X": [{"Value": "1"}]}})
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ", "000002.SZ"]}).json()
    assert body["code"] == 0
    assert body["data"]["warning"] == "部分股票无返回（1/2）"


def test_test_run_connection_error_503(client, monkeypatch):
    _patch_compute_by_src(
        monkeypatch, error=TDXConnectionError("连接通达信失败，请确认通达信已启动并登录"))
    body = client.post("/api/formulas/test-run", json={
        "content": "C;", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert "连接通达信失败" in body["message"]


# ---------------------------------------------------------------------------
# POST /api/tdx/user-block — 写自选股板块（ensure_sector → send_user_block）
# ---------------------------------------------------------------------------

def test_user_block_success_chain(client, monkeypatch):
    log = []

    def fake_delete(self, block_code):
        log.append(("delete", block_code))
        return {"ErrorId": "0"}

    def fake_ensure(self, block_code, block_name):
        log.append(("ensure", block_code, block_name))
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append(("send", block_code, list(stock_codes), show))
        return {"ErrorId": "0"}

    _patch_data(monkeypatch, "delete_sector", fake_delete)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    resp = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "block_name": "我的板块",
        "stock_codes": ["000001.SZ", "000002.SZ"], "show": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"block_code": "MYBK", "count": 2, "mode": "replace"}
    assert log == [
        ("delete", "MYBK"),
        ("ensure", "MYBK", "我的板块"),
        ("send", "MYBK", ["000001.SZ", "000002.SZ"], True),
    ]


def test_user_block_empty_name_falls_back_to_code(client, monkeypatch):
    log = []

    def fake_ensure(self, block_code, block_name):
        log.append(("ensure", block_code, block_name))
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append(("send", block_code, list(stock_codes), show))
        return {"ErrorId": "0"}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 0
    assert log[0] == ("ensure", "MYBK", "MYBK")


def test_user_block_ensure_error_503_no_send(client, monkeypatch):
    log = []

    def fake_ensure(self, block_code, block_name):
        log.append("ensure")
        return {"ErrorId": "1", "Error": "创建板块失败"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append("send")
        return {"ErrorId": "0"}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert body["message"] == "创建板块失败"
    assert log == ["ensure"]


def test_user_block_ensure_existing_exempt_continues_send(client, monkeypatch):
    # SDK 对已存在板块的重复创建报 ErrorId≠0（中文「已存在」）→ 豁免，继续写入
    log = []

    def fake_ensure(self, block_code, block_name):
        log.append("ensure")
        return {"ErrorId": "3", "Error": "板块已存在"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append(("send", block_code, list(stock_codes)))
        return {"ErrorId": "0"}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "block_name": "我的板块",
        "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 0
    assert body["data"] == {"block_code": "MYBK", "count": 1, "mode": "replace"}
    assert log == ["ensure", ("send", "MYBK", ["000001.SZ"])]


def test_user_block_ensure_english_exist_exempt(client, monkeypatch):
    # 英文文案 "Already exist"（大小写不敏感）同样豁免
    log = []

    def fake_ensure(self, block_code, block_name):
        log.append("ensure")
        return {"ErrorId": "3", "Msg": "Block Already Exist"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append("send")
        return {"ErrorId": "0"}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 0
    assert log == ["ensure", "send"]


def test_user_block_send_empty_dict_503(client, monkeypatch):
    # SDK send_user_block 失败返回 {}（wrapper 归一）→ 503
    def fake_ensure(self, block_code, block_name):
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        return {}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert body["message"]


def test_user_block_send_error_503(client, monkeypatch):
    def fake_ensure(self, block_code, block_name):
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        return {"ErrorId": "2", "Error": "写入自选股失败"}

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert body["message"] == "写入自选股失败"


def test_user_block_connection_error_503(client, monkeypatch):
    def fake_ensure(self, block_code, block_name):
        raise TDXConnectionError("通达信未启动")

    _patch_delete_noop(monkeypatch)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 503
    assert body["message"] == "通达信未启动"


def test_user_block_validation_400(client, monkeypatch):
    calls = []
    _patch_data(monkeypatch, "ensure_sector",
                lambda self, c, n: calls.append(c) or {"ErrorId": "0"})
    _patch_data(monkeypatch, "send_user_block",
                lambda self, c, s, show=False: {"ErrorId": "0"})
    body = client.post("/api/tdx/user-block", json={
        "block_code": "  ", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 400
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": []}).json()
    assert body["code"] == 400
    assert calls == []


def test_user_block_replace_mode_order_delete_ensure_send(client, monkeypatch):
    # mode=replace：delete→ensure→send 顺序（覆盖写：先清空板块再写入）
    log = []

    def fake_delete(self, block_code):
        log.append(("delete", block_code))
        return {"ErrorId": "0"}

    def fake_ensure(self, block_code, block_name):
        log.append(("ensure", block_code, block_name))
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append(("send", block_code, list(stock_codes), show))
        return {"ErrorId": "0"}

    _patch_data(monkeypatch, "delete_sector", fake_delete)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "block_name": "我的板块",
        "stock_codes": ["000001.SZ"], "mode": "replace"}).json()
    assert body["code"] == 0
    assert body["data"] == {"block_code": "MYBK", "count": 1, "mode": "replace"}
    assert log == [("delete", "MYBK"), ("ensure", "MYBK", "我的板块"),
                   ("send", "MYBK", ["000001.SZ"], False)]


def test_user_block_replace_delete_failure_best_effort(client, monkeypatch):
    # replace 模式 delete_sector 抛异常 → warning 不阻断，继续 ensure→send
    log = []

    def fake_delete(self, block_code):
        raise TDXConnectionError("删除板块失败")

    def fake_ensure(self, block_code, block_name):
        log.append("ensure")
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append("send")
        return {"ErrorId": "0"}

    _patch_data(monkeypatch, "delete_sector", fake_delete)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"]}).json()
    assert body["code"] == 0
    assert body["data"]["mode"] == "replace"
    assert log == ["ensure", "send"]


def test_user_block_append_mode_no_delete(client, monkeypatch):
    # mode=append：保持 ensure→send，不触发 delete
    log = []

    def fake_delete(self, block_code):
        log.append("delete")
        return {"ErrorId": "0"}

    def fake_ensure(self, block_code, block_name):
        log.append("ensure")
        return {"ErrorId": "0"}

    def fake_send(self, block_code, stock_codes, show=False):
        log.append("send")
        return {"ErrorId": "0"}

    _patch_data(monkeypatch, "delete_sector", fake_delete)
    _patch_data(monkeypatch, "ensure_sector", fake_ensure)
    _patch_data(monkeypatch, "send_user_block", fake_send)
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"],
        "mode": "append"}).json()
    assert body["code"] == 0
    assert body["data"] == {"block_code": "MYBK", "count": 1, "mode": "append"}
    assert log == ["ensure", "send"]


def test_user_block_invalid_mode_400(client, monkeypatch):
    calls = []
    _patch_data(monkeypatch, "delete_sector",
                lambda self, c: calls.append("delete") or {"ErrorId": "0"})
    _patch_data(monkeypatch, "ensure_sector",
                lambda self, c, n: calls.append("ensure") or {"ErrorId": "0"})
    body = client.post("/api/tdx/user-block", json={
        "block_code": "MYBK", "stock_codes": ["000001.SZ"],
        "mode": "upsert"}).json()
    assert body["code"] == 400
    assert "mode" in body["message"]
    assert calls == []


# ---------------------------------------------------------------------------
# GET /api/tdx/divid-factors — 除权除息
# ---------------------------------------------------------------------------

def _divid_df():
    """按 tqcenter.get_divid_factors 实际形状构造：Date DatetimeIndex + 五列。"""
    return pd.DataFrame(
        {"Type": [1, 2],
         "Bonus": [0.5, float("nan")],
         "AllotPrice": [0.0, 3.5],
         "ShareBonus": [0.0, 0.0],
         "Allotment": [0.0, 1.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2023-06-08", "2024-06-14"]),
                               name="Date"),
    )


def test_divid_factors_serialization(client, monkeypatch):
    calls = {}

    def fake(self, stock_code, start="", end=""):
        calls.update(stock_code=stock_code, start=start, end=end)
        return _divid_df()

    _patch_data(monkeypatch, "get_divid_factors", fake)
    resp = client.get("/api/tdx/divid-factors", params={
        "stock_code": "000001.SZ", "start": "20200101", "end": "20240101"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    events = body["data"]["events"]
    assert len(events) == 2
    assert events[0] == {"Date": "2023-06-08T00:00:00", "Type": 1, "Bonus": 0.5,
                         "AllotPrice": 0.0, "ShareBonus": 0.0, "Allotment": 0.0}
    # NaN 不可进 JSON（Starlette allow_nan=False）→ 序列化为 null
    assert events[1]["Bonus"] is None
    assert events[1]["AllotPrice"] == 3.5
    assert calls == {"stock_code": "000001.SZ", "start": "20200101",
                     "end": "20240101"}


def test_divid_factors_missing_stock_code_400(client, monkeypatch):
    def fake(self, stock_code, start="", end=""):
        raise AssertionError("不应触发 SDK")

    _patch_data(monkeypatch, "get_divid_factors", fake)
    body = client.get("/api/tdx/divid-factors").json()
    assert body["code"] == 400


def test_divid_factors_connection_error_503(client, monkeypatch):
    def fake(self, stock_code, start="", end=""):
        raise TDXConnectionError("通达信未启动")

    _patch_data(monkeypatch, "get_divid_factors", fake)
    body = client.get("/api/tdx/divid-factors",
                      params={"stock_code": "000001.SZ"}).json()
    assert body["code"] == 503
    assert body["message"] == "通达信未启动"


# ---------------------------------------------------------------------------
# GET /api/tdx/trading-dates — 交易日历
# ---------------------------------------------------------------------------

def test_trading_dates_passthrough(client, monkeypatch):
    calls = {}

    def fake(self, start="", end=""):
        calls.update(start=start, end=end)
        return ["20240102", "20240103"]

    _patch_data(monkeypatch, "get_trading_dates", fake)
    resp = client.get("/api/tdx/trading-dates", params={
        "start": "20240101", "end": "20240131"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"dates": ["20240102", "20240103"]}
    assert calls == {"start": "20240101", "end": "20240131"}


@pytest.mark.parametrize("params", [{}, {"start": "20240101"}, {"end": "20240131"}])
def test_trading_dates_missing_params_400(client, monkeypatch, params):
    def fake(self, start="", end=""):
        raise AssertionError("不应触发 SDK")

    _patch_data(monkeypatch, "get_trading_dates", fake)
    body = client.get("/api/tdx/trading-dates", params=params).json()
    assert body["code"] == 400


def test_trading_dates_connection_error_503(client, monkeypatch):
    def fake(self, start="", end=""):
        raise TDXConnectionError("通达信未启动")

    _patch_data(monkeypatch, "get_trading_dates", fake)
    body = client.get("/api/tdx/trading-dates", params={
        "start": "20240101", "end": "20240131"}).json()
    assert body["code"] == 503
    assert body["message"] == "通达信未启动"
