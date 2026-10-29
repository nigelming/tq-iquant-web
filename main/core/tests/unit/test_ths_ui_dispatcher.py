"""ThsUiDispatcher 纯映射逻辑测试（不启动同花顺 GUI）。

覆盖 Core 契约：/orders 形状（source=BRIDGE、remark 空、order_ref=合同编号、
整型状态码）、/deals 由委托表成交数量/均价合成、TQ 行情/日历转发、持仓 available=0
不被回退成持仓量；file_builtin 双模式：place_order 秒回入队不触 UI、内置执行器
消费指令（成功/失败回执）、direct 模式回归现状。
"""
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from core.engine.event import OrderEvent
from core.engine.http_bridge_dispatcher import (
    BridgeOrderRejected,
    BridgeUnavailableError,
)
from core.engine.live.order_machine import OrderStateMachine
from core.engine.ths_ui_dispatcher import ThsUiDispatcher
from tq_iquant_shared.constants import SignalType, TradeType


ORDER_HEADER = (
    "委托时间\t证券代码\t证券名称\t操作\t备注\t委托数量\t成交数量\t"
    "委托价格\t成交均价\t撤消数量\t合同编号\t交易市场\n"
)


def _order_row(time="20261023 09:30:01", code="000001", name="平安银行",
               bs="买入", note="", volume=1000, filled=1000, price=10.5,
               trade_price=10.4, cancelled=0, ref="100001", market="深圳"):
    return "\t".join([
        time, code, name, bs, note, str(volume), str(filled),
        str(price), str(trade_price), str(cancelled), ref, market,
    ])


class _FakeThs(ThsUiDispatcher):
    def __init__(self, text=""):
        super().__init__()
        self._text = text
        self._connected = True

    def get_orders(self):
        return self._parse_orders(self._text)

    def _read_position_rows(self):
        return self._parse_position_rows(
            "证券代码\t证券名称\t股票余额\t可用余额\t成本价\t市价\t市值\n"
            "000001\t平安银行\t1000\t0\t10.0\t10.5\t10500\n"
            "600000\t浦发银行\t500\t500\t8.0\t8.2\t4100\n"
        )


def test_query_orders_maps_core_contract_for_filled_order():
    d = _FakeThs(ORDER_HEADER + _order_row())
    rows = d.query_orders()

    assert len(rows) == 1
    o = rows[0]
    assert o["source"] == "BRIDGE"
    assert o["remark"] == ""             # THS 无法回写 remark
    assert o["order_ref"] == "100001"    # 合同编号
    assert o["instrument"] == "000001"
    assert o["exchange"] == "SZ"
    assert o["direction"] == 48
    assert o["volume"] == 1000
    assert o["traded_volume"] == 1000
    assert o["status"] == 56             # 已成
    assert o["insert_date"] == "20261023"
    assert o["insert_time"] == "093001"  # Core _parse_insert_utc 要 6 位纯数字


def test_query_orders_status_codes_for_cancel_junk_and_partial():
    text = ORDER_HEADER + "\n".join([
        _order_row(filled=0, note="已撤", cancelled=1000, ref="1", trade_price=0),
        _order_row(filled=0, note="废单", ref="2", trade_price=0),
        _order_row(filled=300, cancelled=700, ref="3"),
        _order_row(filled=300, volume=1000, ref="4"),
        _order_row(filled=0, note="已报", ref="5", trade_price=0),
    ])
    rows = _FakeThs(text).query_orders()
    by_ref = {r["order_ref"]: r["status"] for r in rows}
    assert by_ref["1"] == 54   # 全撤
    assert by_ref["2"] == 57   # 废单
    assert by_ref["3"] == 53   # 部撤
    assert by_ref["4"] == 55   # 部成（非终态）
    assert by_ref["5"] == 50   # 已报


def test_query_deals_synthesizes_cumulative_fill_rows():
    text = ORDER_HEADER + "\n".join([
        _order_row(filled=300, price=10.5, trade_price=10.4, ref="1"),
        _order_row(filled=0, ref="2", trade_price=0),
        _order_row(filled=1000, price=9.9, trade_price=9.8, ref="3"),
    ])
    deals = _FakeThs(text).query_deals()

    assert [d["order_ref"] for d in deals] == ["1", "3"]
    d1 = deals[0]
    assert d1["instrument"] == "000001"
    assert d1["exchange"] == "SZ"
    assert d1["direction"] == 48
    assert d1["volume"] == 300
    assert d1["price"] == pytest.approx(10.4)
    assert d1["amount"] == pytest.approx(3120.0)
    assert d1["commission"] == 0
    assert d1["remark"] == ""


def test_split_time_accepts_time_only_and_full_datetime():
    d = ThsUiDispatcher()
    date_part, time_part = d._split_time("09:30:01")
    assert len(date_part) == 8 and date_part.isdigit()
    assert time_part == "093001"

    date_part, time_part = d._split_time("20261023 14:59:58")
    assert (date_part, time_part) == ("20261023", "145958")


def test_query_positions_keeps_zero_available_for_t1():
    rows = _FakeThs().query_positions()
    by_code = {r["instrument"]: r for r in rows}
    assert by_code["000001"]["available"] == 0   # 不能回退成 1000
    assert by_code["000001"]["exchange"] == "SZ"
    assert by_code["600000"]["exchange"] == "SH"
    assert by_code["600000"]["cost"] == pytest.approx(8.0)


def test_query_quote_forwards_tq_bars(monkeypatch):
    index = pd.to_datetime(["2026-10-23 09:30:00", "2026-10-23 09:31:00"])
    raw = {"1m": {
        "Open": pd.DataFrame({"000001.SZ": [10.0, 10.1]}, index=index),
        "High": pd.DataFrame({"000001.SZ": [10.2, 10.3]}, index=index),
        "Low": pd.DataFrame({"000001.SZ": [9.9, 10.0]}, index=index),
        "Close": pd.DataFrame({"000001.SZ": [10.1, 10.2]}, index=index),
        "Volume": pd.DataFrame({"000001.SZ": [1000, 2000]}, index=index),
        "Amount": pd.DataFrame({"000001.SZ": [10100, 20400]}, index=index),
    }}

    class FakeTQ:
        def get_history_raw(self, stocks, periods, **kwargs):
            assert stocks == ["000001.SZ"]
            assert periods == ["1m"]
            return raw

    monkeypatch.setattr("core.tq.data.TQData", FakeTQ)
    bars = ThsUiDispatcher().query_quote("000001.SZ", "1m", 2)
    assert [b["stime"] for b in bars] == ["20261023093000", "20261023093100"]
    assert bars[0]["close"] == pytest.approx(10.1)
    assert bars[1]["volume"] == 2000


def test_query_calendar_delegates_to_tdx_authority(monkeypatch):
    """ths 日历委托 TdxTqDispatcher：get_trading_dates 权威口径命中 → 直接返回。"""
    class FakeTQ:
        def get_trading_dates(self, market="SH", start="", end=""):
            assert market == "SH"
            return ["20261022", "20261023"]

        def get_history_raw(self, stocks, periods, **kwargs):
            raise AssertionError("命中权威日历后不应走日线反推")

    monkeypatch.setattr("core.tq.data.TQData", FakeTQ)
    assert ThsUiDispatcher().query_calendar() == ["20261022", "20261023"]


def test_query_calendar_falls_back_to_daily_index(monkeypatch):
    """权威日历不可用（get_trading_dates 缺失/异常）→ 日线反推兜底仍生效。"""
    index = pd.to_datetime(["2026-10-22", "2026-10-23"])
    raw = {"1d": {"Close": pd.DataFrame({"000001.SH": [1.0, 1.1]}, index=index)}}

    class FakeTQ:
        def get_history_raw(self, stocks, periods, **kwargs):
            return raw

    monkeypatch.setattr("core.tq.data.TQData", FakeTQ)
    assert ThsUiDispatcher().query_calendar() == ["20261022", "20261023"]


def test_try_match_order_ref_uses_legacy_fuzzy_when_channel_has_no_remark():
    class Disp:
        supports_remark = False

        @staticmethod
        def query_orders():
            return []

    om = OrderStateMachine.__new__(OrderStateMachine)
    om._ctx = SimpleNamespace(dispatcher=Disp())
    lo = SimpleNamespace(
        stock_code="600000.SH", trade_type="buy", quantity=600,
        created_at=datetime(2026, 10, 23, 1, 30, 0),  # UTC
        bridge_order_id="deadbeef", order_ref=None,
    )
    orders = [{
        "source": "BRIDGE", "remark": "", "order_ref": "ref-1",
        "instrument": "600000", "exchange": "SH", "direction": 48,
        "volume": 600, "insert_date": "20261023", "insert_time": "093030",
    }]
    om.try_match_order_ref(lo, orders=orders)
    assert lo.order_ref == "ref-1"


def test_read_fund_values_handles_grouped_and_alternating_layouts():
    class Static:
        def __init__(self, text):
            self._text = text

        def window_text(self):
            return self._text

    texts = [
        "资金余额", "冻结金额", "可用金额",
        "5541.66", "0.00", "5541.66",
        "港股市值", "A股市值", "总资产",
        "0.00", "142663.92", "148205.58",
        "可用金额", "5541.66", "股票市值", "142663.92", "总资产", "148205.58",
    ]

    class FakeWin:
        def descendants(self, class_name=None):
            return [Static(t) for t in texts]

    d = ThsUiDispatcher()
    d._win = FakeWin()
    funds = d._read_fund_values()
    assert funds["available"] == 5541.66
    assert funds["frozen"] == 0.0
    assert funds["balance"] == 5541.66
    assert funds["market_value"] == 142663.92
    assert funds["total_asset"] == 148205.58


def test_ocr_digits_extracts_four_digits_from_spaced_text(monkeypatch):
    from PIL import Image

    d = ThsUiDispatcher(captcha_ocr=True)
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    monkeypatch.setattr(d, "_recognize_image", lambda engine, img: "3 5 1 0")

    img = Image.new("RGB", (123, 51), "white")
    assert d._ocr_digits(img) == "3510"


def test_copy_grid_rereads_clipboard_after_captcha(monkeypatch):
    """验证码弹窗时剪贴板仍是旧页数据；解完必须重读，不能返回旧内容。"""
    old = ORDER_HEADER + _order_row(ref="OLD")
    new = ORDER_HEADER + _order_row(ref="NEW")

    class FakeClip:
        def __init__(self):
            self.value = old

        def copy(self, value):
            self.value = value

        def paste(self):
            return self.value

    fake = FakeClip()
    state = {"captcha": False}

    def fake_key(keys):
        if keys == "^c":
            state["captcha"] = True

    def fake_solve(dlg):
        state["captcha"] = False
        fake.value = new
        return True

    d = ThsUiDispatcher(captcha_ocr=True)
    d._COPY_WAIT = 0
    # 不得触发真实 _get_ocr_engine()：winrt 引擎在进程退出阶段会引发
    # access violation（0xC0000005），拖垮整个 pytest 退出码。
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    monkeypatch.setattr(d, "_key", fake_key)
    monkeypatch.setattr(d, "_find_captcha_dialog",
                        lambda: object() if state["captcha"] else None)
    monkeypatch.setattr(d, "_solve_captcha", fake_solve)
    monkeypatch.setattr(
        "core.engine.ths_ui_dispatcher.pyperclip", fake)

    assert d._copy_grid(required=("委托时间", "合同编号")) == new


def _order(**overrides):
    kwargs = dict(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN,
        quantity=100, price=Decimal("10.00"),
        bar_time=datetime(2026, 10, 23, 9, 30), signal_name="open_sig",
    )
    kwargs.update(overrides)
    return OrderEvent(**kwargs)


def _file_dispatcher(tmp_path, **overrides):
    kwargs = dict(
        execute_mode="file_builtin", work_dir=str(tmp_path), session_id=42,
        execute_interval_seconds=3600,  # 测试不启动真实节拍（需要时再覆盖）
    )
    kwargs.update(overrides)
    return ThsUiDispatcher(**kwargs)


def test_place_order_refuses_without_captcha_ocr():
    """direct 模式：未启用 OCR 时必须先拒单——否则先发单再查不到合同编号会误报失败。"""
    order = SimpleNamespace(
        stock_code="000001.SZ", quantity=100, price=Decimal("10.00"),
        trade_type=TradeType.BUY,
    )
    d = ThsUiDispatcher(captcha_ocr=False, execute_mode="direct")
    d._connected = True
    with pytest.raises(BridgeUnavailableError):
        d.place_order(order)


# ---------------- file_builtin 双模式 ----------------

def test_file_mode_place_order_enqueues_and_returns_none_without_ui(tmp_path, monkeypatch):
    """file 模式 place_order：指令入 orders.jsonl 并秒回 None，全程不触 UI 自动化。"""
    d = _file_dispatcher(tmp_path)
    d._connected = True

    def _boom(order):
        raise AssertionError("file 模式 place_order 不得直接执行 UI 自动化")

    monkeypatch.setattr(d, "_execute_via_ui", _boom)
    order = _order()
    result = d.place_order(order)

    assert result is None
    assert d.last_enqueued_order_id == d.order_id(order)
    with open(d._handoff.orders_path, "r", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["order_id"] == d.order_id(order)
    assert entry["stock_code"] == "000001.SZ"
    assert entry["direction"] == "BUY"
    assert entry["quantity"] == 100
    assert entry["price"] == pytest.approx(10.0)
    assert entry["strategy_id"] == 1
    assert entry["portfolio_id"] == 1
    assert entry["signal_name"] == "open_sig"
    d.close()


def test_file_mode_place_order_does_not_preflight_ocr(tmp_path):
    """file 模式不做 captcha/OCR 前置拒单——OCR 守卫移入执行器（失败→rejected 回执）。"""
    d = _file_dispatcher(tmp_path, captcha_ocr=False)
    d._connected = True
    assert d.place_order(_order()) is None  # 不抛 BridgeUnavailableError
    d.close()


def test_file_mode_place_order_rejects_invalid_order(tmp_path):
    d = _file_dispatcher(tmp_path)
    with pytest.raises(BridgeOrderRejected):
        d.place_order(_order(stock_code="60000X"))
    with pytest.raises(BridgeOrderRejected):
        d.place_order(_order(quantity=0))
    assert d.last_enqueued_order_id is None
    assert not any(r.get("order_id") for r in _jsonl(d._handoff.orders_path))


def _jsonl(path):
    import os
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_executor_entry_marks_accepted_with_ref(tmp_path):
    """执行成功：results.jsonl 落 accepted+ref，_known_refs/_submit_times 补记（批次 A 口径）。"""
    d = _file_dispatcher(tmp_path)
    order = _order()
    entry = {"order_id": d.order_id(order), "stock_code": "000001.SZ",
             "direction": "BUY", "quantity": 100, "price": 10.0,
             "strategy_id": 1, "portfolio_id": 1, "signal_name": "open_sig"}

    def _fake_ui(o):
        assert isinstance(o, OrderEvent)
        assert o.stock_code == "000001.SZ" and o.trade_type == TradeType.BUY
        d._last_submit_ref = "100001"
        d._last_submit_time = ("20261023", "093001")
        return object()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", _fake_ui)
    try:
        d._execute_entry(entry)
    finally:
        monkey.undo()

    results = _jsonl(d._handoff.results_path)
    assert results[0] == dict(results[0], status="accepted", ref="100001",
                              order_id=d.order_id(order))
    assert "100001" in d._known_refs
    assert d._submit_times["100001"] == ("20261023", "093001")
    assert d.order_id(order) in d._handoff.cursor["done"]


def test_executor_entry_marks_rejected_on_bridge_error(tmp_path):
    d = _file_dispatcher(tmp_path)
    oid = "abc123"
    entry = {"order_id": oid, "stock_code": "000001.SZ", "direction": "BUY",
             "quantity": 100, "price": 10.0}

    def _boom(o):
        raise BridgeOrderRejected("invalid order")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", _boom)
    try:
        d._execute_entry(entry)
    finally:
        monkey.undo()

    results = _jsonl(d._handoff.results_path)
    assert results[0]["status"] == "rejected"
    assert "invalid order" in results[0]["error"]
    assert d._file_rejects[oid]["error"] == "invalid order"
    assert d._file_rejects[oid]["entry"]["stock_code"] == "000001.SZ"
    assert oid in d._handoff.cursor["done"]


def test_executor_entry_survives_unexpected_error(tmp_path):
    """UI 异常不杀执行线程：意外异常同样落 rejected 回执，下轮继续。"""
    d = _file_dispatcher(tmp_path)
    entry = {"order_id": "zzz", "stock_code": "000001.SZ", "direction": "BUY",
             "quantity": 100, "price": 10.0}

    def _boom(o):
        raise RuntimeError("com fail")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", _boom)
    try:
        d._execute_entry(entry)
    finally:
        monkey.undo()

    assert _jsonl(d._handoff.results_path)[0]["status"] == "rejected"
    assert d._file_rejects["zzz"]["error"] == "com fail"


def test_execution_receipts_pop_file_rejects(tmp_path):
    """execution_receipts：弹出 _file_rejects 回执（单消费者=引擎），含 entry 形状。"""
    d = _file_dispatcher(tmp_path)
    entry = {"order_id": "abc123", "stock_code": "000001.SZ", "direction": "BUY",
             "quantity": 100, "price": 10.0}

    def _boom(o):
        raise BridgeOrderRejected("invalid order")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", _boom)
    try:
        d._execute_entry(entry)
    finally:
        monkey.undo()

    receipts = d.execution_receipts()
    assert len(receipts) == 1
    r = receipts[0]
    assert r["order_id"] == "abc123"
    assert r["status"] == "rejected"
    assert r["error"] == "invalid order"
    assert r["ts"]  # 上海时间
    assert r["entry"]["stock_code"] == "000001.SZ"
    assert r["entry"]["direction"] == "BUY"
    assert r["entry"]["quantity"] == 100
    # 弹出语义：第二次调用为空（单消费者）
    assert d.execution_receipts() == []
    d.close()


def test_execution_receipts_direct_mode_returns_empty(tmp_path):
    d = ThsUiDispatcher(execute_mode="direct")
    assert d.execution_receipts() == []


def test_order_dispatcher_default_execution_receipts_empty():
    """基类默认无回执（TdxTqDispatcher 等无需实现）。"""
    from core.engine.execution_engine import SimulatedDispatcher
    assert SimulatedDispatcher().execution_receipts() == []


def test_executor_thread_consumes_pending_and_close_stops(tmp_path):
    """后台执行线程按频率消费指令文件；close() 停线程。"""
    d = _file_dispatcher(tmp_path, execute_interval_seconds=0.05)

    def _fake_ui(o):
        d._last_submit_ref = "200002"
        d._last_submit_time = ("20261023", "093005")
        return object()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", _fake_ui)
    try:
        assert d.place_order(_order()) is None
        deadline = time.time() + 5
        while time.time() < deadline:
            if _jsonl(d._handoff.results_path):
                break
            time.sleep(0.05)
        assert any(r.get("ref") == "200002" for r in _jsonl(d._handoff.results_path))
        assert d._executor_thread is not None and d._executor_thread.is_alive()
    finally:
        monkey.undo()
    worker = d._executor_thread
    d.close()
    assert not worker.is_alive()


def test_direct_mode_place_order_delegates_to_ui(tmp_path):
    """direct 模式回归：place_order = 同步 _execute_via_ui（现行为不变）。"""
    d = ThsUiDispatcher(execute_mode="direct")
    d._connected = True
    sentinel = object()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(d, "_execute_via_ui", lambda o: sentinel)
    try:
        assert d.place_order(_order()) is sentinel
    finally:
        monkey.undo()


class _FakeField:
    """UIA 字段替身：control_id + rect（供 _click_field 计算点击坐标）。"""

    def __init__(self, control_id, left, top, width):
        self.element_info = SimpleNamespace(control_id=control_id)
        self._left, self._top, self._w = left, top, width

    def rectangle(self):
        return SimpleNamespace(
            left=self._left, top=self._top,
            right=self._left + self._w, bottom=self._top + 30,
            width=lambda: self._w,
        )


def test_execute_via_ui_calibrated_click_flow_buy_and_sell(monkeypatch):
    """校准点击流断言：F1/F2 方向；代码框点击=rect 左起 8px（避开下拉按钮）、
    价格/数量中心点击；键入 kb_send 序（^a 清空 + 值）；显式价格（限价语义）。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    clicks = []
    kb_transcript = []
    switches = []
    fake_fields = {
        "code": _FakeField(1032, 316, 141, 121),
        "price": _FakeField(1033, 316, 205, 98),
        "quantity": _FakeField(1034, 316, 301, 98),
    }

    monkeypatch.setattr(d, "_gui_guard", lambda: True)
    monkeypatch.setattr(d, "_switch", lambda k: switches.append(k))
    monkeypatch.setattr(d, "_uia_window", lambda: object())
    monkeypatch.setattr(d, "_uia_field", lambda w, kind: fake_fields[kind])
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "click",
                        lambda coords: clicks.append((coords[0], coords[1])))
    monkeypatch.setattr(d, "kb_send", lambda k: kb_transcript.append(k))
    monkeypatch.setattr(d, "_snapshot_hwnds", lambda: set())
    monkeypatch.setattr(d, "_confirm_dialog", lambda before, buttons: True)
    monkeypatch.setattr(d, "_find_order",
                        lambda code, direction, vol: {"order_id": "200001"})
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())

    d._execute_via_ui(_order(stock_code="600519.SH", quantity=100,
                             price=Decimal("1700.55")))
    assert switches == ["{F1}"]                    # 买入 → F1
    # 代码框点击 x = 316+8 = 324（左偏 8px，避开下拉按钮）
    assert clicks[0] == (324, 156)
    # 价格/数量中心点击
    assert clicks[1] == (316 + 49, 220)
    assert clicks[2] == (316 + 49, 316)
    assert kb_transcript == [
        "{END}", "{BACKSPACE}" * 12, "600519",
        "{END}", "{BACKSPACE}" * 12, "1700.55",              # 显式价格（限价）
        "{END}", "{BACKSPACE}" * 12, "100",
        "{ENTER}",
    ]

    clicks.clear()
    kb_transcript.clear()
    switches.clear()
    d._execute_via_ui(_order(stock_code="000001.SZ", quantity=300,
                             price=Decimal("9.99"),
                             trade_type=TradeType.SELL,
                             signal_type=SignalType.CLOSE))
    assert switches == ["{F2}"]                    # 卖出 → F2
    assert clicks[0] == (324, 156)
    assert kb_transcript == [
        "{END}", "{BACKSPACE}" * 12, "000001",
        "{END}", "{BACKSPACE}" * 12, "9.99",
        "{END}", "{BACKSPACE}" * 12, "300",
        "{ENTER}",
    ]


def test_cancel_order_geometry_flow(monkeypatch):
    """cancel_order 几何双击方案：F3 → 网格定位 → 双击目标行（y=top+45+idx*24）
    → 复读目标消失 → True。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6333266769"}

    rows_now = [{"order_id": "6333266769", "symbol": "510300",
                 "direction": "buy", "price": 4.35, "volume": 100}]

    class _GridEl:
        class element_info:
            class_name = "CVirtualGridCtrl"

        def rectangle(self):
            from types import SimpleNamespace
            return SimpleNamespace(left=207, top=111, right=1919, bottom=1044)

    class _FakeWin:
        def descendants(self):
            return [_GridEl()]

    d._win = _FakeWin()
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append((coords[0], coords[1])))

    # 撤单生效：双击后复读为空（第一次读回有目标 → 双击 → 第二次空）
    state = {"n": 0}

    def _orders():
        state["n"] += 1
        return list(rows_now) if state["n"] == 1 else []

    monkeypatch.setattr(d, "get_orders", _orders)
    assert d.cancel_order("6333266769", "510300.SH") is True
    assert len(clicks) == 1
    assert clicks[0] == ((207 + 1919) // 2, 156)   # 首行 y = top(111)+45


def test_cancel_order_refuses_unknown_refs(monkeypatch):
    """可撤列表含非平台单（order_id ∉ _known_refs）→ 拒绝执行（防误撤手动委托）。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6333266769"}

    class _GridEl:
        class element_info:
            class_name = "CVirtualGridCtrl"

        def rectangle(self):
            from types import SimpleNamespace
            return SimpleNamespace(left=207, top=111, right=1919, bottom=1044)

    class _FakeWin:
        def descendants(self):
            return [_GridEl()]

    d._win = _FakeWin()

    rows_now = [
        {"order_id": "6333266769", "symbol": "510300", "direction": "buy"},
        {"order_id": "9999999999", "symbol": "600519", "direction": "buy"},  # 手动单
    ]
    monkeypatch.setattr(d, "get_orders", lambda: list(rows_now))
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append(coords))

    assert d.cancel_order("6333266769", "510300.SH") is False
    assert d.last_error and "非本平台委托" in d.last_error
    assert clicks == []   # 未做任何双击


def test_cancel_order_target_missing(monkeypatch):
    """目标不在可撤列表（已撤/已成交收口）→ False。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6333266769"}
    monkeypatch.setattr(d, "get_orders", lambda: [])

    assert d.cancel_order("6333266769", "510300.SH") is False


def test_get_orders_retries_once_on_empty(monkeypatch):
    """委托表读回偶发整表读空 → 空结果重试一次。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    calls = {"n": 0}

    def _once():
        calls["n"] += 1
        return [] if calls["n"] == 1 else [{"order_id": "W1"}]

    monkeypatch.setattr(d, "_read_orders_once", _once)
    rows = d.get_orders()
    assert rows == [{"order_id": "W1"}]
    assert calls["n"] == 2
