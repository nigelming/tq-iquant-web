# -*- coding: gbk -*-
# verify_deals_fields.py - verify /deals fields (G4) + order matching key (G3)
# =====================================================
# Runs INSIDE the iQuant client as a strategy (Python 3.6, GBK editor).
# Pure ASCII comments, stdlib only. It calls get_trade_detail_data DIRECTLY
# (injected into globals by iQuant), NOT the bridge's HTTP /deals - because
# the bridge's query_deals only returns 4 fields today, which is exactly what
# G4 asks whether it is enough.
#
# IMPORTANT: this script does NOT depend on the bridge. It places the order
# directly via passorder and queries deals directly via get_trade_detail_data.
# Reason: iQuant runs strategies serially in one client instance - the bridge's
# init blocks the main loop forever, so a verify script in the same instance
# never runs until the bridge stops. Running the order through the bridge would
# require the bridge to be alive, which blocks this script. So bypass it.
#
# Two phases (see docs/live-flow-checklist.md G3/G4 rows):
#   PHASE 1 (always, read-only, zero risk): dump ALL m_xxx attributes of
#     DEAL / ORDER / POSITION / ACCOUNT objects from existing history.
#     Answers G4: which fields exist that the bridge is NOT returning now
#     (trade time, direction, deal amount, ...). Also gives F5/D3 the
#     field list for m_dAvailable / positions reconciliation.
#   PHASE 2 (only if PLACE_ORDER=True): place ONE real order via passorder,
#     poll get_trade_detail_data for the new DEAL, dump its fields, and test
#     which fields can anchor a Core LiveOrder -> broker DEAL match. Answers
#     G3: passorder returns 0 (no order id), broker m_nOrderID won't match
#     Core's MD5 order_id, so we need a stable composite key
#     (stock + direction + qty + time).
#
# Usage:
#   1. copy the WHOLE file into the iQuant strategy editor as-is (GBK-safe).
#   2. set ACCOUNT below to your account (same as bridge).
#   3. STOP the bridge strategy first (serial execution - bridge blocks).
#   4. run THIS script as the only strategy; output goes to iQuant log panel.
#   5. PHASE 1 runs automatically. To also run PHASE 2 (one real order),
#      set PLACE_ORDER = True and run once more.
import time


# ================= config =================
ACCOUNT = "110002348760"      # TODO: change to your account (same as bridge)
ORDER_CODE = "600000.SH"      # stock for the real order (active, liquid)
ORDER_OP = "buy"              # "buy" = safe (needs cash); "sell" = needs holding
ORDER_VOLUME = 100            # 1 lot = 100 shares (minimum)
ORDER_PRICE = 0               # 0 = prType=14 (opposite best price)
PLACE_ORDER = False           # SAFETY: flip to True ONLY for ONE real order
DEAL_POLL_SECONDS = 60        # max wait for the fill to appear
DEAL_POLL_INTERVAL = 2        # deal poll interval (seconds)
MAX_ATTR_LEN = 80             # truncate long attribute values in dump
# ==========================================

_CTX = None


def _iq(name):
    """iQuant injects get_trade_detail_data/passorder into globals()."""
    return globals().get(name)


def _dump(obj, label):
    """Print every non-method attribute of a trade-data object."""
    print("[VERIFY] == %s ==" % label)
    if obj is None:
        print("[VERIFY]   (None)")
        return
    names = sorted(a for a in dir(obj) if not a.startswith("_"))
    for name in names:
        try:
            val = getattr(obj, name)
        except Exception as e:
            print("[VERIFY]   %-24s = <getattr err: %s>" % (name, e))
            continue
        if callable(val):
            continue
        sval = str(val)
        if len(sval) > MAX_ATTR_LEN:
            sval = sval[:MAX_ATTR_LEN] + "..."
        print("[VERIFY]   %-24s = %s" % (name, sval))
    print("[VERIFY]   (%d attrs)" % len(names))


def _dump_category(category):
    """Dump up to 3 rows of a category (DEAL/ORDER/POSITION/ACCOUNT)."""
    fn = _iq("get_trade_detail_data")
    if fn is None:
        print("[FAIL] get_trade_detail_data not available")
        return
    try:
        rows = fn(ACCOUNT, "STOCK", category) or []
    except Exception as e:
        print("[FAIL] %s query error: %s" % (category, e))
        return
    print("[VERIFY] %s: %d rows total" % (category, len(rows)))
    for i, obj in enumerate(rows[:3]):
        _dump(obj, "%s row %d" % (category, i))


# ---------------- PHASE 1: field completeness (G4) ----------------
def phase1():
    print("=" * 70)
    print("[VERIFY] PHASE 1: dump all fields of trade-data objects (G4)")
    print("=" * 70)
    for cat in ("DEAL", "ORDER", "POSITION", "ACCOUNT"):
        _dump_category(cat)


# ---------------- PHASE 2: place order + matching key (G3) ----------------
def _place_passorder(code, op, volume, price):
    """Place one order directly via passorder (no bridge).

    iQuant real C++ signature (same as bridge, verified returns 0 = accepted):
      passorder(opType, orderType, accountID, orderCode, prType, price,
                volume, strategyName, quickTrade, ContextInfo)
    opType: 23 buy, 24 sell; orderType=1101; prType=14 opposite best price.
    """
    fn = _iq("passorder")
    if fn is None:
        return {"ok": False, "error": "passorder not available"}
    op_type = 23 if op == "buy" else 24
    try:
        result = fn(op_type, 1101, ACCOUNT, code, 14, float(price), float(volume),
                    "verify_deals", 2, _CTX)
        return {"ok": True, "passorder_result": str(result)}
    except Exception as e:
        return {"ok": False, "error": "passorder raised: %s" % e}


def _check_key_fields(deal):
    """Which of the fields the bridge's /deals SHOULD return exist on the object?"""
    want = ("m_strTradeTime", "m_nTradeTime", "m_nDirection",
            "m_dTradeAmount", "m_nOrderID", "m_nDealID",
            "m_strInstrumentID", "m_nVolume", "m_dPrice")
    print("[VERIFY] G4 field checklist on the NEW DEAL (bridge /deals currently returns only 4):")
    for w in want:
        val = getattr(deal, w, None)
        print("[VERIFY]   %-18s = %r  %s" % (
            w, val, "OK" if val is not None else "MISSING"))


def _test_matching_key(deal, order_id):
    """Which fields can anchor a Core LiveOrder -> broker DEAL match?"""
    oid = getattr(deal, "m_nOrderID", None)
    code = getattr(deal, "m_strInstrumentID", None)
    qty = getattr(deal, "m_nVolume", None)
    direction = getattr(deal, "m_nDirection", None)
    t_time = getattr(deal, "m_strTradeTime", None) or getattr(deal, "m_nTradeTime", None)

    print("=" * 70)
    print("[VERIFY] MATCHING-KEY TEST (G3)")
    print("=" * 70)
    print("[VERIFY] Core order_id = %s" % order_id)
    print("[VERIFY]   m_nOrderID   = %r  (direct match to Core order_id? %s)"
          % (oid, "YES" if oid is not None and str(oid) == str(order_id) else "NO"))

    code_ok = code is not None and code == ORDER_CODE
    qty_ok = qty is not None and int(qty) == ORDER_VOLUME
    dir_ok = direction is not None
    t_ok = t_time is not None
    print("\n[VERIFY] composite-key candidate checks:")
    print("[VERIFY]   code %-6r == %s ? %s" % (code, ORDER_CODE, code_ok))
    print("[VERIFY]   qty  %-6r == %s ? %s" % (qty, ORDER_VOLUME, qty_ok))
    print("[VERIFY]   direction field present?  %s (m_nDirection=%r)" % (dir_ok, direction))
    print("[VERIFY]   trade-time field present? %s (m_strTradeTime/m_nTradeTime=%r)" % (t_ok, t_time))

    if code_ok and qty_ok and dir_ok and t_ok:
        print("\n[VERIFY] RESULT: composite key (code+qty+direction+time) can anchor the match")
        print("[VERIFY]   -> G3 PLAUSIBLE: Core can correlate this DEAL back to its LiveOrder")
    elif code_ok and qty_ok and dir_ok:
        print("\n[VERIFY] RESULT: composite key without time (code+qty+direction) present")
        print("[VERIFY]   -> G3 PARTIAL: time field missing, uniqueness needs manual check")
    else:
        print("\n[VERIFY] RESULT: composite key NOT fully present -> G3 still open")
        print("[VERIFY]   -> decide the matching key from the dumped fields above")


def phase2():
    print("=" * 70)
    print("[VERIFY] PHASE 2: place 1 order + test matching key (G3)")
    print("=" * 70)
    if not PLACE_ORDER:
        print("[VERIFY] PLACE_ORDER=False -> skipping (set True for one real order)")
        return

    fn = _iq("get_trade_detail_data")
    if fn is None:
        print("[FAIL] get_trade_detail_data not available")
        return
    try:
        n_before = len(fn(ACCOUNT, "STOCK", "DEAL") or [])
    except Exception as e:
        print("[FAIL] cannot read DEAL count before order: %s" % e)
        return

    order_id = "verify-%s" % time.strftime("%Y%m%d%H%M%S")
    print("[VERIFY] passorder %s %s vol=%s (order_id=%s)"
          % (ORDER_OP, ORDER_CODE, ORDER_VOLUME, order_id))
    resp = _place_passorder(ORDER_CODE, ORDER_OP, ORDER_VOLUME, ORDER_PRICE)
    print("[VERIFY] passorder resp: %s" % resp)
    if not resp.get("ok"):
        print("[FAIL] order not accepted: %s" % resp.get("error"))
        return

    # poll get_trade_detail_data for a NEW DEAL row
    deadline = time.time() + DEAL_POLL_SECONDS
    new_deal = None
    rows = []
    while time.time() < deadline:
        try:
            rows = fn(ACCOUNT, "STOCK", "DEAL") or []
        except Exception:
            rows = []
        if len(rows) > n_before:
            new_deal = rows[n_before] if n_before < len(rows) else rows[-1]
            break
        time.sleep(DEAL_POLL_INTERVAL)

    if new_deal is None:
        print("[FAIL] no new DEAL within %ds (rejected / not filled / halted?)" % DEAL_POLL_SECONDS)
        _dump_category("DEAL")   # dump latest anyway for manual inspection
        return

    print("[VERIFY] new DEAL found (DEAL rows %d -> %d)" % (n_before, len(rows)))
    _dump(new_deal, "NEW DEAL")
    _check_key_fields(new_deal)
    _test_matching_key(new_deal, order_id)


# ---------------- strategy entry ----------------
def init(ContextInfo):
    global _CTX
    _CTX = ContextInfo
    try:
        ContextInfo.set_account(ACCOUNT)
    except Exception:
        pass
    ContextInfo.accID = ACCOUNT

    print("=" * 70)
    print("[VERIFY] start %s  account=%s  PLACE_ORDER=%s"
          % (time.strftime("%Y-%m-%d %H:%M:%S"), ACCOUNT, PLACE_ORDER))
    print("[VERIFY] run during trading hours; logs go to the iQuant panel")
    print("=" * 70)
    phase1()
    phase2()
    print("[VERIFY] done. Paste the log into the checklist G3/G4 rows.")


def handlebar(ContextInfo):
    """Not driven in live pull mode. Kept for framework requirement."""
    pass
