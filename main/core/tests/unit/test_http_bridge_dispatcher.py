"""HttpBridgeDispatcher 单测（0009 切片2）— httpx MockTransport 模拟 QMT 桥。

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
    BridgeOrderRejected,
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
def _confirmed_responder(ref="cl-95514a12", status="filled", filled=100):
    """桥 ok:true 带 cl_ord_id + /orders 回读到该委托（默认全部成交）。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True, "cl_ord_id": ref})
        if request.url.path == "/orders":
            return httpx.Response(200, json={"ok": True, "data": [{
                "cl_ord_id": ref, "status": status, "filled_volume": filled,
            }]})
        return httpx.Response(404)
    return respond


def test_place_order_maps_to_http():
    disp, rec = _make_dispatcher(_Recorder(respond=_confirmed_responder()))
    order = _make_order()
    trade = disp.place_order(order)

    order_req = [r for r in rec.requests if r.url.path == "/order"][-1]
    assert order_req.method == "POST"
    payload = json.loads(order_req.content)
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


def test_place_order_sends_prtype_14():
    """0009 切片4：place_order payload 含 pr_type=14（对手价）。"""
    disp, rec = _make_dispatcher(_Recorder())
    disp.place_order(_make_order())
    assert _last_json(rec)["pr_type"] == 14


def test_place_order_sends_remark_oid_prefix():
    """remark = order_id(order) 前 20 位，桥作为 passorder userOrderId 写 m_strRemark。

    这是 Core↔桥订单精确匹配的根基：回填时 Core 按 remark 全局唯一认领本单，
    避免 代码+方向+数量 模糊匹配撞到跨会话遗留的同代码同向同量旧单。
    """
    disp, rec = _make_dispatcher(_Recorder())
    order = _make_order()
    disp.place_order(order)
    oid = HttpBridgeDispatcher.order_id(order)
    assert len(oid) == 32
    payload = _last_json(rec)
    assert payload["remark"] == oid[:20]
    assert len(payload["remark"]) == 20


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
    assert disp.last_probe_error is None


def test_heartbeat_probe_error_refused():
    """连接被拒 = 端口无监听（启动闸据此给「桥进程未启动」）。"""
    disp, _ = _make_dispatcher(_Recorder(fail=True))
    assert disp.heartbeat() is False
    assert disp.last_probe_error == "refused"


def test_heartbeat_probe_error_timeout():
    """端口可连但超时 = 已监听但无响应（常见于桥卡在 SDK 查询）。"""
    def handler(request):
        raise httpx.ReadTimeout("timed out")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:7801", client=client)
    assert disp.heartbeat() is False
    assert disp.last_probe_error == "timeout"


def test_heartbeat_probe_error_protocol_on_non_200():
    def handler(request):
        return httpx.Response(404, json={"ok": False})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:7801", client=client)
    assert disp.heartbeat() is False
    assert disp.last_probe_error == "protocol"


def test_heartbeat_probe_error_protocol_on_non_json():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:7801", client=client)
    assert disp.heartbeat() is False
    assert disp.last_probe_error == "protocol"


# ---------------- 下单超时/连接中断 → 在途（不得判拒单）----------------
def test_place_order_read_timeout_returns_none_as_inflight(caplog):
    """桥响应慢导致 /order 读超时（请求已发出）→ 返回 None（在途），不判拒单。

    原行为：任何异常都抛 BridgeUnavailableError → 引擎标 rejected 并移出在途集合，
    真实已发出的委托从此无 ref、无成交回填、无法撤（虚拟账本与柜台脱节）。
    修正：超时/连接中断按「已发出未确认」处理，引擎保持 submitted 等 /orders 认领。
    """
    import logging

    def handler(request):
        if request.url.path == "/order":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:7801", client=client)
    order = _make_order()
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        trade = disp.place_order(order)

    assert trade is None
    oid = HttpBridgeDispatcher.order_id(order)
    assert disp.last_unconfirmed_order_id == oid
    assert "在途" in (disp.last_unconfirmed_message or "")
    assert any("in-flight" in r.message for r in caplog.records), \
        "超时转在途必须留可回读线索（warning）"


def test_place_order_connection_interrupted_returns_none_as_inflight():
    """连接中断（对端未发完整响应就断开）→ 同样按在途返回 None，不判拒单。"""
    def handler(request):
        if request.url.path == "/order":
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    disp = HttpBridgeDispatcher(base_url="http://127.0.0.1:7801", client=client)
    order = _make_order()

    assert disp.place_order(order) is None
    assert disp.last_unconfirmed_order_id == HttpBridgeDispatcher.order_id(order)
    assert "在途" in (disp.last_unconfirmed_message or "")


def test_place_order_ok_true_confirmed_filled_returns_trade():
    """(a) ok:true + 带 cl_ord_id + /orders 回读到 filled → 才可构造成交回报。"""
    disp, _ = _make_dispatcher(_Recorder(respond=_confirmed_responder()))
    trade = disp.place_order(_make_order())
    assert isinstance(trade, TradeEvent)
    assert trade.amount == Decimal("9.3") * 100
    assert disp.last_unconfirmed_order_id is None


def test_place_order_ok_true_without_ref_stays_unconfirmed(caplog):
    """(b) ok:true 但不含可识别委托标识 → 绝不成交，只能待确认（返回 None）。

    掘金桥对柜台拒单也回 {"ok":true}（如价格超涨跌停），且拒单不回显到
    /orders、/deals；若直接构造成交即伪造持仓/现金。无标识时降级为在途，
    日志带原文片段（bridge_order_unconfirmed）。
    """
    import logging

    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True,
                                             "result": "委托被拒绝: 委托价超出涨跌停范围"})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    order = _make_order()
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        trade = disp.place_order(order)

    assert trade is None
    assert disp.last_unconfirmed_order_id == HttpBridgeDispatcher.order_id(order)
    assert "在途" in (disp.last_unconfirmed_message or "")
    assert "涨跌停" in (disp.last_unconfirmed_message or ""), "线索须带应答原文片段"
    assert any("bridge_order_unconfirmed" in r.message for r in caplog.records)


def test_place_order_ok_true_ref_not_in_orders_stays_unconfirmed():
    """(b) ok:true + ref，但 /orders 回读不到该委托 → 待确认，不成交。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True, "cl_ord_id": "cl-1"})
        if request.url.path == "/orders":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    trade = disp.place_order(_make_order())
    assert trade is None
    assert disp.last_unconfirmed_order_id
    assert "未回读" in (disp.last_unconfirmed_message or "")


def test_place_order_ok_true_ref_pending_stays_unconfirmed():
    """(b) ok:true + ref，回读到但状态未成交（挂单/未知状态码）→ 待确认，不成交。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True, "cl_ord_id": "cl-1"})
        if request.url.path == "/orders":
            return httpx.Response(200, json={"ok": True, "data": [{
                "cl_ord_id": "cl-1", "status": 11, "filled_volume": 0,
            }]})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    trade = disp.place_order(_make_order())
    assert trade is None
    assert disp.last_unconfirmed_order_id
    assert "未确认全部成交" in (disp.last_unconfirmed_message or "")


def test_place_order_ok_true_query_failure_stays_unconfirmed():
    """(b) ok:true + ref，但 /orders 回查超时 → 待确认（不判拒单、不成交）。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": True, "cl_ord_id": "cl-1"})
        if request.url.path == "/orders":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    trade = disp.place_order(_make_order())
    assert trade is None
    assert disp.last_unconfirmed_order_id
    assert "回查失败" in (disp.last_unconfirmed_message or "")


def test_place_order_business_error_still_rejected():
    """明确收到桥的拒绝应答（{ok:false,error}）→ 仍判拒单（BridgeOrderRejected）。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": False, "error": "重复委托"})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with pytest.raises(BridgeOrderRejected, match="重复委托"):
        disp.place_order(_make_order())


def test_place_order_connect_error_still_unavailable():
    """连接被拒（请求从未发出）→ 仍是桥不可用，不按在途处理。"""
    disp, _ = _make_dispatcher(_Recorder(fail=True))
    with pytest.raises(BridgeUnavailableError):
        disp.place_order(_make_order())
    assert disp.last_unconfirmed_order_id is None


# ---------------- 桥返回 ok:false 但属于连接层瞬时故障 → 在途（不判拒单）--------
def _ok_false_responder(**body):
    """桥 HTTP 200 + ok:false（可带 error/data/result）。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json=dict(body))
        return httpx.Response(404)
    return respond


@pytest.mark.parametrize("error_text", [
    "order_volume raised: SdkError: 交易服务未连接",
    "order_volume returned empty list (SDK/网关未受理，无 cl_ord_id)",
    "[WinError 10061] 由于目标计算机积极拒绝，无法连接。",
    "HTTPConnectionPool(host='127.0.0.1', port=7801): Read timed out.",
    "query_orders: 交易服务不可用",
])
def test_place_order_transient_bridge_error_returns_none_as_inflight(caplog, error_text):
    """桥返回 ok:false，但错误文案属于连接层瞬时故障 → 返回 None（在途），不判拒单。

    掘金桥在 gm 交易服务断连窗口内会回 ok:false + "order_volume raised: ..." /
    "returned empty list"；这是连接层故障而非柜台明确拒单，判 rejected 会让真实
    委托脱跟踪（无 ref、无成交回填、无法撤）。正确语义是保持 submitted，由 180s
    超窗 + grace 兜底。
    """
    import logging

    disp, _ = _make_dispatcher(_Recorder(respond=_ok_false_responder(
        ok=False, error=error_text,
        data={"cl_ord_id": None, "accepted": False, "rejected": False,
              "status": "Error", "reason": error_text},
    )))
    order = _make_order()
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        trade = disp.place_order(order)

    assert trade is None, "瞬时故障不得构造成交回报"
    assert disp.last_unconfirmed_order_id == HttpBridgeDispatcher.order_id(order)
    assert disp.last_order_rejection is None, "瞬时故障不得留下拒单原文"
    assert "在途" in (disp.last_unconfirmed_message or "")
    assert any("bridge_order_transient" in r.message for r in caplog.records), \
        "瞬时故障转在途必须有 warning 级线索"


def test_place_order_transient_warning_is_throttled(caplog):
    """同类瞬时故障告警按 60s 窗口节流，避免断连窗口内逐单刷屏。"""
    import logging

    clock = _Clock()
    disp, _ = _make_dispatcher(_Recorder(respond=_ok_false_responder(
        ok=False, error="order_volume raised: 交易服务未连接",
    )))
    disp._now = clock
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        disp.place_order(_make_order(quantity=100))   # t=0 → 首报
        disp.place_order(_make_order(quantity=200))   # t=0 → 窗口内，抑制
        clock.t = 61.0
        disp.place_order(_make_order(quantity=300))   # t=61 → 再报

    warns = [r for r in caplog.records if "bridge_order_transient" in r.message]
    assert len(warns) == 2


def test_place_order_broker_rejected_flag_still_rejected():
    """柜台明确拒单（data.rejected=true + reason）→ 仍抛 BridgeOrderRejected。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={
                "ok": False,
                "error": "委托价超出涨跌停范围",
                "data": {"cl_ord_id": "cl-1", "accepted": False, "rejected": True,
                         "status": "Rejected", "reason": "委托价超出涨跌停范围"},
            })
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with pytest.raises(BridgeOrderRejected, match="涨跌停"):
        disp.place_order(_make_order())
    assert disp.last_unconfirmed_order_id is None
    assert disp.last_order_rejection == "委托价超出涨跌停范围"


@pytest.mark.parametrize("error_text", [
    "委托价超出涨跌停范围",
    "可用资金不足",
    "委托数量非法",
    "order_volume raised: 委托数量必须为 100 的整数倍",
])
def test_place_order_business_rejection_text_still_rejected(error_text):
    """业务校验类文案（即使包在 order_volume raised 里）→ 仍是明确拒单。"""
    disp, _ = _make_dispatcher(_Recorder(
        respond=_ok_false_responder(ok=False, error=error_text)))
    with pytest.raises(BridgeOrderRejected):
        disp.place_order(_make_order())


def test_place_order_unknown_error_conservative_rejected():
    """未知错误文案 → 保守判「明确拒单」（宁可人工确认，不挂成在途 10 分钟）。"""
    disp, _ = _make_dispatcher(_Recorder(
        respond=_ok_false_responder(ok=False, error="bridge: something odd happened")))
    with pytest.raises(BridgeOrderRejected, match="something odd"):
        disp.place_order(_make_order())
    assert disp.last_unconfirmed_order_id is None


@pytest.mark.parametrize("error_text,payload,expected", [
    # 桥两条实测瞬时故障文案
    ("order_volume raised: SdkError: not connected", {}, "transient"),
    ("order_volume returned empty list (SDK/网关未受理，无 cl_ord_id)", {}, "transient"),
    # 桥内部查询失败
    ("query_positions: 交易服务断开", {}, "transient"),
    ("query_orders: timed out", {}, "transient"),
    # 连接层
    ("[WinError 10061] 由于目标计算机积极拒绝，无法连接。", {}, "transient"),
    ("[WinError 10054] 远程主机强迫关闭了一个现有的连接。", {}, "transient"),
    ("HTTPSConnectionPool: Read timed out.", {}, "transient"),
    ("Connection refused", {}, "transient"),
    # 柜台明确拒单
    ("委托价超出涨跌停范围", {}, "rejected"),
    ("可用资金不足", {}, "rejected"),
    ("", {"rejected": True, "reason": "废单"}, "rejected"),
    ("", {"data": {"rejected": True, "reason": "涨跌停"}}, "rejected"),
    ("重复委托", {}, "rejected"),
    # 未知 → 保守拒单
    ("bridge: something odd happened", {}, "rejected"),
    ("", {}, "rejected"),
])
def test_classify_bridge_error_table(error_text, payload, expected):
    """分类纯函数表驱动：模式 → rejected / transient。"""
    assert HttpBridgeDispatcher._classify_bridge_error(error_text, payload) == expected


# ---------------- 慢响应告警（阈值 + endpoint 去重）----------------
class _Clock:
    """可控单调时钟：handler 内推进，模拟桥单次调用耗时。"""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _slow_dispatcher(clock, advance=20.0, **kwargs):
    def handler(request):
        clock.t += advance
        path = request.url.path
        if path in ("/positions", "/account", "/orders", "/deals"):
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpBridgeDispatcher(
        base_url="http://127.0.0.1:7801", client=client, now_fn=clock,
        slow_threshold=15.0, **kwargs,
    )


def test_slow_response_warns_with_endpoint_and_elapsed(caplog):
    """单次调用耗时超慢阈值 → WARNING 带 endpoint + 耗时（非 errors）。"""
    import logging

    clock = _Clock()
    disp = _slow_dispatcher(clock)
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        disp.query_positions()

    slow = [r for r in caplog.records if "slow" in r.message]
    assert len(slow) == 1
    assert "/positions" in slow[0].message
    assert "20.00s" in slow[0].message or "20.0" in slow[0].message
    assert disp.last_slow[0] == "/positions"


def test_slow_response_warning_deduped_per_window(caplog):
    """慢告警按会话窗口去重：窗口内不重复刷（端点可不同，也合并成一条）。"""
    import logging

    clock = _Clock()
    disp = _slow_dispatcher(clock, slow_warn_interval=300.0)
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        disp.query_positions()      # t=20 → 首报
        disp.query_positions()      # t=40 → 窗口内，抑制
        clock.t = 300.0
        disp.query_positions()      # t=320 → 窗口期满，再报一次

    slow = [r for r in caplog.records if "slow" in r.message]
    assert len(slow) == 2


def test_slow_window_merges_different_endpoints_into_one_event():
    """同一窗口内 4 个不同端点各慢一次 → 只回调 1 条（首条及时，其余累计计数）。"""
    clock = _Clock()
    costs = {"/ping": 16.0, "/account": 19.3, "/positions": 17.0, "/orders": 18.0}

    def handler(request):
        clock.t += costs[request.url.path]
        return httpx.Response(200, json={"ok": True, "data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    calls = []
    disp = HttpBridgeDispatcher(
        base_url="http://127.0.0.1:7801", client=client, now_fn=clock,
        slow_threshold=15.0, slow_warn_interval=300.0,
        on_slow=lambda e, s, summary: calls.append((e, s, summary)),
    )
    for path in ("/ping", "/account", "/positions", "/orders"):
        disp._do_request("GET", path)

    assert len(calls) == 1, "同一窗口内不得按端点各报一条"
    assert calls[0][0] == "/ping"
    assert calls[0][2] is None  # 首条无上一窗口汇总
    assert disp._slow_window_count == 3  # 窗口内后续 3 次已累计（首条已单独上报）


def test_slow_window_summary_carried_to_next_event():
    """窗口过期后的首条慢请求带上上一窗口汇总：N 次 + 最慢端点/耗时。"""
    clock = _Clock()
    costs = {"/ping": 16.0, "/account": 19.3, "/positions": 17.0, "/orders": 18.0}

    def handler(request):
        clock.t += costs[request.url.path]
        return httpx.Response(200, json={"ok": True, "data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    calls = []
    disp = HttpBridgeDispatcher(
        base_url="http://127.0.0.1:7801", client=client, now_fn=clock,
        slow_threshold=15.0, slow_warn_interval=300.0,
        on_slow=lambda e, s, summary: calls.append((e, s, summary)),
    )
    for path in ("/ping", "/account", "/positions", "/orders"):
        disp._do_request("GET", path)

    clock.t = 400.0  # 跨过 300s 窗口
    disp._do_request("GET", "/positions")

    assert len(calls) == 2
    summary = calls[1][2]
    assert summary == {
        "count": 3,
        "slowest_endpoint": "/account",
        "slowest_elapsed_seconds": 19.3,
        "window_seconds": 300.0,
    }


def test_slow_response_invokes_on_slow_callback():
    """超慢阈值时的回调（引擎据此发 risk 事件）；窗口内只回调一次。"""
    clock = _Clock()
    calls = []
    disp = _slow_dispatcher(
        clock, slow_warn_interval=300.0,
        on_slow=lambda endpoint, elapsed, summary: calls.append((endpoint, elapsed))
    )
    disp.query_positions()
    disp.query_positions()
    assert calls == [("/positions", 20.0)]


def test_fast_response_no_slow_alert(caplog):
    """未超阈值（含默认配置下的正常响应）→ 不告警、不回调。"""
    import logging

    clock = _Clock()
    calls = []
    disp = _slow_dispatcher(clock, advance=5.0,
                            on_slow=lambda e, s, summary: calls.append((e, s)))
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        disp.query_positions()
    assert not [r for r in caplog.records if "slow" in r.message]
    assert calls == []


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


# ---------------- #24：桥返回非 JSON 不静默吞 ----------------
# ---------------- #4：桥业务拒单回显 ----------------
def test_place_order_bridge_rejection_raises_with_error():
    """#4：桥 {ok:false, error:...}（白名单/限额/重复）→ 抛 BridgeOrderRejected 回显原因。

    原行为：返回 None 被上层当笼统拒单，真实原因（如 volume 超限）丢在桥里查不到。
    """
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={
                "ok": False, "error": "volume 33900 exceeds max 100000",
            })
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with pytest.raises(BridgeOrderRejected, match="volume 33900 exceeds max 100000"):
        disp.place_order(_make_order())


def test_place_order_ok_false_no_error_returns_none():
    """桥 {ok:false} 但无 error（异常路径）→ 维持返回 None（#24 非 JSON 语义，不当业务拒单）。"""
    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(200, json={"ok": False})
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    assert disp.place_order(_make_order()) is None


def test_place_order_non_json_body_warns_and_returns_none(caplog):
    """#24：桥 /order 返回非 JSON（如 HTML 错误页/坏网关）→ 告警 + 返回 None。

    原行为：r.json() 失败静默 data={} → data.get("ok") None → return None，
    被当业务拒绝（券商拒单），真实桥故障无痕迹。加 warning 让"非 JSON"可见。
    """
    import logging

    def respond(request):
        if request.url.path == "/order":
            return httpx.Response(
                200, text="<html>502 Bad Gateway</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        trade = disp.place_order(_make_order())
    assert trade is None
    assert any("non-JSON" in r.message for r in caplog.records), \
        "桥返回非 JSON 应触发 warning，不应静默"


def test_query_positions_non_json_body_warns_and_returns_empty(caplog):
    """#24：/positions 返回非 JSON → 告警 + 返回 []（不当无持仓）。

    原行为：返回 [] 被上层当"无持仓"，掩盖桥故障。加 warning 可见。
    """
    import logging

    def respond(request):
        if request.url.path == "/positions":
            return httpx.Response(
                200, text="<html>error</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        rows = disp.query_positions()
    assert rows == []
    assert any("non-JSON" in r.message for r in caplog.records), \
        "桥返回非 JSON 应触发 warning，不应静默"


def test_query_quote_non_json_body_warns_and_returns_empty(caplog):
    """#24：/quote 返回非 JSON → 告警 + 返回 []（不当无行情）。

    原行为：返回 [] 让 BarPoller 拉不到 bar 静默不触发。加 warning 可见。
    """
    import logging

    def respond(request):
        if request.url.path == "/quote":
            return httpx.Response(
                200, text="<html>504 Gateway Timeout</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    disp, _ = _make_dispatcher(_Recorder(respond=respond))
    with caplog.at_level(logging.WARNING, logger="core.engine.http_bridge_dispatcher"):
        rows = disp.query_quote("600000.SH")
    assert rows == []
    assert any("non-JSON" in r.message for r in caplog.records), \
        "桥返回非 JSON 应触发 warning，不应静默"
