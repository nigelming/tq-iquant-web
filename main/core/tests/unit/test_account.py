from decimal import Decimal

from core.engine.account import Account
from core.engine.event import TradeEvent
from tq_iquant_shared.constants import TradeType

def _trade(trade_type, price, quantity):
    return TradeEvent(
        strategy_id=1,
        portfolio_id=1,
        stock_code="000001.SZ",
        trade_type=trade_type,
        price=Decimal(price),
        quantity=quantity,
        amount=Decimal(price) * quantity,
        commission=Decimal("5"),
        stamp_duty=Decimal("0"),
        trade_time=None,
    )


def test_apply_trade_buy_deducts_cash_with_commission():
    acc = Account(Decimal("100000"))
    acc.apply_trade(_trade(TradeType.BUY, "10", 1000))
    # 金额 10000 + 佣金 5 = 10005
    assert acc.cash == Decimal("100000") - Decimal("10005")


def test_apply_trade_sell_adds_cash_minus_fees():
    acc = Account(Decimal("100000"))
    trade = _trade(TradeType.SELL, "12", 1000)
    trade.stamp_duty = Decimal("6")  # 12000 * 0.0005
    acc.apply_trade(trade)
    # 金额 12000 - 佣金 5 - 印花税 6 = 11989
    assert acc.cash == Decimal("100000") + Decimal("11989")


def test_approve_order_within_strategy_limit_approves():
    # 策略上限 60% × 初始 100000 = 60000
    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("60000"))
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 1000


def test_approve_order_exceeds_strategy_limit_reduces():
    # 策略上限 60000，买 7000 股 × 10 = 70000 超限 → 缩减到 6000 股
    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("60000"))
    approved, qty = acc.approve_order(7000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 6000


def test_approve_order_exceeds_cash_reduces_to_affordable():
    # 现金只剩 5000，买 1000 股 × 10 = 10000 → 缩减到 500 股
    acc = Account(Decimal("5000"), strategy_capital_limit=Decimal("100000"))
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 500


def test_approve_order_below_one_lot_rejects_and_counts():
    # 现金 400，买 1000 股 × 10 → 缩减到 0 股（不足1手）→ 拒绝 + 计数
    acc = Account(Decimal("400"), strategy_capital_limit=Decimal("100000"))
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is False
    assert qty == 0
    assert acc.insufficient_count == 1


def test_approve_order_bond_lot_10_rounds_down():
    """可转债 10 张/手（可选）：现金 1550、价 100、申请 20 张 → 缩到 10 张成交。
    同口径股票 100 股/手 → 不足 1 手拒绝（原行为）。"""
    acc = Account(Decimal("1550"))
    approved, qty = acc.approve_order(20, Decimal("100"), Decimal("0"), lot_size=10)
    assert approved is True
    assert qty == 10
    # 默认/股票手：不足 100 股 → 拒绝
    acc2 = Account(Decimal("1550"))
    approved2, qty2 = acc2.approve_order(20, Decimal("100"), Decimal("0"))
    assert approved2 is False and qty2 == 0


def test_approve_order_bond_lot_below_10_rejects():
    """转债手：可买量不足 10 张（1 手）仍拒绝。"""
    acc = Account(Decimal("800"))  # 价 100 仅够 8 张
    approved, qty = acc.approve_order(20, Decimal("100"), Decimal("0"), lot_size=10)
    assert approved is False and qty == 0
    assert acc.insufficient_count == 1


def test_approve_order_below_one_lot_logs_debug(caplog):
    """④ 资金不足不足1手拒绝 → logger.debug（回测/实盘共用，DEBUG 级别）。"""
    import logging
    acc = Account(Decimal("400"), strategy_capital_limit=Decimal("100000"))
    with caplog.at_level(logging.DEBUG, logger="core.engine.account"):
        approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is False
    assert qty == 0
    assert any(
        r.levelno == logging.DEBUG for r in caplog.records
    ), "资金不足不足1手拒绝应打 debug 日志"


def test_approve_order_existing_position_reduces_available_limit():
    # 策略上限 60000，已持仓市值 30000，再买 4000 股 × 10 = 40000 → 上限剩 30000 → 缩到 3000
    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("60000"))
    approved, qty = acc.approve_order(4000, Decimal("10"), Decimal("30000"))
    assert approved is True
    assert qty == 3000


# ---------------- apply_reverse（切片5 G6 拒单/撤单反向修正）----------------
def test_apply_reverse_buy_adds_cash_back():
    """买入被撤：现金加回 amount+佣金+印花税（原 apply_trade 扣的）。"""
    acc = Account(Decimal("100000"))
    trade = _trade(TradeType.BUY, "10", 1000)  # amount=10000, commission=5
    acc.apply_trade(trade)
    assert acc.cash == Decimal("100000") - Decimal("10005")
    acc.apply_reverse(trade)
    assert acc.cash == Decimal("100000")  # 全额退回


def test_apply_reverse_sell_deducts_cash():
    """卖出被撤：现金扣回 amount-佣金-印花税（原 apply_trade 加的）。"""
    acc = Account(Decimal("100000"))
    trade = _trade(TradeType.SELL, "12", 1000)
    trade.stamp_duty = Decimal("6")
    acc.apply_trade(trade)
    assert acc.cash == Decimal("100000") + Decimal("11989")
    acc.apply_reverse(trade)
    assert acc.cash == Decimal("100000")  # 回退扣回


# ---------------- 费用预留（buy_fee_cfg，可选，默认 None=原行为）----------------
def _fee_acc(cash, limit=None, min_comm=Decimal("5"), buy_rate=Decimal("0.00025")):
    """启用买入费用预留的账户：审批口径 = 价×量 + max(最低佣金, 金额×费率)。"""
    acc = Account(Decimal(cash), strategy_capital_limit=limit)
    acc.buy_fee_cfg = (min_comm, buy_rate)
    return acc


def test_fee_reserve_disabled_by_default_keeps_old_behavior():
    """默认 buy_fee_cfg=None：现金恰够价×量（无佣金余地）仍全额批（原行为）。"""
    acc = Account(Decimal("10000"))
    assert acc.buy_fee_cfg is None
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 1000


def test_fee_reserve_full_approval_when_cash_covers_commission():
    """现金恰好 = 价×量 + 最低佣金（10000+5）→ 全额批。"""
    acc = _fee_acc("10005")
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 1000


def test_fee_reserve_shrinks_one_lot_when_no_commission_room():
    """现金 = 价×量（无佣金余地）→ 缩 100 股（9000+5 ≤ 10000）。"""
    acc = _fee_acc("10000")
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 900


def test_fee_reserve_proportional_commission_above_minimum():
    """比例佣金 > 最低佣金：100000 现金买 10000 股×10（佣金 100）→ 缩到 9900。"""
    acc = _fee_acc("100000", buy_rate=Decimal("0.001"))
    approved, qty = acc.approve_order(10000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 9900


def test_fee_reserve_strategy_limit_cap_also_fee_aware():
    """cap 来自策略上限（60000）时同样按含佣口径缩量。"""
    acc = _fee_acc("1000000", limit=Decimal("60000"))
    approved, qty = acc.approve_order(6000, Decimal("10"), Decimal("0"))
    assert approved is True
    assert qty == 5900


def test_fee_reserve_rejects_when_one_lot_plus_commission_unaffordable():
    """一手+佣金都付不起（现金 1004 < 1000+5）→ 拒绝 + 计数。"""
    acc = _fee_acc("1004")
    approved, qty = acc.approve_order(1000, Decimal("10"), Decimal("0"))
    assert approved is False
    assert qty == 0
    assert acc.insufficient_count == 1
