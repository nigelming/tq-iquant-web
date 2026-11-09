"""掘金桥方向归一单测：未知方向必须「不猜」。

背景：``side`` 缺失/无法识别时，旧实现 ``_is_buy`` 返回 False → direction=49（卖），
与 QMT 桥「未知方向不认领」的口径相反。实盘系统里方向猜错比丢单更危险，本文件
锁定「未知 → side="" 且不写 direction」的行为，防未来回退。

main venv 无 gm SDK（项目规则禁止把 gm 装进平台 venv），故先注入空壳
``gm`` / ``gm.api`` / ``gm.enum`` 再按文件路径加载桥模块。
"""
import importlib.util
import pathlib
import sys
import types

import pytest

_BRIDGE_DIR = pathlib.Path(__file__).resolve().parents[1]

_GM_API_NAMES = ("set_token", "set_account_id", "order_volume", "order_cancel", "run",
                 "get_cash", "get_position", "get_execution_reports", "get_orders",
                 "get_trading_dates", "history_n", "MODE_LIVE")
_GM_ENUM_NAMES = ("OrderSide_Buy", "OrderSide_Sell", "OrderType_Limit",
                  "PositionEffect_Open", "PositionEffect_Close")


def _stub_gm():
    gm = types.ModuleType("gm")
    api = types.ModuleType("gm.api")
    enum = types.ModuleType("gm.enum")
    for n in _GM_API_NAMES:
        setattr(api, n, 0 if n == "MODE_LIVE" else (lambda *a, **k: None))
    for n in _GM_ENUM_NAMES:
        setattr(enum, n, n)
    gm.api = api
    gm.enum = enum
    sys.modules.setdefault("gm", gm)
    sys.modules.setdefault("gm.api", api)
    sys.modules.setdefault("gm.enum", enum)


def _load_bridge():
    _stub_gm()
    path = _BRIDGE_DIR / "goldminer_bridge.py"
    spec = importlib.util.spec_from_file_location("goldminer_bridge_under_test",
                                                  str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_bridge()


# ---------------- _side_of：三分结果 ----------------

def test_side_of_known_values(bridge):
    assert bridge._side_of(1) == "buy"
    assert bridge._side_of("1") == "buy"
    assert bridge._side_of("OrderSide_Buy") == "buy"
    assert bridge._side_of(2) == "sell"
    assert bridge._side_of("2") == "sell"
    assert bridge._side_of("OrderSide_Sell") == "sell"


def test_side_of_unknown_never_guesses_sell(bridge):
    for v in (None, 0, "", "garbage", "OrderSide_Unknown", 99):
        assert bridge._side_of(v) == "", "side=%r 必须判未知（空串）" % (v,)


# ---------------- /orders：未知方向不写 direction ----------------

def _state(side, **kw):
    st = {"symbol": "SHSE.600000", "side": side, "price": 10.0, "volume": 100,
          "filled_volume": 0, "filled_amount": 0.0, "status_text": "New",
          "reason": "", "created_at": None}
    st.update(kw)
    return st


def test_order_row_omits_direction_when_side_unknown(bridge):
    for side in (None, 0, "", "garbage"):
        row = bridge._order_row("c-unknown", _state(side), None)
        assert "direction" not in row, "side=%r 不得写 direction" % (side,)
        assert row["side"] == ""


def test_order_row_known_side_maps_to_qmt_direction(bridge):
    buy = bridge._order_row("c-buy", _state(1), None)
    assert buy["direction"] == 48
    assert buy["side"] == "buy"
    sell = bridge._order_row("c-sell", _state("OrderSide_Sell"), None)
    assert sell["direction"] == 49
    assert sell["side"] == "sell"


# ---------------- /deals：未知方向不写 direction ----------------

def test_query_deals_unknown_side_omits_direction(bridge, monkeypatch):
    class _Rep(object):
        exec_type = 15
        side = None
        symbol = "SHSE.600000"
        cl_ord_id = "d-unknown"
        exec_id = "t-1"
        price = 10.0
        volume = 100
        filled_amount = 1000.0
        commission = 0.5
        created_at = "2026-11-07 10:00:00"

    monkeypatch.setattr(bridge, "_today_reports", lambda: [_Rep()])
    res = bridge.query_deals({})
    assert res["ok"] is True
    row = res["data"][0]
    assert "direction" not in row
    assert row["side"] == ""


# ---------------- /order：参数缺失/非法直接拒，不默认 buy/sell、不送 0 ----------------

@pytest.mark.parametrize("payload, keyword", [
    ({"code": "", "op": "buy", "volume": 100, "price": 10.0}, "code"),
    ({"code": "600000.SH", "volume": 100, "price": 10.0}, "op"),
    ({"code": "600000.SH", "op": "short", "volume": 100, "price": 10.0}, "op"),
    ({"code": "600000.SH", "op": "buy", "volume": 0, "price": 10.0}, "volume"),
    ({"code": "600000.SH", "op": "buy", "price": 10.0}, "volume"),
    ({"code": "600000.SH", "op": "buy", "volume": 100, "price": 0}, "price"),
    ({"code": "600000.SH", "op": "buy", "volume": 100}, "price"),
])
def test_place_order_rejects_missing_or_invalid_fields(bridge, payload, keyword):
    res = bridge.place_order(payload)
    assert res["ok"] is False
    assert res["data"]["status"] == "Invalid"
    assert keyword in res["error"]
    assert bridge._ledger_all() == [], "非法参数不得进台账"
