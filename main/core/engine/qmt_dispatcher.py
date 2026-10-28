"""QMT（迅投 miniQMT）交易通道（QmtDispatcher）。

与 iQuant 桥 / 通达信 TQ 的差异：

- 依赖 ``xtquant``（随 QMT 客户端安装，通常需手工把 ``xtquant`` 目录加入
  sys.path 或 pip 安装）。未安装时 ``available=False``，启动会话会给出明确告警，
  不会静默失败。
- ``order_stock`` 支持写入备注（``order_remark``），委托表可回读 remark，因此
  ``supports_remark = True``，Core 走「按 remark 精确认领 order_ref」主路径。
- 行情/交易日历优先用 ``xtdata``（QMT 自带），不可用时回退 Core 内嵌 TQ 模块，
  与回测取数口径保持一致。

xtquant 常量：STOCK_BUY=23 / STOCK_SELL=24；FIX_PRICE=11 限价 / MARKET_PEER_PRICE_FIRST=42
等，本通道只使用限价与最新价（市价）两种。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from .http_bridge_dispatcher import BridgeOrderRejected, BridgeUnavailableError
from .live.timing import now_shanghai

logger = logging.getLogger(__name__)

# iQuant m_nOrderStatus 口径（与 Core 状态机一致，见 order_machine 注释）
_ORDER_STATUS_MAP = {
    48: 50,   # 未报 → 已报
    49: 50,   # 待报 → 已报
    50: 50,   # 已报
    51: 51,   # 已报待撤
    52: 52,   # 部成待撤
    53: 53,   # 部撤
    54: 54,   # 已撤
    55: 55,   # 部成
    56: 56,   # 已成
    57: 57,   # 废单
}


def _exchange_for(code6: str) -> str:
    if code6.startswith(("6", "5", "9")):
        return "SH"
    if code6.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def normalize_symbol(code: str) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        return text
    code6 = re.sub(r"\D", "", text)[:6]
    if len(code6) != 6:
        return text
    return "%s.%s" % (code6, _exchange_for(code6))


def xtquant_available() -> bool:
    """xtquant 是否可导入（打包分发包默认不含，需用户自行配置 QMT 环境）。"""
    try:
        import xtquant  # noqa: F401
        from xtquant import xtconstant  # noqa: F401
        from xtquant.xttrader import XtQuantTrader  # noqa: F401
        from xtquant.xttype import StockAccount  # noqa: F401
        return True
    except Exception:
        return False


class QmtDispatcher(OrderDispatcher):
    """miniQMT 交易通道（xtquant.xttrader）。"""

    channel = "qmt"
    #: QMT 委托支持 remark 回写，Core 按 remark 精确认领，无需模糊匹配。
    supports_remark = True

    def __init__(self, path: str = "", account: str = "", account_type: str = "STOCK",
                 session_id: int = 0, price_mode: str = "fix",
                 xtquant_path: str = "", **_kwargs):
        self._path = str(path or "")
        self._account = str(account or "")
        self._account_type = str(account_type or "STOCK").upper()
        self._session_id = int(session_id or 0)
        self._price_mode = str(price_mode or "fix")
        self._xtquant_path = str(xtquant_path or "")
        self._trader = None
        self._acc = None
        self._connected = False
        self._last_error = ""
        self._lock = threading.Lock()

    @property
    def last_error(self) -> str:
        return self._last_error

    @staticmethod
    def order_id(order: OrderEvent) -> str:
        """确定性订单 ID（同 OrderEvent 重试生成相同值），前 20 位作 remark 写入柜台。"""
        key = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            order.strategy_id, order.portfolio_id, order.stock_code,
            order.trade_type.value, order.signal_type.value, order.signal_name,
            order.quantity, order.price,
        )
        if order.bar_time is not None:
            key += "|%s" % order.bar_time.isoformat()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    def connect(self) -> bool:
        self._last_error = ""
        if self._xtquant_path:
            import sys
            if self._xtquant_path not in sys.path:
                sys.path.insert(0, self._xtquant_path)
        if not xtquant_available():
            self._last_error = (
                "未找到 xtquant（QMT 交易依赖）：请在系统配置填写 QMT 安装目录，"
                "或把 xtquant 加入 Python 路径")
            self._connected = False
            return False
        if not self._path:
            self._last_error = "未配置 QMT userdata 路径（config.qmt.path）"
            self._connected = False
            return False
        if not self._account:
            self._last_error = "未配置 QMT 资金账号（config.qmt.account）"
            self._connected = False
            return False
        try:
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount

            sid = self._session_id or int(time.time()) % 100000 + 1000
            trader = XtQuantTrader(self._path, sid)
            trader.start()
            if trader.connect() != 0:
                self._last_error = "QMT 交易连接失败：请确认 QMT 客户端已登录且开启极简模式"
                self._connected = False
                return False
            acc = StockAccount(self._account, self._account_type)
            trader.subscribe(acc)
            self._trader = trader
            self._acc = acc
            self._connected = True
            return True
        except Exception as e:
            self._trader = None
            self._acc = None
            self._connected = False
            self._last_error = "QMT 连接异常：%s" % e
            return False

    def heartbeat(self) -> bool:
        if not self._connected or self._trader is None:
            return self.connect()
        try:
            self._trader.query_stock_asset(self._acc)
            return True
        except Exception:
            self._connected = False
            return False

    def close(self) -> None:
        trader = self._trader
        self._trader = None
        self._acc = None
        self._connected = False
        if trader is not None:
            try:
                trader.stop()
            except Exception:
                pass

    def _ensure(self) -> None:
        if not self._connected or self._trader is None:
            if not self.connect():
                raise BridgeUnavailableError(self._last_error or "QMT 未连接")
        if self._acc is None:
            raise BridgeUnavailableError("QMT 账户未初始化")

    def _call(self, name: str, default):
        self._ensure()
        with self._lock:
            try:
                out = getattr(self._trader, name)(self._acc)
            except Exception as e:
                self._last_error = "QMT %s 查询失败：%s" % (name, e)
                logger.warning("qmt %s failed: %s", name, e)
                return default
        return default if out is None else out

    # ---- 资产 / 持仓 / 委托 ----

    def get_asset(self) -> dict:
        a = self._call("query_stock_asset", None)
        if a is None:
            return {}
        return {
            "total_asset": float(getattr(a, "total_asset", 0) or 0),
            "available": float(getattr(a, "cash", 0) or 0),
            "cash": float(getattr(a, "cash", 0) or 0),
            "market_value": float(getattr(a, "market_value", 0) or 0),
            "frozen": float(getattr(a, "frozen_cash", 0) or 0),
        }

    def get_positions(self) -> list:
        rows = self._call("query_stock_positions", [])
        out = []
        for p in rows:
            vol = int(getattr(p, "volume", 0) or 0)
            out.append({
                "symbol": str(getattr(p, "stock_code", "") or ""),
                "volume": vol,
                # can_use_volume 即 T+1 可用股数；0 是合法锁定值，不回退持仓量
                "available": int(getattr(p, "can_use_volume", vol) or 0),
                "cost": float(getattr(p, "open_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
            })
        return out

    def get_orders(self) -> list:
        rows = self._call("query_stock_orders", [])
        out = []
        for o in rows:
            out.append({
                "order_id": str(getattr(o, "order_id", "") or ""),
                "symbol": str(getattr(o, "stock_code", "") or ""),
                "direction": "buy" if int(getattr(o, "order_type", 0) or 0) == 23 else "sell",
                "price": float(getattr(o, "price", 0) or 0),
                "volume": int(getattr(o, "order_volume", 0) or 0),
                "filled_volume": int(getattr(o, "traded_volume", 0) or 0),
                "status": _ORDER_STATUS_MAP.get(int(getattr(o, "order_status", 50) or 50), 50),
                "remark": str(getattr(o, "order_remark", "") or ""),
                "order_time": int(getattr(o, "order_time", 0) or 0),
            })
        return out

    # ---- OrderDispatcher 契约 ----

    def query_positions(self) -> list:
        out = []
        for r in self.get_positions():
            code6 = re.sub(r"\D", "", str(r.get("symbol") or ""))[:6]
            if len(code6) != 6:
                continue
            out.append({
                "instrument": code6,
                "exchange": _exchange_for(code6),
                "volume": int(r.get("volume") or 0),
                "available": int(r.get("available") or 0),
                "cost": float(r.get("cost") or 0),
                "price": 0.0,
                "name": "",
            })
        return out

    def query_account(self) -> list:
        return [self.get_asset()]

    def query_orders(self, order_id: Optional[str] = None) -> list:
        out = []
        for o in self.get_orders():
            ref = str(o.get("order_id") or "").strip()
            code = re.sub(r"\D", "", str(o.get("symbol") or ""))[:6]
            if not ref or len(code) != 6:
                continue
            insert_date, insert_time = self._split_order_time(o.get("order_time"))
            out.append({
                "source": "BRIDGE",
                "remark": o.get("remark") or "",
                "order_ref": ref,
                "instrument": code,
                "exchange": _exchange_for(code),
                "direction": 49 if o.get("direction") == "sell" else 48,
                "volume": int(o.get("volume") or 0),
                "traded_volume": int(o.get("filled_volume") or 0),
                "filled_volume": int(o.get("filled_volume") or 0),
                "price": float(o.get("price") or 0),
                "insert_date": insert_date,
                "insert_time": insert_time,
                "status": int(o.get("status") or 50),
            })
        return out

    @staticmethod
    def _split_order_time(raw) -> tuple:
        """QMT order_time 是秒级时间戳 → (YYYYMMDD, HHMMSS)；无效返回 ('','')。"""
        try:
            ts = int(raw or 0)
        except (TypeError, ValueError):
            return ("", "")
        if ts <= 0:
            return ("", "")
        dt = datetime.fromtimestamp(ts)
        return (dt.strftime("%Y%m%d"), dt.strftime("%H%M%S"))

    def query_deals(self, order_id: Optional[str] = None) -> list:
        """QMT 成交回报 → Core /deals 形状（按 order_ref 聚合）。"""
        rows = self._call("query_stock_trades", [])
        out = []
        for t in rows:
            code = re.sub(r"\D", "", str(getattr(t, "stock_code", "") or ""))[:6]
            ref = str(getattr(t, "order_id", "") or "")
            if len(code) != 6:
                continue
            volume = int(getattr(t, "traded_volume", 0) or 0)
            price = float(getattr(t, "traded_price", 0) or 0)
            if volume <= 0 or price <= 0:
                continue
            insert_date, insert_time = self._split_order_time(getattr(t, "traded_time", 0))
            out.append({
                "order_ref": ref,
                "remark": "",
                "instrument": code,
                "exchange": _exchange_for(code),
                "direction": 49 if int(getattr(t, "order_type", 0) or 0) == 24 else 48,
                "volume": volume,
                "price": price,
                "amount": float(getattr(t, "traded_amount", price * volume) or price * volume),
                "commission": float(getattr(t, "commission", 0) or 0),
                "stamp_duty": 0,
                "trade_date": insert_date,
                "trade_time": insert_time,
            })
        return out

    def query_calendar(self) -> list:
        try:
            from xtquant import xtdata
            dates = xtdata.get_trading_dates("SH") or []
            return [datetime.fromtimestamp(int(d) / 1000).strftime("%Y%m%d")
                    for d in dates if int(d or 0) > 0]
        except Exception:
            pass
        from .tdx_tq_dispatcher import TdxTqDispatcher
        return TdxTqDispatcher().query_calendar()

    def query_quote(self, code: str, period: str = "1m", count: int = 10) -> list:
        tq_code = normalize_symbol(code)
        try:
            from xtquant import xtdata
            data = xtdata.get_market_data_ex(
                [], [tq_code], period=period, count=max(int(count or 10), 2))
            frame = (data or {}).get(tq_code)
            if frame is not None and not getattr(frame, "empty", True):
                bars = []
                for ts in frame.index:
                    try:
                        millis = int(ts)
                        stime = datetime.fromtimestamp(millis / 1000).strftime("%Y%m%d%H%M%S")
                    except Exception:
                        continue
                    row = frame.loc[ts]
                    bars.append({
                        "stime": stime,
                        "open": float(row.get("open", 0) or 0),
                        "high": float(row.get("high", 0) or 0),
                        "low": float(row.get("low", 0) or 0),
                        "close": float(row.get("close", 0) or 0),
                        "volume": float(row.get("volume", 0) or 0),
                        "amount": float(row.get("amount", 0) or 0),
                    })
                if bars:
                    return bars
        except Exception as e:
            logger.warning("qmt quote via xtdata failed, fallback to TQ: %s", e)
        from .tdx_tq_dispatcher import TdxTqDispatcher
        return TdxTqDispatcher().query_quote(code, period, count)

    def check_price_limit(self, stock_code: str, direction: str, price) -> Optional[str]:
        """涨跌停前置校验委托 TdxTqDispatcher（与 query_calendar/query_quote 同模式）。

        引擎按 price_limit_check_enabled 配置门控调用；返回拒绝原因文案，None=通过。
        """
        from .tdx_tq_dispatcher import TdxTqDispatcher
        return TdxTqDispatcher().check_price_limit(stock_code, direction, price)

    # ---- 下单 ----

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        self._ensure()
        symbol = normalize_symbol(order.stock_code)
        vol = int(order.quantity)
        price = float(order.price) if order.price is not None else 0.0
        if len(re.sub(r"\D", "", symbol)[:6]) != 6:
            raise BridgeOrderRejected("invalid stock code %s" % order.stock_code)
        if vol <= 0 or price <= 0:
            raise BridgeOrderRejected("invalid order volume/price %s/%s" % (vol, price))

        from xtquant import xtconstant
        direction = "buy" if order.trade_type.value == "BUY" else "sell"
        order_type = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL
        price_type = (xtconstant.LATEST_PRICE if self._price_mode in ("latest", "market", "sj")
                      else xtconstant.FIX_PRICE)
        remark = self.order_id(order)[:20]
        try:
            with self._lock:
                oid = self._trader.order_stock(
                    self._acc, symbol, order_type, vol, price_type, price,
                    "创懿量化", remark,
                )
        except Exception as e:
            raise BridgeOrderRejected("qmt place order error: %s" % e)
        if oid is None or int(oid) < 0:
            raise BridgeOrderRejected("QMT 下单被拒（seq=%s）" % (oid,))

        fill_price = order.price if order.price is not None else Decimal(str(price))
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=fill_price,
            quantity=vol,
            amount=fill_price * vol,
            commission=Decimal("0"),
            stamp_duty=Decimal("0"),
            trade_time=order.bar_time or datetime.now(),
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )
