"""OrderStateMachine — LiveEngine 的委托状态机/成交回填协作者（0010 步骤 3）。

承接原 live_engine.py 里揉在一起的委托持久化、order_ref 匹配、/deals 轮询回填、
终态同步、陈旧单失效与 filled apply 一整段逻辑。这是最近反复修 bug 的区域
（修复 A+ order_ref/remark 兜底、id40-41 陈旧 submitted、53/55/56/57 状态码、
partial 不 apply、F7 在途门、I4 命门窗口），本步为**纯搬移**：逻辑一字不改，只把
``self.session_id``/``self._dispatcher``/``self.portfolios``/``self._db_session_factory``
改为经 EngineContext 读取，``self._emit`` 改为注入的回调，``self._pending_orders``/
``self._last_backfill_time`` 归本协作者持有。

协作者不反向 import LiveEngine：事件发射经注入的 ``emit`` 回调（LiveEngine 传入
``self._emit``，最终走 EventBus），持仓/账户经 ``ctx.portfolios`` 访问。LiveEngine
保留全部同名方法/属性作为薄委托，故既有测试（直连 ``engine._poll_deals()``、
``engine._pending_orders``、``engine._backfill_order(...)`` 等）无需改动即穿透到本对象。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Callable, Dict, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..position import Position
from ..format import dec_money, dec_price
from ..event import OrderEvent, TradeEvent
from ..http_bridge_dispatcher import BridgeUnavailableError
from ...config import load_config
from ...models import LiveOrder, LiveTrade
from tq_iquant_shared.constants import SignalType, TradeType

from .context import EngineContext
from .timing import _parse_insert_utc, now_shanghai

logger = logging.getLogger("core.engine.live.order_machine")

# submitted 但始终匹配不到桥 order_ref 的单的失效阈值（秒）。
# passorder 受理后桥侧若从未出现该委托（被 QMT 静默丢弃/拒单），order_ref 永远为 None，
# 超过此时长判定失效，避免无限轮询陈旧单（含跨重启遗留单）。
_ORDER_REF_MATCH_TIMEOUT = timedelta(seconds=180)
# 模糊匹配（仅用于无 bridge_order_id 的遗留单）的时间窗：候选委托的柜台插入时间
# 不得早于本单 created_at 超过此时长。挡住跨会话遗留的同代码同向同量旧单（真机 bug：
# 新单 11:21 创建时真单尚未可查，被匹配到 09:55 的遗留单 → 回填错成交、真单丢失）。
_ORDER_INSERT_TOLERANCE = timedelta(seconds=120)
# QMT ORDER 实时表 m_nOrderStatus（官方码，D:\iquant xtconstant.py / API 文档 10643）：
#   48=未报 49=待报 50=已报 51=已报待撤 52=部成待撤 53=部撤(终态) 54=已撤(终态)
#   55=部成(非终态!剩余仍在撮合) 56=已成(终态) 57=废单(终态) 255=未知
# 关键：55 是「部成」非终态，绝不能当撤单——否则剩余后续成交会重复 apply/丢单。
# 第一层自愈：Core 读这些状态把已撤/废单转出 submitted（桥 /orders 本就返回 status 字段）。
_ORDER_STATUS_FILLED = 56
_ORDER_STATUS_PARTIAL_CANCELED = 53   # 部撤：部分成交后撤剩余，终态
_ORDER_STATUS_CANCELED = 54           # 已撤：全撤，终态
_ORDER_STATUS_JUNK = 57               # 废单：柜台拒单，终态
# 终态集合：撤单类 → canceled；废单 → rejected（语义：柜台拒单非我方撤）。
_ORDER_STATUS_TERMINAL_CANCELED = (53, 54)
_ORDER_STATUS_TERMINAL_REJECTED = (57,)

# A-2：cancel_unfilled_orders 撤单成功后在 error_message 打的「已请求撤单」标记前缀。
# 不加模型列——DB 持久化，Core 重启后仍可识别（区别于进程内 set）。
_CANCEL_REQUESTED_TAG = "超时撤单请求中："

# HTTP 桥超长兜底窗口（秒）：超过该窗口仍无法在委托表/成交表追溯 → 收口为 rejected，
# 释放「同股同向在途门」（否则卡住的止损卖单会一直挡住后续风控卖出）。
# 基准恒为提交时间（created_at），不随重复检查顺延；可用
# config.yaml 的 goldminer.untraceable_grace_seconds 覆盖（缺键/非法回退本值）。
_UNTRACEABLE_GRACE_SECONDS = 600
# error_message 标记前缀：180s~grace 内保持 pending（bridge_order_untraceable:），
# 超过 grace 收口为已拒（bridge_order_untraceable_expired:）。
# 迟到回填按公共前缀 "bridge_order_untraceable" 识别这两类单（不得只认 pending 类）。
_UNTRACEABLE_MARKER = "bridge_order_untraceable"
_UNTRACEABLE_PENDING_MARKER = "bridge_order_untraceable:"
_UNTRACEABLE_EXPIRED_MARKER = "bridge_order_untraceable_expired:"
# 迟到回填纠正后的 error_message 前缀（审计用：该单曾被收口但柜台迟到成交）。
_LATE_FILL_MARKER = "late_fill_after_untraceable:"

# 柜台侧无平台委托的成交（人工/外部下单）补记：LiveOrder 标记 + 幂等键前缀。
# 幂等键放 bridge_order_id（String(32)）：「ext:<order_ref>」优先（同一外部委托的
# 多笔分笔成交聚合到一行），无 order_ref 时退化为 trade_id。
_EXTERNAL_DEAL_MARKER = "bridge_unmatched:"
_EXTERNAL_KEY_PREFIX = "ext:"


def _deal_code(deal: dict) -> str:
    """成交回报行的代码归一：symbol 优先，桥通道回退 instrument+exchange。

    与 live_engine._position_code 同一口径（桥 /deals 行只有 instrument/exchange）。
    """
    if not isinstance(deal, dict):
        return ""
    code = str(deal.get("symbol") or "").strip()
    if code:
        return code
    inst = str(deal.get("instrument") or "").strip()
    exch = str(deal.get("exchange") or "").strip()
    if inst and exch:
        return "%s.%s" % (inst, exch)
    return ""


# QMT 方向字段（2026-11-06 实测修正）：DEAL/ORDER 的 m_nDirection 是
# EEntrustBS，**对股票恒为 48**（迅投官方文档：股票此值始终是 48），用它判买卖
# 会把卖单当买单（实测 t-sell-0001 601827 卖出 100 股也返回 48）。真正区分
# 股票买卖的是 m_nOffsetFlag（EOffset_Flag_Type）：48=买入/开仓，
# 49/50/51/52/53/54=卖出/平仓/平今/平昨/强平/强减/本地强平。
_OFFSET_BUY = (48,)
_OFFSET_SELL = (49, 50, 51, 52, 53, 54)
# 兼容口径（仅当 offset 缺失时用）：23/24 = 股票买卖 opType，0/1 = 部分接口的方向枚举
_DIRECTION_BUY = ("23", "0", "buy", "b")
_DIRECTION_SELL = ("24", "1", "sell", "s")


def _deal_trade_type(deal: dict) -> str:
    """成交方向归一：side → offset_flag/m_nOffsetFlag → direction；判定不了返回 ''。

    桥新版本每行给 ``side``（"BUY"/"SELL"）与 ``offset_flag``；旧版本只有
    ``direction``（股票恒 48，不可用）。这里对未知值一律返回空串，由调用方
    告警并跳过——**绝不默认买入**（否则卖单会变成加仓）。
    """
    if not isinstance(deal, dict):
        return ""
    side = str(deal.get("side") or "").strip().lower()
    if side in ("buy", "b"):
        return "buy"
    if side in ("sell", "s"):
        return "sell"
    offset = deal.get("offset_flag")
    if offset is None:
        offset = deal.get("m_nOffsetFlag")
    if offset is not None:
        try:
            off = int(float(str(offset).strip()))
        except (TypeError, ValueError):
            off = None
        if off in _OFFSET_BUY:
            return "buy"
        if off in _OFFSET_SELL:
            return "sell"
    raw = deal.get("trade_type")
    if raw is None:
        raw = deal.get("direction")
    value = str(raw).strip().lower()
    if value in _DIRECTION_BUY:
        return "buy"
    if value in _DIRECTION_SELL:
        return "sell"
    return ""


class OrderStateMachine:
    """委托状态机：持久化 → 匹配 order_ref → 轮询 /deals 回填 → 终态/超时收口。

    持有本 session 在途未完结单（``pending_orders``，key=LiveOrder.id）与最近一次成交
    回填时点（``last_backfill_time``）。主池（bar/approve/submit/超时收割/收盘清扫）
    与 deals 池（5s 回填）为两个独立单 worker 池、真并发调用本对象；跨池记账
    （backfill/apply、pending_orders 增删与遍历）由引擎级 ``_book_lock``
    （LiveEngine，threading.RLock）在调用点串行化——本协作者内部不加锁，
    避免嵌套持锁语义混乱（锁序恒为 book_lock → get_tdx_lock，无死锁）。
    """

    def __init__(self, ctx: EngineContext, emit: Callable[[str, dict], None],
                 resolve_attribution: Optional[Callable[[str], Optional[tuple]]] = None):
        self._ctx = ctx
        # SSE 事件发射回调（LiveEngine 注入 self._emit，走 EventBus）。
        self._emit = emit
        # 外部成交归属解析：code -> (portfolio_strategy_id, strategy_id) 或 None。
        # 与 _import_external_positions 同一口径（LiveEngine 注入 _strategy_ctx_for_code），
        # 未注入时退化为「首个组合的首个策略」，都没有则该笔成交只记 WARN 不入库。
        self._resolve_attribution = resolve_attribution
        # 切片5（I4）：Core 重启后从 DB 挂回的未完结 LiveOrder（submitted/partial），
        # 主循环 poll_deals 据此轮询 /deals 回填。key=LiveOrder.id。
        # 运行中 _handle_bar 发单也计入、拒单弹出；每轮回合重查 DB 同步（G7）。
        self.pending_orders: Dict[int, LiveOrder] = {}
        # G7（0011 §5.11）：最近一次 /deals 成交回报回填时点（None=尚无回填）。
        self.last_backfill_time: Optional[datetime] = None
        # A-2：在途单超时撤单的连续失败计数与已告警标记（进程内，重启归零可接受）。
        self._cancel_fail_counts: Dict[int, int] = {}
        self._cancel_alerted: set = set()

    # ------------------------------------------------------------------
    # 持久化 / 匹配
    # ------------------------------------------------------------------
    def persist_order_submitted(self, db: Session, order: OrderEvent,
                                status: str = "submitted") -> LiveOrder:
        """写 LiveOrder(status 默认 submitted)，不写 LiveTrade（正确成交后另写）。

        status="deferred"（P2 持久化）：B-1 延期单落库——重启 recover 重建
        内存 deferred 队列后删除该行（转移语义）。否则日线级止损/信号单随
        重启消失、永不执行（2026-11-03 实证：止损 defer 57 次零执行）。

        提交时（G1/I4）：_handle_bar 先落库 + commit，再发 passorder（万一
        passorder 已发但未确认成功，DB 里永远有 submitted 记录兜底）。
        """
        live_order = LiveOrder(
            live_session_id=self._ctx.session_id,
            portfolio_strategy_id=order.portfolio_id,
            strategy_id=order.strategy_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type.value.lower(),  # "buy"/"sell"
            order_type="limit",
            price=order.price,
            quantity=order.quantity,
            filled_quantity=0,
            filled_price=None,
            status=status,
            signal_name=order.signal_name or None,
            signal_type=order.signal_type.value if order.signal_type else None,
            bar_time=order.bar_time,
        )
        db.add(live_order)
        db.flush()  # 取 live_order.id
        return live_order

    def try_match_order_ref(self, live_order: LiveOrder, claimed_refs=None,
                            orders=None) -> None:
        """轮询桥 /orders 定位本单的 m_strOrderRef 回写（G3 匹配键）。

        passorder 返回 0 无法预知 OrderRef，需从桥 /orders 列表定位本单。两层策略：

        1. **remark 精确认领（主路径）**：下单时 Core 把确定性 bridge_order_id 前 20 位
           作为 userOrderId 传给 passorder，柜台写回委托的 m_strRemark。本单已记录
           bridge_order_id（正常路径下单成功即写）时，按 remark 全局唯一精确定位——
           不依赖代码/方向/数量，彻底杜绝同代码同向同量旧单误绑（真机 bug 见下）。
        2. **模糊+时间窗（遗留兜底）**：重启恢复的历史单可能无 bridge_order_id，退回
           source=BRIDGE + 代码 + direction(48买/49卖) + volume 组合键。两个加固：
           (a) **跳过带 remark 的候选**——它们属于已被 bridge_order_id 跟踪的单，不能
               被遗留单冒领；
           (b) **时间窗**：候选柜台插入时间(上海本地)换算 UTC 后不得早于本单
               created_at 超过 _ORDER_INSERT_TOLERANCE，挡住跨会话遗留的同代码同向同量
               旧单（真机 bug：新单 11:21 创建时真单尚未可查，被匹配到 09:55 遗留单 →
               回填错成交 1.041、真单 1.037 丢失）。

        同代码同向同量可能有多笔在途单，候选按 insert_date+insert_time 降序取最新，且
        跳过 claimed_refs 中已被本 session 其他单占用的 order_ref。找不到 → 留 None，
        下轮 poll_deals 再找。

        orders：调用方已拉取的 /orders 列表（poll_deals 一次查询复用于定位+终态同步，
        避免每笔单各查一次）；None 时本方法自取（_handle_bar 单笔下单后立即定位走此路径）。
        """
        if orders is None:
            try:
                orders = self._ctx.dispatcher.query_orders()
            except BridgeUnavailableError:
                return  # 桥离线，下轮再找
        claimed = claimed_refs if claimed_refs is not None else set()
        bridge_oid = live_order.bridge_order_id
        # 通道不支持 remark 回写（如 THS UI 自动化）时，bridge_order_id 无法在柜台
        # 委托上找到，直接走 legacy 模糊匹配，避免 180s 后被误判超时拒单。
        supports_remark = getattr(self._ctx.dispatcher, "supports_remark", True)
        if bridge_oid and supports_remark:
            ref = self.match_by_remark(orders, bridge_oid[:20], claimed)
            if ref is None:
                # ① 精确再试一次：GET /orders?order_id=<bridge_order_id>。桥支持按
                # cl_ord_id/order_ref/order_id/remark 任一匹配；柜台「当日委托查询」
                # 对废单/秒成单可能不回显全量行，精确查询仍能命中（避免退化到
                # 模糊匹配误绑）。只在 remark 全量匹配落空时发这一次请求。
                try:
                    rows = self._ctx.dispatcher.query_orders(order_id=bridge_oid)
                except BridgeUnavailableError:
                    rows = []
                ref = self.match_by_remark(rows, bridge_oid[:20], claimed)
        else:
            ref = self.match_legacy_fuzzy(live_order, orders, claimed)
        if ref is not None:
            live_order.order_ref = ref

    @staticmethod
    def match_by_remark(orders, expected_remark, claimed):
        """主路径：按 m_strRemark 全局唯一精确认领本单的 order_ref。"""
        candidates = []
        for o in orders or []:
            if o.get("source") != "BRIDGE":
                continue
            if o.get("remark") != expected_remark:
                continue
            ref = o.get("order_ref")
            if ref is None or ref in claimed:
                continue
            candidates.append(o)
        if not candidates:
            return None
        candidates.sort(
            key=lambda o: (
                str(o.get("insert_date") or ""),
                str(o.get("insert_time") or ""),
            ),
            reverse=True,
        )
        return candidates[0].get("order_ref")

    @staticmethod
    def match_legacy_fuzzy(live_order, orders, claimed):
        """遗留兜底：无 bridge_order_id 的重启单用代码+方向+数量模糊匹配。

        仅认无 remark 的候选（带 remark 的属于已跟踪单），且柜台插入时间须落在本单
        created_at 的时间窗内，挡住跨会话遗留旧单。
        I-2（终审）：``insert_fabricated=True`` 的候选（通达信通道 get_orders 对陌生
        ref 补记的当前时间，非柜台真实插入时间）一律不绑——补记时间恒为 now，时间窗
        校验对它无意义，同形态遗留单/手工单会在新单创建后的首次回查即被误绑（真钱
        风险）。宁可留 None 下轮再找（真实时间候选出现后正常绑定）。
        """
        code = live_order.stock_code
        op_dir = 48 if live_order.trade_type == "buy" else 49
        created_utc = live_order.created_at
        # created_at 为 UTC naive；缺失则无法做时间窗校验，保守不绑定（避免重蹈误绑）。
        if created_utc is None:
            return None
        earliest = created_utc - _ORDER_INSERT_TOLERANCE
        candidates = []
        for o in orders or []:
            if o.get("source") != "BRIDGE":
                continue
            # 带 remark 的候选属于有 bridge_order_id 的在跟踪单，遗留单不得冒领。
            if o.get("remark"):
                continue
            # I-2：fabricated 插入时间是补记的 now，非柜台真实值——不参与绑定。
            if o.get("insert_fabricated"):
                continue
            inst = o.get("instrument") or ""
            exch = o.get("exchange") or ""
            if "%s.%s" % (inst, exch) != code:
                continue
            if o.get("direction") != op_dir:
                continue
            if o.get("volume") != live_order.quantity:
                continue
            ref = o.get("order_ref")
            if ref is None or ref in claimed:
                continue
            insert_utc = _parse_insert_utc(o.get("insert_date"), o.get("insert_time"))
            if insert_utc is None or insert_utc < earliest:
                continue  # 无法解析时间或早于创建窗口 → 视为遗留旧单，排除
            candidates.append(o)
        if not candidates:
            return None
        candidates.sort(
            key=lambda o: (
                str(o.get("insert_date") or ""),
                str(o.get("insert_time") or ""),
            ),
            reverse=True,
        )
        return candidates[0].get("order_ref")

    # ------------------------------------------------------------------
    # 轮询 / 回填 / 终态
    # ------------------------------------------------------------------
    def poll_deals(self) -> None:
        """主循环每轮：查未完结 LiveOrder → 定位 OrderRef → 轮询桥 /deals → 回填（G2）。

        每轮处理：未回填 order_ref 的单先尝试定位；有 order_ref 的单按 order_ref
        过滤 /deals 成交回报，调 backfill_order 更新状态 + 写 LiveTrade + apply。
        成交回填后，按 /orders 的 m_nOrderStatus 同步终态（53部撤/54全撤→canceled，
        57废单→rejected；55部成是在途不碰、56全成走deals回填），
        把终态单转出 submitted——第一层自愈，覆盖 GUI 撤单/收盘自动撤/柜台废单。
        桥离线/查失败 → 本轮跳过（不改状态），下轮重试。
        返回值：本轮带撤单请求标记且收敛为 canceled 的单 id 列表（撤单收敛统一
        requote 挂点，LiveEngine._tick_deals 据此追价重下；其余调用方忽略）。
        """
        db = self._ctx.db_session_factory()
        session_id = self._ctx.session_id
        try:
            # 待处理单 = 在途（submitted/partial）+ 已被收口但可能迟到成交的桥单
            # （rejected 且 error_message 带 bridge_order_untraceable 前缀）。后者必须
            # 继续参与 order_ref/remark 匹配，否则收口后柜台迟到成交永远无法纠正。
            pending = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == session_id,
                    or_(
                        LiveOrder.status.in_(["submitted", "partial"]),
                        and_(
                            LiveOrder.status == "rejected",
                            LiveOrder.error_message.like(
                                _UNTRACEABLE_MARKER + "%"),
                        ),
                    ),
                )
                .all()
            )
            if not pending:
                self.sync_pending_orders(db)  # 无在途单 → 清空计数（G7）
                # 无在途单也必须查 /deals：柜台侧人工/外部成交没有平台委托记录，
                # 这是它们唯一能入库的路径（2026-11-06 实证：8790 手工买卖两笔已
                # 成交，平台成交列表 0 条、last_backfill_time 永久为空）。
                try:
                    deals = self._ctx.dispatcher.query_deals()
                except BridgeUnavailableError:
                    return []  # 桥离线：下轮重试，不动任何状态
                self.last_backfill_time = now_shanghai()  # 轮询存活标记（0 条也写）
                self.record_external_deals(db, deals)
                db.commit()
                return []
            # 0. 一次 /orders 查询，复用于 order_ref 定位 + 撤单终态同步（避免每笔单各查一次）。
            #    桥离线时 orders=None：定位与终态同步都跳过，本单留 submitted 下轮重试；
            #    /deals 回填仍可独立进行（成交优先）。
            try:
                orders = self._ctx.dispatcher.query_orders()
            except BridgeUnavailableError:
                orders = None
            # 1. 未回填 order_ref 的单：尝试定位。
            # claimed_refs = 本 session 所有已占用 order_ref（历史已回填 + 本轮刚分配），
            # 防止同代码同向同量的多笔在途单撞同一个 ref（重复回填 → 虚拟持仓虚高）。
            claimed_refs = set(
                r[0] for r in db.query(LiveOrder.order_ref).filter(
                    LiveOrder.live_session_id == session_id,
                    LiveOrder.order_ref.isnot(None),
                ).all()
            )
            for lo in pending:
                if lo.order_ref is None:
                    self.try_match_order_ref(lo, claimed_refs, orders=orders)
                    if lo.order_ref is not None:
                        claimed_refs.add(lo.order_ref)
            # 1b. 陈旧单失效：始终匹配不到 order_ref 且超过阈值的 submitted/partial 单 → rejected。
            # （抽为 expire_stale_orders 供主循环 _tick_main 复用——deals 循环被 60s 主循环
            # 饿死时，超时检查随主循环 60s 节拍跑，不再 440s 才生效。两处调用幂等。）
            self.expire_stale_orders(db, pending)
            db.commit()
            # 2. 查 /deals 回填。两类待回填单共用一次 /deals 查询：
            #    need_ref  = 已定位 order_ref 的单（主路径，按 order_ref 过滤 deals）
            #    need_remark = order_ref 始终匹配不上（/orders 实时表无此单）但有
            #                  bridge_order_id 的单 → 按 remark 直连 /deals 回填（修复 A）。
            #    修复 A 背景（真机 2026-08-19 id45/46）：QMT get_trade_detail_data(ORDER)
            #    对已成交单不可靠（成交后从 ORDER 实时表移除），Core 轮询 /orders 拿不到
            #    order_ref → 旧逻辑走到超时 rejected 且成交不回填。但 /deals（DEAL 表）保留
            #    全部已成交记录且 DEAL 对象带 m_strRemark，故 order_ref 匹配失败的单改按
            #    bridge_order_id[:20] 在 /deals 直连 remark 匹配回填，绕过 order_ref。
            #    backfill_order 本就用 live_order.id 写 LiveTrade、不依赖 order_ref，
            #    此处只是补一条进入它的路径。
            need_ref = [lo for lo in pending if lo.order_ref is not None]
            need_remark = [
                lo for lo in pending
                if lo.order_ref is None and lo.bridge_order_id
            ]
            # 每轮都查一次 /deals：既回填在途单（need_ref / need_remark 两类），
            # 也补记柜台侧没有平台委托的成交（人工/外部下单，见 2c）。
            try:
                deals = self._ctx.dispatcher.query_deals()
            except BridgeUnavailableError:
                deals = None  # 桥离线：本轮跳过成交回填，但终态同步仍可据 /orders 推进
            if deals is not None:
                # 轮询存活标记：成功取到 /deals 即更新（本轮 0 条成交也算），
                # 供「成交回填轮询是否在跑」判读；旧实现只在真回填时才写 → 恒为空。
                self.last_backfill_time = now_shanghai()
                # 2a. 主路径：有 order_ref 的单按 order_ref 过滤 deals
                for lo in need_ref:
                    matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                    if not matched:
                        continue
                    self.backfill_order_or_late_fill(db, lo, matched)
                # 2b. 修复 A：order_ref 匹配不上的单按 remark 直连 /deals 回填。
                # 跳过已被主路径回填（status 已转 filled/partial）的单；remark 匹配键 =
                # bridge_order_id[:20] = 桥 passorder 写入委托/成交的 m_strRemark。
                for lo in need_remark:
                    if lo.status not in ("submitted", "partial", "rejected"):
                        continue  # 主路径已回填，不重复
                    expected_remark = lo.bridge_order_id[:20]
                    matched = [d for d in deals if d.get("remark") == expected_remark]
                    if not matched:
                        continue
                    self.backfill_order_or_late_fill(db, lo, matched)
                # 2c. 柜台已成交但平台无对应在途单（人工/外部下单）→ 补记入库
                self.record_external_deals(db, deals)
                db.commit()
            # 3. 终态同步（第一层自愈）：成交回填后，按 /orders m_nOrderStatus 把
            #    53部撤/54全撤→canceled、57废单→rejected 转出 submitted。必须在 deals 回填
            #    之后——否则 backfill_order 会把刚标的 canceled 覆盖回 partial。53部撤的部分
            #    成交真实存在，此处补 apply（backfill_order 对 partial 不 apply，撤单终态必落持仓）。
            #    注意 55=部成是在途非终态，绝不在此处理（2026-08-24 官方码修正）。
            cancel_converged = self.sync_terminal_order_status(
                db, pending, orders, deals)
            # id 须在 commit 前取（expire_on_commit=True 的 session close 后
            # 访问属性会 DetachedInstanceError）；LiveEngine._tick_deals 据 id
            # 重查后走撤单收敛统一 requote 判定。
            converged_ids = [lo.id for lo in cancel_converged]
            db.commit()
            # 4. G7：重查剩余 submitted/partial 同步在途集合（filled/rejected/canceled 自然移除）
            self.sync_pending_orders(db)
            return converged_ids
        finally:
            # 异常向上抛到 _deals_loop 统一记日志（不再此处静默吞），
            # rollback 清理未提交事务，close 归还连接。
            db.rollback()
            db.close()

    def backfill_order_or_late_fill(self, db: Session, live_order: LiveOrder,
                                    matched_deals: list) -> None:
        """成交回填统一入口：若该单此前已被「不可追溯」收口为 rejected，纠正并告警。

        背景：超长兜底窗口（``_UNTRACEABLE_GRACE_SECONDS``）把毫无踪迹的桥单收口为
        rejected 以释放同股同向在途门。若柜台随后才回显该笔成交（迟到回报/桥恢复），
        必须允许纠正——否则 DB 已拒、柜台已成交，虚拟持仓/现金与真实账户脱节。
        纠正动作 = 正常 ``backfill_order``（状态 filled/partial、写/更新 LiveTrade、
        filled 才 apply）+ 一条 ``late_fill_after_untraceable`` risk 事件（避免重复：
        纠正后 status 不再是 rejected，后续轮次不会再次触发）。
        """
        was_untraceable = (
            live_order.status == "rejected"
            and (live_order.error_message or "").startswith(_UNTRACEABLE_MARKER)
        )
        self.backfill_order(db, live_order, matched_deals)
        if not was_untraceable or live_order.status == "rejected":
            return  # 无实际成交（total_qty<=0）→ 仍 rejected，不误报纠正
        filled = int(live_order.filled_quantity or 0)
        live_order.error_message = (
            "%s 已收口为已拒的委托出现迟到成交，已纠正并记账（%d/%d 股），"
            "请核对柜台与持仓" % (_LATE_FILL_MARKER, filled, int(live_order.quantity or 0))
        )
        logger.warning(
            "late fill after untraceable: order %s %s %s corrected to %s "
            "(filled=%d/%d)",
            live_order.id, live_order.trade_type, live_order.stock_code,
            live_order.status, filled, int(live_order.quantity or 0),
        )
        self._emit("risk", {
            "session_id": self._ctx.session_id,
            "rule": "late_fill_after_untraceable",
            "order_id": live_order.id,
            "bridge_order_id": live_order.bridge_order_id,
            "broker_order_id": live_order.order_ref,
            "stock_code": live_order.stock_code,
            "trade_type": (live_order.trade_type or "").upper(),
            "quantity": filled,
            "submitted_at": (
                live_order.created_at.isoformat() if live_order.created_at else None),
            "message": (
                "已按不可追溯收口（rejected）的委托出现迟到成交：平台单号 %s，"
                "柜台单号 %s，%s %s %d 股，已纠正为 %s 并正常记账，请核对柜台与持仓"
                % (live_order.id, live_order.order_ref or "-", live_order.stock_code,
                   (live_order.trade_type or "").upper(), filled, live_order.status)
            ),
        })

    def record_external_deals(self, db: Session, deals: list) -> int:
        """补记柜台侧没有平台委托的成交（人工/外部下单），按幂等键去重入库。

        背景（2026-11-06 实证）：在 8790 柜台/客户端手工下了买单与卖单，桥 /deals
        两条 status=56 成交、持仓已变；平台成交/委托列表 0 条——旧实现只在「有在途
        单」时才查 /deals，且只按 order_ref/remark 认领在途单，柜台侧人工成交没有任何
        入口（既不跳过也不告警，纯静默丢失）。

        认领判定：deal 的 order_ref 命中本 session 任一 LiveOrder.order_ref，或 remark
        命中任一 bridge_order_id[:20]，即视为平台委托的成交，交给 2a/2b 回填；两者都
        不命中的就是外部成交，在此补记。

        幂等：bridge_order_id = ``ext:<order_ref>``（无 order_ref 退化为 trade_id），
        每轮按该键查已存在的补记单——已补且成交量未增则跳过；成交量增加（分笔成交）
        则复用现有单重新聚合，不会重复入库、也不会漏掉后到的分笔。

        记账口径（2026-11-06 二次修正）：**只留痕，不 apply 到虚拟账本**。
        外部成交往往在 60s 桥持仓快照「之前/之间」完成，快照里的 volume/cost 已经
        包含它；若再 apply 一次就变成双计（实测 000001 虚拟=200/桥实际=100）。
        虚拟账本的数量与成本由 ``_mirror_bridge_positions`` 按桥 /positions 镜面
        覆盖，这里只写 LiveOrder/LiveTrade + 更新 ``strategy_cost`` 归因。
        归属：与 ``_import_external_positions`` 同一解析器；解析不到策略则跳过并告警
        （LiveOrder.portfolio_strategy_id/strategy_id 非空外键，不能凭空造）。
        """
        if not deals:
            return 0
        session_id = self._ctx.session_id
        rows = db.query(LiveOrder.order_ref, LiveOrder.bridge_order_id).filter(
            LiveOrder.live_session_id == session_id,
        ).all()
        # 补记单自己也带 order_ref，必须排除在「已认领」之外，否则同一外部委托的
        # 后到分笔会被误判成平台单而永远不再聚合。
        claimed_refs = set(
            r[0] for r in rows
            if r[0] and not str(r[1] or "").startswith(_EXTERNAL_KEY_PREFIX))
        claimed_remarks = set(
            str(r[1])[:20] for r in rows
            if r[1] and not str(r[1]).startswith(_EXTERNAL_KEY_PREFIX))

        groups: Dict[str, list] = {}
        for d in deals:
            if not isinstance(d, dict):
                continue
            ref = str(d.get("order_ref") or "").strip()
            remark = str(d.get("remark") or "").strip()
            if ref and ref in claimed_refs:
                continue
            if remark and remark in claimed_remarks:
                continue
            code = _deal_code(d)
            if not code:
                continue
            key = ref or remark or "%s|%s" % (code, d.get("trade_id") or d.get("trade_time"))
            groups.setdefault(key, []).append(d)

        recorded = 0
        for key, group in groups.items():
            deal_id = str(group[0].get("trade_id") or "").strip()
            dedupe_key = (_EXTERNAL_KEY_PREFIX + (key or deal_id))[:32]
            code = _deal_code(group[0])
            trade_type = _deal_trade_type(group[0])
            qty = sum(int(d.get("volume") or 0) for d in group)
            if qty <= 0:
                continue
            if trade_type not in ("buy", "sell"):
                # 方向不可判定：绝不默认买入（旧实现把 48 一律当买，卖单变加仓）。
                # 只告警跳过，不写任何账（也不 apply）。
                logger.warning(
                    "external deal skipped: unknown direction %r "
                    "(side=%r offset_flag=%r, %s qty=%d session %s)",
                    group[0].get("direction"), group[0].get("side"),
                    group[0].get("offset_flag"), code, qty, session_id)
                continue
            existing = db.query(LiveOrder).filter(
                LiveOrder.live_session_id == session_id,
                LiveOrder.bridge_order_id == dedupe_key,
            ).first()
            if existing is not None:
                if int(existing.filled_quantity or 0) >= qty:
                    continue  # 已完整补记，幂等跳过
                self.backfill_order(db, existing, group, apply=False)  # 后到分笔：重新聚合
                continue
            target = None
            if self._resolve_attribution is not None:
                target = self._resolve_attribution(code)
            if target is None:
                logger.warning(
                    "external deal not attributable: %s %s x%d (session %s) — "
                    "no portfolio strategy to book it, skipped",
                    trade_type, code, qty, session_id)
                continue
            pid, sid = target
            amount = sum(Decimal(str(d.get("amount") or 0)) for d in group)
            avg_price = (amount / Decimal(qty)) if amount > 0 else Decimal("0")
            trade_time = self.parse_trade_time(group[-1])
            live_order = LiveOrder(
                live_session_id=session_id,
                portfolio_strategy_id=pid,
                strategy_id=sid,
                stock_code=code,
                trade_type=trade_type,
                # order_type 长度 10：external 是对外可辨识的「非策略下单」标记
                order_type="external",
                price=avg_price,
                quantity=qty,
                status="submitted",  # 交给 backfill_order 按成交量转 filled
                error_message=(
                    _EXTERNAL_DEAL_MARKER
                    + "柜台成交无对应平台委托（人工/外部下单），仅补记留痕；"
                      "持仓数量/成本以桥快照为准，不重复记账"),
                signal_name="external_deal",
                bar_time=trade_time,
                order_ref=str(group[0].get("order_ref") or "").strip() or None,
                bridge_order_id=dedupe_key,
            )
            db.add(live_order)
            db.flush()  # 取 id 供 LiveTrade 外键 + 事件
            self.backfill_order(db, live_order, group, apply=False)
            recorded += 1
            logger.info(
                "external deal recorded (ledger untouched): trade_id=%s %s %s "
                "qty=%d price=%s (order %s, session %s, source=bridge_unmatched)",
                deal_id or "-", trade_type, code, qty, avg_price,
                live_order.id, session_id)
        if recorded:
            logger.info(
                "external deals backfilled: %d (session %s, source=bridge_unmatched)",
                recorded, session_id)
        return recorded

    def sync_terminal_order_status(self, db: Session, pending: list,
                                   orders: Optional[list],
                                   deals: Optional[list]) -> list:
        """终态同步（第一层自愈）：据 /orders 的 m_nOrderStatus 转出 submitted。

        QMT 收盘自动撤 / GUI 手动撤 / 柜台废单后，ORDER 实时表里该单 status 变终态：
          53=部撤（部分成交后撤剩余）、54=全撤（无成交）、57=废单（柜台拒单）。
          55=部成是**非终态**（剩余仍在撮合），不在此处理——误判会提前 cancel 真实在途单，
          剩余成交后重复 apply/丢单（2026-08-24 据官方码修正，旧代码误把 55 当部撤）。
          56=全成走 deals 回填，不碰。
        Core 旧状态机只认 /deals 成交与 order_ref 超时，从不读 status，导致已撤单（有
        order_ref、无成交）永远卡在 submitted（真机 id40/41/44，F7 在途门被污染）。本方法
        在成交回填之后补这个出口。

        安全约束：
        - 必须在 backfill_order 之后调（否则回填会把 canceled 覆盖回 partial）。
        - 只认 /orders 列表里**确实查到**的单；查不到（实时表移除）不据缺席判撤——已成单
          同样会被移除，缺席无法区分，保持 submitted 等下轮 /deals 或超时兜底。
        - 部撤(53)的部分成交是真实持仓：必须先 apply 再 canceled。若 /orders 报 traded_volume>0
          但本单 filled_quantity 尚未追上（/deals 回报滞后或离线），**延后不 cancel**——
          等下轮 /deals 把成交价/量/金额回填齐再 apply，避免无价格依据的空 apply。
        - 废单(57)按 rejected 处理（语义：柜台拒单，非我方撤）；正常不会带成交，若有残留
          filled_quantity 一并保留记录但不再 apply。

        返回值（撤单收敛统一 requote 挂点）：本轮带撤单请求标记
        （_CANCEL_REQUESTED_TAG）且收敛为 canceled 的单列表。TDX/THS 当日委托表
        **保留已撤单**（不消失），真机超时撤单实际全走本路径（53/54 终态）收敛，
        requote 判定必须覆盖本路径——调用方（LiveEngine._tick_deals）据此做有限
        追价重下；非标记单（GUI 手撤/收盘自动撤/柜台正常收敛）不进返回值，不重下。
        """
        if not orders:
            return []  # 桥离线或无 /orders 数据：无法判终态，全部留待下轮
        cancel_converged: list = []  # 带撤单标记且本轮收敛 canceled（requote 挂点）
        # 按 order_ref 索引 /orders（有 ref 才能精确对应本单；无 ref 的单此处不处理，
        # 它们走 expire 超时或 remark /deals 回填路径）。
        by_ref = {}
        for o in orders:
            ref = o.get("order_ref")
            if ref is not None:
                by_ref[ref] = o
        for lo in pending:
            if lo.status not in ("submitted", "partial"):
                continue  # 本轮已 filled/rejected/canceled，不碰
            if lo.order_ref is None:
                continue
            o = by_ref.get(lo.order_ref)
            if o is None:
                continue  # 实时表查不到：不据缺席判撤
            status = o.get("status")
            if status in _ORDER_STATUS_TERMINAL_REJECTED:
                # 57 废单：柜台拒单 → rejected（废单通常无成交；若已有部分成交记录保留）。
                lo.status = "rejected"
                lo.error_message = "order rejected by broker as junk (m_nOrderStatus=%s)" % status
                self.pending_orders.pop(lo.id, None)
                logger.warning(
                    "terminal sync: order %s %s %s rejected (junk m_nOrderStatus=%s)",
                    lo.id, lo.trade_type, lo.stock_code, status,
                )
                self._emit("order", {
                    "portfolio_id": lo.portfolio_strategy_id,
                    "order_id": lo.id,
                    "status": "rejected",
                    "stock_code": lo.stock_code,
                    "trade_type": (lo.trade_type or "").upper(),
                    "quantity": lo.quantity,
                    "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
                    "filled_quantity": int(lo.filled_quantity or 0),
                    "error_message": lo.error_message,
                })
                continue
            if status not in _ORDER_STATUS_TERMINAL_CANCELED:
                continue  # 55 部成/56 全成或在途非终态：不处理（56 走 deals 回填，55 仍在途）
            traded_volume = int(o.get("traded_volume") or 0)
            # 部撤/全撤带成交：必须等 /deals 把成交回填齐（有成交价/量/金额）才 apply + cancel。
            # filled_quantity < traded_volume 说明成交回报滞后，延后下轮，不空 apply。
            if traded_volume > 0 and int(lo.filled_quantity or 0) < traded_volume:
                continue
            # 有已回填的部分成交（partial）→ 终态撤单前 apply 落持仓（backfill_order 对
            # partial 不 apply，此处补上；撤单后不会再有成交，这是最后的 apply 时机）。
            if int(lo.filled_quantity or 0) > 0 and lo.status == "partial":
                trade = (
                    db.query(LiveTrade)
                    .filter(LiveTrade.live_order_id == lo.id)
                    .first()
                )
                if trade is not None:
                    self.apply_filled_trade(
                        lo, trade.price, trade.quantity, trade.amount, trade.commission
                    )
            # 撤单请求标记须在 error_message 覆盖前捕获（收敛后原文被终态文案替换）
            cancel_requested = (lo.error_message or "").startswith(
                _CANCEL_REQUESTED_TAG)
            lo.status = "canceled"
            lo.error_message = "order canceled by broker (m_nOrderStatus=%s)" % status
            self.pending_orders.pop(lo.id, None)
            logger.info(
                "terminal sync: order %s %s %s canceled (m_nOrderStatus=%s, filled=%s)",
                lo.id, lo.trade_type, lo.stock_code, status,
                int(lo.filled_quantity or 0),
            )
            self._emit("order", {
                "portfolio_id": lo.portfolio_strategy_id,
                "order_id": lo.id,
                "status": "canceled",
                "stock_code": lo.stock_code,
                "trade_type": (lo.trade_type or "").upper(),
                "quantity": lo.quantity,
                "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
                "filled_quantity": int(lo.filled_quantity or 0),
                "error_message": lo.error_message,
            })
            if cancel_requested:
                cancel_converged.append(lo)
        return cancel_converged

    # ------------------------------------------------------------------
    # 超长兜底窗口 / 收口前的最后一次追溯
    # ------------------------------------------------------------------
    @staticmethod
    def _untraceable_grace_seconds() -> int:
        """超长兜底窗口（秒）：goldminer.untraceable_grace_seconds，缺键/非法回退 600。

        注意只读配置、不写回；config.yaml 未加该键时行为与模块常量一致。
        """
        try:
            gm = load_config().get("goldminer") or {}
            value = int(gm.get("untraceable_grace_seconds", _UNTRACEABLE_GRACE_SECONDS))
            return value if value > 0 else _UNTRACEABLE_GRACE_SECONDS
        except (TypeError, ValueError):
            return _UNTRACEABLE_GRACE_SECONDS
        except Exception:  # noqa: BLE001 — 配置读取故障不阻断主 tick
            return _UNTRACEABLE_GRACE_SECONDS

    @staticmethod
    def _rows_for_order(live_order: LiveOrder, rows: Optional[list]) -> list:
        """从 /orders 结果里筛出确实属于本单的行（remark/柜台号/平台单号任一命中）。"""
        remark = (live_order.bridge_order_id or "")[:20]
        oid = live_order.bridge_order_id
        ref = live_order.order_ref
        out = []
        for o in rows or []:
            if not isinstance(o, dict):
                continue
            if o.get("source") not in (None, "BRIDGE"):
                continue
            matched = False
            if remark and o.get("remark") == remark:
                matched = True
            if ref and o.get("order_ref") == ref:
                matched = True
            if oid:
                for key in ("cl_ord_id", "order_id", "entrust_id", "ref",
                            "order_ref"):
                    if str(o.get(key) or "") == oid:
                        matched = True
                        break
            if matched:
                out.append(o)
        return out

    def _probe_untraceable_trace(self, live_order: LiveOrder,
                                 deals_cache: Optional[list],
                                 deals_online: bool = True):
        """收口前最后一次追溯（防误杀）：精确 /orders + /deals（order_ref/remark）。

        返回 ``(traced, matched_deals, query_ok)``：
          - traced：委托表确实存在本单（含部成/已成/已撤）→ 交给既有回填/终态同步；
          - matched_deals：/deals 命中的成交（迟到成交/正常回填用，非空即不得收口）；
          - query_ok：追溯查询本身可用；桥离线/查询异常时为 False → 禁止收口。
        命中 `/orders` 时顺带认领 order_ref（此前为 None），让后续终态同步/成交回填
        能按柜台单号精确匹配。
        """
        query_ok = deals_online
        matched_deals: list = []
        remark = (live_order.bridge_order_id or "")[:20]
        # ① /deals：优先 order_ref（若有），否则 remark（bridge_order_id[:20]）。
        if deals_cache is None:
            try:
                deals_cache = self._ctx.dispatcher.query_deals()
            except BridgeUnavailableError:
                deals_cache = []
                query_ok = False
        if live_order.order_ref:
            matched_deals = [
                d for d in deals_cache if d.get("order_ref") == live_order.order_ref]
        if not matched_deals and remark:
            matched_deals = [d for d in deals_cache if d.get("remark") == remark]
        # ② /orders 精确查询（按 cl_ord_id/order_id/remark 任一匹配）。
        target = live_order.order_ref or live_order.bridge_order_id
        rows: list = []
        if target:
            try:
                rows = self._ctx.dispatcher.query_orders(order_id=target)
            except BridgeUnavailableError:
                rows = []
                query_ok = False
        rows = self._rows_for_order(live_order, rows)
        if rows:
            if live_order.order_ref is None:
                ref = next(
                    (str(o.get("order_ref")) for o in rows if o.get("order_ref")),
                    None,
                )
                if ref:
                    live_order.order_ref = ref
            if live_order.order_ref and not matched_deals:
                try:
                    by_ref = self._ctx.dispatcher.query_deals(
                        order_id=live_order.order_ref)
                except BridgeUnavailableError:
                    by_ref = []
                    query_ok = False
                matched_deals = [
                    d for d in by_ref if d.get("order_ref") == live_order.order_ref]
                if not matched_deals and remark:
                    matched_deals = [d for d in by_ref if d.get("remark") == remark]
        return bool(rows), matched_deals, query_ok

    def expire_stale_orders(self, db: Session, pending: Optional[list] = None) -> None:
        """陈旧单失效：始终匹配不到 order_ref 且超过阈值的 submitted/partial 单 → rejected。

        created_at 为 UTC（SQLite CURRENT_TIMESTAMP），用 utcnow 比较；
        created_at 缺失（异常数据）不过期，留给后续轮次。
        被调用两处（幂等：已 rejected 的单 status not in (submitted,partial) 跳过）：
          - poll_deals（deals 循环 5s 节拍，兜底）
          - _tick_main（主循环 60s 节拍，核心——deals 循环被单 worker 饿死时此处保证
            180s 超时最坏 60s 延迟生效，而非现状 440s）。
        pending 为调用方已查的 submitted/partial 列表（复用省一次查询）；None 则自查。
        注意：调用方负责 commit（poll_deals 在 expire 后 commit；_tick_main 自带
        try/commit/rollback/finally close，同 _persist_breaker_count 模式）。

        修复 A+（真机 2026-08-21 id54-58）：超时单标 rejected 前，先按 remark 查 /deals
        兜底一次——QMT 秒成后 ORDER 实时表移除单的 order_ref 永远 None，旧逻辑直接
        rejected 但其实 /deals（DEAL 表）有成交记录。兜底命中则 backfill_order 转 filled，
        不 reject；真空单（/deals 也无成交）才 rejected。这同时覆盖两条调用路径
        （poll_deals 的顺序 bug + _tick_main 饿死场景不查 /deals）——两处调 expire 都兜底。
        无 bridge_order_id 的单无 remark 匹配键，跳过兜底直接 reject；桥离线则容错退回 reject。
        deals 只在确有超时待兜底单时查一次（lazy），无超时单不产生查询开销。
        """
        session_id = self._ctx.session_id
        if pending is None:
            pending = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == session_id,
                    LiveOrder.status.in_(["submitted", "partial"]),
                )
                .all()
            )
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        # 修复 A+：超时单标 rejected 前，先按 remark 查 /deals 兜底一次——QMT 秒成后
        # ORDER 实时表移除单的 order_ref 永远 None，旧逻辑直接 rejected 但 /deals（DEAL 表）
        # 其实有成交记录。兜底命中则 backfill_order 转 filled 不 reject；真空单
        # （/deals 也无成交）才 rejected。同时覆盖两条调用路径（poll_deals 顺序 bug +
        # _tick_main 饿死场景不查 /deals）。deals 只在确有超时待兜底单时 lazy 查一次，复用。
        deals_cache: Optional[list] = None  # None=未查；list=已查（含空列表），全循环复用
        deals_online = True  # 桥离线时禁止收口（查不到 ≠ 不存在，防误杀）
        grace = self._untraceable_grace_seconds()
        for lo in pending:
            if lo.order_ref is not None or lo.status not in ("submitted", "partial"):
                continue
            if lo.created_at is None:
                continue
            age = now_utc - lo.created_at
            if age < _ORDER_REF_MATCH_TIMEOUT:
                continue
            # 超时单：先尝试 remark 兜底（修复 A+）。无 bridge_order_id 跳过兜底直接 reject。
            if lo.bridge_order_id:
                if deals_cache is None:
                    try:
                        deals_cache = self._ctx.dispatcher.query_deals()
                    except BridgeUnavailableError:
                        deals_cache = []  # 桥离线：本单及后续超时单都无法兜底，退回 reject
                        deals_online = False
                expected_remark = lo.bridge_order_id[:20]
                matched = [d for d in deals_cache if d.get("remark") == expected_remark]
                if matched:
                    self.backfill_order(db, lo, matched)  # 幂等：转 filled + 写 LiveTrade + apply
                    logger.info(
                        "expire: order %s %s %s rescued via remark backfill "
                        "(no order_ref within %ds, /deals matched remark)",
                        lo.id, lo.trade_type, lo.stock_code, int(age.total_seconds()),
                    )
                    continue
            # 桥通道（supports_remark=True：掘金/QMT 通道）：
            #   - 180s~grace：无法追溯时**不自动驳回也不伪造成交**（柜台当日委托查询
            #     不含废单、桥进程重启丢内存台账，但真实委托可能仍在柜台在途）；
            #     保持 pending，一次性发 risk 事件由人工核对（DB 是记帐真相）。
            #   - ≥grace：收口前最后一次追溯（精确 /orders + /deals，防误杀），
            #     仍毫无踪迹才 rejected，释放同股同向在途门（否则卡住的止损卖单会
            #     一直挡住后续风控卖出）；迟到成交由 poll_deals 的迟到回填兜底纠正。
            # 非 remark 通道（通达信/同花顺 UI）维持原「超时收口 rejected」语义不变。
            if getattr(self._ctx.dispatcher, "supports_remark", False):
                if age >= timedelta(seconds=grace):
                    traced, late_deals, query_ok = self._probe_untraceable_trace(
                        lo, deals_cache, deals_online)
                    if late_deals:
                        self.backfill_order(db, lo, late_deals)
                        logger.info(
                            "expire: order %s %s %s rescued via final trace "
                            "(age %ds >= grace %ds, /deals matched)",
                            lo.id, lo.trade_type, lo.stock_code,
                            int(age.total_seconds()), grace,
                        )
                        continue
                    if not query_ok:
                        # 桥离线/查询异常：无法确认「确实不存在」，禁止收口，保持 pending
                        lo.error_message = _UNTRACEABLE_PENDING_MARKER + (
                            "桥查询不可用（已 %ds），无法确认委托是否存在，保持 pending"
                            % int(age.total_seconds()))
                        logger.warning(
                            "expire: order %s %s %s final trace unavailable (bridge "
                            "offline) — keep pending",
                            lo.id, lo.trade_type, lo.stock_code,
                        )
                        continue
                    if traced:
                        # 委托表确实存在本单（部成/已成/已撤）：交给既有回填/终态同步，
                        # 不得按「不可追溯」收口；order_ref 已在 probe 内认领（如有）。
                        logger.info(
                            "expire: order %s %s %s traceable in order table after %ds "
                            "— keep for terminal/deal sync",
                            lo.id, lo.trade_type, lo.stock_code,
                            int(age.total_seconds()),
                        )
                        continue
                    # 确实毫无踪迹：收口 rejected，释放同股同向在途门（不生成成交、不动账）
                    self.pending_orders.pop(lo.id, None)
                    lo.status = "rejected"
                    lo.error_message = _UNTRACEABLE_EXPIRED_MARKER + (
                        "超过 %d 分钟无法在柜台查询到该委托（平台单号 %s / 柜台单号 %s），"
                        "已置已拒以释放同股同向在途门，请人工核对柜台"
                        % (max(1, grace // 60), lo.bridge_order_id or lo.id,
                           lo.order_ref or "-")
                    )
                    logger.warning(
                        "expire: order %s %s %s untraceable_expired (age %ds >= grace "
                        "%ds) — rejected to release in-flight gate",
                        lo.id, lo.trade_type, lo.stock_code,
                        int(age.total_seconds()), grace,
                    )
                    self._emit("order", {
                        "portfolio_id": lo.portfolio_strategy_id,
                        "order_id": lo.id,
                        "status": "rejected",
                        "stock_code": lo.stock_code,
                        "trade_type": (lo.trade_type or "").upper(),
                        "quantity": lo.quantity,
                        "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
                        "error_message": lo.error_message,
                    })
                    self._emit("risk", {
                        "session_id": session_id,
                        "rule": "bridge_order_untraceable_expired",
                        "order_id": lo.id,
                        "bridge_order_id": lo.bridge_order_id,
                        "broker_order_id": lo.order_ref,
                        "stock_code": lo.stock_code,
                        "trade_type": (lo.trade_type or "").upper(),
                        "quantity": lo.quantity,
                        "submitted_at": (
                            lo.created_at.isoformat() if lo.created_at else None),
                        "grace_seconds": grace,
                        "message": (
                            "委托超过 %d 分钟仍无法在柜台追溯（平台单号 %s，柜台单号 %s，"
                            "%s %s %s 股，提交于 %s），已收口为已拒以释放同股同向在途门；"
                            "迟到成交将自动纠正，请人工核对柜台"
                            % (max(1, grace // 60), lo.id, lo.order_ref or "-",
                               lo.stock_code, (lo.trade_type or "").upper(), lo.quantity,
                               lo.created_at.isoformat() if lo.created_at else "-")
                        ),
                    })
                    continue
                marker = _UNTRACEABLE_PENDING_MARKER
                if not (lo.error_message or "").startswith(marker):
                    age_s = int(age.total_seconds())
                    lo.error_message = marker + (
                        "柜台委托表与成交表均查不到该单（已 %ds），保持 pending 待人工核对"
                        % age_s)
                    logger.warning(
                        "expire: order %s %s %s untraceable (no order_ref within %ds, "
                        "no /deals remark match) — keep pending",
                        lo.id, lo.trade_type, lo.stock_code, age_s,
                    )
                    self._emit("risk", {
                        "session_id": session_id,
                        "rule": "bridge_order_untraceable",
                        "order_id": lo.id,
                        "bridge_order_id": lo.bridge_order_id,
                        "broker_order_id": lo.order_ref,
                        "stock_code": lo.stock_code,
                        "trade_type": (lo.trade_type or "").upper(),
                        "quantity": lo.quantity,
                        "submitted_at": (
                            lo.created_at.isoformat() if lo.created_at else None),
                        "grace_seconds": grace,
                        "message": (
                            "委托无法追溯：委托表与成交表均查不到（平台订单 %s，"
                            "cl_ord_id=%s，%s %s %s 股，提交于 %s），保持 pending，"
                            "超过 %d 分钟仍不可追溯将收口为已拒，请人工核对柜台"
                            % (lo.id, lo.order_ref or "-", lo.stock_code,
                               (lo.trade_type or "").upper(), lo.quantity,
                               lo.created_at.isoformat() if lo.created_at else "-",
                               max(1, grace // 60))
                        ),
                    })
                continue
            lo.status = "rejected"
            lo.error_message = (
                "order match timeout: no bridge order_ref within %ds"
                % int(age.total_seconds())
            )
            logger.warning(
                "expire: order %s %s %s rejected (no order_ref and no /deals match "
                "within %ds)",
                lo.id, lo.trade_type, lo.stock_code, int(age.total_seconds()),
            )
            self.pending_orders.pop(lo.id, None)
            self._emit("order", {
                "portfolio_id": lo.portfolio_strategy_id,
                "order_id": lo.id,
                "status": "rejected",
                "stock_code": lo.stock_code,
                "trade_type": (lo.trade_type or "").upper(),
                "quantity": lo.quantity,
                "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
                "error_message": lo.error_message,
            })

    # ------------------------------------------------------------------
    # A-2：在途单超时自动撤单（I-4 + C-1 主路径）
    # ------------------------------------------------------------------
    @staticmethod
    def _cancel_unfilled_minutes() -> int:
        """A-2 配置：tq_trade.cancel_unfilled_minutes（int，默认 3；<=0 禁用；非法回退默认）。"""
        try:
            tq = load_config().get("tq_trade") or {}
            return int(tq.get("cancel_unfilled_minutes", 3))
        except (TypeError, ValueError):
            return 3
        except Exception:  # noqa: BLE001 — 配置读取故障不阻断主 tick
            return 3

    def cancel_unfilled_orders(self, db: Session, pending: Optional[list] = None) -> None:
        """A-2（I-4 + C-1 主路径）：在途单超时未成交 → 真实撤单（每轮主 tick 调用）。

        对 submitted/partial 且**已有 order_ref** 的单：now - created_at ≥
        ``tq_trade.cancel_unfilled_minutes``（默认 3 分钟；<=0 完全跳过=禁用）→
        ``dispatcher.cancel_order(ref, code)``。通道无 cancel_order（如 QMT 桥）→ 跳过。
        partial 单同样适用（撤剩余量，已成交部分由回填收割）。

        cancel 成功后的收敛顺序（**绝不立即标 canceled 丢已成交量**）：
          1. error_message 打「超时撤单请求中：」标记（不加模型列，DB 持久化，重启后仍可识别）；
          2. 立即调一次既有 backfill（query_deals 按 order_ref 过滤）收割已成交量；
          3. 剩余量由 5s 轮询的终态同步收敛：委托表 53部撤/54全撤 → canceled；
             委托表已查不到该 ref（撤单且无成交）→ 本方法下一轮直接标 canceled。
        cancel 失败：保留 submitted 下轮重试；连续失败 ≥3 次经 _emit("risk") 告警一次
        （不刷屏），继续下轮重试。
        created_at 为 UTC naive（SQLite CURRENT_TIMESTAMP），与 expire_stale_orders
        同用 utcnow 比较。调用方负责 commit（_tick_main 与 expire 同块提交）。

        返回值（批次1 3a 挂点）：本轮经 _finalize_timeout_canceled 收敛为 canceled
        的 LiveOrder 列表——LiveEngine 据此对开启 requote_enabled 的策略做有限
        追价重下；其余调用方忽略返回值即可（行为不变）。
        注意：这只是「消失」路径的挂点——TDX/THS 当日委托表**保留已撤单**，真机
        实际全部走 5s 轮询 sync_terminal_order_status 的 53/54 终态路径收敛，该
        路径的 requote 挂点在 poll_deals 返回值（两条路径统一进 LiveEngine
        _requote_after_timeout_cancel 判定，含 _requote_fired 防双触发）。
        """
        minutes = self._cancel_unfilled_minutes()
        if minutes <= 0:
            return []
        cancel = getattr(self._ctx.dispatcher, "cancel_order", None)
        if not callable(cancel):
            return []  # 通道不支持撤单（QMT 桥等）：完全跳过
        if pending is None:
            pending = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == self._ctx.session_id,
                    LiveOrder.status.in_(["submitted", "partial"]),
                )
                .all()
            )
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        threshold = timedelta(minutes=minutes)
        targets = [
            lo for lo in pending
            if lo.status in ("submitted", "partial")
            and lo.order_ref is not None
            and lo.created_at is not None
            and (now_utc - lo.created_at) >= threshold
        ]
        if not targets:
            return []
        # 已请求撤单的单：委托表已查不到该 ref（撤单且无成交）→ 直接标 canceled；
        # 委托表仍在 → 53部撤/54全撤终态由 5s 轮询 sync_terminal_order_status 收敛。
        finalized_canceled: list = []  # 本轮收敛为 canceled 的单（requote 挂点，批次1 3a）
        tagged = [
            lo for lo in targets
            if (lo.error_message or "").startswith(_CANCEL_REQUESTED_TAG)
        ]
        if tagged:
            try:
                table_refs = {
                    o.get("order_ref") for o in self._ctx.dispatcher.query_orders()
                }
            except Exception as e:  # noqa: BLE001 — 桥离线等：本轮跳过收敛，下轮再判
                table_refs = None
                logger.debug("cancel_unfilled: query_orders failed: %s", e)
            if table_refs is not None:
                for lo in tagged:
                    if lo.order_ref in table_refs:
                        continue
                    self._finalize_timeout_canceled(db, lo)
                    if lo.status == "canceled":
                        finalized_canceled.append(lo)
        for lo in targets:
            if lo.status not in ("submitted", "partial"):
                continue  # 本轮已收敛（_finalize_timeout_canceled 标 canceled 等）
            if (lo.error_message or "").startswith(_CANCEL_REQUESTED_TAG):
                continue  # 已请求过撤单，等终态收敛（或委托表消失后下一轮收口）
            try:
                ok = bool(cancel(lo.order_ref, lo.stock_code))
            except Exception as e:  # noqa: BLE001 — 单笔失败计数重试，不阻断其余单
                logger.warning(
                    "cancel_unfilled: order %s ref %s cancel error: %s",
                    lo.id, lo.order_ref, e,
                )
                ok = False
            if ok:
                self._cancel_fail_counts.pop(lo.id, None)
                self._cancel_alerted.discard(lo.id)
                lo.error_message = (
                    _CANCEL_REQUESTED_TAG + "超时未成交已撤单（阈值 %d 分钟）" % minutes
                )
                logger.info(
                    "cancel_unfilled: order %s %s %s cancel requested (ref=%s, "
                    "age>=%dmin, filled=%s)",
                    lo.id, lo.trade_type, lo.stock_code, lo.order_ref, minutes,
                    int(lo.filled_quantity or 0),
                )
                # 实现取简：cancel 成功即按既有 backfill 收割一次已成交量（撤单前可能
                # 已有部分成交）；剩余量按委托表状态（53部撤/54全撤）由 5s 轮询
                # sync_terminal_order_status 自然收敛（含 partial 先 apply 再 cancel）。
                try:
                    deals = self._ctx.dispatcher.query_deals()
                except Exception as e:  # noqa: BLE001
                    deals = None
                    logger.debug("cancel_unfilled: query_deals failed: %s", e)
                if deals is not None:
                    matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                    if matched:
                        self.backfill_order(db, lo, matched)
            else:
                n = self._cancel_fail_counts.get(lo.id, 0) + 1
                self._cancel_fail_counts[lo.id] = n
                if n >= 3 and lo.id not in self._cancel_alerted:
                    self._cancel_alerted.add(lo.id)
                    self._emit("risk", {
                        "rule": "cancel_unfilled_timeout",
                        "portfolio_id": lo.portfolio_strategy_id,
                        "order_id": lo.id,
                        "stock_code": lo.stock_code,
                        "message": (
                            "在途单 %s（%s %s）超时撤单连续失败 %d 次，"
                            "请人工检查交易客户端/柜台" % (
                                lo.order_ref, lo.stock_code,
                                (lo.trade_type or "").upper(), n)
                        ),
                    })
                    logger.warning(
                        "cancel_unfilled: order %s ref %s cancel failed %d times "
                        "(risk alerted once)",
                        lo.id, lo.order_ref, n,
                    )
        return finalized_canceled

    def _finalize_timeout_canceled(self, db: Session, lo: LiveOrder) -> None:
        """A-2 收敛：撤单请求后委托表已查不到该单 → 标 canceled（有部分成交先 apply）。

        I-1（终审）：撤单**受理**与柜台**生效**之间存在窗口，残余成交可能仍落在
        /deals（委托表已无此单≠无成交）。finalize 前对该 ref 补一次 query_deals
        收割（与撤单成功后的即时收割同款逻辑），收割后再收口：
        - 收割后单转 filled（残量把单补齐）→ 终态由回填路径落定，不再标 canceled；
        - 收割后仍 submitted/partial → 走原有 partial apply + canceled 收口。
        """
        if lo.order_ref:
            try:
                deals = self._ctx.dispatcher.query_deals()
            except Exception as e:  # noqa: BLE001 — 桥离线：跳过收割按原逻辑收口，下轮不再重试
                deals = None
                logger.debug("finalize_timeout: query_deals failed: %s", e)
            if deals is not None:
                matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                if matched:
                    self.backfill_order(db, lo, matched)
        if lo.status not in ("submitted", "partial"):
            # 残余成交已把单补齐（filled）：终态/持仓由 backfill 路径落定
            logger.info(
                "cancel_unfilled: order %s ref %s finalized filled after residual "
                "deal harvest (%s fills) — not canceled",
                lo.id, lo.order_ref, int(lo.filled_quantity or 0),
            )
            return
        if int(lo.filled_quantity or 0) > 0 and lo.status == "partial":
            trade = (
                db.query(LiveTrade)
                .filter(LiveTrade.live_order_id == lo.id)
                .first()
            )
            if trade is not None:
                self.apply_filled_trade(
                    lo, trade.price, trade.quantity, trade.amount, trade.commission
                )
        lo.status = "canceled"
        lo.error_message = "超时未成交已撤单（撤单后委托表已查不到该单）"
        self.pending_orders.pop(lo.id, None)
        logger.info(
            "cancel_unfilled: order %s %s %s finalized canceled (ref=%s vanished "
            "from order table, filled=%s)",
            lo.id, lo.trade_type, lo.stock_code, lo.order_ref,
            int(lo.filled_quantity or 0),
        )
        self._emit("order", {
            "portfolio_id": lo.portfolio_strategy_id,
            "order_id": lo.id,
            "status": "canceled",
            "stock_code": lo.stock_code,
            "trade_type": (lo.trade_type or "").upper(),
            "quantity": lo.quantity,
            "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
            "filled_quantity": int(lo.filled_quantity or 0),
            "error_message": lo.error_message,
        })

    def sync_pending_orders(self, db: Session) -> None:
        """重查 DB 剩余 submitted/partial 同步在途集合（G7 计数）。

        以 DB 为准：回填置 filled / 拒单置 rejected 的单自然移除，_handle_bar 新增
        的单（已 commit）自然纳入。调用点在 poll_deals 各出口，保证 get_session
        读到的是最近一轮的实际在途单数。
        """
        remaining = (
            db.query(LiveOrder)
            .filter(
                LiveOrder.live_session_id == self._ctx.session_id,
                LiveOrder.status.in_(["submitted", "partial"]),
            )
            .all()
        )
        self.pending_orders = {lo.id: lo for lo in remaining}

    def _stamp_duty_for(self, live_order: LiveOrder, amount: Decimal) -> Decimal:
        """SELL 印花税补记：按组合 stamp_duty_rate × 成交额（买入恒 0）。

        口径对齐回测 SimulatedDispatcher（amount × rate，不额外取整）。
        组合上下文缺失（历史会话/组合已删）或费率缺省 → 0（宁宽勿错）。
        """
        if (live_order.trade_type or "").lower() != "sell":
            return Decimal("0")
        portfolio = next(
            (p for p in self._ctx.portfolios
             if p.portfolio_id == live_order.portfolio_strategy_id),
            None,
        )
        if portfolio is None:
            return Decimal("0")
        rate = (getattr(portfolio, "cost_params", None) or {}).get("stamp_duty_rate")
        if not rate:
            return Decimal("0")
        return amount * rate

    def backfill_order(self, db: Session, live_order: LiveOrder,
                       matched_deals: list, apply: bool = True) -> None:
        """据成交回报回填 LiveOrder + LiveTrade + apply_trade（G2/G6）。

        聚合 order_ref 下全部成交：总成交量/总金额/总佣金，成交均价 = 金额/量。
        filled（成交量 ≥ 委托量）→ status=filled + apply_trade（首次用真实价/量/佣金）；
        partial（成交量 < 委托量）→ status=partial，写/更新 LiveTrade 但不 apply
        （等最终 filled 或撤单，避免部分成交误动持仓）。

        ``apply=False``（外部/人工成交补记）：只写 LiveOrder/LiveTrade 留痕 +
        推 SSE 事件，**不动虚拟持仓与现金**——数量/成本由 60s 桥快照镜面覆盖，
        避免「桥持仓已含该成交 + 平台再 apply」的双计（2026-11-06 实测
        000001 虚拟=200/实际=100）。"""
        total_qty = sum(int(d.get("volume") or 0) for d in matched_deals)
        if total_qty <= 0:
            return  # 无实际成交（可能已撤），下轮重查
        total_amount = sum(Decimal(str(d.get("amount") or 0)) for d in matched_deals)
        total_commission = sum(Decimal(str(d.get("commission") or 0)) for d in matched_deals)
        avg_price = total_amount / Decimal(total_qty)

        # 5s 轮询会对同一 partial 反复调本方法；先记旧值，只在量增或状态变化时记日志，避免刷屏。
        prev_qty = int(live_order.filled_quantity or 0) if live_order.filled_quantity else 0
        prev_status = live_order.status
        live_order.filled_quantity = total_qty
        live_order.filled_price = avg_price
        new_status = "filled" if total_qty >= live_order.quantity else "partial"
        live_order.status = new_status
        self.last_backfill_time = now_shanghai()  # G7：记录最近一次回填时点
        # 量与状态均无变化 → 5s 轮询对同一 partial 的重复回填（audit I2 根因）：
        # 不重复发 order/trade 事件（否则 SSE 流被相同事件刷屏）；任一变化照发
        # （终态 filled 到达必发；canceled 终态由终态同步路径发射，不经本方法）。
        changed = total_qty != prev_qty or new_status != prev_status
        if changed:
            logger.info(
                "backfill: order %s %s %s %s qty=%s/%s price=%.4f amount=%s commission=%s",
                live_order.id, live_order.trade_type, live_order.stock_code, new_status,
                total_qty, live_order.quantity, float(avg_price), total_amount,
                total_commission,
            )
        # B5：订单状态推送（filled/partial 都由成交回报回填推进）
        if changed:
            self._emit("order", {
                "portfolio_id": live_order.portfolio_strategy_id,
                "order_id": live_order.id,
                "status": live_order.status,
                "stock_code": live_order.stock_code,
                "trade_type": (live_order.trade_type or "").upper(),
                "quantity": live_order.quantity,
                "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
                "filled_quantity": live_order.filled_quantity,
                "filled_price": float(avg_price),
            })

        # 写/更新 LiveTrade（按 order_ref 聚合为一笔）
        trade_time = self.parse_trade_time(matched_deals[-1])
        existing = (
            db.query(LiveTrade)
            .filter(LiveTrade.live_order_id == live_order.id)
            .first()
        )
        if existing:
            existing.price = avg_price
            existing.quantity = total_qty
            existing.amount = total_amount
            existing.commission = total_commission
            existing.stamp_duty = self._stamp_duty_for(live_order, total_amount)
            existing.trade_time = trade_time
            trade_rec = existing
        else:
            trade_rec = LiveTrade(
                live_session_id=self._ctx.session_id,
                live_order_id=live_order.id,
                portfolio_strategy_id=live_order.portfolio_strategy_id,
                strategy_id=live_order.strategy_id,
                stock_code=live_order.stock_code,
                trade_type=live_order.trade_type,
                price=avg_price,
                quantity=total_qty,
                amount=total_amount,
                commission=total_commission,
                stamp_duty=self._stamp_duty_for(live_order, total_amount),
                trade_time=trade_time,
            )
            db.add(trade_rec)
        db.flush()  # 取 trade_rec.id（B5 trade 事件用）
        # B5：成交回报推送（同量同状态的重复回填不重复发，见上 changed 注释）
        if changed:
            self._emit("trade", {
                "portfolio_id": live_order.portfolio_strategy_id,
                "trade_id": trade_rec.id,
                "stock_code": trade_rec.stock_code,
                "trade_type": (trade_rec.trade_type or "").upper(),
                "price": float(avg_price),
                "quantity": total_qty,
                "amount": float(total_amount),
            })

        # filled：回填确认后 apply_trade（submitted 阶段未 apply，此处首次落持仓）
        if apply and live_order.status == "filled":
            self.apply_filled_trade(
                live_order, avg_price, total_qty, total_amount, total_commission
            )
        elif not apply and live_order.order_type == "external":
            # 外部成交补记：数量/成本由桥快照镜面覆盖，这里只把**增量**成交
            # 记入 strategy_cost 归因（双口径展示用，不参与止损/止盈触发）。
            delta = total_qty - prev_qty
            if delta > 0:
                # 增量成交按比例分摊佣金，归因均价 =（成交额 + 分摊手续费）/ 股数
                comm_delta = (total_commission * Decimal(delta) / Decimal(total_qty)
                              if total_qty else Decimal("0"))
                portfolio = next(
                    (p for p in self._ctx.portfolios
                     if p.portfolio_id == live_order.portfolio_strategy_id), None)
                ctx = (portfolio.find_strategy(live_order.strategy_id)
                       if portfolio is not None else None)
                pos = ctx.positions.get(live_order.stock_code) if ctx else None
                if pos is not None:
                    pos.note_strategy_trade(TradeEvent(
                        strategy_id=live_order.strategy_id,
                        portfolio_id=live_order.portfolio_strategy_id,
                        stock_code=live_order.stock_code,
                        trade_type=TradeType((live_order.trade_type or "").upper()),
                        price=avg_price,
                        quantity=delta,
                        amount=avg_price * Decimal(delta),
                        commission=comm_delta,
                        stamp_duty=Decimal("0"),
                        trade_time=trade_time,
                        signal_type=(SignalType(live_order.signal_type)
                                     if live_order.signal_type else None),
                    ))

    @staticmethod
    def parse_trade_time(deal: dict) -> datetime:
        """DEAL 的 trade_date(YYYYMMDD) + trade_time(HHMMSS / HH:MM:SS) → datetime。

        桥 query_deals 返回 m_strTradeTime/m_strTradeDate 原文；解析失败用 now() 兜底。
        """
        d = str(deal.get("trade_date") or "").strip()
        t = str(deal.get("trade_time") or "").strip()
        try:
            if len(t) == 6 and t.isdigit():
                return datetime.strptime(d + t, "%Y%m%d%H%M%S")
            if ":" in t:
                return datetime.strptime(d + " " + t, "%Y%m%d %H:%M:%S")
        except (ValueError, TypeError):
            pass
        return now_shanghai()

    def apply_filled_trade(self, live_order: LiveOrder, price: Decimal,
                           qty: int, amount: Decimal, commission: Decimal) -> None:
        """回填确认 filled 后 apply_trade 更新虚拟持仓/现金（G6）。

        仅在此处 apply——submitted 阶段不 apply，真实成交回报确认后才动虚拟账户。
        signal_type 从 LiveOrder 取（Position.apply_trade 据此判 ADD）。
        """
        portfolio = next(
            (p for p in self._ctx.portfolios
             if p.portfolio_id == live_order.portfolio_strategy_id),
            None,
        )
        if portfolio is None:
            return
        ctx = portfolio.find_strategy(live_order.strategy_id)
        if ctx is None:
            return
        pos = ctx.positions.get(live_order.stock_code)
        sig_type = SignalType(live_order.signal_type) if live_order.signal_type else None
        trade = TradeEvent(
            strategy_id=live_order.strategy_id,
            portfolio_id=live_order.portfolio_strategy_id,
            stock_code=live_order.stock_code,
            trade_type=TradeType(live_order.trade_type.upper()),
            price=price,
            quantity=qty,
            amount=amount,
            commission=commission,
            stamp_duty=self._stamp_duty_for(live_order, amount),
            trade_time=live_order.bar_time or now_shanghai(),
            signal_type=sig_type,
        )
        if pos is None and trade.trade_type == TradeType.BUY:
            pos = Position(live_order.stock_code)
            ctx.positions[live_order.stock_code] = pos
        elif pos is None and trade.trade_type == TradeType.SELL:
            # 虚拟持仓缺失却收到卖出成交：现金会按成交额增加但持仓无从扣减，
            # 两者口径不一致（对账只能告警）。记日志便于事后定位（手工卖出/
            # live_trades 回放缺 BUY 等），不阻断记账以免与真实账户现金背离。
            logger.warning(
                "live trade: SELL without virtual position (portfolio %s, strategy %s, "
                "code %s, qty %s, order %s) — cash credited but no position deducted",
                live_order.portfolio_strategy_id, live_order.strategy_id,
                live_order.stock_code, qty, live_order.id,
            )
        portfolio.account.apply_trade(trade)
        if pos is not None:
            pos.apply_trade(trade)
            # B5：持仓变化推送（filled 后真实持仓/成本；pnl 无市价标记暂为 0）
            self._emit("position", {
                "portfolio_id": live_order.portfolio_strategy_id,
                "strategy_id": live_order.strategy_id,
                "stock_code": live_order.stock_code,
                "quantity": pos.quantity,
                "avg_cost": float(dec_price(pos.avg_cost) or 0),
                "market_value": float(dec_money(pos.avg_cost * pos.quantity) or 0),
                "pnl": 0,
            })
