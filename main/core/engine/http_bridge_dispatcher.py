"""HttpBridgeDispatcher — 通过 HTTP 调 iQuant 客户端内桥（0009 切片2）。

桥（live/bridge/iquant_bridge.py）在 iQuant 客户端内暴露 127.0.0.1:8790 HTTP 服务，
本 dispatcher 实现 OrderDispatcher 接口，把下单/查询转成对桥的 HTTP 调用。

真实下单语义：
  - place_order 桥「受理成功」（passorder 返回 0）即构造 TradeEvent 返回。
    成交价格首期用请求价（order.price = bar.close 近似）；prType=14 对手价实际
    成交价是盘口一档价（≠ close），真实成交回报轮询在切片5 /deals 回填。
  - 桥业务拒绝（白名单/限额/重复）→ 抛 BridgeOrderRejected（带桥侧 error 文案），
    上层标 rejected 并回显原因；桥返回 {ok:false} 但无 error（非 JSON 故障路径）
    → 返回 None（#24 语义，不静默吞）。
  - 桥网络不可用（iQuant 客户端离线）→ 抛 BridgeUnavailableError，上层暂停交易。

幂等：同一 OrderEvent 生成确定性 order_id（策略/组合/股票/方向/信号/时间 的 MD5），
桥侧按 order_id 去重，重复请求不重复下单。
"""
import hashlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)


class BridgeUnavailableError(RuntimeError):
    """桥不可用（iQuant 客户端离线 / 未启动 / 网络异常）。"""


class BridgeOrderRejected(RuntimeError):
    """桥业务拒绝下单（白名单/限额/重复），message 带桥侧真实原因。

    与 BridgeUnavailableError 的区别：桥在线、正常受理请求，但业务上拒单
    （如 volume 超限、股票不在白名单、重复单）。上层据此标 rejected 并回显原因，
    而不是笼统的 "approval failed"。
    """


class HttpBridgeDispatcher(OrderDispatcher):
    def __init__(self, base_url: str = "http://127.0.0.1:8790",
                 timeout: float = 10.0,
                 client: Optional[httpx.Client] = None):
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)

    # ---------------- 基础 ----------------
    def heartbeat(self) -> bool:
        try:
            r = self._client.get(self._base_url + "/ping")
            return r.status_code == 200
        except Exception:
            return False

    # ---------------- 下单 ----------------
    @staticmethod
    def order_id(order: OrderEvent) -> str:
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
        oid = self.order_id(order)
        payload = {
            "order_id": oid,
            "code": order.stock_code,
            "op": "buy" if order.trade_type.value == "BUY" else "sell",
            "volume": order.quantity,
            "price": float(order.price) if order.price is not None else 0,
            # prType=14 对手价:BUY 取卖1价、SELL 取买1价报限价单 -> 立即成交。
            # 桥 _do_place 据此调 passorder(op, 1101, acct, code, 14, 0, vol, ...)。
            # price 对 prType!=11 无效,传 close 仅作记录/切片5 回填参考。
            "pr_type": 14,
            # remark = oid 前 20 位:桥作为 passorder 的 userOrderId 写入委托/成交的
            # m_strRemark，回填时 Core 按 remark 精确认领本单（见 LiveEngine._try_match_order_ref），
            # 避免 代码+方向+数量 模糊匹配撞到跨会话遗留的同代码同向同量旧单。
            "remark": oid[:20],
        }
        try:
            r = self._client.post(self._base_url + "/order", json=payload)
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            data = r.json()
        except Exception as e:
            # #24：桥返回非 JSON（HTML 错误页/坏网关）不静默吞——原行为 data={} →
            # 当业务拒绝，真实桥故障无痕迹。告警让"非 JSON"可见。
            logger.warning(
                "bridge /order returned non-JSON body, treated as rejection: %s", e
            )
            data = {}
        if not data.get("ok"):
            # 桥业务拒绝（白名单/限额/重复）→ 抛 BridgeOrderRejected，上层回显真实原因。
            # data={}（非 JSON 路径）无 error → 维持返回 None（#24 语义：桥故障非拒单）。
            error = data.get("error")
            if error:
                raise BridgeOrderRejected(str(error))
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
            r = self._client.get(self._base_url + path)
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            return (r.json() or {}).get("data", [])
        except Exception as e:
            # #24：非 JSON 不静默吞——返回 [] 会被上层当无持仓/无订单，掩盖桥故障。
            logger.warning(
                "bridge %s returned non-JSON body, returning empty list: %s", path, e
            )
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

    def query_calendar(self) -> list:
        """拉权威交易日历（桥 xtdata.get_trading_dates）。

        返回 'YYYYMMDD' 字符串列表（桥侧把毫秒时间戳转成字符串，兼容 3.6）。
        桥离线或老桥无 /calendar（HTTP 404）时抛 BridgeUnavailableError，
        由 TradingCalendar fail-open 处理（工作日默认交易日）。
        """
        return self._get_json("/calendar")

    def query_quote(self, code: str, period: str = "1m", count: int = 10) -> list:
        """拉 1m/5m/1d bar（0009 切片3 行情通道）。

        桥 GET /quote?code=&period=&count= 返回
        {"ok": True, "data": {code: [bar, ...]}}，每 bar 含 stime/open/high/low/close/volume。
        返回该 code 的 bar dict 列表（空数据 → 空列表）。
        """
        path = "/quote?code=%s&period=%s&count=%s" % (code, period, count)
        try:
            r = self._client.get(self._base_url + path)
        except Exception as e:
            raise BridgeUnavailableError("bridge request failed: %s" % e) from e
        if r.status_code != 200:
            raise BridgeUnavailableError("bridge returned HTTP %s" % r.status_code)
        try:
            body = r.json() or {}
        except Exception as e:
            # #24：非 JSON 不静默吞——返回 [] 会让 BarPoller 拉不到 bar 静默不触发。
            logger.warning(
                "bridge /quote returned non-JSON body for %s, returning empty list: %s",
                code, e,
            )
            return []
        if not body.get("ok"):
            return []
        data = body.get("data") or {}
        return data.get(code, []) if isinstance(data, dict) else []
