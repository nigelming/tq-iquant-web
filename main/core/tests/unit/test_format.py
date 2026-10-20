from decimal import Decimal

from core.engine.format import fmt_money, fmt_price, fmt_qty, fmt_pct


def test_fmt_money_rounds_and_thousands():
    assert fmt_money(Decimal("1369882.7275000000")) == "1,369,882.73"
    assert fmt_money(-62322.46875) == "-62,322.47"
    assert fmt_money(0) == "0.00"


def test_fmt_price_three_decimals():
    assert fmt_price(Decimal("7.42")) == "7.420"
    assert fmt_price(6.589) == "6.589"


def test_fmt_qty_integer_thousands():
    assert fmt_qty(27100) == "27,100"
    assert fmt_qty(Decimal("76300.0")) == "76,300"


def test_fmt_pct_ratio_to_percent():
    assert fmt_pct(Decimal("0.062275")) == "6.23%"
    assert fmt_pct(0.05) == "5.00%"


def test_none_and_garbage_return_dash():
    assert fmt_money(None) == "—"
    assert fmt_price(None) == "—"
    assert fmt_qty(None) == "—"
    assert fmt_pct(None) == "—"
    assert fmt_money("not-a-number") == "—"
