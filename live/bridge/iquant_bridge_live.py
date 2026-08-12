# -*- coding: gbk -*-
# iQuant in-client trading bridge -- LIVE bridge (0009 slice 1)
# =================================================================
# Runs as a Python strategy inside the iQuant client and exposes a local HTTP
# server on 127.0.0.1:8791 so the Core (main/) can place real orders and pull
# 1m/5m bars through iQuant.
#
# *** DUAL-BRIDGE MIRROR ***
# This is the LIVE bridge (port 8791, real account = real funds). The SIMULATION
# bridge (virtual funds) is iquant_bridge.py (port 8790). The two files are
# INDEPENDENT SELF-CONTAINED COPIES -- iQuant loads a strategy by pasting file
# content into its editor (not by file-path import), so the two bridges CANNOT
# share an imported module. ANY logic change MUST be applied to BOTH files by
# hand. Only the config block below (PORT / ACCOUNT_DEFAULT) differs.
#
# Mode model (two independent dimensions):
#   - simulation vs live (THIS dimension, by account/bridge): which account the
#     order goes to. Core routes by session.mode -> this bridge (live) or the
#     simulation bridge.
#   - signal-only vs real-order (the OTHER dimension): controlled by the iQuant
#     client's "live trade / simulation" START BUTTON, NOT by this bridge and
#     NOT by DRY_RUN. Even with DRY_RUN=False, starting the strategy in iQuant's
#     simulation mode makes passorder emit only a signal, no real order. DRY_RUN
#     below stays a dev-only print switch.
#
# Design basis: docs/plans/0009-iquant-http-bridge.md
#   - env constraints: threading never runs, handlebar only driven during
#     startup replay, run_time not called  -> bridge uses init blocking main
#     loop = single-thread event loop (verified 24-39ms)
#   - quotes via get_market_data_ex (get_history_data is deprecated; subscribe
#     push is unusable)  -> pull mode
#   - 5m is a native period, can pull directly
#
# NOTE: this file is intentionally pure ASCII (English comments) so it is
# readable by any editor and stays compatible with the iQuant editor's GBK
# storage. Do NOT add non-ASCII characters.
#
# Endpoints (all JSON):
#   GET  /ping                 heartbeat + bridge status
#   POST /order                place order (idempotent order_id + whitelist + rate limit)
#   GET  /positions            positions
#   GET  /account              account funds
#   GET  /orders?order_id=     order query
#   GET  /deals?order_id=      deal query
#   GET  /quote?code=&period=&count=   1m/5m/1d bars (cached)
#
# Security: bridge binds 127.0.0.1 only (loopback, single-user host), so no
# auth token is required. Defense is at the machine boundary: only local
# processes can reach port 8791. If the bridge is ever exposed off-loopback,
# add auth then. Whitelist ALLOWED_STOCKS, per-order limit MAX_VOLUME, rate
# limit RATE_LIMIT, audit log remain enforced.
import json
import os
import socket
import time
from collections import OrderedDict

# ================= config =================
HOST = "127.0.0.1"
PORT = 8791
# Real account (real funds). Override via env IQUANT_BRIDGE_ACCOUNT /
# .bridge_account file if you switch accounts without editing code.
ACCOUNT_DEFAULT = "110002348760"  # LIVE account; replace with the real funded account at deploy
ACCOUNT = None                     # loaded by load_account() below
DRY_RUN = False                    # dev-only print switch (NOT the signal/real-order control -- that is the iQuant start button)
ALLOWED_STOCKS = set()            # whitelist (empty = no restriction; configure in production)
MAX_VOLUME = 10000                # max shares per order
RATE_LIMIT = 1000                 # max RATE_LIMIT orders per RATE_WINDOW seconds
RATE_WINDOW = 10                  # rate-limit window (seconds)
QUOTE_CACHE_TTL = 1               # quote cache refresh interval (seconds)
QUOTE_COUNT = 10                  # default bar count for /quote
HISTORY_DAYS = 30                 # history depth to download before pulling bars
PLACED_MAX = 5000                 # _placed idempotency cache cap; oldest evicted past this (audit #32)
# ==========================================

_CTX = None
_listen_sock = None
_placed = OrderedDict()            # order_id -> result (idempotency), capped at PLACED_MAX (audit #32)
_placing = set()                  # in-flight order_ids
_requests = []                    # rate-limit timestamps
_quote_cache = {}                 # (code, period, count) -> (ts, bars)
_downloaded = set()               # (code, period) already history-downloaded


def load_account():
    """Account ID: env IQUANT_BRIDGE_ACCOUNT first, else .bridge_account file.

    The account is not hardcoded into the strategy (switch accounts via env var
    or a local file; the file stays out of git and is written at deploy time).
    Falls back to ACCOUNT_DEFAULT (real account placeholder).
    """
    acc = os.environ.get("IQUANT_BRIDGE_ACCOUNT", "")
    if not acc:
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bridge_account")
            with open(p, "r") as f:
                acc = f.read().strip()
        except Exception:
            pass
    return acc or ACCOUNT_DEFAULT


ACCOUNT = load_account()


# ---------------- iQuant API access (isolated for test mocks) ----------------
def _iq(name):
    """iQuant injects passorder/get_trade_detail_data/get_market_data_ex into globals()."""
    return globals().get(name)


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
    if not oid:
        return {"ok": False, "error": "missing order_id"}
    if oid in _placed:
        return _placed[oid]                       # already accepted -> return original result
    if oid in _placing:
        return {"ok": False, "error": "duplicate in-flight"}
    ok, err = check_whitelist(params.get("code"), params.get("volume", 100))
    if not ok:
        return {"ok": False, "error": err}
    if not check_rate_limit():
        return {"ok": False, "error": "rate limited"}
    _placing.add(oid)
    try:
        result = _do_place(params)
        _placed[oid] = result
        # audit #32: cap _placed to PLACED_MAX; evict oldest (insertion-ordered) to bound memory.
        # Idempotency only matters for near-term retries; a months-old order_id won't be re-sent.
        while len(_placed) > PLACED_MAX:
            _placed.popitem(last=False)
        return result
    finally:
        _placing.discard(oid)


def _do_place(params):
    code = params.get("code")
    op = params.get("op", "buy")                  # buy / sell
    volume = params.get("volume", 100)
    price = params.get("price", 0)                # 0=latest price, >0=limit price
    account = params.get("account", ACCOUNT)

    op_type = 23 if op == "buy" else 24           # passorder opType: 23 buy, 24 sell
    # prType=14 = opposite best price (client-side orderbook-price limit order):
    #   BUY takes sell1 price, SELL takes buy1 price -> immediate fill.
    #   NOT an exchange market order, so no market-order symbol/session limits.
    #   price param has no effect for prType!=11; pass 0 as placeholder.
    #   Real fill price is backfilled from /deals in a later slice.
    pr_type = 14
    print("[BRIDGE-LIVE] order %s %s prType=%s vol=%s price=%s acct=%s"
          % (op, code, pr_type, volume, price, account))

    if DRY_RUN:
        return {"ok": True, "dry_run": True,
                "params": {"code": code, "op": op, "volume": volume,
                           "price": price, "pr_type": pr_type}}

    fn = _iq("passorder")
    if fn is None:
        return {"ok": False, "error": "passorder not found in strategy namespace"}
    try:
        # iQuant real C++ signature (10-arg variant, verified returns 0 = accepted):
        # passorder(opType, orderType, accountID, orderCode, prType, price,
        #           volume, strategyName, quickTrade, ContextInfo)
        # orderType=1101 = single-stock/single-account/normal/by-share
        #   (official single-stock standard value; old 0 was non-standard).
        result = fn(op_type, 1101, account, code, pr_type, float(price), float(volume),
                    "iquant_bridge_live", 2, _CTX)
        return {"ok": True, "passorder_result": str(result)}
    except Exception as e:
        return {"ok": False, "error": "passorder raised: %s" % e}


# ---------------- query endpoints ----------------
def query_positions(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        for o in (fn(account, "STOCK", "POSITION") or []):
            rows.append({
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                "volume": getattr(o, "m_nVolume", None),
                "available": getattr(o, "m_nCanUseVolume", None),  # T+1 usable qty
                "yesterday_volume": getattr(o, "m_nYesterdayVolume", None),
                "on_road_volume": getattr(o, "m_nOnRoadVolume", None),
                "market_value": getattr(o, "m_dMarketValue", None),
            })
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_positions: %s" % e}


def query_account(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        for o in (fn(account, "STOCK", "ACCOUNT") or []):
            rows.append({
                "available": getattr(o, "m_dAvailable", None),
                "total_asset": getattr(o, "m_dAssetBalance", None),
                "market_value": getattr(o, "m_dStockValue", None),
                "balance": getattr(o, "m_dBalance", None),
                "frozen_cash": getattr(o, "m_dFrozenCash", None),
                "commission": getattr(o, "m_dCommission", None),
                "position_profit": getattr(o, "m_dPositionProfit", None),
            })
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_account: %s" % e}


def query_orders(params):
    fn = _iq("get_trade_detail_data")
    if fn is None:
        return {"ok": False, "error": "get_trade_detail_data not found"}
    account = params.get("account", ACCOUNT)
    try:
        rows = []
        for o in (fn(account, "STOCK", "ORDER") or []):
            rows.append({
                "order_ref": getattr(o, "m_strOrderRef", None),      # matching key
                "order_sysid": getattr(o, "m_strOrderSysID", None),
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                "direction": getattr(o, "m_nDirection", None),       # 48 buy / 49 sell
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
            })
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
        for o in (fn(account, "STOCK", "DEAL") or []):
            rows.append({
                "order_ref": getattr(o, "m_strOrderRef", None),      # matching key
                "order_sysid": getattr(o, "m_strOrderSysID", None),
                "trade_id": getattr(o, "m_strTradeID", None),
                "instrument": getattr(o, "m_strInstrumentID", None),
                "exchange": getattr(o, "m_strExchangeID", None),
                "direction": getattr(o, "m_nDirection", None),       # 48 buy / 49 sell
                "price": getattr(o, "m_dPrice", None),
                "volume": getattr(o, "m_nVolume", None),
                "amount": getattr(o, "m_dTradeAmount", None),
                "commission": getattr(o, "m_dCommission", None),
                "trade_time": getattr(o, "m_strTradeTime", None),
                "trade_date": getattr(o, "m_strTradeDate", None),
                "source": getattr(o, "m_strSource", None),           # BRIDGE / GUI
                "order_type": getattr(o, "m_strOrderStrategyType", None),
            })
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_deals: %s" % e}


# ---------------- quotes (get_market_data_ex pull + cache) ----------------
def _history_start(days=HISTORY_DAYS):
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y%m%d")


def _fetch_quote(code, period, count):
    # 1) try xtquant.xtdata: can pull any stock, not tied to current symbol
    try:
        from xtquant import xtdata
        xtdata.download_history_data(code, period, _history_start(), "")
        res = xtdata.get_market_data_ex([], [code], period=period, count=count)
        df = (res or {}).get(code)
        if df is not None and len(df) > 0:
            print("[BRIDGE-LIVE] xtdata ok %s %s count=%d got=%d" % (code, period, count, len(df)))
            return df
        print("[BRIDGE-LIVE] xtdata empty %s %s count=%d" % (code, period, count))
    except Exception as e:
        print("[BRIDGE-LIVE] xtdata FAIL %s %s: %s" % (code, period, e))
    # 2) fallback: ContextInfo (depends on current symbol context)
    fn = _iq("get_market_data_ex")
    if fn is None:
        return None
    try:
        res = fn([], [code], period=period, count=count, dividend_type="none")
        return (res or {}).get(code)
    except Exception as e:
        print("[BRIDGE-LIVE] ContextInfo FAIL %s %s: %s" % (code, period, e))
        return None


def _df_to_bars(df):
    if df is None:
        return []
    try:
        return df.reset_index().to_dict("records")
    except Exception:
        return []


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
    _quote_cache[cache_key] = (now, bars)
    return {"ok": True, "data": {code: bars}, "cached": False}


def _refresh_quote_cache():
    """Event-loop timer: keep default-count entries warm.

    Only refreshes cache keys whose count == QUOTE_COUNT (the bar-completion
    polling count). Non-default counts (e.g. large history pulls for formula
    injection) are fetched on demand by get_quote and left to expire on their
    own, so they are never silently replaced by a 10-bar refresh."""
    for key in list(_quote_cache.keys()):
        code, period, count = key
        if count != QUOTE_COUNT:
            continue
        df = _fetch_quote(code, period, count)
        if df is not None:
            _quote_cache[key] = (time.time(), _df_to_bars(df))


# ---------------- HTTP ----------------
def _json(obj, status=200):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
              403: "Forbidden", 404: "Not Found"}.get(status, "OK")
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
    path, _, query = path.partition("?")
    path = path.rstrip("/")
    params = _parse_query(query)

    if method == "GET" and path == "/ping":
        return _json({"ok": True, "service": "iquant-bridge-live",
                      "port": PORT, "account": ACCOUNT, "dry_run": DRY_RUN})

    if method == "POST" and path == "/order":
        try:
            p = json.loads(body.decode("utf-8"))
        except Exception as e:
            return _json({"ok": False, "error": "bad body: %s" % e}, 400)
        print("[AUDIT-LIVE] POST /order %s" % json.dumps(p, ensure_ascii=False))
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

    return _json({"ok": False, "error": "unknown path %s" % path}, 404)


# ---------------- event loop (init blocking main loop) ----------------
def init(ContextInfo):
    """iQuant strategy init. Non-blocking event loop: serve requests + refresh
    quote cache. init never returns."""
    global _CTX, _listen_sock
    _CTX = ContextInfo
    try:
        ContextInfo.set_account(ACCOUNT)
    except Exception as e:
        print("[BRIDGE-LIVE] set_account failed: %s" % e)
    ContextInfo.accID = ACCOUNT

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(32)
        s.setblocking(False)
        _listen_sock = s
        print("[BRIDGE-LIVE] bridge listening on %s:%d  (dry_run=%s)"
              % (HOST, PORT, DRY_RUN))
    except Exception as e:
        print("[BRIDGE-LIVE] socket bind/listen FAILED: %s" % e)
        return

    clients = {}
    last_quote = 0
    while True:
        # 1. accept new connections (non-blocking)
        try:
            conn, _ = s.accept()
            conn.setblocking(False)
            clients[conn] = b""
        except BlockingIOError:
            pass
        except Exception:
            pass
        # 2. serve existing connections
        for conn in list(clients):
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                continue
            except Exception:
                data = b""
            if not data:
                try:
                    conn.close()
                except Exception:
                    pass
                clients.pop(conn, None)
                continue
            clients[conn] = clients.get(conn, b"") + data
            req = _parse_request(clients[conn])
            if req is None:
                continue
            try:
                conn.sendall(_handle(*req))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            clients.pop(conn, None)
        # 3. periodic quote cache refresh
        now = time.time()
        if now - last_quote >= QUOTE_CACHE_TTL:
            _refresh_quote_cache()
            last_quote = now
        time.sleep(0.01)


def handlebar(ContextInfo):
    """Not called after init blocks. Kept for framework requirement."""
    pass
