"""alert 模式专用行情 dispatcher：TQ 行情 + 禁止下单。"""
import logging

from ..tdx_tq_dispatcher import TdxTqDispatcher

logger = logging.getLogger(__name__)


class AlertDataDispatcher:
    channel = "alert"
    supports_remark = False

    def __init__(self, **kwargs):
        self.last_error = ""
        self._quote = TdxTqDispatcher(account="", **kwargs)

    def query_quote(self, code, period="1m", count=10):
        return self._quote.query_quote(code, period=period, count=count)

    def query_calendar(self):
        return self._quote.query_calendar()

    def heartbeat(self) -> bool:
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(["000001.SH"], ["1d"], count=2)
            close = (raw.get("1d") or {}).get("Close")
            if close is None or getattr(close, "empty", True):
                self.last_error = "TQ 行情不可用：未取到 000001.SH 日线"
                return False
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = "TQ 行情不可用：%s" % exc
            return False

    def place_order(self, *args, **kwargs):
        raise RuntimeError("alert 模式禁止下单")

    def get_positions(self):
        return []

    def get_asset(self):
        return {}

    def query_orders(self):
        return []

    def query_deals(self):
        return []

    def close(self):
        self._quote.close()
