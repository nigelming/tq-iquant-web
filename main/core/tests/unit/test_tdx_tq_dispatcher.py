"""TdxTqDispatcher 映射逻辑测试（用假 tqcenter 模块，不启动通达信）。

覆盖 Core 契约：委托/持仓/资金字段归一化（通达信字段名与 iQuant 不同）、
下单链路（order_stock → 回查 Wtbh 认领）、无 remark 时的 legacy 匹配所需插入时间，
以及下单失败/半自动确认模式的明确报错。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.engine.http_bridge_dispatcher import BridgeOrderRejected
from core.engine.live.order_machine import OrderStateMachine
from core.engine.tdx_tq_dispatcher import TdxTqDispatcher, normalize_symbol
from tq_iquant_shared.constants import SignalType, TradeType

CONST = SimpleNamespace(
    STOCK_BUY=0, STOCK_SELL=1,
    PRICE_MY=0, PRICE_SJ=1, PRICE_ZTJ=2, PRICE_DTJ=3,
)


def _order(code="000001", flag=0, status=1, price=10.5, vol=1000,
           filled=0, ref="W001"):
    """通达信 query_stock_orders 原始行（字段名照手册）。"""
    return {
        "Wtbh": ref, "Code": code, "BSFlag": flag, "Status": status,
        "WtPrice": price, "WtVol": vol, "CjVol": filled, "CjPrice": price,
    }


class FakeTq:
    """假 tqcenter.tq：记录调用、返回预置数据。"""

    def __init__(self, account_id=1, asset=None, positions=None, orders=None,
                 order_result=0, msg=""):
        self.account_id = account_id
        self.asset = asset or {}
        self.positions = positions or []
        self.orders = orders or []
        self.order_result = order_result
        self.msg = msg
        self.calls = []

    def stock_account(self, account="", account_type=""):
        self.calls.append(("stock_account", account, account_type))
        return self.account_id

    def query_stock_asset(self, aid):
        self.calls.append(("query_stock_asset", aid))
        return self.asset

    def query_stock_positions(self, aid):
        self.calls.append(("query_stock_positions", aid))
        return self.positions

    def query_stock_orders(self, aid):
        self.calls.append(("query_stock_orders", aid))
        return self.orders

    def order_stock(self, **kwargs):
        self.calls.append(("order_stock", kwargs))
        if isinstance(self.order_result, dict):
            return self.order_result
        if self.order_result == 0 and self.msg:
            return {"ErrorId": "0", "Msg": self.msg}
        return self.order_result


@pytest.fixture
def fake(monkeypatch):
    """注入假 tq + 假 tqconst。"""
    tq = FakeTq()
    monkeypatch.setattr("core.engine.tdx_tq_dispatcher.get_tq", lambda: tq)
    monkeypatch.setattr("core.engine.tdx_tq_dispatcher._tqconst", lambda: CONST)
    return tq


def test_normalize_symbol_exchange_suffix():
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("430047") == "430047.BJ"
    assert normalize_symbol("000001.SZ") == "000001.SZ"


def test_connect_fails_with_actionable_error_when_account_handle_invalid(fake):
    fake.account_id = -1
    d = TdxTqDispatcher(account="123456")
    assert d.heartbeat() is False
    assert "账户句柄" in d.last_error
    assert ("stock_account", "123456", "stock") in fake.calls


def test_connect_reports_tdx_not_logged_in(monkeypatch):
    from core.tq.utils import TDXConnectionError

    def boom():
        raise TDXConnectionError("无法加载 tqcenter")

    monkeypatch.setattr("core.engine.tdx_tq_dispatcher.get_tq", boom)
    d = TdxTqDispatcher()
    assert d.heartbeat() is False
    assert "tqcenter" in d.last_error


def test_get_asset_normalizes_field_names(fake):
    fake.asset = {"Asset": 100000.0, "Cash": 20000.0, "MarketValue": 80000.0,
                  "TotalFreeze": 0.0}
    d = TdxTqDispatcher()
    out = d.get_asset()
    assert out["total_asset"] == 100000.0
    assert out["available"] == 20000.0
    assert out["market_value"] == 80000.0
    assert out["frozen"] == 0.0


def test_get_positions_keeps_zero_available(fake):
    """available=0 是 T+1 当日买入的合法锁定值，不能回退成持仓量。"""
    fake.positions = [
        {"Code": "000001", "TotalVol": 1000, "CanUseVol": 0, "Cbj": 10.0},
        {"Code": "600000", "TotalVol": 500, "CanUseVol": 500, "Cbj": 8.0,
         "MarketValue": 4100.0},
    ]
    d = TdxTqDispatcher()
    rows = d.query_positions()
    assert rows[0] == {"instrument": "000001", "exchange": "SZ", "volume": 1000,
                       "available": 0, "cost": 10.0, "price": 0.0, "name": ""}
    assert rows[1]["exchange"] == "SH"
    assert rows[1]["available"] == 500


def test_query_orders_maps_status_and_skips_cancel_rows(fake):
    fake.orders = [
        _order(status=3, filled=1000, ref="W001"),          # 全部成交
        _order(code="600000", flag=1, status=5, ref="W002"),  # 全部撤单(卖出)
        {**_order(ref="W003"), "BSFlag": -1},               # 撤单流水，跳过
    ]
    d = TdxTqDispatcher()
    rows = d.query_orders()
    assert len(rows) == 2
    o = rows[0]
    assert o["source"] == "BRIDGE"
    assert o["remark"] == ""            # 通达信无法写 remark
    assert o["order_ref"] == "W001"
    assert o["direction"] == 48         # 买
    assert o["status"] == 56            # 已成
    assert o["filled_volume"] == 1000
    assert rows[1]["direction"] == 49   # 卖
    assert rows[1]["status"] == 54      # 已撤
    # 非本进程下单的委托无插入时间（状态机据此跳过，避免冒领）
    assert rows[0]["insert_date"] == "" and rows[0]["insert_time"] == ""


def test_place_order_resolves_ref_and_enables_legacy_match(fake):
    d = TdxTqDispatcher(account="123456")
    fake.orders = [_order(status=1, ref="W900")]
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=datetime(2026, 10, 24, 9, 31),
    )
    trade = d.place_order(order)

    call = [c for c in fake.calls if c[0] == "order_stock"][0][1]
    assert call["stock_code"] == "000001.SZ"
    assert call["order_type"] == 0
    assert call["order_volume"] == 1000
    assert call["price_type"] == 0
    assert call["price"] == 10.5

    assert trade is not None
    assert trade.quantity == 1000
    assert trade.price == Decimal("10.5")

    # 本进程下单后：query_orders 带插入时间，query_deals 出累计成交行
    fake.orders = [_order(status=3, filled=1000, ref="W900")]
    row = d.query_orders()[0]
    assert row["order_ref"] == "W900"
    assert row["insert_date"] and row["insert_time"]
    deal = d.query_deals()[0]
    assert deal["volume"] == 1000
    assert deal["amount"] == pytest.approx(10500.0)

    # legacy 模糊匹配（supports_remark=False）能在时间窗内认领该单
    live_order = SimpleNamespace(
        stock_code="000001.SZ", trade_type="buy", quantity=1000,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5),
    )
    assert OrderStateMachine.match_legacy_fuzzy(live_order, [row], set()) == "W900"


def test_place_order_rejects_when_order_not_in_orders_table(fake):
    d = TdxTqDispatcher()
    fake.orders = []
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    fake.order_result = 0
    fake.msg = "已提交"
    with pytest.raises(BridgeOrderRejected) as ei:
        d.place_order(order)
    assert "委托列表未见该单" in str(ei.value)


def test_place_order_flags_half_auto_confirm_mode(fake):
    d = TdxTqDispatcher()
    fake.orders = []
    fake.msg = "待用户确认"
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    with pytest.raises(BridgeOrderRejected) as ei:
        d.place_order(order)
    assert "下单确认" in str(ei.value)


def test_place_order_failure_returns_rejected(fake):
    d = TdxTqDispatcher()
    fake.order_result = -1
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    with pytest.raises(BridgeOrderRejected):
        d.place_order(order)


def test_place_order_error_id_raises(monkeypatch):
    tq = FakeTq(order_result={"ErrorId": "5", "Error": "资金不足"})
    monkeypatch.setattr("core.engine.tdx_tq_dispatcher.get_tq", lambda: tq)
    monkeypatch.setattr("core.engine.tdx_tq_dispatcher._tqconst", lambda: CONST)
    d = TdxTqDispatcher()
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="600000",
        trade_type=TradeType.SELL, signal_type=SignalType.CLOSE, signal_name="close_sig",
        quantity=500, price=Decimal("8.0"), bar_time=None,
    )
    with pytest.raises(BridgeOrderRejected) as ei:
        d.place_order(order)
    assert "资金不足" in str(ei.value)


def test_query_calendar_and_quote_degrade_to_empty(monkeypatch):
    """TQ 数据不可用时不抛异常（交易日历 fail-open，行情返回空）。"""
    d = TdxTqDispatcher()
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: SimpleNamespace(get_history_raw=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no tdx"))),
    )
    assert d.query_calendar() == []
    assert d.query_quote("000001") == []
    assert "unavailable" in d.last_error
