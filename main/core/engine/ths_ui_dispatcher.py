from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    from pywinauto import Application, Desktop
    from pywinauto.keyboard import send_keys
    from pywinauto import timings
    import pyperclip
except Exception:
    Application = None
    Desktop = None
    send_keys = None
    timings = None
    pyperclip = None

try:
    from PIL import ImageGrab
    from PIL import Image
except Exception:
    ImageGrab = None
    Image = None

# 验证码 OCR：同花顺复制表格会弹「先输入验证码」，必须自动识别才能无人值守。
# 用 Windows 内置 OCR（WinRT，winrt-* 包），不依赖 onnxruntime（本机 ddddocr
# 导入即 access violation，无法 try/except 捕获）。延迟导入 + 默认关闭。
_OCR_ENGINE = None
_OCR_FAILED = False

from .event import OrderEvent, TradeEvent
from .execution_engine import OrderDispatcher
from .http_bridge_dispatcher import BridgeOrderRejected, BridgeUnavailableError
from .live.timing import now_shanghai
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)


class ThsUiDispatcher(OrderDispatcher):
    """基于同花顺独立交易客户端（xiadan.exe）UI自动化的下单通道。

    参考 OpenCrabTrader 的 ths_xiadan 实现，使用剪贴板 + pywinauto 驱动。

    与 iQuant HTTP 桥的差异（决定 Core 侧匹配路径）：
    - 同花顺下单对话框不支持写入 userOrderId/Remark，委托表只能拿到「合同编号」，
      无法把 Core 的 bridge_order_id 写进柜台。因此 ``supports_remark = False``，
      OrderStateMachine 改走「代码+方向+数量+插入时间窗」的遗留模糊匹配；
    - 行情/交易日历不来自同花顺，``query_quote``/``query_calendar`` 直接复用
      Core 同进程内嵌的 TQ（通达信）模块，与回测取数口径一致；
    - 委托表的「成交数量/成交均价」是当日累计值，``query_deals`` 据此合成成交行
      （佣金/印花税在委托表不可见，按 0 记，与实盘首期口径一致）。
    """

    channel = "ths_ui"
    #: 柜台不回写备注，Core 不能按 remark 精确认领，只能走 legacy 模糊匹配。
    supports_remark = False
    #: 引擎侧文件交接层挂载门控（通道无关）：file_builtin 模式 True（构造时按
    #: execute_mode 落实例属性），direct 模式 False。LiveEngine 据此挂 FileHandoff。
    file_handoff_enabled = False

    TAB_BUY = "{F1}"
    TAB_SELL = "{F2}"
    TAB_CANCEL = "{F3}"
    TAB_POSITION = "{F4}"
    _SWITCH_WAIT = 2.5
    _COPY_WAIT = 0.6
    _FOCUS_WAIT = 0.6
    GUI_LOCK_TIMEOUT = 30.0
    _GRID_LOCK = threading.RLock()
    _CODE_RE = re.compile(r"^\d{6}$")

    _POS_FIELDS = {
        "symbol": ("证券代码", "代码"),
        "name": ("证券名称", "名称"),
        "volume": ("股票余额", "持仓数量", "数量"),
        "available": ("可用余额", "可卖数量", "可用"),
        "cost": ("成本价", "摊薄成本价", "买入均价"),
        "price": ("市价", "现价", "最新价"),
        "market_value": ("市值", "最新市值"),
    }
    _ORD_FIELDS = {
        "time": ("委托时间", "时间", "申报时间"),
        "symbol": ("证券代码", "代码"),
        "name": ("证券名称", "名称"),
        "bs": ("操作", "买卖标志"),
        "remark": ("备注",),
        "volume": ("委托数量", "数量"),
        "filled": ("成交数量", "已成交数量"),
        "price": ("委托价格", "价格"),
        "trade_price": ("成交均价", "成交价格"),
        "cancelled": ("撤消数量", "撤单数量"),
        "order_id": ("合同编号", "委托编号", "编号"),
        "market": ("交易市场", "市场"),
    }
    _GRID_HEADER_HINTS = ("证券代码", "股票余额", "合同编号", "委托编号", "可用余额", "证券名称")
    #: F4「查询资金股票」页顶部的资金字段标签 → Core account 字段。
    _FUND_LABELS = {
        "资金余额": "balance",
        "冻结金额": "frozen",
        "可用金额": "available",
        "股票市值": "market_value",
        "总资产": "total_asset",
    }
    _NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
    CONFIRM_BUTTONS = ("确定", "确认")
    CANCEL_CONFIRM_BUTTONS = ("是(Y)", "是（Y）", "是")

    def __init__(self, window_title: str = "网上股票交易系统5.0", client_exe_path: str = None,
                 captcha_ocr: bool = False, execute_mode: str = "file_builtin",
                 execute_interval_seconds: float = 5, work_dir: str = "data/ths",
                 session_id: int = 0, **kwargs):
        self._window_title = window_title
        self._client_exe_path = client_exe_path
        self._captcha_ocr = bool(captcha_ocr)
        self._app = None
        self._win = None
        self._uia_win = None
        self._connected = False
        self._last_error = ""
        self._price_map: Dict[str, float] = {}
        # ---- 文件交接层双模式（file_builtin=内置执行器 / direct=同步直呼现状）----
        self.execute_mode = "file_builtin" if str(execute_mode or "") == "file_builtin" else "direct"
        self.file_handoff_enabled = self.execute_mode == "file_builtin"
        self._execute_interval_seconds = max(float(execute_interval_seconds or 5), 0.01)
        self._work_dir = str(work_dir or "data/ths")
        self._session_id = int(session_id or 0)
        # 批次 A 机制（与 TdxTqDispatcher 同口径）：本进程下单 ref 基线 + 提交时间。
        # THS 委托表有「委托时间」列，_submit_times 主要供 recover 补记/对账口径统一。
        self._submit_times: Dict[str, tuple] = {}
        self._known_refs: set = set()
        self._claimed_refs: set = set()
        # file 模式执行器记账：order_id -> {"error","ts","entry"}（诊断 + 引擎
        # execution_receipts 消费源——file 拒单即时可见，不必等 180s expire）。
        self._file_rejects: Dict[str, dict] = {}
        self._rejects_lock = threading.Lock()
        self._last_submit_ref = ""
        self._last_submit_time: tuple = ("", "")
        self._last_enqueued_order_id: Optional[str] = None
        self._handoff = None
        self._executor_thread: Optional[threading.Thread] = None
        self._executor_stop = threading.Event()
        self._executor_lock = threading.Lock()
        if self.execute_mode == "file_builtin":
            from .live.file_handoff import FileHandoff
            self._handoff = FileHandoff(self._work_dir, self._session_id)

    @staticmethod
    def _get_ocr_engine():
        """延迟创建 Windows 内置 OCR 引擎（用户配置语言，缺失时退 zh/en）。"""
        global _OCR_ENGINE, _OCR_FAILED
        if _OCR_ENGINE is not None or _OCR_FAILED:
            return _OCR_ENGINE
        try:
            from winrt.windows.media.ocr import OcrEngine
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                from winrt.windows.globalization import Language
                for tag in ("zh-Hans-CN", "en-US"):
                    engine = OcrEngine.try_create_from_language(Language(tag))
                    if engine is not None:
                        break
            _OCR_ENGINE = engine
        except Exception:
            _OCR_ENGINE = None
            _OCR_FAILED = True
        return _OCR_ENGINE

    @staticmethod
    def _run_coro(coro):
        """同步上下文执行 WinRT 协程（已在事件循环内时改到独立线程）。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _recognize_image(self, engine, img) -> str:
        """把 PIL 图片落临时 PNG，交给 Windows OCR 识别（内存流实测识别为空）。"""
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage import FileAccessMode, StorageFile

        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            img.save(path, "PNG")

            async def _go():
                f = await StorageFile.get_file_from_path_async(path)
                stream = await f.open_async(FileAccessMode.READ)
                decoder = await BitmapDecoder.create_async(stream)
                bitmap = await decoder.get_software_bitmap_async()
                result = await engine.recognize_async(bitmap)
                return str(result.text or "")

            return self._run_coro(_go())
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def order_id(order: OrderEvent) -> str:
        import hashlib
        key = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            order.strategy_id, order.portfolio_id, order.stock_code,
            order.trade_type.value, order.signal_type.value, order.signal_name,
            order.quantity, order.price,
        )
        if order.bar_time is not None:
            key += "|%s" % order.bar_time.isoformat()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_enqueued_order_id(self) -> Optional[str]:
        """file 模式最近一次成功入队的指令 order_id（None=未入队/已重置）。

        LiveEngine 据此把 place_order 的 None 返回识别为「已受理入队」，
        订单保持 submitted 等回填，而不是走 approval failed 拒单。
        """
        return self._last_enqueued_order_id

    def heartbeat(self) -> bool:
        try:
            if self._connected and self._win is not None and self._win.exists():
                return True
            return self.connect()
        except Exception:
            return False

    def close(self) -> None:
        if self._handoff is not None:
            self._executor_stop.set()
            t = self._executor_thread
            if t is not None and t.is_alive():
                t.join(timeout=3)
            self._executor_thread = None
        self._app = None
        self._win = None
        self._uia_win = None
        self._connected = False

    def connect(self) -> bool:
        self._last_error = ""
        if Application is None:
            self._connected = False
            self._last_error = "pywinauto not installed"
            return False
        try:
            if timings is not None:
                try:
                    timings.Timings.fast()
                except Exception:
                    pass
            if self._client_exe_path and os.path.exists(self._client_exe_path):
                try:
                    self._app = Application(backend="win32").connect(path=self._client_exe_path, timeout=5)
                except Exception:
                    self._app = Application(backend="win32").connect(title_re=re.escape(self._window_title))
            else:
                self._app = Application(backend="win32").connect(title_re=re.escape(self._window_title))
            try:
                self._win = self._app.window(title_re=re.escape(self._window_title))
                if not self._win.exists():
                    self._win = self._app.top_window()
            except Exception:
                self._win = self._app.top_window() if self._app else None
            self._connected = self._win is not None and self._win.exists()
            if self._connected and self.execute_mode == "file_builtin":
                self._ensure_executor()
            return self._connected
        except Exception as e:
            self._app = None
            self._win = None
            self._uia_win = None
            self._connected = False
            self._last_error = str(e)
            return False

    def _focus(self) -> None:
        if self._win is not None:
            try:
                self._win.set_focus()
            except Exception:
                pass

    def _key(self, keys: str) -> None:
        self._focus()
        time.sleep(self._FOCUS_WAIT)
        if self._win is not None:
            try:
                self._win.type_keys(keys)
                return
            except Exception:
                pass
        try:
            if send_keys is not None:
                send_keys(keys)
        except Exception:
            pass

    def _switch(self, tab: str) -> None:
        self._key(tab)
        time.sleep(self._SWITCH_WAIT)

    def _select_page(self, name: str) -> bool:
        """用 UIA 左侧树切页（功能键在焦点漂移后不可靠），并聚焦表格。

        F4/F3 仍是兜底；UIA 方式能明确切到「资金股票」「当日委托」，
        避免 F3「撤单」页可能过滤掉已成交委托。切页后必须点一下表格，
        否则 Ctrl+A/C 作用在树上，复制不到数据。
        """
        if Application is None or self._win is None:
            return False
        try:
            if self._uia_win is None:
                app = Application(backend="uia").connect(
                    title_re=re.escape(self._window_title), timeout=5)
                self._uia_win = app.window(
                    title_re=re.escape(self._window_title))
            for item in self._uia_win.descendants(control_type="TreeItem"):
                try:
                    if item.window_text() != name:
                        continue
                    item.select()
                    time.sleep(self._SWITCH_WAIT)
                    grids = self._uia_win.descendants(
                        class_name="CVirtualGridCtrl")
                    if grids:
                        grids[0].click_input()
                        time.sleep(0.4)
                    return True
                except Exception:
                    continue
        except Exception:
            self._uia_win = None
        return False

    def _gui_guard(self) -> bool:
        return self._GRID_LOCK.acquire(timeout=self.GUI_LOCK_TIMEOUT)

    _CLIP_SENTINEL = "__THS_CLIPBOARD_EMPTY__"

    def _copy_grid(self, required=()) -> str:
        """复制当前表格，自动处理复制验证码。

        同花顺在 Ctrl+C 后弹验证码，此时剪贴板仍是上一次内容；必须等验证码
        提交、THS 真正完成复制后再读剪贴板。这里每轮先写哨兵值，避免把旧
        页面的数据误当成本页结果（UIA 切页后尤其容易发生）。
        """
        if pyperclip is None:
            return ""
        if not self._captcha_ocr:
            self._last_error = "验证码 OCR 未启用，无法复制同花顺表格"
            return ""
        if self._get_ocr_engine() is None:
            self._last_error = "验证码 OCR 不可用（Windows OCR 未就绪）"
            return ""
        for _ in range(3):
            try:
                pyperclip.copy(self._CLIP_SENTINEL)
            except Exception:
                pass
            self._key("^a")
            time.sleep(self._COPY_WAIT)
            self._key("^c")
            time.sleep(self._COPY_WAIT)
            try:
                last = str(pyperclip.paste() or "")
            except Exception:
                last = ""
            dlg = self._find_captcha_dialog()
            if dlg is not None:
                try:
                    if not self._solve_captcha(dlg):
                        self._last_error = "验证码弹窗无法自动处理"
                        continue
                except Exception:
                    self._last_error = "验证码处理异常"
                    continue
                time.sleep(self._COPY_WAIT + 0.4)
                try:
                    last = str(pyperclip.paste() or "")
                except Exception:
                    last = ""
            if last == self._CLIP_SENTINEL or not self._looks_like_grid(last):
                time.sleep(0.4)
                continue
            if required and not all(k in last[:600] for k in required):
                self._last_error = "表格页签不匹配: %s" % (required,)
                time.sleep(0.4)
                continue
            return last
        return ""

    def _find_captcha_dialog(self):
        if Desktop is None:
            return None
        try:
            for w in Desktop(backend="win32").windows():
                try:
                    texts = [s.window_text() for s in w.descendants(class_name="Static")]
                    if any("先输入验证码" in t for t in texts):
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _captcha_crop(dlg, edit, dlg_rect):
        """截取验证码图片（编辑框右侧、与之垂直重叠的无文本 Static）。"""
        edit_rect = edit.rectangle()
        for s in dlg.descendants(class_name="Static"):
            try:
                if s.window_text():
                    continue
                r = s.rectangle()
                if r.left >= edit_rect.right and not (
                        r.bottom < edit_rect.top or r.top > edit_rect.bottom):
                    shot = ImageGrab.grab(
                        bbox=(dlg_rect.left, dlg_rect.top, dlg_rect.right, dlg_rect.bottom))
                    return shot.crop((
                        r.left - dlg_rect.left, r.top - dlg_rect.top,
                        r.right - dlg_rect.left, r.bottom - dlg_rect.top,
                    ))
            except Exception:
                continue
        return None

    @staticmethod
    def _dismiss_captcha(dlg) -> bool:
        """关闭验证码弹窗，避免失败后对话框卡住整个交易界面。"""
        if dlg is None:
            return False
        try:
            for b in dlg.descendants(class_name="Button"):
                try:
                    if "取消" in (b.window_text() or ""):
                        b.click()
                        time.sleep(0.4)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            dlg.type_keys("{ESC}")
            time.sleep(0.4)
            return True
        except Exception:
            return False

    def _solve_captcha(self, dlg) -> bool:
        if ImageGrab is None or not self._captcha_ocr:
            self._dismiss_captcha(dlg)
            return False
        if self._get_ocr_engine() is None:
            self._last_error = "验证码 OCR 不可用（Windows OCR 未就绪）"
            self._dismiss_captcha(dlg)
            return False
        try:
            edits = dlg.descendants(class_name="Edit")
            if not edits:
                self._dismiss_captcha(dlg)
                return False
            edit = edits[0]
            try:
                dlg.set_focus()
            except Exception:
                pass
            # 每次输错后验证码会刷新，必须每轮重新截图识别，不能复用旧 code。
            for _attempt in range(3):
                time.sleep(0.6)
                crop = self._captcha_crop(dlg, edit, dlg.rectangle())
                if crop is None:
                    self._dismiss_captcha(dlg)
                    return False
                code = self._ocr_digits(crop)
                if not code:
                    self._last_error = "验证码识别失败"
                    self._dismiss_captcha(dlg)
                    return False
                try:
                    edit.click()
                    time.sleep(0.3)
                    edit.type_keys("^a{DELETE}")
                    edit.type_keys(code)
                except Exception:
                    try:
                        dlg.set_focus()
                        time.sleep(0.5)
                        dlg.type_keys("^a{DELETE}")
                        dlg.type_keys(code)
                    except Exception:
                        pass
                clicked = False
                for b in dlg.descendants(class_name="Button"):
                    t = b.window_text() or ""
                    if "确定" in t or "确认" in t:
                        try:
                            b.click()
                            clicked = True
                        except Exception:
                            pass
                        break
                if not clicked:
                    try:
                        edit.type_keys("{ENTER}")
                    except Exception:
                        pass
                time.sleep(1.2)
                if self._find_captcha_dialog() is None:
                    return True
                try:
                    dlg.set_focus()
                except Exception:
                    pass
            self._dismiss_captcha(dlg)
            return False
        except Exception:
            self._dismiss_captcha(dlg)
            return False

    def _ocr_digits(self, img) -> str:
        """Windows OCR 识别 4 位数字验证码（多尺度 + 多阈值投票）。

        同花顺验证码是蓝底数字带干扰线，直接 OCR 常为空；灰度放大 + 二值化后
        Windows OCR 可稳定读出（实测 3/4/5 倍 + 阈值 170/190/210 多数一致）。
        """
        engine = self._get_ocr_engine()
        if engine is None or Image is None:
            return ""
        try:
            from collections import Counter
            votes: list = []
            for scale in (3, 4, 5):
                src = img.convert("L")
                src = src.resize(
                    (src.width * scale, src.height * scale), Image.LANCZOS)
                for threshold in (170, 190, 210):
                    try:
                        bw = src.point(
                            lambda p, t=threshold: 0 if p < t else 255
                        ).convert("RGB")
                        text = self._recognize_image(engine, bw)
                        digits = "".join(
                            ch for ch in str(text or "") if ch.isdigit())
                        if digits:
                            votes.append(digits[:4])
                    except Exception:
                        continue
            if not votes:
                return ""
            top, n = Counter(votes).most_common(1)[0]
            if n >= 2:
                return top
            return max(votes, key=len)
        except Exception:
            return ""

    def _looks_like_grid(self, text: str) -> bool:
        if not text or "\t" not in text:
            return False
        head = text[:400]
        return any(k in head for k in self._GRID_HEADER_HINTS)

    def _snapshot_hwnds(self) -> set:
        if Desktop is None:
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

    def _new_dialog_with_button(self, before: set, keywords):
        if Desktop is None:
            return None, None
        try:
            for w in Desktop(backend="win32").windows():
                try:
                    if w.class_name() != "#32770" or not w.is_visible():
                        continue
                    if w.handle in before:
                        continue
                    for b in w.descendants(class_name="Button"):
                        t = b.window_text() or ""
                        if any(k in t for k in keywords):
                            return w, b
                except Exception:
                    continue
        except Exception:
            pass
        return None, None

    def _confirm_dialog(self, before: set, keywords) -> bool:
        dlg, btn = self._new_dialog_with_button(before, keywords)
        if dlg is None:
            return True
        try:
            btn.click()
        except Exception:
            pass
        time.sleep(1.2)
        try:
            if not dlg.is_visible():
                return True
        except Exception:
            return True
        try:
            dlg.set_focus()
            time.sleep(0.4)
            dlg.type_keys("{ENTER}")
        except Exception:
            pass
        time.sleep(1.0)
        try:
            return not dlg.is_visible()
        except Exception:
            return True

    def _rows(self, text: str) -> list:
        out = []
        for line in str(text or "").splitlines():
            line = line.rstrip("\r")
            if not line.strip():
                continue
            cells = line.split("\t") if "\t" in line else line.split()
            out.append([c.strip() for c in cells])
        return out

    def _header_map(self, rows: list, fields: dict) -> dict:
        for cells in rows:
            if not any("证券代码" in c or "代码" in c for c in cells):
                continue
            idx = {}
            for field, names in fields.items():
                for i, c in enumerate(cells):
                    if any(n == c or n in c for n in names):
                        idx[field] = i
                        break
            if "symbol" in idx:
                idx["__header__"] = rows.index(cells)
                return idx
        return {}

    def _cell(self, cells: list, idx: dict, field: str) -> str:
        i = idx.get(field)
        if i is None or i >= len(cells):
            return ""
        return cells[i]

    def _f(self, v: str) -> float:
        s = str(v or "").strip().replace(",", "").replace("%", "").replace("￥", "")
        try:
            return float(s)
        except Exception:
            return 0.0

    def _i(self, v: str) -> int:
        try:
            return int(self._f(v))
        except Exception:
            return 0

    @staticmethod
    def _exchange_for(code: str, market: str = "") -> str:
        """6 位代码 + 「交易市场」列 → SH/SZ/BJ。"""
        m = str(market or "").upper()
        if "上海" in m or m in ("SH", "1"):
            return "SH"
        if "深圳" in m or m in ("SZ", "2"):
            return "SZ"
        if "北京" in m or m in ("BJ", "3"):
            return "BJ"
        if code.startswith(("6", "5", "9")):
            return "SH"
        if code.startswith(("4", "8")):
            return "BJ"
        return "SZ"

    @staticmethod
    def _split_time(raw: str):
        """委托时间文本 → (YYYYMMDD, HHMMSS)。仅时间时按上海当天补日期。

        insert_time 必须是 6 位纯数字：Core ``_parse_insert_utc`` 取前 6 位拼
        YYYYMMDDHHMMSS，带冒号会解析成 None，导致 legacy 模糊匹配全部落空。
        """
        text = str(raw or "").strip()
        m = re.search(
            r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})[ T]+(\d{1,2}):?(\d{2}):?(\d{2})",
            text,
        )
        if m:
            y, mo, d, hh, mm, ss = m.groups()
            return y + mo + d, "%02d%02d%02d" % (int(hh), int(mm), int(ss))
        m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", text)
        if m:
            hh, mm, ss = m.groups()
            return now_shanghai().strftime("%Y%m%d"), "%02d%02d%02d" % (
                int(hh), int(mm), int(ss))
        m = re.search(r"(\d{6})$", text)
        if m:
            return now_shanghai().strftime("%Y%m%d"), m.group(1)
        return now_shanghai().strftime("%Y%m%d"), ""

    @staticmethod
    def _order_status_code(note: str, filled: int, volume: int, cancelled: int) -> int:
        """委托表文本/数量 → iQuant m_nOrderStatus 码（Core 状态机口径）。

        56=已成、55=部成（非终态）、54=已撤、53=部撤（终态）、57=废单、50=已报。
        THS 委托表没有独立状态列，只能综合「备注/撤消数量/成交数量」推断。
        """
        text = str(note or "")
        if "废" in text:
            return 57
        if "撤" in text:
            return 53 if filled > 0 else 54
        if cancelled > 0:
            return 53 if filled > 0 else 54
        if filled > 0:
            return 56 if (volume <= 0 or filled >= volume) else 55
        return 50

    def _parse_position_rows(self, text: str) -> list:
        rows = self._rows(text)
        idx = self._header_map(rows, self._POS_FIELDS)
        if not idx:
            return []
        out = []
        for cells in rows[idx["__header__"] + 1:]:
            code = self._cell(cells, idx, "symbol")
            if not self._CODE_RE.match(code):
                continue
            vol = self._i(self._cell(cells, idx, "volume"))
            # 有「可用余额」列时严格采用其值（0 表示 T+1 锁定，不能回退成持仓量）；
            # 仅当该列不存在时才按持仓量放行，与桥离线时的 fail-open 口径一致。
            if idx.get("available") is not None:
                avail = self._i(self._cell(cells, idx, "available"))
            else:
                avail = vol
            out.append({
                "symbol": code,
                "name": self._cell(cells, idx, "name"),
                "volume": vol,
                "available": avail,
                "cost": self._f(self._cell(cells, idx, "cost")),
                "price": self._f(self._cell(cells, idx, "price")),
                "market_value": self._f(self._cell(cells, idx, "market_value")),
            })
        return out

    def _parse_orders(self, text: str) -> list:
        rows = self._rows(text)
        idx = self._header_map(rows, self._ORD_FIELDS)
        if not idx:
            return []
        out = []
        for cells in rows[idx["__header__"] + 1:]:
            code = self._cell(cells, idx, "symbol")
            if not self._CODE_RE.match(code):
                continue
            bs = self._cell(cells, idx, "bs")
            out.append({
                "order_id": self._cell(cells, idx, "order_id"),
                "symbol": code,
                "name": self._cell(cells, idx, "name"),
                "direction": "sell" if "卖" in bs else "buy",
                "price": self._f(self._cell(cells, idx, "price")),
                "trade_price": self._f(self._cell(cells, idx, "trade_price")),
                "volume": self._i(self._cell(cells, idx, "volume")),
                "filled_volume": self._i(self._cell(cells, idx, "filled")),
                "cancelled_volume": self._i(self._cell(cells, idx, "cancelled")),
                "market": self._cell(cells, idx, "market"),
                "note": self._cell(cells, idx, "remark"),
                "time": self._cell(cells, idx, "time"),
            })
        return out

    def _find_order(self, code: str, direction: str, volume: int):
        """下单后回查委托列表；同代码/方向/数量可能有多笔，取最新一行。"""
        try:
            for o in reversed(self.get_orders()):
                if o.get("symbol") != code or o.get("direction") != direction:
                    continue
                if volume and o.get("volume") != volume:
                    continue
                return o
        except Exception:
            pass
        return None

    def get_positions(self) -> list:
        if not self._connected:
            if not self.connect():
                return []
        if not self._gui_guard():
            return []
        try:
            if not self._select_page("资金股票"):
                self._switch(self.TAB_POSITION)
            rows = self._parse_position_rows(
                self._copy_grid(required=("证券代码", "股票余额")))
        finally:
            try:
                self._GRID_LOCK.release()
            except Exception:
                pass
        self._price_map = {r["symbol"]: r["price"] for r in rows if r.get("price", 0) > 0}
        return rows

    def get_orders(self) -> list:
        if not self._connected:
            if not self.connect():
                return []
        if not self._gui_guard():
            return []
        try:
            # 「当日委托」页含全部当日委托（含已成交/已撤）；F3「撤单」页
            # 可能只显示可撤单，缺失已成交单会导致 backfill 漏成交。
            if not self._select_page("当日委托"):
                self._switch(self.TAB_CANCEL)
            return self._parse_orders(
                self._copy_grid(required=("委托时间", "合同编号")))
        finally:
            try:
                self._GRID_LOCK.release()
            except Exception:
                pass

    def _read_fund_values(self) -> dict:
        """读 F4 页静态标签后的数值（标签/数值成对，后出现的面板覆盖前面的）。"""
        if self._win is None:
            return {}
        try:
            texts = [
                (s.window_text() or "").strip()
                for s in self._win.descendants(class_name="Static")
            ]
        except Exception:
            return {}
        out: dict = {}
        i = 0
        while i < len(texts):
            if texts[i] not in self._FUND_LABELS:
                i += 1
                continue
            # 两种版式：顶部「标签组 + 数值组」按位置一一对应；底部「标签/数值」交替。
            labels = []
            j = i
            while j < len(texts) and texts[j] in self._FUND_LABELS:
                labels.append(texts[j])
                j += 1
            values = []
            k = j
            while (k < len(texts) and len(values) < len(labels)
                   and self._NUM_RE.match(texts[k])):
                values.append(self._f(texts[k]))
                k += 1
            if len(values) == len(labels):
                for label, value in zip(labels, values):
                    out[self._FUND_LABELS[label]] = value
                i = j
            else:
                # 组内数值缺失：退化为「下一个数值即本标签值」（交替版式）。
                for nxt in texts[i + 1:i + 10]:
                    if self._NUM_RE.match(nxt):
                        out[self._FUND_LABELS[texts[i]]] = self._f(nxt)
                        break
                i += 1
        return out

    def get_account(self) -> list:
        try:
            positions = self.get_positions()
        except Exception:
            positions = []
        total_mv = sum(float(p.get("market_value") or 0) for p in positions)
        funds = self._read_fund_values()
        available = funds.get("available")
        total_asset = funds.get("total_asset")
        if total_asset is None:
            total_asset = total_mv + (available or 0) + (funds.get("frozen") or 0)
        return [{
            "available": available,
            "total_asset": total_asset,
            "market_value": funds.get("market_value") or total_mv,
            "balance": funds.get("balance"),
            "frozen": funds.get("frozen"),
        }]

    def query_positions(self) -> list:
        try:
            rows = self.get_positions()
        except Exception:
            return []
        out = []
        for r in rows:
            code = str(r.get("symbol") or "")
            if not self._CODE_RE.match(code):
                continue
            exch = self._exchange_for(code)
            avail = r.get("available")
            if avail is None:  # 无可用列才回退持仓量；0 是合法的 T+1 锁定值
                avail = r.get("volume") or 0
            out.append({
                "instrument": code,
                "exchange": exch,
                "volume": int(r.get("volume") or 0),
                "available": int(avail),
                "cost": float(r.get("cost") or 0),
                "price": float(r.get("price") or 0),
                "name": r.get("name") or "",
            })
        return out

    def query_account(self) -> list:
        try:
            acc = self.get_account()
        except Exception:
            return []
        return acc

    def query_orders(self, order_id: Optional[str] = None) -> list:
        """委托表 → Core /orders 形状（source=BRIDGE，remark 恒空）。

        THS 委托表没有可回写的 remark，Core 的 remark 精确认领路径必然落空；
        这里显式返回 remark=""，配合 ``supports_remark=False`` 让状态机走
        代码+方向+数量+插入时间窗的 legacy 模糊匹配。order_ref=合同编号。
        """
        try:
            orders = self.get_orders()
        except Exception:
            return []
        out = []
        for o in orders:
            ref = str(o.get("order_id") or "").strip()
            if not ref:
                continue
            code = str(o.get("symbol") or "")
            if not self._CODE_RE.match(code):
                continue
            volume = int(o.get("volume") or 0)
            filled = int(o.get("filled_volume") or 0)
            cancelled = int(o.get("cancelled_volume") or 0)
            insert_date, insert_time = self._split_time(o.get("time"))
            out.append({
                "source": "BRIDGE",
                "remark": "",
                "order_ref": ref,
                "instrument": code,
                "exchange": self._exchange_for(code, o.get("market")),
                "direction": 49 if o.get("direction") == "sell" else 48,
                "volume": volume,
                "traded_volume": filled,
                "filled_volume": filled,
                "price": float(o.get("price") or 0),
                "insert_date": insert_date,
                "insert_time": insert_time,
                "status": self._order_status_code(
                    o.get("note") or "", filled, volume, cancelled),
            })
        return out

    def query_deals(self, order_id: Optional[str] = None) -> list:
        """由委托表「成交数量/成交均价」合成成交回报（当日累计值）。

        Core 的 backfill_order 按 order_ref 聚合、用累计量覆盖 filled_quantity，
        因此这里直接返回累计成交行即可；分批成交在下一次轮询会自然收敛。
        佣金/印花税不在委托表暴露，按 0 记（与 iQuant 桥首期口径一致）。
        """
        try:
            orders = self.get_orders()
        except Exception:
            return []
        out = []
        for o in orders:
            ref = str(o.get("order_id") or "").strip()
            filled = int(o.get("filled_volume") or 0)
            if not ref or filled <= 0:
                continue
            code = str(o.get("symbol") or "")
            if not self._CODE_RE.match(code):
                continue
            price = float(o.get("trade_price") or 0) or float(o.get("price") or 0)
            if price <= 0:
                continue
            trade_date, trade_time = self._split_time(o.get("time"))
            out.append({
                "order_ref": ref,
                "remark": "",
                "instrument": code,
                "exchange": self._exchange_for(code, o.get("market")),
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
        """权威交易日历经 TdxTqDispatcher（get_trading_dates 优先、日线反推兜底），
        失败 [] 走 fail-open。"""
        from .tdx_tq_dispatcher import TdxTqDispatcher
        return TdxTqDispatcher().query_calendar()

    def query_quote(self, code: str, period: str = "1m", count: int = 10) -> list:
        """行情复用 Core 同进程内嵌的 TQ（通达信）模块。

        同花顺只负责交易执行，不提供行情；BarPoller 需要 stime/OHLCV 形状。
        TQ 返回 {field: DataFrame(index=时间, columns=[code])}，这里转成 bar dict。
        """
        text = str(code or "").strip()
        code6 = re.sub(r"\D", "", text)[:6]
        if len(code6) != 6:
            return []
        tq_code = text if "." in text else "%s.%s" % (
            code6, self._exchange_for(code6))
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(
                [tq_code], [period], count=max(int(count or 10), 2))
        except Exception as e:
            self._last_error = "quote unavailable: %s" % e
            return []
        block = raw.get(period) or {}
        close = block.get("Close")
        if close is None or getattr(close, "empty", True):
            return []
        if tq_code not in getattr(close, "columns", []):
            return []

        def _value(field: str, ts) -> float:
            frame = block.get(field)
            if frame is None:
                return 0.0
            try:
                return float(frame.at[ts, tq_code])
            except Exception:
                return 0.0

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
                "open": _value("Open", ts),
                "high": _value("High", ts),
                "low": _value("Low", ts),
                "close": _value("Close", ts),
                "volume": _value("Volume", ts),
                "amount": _value("Amount", ts),
            })
        return bars

    def check_price_limit(self, stock_code: str, direction: str, price) -> Optional[str]:
        """涨跌停前置校验委托 TdxTqDispatcher（与 query_calendar/query_quote 同模式）。

        引擎按 price_limit_check_enabled 配置门控调用；返回拒绝原因文案，None=通过。
        """
        from .tdx_tq_dispatcher import TdxTqDispatcher
        return TdxTqDispatcher().check_price_limit(stock_code, direction, price)

    # ---------------- 下单（双模式） ----------------

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        """file_builtin：指令写入交接文件秒回 None（内置执行器异步执行）；
        direct：同步执行 UI 自动化（现行为）。"""
        if self.execute_mode == "file_builtin":
            return self._place_order_via_file(order)
        return self._execute_via_ui(order)

    def _place_order_via_file(self, order: OrderEvent) -> Optional[TradeEvent]:
        """file 模式 place_order：只做参数校验 + 指令入队，秒回 None。

        不做 captcha/OCR 前置拒单——OCR 守卫在 _execute_via_ui（执行器）里判，
        失败经 mark_done 落 rejected 回执。place_order 返回 None 时 LiveEngine
        按 last_enqueued_order_id 识别「已入队」，订单保持 submitted 等回填。
        """
        code = re.sub(r"\D", "", str(order.stock_code))[:6]
        if not self._CODE_RE.match(code):
            raise BridgeOrderRejected(f"invalid code {order.stock_code}")
        vol = int(order.quantity)
        price = float(order.price) if order.price is not None else 0.0
        if vol <= 0 or price <= 0:
            raise BridgeOrderRejected(f"invalid order vol/price {vol}/{price}")
        self._last_enqueued_order_id = None
        oid = self.order_id(order)
        entry = {
            "order_id": oid,
            "stock_code": order.stock_code,
            "code": code,
            "direction": order.trade_type.value,
            "quantity": vol,
            "price": price,
            "strategy_id": order.strategy_id,
            "portfolio_id": order.portfolio_id,
            "signal_type": order.signal_type.value if order.signal_type else None,
            "signal_name": order.signal_name,
            "bar_time": order.bar_time.isoformat() if order.bar_time else None,
        }
        self._handoff.append_order(entry)
        self._last_enqueued_order_id = oid
        self._ensure_executor()
        return None

    @staticmethod
    def _entry_to_order(entry: dict) -> OrderEvent:
        """orders.jsonl 指令行 → OrderEvent（执行器重建用）。"""
        raw_bt = entry.get("bar_time")
        bar_time = None
        if raw_bt:
            try:
                bar_time = datetime.fromisoformat(str(raw_bt))
            except ValueError:
                bar_time = None
        price = entry.get("price")
        return OrderEvent(
            strategy_id=int(entry.get("strategy_id") or 0),
            portfolio_id=int(entry.get("portfolio_id") or 0),
            stock_code=str(entry.get("stock_code") or ""),
            trade_type=TradeType(str(entry.get("direction") or "BUY").upper()),
            signal_type=SignalType(str(entry.get("signal_type") or "OPEN")),
            quantity=int(entry.get("quantity") or 0),
            price=Decimal(str(price)) if price is not None else None,
            bar_time=bar_time,
            signal_name=str(entry.get("signal_name") or ""),
        )

    def _ensure_executor(self) -> None:
        """惰性启动内置执行线程（daemon）：首次 place_order 或 connect 成功时。"""
        if self.execute_mode != "file_builtin" or self._handoff is None:
            return
        with self._executor_lock:
            t = self._executor_thread
            if t is not None and t.is_alive():
                return
            self._executor_stop.clear()
            t = threading.Thread(
                target=self._executor_loop, name="ths-file-executor", daemon=True)
            self._executor_thread = t
            t.start()

    def _executor_loop(self) -> None:
        """内置执行器节拍：每 execute_interval_seconds 消费一轮待执行指令。

        UI 异常不杀线程（_execute_entry 全包）；读文件失败记 warning 下轮再试。
        """
        while not self._executor_stop.wait(self._execute_interval_seconds):
            try:
                pending = self._handoff.read_pending_orders()
            except Exception as e:  # noqa: BLE001
                logger.warning("ths file executor read pending failed: %s", e)
                continue
            for entry in pending:
                if self._executor_stop.is_set():
                    return
                self._execute_entry(entry)

    def _execute_entry(self, entry: dict) -> None:
        """执行单条指令：成功 accepted（ref 记 _known_refs/_submit_times，批次 A
        口径）/失败 rejected（_file_rejects + results 回执），均不抛出。"""
        order_id = str(entry.get("order_id") or "")
        try:
            order = self._entry_to_order(entry)
            self._execute_via_ui(order)
            ref = str(self._last_submit_ref or "")
            if ref:
                self._known_refs.add(ref)
                if ref not in self._submit_times:
                    self._submit_times[ref] = self._last_submit_time or (
                        now_shanghai().strftime("%Y%m%d"),
                        now_shanghai().strftime("%H%M%S"),
                    )
            self._handoff.mark_done(order_id, {"status": "accepted", "ref": ref})
        except (BridgeOrderRejected, BridgeUnavailableError) as e:
            self._record_file_reject(order_id, entry, e)
            try:
                self._handoff.mark_done(order_id, {"status": "rejected", "error": str(e)})
            except Exception:  # noqa: BLE001
                logger.warning("ths handoff mark_done failed", exc_info=True)
        except Exception as e:  # noqa: BLE001  UI 异常不杀执行线程
            logger.warning("ths file executor error (order %s): %s", order_id, e)
            self._record_file_reject(order_id, entry, e)
            try:
                self._handoff.mark_done(order_id, {"status": "rejected", "error": str(e)})
            except Exception:  # noqa: BLE001
                logger.warning("ths handoff mark_done failed", exc_info=True)

    def _record_file_reject(self, order_id: str, entry: dict, exc: Exception) -> None:
        """执行失败记账（带原始指令 entry 供引擎形状匹配）。"""
        with self._rejects_lock:
            self._file_rejects[str(order_id)] = {
                "error": str(exc),
                "ts": now_shanghai().isoformat(timespec="seconds"),
                "entry": dict(entry or {}),
            }

    def execution_receipts(self) -> List[dict]:
        """弹出执行失败回执（file_builtin 模式；单消费者=LiveEngine deals 轮询）。

        引擎在记账锁内消费：按 order_id/entry 形状匹配在途单即时标 rejected
        （工作台立即可见），不必等 180s expire。direct 模式无执行回执（恒 []）。
        """
        if self.execute_mode != "file_builtin":
            return []
        with self._rejects_lock:
            if not self._file_rejects:
                return []
            items = list(self._file_rejects.items())
            self._file_rejects.clear()
        return [
            {
                "order_id": oid,
                "status": "rejected",
                "error": str(rec.get("error") or ""),
                "ts": rec.get("ts"),
                "entry": dict(rec.get("entry") or {}),
            }
            for oid, rec in items
        ]

    def _execute_via_ui(self, order: OrderEvent) -> TradeEvent:
        """同步 UI 自动化下单主体（direct 模式 place_order 与内置执行器共用）。

        connect 守卫 / 验证码 OCR 守卫 / _gui_guard / F1F2 键鼠 / 确认框 /
        _find_order 回查 / 合成 TradeEvent。命中合同编号时补记
        _last_submit_ref / _last_submit_time 供执行器记账。
        """
        self._last_submit_ref = ""
        self._last_submit_time = ("", "")
        if not self._connected:
            if not self.connect():
                raise BridgeUnavailableError("ths ui not connected")
        code = re.sub(r"\D", "", str(order.stock_code))[:6]
        if not self._CODE_RE.match(code):
            raise BridgeOrderRejected(f"invalid code {order.stock_code}")
        vol = int(order.quantity)
        price = float(order.price) if order.price is not None else 0.0
        if vol <= 0 or price <= 0:
            raise BridgeOrderRejected(f"invalid order vol/price {vol}/{price}")
        if not self._captcha_ocr:
            # 下单后必须复制委托表回查合同编号；THS 复制会弹验证码，
            # OCR 未启用时宁可拒单，也不能先发单再因查不到而误报失败。
            raise BridgeUnavailableError(
                "ths ui captcha_ocr disabled: cannot verify order after submit")
        if self._get_ocr_engine() is None:
            raise BridgeUnavailableError(
                "ths ui OCR unavailable: cannot verify order after submit")
        if not self._gui_guard():
            raise BridgeUnavailableError("ths ui busy")
        try:
            self._switch(self.TAB_BUY if order.trade_type.value == "BUY" else self.TAB_SELL)
            self._key("^a")
            self._key("{DELETE}")
            self._key(code)
            time.sleep(self._SWITCH_WAIT)
            # 字段顺序 [代码 → 委托价格 → 委托数量]：显式输入委托价格（限价语义，
            # 与回测审批价一致）——此前连按两个 TAB 跳过价格框，真实委托按对话框
            # 默认价（最新价）成交，限价/涨跌停前置校验全部失真（准确性缺口）。
            self._key("{TAB}")
            self._key("^a")
            self._key("{DELETE}")
            self._key(str(price))
            time.sleep(0.3)
            self._key("{TAB}")
            self._key("^a")
            self._key("{DELETE}")
            self._key(str(vol))
            time.sleep(0.3)
            before = self._snapshot_hwnds()
            self._key("{ENTER}")
            time.sleep(1.5)
            confirmed = self._confirm_dialog(before, self.CONFIRM_BUTTONS)
        except BridgeOrderRejected:
            raise
        except BridgeUnavailableError:
            raise
        except Exception as e:
            raise BridgeOrderRejected(f"ths ui place order error: {e}")
        finally:
            try:
                self._GRID_LOCK.release()
            except Exception:
                pass
        if not confirmed:
            raise BridgeOrderRejected("order confirm dialog not confirmed")
        found = self._find_order(code, order.trade_type.value.lower(), vol)
        if found is None:
            raise BridgeOrderRejected("order not found in order list after submit")
        ref = str(found.get("order_id") or "")
        if ref:
            self._last_submit_ref = ref
            self._last_submit_time = self._split_time(found.get("time"))
        fill_price = order.price if order.price is not None else Decimal(str(price))
        amount = fill_price * vol
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=fill_price,
            quantity=vol,
            amount=amount,
            commission=Decimal("0"),
            stamp_duty=Decimal("0"),
            trade_time=order.bar_time or datetime.now(),
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )
