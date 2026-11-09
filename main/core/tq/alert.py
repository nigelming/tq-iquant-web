"""通达信 TQ send_warn / send_message 薄封装。"""
from dataclasses import dataclass
from typing import List, Optional

from .utils import get_tdx_lock, get_tq

_BATCH = 50


class TQAlertError(RuntimeError):
    pass


@dataclass
class WarnItem:
    stock_code: str
    time: str
    price: str = "0"
    close: str = "0"
    volume: str = "0"
    bs_flag: str = "2"
    warn_type: str = "0"
    reason: str = ""


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _num(value, default="0") -> str:
    try:
        float(str(value))
        return str(value)
    except (TypeError, ValueError):
        return default


class TQAlert:
    def preflight(self) -> Optional[str]:
        try:
            tq = get_tq()
        except Exception as exc:  # noqa: BLE001
            return "通达信 TQ 未初始化：%s" % exc
        if not hasattr(tq, "send_warn"):
            return "当前通达信 TQ 不支持 send_warn，请升级客户端/插件"
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(["000001.SH"], ["1d"], count=2)
            close = (raw.get("1d") or {}).get("Close")
            if close is None or getattr(close, "empty", True):
                return "通达信行情不可用：未取到 000001.SH 日线"
        except Exception as exc:  # noqa: BLE001
            return "通达信行情不可用：%s" % exc
        return None

    def send_warn(self, items: List[WarnItem]) -> dict:
        if not items:
            return {"ok": True, "ErrorId": "0", "skipped": True, "batches": 0}
        tq = get_tq()
        if not hasattr(tq, "send_warn"):
            raise TQAlertError("当前通达信 TQ 不支持 send_warn")
        run_id = ""
        for chunk in _chunks(items, _BATCH):
            payload = {
                "stock_list": [i.stock_code for i in chunk],
                "time_list": ["".join(c for c in i.time if c.isdigit())[:14]
                              for i in chunk],
                "price_list": [_num(i.price) for i in chunk],
                "close_list": [_num(i.close) for i in chunk],
                "volum_list": [_num(i.volume) for i in chunk],
                "bs_flag_list": [i.bs_flag if i.bs_flag in ("0", "1", "2")
                                 else "2" for i in chunk],
                # 原实现硬编码 "0"，WarnItem.warn_type 被静默丢弃（调用方显式
                # 指定的预警类型永远不生效）。非数字值退化为默认 "0"。
                "warn_type_list": [str(i.warn_type) if str(i.warn_type).isdigit()
                                   else "0" for i in chunk],
                "reason_list": [(i.reason or "")[:25] for i in chunk],
                "count": len(chunk),
            }
            with get_tdx_lock():
                raw = tq.send_warn(**payload)
            raw = raw or {}
            err_id = str(raw.get("ErrorId", "0"))
            if err_id not in ("0", ""):
                return {
                    "ok": False, "ErrorId": err_id,
                    "Error": str(raw.get("Error") or ""),
                    "run_id": str(raw.get("run_id") or ""),
                    "batches": len(_chunks(items, _BATCH)),
                }
            run_id = str(raw.get("run_id") or run_id)
        return {"ok": True, "ErrorId": "0", "Error": "",
                "run_id": run_id, "batches": len(_chunks(items, _BATCH))}

    def send_message(self, msg: str) -> dict:
        try:
            tq = get_tq()
            if not hasattr(tq, "send_message"):
                return {"ok": False, "Error": "send_message unavailable"}
            with get_tdx_lock():
                raw = tq.send_message(msg)
            raw = raw or {}
            return {"ok": str(raw.get("ErrorId", "0")) in ("0", ""),
                    "Error": str(raw.get("Error") or ""), "raw": raw}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "Error": str(exc)}
