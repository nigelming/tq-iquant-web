"""QmtDispatcher 映射测试（伪造 xtquant 模块，不启动 QMT 客户端）。

覆盖：xtquant 缺失时的明确告警、资产/持仓（can_use_volume=T+1 可用）/委托/成交映射、
remark 认领（supports_remark=True）、limit/latest 价格模式与下单常量。
"""
import sys
import types
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.engine.http_bridge_dispatcher import BridgeOrderRejected, BridgeUnavailableError
from core.engine.qmt_dispatcher import QmtDispatcher, normalize_symbol
from tq_iquant_shared.constants import SignalType, TradeType


def _obj(**kwargs):
    return SimpleNamespace(**kwargs)


@pytest.fixture
def fake_xt(monkeypatch):
    """把伪造的 xtquant 装进 sys.modules，并让它可被导入。"""
    state = {}

    class FakeTrader:
        def __init__(self, path, session_id):
            state["path"] = path
            state["session_id"] = session_id
            self.orders = state.get("orders", [])
            self.trades = state.get("trades", [])
            self.asset = state.get("asset")
            self.positions = state.get("positions", [])
            self.next_id = 100

        def start(self):
            state["started"] = True

        def connect(self):
            return state.get("connect_rc", 0)

        def subscribe(self, acc):
            state["subscribed"] = acc

        def stop(self):
            state["stopped"] = True

        def query_stock_asset(self, acc):
            return self.asset

        def query_stock_positions(self, acc):
            return self.positions

        def query_stock_orders(self, acc):
            return self.orders

        def query_stock_trades(self, acc):
            return self.trades

        def order_stock(self, acc, code, otype, vol, ptype, price, strategy, remark):
            state["order_args"] = dict(code=code, otype=otype, vol=vol,
                                       ptype=ptype, price=price, remark=remark)
            return state.get("order_rc", 108)

    class FakeAccount:
        def __init__(self, account, account_type):
            state["account"] = (account, account_type)

    xtconstant = types.ModuleType("xtquant.xtconstant")
    xtconstant.STOCK_BUY = 23
    xtconstant.STOCK_SELL = 24
    xtconstant.FIX_PRICE = 11
    xtconstant.LATEST_PRICE = 5
    xttrader = types.ModuleType("xtquant.xttrader")
    xttrader.XtQuantTrader = FakeTrader
    xttype = types.ModuleType("xtquant.xttype")
    xttype.StockAccount = FakeAccount
    pkg = types.ModuleType("xtquant")
    pkg.xtconstant = xtconstant
    pkg.xttrader = xttrader
    pkg.xttype = xttype

    monkeypatch.setitem(sys.modules, "xtquant", pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xtconstant", xtconstant)
    monkeypatch.setitem(sys.modules, "xtquant.xttrader", xttrader)
    monkeypatch.setitem(sys.modules, "xtquant.xttype", xttype)
    monkeypatch.setattr("core.engine.qmt_dispatcher.xtquant_available", lambda: True)
    return state


def test_normalize_symbol():
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("000001.sh") == "000001.SH"


def test_reports_missing_xtquant(monkeypatch):
    monkeypatch.setattr("core.engine.qmt_dispatcher.xtquant_available", lambda: False)
    d = QmtDispatcher(path="C:/qmt", account="8888")
    assert d.connect() is False
    assert "xtquant" in d.last_error


def test_reports_missing_path_or_account(monkeypatch):
    monkeypatch.setattr("core.engine.qmt_dispatcher.xtquant_available", lambda: True)
    d = QmtDispatcher(path="", account="8888")
    assert d.connect() is False
    assert "userdata" in d.last_error
    d2 = QmtDispatcher(path="C:/qmt", account="")
    assert d2.connect() is False
    assert "资金账号" in d2.last_error


def test_connect_failure_when_client_not_logged_in(fake_xt):
    fake_xt["connect_rc"] = -1
    d = QmtDispatcher(path="C:/qmt", account="8888")
    assert d.connect() is False
    assert "极简模式" in d.last_error


def test_asset_and_positions_mapping(fake_xt):
    fake_xt["asset"] = _obj(total_asset=100000.0, cash=20000.0,
                            market_value=80000.0, frozen_cash=0.0)
    fake_xt["positions"] = [
        _obj(stock_code="000001.SZ", volume=1000, can_use_volume=0, open_price=10.0),
        _obj(stock_code="600000.SH", volume=500, can_use_volume=500, open_price=8.0,
             market_value=4100.0),
    ]
    d = QmtDispatcher(path="C:/qmt", account="8888")
    assert d.get_asset()["available"] == 20000.0
    rows = d.query_positions()
    # can_use_volume=0 是 T+1 合法锁定值，不能回退成 1000
    assert rows[0]["available"] == 0
    assert rows[0]["exchange"] == "SZ"
    assert rows[1]["available"] == 500
    assert rows[1]["exchange"] == "SH"


def test_query_orders_carries_remark_and_order_time(fake_xt):
    ts = int(datetime(2026, 10, 24, 9, 31, 5).timestamp())
    fake_xt["orders"] = [
        _obj(order_id=12345, stock_code="000001.SZ", order_type=23, price=10.5,
             order_volume=1000, traded_volume=1000, order_status=56,
             order_remark="abc123", order_time=ts),
        _obj(order_id=12346, stock_code="600000.SH", order_type=24, price=8.0,
             order_volume=500, traded_volume=0, order_status=50,
             order_remark="", order_time=ts),
    ]
    d = QmtDispatcher(path="C:/qmt", account="8888")
    rows = d.query_orders()
    assert rows[0]["remark"] == "abc123"          # QMT 可回读 remark → 精确认领
    assert rows[0]["direction"] == 48
    assert rows[0]["status"] == 56
    assert rows[0]["insert_date"] == "20261024"
    assert rows[0]["insert_time"] == "093105"
    assert rows[1]["direction"] == 49
    assert rows[1]["status"] == 50


def test_query_deals_maps_trades(fake_xt):
    ts = int(datetime(2026, 10, 24, 9, 31, 5).timestamp())
    fake_xt["trades"] = [
        _obj(stock_code="000001.SZ", order_id=12345, traded_volume=1000,
             traded_price=10.4, traded_amount=10400.0, commission=5.0,
             order_type=23, traded_time=ts),
    ]
    rows = QmtDispatcher(path="C:/qmt", account="8888").query_deals()
    assert rows[0]["volume"] == 1000
    assert rows[0]["price"] == 10.4
    assert rows[0]["trade_date"] == "20261024"
    assert rows[0]["direction"] == 48


def test_place_order_writes_deterministic_remark(fake_xt):
    d = QmtDispatcher(path="C:/qmt", account="8888", price_mode="fix")
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=datetime(2026, 10, 24, 9, 31),
    )
    trade = d.place_order(order)
    args = fake_xt["order_args"]
    assert args["code"] == "000001.SZ"
    assert args["otype"] == 23
    assert args["ptype"] == 11        # 限价
    assert args["remark"] == d.order_id(order)[:20]
    assert trade.quantity == 1000


def test_place_order_uses_latest_price_mode(fake_xt):
    d = QmtDispatcher(path="C:/qmt", account="8888", price_mode="latest")
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="600000",
        trade_type=TradeType.SELL, signal_type=SignalType.CLOSE, signal_name="close_sig",
        quantity=500, price=Decimal("8.0"), bar_time=None,
    )
    d.place_order(order)
    assert fake_xt["order_args"]["ptype"] == 5
    assert fake_xt["order_args"]["otype"] == 24


def test_place_order_rejects_negative_seq(fake_xt):
    fake_xt["order_rc"] = -1
    d = QmtDispatcher(path="C:/qmt", account="8888")
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    with pytest.raises(BridgeOrderRejected):
        d.place_order(order)


def test_place_order_raises_unavailable_when_not_connected(monkeypatch):
    monkeypatch.setattr("core.engine.qmt_dispatcher.xtquant_available", lambda: False)
    d = QmtDispatcher(path="C:/qmt", account="8888")
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    with pytest.raises(BridgeUnavailableError):
        d.place_order(order)
