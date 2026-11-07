"""通达信 TQ 交易通道（TdxTqDispatcher）。

参照 OpenCrabTrader ``backend/brokers/tdx_tq.py``，与 Core 内嵌的 TQ 模块用同一条
``tqcenter.tq`` 连接（同进程、同一通达信客户端，见 core/tq/utils.py）。前置条件：
通达信客户端已启动并登录资金账号，且 ``PYPlugins`` 下存在 ``tqcenter.py``。

与 QMT 桥的差异（决定 Core 侧委托认领路径）：

- 通达信 ``order_stock`` 无法写入备注（remark），委托表只有 ``Wtbh`` / ``Code`` /
  ``BSFlag`` / ``Status`` / ``WtPrice`` / ``WtVol`` / ``CjVol``，**没有委托时间列**。
  因此 ``supports_remark = False``，走 Core 的 legacy 模糊匹配（代码+方向+数量+插入
  时间窗）。为让该匹配成立，``place_order`` 成功认领委托号（Wtbh）后在本进程记录
  提交时间；``query_orders`` 对本进程下过的单回填真实提交时间，对陌生 ref（遗留单/
  手工单）补记当前时间并标记 ``insert_fabricated=True``——状态机 legacy 匹配不绑
  fabricated 候选、_resolve_order_id 兜底将其沉底，避免同形态遗留单被新单误绑（I-2）。
- 行情/交易日历复用 Core 同进程 TQ（通达信）模块，与回测取数口径一致。

TQ 常量（手册）：STOCK_BUY=0 / STOCK_SELL=1；PRICE_MY=0 自填价 / PRICE_SJ=1
市价(真实价) / PRICE_ZTJ=2 涨停价 / PRICE_DTJ=3 跌停价。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from core.tq.utils import TDXConnectionError, get_tdx_lock, get_tq, inject_tqcenter_path, _tdx_path

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from .http_bridge_dispatcher import BridgeOrderRejected, BridgeUnavailableError
from .live.timing import now_shanghai
from .market_rules import _limit_ratio_for

logger = logging.getLogger(__name__)

_CONST_LOCK = threading.Lock()
_CONST_MOD = None

#: 价格模式 -> TQ price_type 常量名（config.tq_trade.price_mode）。
PRICE_MODE_MAP = {
    "cage": "PRICE_MY",
    "my": "PRICE_MY",
    "latest": "PRICE_SJ",
    "sj": "PRICE_SJ",
    "market": "PRICE_SJ",
    "ztj": "PRICE_ZTJ",
    "dtj": "PRICE_DTJ",
}

# TQ 委托状态 WTSTATUS -> QMT m_nOrderStatus（Core 状态机口径）：
#   0 无效单/废单(57) 1 未成交(50) 2 部分成交(55, 非终态) 3 全部成交(56)
#   4 部分撤单(53, 终态) 5 全部撤单(54, 终态)
_STATUS_MAP = {0: 57, 1: 50, 2: 55, 3: 56, 4: 53, 5: 54}

# 手册 query_stock_positions 字段：Code / Cbj(成本价) / TotalVol / CanUseVol
_POS_VOL_KEYS = ("TotalVol", "Amount", "Volume", "Cjsl")
_POS_AVAIL_KEYS = ("CanUseVol", "CanAmount", "CanUseVolume", "Available")
_POS_COST_KEYS = ("Cbj", "Cost", "CostPrice", "AvgPrice")
_POS_MV_KEYS = ("MarketValue", "Cszsz", "Gpszsz")

# 手册 query_stock_orders 字段：Wtbh / Code / BSFlag / Status / WtPrice / WtVol / CjVol
_ORD_ID_KEYS = ("Wtbh", "OrderId")
_ORD_PRICE_KEYS = ("WtPrice", "Price", "Wtjg")
_ORD_FILL_PRICE_KEYS = ("CjPrice", "Cjjg", "DealPrice")
_ORD_VOL_KEYS = ("WtVol", "Amount", "Volume", "Wtsl")
_ORD_FILLED_KEYS = ("CjVol", "DealAmount", "DealVolume", "Cjsl")

_ASSET_TOTAL_KEYS = ("Asset", "TotalAsset", "total_asset", "total_assets", "Zzc")
_ASSET_AVAIL_KEYS = ("Cash", "Available", "available", "Kyzj", "EnableBalance", "Kqzj", "Balance")
_ASSET_MV_KEYS = ("MarketValue", "market_value", "Cszsz", "Gpszsz")
_ASSET_BALANCE_KEYS = ("Balance", "balance", "Zye")
_ASSET_FREEZE_KEYS = ("TotalFreeze", "frozen", "Djzj")


def _num(row: dict, keys, default=None):
    """按候选字段名取数值：TQ 各版本字段名不统一，缺一套会静默变 0。"""
    for k in keys:
        if k in row:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return default


def _exchange_for(code: str) -> str:
    # 92xxxx 属北交所（新股段）须先判；11xxxx 沪市可转债（110/113/118）→SH——
    # 落入默认 SZ 分支会让持仓 exchange 与带后缀代码对不上，T+1 可用量校验
    # fail-open 成全量（当日买入即可卖出），下单也会发往错误市场。
    # 其余 6/5/9→SH、4/8→BJ、0/1/2/3→SZ。
    if code.startswith("92"):
        return "BJ"
    if code.startswith("11"):
        return "SH"
    if code.startswith(("6", "5", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def normalize_symbol(code: str) -> str:
    """6 位代码 → 带后缀（000001.SZ）。已带后缀原样返回。"""
    text = str(code or "").strip()
    if "." in text:
        return text
    code6 = re.sub(r"\D", "", text)[:6]
    if len(code6) != 6:
        return text
    return "%s.%s" % (code6, _exchange_for(code6))


def _tqconst():
    """导入 tqcenter.tqconst（与 tq 同一 PYPlugins 目录）。

    优先用 config.tq_trade.client_path（交易客户端），否则回退 config.tdx_path。
    """
    global _CONST_MOD
    with _CONST_LOCK:
        if _CONST_MOD is None:
            from core.config import load_config
            cfg = load_config()
            tq_cfg = cfg.get("tq_trade") or {}
            base = (tq_cfg.get("client_path") if isinstance(tq_cfg, dict) else "") or _tdx_path()
            inject_tqcenter_path(base)
            try:
                from tqcenter import tqconst
            except ModuleNotFoundError as e:
                raise TDXConnectionError(
                    "无法加载 tqconst（通达信 PYPlugins 下未找到 tqcenter）。"
                    "请检查配置中的通达信路径：%s" % base
                ) from e
            _CONST_MOD = tqconst
        return _CONST_MOD


def _call(fn, *args, **kwargs):
    """调用 TQ 接口并吞掉其控制台输出。

    打包后是无控制台的 GUI 程序，``sys.stdout`` 可能为 None；TQ 内部 print 会抛
    AttributeError。统一重定向到 StringIO，同时保留最后一行用于错误信息。
    """
    out = io.StringIO()
    with get_tdx_lock():
        with contextlib.redirect_stdout(out):
            return fn(*args, **kwargs)


class TdxTqDispatcher(OrderDispatcher):
    """通达信 TQ 官方接口下单通道（同进程直连通达信客户端）。"""

    channel = "tdx_tq"
    #: 通达信委托表无备注列，Core 不能按 remark 认领，只能走 legacy 模糊匹配。
    supports_remark = False
    #: 支持批量行情（query_quotes_batch）：BarPoller 一轮一次批量拉取替代逐只。
    supports_batch_quote = True

    def __init__(self, account: str = "", account_type: str = "stock",
                 price_mode: str = "my", client_path: str = "",
                 auto_confirm_dialog: bool = True, confirm_timeout: float = 12.0,
                 confirmer=None, **_kwargs):
        self._account = str(account or "")
        self._account_type = str(account_type or "stock")
        self._price_mode = str(price_mode or "my")
        self._client_path = str(client_path or "")
        # 「下单确认」自动确认（无人值守）：实盘账户 order_stock 恒返回 Value=1
        # 「已发送信号至客户端，待用户确认！」，信号落在客户端「交易信号」窗口，
        # 每行需点「发送」才真正报柜台。
        self._auto_confirm_dialog = bool(auto_confirm_dialog)
        #: 单笔下单「定位待发送行 + 点击 + 等委托表」的总时限（秒）
        self._confirm_timeout = float(confirm_timeout)
        #: 可注入的确认器（测试用）；None 时首次需要才构造
        self._confirmer = confirmer
        self._account_id = -1
        self._connected = False
        self._last_error = ""
        #: order_ref -> (YYYYMMDD, HHMMSS)：本进程下单时记录的柜台插入时间。
        #: 通达信委托表无时间列，仅靠它让 Core 的 legacy 模糊匹配时间窗成立。
        self._submit_times: Dict[str, tuple] = {}
        #: A-5（C-3）认领加固：已认领 refs（本进程单，防重复认领同一委托）与
        #: 已知 refs（委托表见过的全部 ref，进程内防重放——connect() 重连不清空）。
        self._claimed_refs: set = set()
        self._known_refs: set = set()
        #: I-2（终审）：get_orders 对「纯陌生 ref」补记的当前时间属 fabricated——
        #: 补记时间不是柜台真实插入时间（通达信委托表无时间列），时间窗校验对它
        #: 无意义。状态机 legacy 匹配一律不绑 fabricated 候选；_resolve_order_id
        #: 兜底段把 fabricated 沉底（仅无真实候选时使用）——防同形态遗留单/手工单
        #: 在首次回查被新单误绑（真钱风险）。A-4 recover 的补记（DB 已绑 ref，
        #: 属真实恢复）不算 fabricated（recover 直写 _submit_times，不经本集合）。
        self._fabricated_refs: set = set()
        #: 柜台已受理（ErrorId=0）但回查窗口内委托表未见该单 → 记本单确定性 order_id，
        #: place_order 返回 None。引擎据此把订单**保持 submitted**（不判拒单），
        #: 由后续 /deals 轮询认领或 180s 超时收口——避免真实在途单脱离跟踪。
        self.last_unconfirmed_order_id: Optional[str] = None

    # ---- 连接 ----

    @property
    def last_error(self) -> str:
        return self._last_error

    @staticmethod
    def order_id(order: OrderEvent) -> str:
        """确定性订单 ID（与 QMT 桥同一算法），仅用于 Core 侧幂等标识。

        通达信委托无法写 remark，本值不会出现在柜台上，也不参与认领。
        """
        key = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            order.strategy_id, order.portfolio_id, order.stock_code,
            order.trade_type.value, order.signal_type.value, order.signal_name,
            order.quantity, order.price,
        )
        if order.bar_time is not None:
            key += "|%s" % order.bar_time.isoformat()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    def _tq(self):
        if self._client_path:
            inject_tqcenter_path(self._client_path)
        return get_tq()

    def connect(self) -> bool:
        self._last_error = ""
        try:
            tq = self._tq()
            out = io.StringIO()
            with get_tdx_lock():
                with contextlib.redirect_stdout(out):
                    aid = tq.stock_account(account=self._account,
                                           account_type=self._account_type)
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                aid = -1
            self._account_id = aid
            self._connected = aid >= 0
            if not self._connected:
                tail = out.getvalue().strip().splitlines()
                self._last_error = tail[-1] if tail else "获取交易账户句柄失败"
            return self._connected
        except TDXConnectionError as e:
            self._connected = False
            self._last_error = str(e)
            return False
        except Exception as e:  # 通达信未启动/未登录，多为 TQ 内部异常
            self._connected = False
            self._last_error = "连接通达信失败，请确认客户端已启动并登录资金账号：%s" % e
            return False

    def heartbeat(self) -> bool:
        if not self._connected or self._account_id < 0:
            return self.connect()
        try:
            self._query("query_stock_asset", {})
        except TDXConnectionError:
            self._connected = False
            return False
        if self._last_error:
            self._connected = False
            return False
        return True

    def close(self) -> None:
        """停止时退订对账：把当前 TQ 行情订阅整表退订。

        前提：平台全局同时只允许 1 个运行中的 live session（B6），当前通达信上的
        行情订阅必属本会话，整表退订不会误伤其它会话。TQ 连接归 tqcenter 单例
        持有（行情模块共用），此处仍不关闭连接，只退订行情。best-effort：
        查订阅/退订失败仅记日志，不影响停止流程。
        """
        self._connected = False
        try:
            subscribed = _call(get_tq().get_subscribe_hq_stock_list) or []
            subscribed = [str(s).strip() for s in subscribed if str(s).strip()]
            if subscribed:
                from core.tq.data import TQData
                TQData().unsubscribe_bars(subscribed)
                logger.info("已退订 %d 只行情订阅（session 停止退订对账）", len(subscribed))
        except Exception as e:
            logger.debug("退订对账失败（忽略）：%s", e)

    def _ensure(self) -> None:
        if not (self._connected and self._account_id >= 0):
            if not self.connect():
                raise BridgeUnavailableError(
                    "通达信交易未就绪：%s" % (self._last_error or "请启动客户端并登录资金账号")
                )

    def _query(self, name: str, default):
        try:
            raw = _call(getattr(self._tq(), name), self._account_id)
        except TDXConnectionError:
            raise
        except Exception as e:
            self._last_error = "%s 查询失败：%s" % (name, e)
            logger.warning("tdx %s failed: %s", name, e)
            return default
        # TQ 查询失败时返回 {"ErrorId": "xx", "Error": "..."}，而非抛异常；
        # 不识别会把「账户未登录」当成「零持仓/零资金」，必须显式报错。
        if isinstance(raw, dict) and str(raw.get("ErrorId", "0")) not in ("", "0"):
            self._last_error = "%s 查询失败：%s" % (
                name, raw.get("Error") or raw.get("Msg") or raw)
            logger.warning("tdx %s error: %s", name, raw)
            return default
        self._last_error = ""
        return default if raw is None else raw

    # ---- 资产 / 持仓 / 委托 ----

    def get_asset(self) -> dict:
        self._ensure()
        raw = self._query("query_stock_asset", {})
        if not isinstance(raw, dict):
            return {}
        out = dict(raw)
        total = _num(raw, _ASSET_TOTAL_KEYS)
        avail = _num(raw, _ASSET_AVAIL_KEYS)
        mkt = _num(raw, _ASSET_MV_KEYS)
        bal = _num(raw, _ASSET_BALANCE_KEYS)
        frozen = _num(raw, _ASSET_FREEZE_KEYS)
        if total is not None:
            out["total_asset"] = total
        if avail is not None:
            out["available"] = avail
        if mkt is not None:
            out["market_value"] = mkt
        elif total is not None and avail is not None:
            out["market_value"] = round(total - avail, 2)
        if bal is not None:
            out["balance"] = bal
        if frozen is not None:
            out["frozen"] = frozen
        return out

    def get_positions(self) -> list:
        self._ensure()
        rows = self._query("query_stock_positions", [])
        out = []
        for r in rows if isinstance(rows, list) else []:
            vol = _num(r, _POS_VOL_KEYS, 0) or 0
            avail = _num(r, _POS_AVAIL_KEYS)
            out.append({
                "symbol": str(r.get("Code") or r.get("StockCode") or ""),
                "volume": int(vol),
                # 有「可用」列时严格采用（0 表示 T+1 锁定，不能回退成持仓量）
                "available": int(avail if avail is not None else vol),
                "cost": float(_num(r, _POS_COST_KEYS, 0) or 0),
                "market_value": float(_num(r, _POS_MV_KEYS, 0) or 0),
            })
        return out

    def get_orders(self) -> list:
        self._ensure()
        rows = self._query("query_stock_orders", [])
        out = []
        for r in rows if isinstance(rows, list) else []:
            raw_flag = r.get("BSFlag", r.get("Type", r.get("BsFlag")))
            try:
                flag = int(raw_flag or 0)
            except (TypeError, ValueError):
                flag = 0
            if raw_flag is None or flag not in (0, 1, -1):
                # 方向字段缺失/未知时静默按「买」会让卖出委托在认领与
                # 对账阶段永远匹配不上（方向不符），必须留下可见告警。
                logger.warning(
                    "委托 %s 方向字段缺失或未知（BSFlag=%r），按买入处理",
                    r.get("Wtbh") or r.get("OrderId"), raw_flag)
            if flag == -1:
                continue  # 撤单流水，不是委托
            raw_status = r.get("Status", r.get("WtStatus"))
            try:
                status = int(raw_status or 0)
            except (TypeError, ValueError):
                status = 0
            if status not in _STATUS_MAP:
                # 未知 WTSTATUS 静默映射为「未成交」会让已成交/已撤单一直
                # 挂成在途单，直到超时收割才消失；留痕便于排查 SDK 版本差异。
                logger.warning(
                    "委托 %s 未知 WTSTATUS=%r，按「未成交」处理",
                    r.get("Wtbh") or r.get("OrderId"), raw_status)
            out.append({
                "order_id": str(r.get("Wtbh") or r.get("OrderId") or ""),
                "symbol": str(r.get("Code") or r.get("StockCode") or ""),
                "direction": "buy" if flag == 0 else "sell",
                "price": float(_num(r, _ORD_PRICE_KEYS, 0) or 0),
                "fill_price": float(_num(r, _ORD_FILL_PRICE_KEYS, 0) or 0),
                "volume": int(_num(r, _ORD_VOL_KEYS, 0) or 0),
                "filled_volume": int(_num(r, _ORD_FILLED_KEYS, 0) or 0),
                "status": _STATUS_MAP.get(status, 50),
            })
        # A-5（C-3）：本轮见过的 refs 并入 _known_refs（供 _resolve_order_id「新增 ref
        # 优先」做 pre 快照）；缺 _submit_times 的 ref 用当前上海时间补记——重启单/手工单
        # 兜底认领后，状态机 legacy 匹配的时间窗（_ORDER_INSERT_TOLERANCE）才可用。
        # I-2（终审）：纯陌生 ref 的补记时间属 fabricated（非柜台真实插入时间），
        # 记入 _fabricated_refs 供匹配降级；_submit_times 里已有的 ref（本进程下单
        # place_order / A-4 recover 补记）是真实/可信时间，不算 fabricated。
        now = now_shanghai()
        date_s = now.strftime("%Y%m%d")
        time_s = now.strftime("%H%M%S")
        for o in out:
            ref = o.get("order_id")
            if not ref:
                continue
            self._known_refs.add(ref)
            if ref not in self._submit_times:
                self._submit_times[ref] = (date_s, time_s)
                self._fabricated_refs.add(ref)
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
        """委托表 → Core /orders 形状（source=BRIDGE，remark 恒空）。

        I-2（终审）：每行带 ``insert_fabricated``——该 ref 的插入时间是否为 get_orders
        对陌生 ref 补记的当前时间（非柜台真实值）。状态机 legacy 匹配据此拒绝
        fabricated 候选，防同形态遗留单/手工单被新单误绑。
        """
        out = []
        for o in self.get_orders():
            ref = str(o.get("order_id") or "").strip()
            code = re.sub(r"\D", "", str(o.get("symbol") or ""))[:6]
            if not ref or len(code) != 6:
                continue
            insert_date, insert_time = self._submit_times.get(ref, ("", ""))
            out.append({
                "source": "BRIDGE",
                "remark": "",
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
                "insert_fabricated": ref in self._fabricated_refs,
                "status": int(o.get("status") or 50),
            })
        return out

    def query_deals(self, order_id: Optional[str] = None) -> list:
        """由委托表「CjVol(成交量) + 成交价」合成成交回报（当日累计值）。

        Core 的 backfill_order 按 order_ref 聚合、用累计量覆盖 filled_quantity，
        因此直接返回累计成交行即可；佣金/印花税不在委托表暴露，按 0 记。
        """
        out = []
        for o in self.get_orders():
            ref = str(o.get("order_id") or "").strip()
            filled = int(o.get("filled_volume") or 0)
            code = re.sub(r"\D", "", str(o.get("symbol") or ""))[:6]
            if not ref or filled <= 0 or len(code) != 6:
                continue
            price = float(o.get("fill_price") or 0) or float(o.get("price") or 0)
            if price <= 0:
                continue
            trade_date, trade_time = self._submit_times.get(
                ref, (now_shanghai().strftime("%Y%m%d"), now_shanghai().strftime("%H%M%S")))
            out.append({
                "order_ref": ref,
                "remark": "",
                "instrument": code,
                "exchange": _exchange_for(code),
                "direction": 49 if o.get("direction") == "sell" else 48,
                "volume": filled,
                "price": price,
                "amount": price * filled,
                "commission": 0,
                "stamp_duty": 0,
                "trade_date": trade_date,
                "trade_time": trade_time,
            })
        return out

    def query_calendar(self) -> list:
        """交易日历：优先 TQ get_trading_dates（权威口径，约近 600 个交易日），

        空/异常回退 000001.SH 日线反推（1.3.1 前的既有路径，原样保留），
        再失败返回 [] 走 fail-open。元素保持 'YYYYMMDD' 字符串
        （TradingCalendar provider 契约）；alert/ths 均委托本方法。
        """
        try:
            from core.tq.data import TQData
            today = now_shanghai()
            start = (today - timedelta(days=900)).strftime("%Y%m%d")
            end = (today + timedelta(days=1)).strftime("%Y%m%d")
            dates = TQData().get_trading_dates(market="SH", start=start, end=end)
            out = []
            for d in dates or []:
                digits = re.sub(r"\D", "", str(d))
                if len(digits) >= 8:
                    out.append(digits[:8])
            if out:
                return out
        except Exception:
            pass
        # 回退：上证指数日线反推（原口径）
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(["000001.SH"], ["1d"], count=600)
        except Exception as e:
            self._last_error = "calendar unavailable: %s" % e
            return []
        close = (raw.get("1d") or {}).get("Close")
        if close is None or getattr(close, "empty", True):
            return []
        out = []
        for ts in close.index:
            digits = re.sub(r"\D", "", str(ts))
            if len(digits) >= 8:
                out.append(digits[:8])
        return out

    def _raw_to_bars(self, raw: dict, period: str) -> Dict[str, list]:
        """TQ get_history_raw 原始数据 → {code: [bar, ...]}。

        query_quote（单股）与 query_quotes_batch（批量）共用的转换逻辑，
        bar 字段规则与原 query_quote 逐字一致：无该周期 Close/空 → {}；
        单字段缺帧按 0.0；stime 非 14 位丢弃。
        """
        out: Dict[str, list] = {}
        block = raw.get(period) or {}
        close = block.get("Close")
        if close is None or getattr(close, "empty", True):
            return out

        def _value(field: str, code: str, ts) -> float:
            frame = block.get(field)
            if frame is None:
                return 0.0
            try:
                v = float(frame.at[ts, code])
            except Exception:
                return 0.0
            # 批量失败走 _fetch_per_stock 逐只拉取 + pd.concat(axis=1) 时，
            # 各股票日期区间不同会产生 NaN 行；NaN 会毒化下游指标与价格。
            return 0.0 if v != v else v

        for col in getattr(close, "columns", []):
            code = str(col)
            bars = []
            for ts in close.index:
                try:
                    stime = ts.strftime("%Y%m%d%H%M%S")
                except Exception:
                    digits = re.sub(r"\D", "", str(ts))
                    stime = digits[:14] if len(digits) >= 14 else ""
                if len(stime) != 14:
                    continue
                cl = _value("Close", code, ts)
                # 收盘价缺失/NaN/≤0 的 bar 无意义（停牌或拼接产物），丢弃
                # 以免污染信号与实时估值。
                if cl <= 0:
                    continue
                bars.append({
                    "stime": stime,
                    "open": _value("Open", code, ts),
                    "high": _value("High", code, ts),
                    "low": _value("Low", code, ts),
                    "close": cl,
                    "volume": _value("Volume", code, ts),
                    "amount": _value("Amount", code, ts),
                })
            out[code] = bars
        return out

    def query_quote(self, code: str, period: str = "1m", count: int = 10) -> list:
        """行情复用 Core 同进程内嵌的 TQ 模块（与回测取数口径一致）。"""
        tq_code = normalize_symbol(code)
        code6 = re.sub(r"\D", "", str(code or ""))[:6]
        if len(code6) != 6:
            return []
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(
                [tq_code], [period], count=max(int(count or 10), 2))
        except Exception as e:
            self._last_error = "quote unavailable: %s" % e
            return []
        return self._raw_to_bars(raw, period).get(tq_code, [])

    def query_quotes_batch(
        self, codes, period: str = "1m", count: int = 10,
    ) -> Dict[str, list]:
        """批量行情：一次 get_history_raw 取多只，{原始入参 code: [bar, ...]}。

        仅本通道提供（supports_batch_quote=True），BarPoller 据此一轮一次
        SDK 调用替代逐只拉取，后续判定逻辑与逐只路径完全一致。
        无效代码剔除（结果键保留原始入参）；异常返回 {} 并记 last_error
        （BarPoller 据此回退逐只路径）。
        """
        try:
            pairs = []
            seen = set()
            for code in codes or []:
                code6 = re.sub(r"\D", "", str(code or ""))[:6]
                if len(code6) != 6:
                    continue
                tq_code = normalize_symbol(code)
                if tq_code in seen:
                    continue
                seen.add(tq_code)
                pairs.append((str(code), tq_code))
            if not pairs:
                return {}
            from core.tq.data import TQData
            raw = TQData().get_history_raw(
                [tq for _, tq in pairs], [period], count=max(int(count or 10), 2))
            converted = self._raw_to_bars(raw, period)
            return {orig: converted.get(tq, []) for orig, tq in pairs}
        except Exception as e:
            self._last_error = "batch quote unavailable: %s" % e
            return {}

    # ---- 下单 ----

    def _price_type(self, price: float):
        const = _tqconst()
        if price is None or price <= 0:
            return getattr(const, "PRICE_SJ", 1)
        name = PRICE_MODE_MAP.get(self._price_mode, "PRICE_MY")
        return getattr(const, name, getattr(const, "PRICE_MY", 0))

    def _resolve_order_id(self, symbol: str, direction: str, price: float,
                          volume: int, attempts: int = 4) -> str:
        """下单后回查委托列表取真实委托号（Wtbh）。

        ``order_stock`` 返回值只是受理标志，不是委托编号；只有出现在
        ``query_stock_orders`` 里才算真正报单。半自动确认模式下会一直查不到。

        A-5（C-3）认领加固：
        - **去重**：候选 ref 已被本进程其它单认领（``_claimed_refs``）→ 跳过，
          杜绝同形状多单重复绑同一委托（重复回填 → 虚拟持仓虚高）；
        - **新增 ref 优先**：每轮回查前快照 ``_known_refs``，只认「本轮新出现」的
          ref（最可能是刚提交的本单）；全部 attempts 无新增命中 → 放宽为
          「形状匹配且未认领」的任意 ref（手工单/重启单兜底）→ 仍无 → 失败返回 ""。
        - **价格过滤放宽**：price_mode 映射 PRICE_SJ/ZTJ/DTJ（市价/最新/涨停/跌停）
          时委托表 WtPrice 可能为 0 或非传入价，跳过价格相等过滤；自填价（cage/my）
          保持价格过滤。
        I-2（终审）：兜底段对 fabricated 候选（get_orders 补记的当前时间，非本进程
        真实下单/恢复单）降级——优先认领有真实时间的候选，仅当无任何真实候选时才
        用 fabricated（同形状遗留单/手工单不被优先误绑）。
        命中即记入 ``_claimed_refs``（提交时间由 place_order 统一补记）。
        """
        skip_price = PRICE_MODE_MAP.get(self._price_mode, "PRICE_MY") != "PRICE_MY"
        fallback = ""
        fallback_fabricated = ""
        for i in range(max(attempts, 1)):
            pre = set(self._known_refs)
            for o in reversed(self.get_orders()):
                ref = str(o["order_id"])
                if not ref or ref in self._claimed_refs:
                    continue
                if normalize_symbol(o["symbol"]) != normalize_symbol(symbol):
                    continue
                if o["direction"] != direction or int(o["volume"]) != int(volume):
                    continue
                if not skip_price and price > 0 and abs(float(o["price"]) - price) > 1e-6:
                    continue
                if ref not in pre:
                    self._claimed_refs.add(ref)
                    return ref
                # I-2：兜底候选分两档——真实时间优先，fabricated 沉底。
                if ref in self._fabricated_refs:
                    if not fallback_fabricated:
                        fallback_fabricated = ref
                elif not fallback:
                    fallback = ref  # 已知 ref：先记兜底候选，本轮继续找新增
            if i < attempts - 1:
                time.sleep(0.35)
        if not fallback and fallback_fabricated:
            fallback = fallback_fabricated
        if fallback:
            self._claimed_refs.add(fallback)
        return fallback

    def cancel_order(self, order_ref: str, stock_code: str) -> bool:
        """撤单（A-1）：受理成功返回 True；失败/拒单返回 False 并记 last_error。

        SDK cancel_order_stock（tqcenter.py :4284，type=5）返回形态：
        dict(ErrorId="0")=成功 / -1=失败（参数缺失、柜台错误、内部异常，错误细节走
        控制台 print）。返回判定与 place_order 同风格。TDXConnectionError（客户端
        离线/连接失效）向上抛，由调用方决定重试节奏。
        """
        self._ensure()
        try:
            res = _call(
                self._tq().cancel_order_stock,
                account_id=self._account_id,
                stock_code=normalize_symbol(stock_code),
                order_id=str(order_ref),
            )
        except TDXConnectionError:
            raise
        except Exception as e:
            self._last_error = "撤单调用失败：%s" % e
            logger.warning("tdx cancel_order failed: %s", e)
            return False
        if res is None or res == -1:
            self._last_error = "通达信撤单失败，返回 %r" % (res,)
            return False
        if isinstance(res, dict):
            err_id = str(res.get("ErrorId", ""))
            if err_id not in ("", "0"):
                self._last_error = "通达信撤单被拒：%s" % (
                    res.get("Error") or res.get("Msg") or res)
                return False
        return True

    def _prev_close_for_limit_check(self, symbol: str) -> Optional[Decimal]:
        """取前收盘价（1d 最近 2 根里最早的一根），无有效数据返回 None。

        取最早一根：count=2 时首根即前一交易日；两根日期相同取该值（同值）；
        仅 1 根用其自身。最新一根 bar 日期必须 == 今天（上海时区）——数据滞后
        （客户端未启动/离线缓存/停牌补数）时前收与当前价基期脱节，视为无效
        → None 跳过校验（fail-open 不变）。校验自身异常一并吞为 None（宁宽勿错）。
        """
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw([symbol], ["1d"], count=2)
            close = (raw.get("1d") or {}).get("Close")
            if close is None or getattr(close, "empty", True):
                return None
            if symbol not in getattr(close, "columns", []):
                return None
            series = close[symbol].dropna().sort_index()
            if series.empty:
                return None
            if series.index[-1].date() != now_shanghai().date():
                logger.warning(
                    "涨跌停校验跳过：%s 日线最新 bar %s 非今日（数据滞后）",
                    symbol, series.index[-1].date())
                return None
            value = float(series.iloc[0])
            if value != value:  # NaN 防御
                return None
            return Decimal(str(value))
        except Exception as e:
            logger.warning("涨跌停校验取前收失败，跳过检查：%s", e)
            return None

    def check_price_limit(
        self, stock_code: str, direction: str, price
    ) -> Optional[str]:
        """涨跌停前置校验（公共口径，通道无关）：买 ≥ 涨停价 / 卖 ≤ 跌停价 →
        返回拒绝原因文案；通过/跳过返回 None。

        引擎按组合 price_limit_check_enabled 配置门控调用（与回测同门控），
        place_order 不再内联校验（防双查）。口径与回测一致（_limit_ratio_for +
        前收四舍五入到分）。get_zdt_data 只有涨跌停状态无价格，不采用。
        市价单（price 空/非正，成交价由交易所决定）与取不到有效前收（无数据 /
        前复权早期失真 <1 元）跳过；整个校验块 fail-open——校验自身故障
        绝不阻断下单，异常吞为 None。
        """
        if price is None or price <= 0:
            return None
        try:
            symbol = normalize_symbol(stock_code)
            prev_close = self._prev_close_for_limit_check(symbol)
            if prev_close is None or prev_close < Decimal("1"):
                return None
            ratio = _limit_ratio_for(symbol)
            cent = Decimal("0.01")
            limit_up = (prev_close * (1 + ratio)).quantize(cent, rounding=ROUND_HALF_UP)
            limit_down = (prev_close * (1 - ratio)).quantize(cent, rounding=ROUND_HALF_UP)
            order_price = Decimal(str(price))
            if direction == "buy" and order_price >= limit_up:
                return ("委托买价 %s ≥ 涨停价 %s（前收 %s ×%s），已拦截"
                        % (price, limit_up, prev_close, ratio))
            if direction == "sell" and order_price <= limit_down:
                return ("委托卖价 %s ≤ 跌停价 %s（前收 %s ×%s），已拦截"
                        % (price, limit_down, prev_close, ratio))
            return None
        except Exception as e:
            logger.warning("涨跌停前置校验异常，跳过检查继续下单：%s", e)
            return None

    @staticmethod
    def _snapshot_dialog_hwnds() -> set:
        """当前可见 #32770 对话框句柄集合（弹窗新增检测基线）。"""
        try:
            from pywinauto import Desktop
        except ImportError:
            return set()
        out = set()
        try:
            for w in Desktop(backend="win32").windows():
                try:
                    if w.class_name() == "#32770" and w.is_visible():
                        out.add(w.handle)
                except Exception:
                    continue
        except Exception:
            pass
        return out

    _CONFIRM_BUTTON_KEYWORDS = ("确定", "确认", "是(Y)", "是")

    @classmethod
    def _pick_confirm_dialog(cls, windows, before: set):
        """从窗口列表挑「新增的、含确认按钮的、**属于通达信进程**的 #32770
        对话框」→ (dlg, button)。

        进程归属过滤（win_dialog_scope）：双引擎并行时桌面上的弹窗可能来自
        THS（xiadan.exe）——TDX 自动化只认映像名含 tdx 的属主进程。
        """
        from .live.win_dialog_scope import dialog_matches_channel
        for w in windows:
            try:
                if w.class_name() != "#32770" or not w.is_visible():
                    continue
                if not dialog_matches_channel(w, "tdx"):
                    continue
                if w.handle in before:
                    continue
                for b in w.descendants(class_name="Button"):
                    t = b.window_text() or ""
                    if any(k in t for k in cls._CONFIRM_BUTTON_KEYWORDS):
                        return w, b
            except Exception:
                continue
        return None, None

    def _find_confirm_panel(self):
        """在 tdxw.exe 顶层窗口树内定位「下单确认面板」。

        实证（2026-11-04 真机）：通达信的确认面板**不是独立 #32770 对话框**，
        而是主框架 `TdxW_MainFrame_Class` 内的自绘面板——含「确定」按钮 +
        「买入委托/卖出委托」标题 +「…成本价、盈利等数据仅供参考」提示。
        特征判定：'确定' 与 '…仅供参考'提示语 同时存在于窗口树（卖出确认面板无买入/卖出标题）。

        返回 (frame, confirm_ctrl, cancel_ctrls) 或 (None, None, [])。"""
        from .live.win_dialog_scope import window_image_name, dialog_matches_channel
        try:
            from pywinauto import Desktop
        except ImportError:
            return None, None, []
        for w in Desktop(backend="win32").windows():
            try:
                if not w.is_visible():
                    continue
                if window_image_name(w.process_id()) != "tdxw.exe":
                    continue
                if not dialog_matches_channel(w, "tdx"):
                    continue
                texts = set()
                confirm_ctrl = None
                cancel_ctrls = []
                for c in w.descendants():
                    try:
                        t = (c.window_text() or "").strip()
                    except Exception:
                        continue
                    if not t:
                        continue
                    texts.add(t)
                    if t == "确定" and confirm_ctrl is None:
                        confirm_ctrl = c
                    if t == "取消":
                        cancel_ctrls.append(c)
                if confirm_ctrl is not None and any(
                        "仅供参考" in t for t in texts):
                    return w, confirm_ctrl, cancel_ctrls
            except Exception:
                continue
        return None, None, []

    def _find_chrome_hosts(self, frame) -> list:
        """通达信交易界面是 WebView（Chrome）渲染——确认/取消弹窗是网页 DOM
        元素，Win32/UIA 控件枚举均不可见。键盘事件必须发给 Chrome 的输入
        接收窗口（RenderWidgetHostHWND）才进入网页。"""
        hosts = []
        try:
            for c in frame.descendants():
                try:
                    if c.class_name() in ("Chrome_RenderWidgetHostHWND",
                                          "Chrome_WidgetWin_1"):
                        hosts.append(c)
                except Exception:
                    continue
        except Exception:
            pass
        return hosts

    def _confirm_order_dialog(self, before: set, timeout: float = 5.0,
                              action: str = "confirm") -> bool:
        """自动处置通达信「下单确认」面板（鼠标键盘模拟，无人值守）。

        真机实证（2026-11-04 凌晨）：
        - 面板是 tdxw 主框架内嵌 WebView 的 **网页 DOM 模态框**——「确定/
          取消/全部确认/全部取消」按钮是 HTML 元素，Win32/UIA 枚举均不可见
          （BM_CLICK/坐标点击/esc 全部无效——焦点不在网页内）；
        - **焦点注入 Chrome_RenderWidgetHostHWND 后发键盘**：网页模态框标准
          行为 Enter=确认、Esc=取消（Esc 实测面板消失 ✓）。
        - 键盘段落持 UI_AUTO_LOCK（跨引擎互斥：THS 自动化同进程并发）。

        action: confirm=Enter（执行委托）；cancel=Esc（放弃委托）。
        """
        from .live.win_dialog_scope import UI_AUTO_LOCK
        key = "{ENTER}" if action == "confirm" else "{ESC}"
        deadline = time.time() + timeout
        with UI_AUTO_LOCK:
            while time.time() < deadline:
                try:
                    frame, confirm_ctrl, _cancels = self._find_confirm_panel()
                    if frame is None:
                        # 面板已消失（键盘生效后的复查窗口）= 处置成功
                        return True
                    hosts = self._find_chrome_hosts(frame)
                    if not hosts:
                        logger.warning("tdx 确认面板无 chrome host，无法注入键盘")
                        return False
                    for h in hosts:
                        try:
                            h.set_focus()
                            time.sleep(0.25)
                            h.type_keys(key)
                            time.sleep(0.7)
                        except Exception as e:
                            logger.warning("tdx 确认键盘注入失败: %s", e)
                    time.sleep(0.6)
                    frame2, _c2, _cn2 = self._find_confirm_panel()
                    if frame2 is None:
                        logger.info(
                            "tdx 下单确认面板已自动处置（%s）", action)
                        return True
                    continue
                except Exception:
                    logger.exception("tdx 确认面板检测异常")
                time.sleep(0.5)
        return False

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        self._ensure()
        symbol = normalize_symbol(order.stock_code)
        code6 = re.sub(r"\D", "", symbol)[:6]
        vol = int(order.quantity)
        price = float(order.price) if order.price is not None else 0.0
        if len(code6) != 6:
            raise BridgeOrderRejected("invalid stock code %s" % order.stock_code)
        if vol <= 0 or price <= 0:
            raise BridgeOrderRejected("invalid order volume/price %s/%s" % (vol, price))

        direction = "buy" if order.trade_type.value == "BUY" else "sell"
        # 涨跌停前置校验已上移引擎提交链（price_limit_check_enabled 门控，
        # check_price_limit 公共方法，四通道同口径）；此处不再内联校验（防双查）。
        const = _tqconst()
        order_type = (const.STOCK_BUY if direction == "buy" else const.STOCK_SELL)
        # 「下单确认」弹窗基线（order_stock 前的对话框句柄集——弹窗自动点击
        # 只点**新增**的确认框，不误碰客户端其它对话框）
        dialog_before = (self._snapshot_dialog_hwnds()
                         if self._auto_confirm_dialog else set())
        #: 信号行时间匹配基准（客户端与本进程同机同时钟，±45s 内即本次信号）
        sent_at = now_shanghai()
        # 「交易信号」待发送行基线（order_stock **之前**拍）：确认流程只点基线
        # 之后新出现的行。2026-11-05 09:51 实证：新行尚未渲染时旧逻辑按时间
        # 就近误点了 09:51:08 的历史残留行（同代码/数量/价格才没出事）。
        baseline = (self._get_confirmer().snapshot_rows()
                    if self._auto_confirm_dialog else ())
        try:
            res = _call(
                self._tq().order_stock,
                account_id=self._account_id,
                stock_code=symbol,
                order_type=order_type,
                order_volume=vol,
                price_type=self._price_type(price),
                price=price,
                notify=0,
            )
        except TDXConnectionError as e:
            raise BridgeUnavailableError(str(e))
        except Exception as e:
            raise BridgeOrderRejected("tdx place order error: %s" % e)

        if res is None or res == -1:
            raise BridgeOrderRejected("通达信下单失败，返回 %r" % (res,))
        if isinstance(res, dict):
            err_id = str(res.get("ErrorId", ""))
            if err_id not in ("", "0"):
                raise BridgeOrderRejected(
                    "通达信拒单：%s" % (res.get("Error") or res.get("Msg") or res))
            msg = str(res.get("Msg") or "")
        else:
            msg = str(res)

        # 实盘账户 order_stock 恒返回 Value=1（待用户确认），信号落到客户端
        # 「交易信号」窗口：每行「发送」才真正报柜台（2026-11-04 真机实证，
        # 该窗口为 CEF 渲染，无 Win32 按钮可 BM_CLICK，必须 OCR 定位后真实点击）。
        pending = self._pending_confirmation(res, msg)
        ref = ""
        confirm_detail = ""
        clicked = False
        if pending and self._auto_confirm_dialog:
            ref, confirm_detail, clicked = self._confirm_pending_order(
                symbol, direction, price, vol, code6, sent_at,
                dialog_before, msg, baseline)

        if not ref:
            ref = self._resolve_order_id(symbol, direction, price, vol)
        if not ref:
            if pending and not self._auto_confirm_dialog:
                raise BridgeOrderRejected(
                    "通达信下单待人工确认（%s）：自动确认已关闭"
                    "（tq_trade.auto_confirm_dialog=false），请在客户端"
                    "「交易信号」窗口点该行『发送』" % (msg or ""))
            if pending and not clicked:
                raise BridgeOrderRejected(
                    "通达信下单待人工确认，自动确认未成功（%s）。请检查客户端"
                    "「交易信号」窗口是否已打开、本单行是否可点『发送』，"
                    "或关闭下单确认后重试" % (confirm_detail or msg))
            # 柜台已受理（ErrorId=0）但回查窗口（~1.05s）内委托表未见该单：
            # 委托表刷新延迟/连续竞价高峰都会命中。**不能当拒单**——拒单会让引擎
            # 把订单标 rejected 并移出在途集合，真实在途单从此无 ref、无成交回填、
            # 无法撤单，虚拟持仓/现金与柜台脱节（SELL 还会二次卖出）。
            # 已点『发送』但委托表尚未出现同理：点击成功≠委托表立即可见。
            # 返回 None + 标记 last_unconfirmed_order_id：引擎保持 submitted，
            # 后续 poll_deals 继续 legacy 模糊认领，最坏 180s expire 收口。
            self.last_unconfirmed_order_id = self.order_id(order)
            self._last_error = (
                "已受理未认领：委托列表暂未见该单（%s）"
                % (confirm_detail or msg or res))
            logger.warning(
                "tdx order accepted but unclaimed: %s %s x%s @%s (%s)",
                direction, symbol, vol, price, confirm_detail or msg or res,
            )
            return None

        now = now_shanghai()
        self._submit_times[ref] = (now.strftime("%Y%m%d"), now.strftime("%H%M%S"))
        # I-2（终审）：本进程真实下单认领的 ref 不是 fabricated（resolve 回查期间
        # get_orders 可能已把它当陌生 ref 补记过），清除标记让状态机 legacy 匹配正常绑定。
        self._fabricated_refs.discard(ref)

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
            trade_time=order.bar_time or now_shanghai(),
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )

    # ---- 「交易信号」待确认自动处置 ----

    @staticmethod
    def _pending_confirmation(res, msg: str) -> bool:
        """order_stock 是否处于「待用户确认」态。

        官方契约：Value 0=失败 / 1=待用户确认 / 2=成功。Value 缺失时按
        Msg 文案兜底（旧版本/桩实现）。
        """
        value = res.get("Value") if isinstance(res, dict) else None
        try:
            value_i = int(value) if value is not None else None
        except (TypeError, ValueError):
            value_i = None
        if value_i == 2:
            return False
        if value_i == 1:
            return True
        text = msg or ""
        return ("待用户确认" in text) or ("待确认" in text)

    def _get_confirmer(self):
        """惰性构造「交易信号」确认器（基线采集与确认共用同一实例）。"""
        if self._confirmer is None:
            from .live.tdx_signal_confirm import TdxSignalConfirmer
            self._confirmer = TdxSignalConfirmer(timeout=self._confirm_timeout)
        return self._confirmer

    def _confirm_pending_order(self, symbol: str, direction: str, price: float,
                               vol: int, code6: str, sent_at, dialog_before,
                               msg: str, baseline=()):
        """点掉「交易信号」窗口本次委托行的『发送』并等委托表认领。

        返回 (order_ref, 诊断说明, 是否已点击)。order_ref 为空表示自动确认未
        成功；是否已点击用于区分「未发出任何委托」（可安全判拒单）与「已点
        但委托表尚未刷新」（不可判拒单，交由引擎保持 submitted 等认领）。
        ``baseline`` 是 order_stock 之前拍下的旧行指纹（只点之后新出现的行）。
        """
        from .live.tdx_signal_confirm import SignalSpec

        spec = SignalSpec(code6=code6, direction=direction, volume=int(vol),
                          price=float(price), sent_at=sent_at)
        state = {"ref": ""}

        def _submitted() -> bool:
            ref = self._resolve_order_id(symbol, direction, price, vol, attempts=1)
            if ref:
                state["ref"] = ref
            return bool(ref)

        def _on_clicked() -> None:
            # 兼容仍带独立「下单确认」面板的旧形态：点『发送』后可能再弹一次
            try:
                self._handle_legacy_confirm_dialog(dialog_before)
            except Exception:  # noqa: BLE001
                logger.exception("tdx 下单后旧式确认面板处置异常")

        outcome = self._get_confirmer().confirm(
            spec, is_submitted=_submitted, on_clicked=_on_clicked,
            baseline=baseline)
        if not outcome.ok:
            logger.warning("tdx 交易信号自动确认失败：%s（%s）",
                           outcome.detail, msg or "")
        return state["ref"], outcome.detail, int(outcome.clicks) > 0

    def _handle_legacy_confirm_dialog(self, dialog_before) -> None:
        """旧版「下单确认」面板（自绘面板或新增 #32770）兜底处置。"""
        try:
            panel, _btn, _txt = self._find_confirm_panel()
        except Exception:  # noqa: BLE001
            panel = None
        if panel is not None:
            self._confirm_order_dialog(dialog_before, timeout=2.0)
            return
        try:
            from pywinauto import Desktop
            dlg, btn = self._pick_confirm_dialog(
                Desktop(backend="win32").windows(), dialog_before)
        except Exception:  # noqa: BLE001
            return
        if dlg is None or btn is None:
            return
        try:
            btn.click_input()
        except Exception:  # noqa: BLE001
            self._confirm_order_dialog(dialog_before, timeout=2.0)
