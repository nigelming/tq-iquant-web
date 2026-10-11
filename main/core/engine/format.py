"""数值精度助手（显示层）：统一金额/价格/数量/百分比的取舍。

仅用于 message 文案与展示，不改变 API 数值字段的原始精度。
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DASH = "—"


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _quantize(v, places: int) -> Optional[Decimal]:
    d = _dec(v)
    if d is None:
        return None
    q = Decimal(1).scaleb(-places)
    return d.quantize(q, rounding=ROUND_HALF_UP)


def fmt_money(v) -> str:
    d = _quantize(v, 2)
    return DASH if d is None else "{:,}".format(d)


def fmt_price(v) -> str:
    d = _quantize(v, 3)
    return DASH if d is None else format(d, "f")


def fmt_qty(v) -> str:
    d = _quantize(v, 0)
    return DASH if d is None else "{:,}".format(int(d))


def fmt_pct(v) -> str:
    d = _dec(v)
    if d is None:
        return DASH
    p = (d * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(p, "f") + "%"