# -*- coding: utf-8 -*-
"""掘金量化桥（goldminer_bridge）——标准掘金策略格式。

用途：桥接本平台（tq-iquant-web）与掘金终端。协议与 live/bridge/iquant_bridge.py
一致（HTTP JSON），平台 HttpBridgeDispatcher 零改动接入。

端点：
  GET  /ping      在线探测（service / account / endpoints + gm 健康字段）
  GET  /positions 持仓
  GET  /account   资金
  GET  /orders    当日委托（含柜台拒单；支持 ?order_id= 精确认领）
  GET  /deals     当日成交（支持 ?order_id=）
  GET  /quote     实时 bar
  GET  /calendar  交易日历（end 缺省=今天）
  POST /order     下单，返回真实委托结果（cl_ord_id / accepted / rejected / status / reason）
  POST /cancel    撤单（入参 cl_ord_id，兼容 order_id），返回撤单结果
  POST /shutdown  自行退出桥进程（须 {"confirm": true}），替代 taskkill 强杀

部署（掘金终端内）：
1. 掘金终端 → 我的策略 → 新建 Python 策略（仿真账户）→ 策略 ID 记下来
2. 将终端分配的策略 ID 填到下方 STRATEGY_ID
3. 用本文件内容覆盖策略文件 → 点击运行（终端须已登录）

注意：与 QMT 桥（init 阻塞主循环）不同——掘金策略由 run() 驱动事件循环，
HTTP 服务放后台线程，查询/下单经 gm RPC 从线程调用。
"""
import json
import os
import threading
import time
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from gm.api import (set_token, set_account_id, order_volume, order_cancel, run,
                    get_cash, get_position, get_execution_reports, get_orders,
                    get_trading_dates, history_n, MODE_LIVE)
from gm.enum import (OrderSide_Buy, OrderSide_Sell, OrderType_Limit,
                     PositionEffect_Open, PositionEffect_Close)

# ====== 按你的掘金账户预填（长线法宝 · 仿真）======
TOKEN = "ab689f1e127e2b5e684e30a6b07c9585f088d44a"
ACCOUNT = "0e5343e4-765b-11ee-871e-00163e022aa6"
STRATEGY_ID = "填入终端分配的策略ID"      # ← 掘金终端新建策略后替换
HOST = "127.0.0.1"
PORT = 7801                              # 平台 goldminer.base_url 指向此端口
DRY_RUN = False
# 下单应答后是否再短等一次日内委托查询确认拒单。柜台拒单（如「委托价超出涨跌停
# 范围」）以异步回报到达，下单返回值里状态往往还是 PendingNew/New，只靠返回值会
# 漏判；多打一次 gm 查询约增加数秒延迟（平台下单超时会降级为「在途待确认」，
# 不会误判成交），因此默认开启。置 False 即退化为「仅看返回值 + /orders 台账回显」。
CONFIRM_REJECT = True
CONFIRM_DELAY_SECONDS = 0.5
# /shutdown 响应发出去后再等多久退出进程（保证 HTTP 应答不被掐断）
SHUTDOWN_DELAY_SECONDS = 0.4
# ====================================================

# gm RPC 串行锁：掘金 SDK 的查询/下单非保证线程安全，而 HTTP 服务是
# ThreadingHTTPServer（每请求一线程）——所有 gm 调用持锁串行。
_GM_LOCK = threading.RLock()
# 桥启动时刻：get_execution_reports / get_orders 只采信此后的回报——终端里若还有
# 其他策略/手工单（同账户），旧回报不得混入平台的委托认领（防串单）。
_BRIDGE_START = datetime.datetime.now()
# /quote 结果缓存：(code, period, count) -> (时间戳, rows)；55s 过期与节拍对齐
_QUOTE_CACHE = {}

# ---------------- gm 健康可见性（/ping 只读回显，零 gm 调用） ----------------
# SDK 的连接类故障（1200/1201 行情、1100/1101 交易消息）只打到 on_error 与控制台，
# 而 /ping 是纯内存应答——平台心跳因此看不见 gm 内部断连（2026-11-06 夜间实证：
# 08:59:35 交易消息服务断 9 秒，平台侧零感知）。这里把「最近一次 gm 调用成功/失败」
# 记进内存，由 /ping 增量暴露；**任何情况下都不在 /ping 里调用 gm**，否则会把
# C 扩展的 GIL 阻塞重新带回心跳（那正是上一轮性能事故的根源）。
_GM_HEALTH_LOCK = threading.Lock()
_GM_LAST_OK_AT = None          # 最近一次 gm 调用成功时刻 "YYYY-mm-dd HH:MM:SS"
_GM_LAST_ERR_AT = None         # 最近一次 gm 调用失败 / on_error 时刻
_GM_LAST_ERR_CODE = None       # 错误码（GmError.status / on_error 的 code）
_GM_LAST_ERR_INFO = None       # 错误原文（截断 300 字）
_GM_ERR_COUNT = 0              # 自桥启动累计错误数
_GM_ERR_RING = []              # 最近 20 条 {"at","code","info"}——全量留痕，不节流
_GM_ERR_RING_MAX = 20
_GM_ERR_THROTTLE = {}          # code -> 上次打印时刻（同类 60s 内只打一次）
_GM_ERR_THROTTLE_SECONDS = 60.0
_GM_CONN_CODES = ("1200", "1201", "1100", "1101")   # 连接类记 WARNING，其余 ERROR

# ---------------- 桥侧委托台账（幂等 / 认领 / 拒单回显） ----------------
# gm 的 order_volume 不接受 remark/user_param（Order 的 properties 是 map，
# order_volume/order_batch 均无途径写入），SDK 的 cl_ord_id 也由 SDK 自行生成，
# 平台传入的 order_id/remark 无法落到柜台。因此桥侧自建台账：
#   cl_ord_id -> 记录（代码/方向/数量/价格/状态/拒单原文/平台 order_id/remark）
#   platform order_id 与 remark -> cl_ord_id（幂等去重 + /orders 精确认领）
_LEDGER_LOCK = threading.RLock()
_LEDGER = {}
_LEDGER_SEQ = []
_PLATFORM_INDEX = {}

# gm OrderStatus（数值，pb_to_dict 默认把枚举转 int）-> 文本
_ORDER_STATUS_TEXT = {
    0: "Unknown", 1: "New", 2: "PartiallyFilled", 3: "Filled", 4: "DoneForDay",
    5: "Canceled", 6: "PendingCancel", 7: "Stopped", 8: "Rejected",
    9: "Suspended", 10: "PendingNew", 11: "Calculated", 12: "Expired",
    13: "AcceptedForBidding", 14: "PendingReplace",
}
# 文本 -> QMT m_nOrderStatus（平台 order_machine 消费）：
#   49=待报 50=已报 51=已报待撤 54=已撤 55=部成 56=已成 57=废单(柜台拒单)
_ORDER_STATUS_IQ = {
    "New": 50, "AcceptedForBidding": 50, "PendingNew": 49, "PendingReplace": 49,
    "PartiallyFilled": 55, "Filled": 56, "Canceled": 54, "Expired": 54,
    "DoneForDay": 54, "Stopped": 54, "Rejected": 57, "PendingCancel": 51,
    "Suspended": 51, "Unknown": 51,
}
# 已终态（不可撤单 / 不再变化）
_FINAL_STATUS = ("Filled", "Canceled", "Rejected", "Expired", "DoneForDay", "Stopped")
# gm ExecType 数值：15=成交（pb_to_dict 默认枚举转 int，兼容旧版文本 "Trade"）
_EXEC_TYPE_TRADE = (15, "Trade", "15")


def to_gm_symbol(code):
    """平台 600000.SH / 000001.SZ / 430047.BJ → 掘金 SHSE./SZSE./BSE. 前缀式。"""
    code = (code or "").strip().upper()
    if "." in code:
        num, _, suf = code.partition(".")
        ex = {"SH": "SHSE", "SZ": "SZSE", "BJ": "BSE"}.get(suf, "SZSE")
        return "%s.%s" % (ex, num)
    return code


def _s(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gm_mark_ok():
    """记录一次 gm 调用成功（只写内存，无锁竞争以外的开销）。"""
    global _GM_LAST_OK_AT
    ts = _now_str()
    with _GM_HEALTH_LOCK:
        _GM_LAST_OK_AT = ts


def _gm_mark_error(code, info):
    """记录一次 gm 错误：更新最近错误字段 + 环形缓冲 + 累计计数。"""
    global _GM_LAST_ERR_AT, _GM_LAST_ERR_CODE, _GM_LAST_ERR_INFO, _GM_ERR_COUNT
    ts = _now_str()
    code_s = str(code)
    info_s = str(info)[:300]
    with _GM_HEALTH_LOCK:
        _GM_LAST_ERR_AT = ts
        _GM_LAST_ERR_CODE = code_s
        _GM_LAST_ERR_INFO = info_s
        _GM_ERR_COUNT += 1
        _GM_ERR_RING.append({"at": ts, "code": code_s, "info": info_s})
        del _GM_ERR_RING[:-_GM_ERR_RING_MAX]
    return code_s, info_s


def _gm_should_print(code):
    """同类错误 60s 内只打印一次（环形缓冲不受节流影响，全量留痕）。"""
    now = time.time()
    with _GM_HEALTH_LOCK:
        last = _GM_ERR_THROTTLE.get(code)
        if last is not None and now - last < _GM_ERR_THROTTLE_SECONDS:
            return False
        _GM_ERR_THROTTLE[code] = now
        return True


def _gm_call(fn, *args, **kwargs):
    """调用 gm API 并记录健康状态：成功记时刻；异常记码/原文后**原样抛出**。

    错误码优先取 GmError.status（gm/api/_errors.py:10），否则用异常类型名。
    所有 gm 调用统一走这里，保证 /ping 的 gm_* 字段始终反映真实链路状态。
    """
    try:
        r = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "status", None)
        _gm_mark_error(code if code is not None else type(e).__name__, e)
        raise
    _gm_mark_ok()
    return r


def _gm_health():
    """/ping 的 gm 健康字段快照（纯内存读，绝不触发 gm 调用）。"""
    with _GM_HEALTH_LOCK:
        return {
            "gm_ok_at": _GM_LAST_OK_AT,
            "gm_err_at": _GM_LAST_ERR_AT,
            "gm_err_code": _GM_LAST_ERR_CODE,
            "gm_err_info": _GM_LAST_ERR_INFO,
            "gm_err_count": _GM_ERR_COUNT,
            "gm_last_errors": [dict(x) for x in _GM_ERR_RING],
        }


def _dt_str(v):
    if v is None:
        return ""
    try:
        return v.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(v)


def _status_text(v):
    """gm 状态（int 枚举号 / 文本枚举名 / 纯数字串）→ 统一文本名。"""
    if isinstance(v, str):
        name = v.split(".")[-1]
        if name.isdigit():
            return _ORDER_STATUS_TEXT.get(int(name), "Unknown")
        return name
    try:
        return _ORDER_STATUS_TEXT.get(int(v), "Unknown")
    except (TypeError, ValueError):
        return str(v)


def _iq_status(v):
    return _ORDER_STATUS_IQ.get(_status_text(v), 51)


# 方向未知时的节流告警（同类 60s 只打一次；只带关键字段，不 dump 整行）
_SIDE_WARN_LOCK = threading.Lock()
_SIDE_WARN_LAST = {}
_SIDE_WARN_SECONDS = 60.0


def _side_of(v):
    """gm side（1=Buy / 2=Sell，或文本枚举名）→ "buy" / "sell" / ""。

    缺失或无法识别一律返回空串（未知），绝不默认成卖——方向猜错的成交回填
    比丢弃整笔更危险，调用方须省略 direction 并告警。
    """
    if isinstance(v, str):
        s = v.split(".")[-1].strip()
        if s.isdigit():
            return {1: "buy", 2: "sell"}.get(int(s), "")
        low = s.lower()
        if "buy" in low:
            return "buy"
        if "sell" in low:
            return "sell"
        return ""
    try:
        return {1: "buy", 2: "sell"}.get(int(v), "")
    except (TypeError, ValueError):
        return ""


def _warn_unknown_side(where, symbol, ref, raw):
    """方向缺失/无法识别告警（节流）：只打关键字，避免整行 dump 敏感字段。"""
    now = time.time()
    with _SIDE_WARN_LOCK:
        last = _SIDE_WARN_LAST.get(where)
        if last is not None and now - last < _SIDE_WARN_SECONDS:
            return
        _SIDE_WARN_LAST[where] = now
    print("[GM-BRIDGE] %s unknown side, direction omitted: symbol=%s ref=%s side=%r"
          % (where, symbol or "", ref or "", raw))


def _is_bridge_time(dt):
    """是否属于「桥启动之后的当日」回报（过滤同账户他策略/手工单）。"""
    if dt is None:
        return True
    try:
        if hasattr(dt, "date") and dt.date() != datetime.date.today():
            return False
        return dt >= _BRIDGE_START
    except Exception:  # noqa: BLE001
        return True


# ---------------- 台账操作 ----------------

def _ledger_put(rec):
    with _LEDGER_LOCK:
        cl = rec.get("cl_ord_id")
        if cl:
            if cl not in _LEDGER:
                _LEDGER_SEQ.append(cl)
            _LEDGER[cl] = rec
        for key in (rec.get("platform_order_id"), rec.get("remark")):
            if key:
                _PLATFORM_INDEX[str(key)] = cl
        return rec


def _ledger_update(cl_ord_id, **fields):
    with _LEDGER_LOCK:
        rec = _LEDGER.get(cl_ord_id)
        if rec is None:
            return None
        rec.update(fields)
        rec["updated_at"] = _now_str()
        return dict(rec)


def _ledger_get(cl_ord_id):
    with _LEDGER_LOCK:
        rec = _LEDGER.get(cl_ord_id)
        return dict(rec) if rec else None


def _ledger_by_platform(order_id=None, remark=None):
    """按平台 order_id / remark 找台账记录（幂等回放与认领用）。"""
    with _LEDGER_LOCK:
        for key in (order_id, remark):
            if not key:
                continue
            cl = _PLATFORM_INDEX.get(str(key))
            if cl and cl in _LEDGER:
                return dict(_LEDGER[cl])
    return None


def _ledger_all():
    with _LEDGER_LOCK:
        return [dict(_LEDGER[cl]) for cl in _LEDGER_SEQ if cl in _LEDGER]


def _make_record(cl_ord_id, code, op, volume, price, status_text, reason,
                 platform_order_id=None, remark=None, raw=None):
    rejected = status_text == "Rejected" or bool(reason)
    return {
        "cl_ord_id": cl_ord_id,
        "platform_order_id": platform_order_id,
        "remark": remark,
        "code": code,
        "symbol": to_gm_symbol(code),
        "op": op,
        "volume": volume,
        "price": price,
        "status_text": status_text,
        "status": _iq_status(status_text),
        "reason": reason or "",
        "accepted": not rejected,
        "rejected": rejected,
        "created_at": _now_str(),
        "updated_at": _now_str(),
        "raw": raw or {},
    }


def _result_data(rec):
    """POST /order 应答的 data 段（增量字段：cl_ord_id/accepted/rejected/status/reason）。

    平台 HttpBridgeDispatcher 从 data 里读 cl_ord_id 作为委托标识，并对 ok:false
    抛 BridgeOrderRejected（回显 error）。ok/data/error 语义保持不变。
    """
    return {
        "cl_ord_id": rec.get("cl_ord_id"),
        "accepted": bool(rec.get("accepted")),
        "rejected": bool(rec.get("rejected")),
        "status": rec.get("status_text"),
        "reason": rec.get("reason") or "",
        "order_id": rec.get("platform_order_id"),
        "remark": rec.get("remark"),
        "raw": rec.get("raw") or {},
    }


def _order_snapshot(o):
    """gm 委托对象 → 可 JSON 序列化的精简快照（放进 raw 便于排查）。"""
    return {
        "cl_ord_id": str(_s(o, "cl_ord_id", default="") or ""),
        "order_id": str(_s(o, "order_id", "ex_ord_id", default="") or ""),
        "symbol": str(_s(o, "symbol", default="") or ""),
        "side": _s(o, "side", default=None),
        "position_effect": _s(o, "position_effect", default=None),
        "price": float(_s(o, "price", default=0) or 0),
        "volume": int(_s(o, "volume", default=0) or 0),
        "status": _status_text(_s(o, "status", default=None)),
        "ord_rej_reason": _s(o, "ord_rej_reason", default=None),
        "ord_rej_reason_detail": str(_s(o, "ord_rej_reason_detail", default="") or ""),
        "created_at": _dt_str(_s(o, "created_at", default=None)),
    }


def _invalid_order(msg, oid, remark):
    """参数非法：直接回 ok:false（status=Invalid），不送柜台、不进台账。"""
    return {"ok": False, "error": msg,
            "data": {"cl_ord_id": None, "accepted": False, "rejected": False,
                     "status": "Invalid", "reason": msg,
                     "order_id": oid, "remark": remark}}


# ---------------- 业务端点（gm RPC，线程内独立连接调用） ----------------

def place_order(p):
    """POST /order：下单并返回真实委托结果（不截断、不吞异常）。

    - 已有字段 ok/data/error 语义不变；data 增补 cl_ord_id/accepted/rejected/status/reason。
    - 柜台拒单（ord_rej_reason_detail 有值或状态 Rejected）→ ok:false + 拒单原文。
    - 下单参数/网络异常 → ok:false + 原文，绝不假装受理。
    - 平台 order_id/remark 落桥侧台账（gm SDK 无对应字段），/orders 回显 remark 供认领。
    """
    code = str(p.get("code") or "").strip()
    oid = str(p.get("order_id") or "").strip() or None
    remark = str(p.get("remark") or "").strip() or (oid[:20] if oid else None)
    # 参数缺失/非法一律拒单：绝不默认 buy/sell，也绝不把 0 股 / 0 价送进柜台
    # （0 价限价单会被柜台拒或异常成交，比直接报错危险）。
    op = str(p.get("op") or "").strip().lower()
    if not code:
        return _invalid_order("missing code", oid, remark)
    if op not in ("buy", "sell"):
        return _invalid_order("invalid op: %r (expected buy/sell)" % (p.get("op"),),
                              oid, remark)
    try:
        volume = int(p.get("volume"))
    except (TypeError, ValueError):
        return _invalid_order("invalid volume: %r" % (p.get("volume"),), oid, remark)
    if volume <= 0:
        return _invalid_order("volume must be > 0: %r" % (p.get("volume"),),
                              oid, remark)
    try:
        price = float(p.get("price"))
    except (TypeError, ValueError):
        return _invalid_order("invalid price: %r" % (p.get("price"),), oid, remark)
    if price <= 0:
        return _invalid_order("price must be > 0: %r" % (p.get("price"),),
                              oid, remark)
    sym = to_gm_symbol(code)
    side = OrderSide_Buy if op == "buy" else OrderSide_Sell
    # A 股普通股票现货：买入=开仓，卖出=平仓（PositionEffect_Close）。
    # gm 的 CloseToday/CloseYesterday 是期货平今/平昨语义，股票卖出统一用 Close。
    effect = PositionEffect_Open if op == "buy" else PositionEffect_Close

    if DRY_RUN:
        rec = _make_record(None, code, op, volume, price, "DryRun", "",
                           platform_order_id=oid, remark=remark)
        _ledger_put(rec)
        data = _result_data(rec)
        data["dry_run"] = True
        return {"ok": True, "dry_run": True, "data": data}

    # 幂等：同一平台 order_id/remark 已下过单 → 回放台账，不重复提交
    prev = _ledger_by_platform(oid, remark)
    if prev is not None:
        return {"ok": bool(prev.get("accepted")),
                "error": prev.get("reason") if prev.get("rejected") else None,
                "data": _result_data(prev),
                "result": "replay cl_ord_id=%s status=%s"
                          % (prev.get("cl_ord_id"), prev.get("status_text"))}

    try:
        with _GM_LOCK:
            r = _gm_call(order_volume, symbol=sym, volume=volume, side=side,
                         order_type=OrderType_Limit, position_effect=effect,
                         price=price, account=ACCOUNT)
    except Exception as e:
        msg = "order_volume raised: %s" % e
        return {"ok": False, "error": msg,
                "data": {"cl_ord_id": None, "accepted": False, "rejected": False,
                         "status": "Error", "reason": msg,
                         "order_id": oid, "remark": remark}}

    if not r:
        # SDK 文档：下单出错返回空列表（无 cl_ord_id，无法追踪）
        msg = "order_volume returned empty list (SDK/网关未受理，无 cl_ord_id)"
        return {"ok": False, "error": msg,
                "data": {"cl_ord_id": None, "accepted": False, "rejected": False,
                         "status": "Unknown", "reason": msg,
                         "order_id": oid, "remark": remark}}

    o = r[0]
    cl = str(_s(o, "cl_ord_id", default="") or "")
    status_text = _status_text(_s(o, "status", default=None))
    reason = str(_s(o, "ord_rej_reason_detail", default="") or "")
    rec = _make_record(cl, code, op, volume, price, status_text, reason,
                       platform_order_id=oid, remark=remark,
                       raw=_order_snapshot(o))
    _ledger_put(rec)

    if rec["rejected"]:
        msg = reason or "order rejected by broker (status=%s)" % status_text
        return {"ok": False, "error": msg, "data": _result_data(rec),
                "result": "cl_ord_id=%s status=%s" % (cl, status_text)}

    # 柜台拒单多为异步回报：下单应答时状态仍是 PendingNew/New，短等一次查询确认
    if CONFIRM_REJECT:
        rej = _confirm_reject(cl)
        if rej:
            rec = _ledger_get(cl) or rec
            return {"ok": False, "error": rej, "data": _result_data(rec),
                    "result": "cl_ord_id=%s status=%s"
                              % (cl, rec.get("status_text"))}

    rec = _ledger_get(cl) or rec
    return {"ok": True, "data": _result_data(rec),
            "result": "cl_ord_id=%s status=%s" % (cl, rec.get("status_text"))}


def _confirm_reject(cl_ord_id):
    """下单后短等一次日内委托查询；命中柜台拒单返回拒单原文，否则 None。

    只查一次（不轮询）：gm 单次调用本身数秒，多轮会拖住 /order 的 HTTP 应答。
    查询失败/查不到该单一律返回 None（不得把已受理的单改判成拒单）。
    """
    if not cl_ord_id:
        return None
    time.sleep(CONFIRM_DELAY_SECONDS)
    try:
        with _GM_LOCK:
            rows = _gm_call(get_orders) or []
    except Exception:  # noqa: BLE001
        return None
    for o in rows:
        if str(_s(o, "cl_ord_id", default="") or "") != cl_ord_id:
            continue
        status_text = _status_text(_s(o, "status", default=None))
        reason = str(_s(o, "ord_rej_reason_detail", default="") or "")
        _ledger_update(cl_ord_id, status_text=status_text,
                       status=_iq_status(status_text), reason=reason,
                       accepted=not (status_text == "Rejected" or bool(reason)),
                       rejected=(status_text == "Rejected" or bool(reason)),
                       raw=_order_snapshot(o))
        if status_text == "Rejected" or reason:
            return reason or "order rejected by broker (status=%s)" % status_text
        return None
    return None


def cancel_order(p):
    """POST /cancel：撤单。入参 cl_ord_id（兼容 order_id，按台账解析）。

    gm 签名：order_cancel(wait_cancel_orders)，入参 [{"cl_ord_id":..., "account_id":...}]，
    返回 None，失败抛 GmError（含 status/message/function 原文）。
    终态（已成/已撤/已拒/过期）在撤单前直接拒绝并说明，不做无意义调用。
    """
    cl = str(p.get("cl_ord_id") or "").strip()
    oid = str(p.get("order_id") or "").strip()
    if not cl and oid:
        rec = _ledger_by_platform(oid, None)
        if rec:
            cl = str(rec.get("cl_ord_id") or "")
    if not cl:
        return {"ok": False,
                "error": "cancel requires cl_ord_id (or an order_id known to bridge)"}

    rec = _ledger_get(cl) or {}
    status_text = rec.get("status_text") or ""
    if status_text in _FINAL_STATUS:
        return {"ok": False,
                "error": "cannot cancel: 委托已终态（%s）%s"
                         % (status_text, ("；" + rec["reason"]) if rec.get("reason") else ""),
                "data": {"cl_ord_id": cl, "status": status_text,
                         "reason": rec.get("reason") or ""}}

    try:
        with _GM_LOCK:
            _gm_call(order_cancel, [{"cl_ord_id": cl, "account_id": ACCOUNT}])
    except Exception as e:
        # 撤单不存在 / 已成交 / 已拒：gm 以 GmError 原文返回（status/message/function），
        # 原样透出，不做二次猜测。
        msg = str(e)
        return {"ok": False, "error": "order_cancel raised: %s" % msg,
                "data": {"cl_ord_id": cl, "status": status_text or "Unknown"}}

    verified, status_after = _verify_cancel(cl)
    if status_after == "Canceled":
        _ledger_update(cl, status_text="Canceled", status=_iq_status("Canceled"),
                       accepted=True, rejected=False)
    return {"ok": True,
            "data": {"cl_ord_id": cl, "cancel_requested": True,
                     "verified": verified,
                     "status": status_after or "PendingCancel"}}


def _verify_cancel(cl_ord_id):
    """撤单后复查委托状态（一次 get_orders）。返回 (是否回读到, 状态文本)。"""
    try:
        with _GM_LOCK:
            rows = _gm_call(get_orders) or []
    except Exception:  # noqa: BLE001
        return False, ""
    for o in rows:
        if str(_s(o, "cl_ord_id", default="") or "") != cl_ord_id:
            continue
        return True, _status_text(_s(o, "status", default=None))
    return False, ""


def query_positions(p):
    try:
        rows = []
        with _GM_LOCK:
            pos = _gm_call(get_position, account_id=ACCOUNT) or []
        for o in pos:
            sym = (_s(o, "symbol", "") or "")
            rows.append({
                "instrument": sym.split(".")[-1],
                "exchange": sym.split(".")[0],
                "volume": int(_s(o, "volume", default=0) or 0),
                "available": int(_s(o, "available", default=0) or 0),
                "cost": float(_s(o, "vwap", "cost_price", default=0) or 0),
                "market_value": float(_s(o, "market_value", default=0) or 0),
            })
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_positions: %s" % e}


def query_account(p):
    try:
        with _GM_LOCK:
            c = _gm_call(get_cash, account_id=ACCOUNT)
        row = {
            "available": float(_s(c, "available", default=0) or 0),
            "total_asset": float(_s(c, "nav", default=0) or 0),
            "market_value": float(_s(c, "market_value", default=0) or 0),
            "balance": float(_s(c, "balance", default=0) or 0),
            "frozen_cash": float(_s(c, "frozen", default=0) or 0),
            "commission": None,
            "position_profit": float(_s(c, "position_profit", default=0) or 0),
        }
        return {"ok": True, "data": [row]}
    except Exception as e:
        return {"ok": False, "error": "query_account: %s" % e}


def _today_reports():
    today = datetime.date.today()
    rows = []
    with _GM_LOCK:
        reps = _gm_call(get_execution_reports) or []
    for r in reps:
        ca = getattr(r, "created_at", None)
        if ca is not None and getattr(ca, "date", lambda: today)() != today:
            continue
        if ca is not None and ca < _BRIDGE_START:
            continue   # 桥启动前的回报（其他策略/手工单）不得混入认领
        rows.append(r)
    return rows


def _collect_order_states():
    """当日委托状态：cl_ord_id -> 快照 + 出现顺序（状态以此为准）。

    优先 get_orders（日内全部委托，含柜台拒单/废单）；查询失败或为空时退化到
    execution reports 的旧路径（只有已报回报、无拒单）。
    """
    states, seq = {}, []
    try:
        with _GM_LOCK:
            gm_orders = _gm_call(get_orders) or []
    except Exception:  # noqa: BLE001
        gm_orders = []

    for o in gm_orders:
        cl = str(_s(o, "cl_ord_id", default="") or "")
        if not cl or not _is_bridge_time(_s(o, "created_at", default=None)):
            continue
        status_text = _status_text(_s(o, "status", default=None))
        states[cl] = {
            "cl_ord_id": cl,
            "symbol": str(_s(o, "symbol", default="") or ""),
            "side": _s(o, "side", default=None),
            "price": float(_s(o, "price", default=0) or 0),
            "filled_vwap": float(_s(o, "filled_vwap", default=0) or 0),
            "volume": int(_s(o, "volume", default=0) or 0),
            "filled_volume": int(_s(o, "filled_volume", default=0) or 0),
            "filled_amount": float(_s(o, "filled_amount", default=0) or 0),
            "status_text": status_text,
            "reason": str(_s(o, "ord_rej_reason_detail", default="") or ""),
            "created_at": _s(o, "created_at", default=None),
            "source": "get_orders",
        }
        seq.append(cl)

    if states:
        return states, seq

    # 旧路径兜底：execution reports（按 cl_ord_id 聚合最新状态、累计成交量）
    for r in _today_reports():
        cl = str(getattr(r, "cl_ord_id", "") or "")
        if not cl:
            continue
        cur = states.get(cl)
        if cur is None:
            cur = {
                "cl_ord_id": cl,
                "symbol": str(getattr(r, "symbol", "") or ""),
                "side": getattr(r, "side", None),
                "price": float(_s(r, "price", default=0) or 0),
                "filled_vwap": float(_s(r, "price", default=0) or 0),
                "volume": int(_s(r, "volume", default=0) or 0),
                "filled_volume": 0,
                "filled_amount": 0.0,
                "status_text": "Unknown",
                "reason": str(_s(r, "ord_rej_reason_detail", default="") or ""),
                "created_at": _s(r, "created_at", default=None),
                "source": "exec_reports",
            }
            states[cl] = cur
            seq.append(cl)
        # ExecRpt 无 status 字段：用 exec_type 推状态（8=拒单 5=撤单 1=已报）
        et_name = str(_s(r, "exec_type", default="")).split(".")[-1]
        et_val = str(_s(r, "exec_type", default=""))
        derived = {"8": "Rejected", "5": "Canceled", "1": "New",
                   "Rejected": "Rejected", "Canceled": "Canceled", "New": "New"}
        if et_val in derived:
            cur["status_text"] = derived[et_val]
        elif et_name in derived:
            cur["status_text"] = derived[et_name]
        if str(_s(r, "exec_type", default="")) in _EXEC_TYPE_TRADE \
                or str(_s(r, "exec_type", default="")).split(".")[-1] == "Trade":
            cur["filled_volume"] += int(_s(r, "volume", default=0) or 0)
            cur["filled_amount"] += float(_s(r, "amount", default=0) or 0)
    return states, seq


def _order_row(cl, st, rec):
    """构造与既有 /orders 风格一致的委托行（增量补充 status_text/reason/remark 等）。

    方向未知（side 缺失/无法识别）时**不写 direction**（平台会按「认领不了」处理，
    绝不会误判成卖），行内 side 置空串并打节流告警。
    """
    sym = st.get("symbol") or ""
    # 与既有 /orders 一致：instrument=数字代码，exchange=交易所前缀（SHSE/SZSE/BSE）
    instrument = sym.split(".")[-1] if sym else ""
    exchange = sym.split(".")[0] if sym else ""
    filled = int(st.get("filled_volume") or 0)
    price = float(st.get("price") or 0)
    traded_price = float(st.get("filled_vwap") or 0) or price
    created = st.get("created_at")
    created_str = _dt_str(created)
    side = _side_of(st.get("side"))
    row = {
        # 既有字段
        "order_ref": cl,
        "order_sysid": cl,
        "instrument": instrument,
        "exchange": exchange,
        "side": side,
        "limit_price": price,
        "traded_price": traded_price,
        "volume": int(st.get("volume") or 0),
        "traded_volume": filled,
        "status": _iq_status(st.get("status_text")),
        "source": "BRIDGE",
        "order_type": None,
        "insert_time": created_str[-8:],
        "insert_date": created_str[:10].replace("-", ""),
        "cancel_amount": 0,
        "remark": rec.get("remark") if rec else None,
        # 增量字段
        "cl_ord_id": cl,
        "order_id": (rec.get("platform_order_id") if rec else None) or cl,
        "filled_volume": filled,
        "filled_amount": float(st.get("filled_amount") or 0),
        "status_text": st.get("status_text") or "Unknown",
        "reason": st.get("reason") or (rec.get("reason") if rec else "") or "",
    }
    if side:
        row["direction"] = 48 if side == "buy" else 49
    else:
        _warn_unknown_side("/orders", sym, cl, st.get("side"))
    return row


def query_orders(p):
    """当日委托（get_orders 主路径 + execution reports 兜底 + 桥侧台账补全）。"""
    try:
        order_id = str(p.get("order_id") or "").strip()
        states, seq = _collect_order_states()

        with _LEDGER_LOCK:
            ledger = {cl: dict(_LEDGER[cl]) for cl in _LEDGER_SEQ if cl in _LEDGER}

        rows = []
        for cl in seq:
            rows.append(_order_row(cl, states[cl], ledger.get(cl)))

        # 台账里但查询源没有的（柜台拒单未进查询、或查询失败）：按台账补行
        for cl, rec in ledger.items():
            if cl in states or not cl:
                continue
            if order_id and order_id not in (
                    cl, str(rec.get("platform_order_id") or ""),
                    str(rec.get("remark") or "")):
                continue
            rows.append(_order_row(cl, {
                "cl_ord_id": cl,
                "symbol": rec.get("symbol") or "",
                "side": rec.get("op") if rec.get("op") in ("buy", "sell") else None,
                "price": rec.get("price") or 0,
                "volume": rec.get("volume") or 0,
                "filled_volume": 0,
                "filled_amount": 0,
                "status_text": rec.get("status_text") or "Unknown",
                "reason": rec.get("reason") or "",
                "created_at": None,
            }, rec))

        if order_id:
            def _match(r):
                vals = [r.get("order_ref"), r.get("order_sysid"), r.get("cl_ord_id"),
                        r.get("order_id"), r.get("remark")]
                return any(str(v) == order_id for v in vals if v not in (None, ""))
            rows = [r for r in rows if _match(r)]
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_orders: %s" % e}


def query_deals(p):
    """当日成交明细（exec_type=Trade 的回报按笔展开）。"""
    try:
        order_id = str(p.get("order_id") or "").strip()
        with _LEDGER_LOCK:
            ledger = {cl: dict(_LEDGER[cl]) for cl in _LEDGER_SEQ if cl in _LEDGER}

        rows = []
        for r in _today_reports():
            et = _s(r, "exec_type", default="")
            et_name = str(et).split(".")[-1]
            if not (et in _EXEC_TYPE_TRADE or et_name == "Trade"
                    or (isinstance(et, int) and et == 15)):
                continue
            sym = (getattr(r, "symbol", "") or "")
            cl = str(getattr(r, "cl_ord_id", "") or "")
            rec = ledger.get(cl) or {}
            side = _side_of(getattr(r, "side", None))
            row = {
                "order_ref": cl,
                "order_sysid": cl,
                "trade_id": str(getattr(r, "exec_id", "") or ""),
                "instrument": sym.split(".")[-1],
                "exchange": sym.split(".")[0],
                "side": side,
                "price": float(_s(r, "price", default=0) or 0),
                "volume": int(_s(r, "volume", default=0) or 0),
                "amount": float(_s(r, "filled_amount", "amount", default=0) or 0),
                "commission": float(_s(r, "commission", default=0) or 0),
                "trade_time": str(_s(r, "created_at", "") or "")[-8:],
                "trade_date": str(_s(r, "created_at", "") or "")[:10].replace("-", ""),
                "source": "BRIDGE",
                "order_type": None,
                "remark": rec.get("remark"),
                "cl_ord_id": cl,
                "order_id": rec.get("platform_order_id"),
            }
            # 方向未知绝不猜：不写 direction（平台 _deal_trade_type 会判空并 WARN 跳过）
            if side:
                row["direction"] = 48 if side == "buy" else 49
            else:
                _warn_unknown_side("/deals", sym, cl, getattr(r, "side", None))
            rows.append(row)
        if order_id:
            rows = [x for x in rows if any(
                str(x.get(k)) == order_id for k in ("order_ref", "cl_ord_id", "order_id", "remark")
                if x.get(k) not in (None, ""))]
        return {"ok": True, "data": rows}
    except Exception as e:
        return {"ok": False, "error": "query_deals: %s" % e}


def get_quote(p):
    """当日实时 bar（掘金 history_n 含当日）。带 55s 结果缓存——
    BarPoller 60s 节拍内重复 /quote 直接复用，gm 云端查询只打一次。"""
    code = p.get("code", "")
    period = p.get("period", "1m")
    count = int(p.get("count", 10))
    freq = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "1d": "1d"}.get(period)
    if not code or not freq:
        return {"ok": False, "error": "code/period required"}
    ck = (code, period, count)
    now = time.time()
    hit = _QUOTE_CACHE.get(ck)
    if hit and now - hit[0] < 55:
        return {"ok": True, "data": {code: hit[1]}}
    try:
        bars = _gm_call(history_n, symbol=to_gm_symbol(code), frequency=freq,
                        count=count + 2, fields="eob,open,high,low,close,volume",
                        adjust=0, df=False)
        rows = []
        for b in (bars or []):
            eob = b.get("eob")
            rows.append({
                "stime": eob.strftime("%Y%m%d%H%M%S") if eob else "",
                "time": int(eob.timestamp() * 1000) if eob else 0,
                "open": b.get("open"), "high": b.get("high"),
                "low": b.get("low"), "close": b.get("close"),
                "volume": b.get("volume"), "amount": 0,
            })
        if rows:
            _QUOTE_CACHE[ck] = (now, rows)
        return {"ok": True, "data": {code: rows}}
    except Exception as e:
        return {"ok": False, "error": "quote: %s" % e}


def _norm_date(v):
    """日期入参归一化：空 → None；支持 YYYY-MM-DD / YYYYMMDD；非法抛 ValueError。"""
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError("invalid date %r (expect YYYY-MM-DD)" % s)


def get_calendar(p):
    """交易日历。end 为空/非法不再透传空串（gm 会抛 [ERR_DATE] 刷日志）：
    空 → 今天；格式非法 → 明确错误。"""
    try:
        start = _norm_date(p.get("start")) or "2026-01-01"
        end = _norm_date(p.get("end")) or datetime.date.today().strftime("%Y-%m-%d")
        if start > end:
            return {"ok": False,
                    "error": "calendar: start(%s) 晚于 end(%s)" % (start, end)}
        dates = _gm_call(get_trading_dates, exchange="SZSE",
                         start_date=start, end_date=end)
        return {"ok": True, "data": list(dates or [])}
    except Exception as e:
        return {"ok": False, "error": "calendar: %s" % e}


# ---------------- HTTP 服务（后台线程） ----------------

# /ping 回显的端点清单（部署后用 GET /ping 核对新增端点是否生效）
ENDPOINTS = ["/ping", "/positions", "/account", "/orders", "/deals",
             "/quote", "/calendar", "/order", "/cancel", "/shutdown"]


def ping_payload():
    """GET /ping 应答（**纯内存**：增量暴露 gm 健康字段，绝不触发任何 gm 调用）。

    既有字段 ok/service/port/account/dry_run/endpoints 原样保留，平台
    HttpBridgeDispatcher 心跳只判 ok/200，零改动兼容。
    """
    body = {"ok": True, "service": "goldminer-bridge", "port": PORT,
            "account": ACCOUNT, "dry_run": DRY_RUN,
            "endpoints": list(ENDPOINTS)}
    body.update(_gm_health())
    return body


def _exit_process(delay=None):
    """延迟退出进程（在独立 daemon 线程里执行，让 /shutdown 的响应先发出去）。"""
    time.sleep(SHUTDOWN_DELAY_SECONDS if delay is None else delay)
    print("[GM-BRIDGE] shutdown requested via /shutdown, exiting", flush=True)
    os._exit(0)


def request_shutdown(p):
    """POST /shutdown：确认后让桥进程自行退出（不再靠 taskkill /T /F 强杀）。

    必须显式 {"confirm": true}；缺省/非真 → ok:false（HTTP 200 业务错误），
    进程继续运行。合法请求先回 ok，再由后台线程延迟 SHUTDOWN_DELAY_SECONDS
    后 os._exit(0)。
    """
    confirm = p.get("confirm") if isinstance(p, dict) else None
    if not confirm:
        return {"ok": False,
                "error": 'shutdown requires {"confirm": true}; 桥进程未退出'}
    threading.Thread(target=_exit_process, name="gm-bridge-shutdown",
                     daemon=True).start()
    return {"ok": True, "data": {"shutdown": True}}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # 静默访问日志（掘金日志面板不刷屏）

    def _respond(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        try:
            if path == "/ping":
                return self._respond(ping_payload())
            if path == "/positions":
                return self._respond(query_positions(q))
            if path == "/account":
                return self._respond(query_account(q))
            if path == "/orders":
                return self._respond(query_orders(q))
            if path == "/deals":
                return self._respond(query_deals(q))
            if path == "/quote":
                return self._respond(get_quote(q))
            if path == "/calendar":
                return self._respond(get_calendar(q))
            return self._respond({"ok": False, "error": "unknown path %s" % path}, 404)
        except Exception as e:
            return self._respond({"ok": False, "error": "handler: %s" % e}, 500)

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        path = path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            p = json.loads(body.decode("utf-8")) if body else {}
            if path == "/order":
                return self._respond(place_order(p))
            if path == "/cancel":
                return self._respond(cancel_order(p))
            if path == "/shutdown":
                return self._respond(request_shutdown(p))
            return self._respond({"ok": False, "error": "unknown path %s" % path}, 404)
        except Exception as e:
            return self._respond({"ok": False, "error": "handler: %s" % e}, 500)


# ---------------- 掘金策略入口（标准格式） ----------------

def _absorb_order(order):
    """把 gm 委托回报并入桥侧台账（状态/拒单原文/成交量）。"""
    cl = str(_s(order, "cl_ord_id", default="") or "")
    if not cl:
        return
    status_text = _status_text(_s(order, "status", default=None))
    reason = str(_s(order, "ord_rej_reason_detail", default="") or "")
    fields = dict(status_text=status_text, status=_iq_status(status_text),
                  reason=reason,
                  accepted=not (status_text == "Rejected" or bool(reason)),
                  rejected=(status_text == "Rejected" or bool(reason)),
                  raw=_order_snapshot(order))
    with _LEDGER_LOCK:
        rec = _LEDGER.get(cl)
    if rec is None:
        # 非桥下单（同账户其它策略/手工单）不建台账，避免混入平台认领
        return
    _ledger_update(cl, **fields)


def on_order_status(context, order):
    """gm 委托状态回报回调：柜台异步拒单实时落台账，/orders 即时可见。"""
    try:
        _absorb_order(order)
    except Exception:  # noqa: BLE001
        pass


def on_execution_report(context, execrpt):
    """gm 成交/委托执行回报回调（本 SDK 无 on_trade；策略回调名为
    on_execution_report）：柜台拒单原文与累计成交量实时落台账。"""
    try:
        cl = str(_s(execrpt, "cl_ord_id", default="") or "")
        if not cl:
            return
        with _LEDGER_LOCK:
            rec = _LEDGER.get(cl)
        if rec is None:
            return   # 非桥下单（同账户其它策略/手工单）不建台账
        fields = {}
        reason = str(_s(execrpt, "ord_rej_reason_detail", default="") or "")
        if reason:
            fields.update(reason=reason, rejected=True, accepted=False,
                          status_text="Rejected", status=_iq_status("Rejected"))
        et = _s(execrpt, "exec_type", default=None)
        et_name = str(et).split(".")[-1]
        if et in _EXEC_TYPE_TRADE or et_name == "Trade" or str(et) == "15":
            vol = int(_s(execrpt, "volume", default=0) or 0)
            fields["filled_volume"] = int(rec.get("filled_volume") or 0) + vol
        if fields:
            _ledger_update(cl, **fields)
    except Exception:  # noqa: BLE001
        pass


def on_error(context, code, info):
    """gm SDK 错误回调接管（SDK 在 basic.py:629 自动绑定模块级 on_error）。

    只做三件事：记录健康字段/环形缓冲、按级原样打印、**不重连、不抛异常**。
    连接类（1200/1201 行情、1100/1101 交易消息）记 WARNING，其余记 ERROR；
    同类 60s 内只打印一次（控制台不刷屏），环形缓冲全量留痕、/ping 可读。
    """
    try:
        code_s, info_s = _gm_mark_error(code, info)
        level = "WARNING" if code_s in _GM_CONN_CODES else "ERROR"
        if _gm_should_print(code_s):
            print("[GM-BRIDGE][%s] code=%s info=%s" % (level, code_s, info_s),
                  flush=True)
    except Exception:  # noqa: BLE001
        # 回调内绝不能再抛：SDK 的 err_callback 会把异常当成编码错误再回调一次
        pass


def init(context):
    """掘金策略初始化：**先启动 HTTP 服务**（非阻塞——init 卡死会让服务永远
    起不来，实证 2026-11-05：init 内同步 get_cash 挂起 → 7801 无监听），
    连接校验放后台线程只打日志。SDK 事件循环由 run() 继续驱动。"""
    set_token(TOKEN)
    set_account_id(ACCOUNT)
    # SDK 已按模块级 on_error 自动绑定（basic.py:629）；这里显式再挂一次，
    # 但用 try 兜底——万一 context 对象禁止动态属性，也绝不能因此拖垮 init。
    try:
        context.on_error_fun = on_error
    except Exception:  # noqa: BLE001
        pass
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print("[GM-BRIDGE] listening on %s:%d account=%s (strategy=%s)"
          % (HOST, PORT, ACCOUNT, STRATEGY_ID), flush=True)

    def _connect_check():
        try:
            time.sleep(1.0)
            c = _gm_call(get_cash, account_id=ACCOUNT)
            print("[GM-BRIDGE] connect check: cash=%s" % str(c)[:120], flush=True)
        except Exception as e:
            print("[GM-BRIDGE] connect check failed: %s" % e, flush=True)
    threading.Thread(target=_connect_check, daemon=True).start()


if __name__ == "__main__":
    # 直接 python 运行（终端外调试用）；终端内运行由终端调 init
    # filename 必须是本文件实际文件名（终端项目里为 main.py）——run() 会按
    # 文件名重新 import 策略模块，文件名不符 = ModuleNotFoundError
    run(strategy_id=STRATEGY_ID,
        filename=os.path.basename(__file__),
        mode=MODE_LIVE, token=TOKEN)
