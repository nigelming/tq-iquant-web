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

    def _read_position_rows(self, max_age: float = 0.0):
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
    # 遮挡校验：本用例全部为替身字段，无真实窗口句柄 → 直接放行
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: True)
    monkeypatch.setattr(d, "_switch", lambda k: switches.append(k) or True)
    monkeypatch.setattr(d, "_uia_window", lambda: object())
    monkeypatch.setattr(d, "_uia_field", lambda w, kind: fake_fields[kind])
    d._PRICE_FOLLOW_WAIT = 0
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "click",
                        lambda coords: clicks.append((coords[0], coords[1])))
    monkeypatch.setattr(d, "kb_send", lambda k: kb_transcript.append(k))
    monkeypatch.setattr(d, "_snapshot_hwnds", lambda: set())
    monkeypatch.setattr(d, "_confirm_dialog", lambda before, buttons: True)
    monkeypatch.setattr(d, "_find_order",
                        lambda code, direction, vol, since=None: {"order_id": "200001"})
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    # 数量读回钳制：mock 剪贴板读取（auto_price/auto_qty 读不到 → 引擎量兜底）
    monkeypatch.setattr(d, "_clipboard_copy_float", lambda: None)
    monkeypatch.setattr(d, "_clipboard_copy_int", lambda: None)

    d._execute_via_ui(_order(stock_code="600519.SH", quantity=100,
                             price=Decimal("1700.55")))
    assert switches == ["{F1}"]                    # 买入 → F1
    # direct 模式下单成功后 ref 入 _known_refs（撤单 unknown 守卫豁免基线）
    assert d._known_refs == {"200001"}
    assert d._last_submit_ref == "200001"
    # 代码框点击 x = 316+8 = 324（左偏 8px，避开下拉按钮）；两段式持锁在
    # 4s 价格跟随等待后重锚一次代码框（同样落输入区左起 8px）
    assert clicks[0] == (324, 156)
    assert clicks[1] == (324, 156)
    # 价格/数量中心点击
    assert clicks[2] == (316 + 49, 220)
    assert clicks[3] == (316 + 49, 316)
    assert kb_transcript == [
        "{END}", "{BACKSPACE}" * 12, "600519",
        "{END}",                                # 重锚代码框后光标归位
        "{TAB}", "{TAB}",
        "{END}", "{BACKSPACE}" * 12, "1700.55",              # 显式价格限价
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
        "{END}",
        "{TAB}", "{TAB}",
        "{END}", "{BACKSPACE}" * 12, "9.99",
        "{END}", "{BACKSPACE}" * 12, "300",
        "{ENTER}",
    ]


def test_cancel_order_ocr_navigation_flow(monkeypatch):
    """cancel_order OCR 导航：F3 → 整窗 OCR 定位目标行坐标 → 双击该行 → 复读为空 → True。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6333266769"}

    rows_now = [{"order_id": "6333266769", "symbol": "510300",
                 "direction": "buy", "price": 4.35, "volume": 100}]

    d._win = SimpleNamespace(
        rectangle=lambda: SimpleNamespace(left=100, top=100, right=2019, bottom=1144))
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append((coords[0], coords[1])))

    # 撤单生效：双击后复读为空（第一次读回有目标 → 双击 → 第二次空）
    state = {"n": 0}

    def _orders(**kwargs):
        state["n"] += 1
        return list(rows_now) if state["n"] == 1 else []

    monkeypatch.setattr(d, "get_orders", _orders)
    monkeypatch.setattr(d, "_ocr_words",
                        lambda img: [("510300", 309, 157), ("沪深300ETF", 340, 157)])
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: True)
    monkeypatch.setattr(d, "_read_fund_values",
                        lambda: {"frozen": "0.0", "available": "148071.29"})

    assert d.cancel_order("6333266769", "510300.SH") is True
    assert len(clicks) == 1
    # 双击点 = 窗口原点(100,100) + 词坐标(309,157) + 偏移(30,8) = (439, 265)
    assert clicks[0] == (100 + 309 + 30, 100 + 157 + 8)


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
    monkeypatch.setattr(d, "get_orders", lambda **kw: list(rows_now))
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append(coords))

    assert d.cancel_order("6333266769", "510300.SH") is False
    assert d.last_error and "非本平台委托" in d.last_error
    assert clicks == []   # 未做任何双击


def test_cancel_order_ignores_stale_cancelled_history_rows(monkeypatch):
    """跨日残留：撤单页并列历史已撤单不应触发「非本平台委托」拒绝。

    2026-11-07 真机实证：THS 客户端跨日未重启，撤销新单 6999 时撤单页仍列
    昨日 5 笔已撤单（831/774/710/666/583）→ 被「含 5 条非本平台委托」拒绝，
    自动撤单实际失效。终结行（已撤/废单）不构成误撤风险，应放行。
    """
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6999"}
    d._win = SimpleNamespace(
        rectangle=lambda: SimpleNamespace(left=100, top=100, right=2019, bottom=1144))
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append(coords))
    rows_now = [
        {"order_id": "6999", "symbol": "002745", "direction": "buy",
         "price": 9.0, "volume": 100, "note": "未报"},
        {"order_id": "831", "symbol": "002745", "direction": "buy",
         "price": 10.56, "volume": 100, "cancelled_volume": 100, "note": "已撤"},
    ]
    state = {"n": 0}

    def _orders(**kwargs):
        state["n"] += 1
        return list(rows_now) if state["n"] == 1 else []

    monkeypatch.setattr(d, "get_orders", _orders)
    monkeypatch.setattr(d, "_ocr_words", lambda img: [("002745", 309, 157)])
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: True)
    assert d.cancel_order("6999", "002745.SZ") is True
    assert len(clicks) == 1


def test_cancel_order_target_missing(monkeypatch):
    """目标不在可撤列表（已撤/已成交收口）→ False。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    d._known_refs = {"6333266769"}
    monkeypatch.setattr(d, "get_orders", lambda **kw: [])

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


# ===========================================================================
# 终审修复：价格闭环撤单腿 / direct known_refs / cancel 页面语义固定
# ===========================================================================
def _ui_mocked_dispatcher(monkeypatch, found_row):
    """_execute_via_ui 真机校准流全 mock（与 test_execute_via_ui_calibrated*
    同款替身），_find_order 可注入回查行。返回 (dispatcher, switches)。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    fake_fields = {
        "code": _FakeField(1032, 316, 141, 121),
        "price": _FakeField(1033, 316, 205, 98),
        "quantity": _FakeField(1034, 316, 301, 98),
    }
    monkeypatch.setattr(d, "_gui_guard", lambda: True)
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: True)
    monkeypatch.setattr(d, "_switch", lambda k: True)
    d._PRICE_FOLLOW_WAIT = 0
    monkeypatch.setattr(d, "_uia_window", lambda: object())
    monkeypatch.setattr(d, "_uia_field", lambda w, kind: fake_fields[kind])
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "click", lambda coords: None)
    monkeypatch.setattr(d, "kb_send", lambda k: None)
    monkeypatch.setattr(d, "_snapshot_hwnds", lambda: set())
    monkeypatch.setattr(d, "_confirm_dialog", lambda before, buttons: True)
    monkeypatch.setattr(d, "_find_order",
                        lambda code, direction, vol, since=None: found_row)
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    return d


def test_execute_via_ui_price_mismatch_cancels_via_known_ref(monkeypatch):
    """价格闭环撤单腿：委托价 ≠ 审批价 → ref 先入 _known_refs（自身豁免守卫）
    → cancel_order 被调 → 撤单成功 → rejected 带已自动撤单说明。"""
    found = {"order_id": "200002", "price": 4.415, "time": "20261028 09:30:01"}
    d = _ui_mocked_dispatcher(monkeypatch, found)
    cancel_calls = []
    monkeypatch.setattr(
        d, "cancel_order",
        lambda ref, code: cancel_calls.append((ref, code)) or True)

    with pytest.raises(BridgeOrderRejected) as ei:
        d._execute_via_ui(_order(stock_code="000001.SZ", quantity=300,
                                 price=Decimal("4.153")))

    assert cancel_calls == [("200002", "000001")]  # 撤单被调（6 位代码）
    assert "200002" in d._known_refs               # 撤单前已入基线
    msg = str(ei.value)
    assert "4.415" in msg and "4.153" in msg
    assert "已自动撤单" in msg


def test_execute_via_ui_price_mismatch_cancel_failed_warns(monkeypatch):
    """价格闭环撤单腿失败：cancel_order False → rejected 带附加警告与合同号。"""
    found = {"order_id": "200003", "price": 4.415, "time": "20261028 09:30:01"}
    d = _ui_mocked_dispatcher(monkeypatch, found)
    cancel_calls = []
    monkeypatch.setattr(
        d, "cancel_order",
        lambda ref, code: cancel_calls.append((ref, code)) or False)

    with pytest.raises(BridgeOrderRejected) as ei:
        d._execute_via_ui(_order(stock_code="000001.SZ", quantity=300,
                                 price=Decimal("4.153")))

    assert cancel_calls == [("200003", "000001")]
    assert "200003" in d._known_refs
    msg = str(ei.value)
    assert "自动撤单失败" in msg and "请人工处理" in msg and "200003" in msg


def test_cancel_order_pinned_to_cancel_page_and_status_gone(monkeypatch):
    """cancel_order 撤单页语义固定：读回 pin_cancel_page=True；目标行仍在但
    撤消数量=委托数量 → gone=True（旧口径按行消失判定会恒 False）。"""
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

        def rectangle(self):
            from types import SimpleNamespace
            return SimpleNamespace(left=100, top=100, right=2019, bottom=1144)

    d._win = _FakeWin()
    import pywinauto.mouse as ths_mouse
    clicks = []
    monkeypatch.setattr(ths_mouse, "double_click",
                        lambda coords: clicks.append((coords[0], coords[1])))
    monkeypatch.setattr(d, "_ocr_words",
                        lambda img: [("510300", 309, 157)])
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: True)

    pin_kwargs = []

    def _orders(**kwargs):
        pin_kwargs.append(kwargs)
        if len(pin_kwargs) == 1:
            # 首次读回：目标在可撤列表（行中途，撤销量 0）
            return [{"order_id": "6333266769", "symbol": "510300",
                     "direction": "buy", "price": 4.35,
                     "volume": 100, "cancelled_volume": 0}]
        # 撤单后复读：目标仍在（但呈撤单态——撤销量扣满委托数量）
        return [{"order_id": "6333266769", "symbol": "510300",
                 "direction": "buy", "price": 4.35,
                 "volume": 100, "cancelled_volume": 100}]

    monkeypatch.setattr(d, "get_orders", _orders)

    assert d.cancel_order("6333266769", "510300.SH") is True
    assert pin_kwargs == [{"pin_cancel_page": True}, {"pin_cancel_page": True}]
    # OCR 导航双击：窗口原点(100,100) + 词坐标(309,157) + 偏移(30,8)
    assert clicks == [(439, 265)]


def test_order_gone_by_status_or_missing_row():
    """撤单生效判定（按行状态）：撤消数量扣满 → True；状态列含「撤」→ True；
    部撤（成交+撤消扣满）→ True；行消失 → True；在途行 → False。"""
    gone = ThsUiDispatcher._order_gone_in
    assert gone([{"order_id": "R1", "volume": 100,
                  "cancelled_volume": 100, "filled_volume": 0}], "R1") is True
    assert gone([{"order_id": "R1", "volume": 1000,
                  "cancelled_volume": 700, "filled_volume": 300}], "R1") is True
    assert gone([{"order_id": "R1", "volume": 100,
                  "cancelled_volume": 0, "filled_volume": 0,
                  "note": "已撤"}], "R1") is True
    assert gone([], "R1") is True                      # 行消失
    assert gone([{"order_id": "R1", "volume": 100,   # 在途：未撤
                  "cancelled_volume": 0, "filled_volume": 0}], "R1") is False
    assert gone([{"order_id": "R1", "volume": 100,   # 部分撤未扣满：未生效
                  "cancelled_volume": 30, "filled_volume": 0}], "R1") is False
    assert gone([{"order_id": "R2", "volume": 100,
                  "cancelled_volume": 0}], "R1") is True  # 目标行不在 → 消失


# ---------------- 登录态 / 读失败 ≠ 空仓 ----------------

class _ReadResultThs(ThsUiDispatcher):
    """固定 _copy_grid 返回值的读数桩（不触真实 UI）。"""

    def __init__(self, text):
        super().__init__()
        self._connected = True
        self._text = text

    def _gui_guard(self):
        return True

    def _select_page(self, name):
        return True

    def _switch(self, tab):
        pass

    def _copy_grid(self, required=()):
        return self._text


def test_position_read_failure_is_none_not_empty():
    """表格复制为空（未登录/验证码/切页失败）→ None，绝不能当真空仓 []。"""
    d = _ReadResultThs("")
    assert d._read_position_rows() is None
    assert d.query_positions() is None
    assert d.get_positions() is None
    assert d.get_account() is None
    assert d.query_account() is None
    assert "读取失败" in d.last_error


def test_position_header_only_is_genuine_empty_account():
    """表头复制成功但没有数据行 → 真·空仓 []（与读失败区分）。"""
    d = _ReadResultThs(
        "证券代码\t证券名称\t股票余额\t可用余额\t成本价\t市价\t市值\n")
    assert d._read_position_rows() == []
    assert d.query_positions() == []
    assert d.last_error == ""


def test_heartbeat_marks_disconnected_on_login_page(monkeypatch):
    """登录页（窗口仍在、标题不变）必须判为未连接，避免假在线/假空仓。"""
    d = ThsUiDispatcher()
    d._connected = True
    d._win = SimpleNamespace(exists=lambda: True)
    d._login_probe_at = 0.0
    monkeypatch.setattr(d, "_detect_login_page", lambda: True)

    assert d.heartbeat() is False
    assert d._connected is False
    assert "登录" in d.last_error


def test_connect_rejects_login_page(monkeypatch):
    import core.engine.ths_ui_dispatcher as ths_mod

    class FakeApp:
        def __init__(self, *a, **k):
            pass

        def connect(self, *a, **k):
            return self

        def window(self, *a, **k):
            return SimpleNamespace(exists=lambda: True)

        def top_window(self):
            return SimpleNamespace(exists=lambda: True)

    monkeypatch.setattr(ths_mod, "Application", lambda *a, **k: FakeApp())
    d = ThsUiDispatcher(client_exe_path="")
    d._login_probe_at = 0.0
    monkeypatch.setattr(d, "_detect_login_page", lambda: True)

    assert d.connect() is False
    assert "登录" in d.last_error


def test_select_page_uses_handle_uia_window(monkeypatch):
    """切页必须走句柄定位的 UIA 窗口。

    2026-11-06 真机实证：`Application(backend="uia").connect(title_re=...)`
    找不到同花顺主窗（切页恒失败 → 委托读回退 F3 撤单页，可能漏已成交单）；
    句柄定位可用。本用例锁死「不再按标题连 UIA」这一回归点。
    """
    d = ThsUiDispatcher()
    d._win = object()  # 仅要求非 None；真正句柄路径被 _uia_window 替身接管
    picked = []

    class _Item:
        def window_text(self):
            return "当日委托"

        def select(self):
            picked.append(True)

    class _Uia:
        def descendants(self, control_type=None, class_name=None):
            return [_Item()] if control_type == "TreeItem" else []

    monkeypatch.setattr(d, "_uia_window", lambda: _Uia())
    monkeypatch.setattr(d, "_SWITCH_WAIT", 0)
    import core.engine.ths_ui_dispatcher as ths_mod

    def _boom(*a, **k):
        raise AssertionError("不得再按标题连接 UIA 后端")

    monkeypatch.setattr(ths_mod, "Application", _boom)

    assert d._select_page("当日委托") is True
    assert picked == [True]


def test_copy_grid_accepts_fresh_clipboard_without_captcha_scan(monkeypatch):
    """剪贴板已是本次复制结果（非哨兵）→ 直接采用，不再扫桌面弹窗。"""
    grid = ORDER_HEADER + _order_row()

    class _Clip:
        def __init__(self):
            self.value = ""

        def copy(self, value):
            self.value = value

        def paste(self):
            return self.value

    clip = _Clip()

    def _key(keys):
        if keys == "^c":
            clip.value = grid   # 本次复制直接成功（无验证码拦截）

    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._COPY_WAIT = 0
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    monkeypatch.setattr(d, "_key", _key)
    monkeypatch.setattr(
        d, "_find_captcha_dialog",
        lambda: (_ for _ in ()).throw(AssertionError("不该扫验证码弹窗")))
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.pyperclip", clip)

    assert d._copy_grid(required=("委托时间", "合同编号")) == grid


def test_copy_grid_rejects_when_clipboard_not_writable(monkeypatch):
    """哨兵写不进去（剪贴板被占用）→ 不能把旧页数据当本轮结果。"""
    old = ORDER_HEADER + _order_row(ref="OLD")

    class _Clip:
        def __init__(self):
            self.value = old

        def copy(self, value):
            pass          # 写入失败（被其它程序占用）

        def paste(self):
            return self.value

    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._COPY_WAIT = 0
    monkeypatch.setattr(d, "_get_ocr_engine", lambda: object())
    monkeypatch.setattr(d, "_key", lambda k: None)
    monkeypatch.setattr(d, "_find_captcha_dialog", lambda: None)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.pyperclip", _Clip())

    assert d._copy_grid(required=("委托时间", "合同编号")) == ""
    assert "剪贴板" in d.last_error


def test_account_reuses_recent_position_rows(monkeypatch):
    """快照链路 get_positions→get_account 5s 内复用同一份持仓行（省一次读表）。"""
    d = ThsUiDispatcher(captcha_ocr=True, execute_mode="direct")
    d._connected = True
    calls = {"n": 0}

    monkeypatch.setattr(d, "_gui_guard", lambda: True)
    monkeypatch.setattr(d, "_select_page", lambda name: True)
    monkeypatch.setattr(d, "_read_fund_values",
                        lambda: {"available": 1000.0, "total_asset": 5000.0})

    text = (
        "证券代码\t证券名称\t股票余额\t可用余额\t成本价\t市价\t市值\n"
        "002745\t木林森\t5300\t0\t10.221\t10.55\t55915\n"
    )

    def _copy(required=()):
        calls["n"] += 1
        return text

    monkeypatch.setattr(d, "_copy_grid", _copy)

    first = d.get_account()
    second = d.get_account()
    assert first == second
    assert calls["n"] == 1          # 第二次 5s 内复用缓存
    assert first[0]["total_asset"] == 5000.0


def _order_row_dict(ref, time_hm="173429", price=20.0, volume=100,
                    direction="buy", status=50):
    """_parse_orders 的真实行形状：只有 time 文本（无 insert_date/insert_time）。"""
    t = time_hm if ":" in time_hm else "%s:%s:%s" % (
        time_hm[:2], time_hm[2:4], time_hm[4:6])
    return {
        "symbol": "002745", "direction": direction, "volume": volume,
        "order_id": ref, "time": t, "price": price, "status": status,
    }


def _freeze_now(monkeypatch, fixed):
    """冻结 dispatcher 的 now_shanghai：_split_time 按「当天」补日期，委托表
    又只有 HH:MM:SS——不冻结的话用例写成 17:34 会在真实时间 17:27 跑时被
    「跨日残留未来戳」防火墙当成明日残留（真实缺陷，非用例本意）。"""
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.now_shanghai", lambda: fixed)


def test_find_order_picks_newest_regardless_of_row_order(monkeypatch):
    """同码/向/量有多笔历史单时必须取最新一笔。

    2026-11-06 真机实证：THS 默认新单在最上，旧实现 reversed() 取首命中 →
    命中旧单 583（10.56）误判委托价 ≠ 20.00 → 误撤旧单、新单 666 滞留柜台。
    """
    _freeze_now(monkeypatch, datetime(2026, 11, 6, 17, 40, 0))
    d = ThsUiDispatcher()
    rows = [
        _order_row_dict("666", "173429", price=20.0),
        _order_row_dict("583", "172722", price=10.56, status=54),
    ]
    monkeypatch.setattr(d, "get_orders", lambda: rows)
    assert d._find_order("002745", "buy", 100)["order_id"] == "666"

    # 列表反序（按时间正序）也必须取最新
    monkeypatch.setattr(d, "get_orders", lambda: list(reversed(rows)))
    assert d._find_order("002745", "buy", 100)["order_id"] == "666"


def test_find_order_since_filters_previous_orders(monkeypatch):
    """since 过滤本次提交前的历史单：只剩旧单时回 None（不误认领）。"""
    _freeze_now(monkeypatch, datetime(2026, 11, 6, 17, 40, 0))
    d = ThsUiDispatcher()
    rows = [_order_row_dict("583", "172722", price=10.56, status=54)]
    monkeypatch.setattr(d, "get_orders", lambda: rows)

    assert d._find_order("002745", "buy", 100)["order_id"] == "583"
    # 提交时刻 17:30:00：120s 容差窗口外的 17:27:22 旧单被排除
    assert d._find_order("002745", "buy", 100, since="20261106173000") is None
    # 窗口内的新单正常命中
    rows.append(_order_row_dict("666", "173429", price=20.0))
    assert d._find_order("002745", "buy", 100,
                         since="20261106173000")["order_id"] == "666"


def test_find_order_skips_cross_day_stale_row(monkeypatch):
    """跨日残留防火墙（2026-11-07 真机实证）。

    THS 客户端跨日不重启时「当日委托」仍列昨日单且只有 HH:MM:SS → 昨日
    17:52:46（已撤 10.56）会被补成「今天更晚」的新单，盖过本次真实新单
    6999@17:19:04（未报 9.0）→ 误判委托价并去撤已撤旧单，真单被拒且滞留柜台。
    """
    _freeze_now(monkeypatch, datetime(2026, 11, 7, 17, 19, 30))
    d = ThsUiDispatcher()
    rows = [
        _order_row_dict("831", "175246", price=10.56, status=54),
        _order_row_dict("6999", "171904", price=9.0, status=50),
    ]
    monkeypatch.setattr(d, "get_orders", lambda: rows)
    assert d._find_order("002745", "buy", 100)["order_id"] == "6999"


def test_find_order_retry_reads_again_when_table_not_refreshed(monkeypatch):
    """真机实证（2026-11-06）：提交瞬间委托表尚未刷新出新行，单次回查 None
    → 真实已提交的单被误判 order not found（710 滞留柜台）。第二次重读应命中。"""
    d = ThsUiDispatcher()
    calls = {"n": 0}
    row = _order_row_dict("710", "174006", price=10.56)

    def _find(code, direction, volume, since=None):
        calls["n"] += 1
        return row if calls["n"] >= 2 else None

    monkeypatch.setattr(d, "_find_order", _find)
    found = d._find_order_retry("002745", "buy", 100,
                                since="20261106174006", interval=0)
    assert found["order_id"] == "710"
    assert calls["n"] == 2


def test_find_order_retry_gives_up_after_attempts(monkeypatch):
    """三次回查仍 None → 返回 None（调用方按拒单+人工核对处理）。"""
    d = ThsUiDispatcher()
    calls = {"n": 0}

    def _find(code, direction, volume, since=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr(d, "_find_order", _find)
    assert d._find_order_retry("002745", "buy", 100,
                               attempts=3, interval=0) is None
    assert calls["n"] == 3


# ===========================================================================
# 遮挡校验 / 锁粒度（2026-11-06 P0/P1）
# ===========================================================================
def _fake_user32(root_seq, iconic=False):
    """user32 替身：_ensure_topmost 只用到这 5 个 API。

    root_seq 是 WindowFromPoint→GetAncestor 每次返回的 root 句柄序列
    （第一次=拉前台前，第二次=SetForegroundWindow 后复检）。
    """
    state = {"i": 0}

    def _ancestor(h, flag):
        i = state["i"]
        state["i"] = i + 1
        return root_seq[min(i, len(root_seq) - 1)]

    return SimpleNamespace(
        WindowFromPoint=lambda pt: 999,
        GetAncestor=_ancestor,
        IsIconic=lambda h: iconic,
        ShowWindow=lambda h, flag: None,
        SetForegroundWindow=lambda h: None,
    )


def test_ensure_topmost_rechecks_after_foreground(monkeypatch):
    """被遮挡 → SetForegroundWindow → 0.25s 复检命中（拉前台成功）。"""
    d = ThsUiDispatcher()
    d._win = SimpleNamespace(handle=123)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.time.sleep", lambda s: None)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.ctypes.WinDLL",
                        lambda *a, **k: _fake_user32([777, 123]))
    assert d._ensure_topmost(10, 20) is True


def test_ensure_topmost_fails_closed_and_records_occluder(monkeypatch):
    """拉前台后仍被挡 → False + 记录最上层 hwnd（错误文案用）。"""
    d = ThsUiDispatcher()
    d._win = SimpleNamespace(handle=123)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.time.sleep", lambda s: None)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.ctypes.WinDLL",
                        lambda *a, **k: _fake_user32([777, 777]))
    assert d._ensure_topmost(10, 20) is False
    assert d._last_occluder_hwnd == 777


def test_click_field_aborts_when_occluded(monkeypatch):
    """遮挡时 fail-closed：不点鼠标，抛 BridgeUnavailableError 带最上层 hwnd。"""
    d = ThsUiDispatcher()
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "click", lambda coords: clicks.append(coords))
    d._last_occluder_hwnd = 777
    monkeypatch.setattr(d, "_ensure_topmost", lambda x, y: False)

    with pytest.raises(BridgeUnavailableError) as ei:
        d._click_field(_FakeField(1032, 316, 141, 121), left_offset=8)
    assert clicks == []
    assert "777" in str(ei.value)


def test_switch_falls_back_to_uia_when_function_key_missed(monkeypatch):
    """功能键未切到目标页 → 降级 UIA 切页；UIA 读不到选中项时保持旧行为。"""
    d = ThsUiDispatcher()
    monkeypatch.setattr(d, "_key", lambda k: None)
    monkeypatch.setattr("core.engine.ths_ui_dispatcher.time.sleep", lambda s: None)
    monkeypatch.setattr(d, "_selected_tree_item", lambda: "资金股票[F4]")
    called = []
    monkeypatch.setattr(d, "_select_page", lambda n: called.append(n) or True)
    assert d._switch("{F1}") is True
    assert called == ["买入"]

    monkeypatch.setattr(d, "_select_page", lambda n: False)
    assert d._switch("{F1}") is False

    monkeypatch.setattr(d, "_selected_tree_item", lambda: None)
    assert d._switch("{F1}") is True


def test_execute_via_ui_releases_ui_lock_during_price_wait(monkeypatch):
    """两段式持锁：4s 价格跟随等待期间不占 UI_AUTO_LOCK（TDX 可插入）。"""
    found = {"order_id": "200009", "price": 10.0, "time": "20261028 09:30:01"}
    d = _ui_mocked_dispatcher(monkeypatch, found)
    events = []
    monkeypatch.setattr(d, "_ui_guard",
                        lambda timeout=None: events.append("acquire") or True)
    monkeypatch.setattr(d, "_release_ui_lock", lambda: events.append("release"))
    d._PRICE_FOLLOW_WAIT = 0.05
    monkeypatch.setattr(
        "core.engine.ths_ui_dispatcher.time.sleep",
        lambda s: events.append("wait") if s == 0.05 else None)

    d._execute_via_ui(_order(stock_code="000001.SZ", quantity=300,
                             price=Decimal("10.0")))
    assert events == ["acquire", "release", "wait", "acquire", "release"]


def test_execute_via_ui_aborts_when_switch_fails(monkeypatch):
    """切页失败（功能键与 UIA 均未命中）→ BridgeUnavailableError，不盲点字段。"""
    d = _ui_mocked_dispatcher(monkeypatch, None)
    monkeypatch.setattr(d, "_switch", lambda k: False)
    clicks = []
    import pywinauto.mouse as ths_mouse
    monkeypatch.setattr(ths_mouse, "click", lambda coords: clicks.append(coords))

    with pytest.raises(BridgeUnavailableError) as ei:
        d._execute_via_ui(_order(stock_code="000001.SZ", quantity=300,
                                 price=Decimal("10.0")))
    assert "切页失败" in str(ei.value) or "切换失败" in str(ei.value)
    assert clicks == []
