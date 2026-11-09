"""HttpBridgeDispatcher — 通过 HTTP 调 QMT 客户端内桥（0009 切片2）。

桥（live/bridge/iquant_bridge.py）在 QMT 客户端内暴露 127.0.0.1:8790 HTTP 服务，
本 dispatcher 实现 OrderDispatcher 接口，把下单/查询转成对桥的 HTTP 调用。

真实下单语义：
  - place_order 桥「受理成功」（passorder 返回 0）即构造 TradeEvent 返回。
    成交价格首期用请求价（order.price = bar.close 近似）；prType=14 对手价实际
    成交价是盘口一档价（≠ close），真实成交回报轮询在切片5 /deals 回填。
  - /order 返回 ok:false 时按「错误性质」分三类处理（见 `_classify_bridge_error`）：
      * **明确拒单**（柜台 rejected=true 带 reason，或业务校验文案如涨跌停/可用不足/
        数量非法）→ 抛 BridgeOrderRejected（带桥侧原文），上层标 rejected 并回显原因；
      * **瞬时故障**（桥内 gm 交易服务断连窗口的 "order_volume raised: ..." /
        "returned empty list" / "query_*" / 连接层文案如 WinError 10061、timed out）
        → 返回 None（在途）+ 记 last_unconfirmed_order_id，**绝不判拒单**：
        这类错误不代表柜台收到了委托，判 rejected 会让真实在途单脱跟踪；
      * **未知错误** → 保守判「明确拒单」（宁可人工确认，也不把真拒单挂成在途）。
    桥返回 {ok:false} 但无 error（非 JSON 故障路径）→ 返回 None（#24 语义，不静默吞）。
  - 桥网络不可用（QMT 客户端离线）→ 抛 BridgeUnavailableError，上层暂停交易。
  - **下单超时/连接中断（请求已发出但未拿到应答）→ 返回 None（在途）+ 记
    last_unconfirmed_order_id**：慢桥（全局锁排队）下不得把真实在途单判成拒单，
    引擎保持 submitted 由后续 /orders 回查认领（对齐 TdxTqDispatcher 先例）。
    连接被拒（请求从未发出）仍抛 BridgeUnavailableError。
  - 单次调用耗时超 slow_threshold → logger.warning + on_slow 回调（引擎转 risk 事件），
    按「会话（本 dispatcher 实例）」维度做窗口节流：每窗口最多一条，窗口内的后续
    慢请求累计计数，在窗口过期后的首条里汇总（避免同一分钟对多个端点各报一条）。

幂等：同一 OrderEvent 生成确定性 order_id（策略/组合/股票/方向/信号/时间 的 MD5），
桥侧按 order_id 去重，重复请求不重复下单。
"""
import hashlib
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, Optional, Tuple

import httpx

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)


class BridgeUnavailableError(RuntimeError):
    """桥不可用（QMT 客户端离线 / 未启动 / 网络异常）。"""


class BridgeOrderRejected(RuntimeError):
    """桥业务拒绝下单（白名单/限额/重复），message 带桥侧真实原因。

    与 BridgeUnavailableError 的区别：桥在线、正常受理请求，但业务上拒单
    （如 volume 超限、股票不在白名单、重复单）。上层据此标 rejected 并回显原因，
    而不是笼统的 "approval failed"。
    """


class HttpBridgeDispatcher(OrderDispatcher):
    channel = "iquant_bridge"
    #: 桥把确定性 bridge_order_id（前 20 位）写进柜台 remark，支持 remark 精确认领
    #: （与 TDX/THS 的 supports_remark=False 相对）。订单超窗无法追溯时按「保持
    #: pending + risk 事件」处理，不自动驳回（柜台当日委托查询不含废单）。
    supports_remark = True

    #: 下单请求「已发出但结果未知」的异常（超时/连接中断/协议中断）。
    #: 与连接被拒（ConnectError/ConnectTimeout，请求从未发出）区分：
    #: 前者按在途处理（返回 None），后者仍是桥不可用。
    _INFLIGHT_ERRORS: Tuple[type, ...] = (
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.WriteError,
    )

    #: /order 应答里可识别的委托标识字段（兼容桥契约变更前后的多种命名；
    #: 一律可选读取，缺失即降级为在途，绝不臆造成交）。
    _ORDER_REF_FIELDS: Tuple[str, ...] = (
        "cl_ord_id", "order_ref", "order_id", "entrust_id", "ref",
    )
    #: 委托回查行里可用于与本单匹配的标识字段（remark=桥回写的 oid 前 20 位）
    _ROW_REF_FIELDS: Tuple[str, ...] = (
        "cl_ord_id", "order_ref", "order_id", "entrust_id", "ref", "remark",
    )
    #: 明确「全部成交」的状态文案（数字状态码不猜——未知即按在途，由 /deals 回填）
    _FILLED_STATUS_VALUES = frozenset(
        ("filled", "filled_all", "all_filled", "已成", "全部成交", "已成交")
    )
    _STATUS_FIELDS: Tuple[str, ...] = ("status", "order_status", "state")
    _BROKER_ORDER_ID_FIELDS: Tuple[str, ...] = ("cl_ord_id", "order_ref")
    _FILLED_VOLUME_FIELDS: Tuple[str, ...] = (
        "filled_volume", "volume_traded", "filled_qty", "deal_volume",
    )

    #: 业务校验类拒单文案（小写匹配）：柜台明确不受理这张单，判 rejected 才是真语义。
    #: 优先级高于瞬时故障模式——"order_volume raised: 委托数量非法" 这类 exception
    #: 文案的根因是业务参数，不是连接层。只收录确定性的校验措辞，绝不加宽泛词。
    _REJECT_ERROR_HINTS: Tuple[str, ...] = (
        "涨跌停", "超过涨跌幅", "可用不足", "资金不足", "可用资金",
        "可用数量", "可用股份", "数量非法", "委托数量", "价格非法",
        "非交易时间", "不在交易时间", "停牌", "重复委托",
    )
    #: 连接层/桥内调用瞬时故障文案（小写包含匹配）：gm 交易服务断连、桥内查询失败、
    #: 网络超时等。gm 侧"断连窗口"实测只产出 "order_volume raised:" /
    #: "returned empty list" 两种文案，其余为底层 socket 错误。
    _TRANSIENT_ERROR_HINTS: Tuple[str, ...] = (
        "order_volume raised:",
        "returned empty list",
        "winerror",
        "timed out",
        "timeout",
        "connection refused",
        "connection reset",
        "10061",
        "10054",
        "远程主机强迫关闭",
    )
    #: 桥内查询函数前缀（"query_positions: ..." 等）：查询失败属桥侧瞬时不可用
    _TRANSIENT_ERROR_PREFIXES: Tuple[str, ...] = ("query_",)
    #: 「瞬时故障转在途」告警的同类节流窗口（秒）
    _transient_warn_interval: float = 60.0

    def __init__(self, base_url: str = "http://127.0.0.1:8790",
                 timeout: float = 10.0,
                 slow_threshold: float = 15.0,
                 slow_warn_interval: float = 300.0,
                 on_slow: Optional[Callable[..., None]] = None,
                 now_fn: Optional[Callable[[], float]] = None,
                 client: Optional[httpx.Client] = None,
                 channel: Optional[str] = None):
        # channel 可按实例覆盖（掘金桥实例上报 goldminer_bridge，供引擎识别/
        # 错误文案分流；缺省保持 iquant_bridge 兼容旧用法）
        if channel:
            self.channel = channel
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)
        # 最近一次 heartbeat 失败分类：refused=端口无监听 / timeout=已监听但无响应
        # / protocol=应答格式不符 / unreachable=其它网络错误。启动闸据此分级提示。
        self.last_probe_error: Optional[str] = None
        # 慢响应阈值（秒，<=0 关闭）与告警窗口（秒）：桥内查询排队时单次调用会
        # 排到数秒~十数秒，超阈值即告警；节流按会话（本实例）维度——每窗口最多
        # 一条，窗口内后续慢请求只累计计数与最慢值，窗口过期后的首条带上汇总。
        self._slow_threshold = slow_threshold
        self._slow_warn_interval = slow_warn_interval
        self._slow_window_at: Optional[float] = None
        self._slow_window_count = 0
        self._slow_window_max: Optional[Tuple[str, float]] = None
        self._now = now_fn or time.monotonic
        # 慢响应回调（引擎注入 → 转 risk/bridge_slow 事件）
        self.on_slow = on_slow
        # 最近一次慢响应 (endpoint, elapsed)；供只读排查/测试
        self.last_slow: Optional[Tuple[str, float]] = None
        # 「下单已发出未确认」线索：引擎按此保持 submitted 待 /orders 回查认领
        # （与 TdxTqDispatcher.last_unconfirmed_order_id 同一语义）。
        self.last_unconfirmed_order_id: Optional[str] = None
        self._last_error: Optional[str] = None
        # 最近一次 /order 受理应答里的柜台委托号（掘金 cl_ord_id）。引擎受理即落库
        # （live_orders.order_ref，重启不丢，作为精确认领键）——DB 是记帐真相，桥的
        # 内存台账重启即丢。与确定性 bridge_order_id（remark 认领键）分开保存，
        # 不得互相覆盖（bridge_order_id[:20] 是写进柜台的 remark）。
        self.last_broker_order_id: Optional[str] = None
        # 最近一次 /order 业务拒单原文（ok:false 的 error/reason），供引擎直接落库回显
        self.last_order_rejection: Optional[str] = None
        # 「瞬时故障转在途」告警节流水位：断连窗口内可能连续多单命中，同类告警
        # 每 _transient_warn_interval 秒只发一条（每单仍记 INFO 级别的 trail）。
        self._transient_warn_at: Optional[float] = None

    @property
    def last_unconfirmed_message(self) -> Optional[str]:
        """最近一次「在途未确认」的可读原因（引擎回写 LiveOrder.error_message）。"""
        return self._last_error

    # ---------------- 请求计时 ----------------
    def _do_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """统一请求入口：计时 → 慢响应告警（会话维度窗口节流 + 计数聚合）。"""
        start = self._now()
        try:
            return self._client.request(method, self._base_url + path, **kwargs)
        finally:
            self._note_timing(path, self._now() - start)

    def _note_timing(self, path: str, elapsed: float) -> None:
        """慢响应告警：按「会话（本 dispatcher 实例）」维度做窗口节流。

        - 窗口内首条慢请求**立即**上报（不被节流吞掉）；
        - 同一窗口内后续慢请求只累计次数与最慢值，不再发事件（不同端点也合并）；
        - 窗口过期后的首条慢请求携带上一窗口汇总
          ``{count, slowest_endpoint, slowest_elapsed_seconds, window_seconds}``。
        """
        endpoint = path.split("?", 1)[0]
        if self._slow_threshold <= 0 or elapsed < self._slow_threshold:
            return
        self.last_slow = (endpoint, elapsed)
        now = self._now()
        if (self._slow_window_at is not None
                and now - self._slow_window_at < self._slow_warn_interval):
            self._slow_window_count += 1
            if self._slow_window_max is None or elapsed > self._slow_window_max[1]:
                self._slow_window_max = (endpoint, elapsed)
            logger.debug(
                "bridge slow response (aggregated in window): endpoint=%s "
                "elapsed=%.2fs window_count=%d",
                endpoint, elapsed, self._slow_window_count,
            )
            return

        summary = None
        if self._slow_window_at is not None and self._slow_window_max is not None:
            summary = {
                "count": self._slow_window_count,
                "slowest_endpoint": self._slow_window_max[0],
                "slowest_elapsed_seconds": round(float(self._slow_window_max[1]), 2),
                "window_seconds": round(float(self._slow_warn_interval), 1),
            }
        self._slow_window_at = now
        # 计数只统计「窗口内被抑制、尚未上报」的慢请求（首条已单独上报）
        self._slow_window_count = 0
        self._slow_window_max = (endpoint, elapsed)
        logger.warning(
            "bridge slow response: endpoint=%s elapsed=%.2fs threshold=%.2fs"
            " window_count=%d%s — 桥内查询排队（全局锁串行），下单可能超时转在途",
            endpoint, elapsed, self._slow_threshold, self._slow_window_count,
            (" prev_window=%s" % (summary,)) if summary else "",
        )
        if self.on_slow is not None:
            try:
                self.on_slow(endpoint, elapsed, summary)
            except Exception:  # noqa: BLE001
                logger.warning("bridge on_slow callback failed", exc_info=True)

    # ---------------- 基础 ----------------
    def heartbeat(self) -> bool:
        """桥在线探测（GET /ping）；失败时记录 last_probe_error 供启动闸分级提示。

        refused=连接被拒（端口无监听）；timeout=端口可连但无响应（常见于桥进程
        卡在阻塞查询）；protocol=有 HTTP 应答但不是桥协议（状态码非 200 / 非 JSON
        / ok=false）；unreachable=其它网络错误。
        """
        self.last_probe_error = None
        try:
            r = self._do_request("GET", "/ping")
        except httpx.TimeoutException:
            self.last_probe_error = "timeout"
            return False
        except httpx.ConnectError:
            self.last_probe_error = "refused"
            return False
        except Exception:
            self.last_probe_error = "unreachable"
            return False
        if r.status_code != 200:
            self.last_probe_error = "protocol"
            return False
        try:
            body = r.json()
        except Exception:
            self.last_probe_error = "protocol"
            return False
        if not isinstance(body, dict) or body.get("ok") is False:
            self.last_probe_error = "protocol"
            return False
        return True

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
        self.last_unconfirmed_order_id = None
        self._last_error = None
        self.last_broker_order_id = None
        self.last_order_rejection = None
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
            r = self._do_request("POST", "/order", json=payload)
        except self._INFLIGHT_ERRORS as e:
            # 请求已发出但未拿到应答（桥全局锁排队导致的读超时/连接中断）：
            # 按「在途未确认」处理，绝不判拒单——否则真实委托脱跟踪（无 ref、
            # 无成交回填、无法撤），虚拟持仓/现金与柜台脱节。引擎据
            # last_unconfirmed_order_id 保持 submitted，后续 /orders 回查认领。
            self.last_unconfirmed_order_id = oid
            self._last_error = (
                "下单请求已发出但未获响应（%s: %s），已按在途处理，"
                "等待 /orders 回查认领" % (type(e).__name__, e)
            )
            logger.warning(
                "bridge /order no response (%s: %s) — keep order in-flight: "
                "oid=%s %s %s qty=%s",
                type(e).__name__, e, oid,
                order.trade_type.value, order.stock_code, order.quantity,
            )
            return None
        except Exception as e:
            # 连接被拒等「请求从未发出」的故障：仍是桥不可用（上层暂停交易）。
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
            # 桥业务拒绝（白名单/限额/重复/废单）→ 抛 BridgeOrderRejected，上层回显真实原因。
            # 拒单原文可能在 error / reason / rejected=true 的 message 上（掘金桥实测
            # 柜台废单走 rejected+reason）。data={}（非 JSON 路径）维持返回 None（#24 语义：
            # 桥故障非拒单）——不得把桥故障臆造成拒单。
            #
            # 但 ok:false 不等于「柜台明确拒单」：掘金桥在 gm 交易服务断连窗口内会回
            # {"ok": false, "error": "order_volume raised: ..." / "returned empty list"}，
            # 这是连接层瞬时故障。这类必须返回 None（在途）——引擎按
            # last_unconfirmed_order_id 保持 submitted，由 /orders 回查认领 + 180s 超窗
            # + grace 兜底；判 rejected 会让真实委托脱跟踪。
            error = data.get("error") or data.get("reason")
            if not error and data.get("rejected"):
                error = data.get("message") or "rejected"
            if error:
                if self._classify_bridge_error(str(error), data) == "transient":
                    return self._mark_transient_unconfirmed(oid, order, str(error))
                self.last_order_rejection = str(error)
                raise BridgeOrderRejected(str(error))
            return None

        # 受理应答里的柜台委托号（cl_ord_id）：引擎随即落库（精确认领键，桥重启不丢）
        self.last_broker_order_id = self._broker_order_id_from_response(data)

        # ok:true 不等于成交：掘金桥实测对柜台拒单（如价格超涨跌停）也回 ok:true，
        # 且拒单不回显到 /orders、/deals。只有「拿到可识别委托标识 + 回查委托表
        # 确认全部成交」才可构造成交回报，否则一律按在途待确认（返回 None）。
        ref = self._order_ref_from_response(data)
        if ref is None:
            return self._mark_unconfirmed(
                oid, order,
                "桥受理应答无可识别委托标识（%s），已按在途处理，等待委托回查认领"
                % self._short_body(data),
            )
        try:
            rows = self.query_orders(order_id=ref)
        except Exception as e:  # noqa: BLE001
            # 回查失败（含桥超时/离线）不得判拒单：保持 submitted 等后续回填
            return self._mark_unconfirmed(
                oid, order,
                "桥已受理（ref=%s）但委托回查失败（%s: %s），已按在途处理"
                % (ref, type(e).__name__, e),
            )
        row = self._find_status_row(rows, ref, oid)
        if row is None:
            return self._mark_unconfirmed(
                oid, order,
                "桥已受理（ref=%s）但委托表未回读到该单，已按在途处理"
                % ref,
            )
        if not self._row_filled(row, order):
            return self._mark_unconfirmed(
                oid, order,
                "桥已受理（ref=%s）但委托未确认全部成交（%s），已按在途处理"
                % (ref, self._short_body(row)),
            )

        # 已回读到该委托且确认全部成交：构造成交回报（成交价仍用请求价近似）
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

    @classmethod
    def _classify_bridge_error(cls, error_text: Optional[str], payload) -> str:
        """把 /order 的 ok:false 应答分两类：``rejected``（明确拒单）/ ``transient``（瞬时故障）。

        纯函数（只读入参，不碰实例状态），便于表驱动单测。判定顺序与依据：

        1. **柜台明确拒单标记**：payload（或 payload.data）里 ``rejected is True``
           并有 reason/error —— 桥已确认柜台判废单，最高优先级，压过文案模式
           （避免把 "order_volume raised: 涨跌停..." 之类误判成连接故障）。
        2. **业务校验文案**：命中 ``_REJECT_ERROR_HINTS``（涨跌停/可用不足/数量非法…）
           → rejected。这类 exception 的根因是委托参数，不是连接层。
        3. **连接层/桥内瞬时故障**：``order_volume raised:`` / ``returned empty list``
           / ``query_*`` 前缀 / WinError 10061/10054、timed out、Connection refused
           → transient。gm 交易服务断连窗口实测只产出前两种文案。
        4. **未知错误 → rejected（保守）**：桥已经明确回了业务层错误文案（不是超时、
           不是无应答），把未知情况挂成在途会占住同名订单 10 分钟（expire/grace），
           期间既无法确认也无法重下，极易被误判为「已下单成功」。宁可判拒单让
           人工/后续信号重新决策，也不静默拖延。
        """
        text = str(error_text or "").strip()
        low = text.lower()
        buckets = [payload]
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            buckets.append(payload["data"])
        for bucket in buckets:
            if isinstance(bucket, dict) and bucket.get("rejected") is True:
                if bucket.get("reason") or text:
                    return "rejected"
        if any(h in text for h in cls._REJECT_ERROR_HINTS):
            return "rejected"
        if low.startswith(cls._TRANSIENT_ERROR_PREFIXES):
            return "transient"
        if any(h in low for h in cls._TRANSIENT_ERROR_HINTS):
            return "transient"
        return "rejected"

    def _mark_transient_unconfirmed(self, oid: str, order: OrderEvent,
                                    error_text: str) -> Optional[TradeEvent]:
        """桥返回 ok:false 但属连接层瞬时故障 → 记在途线索并返回 None（不判拒单）。

        语义与「请求已发出未拿到应答」完全一致：``last_unconfirmed_order_id`` 有值
        → 引擎保持 submitted（不落 rejected、不移出在途集合），后续 /orders 回查
        认领；超过 180s 超窗仍未认领由引擎 expire 兜底。
        """
        self.last_unconfirmed_order_id = oid
        self._last_error = (
            "桥下单返回瞬时故障（%s），已按在途处理，等待 /orders 回查认领"
            % error_text
        )
        logger.info(
            "bridge_order_transient: keep order in-flight: oid=%s %s %s qty=%s "
            "error=%s",
            oid, order.trade_type.value, order.stock_code, order.quantity,
            error_text,
        )
        now = self._now()
        if (self._transient_warn_at is None
                or now - self._transient_warn_at >= self._transient_warn_interval):
            self._transient_warn_at = now
            logger.warning(
                "bridge_order_transient: 桥瞬时故障（连接层）按在途处理，不判拒单: "
                "oid=%s %s %s qty=%s error=%s（同类告警 %.0fs 内只报一次）",
                oid, order.trade_type.value, order.stock_code, order.quantity,
                error_text, self._transient_warn_interval,
            )
        return None

    def _mark_unconfirmed(self, oid: str, order: OrderEvent,
                          message: str) -> Optional[TradeEvent]:
        """记「已受理未确认」线索并返回 None（引擎据此保持 submitted 等回填）。

        绝不在无法确认委托状态时返回 amount>0 的 TradeEvent——桥对柜台拒单
        也回 ok:true，臆造成交会把虚拟持仓/现金改错。
        """
        self.last_unconfirmed_order_id = oid
        self._last_error = message
        logger.warning(
            "bridge_order_unconfirmed: keep order in-flight: oid=%s %s %s qty=%s"
            " reason=%s",
            oid, order.trade_type.value, order.stock_code, order.quantity, message,
        )
        return None

    @staticmethod
    def _short_body(body) -> str:
        """日志/线索用应答片段（桥侧把结果截断为 ~120 字符，这里保持同量级）。"""
        try:
            return str(body)[:120]
        except Exception:  # noqa: BLE001
            return "<unprintable>"

    @classmethod
    def _order_ref_from_response(cls, data: dict) -> Optional[str]:
        """从 /order 应答里取可识别委托标识；取不到返回 None（调用方降级在途）。"""
        if not isinstance(data, dict):
            return None
        buckets = [data]
        nested = data.get("data")
        if isinstance(nested, dict):
            buckets.append(nested)
        for bucket in buckets:
            for key in cls._ORDER_REF_FIELDS:
                v = bucket.get(key)
                if v not in (None, ""):
                    return str(v)
        return None

    @classmethod
    def _broker_order_id_from_response(cls, data: dict) -> Optional[str]:
        """从 /order 受理应答里取柜台委托号（cl_ord_id，兼容 order_ref 命名）。

        与 `_order_ref_from_response`（泛化的「可识别标识」，含回显 order_id）不同：
        这里只要**柜台侧**委托号，用于落库精确认领；缺失返回 None（不影响在途判定）。
        """
        if not isinstance(data, dict):
            return None
        buckets = [data]
        nested = data.get("data")
        if isinstance(nested, dict):
            buckets.append(nested)
        for bucket in buckets:
            for key in cls._BROKER_ORDER_ID_FIELDS:
                v = bucket.get(key)
                if v not in (None, ""):
                    return str(v)
        return None

    @classmethod
    def _find_status_row(cls, rows, ref: str, oid: str) -> Optional[dict]:
        """在 /orders 回查结果里按委托标识/remark 定位本单行；找不到返回 None。"""
        if not isinstance(rows, list):
            return None
        expected_remark = oid[:20]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in cls._ROW_REF_FIELDS:
                v = row.get(key)
                if v is None:
                    continue
                sv = str(v)
                if sv in (ref, oid) or (key == "remark" and sv == expected_remark):
                    return row
        return None

    @classmethod
    def _row_filled(cls, row: dict, order: OrderEvent) -> bool:
        """委托行是否确认「全部成交」：只认明确状态文案或成交量字段。

        数字状态码不猜（各桥/版本编号不一致，猜错会误判成交）——未知即按在途，
        成交与否交由 /deals 回填周期权威确认。
        """
        for key in cls._FILLED_VOLUME_FIELDS:
            v = row.get(key)
            if v is None:
                continue
            try:
                if float(v) >= float(order.quantity):
                    return True
            except (TypeError, ValueError):
                continue
        for key in cls._STATUS_FIELDS:
            v = row.get(key)
            if v is None:
                continue
            if str(v).strip().lower() in cls._FILLED_STATUS_VALUES:
                return True
        return False

    # ---------------- 查询 ----------------
    def check_price_limit(self, stock_code: str, direction: str, price) -> Optional[str]:
        """涨跌停前置校验：桥通道暂无涨跌停校验，恒放行（返回 None）。

        引擎按 price_limit_check_enabled 配置统一调用，三通道（iquant/tdx/ths）
        同签名同语义；QMT 桥无前收/涨跌停数据源，fail-open 放行。
        """
        return None

    def _get_json(self, path: str) -> list:
        try:
            r = self._do_request("GET", path)
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

    def get_asset(self) -> dict:
        """账户资金（快照采集用）：GET /account → 归一化资产字段。"""
        try:
            rows = self._get_json("/account")
            row = rows[0] if isinstance(rows, list) and rows else {}
        except Exception:
            return {}
        out = {}
        for src, dst in (("total_asset", "total_asset"), ("nav", "total_asset"),
                         ("available", "available"), ("market_value", "market_value"),
                         ("balance", "balance"), ("frozen", "frozen_cash")):
            v = row.get(src)
            if v is not None and dst not in out:
                out[dst] = float(v)
        return out

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
            r = self._do_request("GET", path)
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
