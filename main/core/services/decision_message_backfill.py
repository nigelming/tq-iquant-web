"""存量决策事件 message 精度回填（一次性工具）。

背景
----
决策事件的 message 在回测/实盘记录时快照入库，不随代码变更重算。2026-10-11
之前（22:37 前）生成的记录，message 内嵌价格/金额/百分比为原始浮点（如
「最高 7.26000000」），与统一精度后的 fmt_* 输出不一致。本模块提供
reformat_message()：按已知闸门模板解析旧文案并用 fmt_* 重排（含千分位），
供 scripts/backfill_decision_messages.py 逐行回写。
"""
import re
from decimal import Decimal, InvalidOperation

from core.engine.format import fmt_money, fmt_price, fmt_pct, fmt_qty

_NUM = r"([+-]?\d[\d,]*(?:\.\d+)?)"


def _dec(s):
    try:
        return Decimal(str(s).strip().replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


_LABELS = {"stop_loss": "止损", "take_profit": "止盈", "trailing_stop": "移动止损"}


def _render_risk_price(gate, cost, price, pct, verb):
    """止损/止盈共用：价格 fmt_price、百分比 fmt_pct。"""
    c, p, k = _dec(cost), _dec(price), _dec(pct)
    if c is None or p is None or k is None:
        return None
    return "%s：成本 %s 现价 %s %s %s" % (
        _LABELS[gate], fmt_price(c), fmt_price(p), verb, fmt_pct(k / 100))


def reformat_message(gate, message):
    """按闸门模板把旧 message 重排为统一精度；无法解析/不适用时原样返回。

    已知模板（与 current 代码文案一致，仅数字精度不同）：
    - stop_loss    止损：成本 # 现价 # 亏 #%
    - take_profit  止盈：成本 # 现价 # 盈 #%
    - trailing_stop 移动止损：最高 # 现价 # 回撤 #%
    - daily_loss   日内盈亏 #（基准 #）亏损 #% 触发当日暂停
    - order_shrunk 资金不足缩量：请求 # 股，按可用现金 # 缩至 # 股 @#
    """
    if not message:
        return message
    try:
        if gate in ("stop_loss", "take_profit", "trailing_stop"):
            if gate == "trailing_stop":
                m = re.match(
                    r"^移动止损：最高 %s 现价 %s 回撤 %s%%$" % (_NUM, _NUM, _NUM), message)
                if not m:
                    return message
                high, price, pct = m.groups()
                h, p, k = _dec(high), _dec(price), _dec(pct)
                if h is None or p is None or k is None:
                    return message
                return "移动止损：最高 %s 现价 %s 回撤 %s" % (
                    fmt_price(h), fmt_price(p), fmt_pct(k / 100))
            verb = "亏" if gate == "stop_loss" else "盈"
            tmpl = {
                "stop_loss": r"^止损：成本 %s 现价 %s 亏 %s%%$" % (_NUM, _NUM, _NUM),
                "take_profit": r"^止盈：成本 %s 现价 %s 盈 %s%%$" % (_NUM, _NUM, _NUM),
            }[gate]
            m = re.match(tmpl, message)
            if not m:
                return message
            out = _render_risk_price(gate, m.group(1), m.group(2), m.group(3), verb)
            return message if out is None else out
        if gate == "daily_loss":
            m = re.match(r"^日内盈亏 %s（基准 %s）亏损 %s%% 触发当日暂停$" % (_NUM, _NUM, _NUM), message)
            if not m:
                return message
            amt, base, pct = m.groups()
            a, b, k = _dec(amt), _dec(base), _dec(pct)
            if a is None or b is None or k is None:
                return message
            return "日内盈亏 %s（基准 %s）亏损 %s 触发当日暂停" % (
                fmt_money(a), fmt_money(b), fmt_pct(k / 100))
        if gate == "order_shrunk":
            m = re.match(
                r"^资金不足缩量：请求 %s 股，按可用现金 %s 缩至 %s 股 @%s$"
                % (_NUM, _NUM, _NUM, _NUM), message)
            if not m:
                return message
            req, cash, fin, price = m.groups()
            q1, csh, q2, p = (_dec(req), _dec(cash), _dec(fin), _dec(price))
            if q1 is None or csh is None or q2 is None or p is None:
                return message
            return "资金不足缩量：请求 %s 股，按可用现金 %s 缩至 %s 股 @%s" % (
                fmt_qty(q1), fmt_money(csh), fmt_qty(q2), fmt_price(p))
        return message
    except Exception:
        return message
