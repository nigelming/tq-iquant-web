# -*- coding: gbk -*-
# QMT in-client trading bridge -- SIMULATION bridge (port 8790)
# =================================================================
# !!! BOTH BRIDGE ACCOUNTS ARE QMT PAPER-TRADING ACCOUNTS -- NOT REAL MONEY !!!
#   8790 (this file)  ACCOUNT = 410038214257   QMT simulation account, 237M cash, 15 positions
#   8791 (live file)  ACCOUNT = 410009993017   the other QMT simulation account, 200M
#   Both are paper accounts inside the QMT client; this deployment has no real
#   funded account yet. To switch to real money: change the ONE `ACCOUNT` line
#   below and restart the strategy -- no other edit is needed.
#   Why deliberately different accounts: it proves multi-account / multi-session
#   bookkeeping does not cross-contaminate (each bridge books only its own account).
#   Why not 100010019387 (a paper account too): the client accepts queries for it
#   (0 positions, 1.001e9 balance) but reports `m_dDetailFundBalance invalid` and
#   `available = 1.797e+308` (DBL_MAX sentinel), so /account comes back empty and
#   the fund readings are unusable. 410038214257 has complete, sane account data.
#   NOTE: the old value 110002348760 is a Guosen QMT FUND account number and is
#   NOT valid in QMT. passorder's accID must be a QMT account id; with a wrong id
#   the counter rejects the order outright -- which looks like "order failed" but
#   is really "wrong account".
# =================================================================
# Runs as a Python STRATEGY inside the QMT client and exposes a local
# HTTP server on 127.0.0.1:8790 so the Core (main/) can place real orders and
# pull 1m/5m bars through QMT.
#
# *** DUAL-BRIDGE MIRROR ***
# This is the SIMULATION bridge (port 8790, QMT paper account 410038214257).
# The other bridge is iquant_bridge_live.py (port 8791, QMT paper account
# 410009993017 -- "live" only names the slot, it is NOT real money yet).
# The two files are INDEPENDENT SELF-CONTAINED COPIES -- QMT loads a strategy
# by pasting file content into its editor (not by file-path import), so the two
# bridges CANNOT share an imported module. ANY logic change MUST be applied to
# BOTH files by hand. Only the config block below (PORT / ACCOUNT / DRY_RUN)
# differs.
#
# ===================== DEPLOY IN THE QMT CLIENT =====================
# 1. Client: strategy trading -> new python strategy.
# 2. Paste THIS WHOLE FILE into the strategy editor. The client loads the pasted
#    content; it does NOT import this file path, so no subdirectory import, no
#    relative path and no third-party module may be referenced here.
# 3. Edit only these constants (top config block):
#       PORT        (8790 simulation / 8791 live)
#       ACCOUNT     (the broker account id)
#       ACCOUNT_TYPE ("STOCK" ordinary / "CREDIT" margin account; must match
#                    the real account class or every query returns empty)
#       DRY_RUN     (dev print switch, keep False for real trading)
# 4. Start the strategy and watch the client console. Expected, in order:
#       [BRIDGE] 2026-11-06 09:30:00 bind ok 127.0.0.1:8790 account=... type=STOCK dry_run=False
#       [BRIDGE] 2026-11-06 09:30:00 init done: tick scheduled every 1.0s (tag=...)
#       [BRIDGE] 2026-11-06 09:30:01 tick#1 alive listen=127.0.0.1:8790 served_total=0 conns=0
#    The tick#N line (with timestamp) must keep advancing -- it is printed on the
#    FIRST tick and then at most once per HEARTBEAT_INTERVAL (60s) while idle.
#    If tick#N never appears, the scheduled timer is not firing (see RUN MODEL)
#    and the bridge will NOT answer any request.
# 5. Verify from the platform host:  curl http://127.0.0.1:8790/ping
# 6. Editing this file requires STOP + START of the strategy in the client;
#    the running strategy keeps the old code until restarted.
#    PORT EXCLUSIVITY: on Windows the bridge binds with SO_EXCLUSIVEADDRUSE, so
#    a second instance on the same port FAILS LOUDLY (FATAL line) instead of
#    "binding ok" and silently swallowing requests. If you see that FATAL line:
#    stop the strategy first, then start it; if it persists, restart the client
#    (a stopped instance can leave a socket handle behind inside the process).
#
# 8791 LEG = CREDIT ACCOUNT: that bridge is bound to the margin/credit account
# 410009993017 (QMT account type "CREDIT"). In the client's strategy trade
# properties select account type = credit and funds account =
# 410009993017, otherwise queries come back empty and orders are rejected.
# This 8790 leg uses the ordinary account 410038214257 (type "STOCK").
#
# WHY THIS FILE IS PURE ASCII: the QMT editor stores strategy source as GBK. A
# UTF-8 Chinese comment pasted into it becomes mojibake and can break loading, so
# every comment here is English/ASCII. Keep it that way.
# ==========================================================================
#
# Mode model (two independent dimensions):
#   - simulation vs live (THIS dimension, by account/bridge): which account the
#     order goes to. Core routes by session.mode -> this bridge (simulation) or
#     the live bridge.
#   - signal-only vs real-order (the OTHER dimension): controlled by the QMT
#     client's "live trade / simulation" START BUTTON, NOT by this bridge and
#     NOT by DRY_RUN. Even with DRY_RUN=False, starting the strategy in QMT's
#     simulation mode makes passorder emit only a signal, no real order. DRY_RUN
#     below stays a dev-only print switch.
#
# Design basis: docs/plans/0009-iquant-http-bridge.md
#
# RUN MODEL (2026-11-06, supersedes the 2026-08 "init blocks forever" model):
#   init() binds the listening socket, registers C.schedule_run(tick, ...) and
#   RETURNS IMMEDIATELY. The scheduled tick() then drives a BOUNDED
#   accept/read/write loop (TICK_BUDGET_SECONDS, < 1s) and returns, releasing
#   the strategy thread on every firing.
#
#   Why the old model had to go: in the current client build a blocked init
#   never executes its loop body -- the port showed LISTENING and the TCP
#   handshake succeeded, but not one byte was ever read, so every Core request
#   hung until CLOSE_WAIT (only symptom: the single "bridge listening" line).
#   The supported shape -- used by every QMT strategy sample --
#   is: init sets the account, calls C.schedule_run(func, start, -1,
#   timedelta, tag) and returns; the callback may even block for seconds.
#
#   TICK_INTERVAL is 1s by preference (Core calls answer within ~1s worst case).
#   If this client build rejects sub-minute intervals, switch to the minute
#   shape: TICK_INTERVAL = timedelta(minutes=1) and TICK_BUDGET_SECONDS = 55 --
#   the same tick() then serves inside itself for 55s and returns 5s before the
#   next firing. Only those two numbers change.
#
#   Do NOT put a while loop back into init().
#
# Environment constraints (verified on real machines):
#   - threading.Thread never runs, handlebar only fires during startup replay,
#     run_time is never called  -> hence the scheduled-callback model above.
#   - quotes via get_market_data_ex (get_history_data is deprecated; subscribe
#     push is unusable)  -> pull mode
#   - 5m is a native period, can pull directly
#   - the embedded interpreter is Python 3.6.8, standard library only: no
#     walrus, no match, no dataclasses/typing novelties.
#
# Endpoints (all JSON, unchanged -- Core's HttpBridgeDispatcher depends on them):
#   GET  /ping                 heartbeat + bridge status
#   POST /order                place order (idempotent order_id + whitelist + rate limit)
#   GET  /positions            positions
#   GET  /account              account funds
#   GET  /orders?order_id=     order query
#   GET  /deals?order_id=      deal query
#   GET  /quote?code=&period=&count=   1m/5m/1d bars (cached, ContextInfo; xtdata fallback)
#   GET  /calendar?market=SH    trading dates ["YYYYMMDD", ...] (cached, ContextInfo; xtdata fallback)
#   GET  /shutdown             release the port (used by the next instance's init
#                              to take over after a strategy restart)
#
# Security: bridge binds 127.0.0.1 only (loopback, single-user host), so no
# auth token is required. Defense is at the machine boundary: only local
# processes can reach port 8790. If the bridge is ever exposed off-loopback,
# add auth then. Whitelist ALLOWED_STOCKS, per-order limit MAX_VOLUME, rate
# limit RATE_LIMIT, audit log remain enforced.
import datetime as dt
import json
import math
import os
import select
import socket
import time
import traceback
from collections import OrderedDict

# ================= config =================
HOST = "127.0.0.1"
PORT = 8790
# QMT paper ("simulation") account, 237M cash / 15 positions, complete account
# detail -- NOT real money. See the account block in the file header. QMT exposes
# no API to read the logged-in account (ContextInfo only has set_account;
# passorder and get_trade_detail_data require an explicit account argument), so
# the id is a hardcoded constant. To bind another account, edit this ONE line
# and restart the strategy.
ACCOUNT = "410038214257"
# Account TYPE for every get_trade_detail_data call (2nd argument). "STOCK" =
# ordinary A-share account (this leg), "CREDIT" = margin/credit account (the
# 8791 leg). QMT rejects queries whose type does not match the real account
# class, so this must follow the account, not the code.
ACCOUNT_TYPE = "STOCK"
DRY_RUN = False                    # dev-only print switch (NOT the signal/real-order control -- that is the QMT start button)
ALLOWED_STOCKS = set()            # whitelist (empty = no restriction; configure in production)
MAX_VOLUME = 100000               # max shares per order (100k: low-priced ETF orders can reach ~34k shares)
RATE_LIMIT = 1000                 # max RATE_LIMIT orders per RATE_WINDOW seconds
RATE_WINDOW = 10                  # rate-limit window (seconds)
QUOTE_CACHE_TTL = 1               # quote cache refresh interval (seconds)
QUOTE_COUNT = 10                  # default bar count for /quote
HISTORY_DAYS = 30                 # history depth floor (calendar days) for downloads
CTX_DAILY_DAYS = 90               # interval window for 1d reads (count is unreliable)
# Fields requested from ContextInfo.get_market_data_ex. MUST be explicit: with
# fields=[] the client's _PyContextInfo keeps 'stime' as BOTH index and column,
# so reset_index() raises "cannot insert stime, already exists" and the old
# _df_to_bars() swallowed it into {"ok": true, "data": {code: []}} in 3ms
# (real-machine 2026-11-06). The explicit-fields branch drops 'stime' instead.
CTX_FIELDS = ["open", "high", "low", "close", "volume", "amount"]
# bars per trading day per period -- used to size the download window so that
# `count` bars can actually be served (30x500 needs ~63 trading days, a flat
# 30-day window only ever yields ~176 bars -- found 2026-09-03).
BARS_PER_DAY = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "1h": 4, "1d": 1}
# QMT/xtquant only STORES tick/1m/5m/1d as base periods; 15m/30m/1h/1w/1mon
# are SYNTHESIZED from them and CANNOT be downloaded (ErrorID 200006, real-machine
# diag 2026-09-03: 30m download silently failed for 149/178 ETFs). Downloading the
# base period makes the synthesized bars readable (verified: 5m -> 30m/1h fresh).
SYNTH_BASE = {"15m": "5m", "30m": "5m", "1h": "5m", "1w": "1d", "1mon": "1d"}
# base-period bars behind each synthesized bar, for sizing the download window
SYNTH_RATIO = {"15m": 3, "30m": 6, "1h": 12, "1w": 5, "1mon": 22}
EMPTY_READ_RETRIES = 3            # reads after a fresh download before giving up
EMPTY_RETRY_INTERVAL = 0.5        # seconds between empty reads
DOWNLOAD_MIN_INTERVAL = 5         # skip re-download within this many seconds
PLACED_MAX = 5000                 # _placed idempotency cache cap; oldest evicted past this (audit #32)
# ---- scheduled tick loop (2026-11-06 run model; see header) ----
TICK_INTERVAL = dt.timedelta(seconds=1)
TICK_BUDGET_SECONDS = 0.9         # max seconds one tick may spend serving requests
TICK_TAG = "iquant_bridge_tick"   # schedule_run tag (unique per bridge)
HEARTBEAT_INTERVAL = 60           # print one tick-heartbeat line per N seconds
EXC_LOG_INTERVAL = 60             # same-kind exception printed at most once per N s
MAX_REQUEST_BYTES = 1048576       # drop a connection whose request buffer exceeds this
CONN_IDLE_TIMEOUT = 30            # drop a half-open/idle connection after N seconds
SEND_TIMEOUT = 5.0                # per-response send timeout (seconds)
ENDPOINTS = ("/ping", "/order", "/positions", "/account",
             "/orders", "/deals", "/quote", "/calendar", "/shutdown")
# ==========================================

_CTX = None
_listen_sock = None
# Set by GET /shutdown; the port is released at the END of the tick that served
# that request (never while a response is still being written).
_shutdown_requested = False
_placed = OrderedDict()            # order_id -> result (idempotency), capped at PLACED_MAX (audit #32)
_placing = set()                  # in-flight order_ids
_requests = []                    # rate-limit timestamps
_quote_cache = {}                 # (code, period, count) -> (ts, bars)
_has_data = set()                 # (code, period) read non-empty at least once this session
_last_download = {}               # (code, period) -> ts of last download_history_data
_last_log = {}                    # (kind, code, period) -> ts, throttle repeated diagnostics
# quote fetch summary window: count successes per period, flush one summary line
# per SUMMARY_INTERVAL instead of one line per fetch (a poll over 17 codes x N
# periods otherwise prints ~80 lines every 60s, ~30k+/day).
_quote_summary = {}               # period -> [ok_count, got_total, got_min, got_max]
_summary_since = 0.0              # ts the current summary window started
SUMMARY_INTERVAL = 60             # seconds between quote-summary flushes
# connection state persists ACROSS ticks, so a request split over two recv()s is
# finished on the next firing: sock -> [buffer bytes, last activity ts]
_clients = OrderedDict()
_tick_count = 0                   # total ticks fired (shown in the heartbeat line)
_tick_served = 0                  # total requests answered this session
_last_heartbeat = 0.0             # ts of last heartbeat print (0 -> first tick prints)


# ---------------- diagnostics ----------------
def _ts():
    """Local timestamp for console lines (the client runs in Asia/Shanghai)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _iq(name):
    """Look up a QMT API injected into this strategy.

    passorder / get_trade_detail_data are injected into the module globals();
    the market-data functions are only bound to the ContextInfo instance handed
    to init() (kept in _CTX). globals() is checked first so trading calls keep
    their exact old behaviour, then the context object is used as fallback --
    without this step _iq("get_market_data_ex") always returned None and the
    ContextInfo quote path never ran.
    """
    fn = globals().get(name)
    if fn is not None:
        return fn
    ctx = _CTX
    if ctx is not None:
        return getattr(ctx, name, None)
    return None


def _throttled_log(key, msg, interval=300):
    """Print msg at most once per interval seconds per key (steady-state log control).

    Failures/empties surface but not every poll -- a full trading day otherwise
    logs thousands of quote lines for ~17 codes x 4 periods.
    """
    now = time.time()
    if now - _last_log.get(key, 0) < interval:
        return
    _last_log[key] = now
    print(msg)


def _log_exception(kind, exc):
    """Print one concise line (type + message + last traceback frames) per
    EXC_LOG_INTERVAL seconds per kind.

    The old event loop swallowed every accept/recv/send exception with a bare
    `except Exception: pass`, which is exactly why the 2026-11 hang produced no
    diagnostics at all. Failures must be visible in the client console, but must
    not flood it: a dead peer would otherwise print on every tick.
    """
    now = time.time()
    key = ("exc", kind)
    if now - _last_log.get(key, 0) < EXC_LOG_INTERVAL:
        return
    _last_log[key] = now
    try:
        lines = [ln.strip() for ln in traceback.format_exc().splitlines() if ln.strip()]
    except Exception:
        lines = []
    tail = "  @ " + " | ".join(lines[-4:]) if lines else ""
    print("[BRIDGE] %s EXC %s %s: %s%s"
          % (_ts(), kind, type(exc).__name__, exc, tail))


def _record_quote_ok(period, got):
    """Count a successful quote fetch into the rolling summary window.

    Successes stay silent per-fetch; the window flushes one line every
    SUMMARY_INTERVAL so the bridge emits a heartbeat without flooding.
    """
    global _summary_since
    now = time.time()
    if _summary_since == 0.0:
        _summary_since = now
    rec = _quote_summary.get(period)
    if rec is None:
        rec = [0, 0, got, got]
        _quote_summary[period] = rec
    rec[0] += 1
    rec[1] += got
    if got < rec[2]:
        rec[2] = got
    if got > rec[3]:
        rec[3] = got
    if now - _summary_since >= SUMMARY_INTERVAL:
        _flush_quote_summary()


def _flush_quote_summary():
    """Emit one line summarising fetches since the last flush, then reset."""
    global _summary_since
    if not _quote_summary:
        _summary_since = time.time()
        return
    parts = []
    total = 0
    for period in sorted(_quote_summary):
        ok, got_total, got_min, got_max = _quote_summary[period]
        total += ok
        rng = str(got_min) if got_min == got_max else "%d-%d" % (got_min, got_max)
        parts.append("%s:%d(%s)" % (period, ok, rng))
    _quote_summary.clear()
    _summary_since = time.time()
    print("[BRIDGE] %s quote %d fetches ok  %s" % (_ts(), total, "  ".join(parts)))


# ---------------- auth / whitelist / rate limit ----------------
def check_whitelist(code, volume):
    if not code:
        return False, "missing code"
    if ALLOWED_STOCKS and code not in ALLOWED_STOCKS:
        return False, "stock not allowed: %s" % code
    if volume > MAX_VOLUME:
        return False, "volume %s exceeds max %s" % (volume, MAX_VOLUME)
    return True, None


def check_rate_limit():
    now = time.time()
    _requests[:] = [t for t in _requests if now - t < RATE_WINDOW]
    if len(_requests) >= RATE_LIMIT:
        return False
    _requests.append(now)
    return True


# ---------------- order (idempotent) ----------------
def place_order(params):
    oid = params.get("order_id")
    code = params.get("code")
    op = params.get("op", "buy")
    volume = params.get("volume", 100)
    if not oid:
        return {"ok": False, "error": "missing order_id"}
    if oid in _placed:
        _log_order(oid, op, code, volume, _placed[oid], dup=True)
        return _placed[oid]                       # already accepted -> return original result
    if oid in _placing:
        result = {"ok": False, "error": "duplicate in-flight"}
    else:
        ok, err = check_whitelist(code, volume)
        if not ok:
            result = {"ok": False, "error": err}
        elif not check_rate_limit():
            result = {"ok": False, "error": "rate limited"}
        else:
            _placing.add(oid)
            try:
                result = _do_place(params)
                _placed[oid] = result
                # audit #32: cap _placed to PLACED_MAX; evict oldest (insertion-ordered) to bound memory.
                # Idempotency only matters for near-term retries; a months-old order_id won't be re-sent.
                while len(_placed) > PLACED_MAX:
                    _placed.popitem(last=False)
            finally:
                _placing.discard(oid)
    _log_order(oid, op, code, volume, result)
    return result


def _log_order(oid, op, code, volume, result, dup=False):
    """One concise audit line per order attempt with its outcome.

    Replaces the old two-line output (a pre-passorder [BRIDGE] order line plus a
    raw [AUDIT] JSON dump) with a single line: oid + direction + code + size +
    result. prType/price/account are omitted: prType is fixed at 14, price is
    ignored for opposite-best orders, account is fixed at startup.
    """
    if result.get("ok"):
        if result.get("dry_run"):
            tail = "dry-run"
        else:
            tail = "ok result=%s" % result.get("passorder_result")
    else:
        tail = "REJECT: %s" % result.get("error")
    if dup:
        tail = "dup " + tail
    print("[BRIDGE] %s ORDER %s %s %s x%s %s"
          % (_ts(), oid, str(op).upper(), code, volume, tail))


def _confirm_order_arrival(remark, account):
    """Confirm the order record exists after passorder (arrival check).

    passorder returning 0 means "accepted", NOT "an order record was created".
    QMT can accept-then-drop the order silently (real-machine 2026-08-14:
    id36-39, passorder result=0 but NO row in the ORDER detail table). Core
    then polls /orders forever finding no order_ref, only timing out at 180s.
    This function closes that gap: after passorder returns 0, sleep briefly and
    re-query the ORDER detail table by remark (m_strRemark == the passorder
    userOrderId we set). If the row is there -> True; if not -> False, so _do_place
    returns ok=False and Core marks the order rejected immediately (its existing
    BridgeOrderRejected path) instead of waiting 180s.

    Sleep 0.3s first (passorder->ORDER row usually <200ms; verified 24-39ms
    backfill), then retry up to 2 times (total <=~1s) -- keeps the place chain
    short. The query runs on the same strategy thread as passorder, no
    concurrency issue (same as query_orders).

    *** DUAL-BRIDGE MIRROR *** -- identical logic must exist in
    iquant_bridge_live.py _confirm_order_arrival. Hand-sync on any change.
    """
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return True  # cannot verify -> do not block the order (let 180s timeout backstop)
    time.sleep(0.3)
    for _ in range(2):
        try:
            rows = fn(account, ACCOUNT_TYPE, "ORDER") or []
            for o in rows:
                if getattr(o, "m_strRemark", None) == remark:
                    return True
        except Exception:
            pass  # query failed -> retry once more, else treat as not-arrived
        time.sleep(0.3)
    return False


def _do_place(params):
    code = params.get("code")
    op = params.get("op", "buy")                  # buy / sell
    volume = params.get("volume", 100)
    price = params.get("price", 0)                # 0=latest price, >0=limit price
    account = params.get("account", ACCOUNT)
    # userOrderId -> m_strRemark: Core's deterministic order_id prefix (ASCII hex).
    # Lets Core match the returned order/deal back to exactly this order (precise
    # remark match), instead of fuzzy code+direction+volume which can collide with
    # leftover orders from a prior session. m_strRemark length varies by client;
    # 20 ASCII chars is safely within limits. Truncate defensively.
    remark = str(params.get("remark") or "")[:20]

    op_type = 23 if op == "buy" else 24           # passorder opType: 23 buy, 24 sell
    # prType=14 = opposite best price (client-side orderbook-price limit order):
    #   BUY takes sell1 price, SELL takes buy1 price -> immediate fill.
    #   NOT an exchange market order, so no market-order symbol/session limits.
    #   price param has no effect for prType!=11; pass 0 as placeholder.
    #   Real fill price is backfilled from /deals in a later slice.
    pr_type = 14

    if DRY_RUN:
        return {"ok": True, "dry_run": True,
                "params": {"code": code, "op": op, "volume": volume,
                           "price": price, "pr_type": pr_type, "remark": remark}}

    fn = _iq("passorder")
    if fn is None:
        return {"ok": False, "error": "passorder not found in strategy namespace"}
    try:
        # QMT real C++ signature (11-arg variant, verified returns 0 = accepted):
        # passorder(opType, orderType, accountID, orderCode, prType, price,
        #           volume, strategyName, quickTrade, userOrderId, ContextInfo)
        # userOrderId (remark) writes m_strRemark on the resulting order/deal so
        # Core can precisely claim this order. quickTrade=2 = immediate dispatch.
        # orderType=1101 = single-stock/single-account/normal/by-share
        #   (official single-stock standard value; old 0 was non-standard).
        # ACCOUNT TYPE: passorder takes no account-type argument -- QMT resolves
        # it from the strategy's trade properties. A CREDIT account (the 8791
        # leg) therefore needs "account type = credit" selected there, otherwise
        # the counter rejects the order. No code change, only the client setting.
        result = fn(op_type, 1101, account, code, pr_type, float(price), float(volume),
                    "iquant_bridge", 2, remark, _CTX)
        # 0 = accepted (verified on real client). Any other value means the
        # broker/client rejected the order -- must report ok=False so Core marks
        # it rejected instead of holding a submitted order whose order_ref never
        # appears.
        if result != 0:
            return {"ok": False, "error": "passorder rejected (code=%s)" % result}
        # Arrival check: passorder accepted does NOT guarantee an order record was
        # created (accept-then-drop, real-machine 2026-08-14 id36-39). Re-query the
        # ORDER table by remark; if no row, report ok=False so Core marks rejected
        # immediately instead of waiting 180s for order_ref that will never appear.
        if not _confirm_order_arrival(remark, account):
            return {"ok": False,
                    "error": "passorder accepted but no order record (dropped)"}
        return {"ok": True, "passorder_result": str(result)}
    except Exception as e:
        return {"ok": False, "error": "passorder raised: %s" % e}


# ---------------- query endpoints ----------------
def _safe_num(value):
    """Normalize one QMT numeric field so the JSON we emit is always valid.

    QMT returns inf / nan / DBL_MAX (1.797e+308) sentinels for a halted or
    broken instrument (real case: 601989 market_value = Infinity), and Python's
    json emits bare `Infinity` which strict parsers and the platform's
    json.dumps(allow_nan=False) reject. Non-finite -> None. Strings and other
    non-numeric values pass through untouched (field semantics unchanged).
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return None if abs(value) >= 1e308 else value
    if isinstance(value, float):
        # DBL_MAX (1.797e+308) is finite but is QMT's "no value" sentinel for
        # account fields, so treat it like inf: emit null.
        if not math.isfinite(value) or abs(value) >= 1e308:
            return None
        return value
    return value


def _safe_row(row):
    """Apply _safe_num to every value of one outward row (names/structure kept)."""
    return dict((k, _safe_num(v)) for k, v in row.items())


# POSITION cost attribute candidates, tried in order (first non-null wins).
# QMT builds differ: m_dOpenPrice is the average open price on most of them;
# the rest are fallbacks seen on other client versions. Never fall back to 0 --
# the platform treats cost<=0 as "unknown" and skips stop-loss/take-profit,
# which is safe, while a fake 0 would silently look like a valid zero cost.
_POSITION_COST_FIELDS = ("m_dOpenPrice", "m_dAvgPrice", "m_dCostPrice",
                         "m_dPositionCost")
# Diagnostic for /ping: rows in the last /positions reply without a usable cost.
_POS_COST_STAT = {"missing": 0}


def _position_cost_raw(o):
    """Position avg cost (float) or None when no candidate attribute has a value."""
    for name in _POSITION_COST_FIELDS:
        value = getattr(o, name, None)
        if value is None:
            continue
        num = _safe_num(value)
        if num is not None:
            return num
    return None


def query_positions(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        missing_cost = 0
        for o in (fn(account, ACCOUNT_TYPE, "POSITION") or []):
            cost = _position_cost_raw(o)
            if cost is None:
                missing_cost += 1
            rows.append(_safe_row({
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                "volume": getattr(o, "m_nVolume", None),
                "available": getattr(o, "m_nCanUseVolume", None),  # T+1 usable qty
                "yesterday_volume": getattr(o, "m_nYesterdayVolume", None),
                "on_road_volume": getattr(o, "m_nOnRoadVolume", None),
                "market_value": getattr(o, "m_dMarketValue", None),
                # avg open price (position cost). Additive field: old platform
                # parsers ignore it. None (not 0) when the client exposes no cost
                # field -- a fabricated 0 silently disables stop-loss protection.
                "cost": cost,
            }))
        _POS_COST_STAT["missing"] = missing_cost
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_positions: %s" % e}


# Account field candidates, tried in order across the ACCOUNT row (and, for
# margin accounts, the ASSET row). First NON-ZERO value wins; if no candidate
# yields a value at all the field is 0 (never null -- the platform treats the
# snapshot as numbers). Order keeps the ordinary-account reading stable:
# m_dAssetBalance / m_dStockValue are what STOCK accounts have always used.
_ACCOUNT_FIELD_MAP = (
    ("available", ("m_dAvailable", "m_dCanUseBalance", "m_dFundAvail")),
    ("total_asset", ("m_dAssetBalance", "m_dTotalAsset", "m_dBalance")),
    ("market_value", ("m_dStockValue", "m_dMarketValue")),
    ("balance", ("m_dBalance",)),
    ("frozen_cash", ("m_dFrozenCash", "m_dFrozenBalance")),
    ("commission", ("m_dCommission", "m_dTotalCommission")),
    ("position_profit", ("m_dPositionProfit",)),
)


def _pick_field(objs, names):
    """First non-zero candidate value across objects/attribute names, else 0."""
    first = None
    for name in names:
        for obj in objs:
            if obj is None:
                continue
            value = _safe_num(getattr(obj, name, None))
            if value is None:
                continue
            if first is None:
                first = value
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value != 0:
                return value
    return 0 if first is None else first


def _dump_attrs(obj):
    """All m_* attributes -> dict (diagnostic only, original names kept)."""
    out = {}
    for name in dir(obj):
        if not name.startswith("m_"):
            continue
        try:
            value = _safe_num(getattr(obj, name))
        except Exception:
            continue
        if value is None or isinstance(value, (int, float, str, bool)):
            out[name] = value
    return out


def query_account(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        acct_objs = fn(account, ACCOUNT_TYPE, "ACCOUNT") or []
        asset_objs = []
        if ACCOUNT_TYPE == "CREDIT":
            # Margin accounts keep total asset / market value in a separate
            # ASSET table: CREDIT ACCOUNT reports them as 0 (real machine
            # 2026-11-06: 8791 total_asset=0 market_value=0 while available
            # = 2e8). Merge ASSET in; non-zero ACCOUNT fields still win.
            asset_objs = fn(account, "CREDIT", "ASSET") or []
        source = "ACCOUNT+ASSET" if asset_objs else "ACCOUNT"
        rows = []
        for i, o in enumerate(acct_objs):
            extra = asset_objs[i] if i < len(asset_objs) else None
            raw = {"ACCOUNT": _dump_attrs(o)}
            if extra is not None:
                raw["ASSET"] = _dump_attrs(extra)
            row = {"account_source": source, "raw": raw}
            for field, names in _ACCOUNT_FIELD_MAP:
                row[field] = _pick_field((o, extra), names)
            rows.append(row)
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_account: %s" % e}


# ---------------- deal/order side (QMT field semantics) ----------------
# QMT DEAL/ORDER objects: m_nDirection is EEntrustBS and is ALWAYS 48 for
# stocks (official xtquant doc: "for stocks this value is always 48"), so it
# cannot tell buy from sell. The real discriminator is m_nOffsetFlag
# (EOffset_Flag_Type): 48 = buy/open, 49..54 = sell/close/close-today/
# close-yesterday/force-close/reduce/local-force-close.
# 2026-11-06: real deals of both a manual buy and a manual sell came back with
# direction=48 -> the platform booked the sell as a buy. Fixed here.
_OFFSET_BUY = (48,)
_OFFSET_SELL = (49, 50, 51, 52, 53, 54)
_SIDE_FALLBACK_LOGGED = {"order_ref": False}


def _row_side(offset_flag, direction):
    """Normalize buy/sell: offset flag first, then unambiguous direction codes.

    Returns "BUY" / "SELL" / None. direction 48 is deliberately NOT treated as
    buy (it is the always-48 EEntrustBS for stocks); 23/24 are stock opTypes
    and 0/1 the generic direction enum.
    """
    off = _safe_num(offset_flag)
    if off is not None:
        off = int(off)
        if off in _OFFSET_BUY:
            return "BUY"
        if off in _OFFSET_SELL:
            return "SELL"
    d = _safe_num(direction)
    if d is not None:
        d = int(d)
        if d in (23, 0):
            return "BUY"
        if d in (24, 1):
            return "SELL"
    return None


def _order_side_index(account):
    """order_ref -> side, built from the ORDER table (deal fallback)."""
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {}
    try:
        out = {}
        for o in (fn(account, ACCOUNT_TYPE, "ORDER") or []):
            ref = str(getattr(o, "m_strOrderRef", "") or "").strip()
            if not ref:
                continue
            side = _row_side(getattr(o, "m_nOffsetFlag", None),
                             getattr(o, "m_nDirection", None))
            if side is not None:
                out[ref] = side
        return out
    except Exception:
        return {}


def query_orders(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        for o in (fn(account, ACCOUNT_TYPE, "ORDER") or []):
            offset = getattr(o, "m_nOffsetFlag", None)
            raw_dir = getattr(o, "m_nDirection", None)
            side = _row_side(offset, raw_dir)
            rows.append(_safe_row({
                "order_ref": getattr(o, "m_strOrderRef", None),      # matching key
                "order_sysid": getattr(o, "m_strOrderSysID", None),
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                # direction kept in legacy shape (48 buy / 49 sell) but derived
                # from offset_flag; direction_raw keeps the raw m_nDirection.
                "direction": (48 if side == "BUY" else 49) if side else raw_dir,
                "direction_raw": raw_dir,
                "offset_flag": _safe_num(offset),
                "side": side,
                "limit_price": getattr(o, "m_dLimitPrice", None),
                "traded_price": getattr(o, "m_dTradedPrice", None),
                "volume": getattr(o, "m_nVolumeTotalOriginal", None),
                "traded_volume": getattr(o, "m_nVolumeTraded", None),
                "status": getattr(o, "m_nOrderStatus", None),        # 54 cancel 56 filled
                "source": getattr(o, "m_strSource", None),           # BRIDGE / GUI
                "order_type": getattr(o, "m_strOrderStrategyType", None),
                "insert_time": getattr(o, "m_strInsertTime", None),
                "insert_date": getattr(o, "m_strInsertDate", None),
                "cancel_amount": getattr(o, "m_dCancelAmount", None),
                "remark": getattr(o, "m_strRemark", None),           # = passorder userOrderId
            }))
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_orders: %s" % e}


def query_deals(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        side_index = None
        for o in (fn(account, ACCOUNT_TYPE, "DEAL") or []):
            offset = getattr(o, "m_nOffsetFlag", None)
            raw_dir = getattr(o, "m_nDirection", None)
            side = _row_side(offset, raw_dir)
            ref_val = str(getattr(o, "m_strOrderRef", "") or "").strip()
            if side is None and ref_val:
                if side_index is None:
                    side_index = _order_side_index(account)
                side = side_index.get(ref_val)
                if side is not None and not _SIDE_FALLBACK_LOGGED["order_ref"]:
                    _SIDE_FALLBACK_LOGGED["order_ref"] = True
                    print("[bridge] deal side resolved from ORDER table "
                          "(order_ref=%s)" % ref_val)
            rows.append(_safe_row({
                "order_ref": getattr(o, "m_strOrderRef", None),      # matching key
                "order_sysid": getattr(o, "m_strOrderSysID", None),
                "trade_id": getattr(o, "m_strTradeID", None),
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                # direction kept in legacy shape (48 buy / 49 sell) but derived
                # from offset_flag; direction_raw keeps the raw m_nDirection.
                "direction": (48 if side == "BUY" else 49) if side else raw_dir,
                "direction_raw": raw_dir,
                "offset_flag": _safe_num(offset),
                "side": side,
                "price": getattr(o, "m_dPrice", None),
                "volume": getattr(o, "m_nVolume", None),
                "amount": getattr(o, "m_dTradeAmount", None),
                "commission": getattr(o, "m_dCommission", None),
                "trade_time": getattr(o, "m_strTradeTime", None),
                "trade_date": getattr(o, "m_strTradeDate", None),
                "source": getattr(o, "m_strSource", None),           # BRIDGE / GUI
                "order_type": getattr(o, "m_strOrderStrategyType", None),
                "remark": getattr(o, "m_strRemark", None),           # = passorder userOrderId
            }))
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_deals: %s" % e}


# ---------------- quotes (get_market_data_ex pull + cache) ----------------
def _history_start(days=HISTORY_DAYS):
    return (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d")


def _history_days(period, count):
    """Download window (calendar days) big enough to actually serve `count` bars.

    Trading days needed = ceil(count / bars-per-day); x1.5 for weekends/holidays
    plus 7 days of margin, floored at HISTORY_DAYS so small counts keep the old
    shallow window.
    """
    bpd = BARS_PER_DAY.get(period, 1)
    need = -(-count // bpd)  # ceil, py3.6-safe
    return max(HISTORY_DAYS, need * 3 // 2 + 7)


def _read_bars_with_retry(read_fn, code, period):
    """Read local bars; short-retry when this (code, period) never returned data.

    download_history_data lands on disk with a delay (2026-09-03: first pull of
    178 ETFs x 30m was all empty, data arrived over the following minute), so a
    read right after the download often sees nothing. Retries only apply while
    the session has NEVER read data for this (code, period) -- steady state pays
    zero retry cost and genuinely dataless codes don't slow every poll.
    """
    tries = EMPTY_READ_RETRIES if (code, period) not in _has_data else 1
    for i in range(tries):
        res = read_fn()
        df = (res or {}).get(code)
        if df is not None and len(df) > 0:
            _has_data.add((code, period))
            return df
        if i < tries - 1:
            time.sleep(EMPTY_RETRY_INTERVAL)
    return None


_ctx_quote_logged = False         # one-shot "ContextInfo quote works" console line


def _bar_time_parts(label):
    """'20261106103000' / '2026-11-06 10:30:00' -> (Y, M, D, hh, mm) or None."""
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        year, month, day = int(digits[0:4]), int(digits[4:6]), int(digits[6:8])
        hh = int(digits[8:10]) if len(digits) >= 10 else 0
        mm = int(digits[10:12]) if len(digits) >= 12 else 0
    except ValueError:
        return None
    return (year, month, day, hh, mm)


def _synth_bucket(label, period):
    """Bucket key for one base-period bar (None when the label is unparseable)."""
    parts = _bar_time_parts(label)
    if parts is None:
        return None
    year, month, day, hh, mm = parts
    window = {"30m": 30, "1h": 60}.get(period)
    if window is not None:
        return (year, month, day, (hh * 60 + mm) // window)
    if period == "1mon":
        return (year, month)
    if period == "1w":
        try:
            return time.strftime("%Y%W", (year, month, day, 0, 0, 0, 0, 0, -1))
        except Exception:
            return (year, month, day // 7)
    return None


def _synth_records(records, period, time_key):
    """Aggregate base-period bar dicts into `period` (OHLCV, first/last/max/min)."""
    out = []
    cur_key = None
    cur = None
    for r in records:
        label = r.get(time_key)
        key = _synth_bucket(label, period)
        if key is None:
            return None
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            cur_key = key
            cur = {"label": label,
                   "open": r.get("open"),
                   "high": r.get("high"),
                   "low": r.get("low"),
                   "close": r.get("close"),
                   "volume": r.get("volume") or 0,
                   "amount": r.get("amount") or 0}
        else:
            try:
                cur["high"] = max(cur["high"], r.get("high"))
                cur["low"] = min(cur["low"], r.get("low"))
                cur["close"] = r.get("close")
                cur["volume"] += r.get("volume") or 0
                cur["amount"] += r.get("amount") or 0
            except TypeError:
                return None
    if cur is not None:
        out.append(cur)
    return out


def _synth_from_base(df, period):
    """Aggregate a base-period DataFrame into `period`; None when impossible.

    Same columns + index shape as get_market_data_ex, so _df_to_bars and the
    /quote contract are identical for both read paths.
    """
    if df is None:
        return None
    try:
        records = df.reset_index().to_dict("records")
    except Exception:
        return None
    if not records:
        return None
    time_key = None
    for k in ("index", "time", "stime", "date", "datetime"):
        if k in records[0]:
            time_key = k
            break
    if time_key is None:
        return None
    bars = _synth_records(records, period, time_key)
    if not bars:
        return None
    try:
        import pandas as pd
        return pd.DataFrame(
            [{"open": b["open"], "high": b["high"], "low": b["low"],
              "close": b["close"], "volume": b["volume"], "amount": b["amount"]}
             for b in bars],
            index=[b["label"] for b in bars])
    except Exception:
        return None


def _ctx_read(fn, code, period, count):
    """One ContextInfo.get_market_data_ex read -> {code: DataFrame} (or None).

    The return shape is normalized to xtdata's before any further processing so
    both read paths share one contract. `1d` is requested BY INTERVAL
    (start_time + count=-1) and truncated to `count` afterwards: real-machine
    diagnosis 2026-11-06 showed `1d count=5` returning a single row because the
    client's local daily store is nearly empty, while the same interval window
    returns whatever history exists. `1w/1mon` reuse this through their 1d base,
    so they inherit the window. Other periods keep the count form (1m/5m stores
    are populated).
    """
    kwargs = {"start_time": "", "count": count,
              "dividend_type": "follow", "subscribe": True}
    if period == "1d":
        kwargs["start_time"] = _history_start(
            max(CTX_DAILY_DAYS, _history_days("1d", max(count, 1))))
        kwargs["count"] = -1
    res = fn(CTX_FIELDS, [code], period=period, **kwargs)
    if not isinstance(res, dict):
        res = {code: res}              # some builds return the frame directly
    df = res.get(code)
    if df is None or len(df) == 0:
        return None
    if period == "1d" and count > 0 and len(df) > count:
        df = df.tail(count)            # interval read gives newest-last rows
    return {code: df}


def _note_thin_daily(code, count, rows):
    """Loud (throttled) line when a 1d interval read still yields one row.

    An empty local daily store is exactly the 2026-11-06 finding (`1d count=5`
    -> rows=1). Without this line the bridge looks healthy while every
    daily/weekly/monthly signal silently starves on a single bar.
    """
    if rows == 1 and count > 1:
        _throttled_log(("thin-1d", code),
                       "[BRIDGE] %s 1d history unavailable, rows=1 code=%s count=%d"
                       % (_ts(), code, count))


def _quote_via_context(fn, code, period, count):
    """Read bars through ContextInfo.get_market_data_ex.

    One call usually suffices (the client synthesizes 30m/1h/1w/1mon itself).
    If the client only exposes base periods, pull the base and aggregate
    locally so the period contract still holds. Both paths return the same
    {code: DataFrame} shape before aggregation. Failures walk _throttled_log
    -- silently returning None is what hid the dead fallback before.
    """
    global _ctx_quote_logged

    def read(p, n):
        return _read_bars_with_retry(lambda: _ctx_read(fn, code, p, n), code, p)

    try:
        df = read(period, count)
        if df is not None:
            if period == "1d":
                _note_thin_daily(code, count, len(df))
            if not _ctx_quote_logged:
                _ctx_quote_logged = True
                print("[BRIDGE] %s quote via ContextInfo ok code=%s period=%s rows=%d"
                      % (_ts(), code, period, len(df)))
            _record_quote_ok(period, len(df))
            return df
        base = SYNTH_BASE.get(period)
        if base is not None:
            need = count * SYNTH_RATIO.get(period, 1)
            raw = read(base, need)
            if raw is not None and base == "1d":
                _note_thin_daily(code, need, len(raw))
            synth = _synth_from_base(raw, period)
            if synth is not None:
                if not _ctx_quote_logged:
                    _ctx_quote_logged = True
                    print("[BRIDGE] %s quote via ContextInfo ok code=%s period=%s rows=%d"
                          " (aggregated from %s)" % (_ts(), code, period, len(synth), base))
                _record_quote_ok(period, len(synth))
                return synth
        _throttled_log(("ctx-empty", code, period),
                       "[BRIDGE] %s ContextInfo empty %s %s count=%d"
                       % (_ts(), code, period, count))
    except Exception as e:
        _throttled_log(("ctx-fail", code, period),
                       "[BRIDGE] %s ContextInfo FAIL %s %s: %s: %s"
                       % (_ts(), code, period, type(e).__name__, e))
    return None


def _fetch_quote(code, period, count):
    # 1) ContextInfo.get_market_data_ex FIRST: the client's own in-process quote
    #    interface, no local data service required. On this machine ports
    #    58610-58620 are not listening, so every xtdata call fails with the
    #    client's "cannot connect to quote service" error -- xtdata stays as the
    #    fallback only.
    fn = _iq("get_market_data_ex")
    if fn is not None:
        df = _quote_via_context(fn, code, period, count)
        if df is not None:
            return df
    else:
        _throttled_log(("ctx-missing",),
                       "[BRIDGE] %s ContextInfo.get_market_data_ex unavailable, "
                       "xtdata path only" % _ts())
    # 2) fallback: xtquant.xtdata (downloads history; needs 58610+ data service)
    try:
        from xtquant import xtdata
        now = time.time()
        base = SYNTH_BASE.get(period)
        if base is not None:
            # synthesized period: download its base (30m/1h <- 5m, 1w/1mon <- 1d)
            bkey = (code, base)
            if now - _last_download.get(bkey, 0) >= DOWNLOAD_MIN_INTERVAL:
                need_base = count * SYNTH_RATIO.get(period, 1)
                xtdata.download_history_data(code, base,
                                             _history_start(_history_days(base, need_base)), "")
                _last_download[bkey] = now
        elif now - _last_download.get((code, period), 0) >= DOWNLOAD_MIN_INTERVAL:
            xtdata.download_history_data(code, period,
                                         _history_start(_history_days(period, count)), "")
            _last_download[(code, period)] = now
        df = _read_bars_with_retry(
            lambda: xtdata.get_market_data_ex(CTX_FIELDS, [code], period=period,
                                              count=count),
            code, period)
        if df is not None:
            # success: counted into the rolling 60s summary, not printed per-fetch
            # (per-fetch otherwise floods ~80 lines per 60s poll, ~30k+/day).
            _record_quote_ok(period, len(df))
            return df
        _throttled_log(("empty", code, period),
                       "[BRIDGE] %s xtdata empty %s %s count=%d"
                       % (_ts(), code, period, count))
    except Exception as e:
        _throttled_log(("fail", code, period),
                       "[BRIDGE] %s xtdata FAIL %s %s: %s" % (_ts(), code, period, e))
    return None


def _bar_time_label(value):
    """Bar time -> JSON-safe scalar (ms int / 14-digit string / plain text)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return value
    fmt = getattr(value, "strftime", None)
    if fmt is not None:                       # pandas.Timestamp
        try:
            return value.strftime("%Y%m%d%H%M%S")
        except Exception:
            return None
    text = str(value)                         # numpy.int64 -> "1762..."
    return text or None


def _df_to_bars(df):
    """DataFrame -> explicit, JSON-safe bar dicts.

    Never derives the shape from `reset_index().to_dict()`: the client's
    fields=[] frames carry 'stime' as BOTH index and column, so reset_index()
    raised "cannot insert stime, already exists" and the old version swallowed
    it into {"ok": true, "data": {code: []}} in ~3ms (real machine 2026-11-06).
    Time is emitted as `stime` (the platform's primary parse key) and `time`,
    values passed through unchanged (ms int or 14-digit string -- both parse in
    bar_poller.parse_bar_time); non-finite numerics become null.
    """
    if df is None:
        return []
    if isinstance(df, list):                  # some builds hand back records
        return [b for b in df if isinstance(b, dict)]
    try:
        columns = [c for c in CTX_FIELDS if c in df.columns]
        if "stime" in df.columns:             # time as a column, not the index
            labels = list(df["stime"])
        else:
            labels = list(df.index)
        values = df[columns].values.tolist() if columns else [[] for _ in labels]
    except Exception as e:
        _throttled_log(("bars-convert",),
                       "[BRIDGE] %s rows to bars FAIL: %s: %s"
                       % (_ts(), type(e).__name__, e))
        return []
    bars = []
    for label, row in zip(labels, values):
        t = _bar_time_label(label)
        bar = {"stime": t, "time": t}
        for field in CTX_FIELDS:
            bar[field] = None
        for i, field in enumerate(columns):
            bar[field] = _safe_num(row[i])
        bars.append(bar)
    if any(b["stime"] is None for b in bars):
        _throttled_log(("bars-no-time",),
                       "[BRIDGE] %s bars rows=%d no_time=%d"
                       % (_ts(), len(bars),
                          sum(1 for b in bars if b["stime"] is None)))
    return bars


# ---------------- trading calendar ----------------
_CAL_CACHE = {"ts": 0.0, "window": None, "dates": []}
_CAL_CACHE_TTL = 6 * 3600.0  # calendar is static; cache 6h


def _calendar_window(params):
    """(start, end) YYYYMMDD for /calendar -- never the full history.

    get_trading_dates(market, "", "", -1) returns every date since 1990-12-19
    (real-machine 2026-11-06), which corrupts the platform's per-year coverage
    stats. Default window = Jan 1 of last year .. today (two natural years);
    ?year=YYYY selects one natural year.
    """
    today = dt.date.today()
    raw = params.get("year")
    if raw:
        try:
            year = int(raw)
        except (TypeError, ValueError):
            year = 0
        if 1990 <= year <= today.year + 1:
            return ("%04d0101" % year, "%04d1231" % year)
    return ("%04d0101" % (today.year - 1), today.strftime("%Y%m%d"))


def _ms_to_yyyymmdd(ms):
    """Convert xtdata.get_trading_dates millisecond timestamp to YYYYMMDD.

    Uses time.localtime to match xtdata.timetagToDateTime; the QMT host runs
    in Asia/Shanghai so localtime is the trading day.
    """
    try:
        d = time.localtime(int(ms) / 1000)
        return "%04d%02d%02d" % (d.tm_year, d.tm_mon, d.tm_mday)
    except Exception:
        return None


def _cal_date(value):
    """One calendar entry -> 'YYYYMMDD' (ms timestamp, date-like or string)."""
    if value is None:
        return None
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else None
    if getattr(value, "strftime", None) is not None:
        try:
            return value.strftime("%Y%m%d")
        except Exception:
            return None
    return _ms_to_yyyymmdd(value)


def _normalize_calendar(raw, start, end):
    dates = []
    for item in raw or []:
        s = _cal_date(item)
        if s is not None and start <= s <= end:
            dates.append(s)
    return dates


def _ctx_trading_dates(fn, market, start, end):
    """Trade dates via ContextInfo.get_trading_dates (no xtdata service needed).

    Real-machine diagnosis 2026-11-06: available in-client (`cal rows=20`)
    while xtdata's identical call fails because ports 58610+ are not listening.
    The in-client signature mirrors xtdata's; some builds take only `market`,
    so the 4-argument call retries once in that shorter form.
    """
    try:
        return fn(market, start, end, -1) or []
    except TypeError:
        return fn(market) or []


def get_calendar(params):
    """Authoritative trading calendar: ContextInfo first, xtdata as fallback.

    Returns ["YYYYMMDD", ...] clipped to the requested window (default: last
    year + this year). market defaults to SH (Shanghai calendar equals the
    A-share trading-day set, same as Shenzhen). Core caches per-year and fails
    open when the bridge is down, so on failure return ok=False and let Core
    degrade.
    """
    start, end = _calendar_window(params)
    now = time.time()
    if (_CAL_CACHE["window"] == (start, end) and _CAL_CACHE["dates"]
            and now - _CAL_CACHE["ts"] <= _CAL_CACHE_TTL):
        return {"ok": True, "data": _CAL_CACHE["dates"], "cached": True}
    market = params.get("market", "SH")
    dates = []
    ctx_fn = _iq("get_trading_dates")
    if ctx_fn is not None:
        try:
            dates = _normalize_calendar(
                _ctx_trading_dates(ctx_fn, market, start, end), start, end)
        except Exception as e:
            _throttled_log(("calendar-ctx",),
                           "[BRIDGE] %s calendar ContextInfo FAIL: %s: %s"
                           % (_ts(), type(e).__name__, e))
    if not dates:
        try:
            from xtquant import xtdata
            dates = _normalize_calendar(
                xtdata.get_trading_dates(market, start, end, -1), start, end)
        except Exception as e:
            _throttled_log(("calendar",),
                           "[BRIDGE] %s calendar FAIL: %s" % (_ts(), e))
            return {"ok": False, "error": "get_trading_dates failed: %s" % e}
    if not dates:
        _throttled_log(("calendar-empty",), "[BRIDGE] %s calendar empty" % _ts())
        return {"ok": False, "error": "empty calendar"}
    _CAL_CACHE["ts"] = now
    _CAL_CACHE["window"] = (start, end)
    _CAL_CACHE["dates"] = dates
    return {"ok": True, "data": dates, "cached": False}


def get_quote(params):
    code = params.get("code")
    period = params.get("period", "1m")
    if not code:
        return {"ok": False, "error": "missing code"}
    try:
        count = int(params.get("count", QUOTE_COUNT))
    except ValueError:
        count = QUOTE_COUNT

    now = time.time()
    cache_key = (code, period, count)
    cached = _quote_cache.get(cache_key)
    if cached is not None and now - cached[0] <= QUOTE_CACHE_TTL:
        return {"ok": True, "data": {code: cached[1]}, "cached": True}

    df = _fetch_quote(code, period, count)
    if df is None:
        return {"ok": False, "error": "no data for %s %s" % (code, period)}
    bars = _df_to_bars(df)
    # Self-evidencing line for "fetched N rows but HTTP returned []": throttled,
    # prints once per code/period, so a probe right after restart shows both.
    _throttled_log(("bars", code, period),
                   "[BRIDGE] %s quote rows code=%s period=%s rows_before=%d rows_after=%d"
                   % (_ts(), code, period, len(df), len(bars)))
    _quote_cache[cache_key] = (now, bars)
    return {"ok": True, "data": {code: bars}, "cached": False}


# ---------------- HTTP ----------------
def _json(obj, status=200):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
              403: "Forbidden", 404: "Not Found",
              500: "Internal Server Error"}.get(status, "OK")
    head = ("HTTP/1.1 %d %s\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n") % (status, reason, len(payload))
    return head.encode("utf-8") + payload


def _parse_request(buf):
    header_end = buf.find(b"\r\n\r\n")
    if header_end < 0:
        return None
    head = buf[:header_end].decode("utf-8", "replace")
    lines = head.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 3:
        return None
    method, path = parts[0], parts[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        cl = int(headers.get("content-length", 0))
    except ValueError:
        cl = 0
    body_start = header_end + 4
    if len(buf) < body_start + cl:
        return None
    return method, path, headers, buf[body_start:body_start + cl]


def _parse_query(query):
    params = {}
    if not query:
        return params
    for pair in query.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
    return params


def _handle(method, path, headers, body):
    """Entry: route -> respond. No auth (loopback-only, single-user host)."""
    global _shutdown_requested
    path, _, query = path.partition("?")
    path = path.rstrip("/")
    params = _parse_query(query)

    if method == "GET" and path == "/ping":
        return _json({"ok": True, "service": "iquant-bridge",
                      "port": PORT, "account": ACCOUNT,
                      "account_type": ACCOUNT_TYPE, "dry_run": DRY_RUN,
                      "ctx_ok": bool(_CTX is not None and callable(
                          getattr(_CTX, "get_market_data_ex", None))),
                      # rows in the last /positions reply without a usable cost
                      "pos_missing_cost": _POS_COST_STAT["missing"],
                      "endpoints": list(ENDPOINTS),
                      "tick": _tick_count, "served": _tick_served})

    if method == "POST" and path == "/order":
        try:
            p = json.loads(body.decode("utf-8"))
        except Exception as e:
            return _json({"ok": False, "error": "bad body: %s" % e}, 400)
        return _json(place_order(p))

    if method == "GET" and path == "/positions":
        return _json(query_positions(params))
    if method == "GET" and path == "/account":
        return _json(query_account(params))
    if method == "GET" and path == "/orders":
        return _json(query_orders(params))
    if method == "GET" and path == "/deals":
        return _json(query_deals(params))
    if method == "GET" and path == "/quote":
        return _json(get_quote(params))
    if method == "GET" and path == "/calendar":
        return _json(get_calendar(params))
    if method == "GET" and path == "/shutdown":
        # Ack first, release later: the answer below is sent by _serve_once and
        # only after that does tick() close the listening socket, so this
        # response always makes it out. Used by the next instance's init to
        # take the port over without restarting the QMT client.
        _shutdown_requested = True
        return _json({"ok": True, "shutting_down": True})

    return _json({"ok": False, "error": "unknown path %s" % path}, 404)


# ---------------- event loop (schedule_run driven; init RETURNS) ----------------
def _close_quietly(sock):
    try:
        sock.close()
    except Exception:
        pass


def _port_in_use():
    """True if something is already accepting connections on HOST:PORT.

    Short client probe used before binding. A listening socket that nobody
    accepts on still completes the TCP handshake, so connect() succeeding is
    enough to prove "someone else already owns this port". Failure/timeout
    means the port is free or nobody is listening -> let bind() decide.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect((HOST, PORT))
        return True
    except Exception:
        return False
    finally:
        _close_quietly(probe)


# port hand-over between strategy restarts: QMT does NOT close the listening
# socket when a strategy is stopped -- the handle survives inside the same QMT
# process, so the next instance can never bind. Instead of forcing a full client
# restart, the new instance asks the old one to release the port via /shutdown.
KICK_ATTEMPTS = 3                 # how many polite shutdown requests before FATAL
KICK_INTERVAL = 0.7               # seconds between attempts
KICK_TIMEOUT = 0.5                # connect/read timeout of one shutdown request


def _request_shutdown():
    """Ask the bridge already holding HOST:PORT to release it. Never raises."""
    req = ("GET /shutdown HTTP/1.1\r\nHost: %s\r\n\r\n" % HOST).encode("ascii")
    conn = None
    try:
        conn = socket.create_connection((HOST, PORT), KICK_TIMEOUT)
        conn.settimeout(KICK_TIMEOUT)
        conn.sendall(req)
        conn.recv(4096)           # read the ack; the body does not matter
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            _close_quietly(conn)


def _release_port(message):
    """Close the listener + every open connection and clear the tables.

    Safe to call from /shutdown (end of tick), stop() and init(): any response
    being written is already flushed by _serve_once before we get here.
    """
    global _listen_sock, _shutdown_requested
    _reset_connections()
    if _listen_sock is not None:
        _close_quietly(_listen_sock)
        _listen_sock = None
    _shutdown_requested = False
    print("[BRIDGE] %s %s" % (_ts(), message))


def _reset_connections():
    """Drop every open connection (used when init re-runs on a strategy restart)."""
    for conn in list(_clients):
        _close_quietly(conn)
    _clients.clear()


def _accept_pending(listen_sock):
    """Accept every pending connection (up to the OS backlog), not just one.

    The old loop accepted at most one connection per iteration with a 10ms
    sleep, which throttled bursts to ~100 conns/s; Core polls with several
    concurrent requests, so drain the backlog instead.
    """
    accepted = 0
    while True:
        try:
            conn, _addr = listen_sock.accept()
        except BlockingIOError:
            return accepted
        except Exception as e:
            _log_exception("accept", e)
            return accepted
        try:
            conn.setblocking(False)
        except Exception as e:
            _log_exception("accept-setblocking", e)
            _close_quietly(conn)
            continue
        _clients[conn] = [b"", time.time()]
        accepted += 1


def _read_once(conn):
    """One non-blocking read: bytes = data, b"" = peer closed, None = nothing yet."""
    try:
        return conn.recv(65536)
    except BlockingIOError:
        return None
    except Exception as e:
        _log_exception("recv", e)
        return b""


def _send_response(conn, payload):
    """Send a full response under a short blocking timeout.

    sendall() on a non-blocking socket can raise BlockingIOError for larger
    responses (/quote with hundreds of bars); with a real timeout we either send
    everything or close the connection -- never silently truncate the JSON.
    """
    try:
        conn.settimeout(SEND_TIMEOUT)
        conn.sendall(payload)
        return True
    except Exception as e:
        _log_exception("send", e)
        return False


def _drop(conn):
    _close_quietly(conn)
    _clients.pop(conn, None)


def _serve_once(timeout):
    """One select-guarded pass: accept + read + dispatch + respond.

    Returns the number of requests answered in this pass. Incomplete requests
    (headers/body split across packets) stay in _clients for a later pass/tick.
    """
    listen_sock = _listen_sock
    if listen_sock is None:
        return 0
    try:
        readable = select.select([listen_sock] + list(_clients), [], [], timeout)[0]
    except Exception as e:
        _log_exception("select", e)
        return 0
    if listen_sock in readable:
        _accept_pending(listen_sock)
    served = 0
    for conn in readable:
        if conn is listen_sock:
            continue
        state = _clients.get(conn)
        if state is None:
            continue
        data = _read_once(conn)
        if data is None:
            continue
        if not data:
            _drop(conn)               # peer closed (also the normal path after a response)
            continue
        state[0] += data
        state[1] = time.time()
        if len(state[0]) > MAX_REQUEST_BYTES:
            _throttled_log(("big-request",),
                           "[BRIDGE] %s request larger than %d bytes, closing connection"
                           % (_ts(), MAX_REQUEST_BYTES))
            _drop(conn)
            continue
        req = _parse_request(state[0])
        if req is None:
            continue                  # incomplete -> wait for the next read/tick
        try:
            payload = _handle(*req)
        except Exception as e:
            _log_exception("handle", e)
            payload = _json({"ok": False, "error": "internal error: %s" % e}, 500)
        if _send_response(conn, payload):
            served += 1
        _drop(conn)                   # Connection: close -- one request per connection
    return served


def _expire_idle(now):
    """Close connections that never completed a request (half-open / dead peer).

    With connection state living across ticks, an unparseable or abandoned
    connection would otherwise be re-selected on every tick forever.
    """
    for conn in list(_clients):
        state = _clients.get(conn)
        if state is None:
            continue
        if now - state[1] > CONN_IDLE_TIMEOUT:
            _throttled_log(("conn-idle",),
                           "[BRIDGE] %s closing connection idle %.0fs without a complete request"
                           % (_ts(), now - state[1]))
            _drop(conn)


def tick(ContextInfo=None):
    """Scheduled callback: serve HTTP for a bounded slice, then RETURN.

    Bounded by TICK_BUDGET_SECONDS so the strategy thread is released on every
    firing (minute-level scheduling, if ever required, just raises the budget --
    see TICK_INTERVAL in the header). Returns early when idle, so an idle bridge
    costs almost nothing per firing. Unfinished requests stay in _clients and are
    continued on the next tick.
    """
    global _tick_count, _tick_served, _last_heartbeat
    _tick_count += 1
    if _listen_sock is not None:
        deadline = time.time() + TICK_BUDGET_SECONDS
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            served = _serve_once(min(remaining, 0.05))
            _tick_served += served
            if _shutdown_requested:
                break                 # response already flushed inside _serve_once
            if not _clients and not served:
                break                 # nothing pending -> release the thread now
        _expire_idle(time.time())
        if _shutdown_requested:
            # /shutdown was served this tick: now it is safe to stop listening.
            # Later ticks see _listen_sock is None and only print the heartbeat.
            _release_port("shutdown requested, port released")
        # flush the 60s quote-summary window from the tick (not from
        # _record_quote_ok): a poll is a burst of ~80 fetches in ~5s then idle
        # ~55s, so flush-on-next-fetch would lag a whole poll cycle and could
        # miss entirely if no second poll arrives in the observation window.
        if _summary_since and time.time() - _summary_since >= SUMMARY_INTERVAL:
            _flush_quote_summary()
    now = time.time()
    if now - _last_heartbeat >= HEARTBEAT_INTERVAL:
        _last_heartbeat = now
        # served_total is CUMULATIVE since init (never reset per heartbeat), so
        # it must never go back to 0 unless the strategy was restarted.
        print("[BRIDGE] %s tick#%d alive  listen=%s:%d  served_total=%d  conns=%d"
              % (_ts(), _tick_count, HOST, PORT, _tick_served, len(_clients)))


def init(ContextInfo):
    """QMT strategy init: bind 127.0.0.1:PORT, schedule tick(), and RETURN.

    Must NOT block. In the current client build a blocked init never runs its
    loop body: the port shows LISTENING and the TCP handshake completes, but no
    byte is ever read, so every request hangs (the 2026-11 incident). The
    supported shape -- used by every QMT strategy sample -- is to register a
    scheduled callback and return; the callback may block for seconds.
    """
    global _CTX, _listen_sock, _last_heartbeat
    _CTX = ContextInfo
    try:
        ContextInfo.set_account(ACCOUNT)
    except Exception as e:
        print("[BRIDGE] %s set_account failed: %s" % (_ts(), e))
    ContextInfo.accID = ACCOUNT

    if _listen_sock is not None:
        # strategy restart in the same interpreter: release the old port first
        _reset_connections()
        _close_quietly(_listen_sock)
        _listen_sock = None

    # Pre-bind probe: a leftover listener (previous strategy instance whose
    # socket handle leaked inside the QMT process) keeps completing TCP
    # handshakes and stealing every connection, so our bind can still "succeed"
    # while every request is swallowed. Ask it to release the port first; only
    # if that fails do we give up loudly.
    if _port_in_use():
        for attempt in range(1, KICK_ATTEMPTS + 1):
            print("[BRIDGE] %s port busy, asking old instance to shutdown (attempt %d)"
                  % (_ts(), attempt))
            _request_shutdown()
            time.sleep(KICK_INTERVAL)
            if not _port_in_use():
                print("[BRIDGE] %s old instance released the port" % _ts())
                break

    if _port_in_use():
        print("[BRIDGE] %s FATAL: %s:%d already in use -- an old bridge/listener is "
              "still there and would swallow every request (bind-ok-but-no-response). "
              "Stop this strategy in QMT, then start it again; if it still fails, "
              "restart the QMT client. tick NOT scheduled, nothing will answer."
              % (_ts(), HOST, PORT))
        return

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: SO_REUSEADDR lets a SECOND bind succeed on a port that is
            # already bound, and the old (possibly leaked) socket keeps receiving
            # the connections -- the 2026-11 "bind ok but /ping times out" bug.
            # EXCLUSIVEADDRUSE makes the second bind FAIL instead of silently
            # stealing half the traffic. Verified available on Python 3.6.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # POSIX: SO_REUSEADDR only skips TIME_WAIT, it does not allow two
            # live listeners -- safe as-is.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(32)
        s.settimeout(0)               # non-blocking accept; tick() polls via select
        _listen_sock = s
    except Exception as e:
        _log_exception("bind", e)
        print("[BRIDGE] %s FATAL: bind/listen failed on %s:%d (%s: %s). "
              "Old instance not exited? Stop this strategy in QMT and start it "
              "again; if it still fails, restart the QMT client. "
              "tick NOT scheduled, nothing will answer."
              % (_ts(), HOST, PORT, type(e).__name__, e))
        return

    _last_heartbeat = 0.0             # let the very first tick print the heartbeat
    print("[BRIDGE] %s bind ok %s:%d account=%s type=%s dry_run=%s"
          % (_ts(), HOST, PORT, ACCOUNT, ACCOUNT_TYPE, DRY_RUN))

    schedule = getattr(ContextInfo, "schedule_run", None)
    if schedule is None:
        print("[BRIDGE] %s FATAL: ContextInfo has no schedule_run; the bridge needs a "
              "scheduled tick to serve HTTP and init must not block. NOTHING WILL ANSWER."
              % _ts())
        return
    try:
        # Same 5-arg form as the official samples:
        #   schedule_run(func, start_time, -1, interval, tag)
        schedule(tick, dt.datetime.now(), -1, TICK_INTERVAL, TICK_TAG)
    except Exception as e:
        _log_exception("schedule_run", e)
        print("[BRIDGE] %s schedule_run FAILED -- no driver, requests will NOT be served. "
              "Check the signature/interval support (minute fallback: TICK_INTERVAL="
              "timedelta(minutes=1), TICK_BUDGET_SECONDS=55)." % _ts())
        return
    print("[BRIDGE] %s init done: tick scheduled every %.1fs (tag=%s); init returns now"
          % (_ts(), TICK_INTERVAL.total_seconds(), TICK_TAG))


def handlebar(ContextInfo):
    """Not used: the scheduled tick drives the bridge. Kept for framework requirement."""
    pass


def stop(ContextInfo):
    """QMT calls this when the strategy is stopped: release the port so the next
    start can bind WITHOUT restarting the whole client.

    Not every client build calls this hook, and it must never raise: a failure
    only prints. If the hook is skipped, the leaked socket is still recovered by
    the next init's /shutdown kick.
    """
    try:
        _release_port("strategy stopping, port released")
    except Exception as e:
        try:
            print("[BRIDGE] %s stop cleanup failed: %s: %s"
                  % (_ts(), type(e).__name__, e))
        except Exception:
            pass
