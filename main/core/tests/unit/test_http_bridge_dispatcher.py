"""HttpBridgeDispatcher 单测（0009 切片2）— httpx MockTransport 模拟 iQuant 桥。

验证 Core 侧 dispatcher 与桥的 HTTP 映射正确：place_order→POST /order、
query_positions→GET /positions、桥离线抛 BridgeUnavailableError、幂等 order_id 透传。
"""
import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from core.engine.event import OrderEvent, TradeEvent
from core.engine.execution_engine import OrderDispatcher
from core.engine.http_bridge_dispatcher import (
    BridgeUnavailableError,
    HttpBridgeDispatcher,
)
from tq_iquant_shared.constants import SignalType, TradeType


def _make_order(**kw):
    defaults = dict(
        strategy_id=1,
        portfolio_id=1,
        stock_code="600000.SH",
        trade_type=TradeType.BUY,
        signal_type=SignalType.OPEN,
        quantity=100,
        price=Decimal("9.3"),
        bar_time=datetime(2026, 8, 5, 10, 0),
        signal_name="open_sig",
    )
    defaults.update(kw)
    return OrderEvent(**defaults)


class _Recorder:
    """MockTransport 辅助：记录每个请求 + 可选抛错。"""

    def __init__(self, respond=None, fail=False):
        self.requests = []
        self._respond = respond
        self._fail = fail

    def handler(self, request):
        self.requests.append(request)
        if self._fail:
            raise httpx.ConnectError("connection refused")
        if self._respond is not None:
            return self._respond(request)
        path = request.url.path
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        if path in ("/positions", "/account", "/orders", "/deals"):
            return httpx.Response(200, json={"ok": True, "data": []})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": "unknown"})


def _make_dispatcher(rec):
    client = httpx.Client(transport=httpx.MockTransport(rec.handler))
    return HttpBridgeDispatcher(base_url="http://127.0.0.1:8790", client=client), rec


def _last_json(rec, i=-1):
    return json.loads(rec.requests[i].content)


# ---------------- place_order 映射 ----------------
def test_place_order_maps_to_http():
    disp, rec = _make_dispatcher(_Recorder())
    order = _make_order()
    trade = disp.place_order(order)

    assert rec.requests[-1].method == "POST"
    assert rec.requests[-1].url.path == "/order"
    payload = _last_json(rec)
    assert payload["code"] == "600000.SH"
    assert payload["op"] == "buy"
    assert payload["volume"] == 100
    assert payload["price"] == 9.3
    assert isinstance(trade, TradeEvent)
    assert trade.strategy_id == 1
    assert trade.stock_code == "600000.SH"
    assert trade.quantity == 100
    assert trade.signal_name == "open_sig"


def test_place_order_sell_op():
    disp, rec = _make_dispatcher(_Recorder())
    order = _make_order(trade_type=TradeType.SELL)
    disp.place_order(order)
    assert _last_json(rec)["op"] == "sell"


def test_place_order_price_none_sends_zero():
    disp, rec = _make_dispatcher(_Recorder())
    order = _make_order(price=None)
    disp.place_order(order)
    assert _last_json(rec)["price"] == 0


# ---------------- 查询映射 ----------------
def test_query_positions_maps_to_http():
    disp, rec = _make_dispatcher(_Recorder())
    rows = disp.query_positions()
    assert rec.requests[-1].method == "GET"
    assert rec.requests[-1].url.path == "/positions"
    assert rows == []


def test_query_account_maps_to_http():
    disp, rec = _make_dispatcher(_Recorder())
    disp.query_account()
    assert rec.requests[-1].url.path == "/account"


def test_query_orders_maps_to_http():
    disp, rec = _make_dispatcher(_Recorder())
    disp.query_orders()
    assert rec.requests[-1].url.path == "/orders"


def test_query_quote_maps_to_http():
    """0009 切片3：query_quote → GET /quote?code=&period=&count=，返回该 code 的 bar 列表。"""
    bars = [
        {"stime": "20260805100800", "open": 9.30, "high": 9.35, "low": 9.28, "close": 9.32, "volume": 12000},
        {"stime": "20260805100900", "open": 9.32, "high": 9.40, "low": 9.31, "close": 9.38, "volume": 8000},
    ]

    def respond(request):
        if request.url.path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {"600000.SH": bars}})
        return httpx.Response(404)

    disp, rec = _make_dispatcher(_Recorder(respond=respond))
    rows = disp.query_quote("600000.SH", period="1m", count=10)
    assert rec.requests[-1].url.path == "/quote"
    assert "code=600000.SH" in str(rec.requests[-1].url)
    assert "period=1m" in str(rec.requests[-1].url)
    assert rows == bars


def test_query_quote_empty_when_no_data():
    def respond(request):
        if request.url.path == "/quote":
            return httpx.Response(200, json={"ok": False, "error": "no data"})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    assert disp.query_quote("600000.SH") == []


# ---------------- 桥离线 ----------------
def test_bridge_offline_raises_on_place():
    disp, _ = _make_dispatcher(_Recorder(fail=True))
    with pytest.raises(BridgeUnavailableError):
        disp.place_order(_make_order())


def test_bridge_offline_heartbeat_false():
    disp, _ = _make_dispatcher(_Recorder(fail=True))
    assert disp.heartbeat() is False


def test_heartbeat_true_when_online():
    disp, _ = _make_dispatcher(_Recorder())
    assert disp.heartbeat() is True


# ---------------- 幂等 order_id 透传 ----------------
def test_idempotent_order_id_passthrough():
    disp, rec = _make_dispatcher(_Recorder())
    order = _make_order()
    disp.place_order(order)
    disp.place_order(order)                       # 同订单重试
    assert len(rec.requests) == 2
    assert _last_json(rec, -1)["order_id"] == _last_json(rec, -2)["order_id"]
    # 不同订单 → 不同 order_id
    disp.place_order(_make_order(quantity=200))
    assert len(rec.requests) == 3
    assert _last_json(rec, -1)["order_id"] != _last_json(rec, -2)["order_id"]
    # order_id 是确定性 hash 串
    assert len(_last_json(rec)["order_id"]) == 32
