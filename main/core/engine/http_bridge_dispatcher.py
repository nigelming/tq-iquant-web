"""HttpBridgeDispatcher — 通过 HTTP 调 iQuant 客户端内桥（0009 切片2）。

桥（live/bridge/iquant_bridge.py）在 iQuant 客户端内暴露 127.0.0.1:8790 HTTP 服务，
本 dispatcher 实现 OrderDispatcher 接口，把下单/查询转成对桥的 HTTP 调用。

真实下单语义：
  - place_order 桥「受理成功」（passorder 返回 0）即构造 TradeEvent 返回。
    成交价格首期用请求价（order.price = bar.close 近似）；prType=14 对手价实际
    成交价是盘口一档价（≠ close），真实成交回报轮询在切片5 /deals 回填。
  - 桥业务拒绝（白名单/限额/重复）→ 返回 None（不成交）。
  - 桥网络不可用（iQuant 客户端离线）→ 抛 BridgeUnavailableError，上层暂停交易。

幂等：同一 OrderEvent 生成确定性 order_id（策略/组合/股票/方向/信号/时间 的 MD5），
桥侧按 order_id 去重，重复请求不重复下单。
"""
import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from tq_iquant_shared.constants import TradeType


class BridgeUnavailableError(RuntimeError):
    """桥不可用（iQuant 客户端离线 / 未启动 / 网络异常）。"""


class HttpBridgeDispatcher(OrderDispatcher):
    def __init__(self, base_url: str = "http://127.0.0.1:8790",
                 token: Optional[str] = None, timeout: float = 10.0,
                 client: Optional[httpx.Client] = None):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)

    # ---------------- 基础 ----------------
    def _headers(self) -> dict:
        return {"X-Auth-Token": self._token} if self._token else {}

    def heartbeat(self) -> bool:
        try:
            r = self._client.get(self._base_url + "/ping", headers=self._headers())
            return r.status_code == 200
        except Exception:
            return False

    # ---------------- 下单 ----------------
    @staticmethod
    def _order_id(order: OrderEvent) -> str:
        """确定性订单 ID：同订单（含数量/价格）重试生成相同 ID，桥侧幂等去重。

        数量/价格必须参与：同策略/股票/信号但数量变化的重新触发是不同订单。
        """
        key = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            order.strategy_id, order.portfolio_id, order.stock_code,
            order.trade_type.value, order.signal_type.value, order.signal_name,
            order.quantity, order.price,
        )
        if order.bar_time is not None:
            key += "|%s" % order.bar_time.isoformat()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        payload = {
            "order_id": self._order_id(order),
            "code": order.stock_code,
            "op": "buy" if order.trade_type.value == "BUY" else "sell",
            "volume": order.quantity,
            "price": float(order.price) if order.price is not None else 0,
            # prType=14 对手价:BUY 取卖1价、SELL 取买1价报限价单 -> 立即成交。
            # 桥 _do_place 据此调 passorder(op, 1101, acct, code, 14, 0, vol, ...)。
            # price 对 prType!=11 无效,传 close 仅作记录/切片5 回填参考。
            "pr_type": 14,
        }
        try:
            r = self._client.post(self._base_url + "/order", json=payload, headers=self._headers())
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            data = r.json()
        except Exception:
            data = {}
        if not data.get("ok"):
            # 桥业务拒绝（白名单/限额/重复）→ 不成交，不抛异常
            return None

        # 首期简化：桥受理成功即视为成交，成交价用请求价（真实回报轮询后续切片）
        fill_price = order.price if order.price is not None else Decimal("0")
        amount = fill_price * order.quantity
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=fill_price,
            quantity=order.quantity,
            amount=amount,
            commission=Decimal("0"),
            stamp_duty=Decimal("0"),
            trade_time=order.bar_time or datetime.now(),
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )

    # ---------------- 查询 ----------------
    def _get_json(self, path: str) -> list:
        try:
            r = self._client.get(self._base_url + path, headers=self._headers())
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            return (r.json() or {}).get("data", [])
        except Exception:
            return []

    def query_positions(self) -> list:
        return self._get_json("/positions")

    def query_account(self) -> list:
        return self._get_json("/account")

    def query_orders(self, order_id: Optional[str] = None) -> list:
        path = "/orders"
        if order_id:
            path += "?order_id=%s" % order_id
        return self._get_json(path)

    def query_deals(self, order_id: Optional[str] = None) -> list:
        path = "/deals"
        if order_id:
            path += "?order_id=%s" % order_id
        return self._get_json(path)

    def query_quote(self, code: str, period: str = "1m", count: int = 10) -> list:
        """拉 1m/5m/1d bar（0009 切片3 行情通道）。

        桥 GET /quote?code=&period=&count= 返回
        {"ok": True, "data": {code: [bar, ...]}}，每 bar 含 stime/open/high/low/close/volume。
        返回该 code 的 bar dict 列表（空数据 → 空列表）。
        """
        path = "/quote?code=%s&period=%s&count=%s" % (code, period, count)
        try:
            r = self._client.get(self._base_url + path, headers=self._headers())
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            body = r.json() or {}
        except Exception:
            return []
        if not body.get("ok"):
            return []
        data = body.get("data") or {}
        return data.get(code, []) if isinstance(data, dict) else []
