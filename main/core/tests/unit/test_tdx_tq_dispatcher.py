"""TdxTqDispatcher 映射逻辑测试（用假 tqcenter 模块，不启动通达信）。

覆盖 Core 契约：委托/持仓/资金字段归一化（通达信字段名与 iQuant 不同）、
下单链路（order_stock → 回查 Wtbh 认领）、无 remark 时的 legacy 匹配所需插入时间，
以及下单失败/半自动确认模式的明确报错。
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from core.engine.http_bridge_dispatcher import BridgeOrderRejected
from core.engine.live.order_machine import OrderStateMachine
from core.engine.live.timing import now_shanghai
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
                 order_result=0, msg="", subscribed=None, cancel_result=None):
        self.account_id = account_id
        self.asset = asset or {}
        self.positions = positions or []
        self.orders = orders or []
        self.order_result = order_result
        self.msg = msg
        self.subscribed = subscribed
        # A-1：撤单返回值（SDK 形态：dict ErrorId="0"=成功 / -1=失败）
        self.cancel_result = cancel_result if cancel_result is not None else {"ErrorId": "0"}
        # A-5：按轮返回委托表（模拟「下单后新单延迟出现在委托表」）；None=用 self.orders
        self.orders_rounds = None
        self._round = 0
        self.calls = []

    def stock_account(self, account="", account_type=""):
        self.calls.append(("stock_account", account, account_type))
        return self.account_id

    def get_subscribe_hq_stock_list(self):
        self.calls.append(("get_subscribe_hq_stock_list",))
        if isinstance(self.subscribed, Exception):
            raise self.subscribed
        return list(self.subscribed or [])

    def query_stock_asset(self, aid):
        self.calls.append(("query_stock_asset", aid))
        return self.asset

    def query_stock_positions(self, aid):
        self.calls.append(("query_stock_positions", aid))
        return self.positions

    def query_stock_orders(self, aid):
        self.calls.append(("query_stock_orders", aid))
        if self.orders_rounds is not None:
            rows = self.orders_rounds[min(self._round, len(self.orders_rounds) - 1)]
            self._round += 1
            return rows
        return self.orders

    def cancel_order_stock(self, **kwargs):
        self.calls.append(("cancel_order_stock", kwargs))
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result
        return self.cancel_result

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


@pytest.fixture
def no_quote(monkeypatch):
    """隔离真实 TQ 行情：涨跌停校验取不到前收 → fail-open 跳过。

    本机通达信可能正在运行，不隔离会让 place_order 单测依赖真实行情
    （前收恰在涨跌停边界时机器相关地失败）。
    """
    monkeypatch.setattr("core.tq.data.TQData", lambda: FakeQuoteData(raw={}))


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
    # A-5（行为变更）：非本进程下单的委托也补记当前上海时间（重启单/手工单兜底
    # 窗口匹配可用），不再留空——留空会让状态机永远无法绑定该 ref → 180s 超时丢单。
    assert rows[0]["insert_date"] and rows[0]["insert_time"]


def test_place_order_resolves_ref_and_enables_legacy_match(fake, no_quote):
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


def test_place_order_rejects_when_order_not_in_orders_table(fake, no_quote):
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


def test_place_order_flags_half_auto_confirm_mode(fake, no_quote):
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


def test_place_order_failure_returns_rejected(fake, no_quote):
    d = TdxTqDispatcher()
    fake.order_result = -1
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=None,
    )
    with pytest.raises(BridgeOrderRejected):
        d.place_order(order)


def test_place_order_error_id_raises(monkeypatch, no_quote):
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


# ---------------- TQ 1.3.1：交易日历（权威 get_trading_dates + 日线反推回退） ----------------

class FakeTQData:
    """假 TQData：预置 get_trading_dates / get_history_raw / unsubscribe_bars 行为。"""

    def __init__(self, trading_dates=None, history_raw=None,
                 raise_dates=False, raise_history=False):
        self._trading_dates = trading_dates
        self._history_raw = history_raw
        self._raise_dates = raise_dates
        self._raise_history = raise_history
        self.cal_args = None
        self.history_calls = []
        self.unsub_calls = []

    def get_trading_dates(self, market="SH", start="", end=""):
        self.cal_args = (market, start, end)
        if self._raise_dates:
            raise RuntimeError("calendar boom")
        return list(self._trading_dates or [])

    def get_history_raw(self, stocks, periods, **kw):
        self.history_calls.append((list(stocks), list(periods), kw))
        if self._raise_history:
            raise RuntimeError("history boom")
        return self._history_raw or {}

    def unsubscribe_bars(self, stocks):
        self.unsub_calls.append(list(stocks))


class FakeQuoteData:
    """假 TQData（仅行情）：get_history_raw 返回预置 raw 或抛错。"""

    def __init__(self, raw=None, error=None):
        self._raw = raw
        self._error = error
        self.history_calls = []

    def get_history_raw(self, stocks, periods, **kw):
        self.history_calls.append((list(stocks), list(periods), kw))
        if self._error is not None:
            raise self._error
        return self._raw or {}


def test_query_calendar_prefers_trading_dates(monkeypatch):
    """get_trading_dates 命中 → 直接返回，不再走 000001.SH 日线反推。"""
    tqd = FakeTQData(trading_dates=["20261023", "20261026"], raise_history=True)
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    assert d.query_calendar() == ["20261023", "20261026"]
    assert tqd.history_calls == []          # 未走日线反推回退
    market, start, end = tqd.cal_args
    assert market == "SH"
    assert len(start) == 8 and len(end) == 8 and start < end


def test_query_calendar_falls_back_to_daily_index(monkeypatch):
    """get_trading_dates 空 → 回退 000001.SH 日线反推（1.3.1 前口径）。"""
    idx = [pd.Timestamp("2026-10-21"), pd.Timestamp("2026-10-22"),
           pd.Timestamp("2026-10-23")]
    raw = {"1d": {"Close": pd.DataFrame({"000001.SH": [1.0, 1.1, 1.2]}, index=idx)}}
    tqd = FakeTQData(trading_dates=[], history_raw=raw)
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    assert d.query_calendar() == ["20261021", "20261022", "20261023"]
    assert tqd.history_calls and tqd.history_calls[0][0] == ["000001.SH"]


def test_query_calendar_all_fail_returns_empty(monkeypatch):
    """两层都失败 → [] 且 last_error（fail-open 契约不变）。"""
    tqd = FakeTQData(raise_dates=True, raise_history=True)
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    assert d.query_calendar() == []
    assert "unavailable" in d.last_error


# ---------------- TQ 1.3.1：close() 退订对账 ----------------

def test_close_unsubscribes_current_subscriptions(fake, monkeypatch, caplog):
    """订阅列表非空 → 整表退订 + info 日志。

    前提：平台全局限 1 个运行 session（B6），当前订阅必属本会话，不会误伤。
    """
    fake.subscribed = ["000001.SZ", "600000.SH"]
    tqd = FakeTQData()
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    d._connected = True
    with caplog.at_level(logging.INFO, logger="core.engine.tdx_tq_dispatcher"):
        d.close()
    assert tqd.unsub_calls == [["000001.SZ", "600000.SH"]]
    assert d._connected is False
    assert any(r.levelno == logging.INFO and "退订" in r.message
               for r in caplog.records)


def test_close_empty_subscription_skips_unsubscribe(fake, monkeypatch):
    """订阅列表空 → 不调 unsubscribe_bars。"""
    fake.subscribed = []
    tqd = FakeTQData()
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    d.close()
    assert tqd.unsub_calls == []
    assert d._connected is False


def test_close_swallows_subscription_query_error(fake, monkeypatch):
    """查订阅列表异常 → 吞并（debug 日志），不抛、不退订。"""
    fake.subscribed = RuntimeError("bridge down")
    tqd = FakeTQData()
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    d._connected = True
    d.close()  # 不抛
    assert tqd.unsub_calls == []
    assert d._connected is False


def test_close_swallows_unsubscribe_error(fake, monkeypatch):
    """unsubscribe_bars 异常 → 吞并，不抛。"""
    fake.subscribed = ["000001.SZ"]
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: SimpleNamespace(
            unsubscribe_bars=lambda stocks: (_ for _ in ()).throw(RuntimeError("unsub boom"))),
    )
    d = TdxTqDispatcher()
    d.close()  # 不抛


# ---------------- TQ 1.3.1：check_price_limit 涨跌停前置校验（引擎接线口径）----------------
# Fix1 口径统一：校验从 place_order 内联上移为公共方法（引擎按
# price_limit_check_enabled 门控调用，四通道同签名），place_order 不再内联校验。

def _raw_1d_close(symbol, closes):
    """1d 原始行情（仅 Close 帧，prev_close 计算够用）。

    最新 bar 固定为今日（新鲜度校验要求最新 bar == 今天，写死历史日期会被
    当成数据滞后跳过校验）。
    """
    today = now_shanghai().date()
    idx = [pd.Timestamp(today - timedelta(days=1)), pd.Timestamp(today)]
    return {"1d": {"Close": pd.DataFrame({symbol: closes}, index=idx)}}


def _limit_order(code="000001", trade_type=TradeType.BUY, price=Decimal("11.00")):
    return SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code=code,
        trade_type=trade_type, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=price, bar_time=None,
    )


def test_check_price_limit_blocks_buy_at_limit_up(fake, monkeypatch):
    """限价买 ≥ 涨停价（前收 10 × 1.10 = 11.00）→ 返回拒绝原因，不触 order_stock。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    d = TdxTqDispatcher()
    reason = d.check_price_limit("000001", "buy", Decimal("11.00"))
    assert reason is not None
    assert "涨停价" in reason
    assert "11.00" in reason
    assert not [c for c in fake.calls if c[0] == "order_stock"]


def test_check_price_limit_blocks_sell_at_limit_down(fake, monkeypatch):
    """限价卖 ≤ 跌停价（前收 10 × 0.90 = 9.00）→ 返回拒绝原因，不触 order_stock。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    d = TdxTqDispatcher()
    reason = d.check_price_limit("000001", "sell", Decimal("9.00"))
    assert reason is not None
    assert "跌停价" in reason
    assert "9.00" in reason
    assert not [c for c in fake.calls if c[0] == "order_stock"]


def test_place_order_buy_below_limit_up_passes(fake, monkeypatch):
    """限价买 < 涨停价 → 校验通过（None）且正常下单。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    fake.orders = [_order(price=10.99, ref="W801")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", Decimal("10.99")) is None
    trade = d.place_order(_limit_order(price=Decimal("10.99")))
    assert trade is not None
    assert [c for c in fake.calls if c[0] == "order_stock"]


def test_place_order_sell_above_limit_down_passes(fake, monkeypatch):
    """限价卖 > 跌停价 → 校验通过（None）且正常下单。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    fake.orders = [_order(flag=1, price=9.01, ref="W802")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "sell", Decimal("9.01")) is None
    trade = d.place_order(
        _limit_order(trade_type=TradeType.SELL, price=Decimal("9.01")))
    assert trade is not None
    assert [c for c in fake.calls if c[0] == "order_stock"]


def test_place_order_no_inline_limit_check(fake, monkeypatch):
    """防双查：涨停价限价单 place_order 直接放行（校验已上移引擎提交链，
    由 price_limit_check_enabled 配置门控调用 check_price_limit）。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    fake.orders = [_order(price=11.00, ref="W807")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", Decimal("11.00")) is not None
    trade = d.place_order(_limit_order(price=Decimal("11.00")))
    assert trade is not None
    assert [c for c in fake.calls if c[0] == "order_stock"]


def test_check_price_limit_market_price_skips(fake, monkeypatch):
    """市价单（price 空/非正，成交价由交易所决定）不做涨跌停校验 → None。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", None) is None
    assert d.check_price_limit("000001", "buy", 0) is None


def test_check_price_limit_quote_error_fail_open(fake, monkeypatch):
    """行情查询异常 → 返回 None（fail-open，校验故障绝不阻断下单）。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(error=RuntimeError("no tdx")))
    fake.orders = [_order(price=11.00, ref="W804")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", Decimal("11.00")) is None
    trade = d.place_order(_limit_order(price=Decimal("11.00")))
    assert trade is not None


def test_check_price_limit_distorted_prev_close_fail_open(fake, monkeypatch):
    """前复权早期失真（前收 < 1 元）→ 返回 None（与回测同口径，宁宽勿错）。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [0.4, 0.5])))
    fake.orders = [_order(price=11.00, ref="W805")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", Decimal("11.00")) is None
    trade = d.place_order(_limit_order(price=Decimal("11.00")))
    assert trade is not None


def test_check_price_limit_stale_bar_skips(fake, monkeypatch):
    """最新 bar=昨日（数据滞后）→ 前收无效，返回 None（不拒单，fail-open）。"""
    today = now_shanghai().date()
    idx = [pd.Timestamp(today - timedelta(days=2)),
           pd.Timestamp(today - timedelta(days=1))]
    raw = {"1d": {"Close": pd.DataFrame({"000001.SZ": [10.0, 10.0]}, index=idx)}}
    monkeypatch.setattr(
        "core.tq.data.TQData", lambda: FakeQuoteData(raw))
    fake.orders = [_order(price=11.00, ref="W806")]
    d = TdxTqDispatcher()
    assert d.check_price_limit("000001", "buy", Decimal("11.00")) is None
    trade = d.place_order(_limit_order(price=Decimal("11.00")))
    assert trade is not None
    assert [c for c in fake.calls if c[0] == "order_stock"]


def test_check_price_limit_fresh_bar_still_blocks_limit_up(fake, monkeypatch):
    """最新 bar=今日（数据新鲜）→ 涨跌停校验照常生效（拒单口径不变）。"""
    monkeypatch.setattr(
        "core.tq.data.TQData",
        lambda: FakeQuoteData(_raw_1d_close("000001.SZ", [10.0, 10.0])))
    d = TdxTqDispatcher()
    reason = d.check_price_limit("000001", "buy", Decimal("11.00"))
    assert reason is not None
    assert "涨停价" in reason
    assert not [c for c in fake.calls if c[0] == "order_stock"]


# ---------------- TQ 1.3.1：批量行情 query_quotes_batch ----------------

def _raw_1m_multi():
    """1m 原始行情：两股票 × Close/Open/Volume 帧。"""
    idx = [pd.Timestamp("2026-10-24 09:31:00"), pd.Timestamp("2026-10-24 09:32:00")]

    def frame(data):
        return pd.DataFrame(data, index=idx)

    return {"1m": {
        "Close": frame({"000001.SZ": [10.0, 10.1], "600000.SH": [8.0, 8.1]}),
        "Open": frame({"000001.SZ": [9.9, 10.0], "600000.SH": [7.9, 8.0]}),
        "Volume": frame({"000001.SZ": [100, 200], "600000.SH": [300, 400]}),
    }}


def test_supports_batch_quote_flag():
    """仅 TdxTqDispatcher 声明 supports_batch_quote=True。"""
    assert TdxTqDispatcher.supports_batch_quote is True


def test_query_quotes_batch_shape(monkeypatch):
    tqd = FakeQuoteData(_raw_1m_multi())
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    out = d.query_quotes_batch(["000001.SZ", "600000.SH"], period="1m", count=10)
    assert set(out.keys()) == {"000001.SZ", "600000.SH"}
    b = out["000001.SZ"]
    assert [x["stime"] for x in b] == ["20261024093100", "20261024093200"]
    assert b[0]["close"] == pytest.approx(10.0)
    assert b[1]["close"] == pytest.approx(10.1)
    assert b[0]["open"] == pytest.approx(9.9)
    assert b[0]["volume"] == pytest.approx(100)
    assert out["600000.SH"][1]["close"] == pytest.approx(8.1)
    # 一次批量 SDK 调用，两只股票一起取
    assert tqd.history_calls[0][0] == ["000001.SZ", "600000.SH"]
    assert tqd.history_calls[0][1] == ["1m"]


def test_query_quotes_batch_filters_invalid_codes(monkeypatch):
    """无效代码剔除（不进 SDK 调用），结果键保留原始入参。"""
    tqd = FakeQuoteData(_raw_1m_multi())
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    out = d.query_quotes_batch(["000001", "12", "xyz"], period="1m")
    assert set(out.keys()) == {"000001"}
    assert tqd.history_calls[0][0] == ["000001.SZ"]


def test_query_quotes_batch_error_returns_empty(monkeypatch):
    """批量查询异常 → {} + last_error（BarPoller 据此回退逐只）。"""
    tqd = FakeQuoteData(error=RuntimeError("no tdx"))
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    assert d.query_quotes_batch(["000001.SZ"]) == {}
    assert "batch" in d.last_error


def test_query_quote_consistent_with_batch(monkeypatch):
    """单股 query_quote 与批量结果同转换规则（共用 _raw_to_bars）。"""
    tqd = FakeQuoteData(_raw_1m_multi())
    monkeypatch.setattr("core.tq.data.TQData", lambda: tqd)
    d = TdxTqDispatcher()
    one = d.query_quote("000001", period="1m", count=10)
    batch = d.query_quotes_batch(["000001"], period="1m", count=10)
    assert one == batch["000001"]


# ---------------- A-1：cancel_order（真实撤单） ----------------

def _connected_dispatcher(**kw):
    """跳过 _ensure 的真连接（单测直连假 tq）。"""
    d = TdxTqDispatcher(**kw)
    d._connected = True
    d._account_id = 1
    return d


def test_cancel_order_success_returns_true(fake):
    """SDK 返回 dict(ErrorId="0") → True，参数带后缀代码。"""
    d = _connected_dispatcher()
    assert d.cancel_order("W001", "000001") is True
    call = [c for c in fake.calls if c[0] == "cancel_order_stock"][0][1]
    assert call == {"account_id": 1, "stock_code": "000001.SZ", "order_id": "W001"}


def test_cancel_order_minus_one_returns_false_with_error(fake):
    """SDK 返回 -1（柜台失败/参数缺失）→ False + last_error。"""
    fake.cancel_result = -1
    d = _connected_dispatcher()
    assert d.cancel_order("W001", "000001.SZ") is False
    assert d.last_error


def test_cancel_order_error_id_dict_returns_false(fake):
    """SDK 返回 dict(ErrorId≠0) → False + last_error（防 SDK 形态变化）。"""
    fake.cancel_result = {"ErrorId": "6", "Msg": "连接已断开"}
    d = _connected_dispatcher()
    assert d.cancel_order("W001", "000001.SZ") is False
    assert "连接已断开" in d.last_error


def test_cancel_order_connection_error_propagates(monkeypatch):
    """TDXConnectionError（客户端离线）向上抛，不吞。"""
    from core.tq.utils import TDXConnectionError

    def boom():
        raise TDXConnectionError("无法加载 tqcenter")

    monkeypatch.setattr("core.engine.tdx_tq_dispatcher.get_tq", boom)
    d = _connected_dispatcher()
    with pytest.raises(TDXConnectionError):
        d.cancel_order("W001", "000001.SZ")


# ---------------- A-5：_resolve_order_id 认领加固 ----------------

def test_get_orders_merges_known_refs_and_backfills_submit_times(fake):
    """A-5：get_orders 返回后 refs 并入 _known_refs，缺 _submit_times 的 ref 用当前上海时间补记。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W001"), _order(code="600000", ref="W002")]
    d.get_orders()
    assert d._known_refs == {"W001", "W002"}
    assert set(d._submit_times) == {"W001", "W002"}
    date_s, time_s = d._submit_times["W001"]
    assert date_s == now_shanghai().strftime("%Y%m%d")
    assert len(time_s) == 6


def test_resolve_prefers_new_ref_over_known_same_shape(fake):
    """A-5：同形状候选中优先认领本轮新增 ref（预置 known 的旧单不冒领）。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W100")]
    d.get_orders()  # 预热：W100 进入 _known_refs（模拟此前轮询已见过）
    fake.orders_rounds = [
        [_order(ref="W100"), _order(ref="W200")],
    ]
    ref = d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1)
    assert ref == "W200"
    assert "W200" in d._claimed_refs


def test_resolve_skips_claimed_refs(fake):
    """A-5：已被本进程其它单认领的 ref 不重复认领。"""
    d = _connected_dispatcher()
    d._claimed_refs.add("W100")
    fake.orders = [_order(ref="W100")]
    assert d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1) == ""


def test_resolve_falls_back_to_known_unclaimed_ref(fake):
    """A-5：全部 attempts 无新增 ref 命中 → 放宽为形状匹配且未认领的任意 ref（兜底）。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W100")]
    d.get_orders()  # W100 已知（非本轮新增）
    ref = d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1)
    assert ref == "W100"
    assert "W100" in d._claimed_refs


def test_resolve_market_mode_skips_price_filter(fake):
    """A-5：市价模式（latest→PRICE_SJ）委托表 WtPrice 可能为 0/非传入价 → 跳过价格过滤。"""
    d = _connected_dispatcher(price_mode="latest")
    fake.orders = [_order(price=0.0, ref="W300")]
    ref = d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1)
    assert ref == "W300"


def test_resolve_limit_mode_keeps_price_filter(fake):
    """A-5：自填价模式（my→PRICE_MY）保持价格相等过滤（错价旧单不认领）。"""
    d = _connected_dispatcher(price_mode="my")
    fake.orders = [_order(price=9.9, ref="W301")]
    assert d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1) == ""


def test_resolve_known_refs_survive_reconnect(fake):
    """A-5：connect() 重连不清空 _claimed_refs/_known_refs（进程内防重放）。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W100")]
    d.get_orders()
    d._claimed_refs.add("W100")
    d.connect()  # fake account_id=1 → 重连成功
    assert d._known_refs == {"W100"}
    assert "W100" in d._claimed_refs


def test_query_orders_backfills_insert_time_for_foreign_refs(fake):
    """A-5（行为变更）：非本进程下单的委托也补记当前上海时间——重启单/手工单兜底认领后
    状态机 legacy 窗口匹配可用（旧行为留空 → 状态机永远无法绑定 → 180s 超时丢单）。"""
    fake.orders = [_order(status=3, filled=1000, ref="W001")]
    d = _connected_dispatcher()
    rows = d.query_orders()
    assert rows[0]["insert_date"] and rows[0]["insert_time"]


# ---------------- I-2：陌生 ref 补记时间标 fabricated，匹配降级 ----------------

def test_get_orders_marks_fabricated_refs(fake):
    """I-2：纯陌生 ref（_submit_times 没有）的补记时间标 fabricated；
    已有时间的 ref（本进程 place_order / A-4 recover 补记）不标。"""
    d = _connected_dispatcher()
    d._submit_times["W900"] = ("20261027", "093000")  # 预置真实时间
    fake.orders = [_order(ref="W001"), _order(ref="W900")]
    d.get_orders()
    assert d._fabricated_refs == {"W001"}
    assert "W900" not in d._fabricated_refs


def test_query_orders_rows_carry_insert_fabricated_flag(fake):
    """I-2：query_orders 每行带 insert_fabricated，状态机据此拒绝 fabricated 候选。"""
    fake.orders = [_order(status=3, filled=1000, ref="W001")]
    d = _connected_dispatcher()
    rows = d.query_orders()
    assert rows[0]["insert_fabricated"] is True
    assert rows[0]["insert_date"] and rows[0]["insert_time"]


def test_resolve_fallback_prefers_real_time_over_fabricated(fake):
    """I-2：兜底段真实时间候选优先，fabricated（遗留单/手工单）沉底——仅无真实候选时才用。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W100"), _order(ref="W101")]
    d._submit_times["W101"] = ("20261027", "100000")  # 真实时间（本进程单未完结）
    d.get_orders()  # W100 陌生 → fabricated；W101 已有真实时间
    ref = d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1)
    assert ref == "W101"
    assert "W100" not in d._claimed_refs


def test_resolve_fallback_uses_fabricated_as_last_resort(fake):
    """I-2：只有 fabricated 候选时兜底仍可用（既有行为保留，但排在真实候选之后）。"""
    d = _connected_dispatcher()
    fake.orders = [_order(ref="W100")]
    d.get_orders()  # W100 fabricated
    ref = d._resolve_order_id("000001.SZ", "buy", 10.5, 1000, attempts=1)
    assert ref == "W100"
    assert "W100" in d._claimed_refs


def test_place_order_clears_fabricated_flag_on_claim(fake, no_quote):
    """I-2：place_order 认领的 ref 清除 fabricated 标记（resolve 回查期间 get_orders
    可能已把它当陌生 ref 补记），本进程单的 legacy 匹配不受降级影响。"""
    d = TdxTqDispatcher(account="123456")
    fake.orders = [_order(status=1, ref="W900")]
    order = SimpleNamespace(
        strategy_id=1, portfolio_id=2, stock_code="000001",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN, signal_name="open_sig",
        quantity=1000, price=Decimal("10.5"), bar_time=datetime(2026, 10, 27, 9, 31),
    )
    trade = d.place_order(order)
    assert trade is not None
    assert "W900" not in d._fabricated_refs
    row = d.query_orders()[0]
    assert row["insert_fabricated"] is False


def test_match_legacy_fuzzy_rejects_fabricated_row():
    """I-2：状态机 legacy 匹配拒绝 insert_fabricated 候选（时间窗校验对补记时间无意义），
    真实时间候选正常绑定。"""
    live_order = SimpleNamespace(
        stock_code="000001.SZ", trade_type="buy", quantity=1000,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5),
    )
    fabricated = {
        "source": "BRIDGE", "instrument": "000001", "exchange": "SZ",
        "direction": 48, "volume": 1000, "order_ref": "W100",
        "insert_date": now_shanghai().strftime("%Y%m%d"),
        "insert_time": now_shanghai().strftime("%H%M%S"),
        "insert_fabricated": True,
    }
    real = {
        "source": "BRIDGE", "instrument": "000001", "exchange": "SZ",
        "direction": 48, "volume": 1000, "order_ref": "W200",
        "insert_date": now_shanghai().strftime("%Y%m%d"),
        "insert_time": now_shanghai().strftime("%H%M%S"),
        "insert_fabricated": False,
    }
    assert OrderStateMachine.match_legacy_fuzzy(live_order, [fabricated], set()) is None
    assert OrderStateMachine.match_legacy_fuzzy(live_order, [real], set()) == "W200"
