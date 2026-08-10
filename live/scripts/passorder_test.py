# -*- coding: gbk -*-
# passorder_test.py - minimal passorder smoke test for the sim account
# =====================================================
# Tests whether a passorder call from a strategy reaches the broker queue
# on THIS account + client. Per XunTou docs: "simulation mode only shows
# strategy signals, it does NOT send orders". So if the strategy runs in
# simulation mode you WILL see a signal record in the strategy table, but NO
# order in the 委托/order page. To actually get an order you must run the
# strategy in "live/realtime" trading mode (the 运行模式 column). The account
# can still be the simulation account.
#
# This script places ONE 100-share BUY of 600000.SH on the first live bar,
# guarded by is_last_bar() + a done flag so it never re-fires on historical
# replay bars (replay runs many historical bars at startup).
#
# How to run:
#   1. 模型交易 UI -> 新建交易 -> select this model, a stock (e.g. 600000),
#      a period, and account 110002348760.
#   2. IMPORTANT: check the 运行模式 column/dropdown - try to pick 实盘
#      (realtime) if available. 模拟 only logs a signal, no real order.
#   3. run; then check 委托/成交 pages for the order.
import time

# ================= config =================
ACCOUNT = "110002348760"
CODE = "600000.SH"
VOLUME = 100
PRICE = 9.35          # limit price; change to today's price if needed
PR_TYPE = 0           # 0 = limit order (PRICE used); 14 = opposite best price
# ==========================================

_done = False


def init(ContextInfo):
    try:
        ContextInfo.set_account(ACCOUNT)
    except Exception as e:
        print("[TEST] set_account failed: %s" % e)
    ContextInfo.accID = ACCOUNT
    print("[TEST] init done account=%s" % ACCOUNT)


def handlebar(ContextInfo):
    global _done
    if _done:
        return
    if not ContextInfo.is_last_bar():
        # replay/historical bar -> skip, only act on the current real bar
        return
    _done = True
    try:
        # passorder(opType, orderType, accountID, orderCode, prType, price,
        #           volume, strategyName, quickTrade, ContextInfo)
        # opType 23=buy; orderType 1101=single-stock normal; quickTrade 2=fire now
        passorder(23, 1101, ACCOUNT, CODE, PR_TYPE, float(PRICE), float(VOLUME),
                  "passorder_test", 2, ContextInfo)
        print("[TEST] passorder sent: buy %s vol=%s price=%s prType=%s"
              % (CODE, VOLUME, PRICE, PR_TYPE))
    except Exception as e:
        print("[TEST] passorder raised: %s" % e)
