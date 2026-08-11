"""桥正式版单测（0009 切片1）— Mock iQuant 内置函数，纯逻辑测试。

桥 live/bridge/iquant_bridge.py 是 iQuant 策略，iQuant API（passorder / get_trade_detail_data /
get_market_data_ex / download_history_data）在运行时注入策略命名空间（globals()）。测试通过
`br.<api> = fake` 注入 Mock，直接调 _handle/place_order/get_quote 等纯逻辑函数。
"""
import json
import os
import sys
import time

import pytest

# 把 live/bridge 加入 path 以 import 桥模块
BRIDGE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "live", "bridge")
)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import iquant_bridge as br  # noqa: E402


def _resp(method, path, headers=None, body=b""):
    """调桥的 _handle，返回 (status, parsed_json)。"""
    raw = br._handle(method, path, headers or {}, body)
    # 解析 HTTP 响应：取 status + JSON body
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    data = json.loads(payload.decode("utf-8"))
    return status, data


@pytest.fixture(autouse=True)
def _reset_state():
    """每个测试前重置全局状态。"""
    br.ALLOWED_STOCKS = set()
    br.MAX_VOLUME = 10000
    br.RATE_LIMIT = 1000
    br.RATE_WINDOW = 10
    br._placed.clear()
    br._placing.clear()
    br._requests.clear()
    br._quote_cache.clear()
    # 清掉可能注入的 fake（从模块命名空间移除，回到 _iq 找不到）
    for name in ("passorder", "get_trade_detail_data", "get_market_data_ex", "download_history_data"):
        br.__dict__.pop(name, None)
    yield


# ---------------- /ping ----------------
def test_ping():
    status, data = _resp("GET", "/ping")
    assert status == 200
    assert data["ok"] is True
    assert data["service"] == "iquant-bridge"


def test_unknown_path_404():
    status, data = _resp("GET", "/nope")
    assert status == 404
    assert data["ok"] is False


# ---------------- /order ----------------
def test_order_dry_run_does_not_call_passorder():
    br.DRY_RUN = True
    br.passorder = lambda *a, **k: pytest.fail("DRY_RUN 不应真调 passorder")
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True
    assert data["dry_run"] is True


def test_order_real_calls_passorder():
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True
    assert data["passorder_result"] == "0"
    assert len(calls) == 1
    # 前 5 参语义：opType=23(买), orderType=1101, account, code, prType int
    assert calls[0][0] == 23
    assert calls[0][2] == br.ACCOUNT
    assert calls[0][3] == "600000.SH"
    assert isinstance(calls[0][4], int)


def test_do_place_uses_prtype_14_and_ordertype_1101():
    """0009 切片4：_do_place 固定 prType=14(对手价) + orderType=1101(单股标准)。"""
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    _resp("POST", "/order", {}, body)
    assert calls[0][1] == 1101            # orderType=1101 单股/单账号/普通/按股数
    assert calls[0][4] == 14              # prType=14 对手价(对方一档)
    # SELL 同样用 14/1101
    calls.clear()
    body = json.dumps({"order_id": "oid2", "code": "600000.SH", "op": "sell",
                       "volume": 100, "price": 0}).encode()
    _resp("POST", "/order", {}, body)
    assert calls[0][0] == 24              # opType=24 (sell)
    assert calls[0][1] == 1101
    assert calls[0][4] == 14


def test_dry_run_returns_pr_type():
    """DRY_RUN 返回 params 含 pr_type=14，便于观测/断言。"""
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["params"]["pr_type"] == 14


def test_order_missing_code():
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "op": "buy", "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is False
    assert "code" in data["error"]


# ---------------- 幂等 ----------------
def test_order_idempotent():
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "same", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    st1, d1 = _resp("POST", "/order", {}, body)
    st2, d2 = _resp("POST", "/order", {}, body)     # 同 order_id 重复
    assert st1 == st2 == 200
    assert d1 == d2
    assert len(calls) == 1                          # passorder 只调一次


# ---------------- 白名单 / 限额 ----------------
def test_whitelist_rejects_other_stock():
    br.ALLOWED_STOCKS = {"600000.SH"}
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "000001.SZ", "op": "buy",
                       "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is False
    assert "not allowed" in data["error"]


def test_whitelist_allows_listed_stock():
    br.ALLOWED_STOCKS = {"600000.SH"}
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True


def test_max_volume_rejects():
    br.MAX_VOLUME = 10000
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 20000}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["ok"] is False
    assert "volume" in data["error"]


def test_rate_limit_rejects_excess():
    br.RATE_LIMIT = 2
    br.RATE_WINDOW = 10
    br.DRY_RUN = True
    for i in range(2):
        body = json.dumps({"order_id": "oid%d" % i, "code": "600000.SH",
                           "op": "buy", "volume": 100}).encode()
        status, data = _resp("POST", "/order", {}, body)
        assert data["ok"] is True
    # 第三个超限
    body = json.dumps({"order_id": "oid3", "code": "600000.SH",
                       "op": "buy", "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["ok"] is False
    assert "rate" in data["error"]


# ---------------- 查询端点 ----------------
def test_positions_calls_trade_detail():
    class FakePos:
        m_strInstrumentID = "600000.SH"
        m_nVolume = 1000
    br.get_trade_detail_data = lambda acc, typ, dt: [FakePos()]
    status, data = _resp("GET", "/positions", {}, b"")
    assert status == 200
    assert data["ok"] is True
    assert data["data"][0]["instrument"] == "600000.SH"


# ---------------- /quote 行情缓存 ----------------
def _make_fake_df():
    import pandas as pd
    return pd.DataFrame(
        {
            "open": [9.28, 9.29],
            "high": [9.29, 9.31],
            "low": [9.28, 9.30],
            "close": [9.29, 9.31],
            "volume": [3700, 727],
            "time": [1785895560000, 1785895680000],
        },
        index=pd.Index(["20260805100600", "20260805100800"], name="stime"),
    )


def test_quote_fetches_and_returns_bars():
    calls = []
    br.get_market_data_ex = lambda *a, **k: calls.append(k) or {"600000.SH": _make_fake_df()}
    status, data = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert status == 200
    assert data["ok"] is True
    bars = data["data"]["600000.SH"]
    assert isinstance(bars, list) and len(bars) == 2
    assert bars[-1]["close"] == 9.31
    assert bars[-1]["stime"] == "20260805100800"
    assert calls[0]["period"] == "1m"


def test_quote_cache_hit_within_ttl():
    br.QUOTE_CACHE_TTL = 100          # 拉大 TTL，保证两次调用在窗口内，避免跨秒 flaky
    calls = []
    br.get_market_data_ex = lambda *a, **k: calls.append(1) or {"600000.SH": _make_fake_df()}
    # 第一次拉取
    st, d1 = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert len(calls) == 1
    # TTL 内第二次 → 读缓存，不再调 get_market_data_ex
    st, d2 = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert d1["data"] == d2["data"]      # bar 数据一致
    assert d2["cached"] is True          # 第二次命中缓存
    assert len(calls) == 1               # get_market_data_ex 只调一次
