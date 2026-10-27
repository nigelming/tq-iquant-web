"""通达信 TQ 交易通道（TdxTqDispatcher）。

参照 OpenCrabTrader ``backend/brokers/tdx_tq.py``，与 Core 内嵌的 TQ 模块用同一条
``tqcenter.tq`` 连接（同进程、同一通达信客户端，见 core/tq/utils.py）。前置条件：
通达信客户端已启动并登录资金账号，且 ``PYPlugins`` 下存在 ``tqcenter.py``。

与 iQuant 桥的差异（决定 Core 侧委托认领路径）：

- 通达信 ``order_stock`` 无法写入备注（remark），委托表只有 ``Wtbh`` / ``Code`` /
  ``BSFlag`` / ``Status`` / ``WtPrice`` / ``WtVol`` / ``CjVol``，**没有委托时间列**。
  因此 ``supports_remark = False``，走 Core 的 legacy 模糊匹配（代码+方向+数量+插入
  时间窗）。为让该匹配成立，``place_order`` 成功认领委托号（Wtbh）后在本进程记录
  提交时间；``query_orders`` 只对**本进程下过的单**回填插入时间，其余历史委托留空
  （空时间解析为 None → 状态机跳过），避免旧单冒领新单。
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

# TQ 委托状态 WTSTATUS -> iQuant m_nOrderStatus（Core 状态机口径）：
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
                 price_mode: str = "my", client_path: str = "", **_kwargs):
        self._account = str(account or "")
        self._account_type = str(account_type or "stock")
        self._price_mode = str(price_mode or "my")
        self._client_path = str(client_path or "")
        self._account_id = -1
        self._connected = False
        self._last_error = ""
        #: order_ref -> (YYYYMMDD, HHMMSS)：本进程下单时记录的柜台插入时间。
        #: 通达信委托表无时间列，仅靠它让 Core 的 legacy 模糊匹配时间窗成立。
        self._submit_times: Dict[str, tuple] = {}

    # ---- 连接 ----

    @property
    def last_error(self) -> str:
        return self._last_error

    @staticmethod
    def order_id(order: OrderEvent) -> str:
        """确定性订单 ID（与 iQuant 桥同一算法），仅用于 Core 侧幂等标识。

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
            try:
                flag = int(r.get("BSFlag", r.get("Type", r.get("BsFlag", 0))) or 0)
            except (TypeError, ValueError):
                flag = 0
            if flag == -1:
                continue  # 撤单流水，不是委托
            try:
                status = int(r.get("Status", r.get("WtStatus", 0)) or 0)
            except (TypeError, ValueError):
                status = 0
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
        """委托表 → Core /orders 形状（source=BRIDGE，remark 恒空）。"""
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
        （TradingCalendar provider 契约）；alert/qmt/ths 均委托本方法。
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
                return float(frame.at[ts, code])
            except Exception:
                return 0.0

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
                bars.append({
                    "stime": stime,
                    "open": _value("Open", code, ts),
                    "high": _value("High", code, ts),
                    "low": _value("Low", code, ts),
                    "close": _value("Close", code, ts),
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
        """
        for i in range(max(attempts, 1)):
            for o in reversed(self.get_orders()):
                if normalize_symbol(o["symbol"]) != normalize_symbol(symbol):
                    continue
                if o["direction"] != direction or int(o["volume"]) != int(volume):
                    continue
                if price > 0 and abs(float(o["price"]) - price) > 1e-6:
                    continue
                return str(o["order_id"])
            if i < attempts - 1:
                time.sleep(0.35)
        return ""

    def _is_limit_order(self, price: float) -> bool:
        """是否限价单：price>0 且 price_mode 映射为自填价（PRICE_MY）。

        市价单（price<=0，或 price_mode 映射 PRICE_SJ/ZTJ/DTJ）的成交价由
        交易所/客户端决定，不做涨跌停前置校验。
        """
        if price is None or price <= 0:
            return False
        return PRICE_MODE_MAP.get(self._price_mode, "PRICE_MY") == "PRICE_MY"

    def _prev_close_for_limit_check(self, symbol: str) -> Optional[Decimal]:
        """取前收盘价（1d 最近 2 根里最早的一根），无有效数据返回 None。

        取最早一根：count=2 时首根即前一交易日；两根日期相同取该值（同值）；
        仅 1 根用其自身。校验自身异常一并吞为 None（宁宽勿错）。
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
            value = float(series.iloc[0])
            if value != value:  # NaN 防御
                return None
            return Decimal(str(value))
        except Exception as e:
            logger.warning("涨跌停校验取前收失败，跳过检查：%s", e)
            return None

    def _check_limit_price(self, symbol: str, direction: str, price: float) -> None:
        """涨跌停前置校验（仅限价单）：买 ≥ 涨停价 / 卖 ≤ 跌停价 → 拒单。

        口径与回测一致（_limit_ratio_for + 前收四舍五入到分）。get_zdt_data
        只有涨跌停状态无价格，不采用。取不到有效前收（无数据 / 前复权早期
        失真 <1 元）跳过；整个校验块 fail-open——校验自身故障绝不阻断下单。
        """
        try:
            prev_close = self._prev_close_for_limit_check(symbol)
            if prev_close is None or prev_close < Decimal("1"):
                return
            ratio = _limit_ratio_for(symbol)
            cent = Decimal("0.01")
            limit_up = (prev_close * (1 + ratio)).quantize(cent, rounding=ROUND_HALF_UP)
            limit_down = (prev_close * (1 - ratio)).quantize(cent, rounding=ROUND_HALF_UP)
            order_price = Decimal(str(price))
            if direction == "buy" and order_price >= limit_up:
                raise BridgeOrderRejected(
                    "委托买价 %s ≥ 涨停价 %s（前收 %s ×%s），已拦截"
                    % (price, limit_up, prev_close, ratio))
            if direction == "sell" and order_price <= limit_down:
                raise BridgeOrderRejected(
                    "委托卖价 %s ≤ 跌停价 %s（前收 %s ×%s），已拦截"
                    % (price, limit_down, prev_close, ratio))
        except BridgeOrderRejected:
            raise
        except Exception as e:
            logger.warning("涨跌停前置校验异常，跳过检查继续下单：%s", e)

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
        if self._is_limit_order(price):
            self._check_limit_price(symbol, direction, price)
        const = _tqconst()
        order_type = (const.STOCK_BUY if direction == "buy" else const.STOCK_SELL)
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

        ref = self._resolve_order_id(symbol, direction, price, vol)
        if not ref:
            if "确认" in msg:
                raise BridgeOrderRejected(
                    "通达信为「下单确认」半自动模式，委托未提交柜台（%s）。"
                    "请在通达信客户端关闭下单确认后重试" % msg)
            raise BridgeOrderRejected(
                "下单未生效（委托列表未见该单）：%s" % (msg or res))

        now = now_shanghai()
        self._submit_times[ref] = (now.strftime("%Y%m%d"), now.strftime("%H%M%S"))

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
