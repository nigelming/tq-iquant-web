"""通达信 TQ 通道「交易信号」下单确认自动化（2026-11-04 真机重写）。

事实依据（2026-11-04 盘中实拍 + 客户端窗口树/OCR 离线验证）：

- 实盘账户 ``order_stock`` 恒返回 ``Value=1`` /「已发送信号至客户端，待用户确认！」。
  信号落到客户端「交易信号」窗口，**每行**两个按钮「发送」「取消」，底部
  「全部发送」「全部取消」——**没有**模态「下单确认」框（上一版按「确定 +
  仅供参考」找 #32770/自绘面板，永远匹配不到）。
- 该窗口是 CEF/Chromium 渲染（子窗口链
  ``SubWebView → CefBrowserWindow → Chrome_WidgetWin_0 →
  Chrome_RenderWidgetHostHWND``）：Win32 控件枚举只能看到 CEF host，
  UIA descendants=0，网页里的按钮**没有控件句柄**，BM_CLICK/UIA 无从下手。
- 因此可靠路径只有「窗口级截图 → OCR 定位『发送』→ 真实鼠标点击」。
  真机截图离线验证：对窗口做 4x 放大 + 反色 + 阈值二值化后，Windows OCR
  能稳定读出 002056/600888、300/900、卖出、发送、取消 与各自坐标
  （原始深色主题直接 OCR 会把按钮读成乱码）。

自动化策略（按可靠性排序）：

1. ``EnumWindows`` + 映像名 ``tdxw.exe`` + 标题含「交易信号」+ 含 CEF 子窗口
   —— 不依赖窗口位置/DPI；多实例（客户端会残留隐藏旧窗）取 **可见且最顶层**。
2. ``PrintWindow(PW_RENDERFULLCONTENT)`` 截窗口自身内容（被遮挡也能截），
   失败退回 BitBlt；截图失败才退回屏幕抓取。
3. OCR 行匹配：代码（OCR 混淆归一 S→5/O→0）+ 数量 + 方向 + 时间（±45s，
   防同代码同量的历史残留待发送行被误点——误点会真的报单）。
4. 点击：若该点真有 ``Button`` 型 HWND → BM_CLICK；否则校验点击点未被其它
   窗口遮挡（``WindowFromPoint``）后，用真实鼠标点击（CEF 唯一可靠方式）。
   点击前先复查行仍匹配、坐标落在窗口内；点击后复查该行「发送」是否消失。
5. 单次调用**最多点一次**（风控单宁可判失败等下一根 bar 重发，也不冒重复
   报单风险）；失败落盘截图 + 窗口树 + OCR 文本到 ``logs/tdx_confirm/``。
6. 全程受 deadline 约束，不会阻塞引擎主循环无限等待。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: OCR 放大倍数（4x 可读小字按钮；2x 会把「发送」读成乱码）
_OCR_SCALE = 4
#: 反色后的二值化阈值（真机截图校准）
_OCR_THRESHOLD = 140
#: 同一行的 y 向容差（窗口坐标，px）
_ROW_TOL = 10.0
#: 订单发出时间与信号行时间的允许偏差（秒）。引擎按 bar 节奏下单（~60s/次），
#: 取 45s 以排除上一根 bar 的残留同形状行，又容忍客户端时钟/OCR 抖动。
_TIME_TOL_SECONDS = 45

WM_MOUSEWHEEL = 0x020A
BM_CLICK = 0x00F5
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
GA_ROOT = 2

#: OCR 常见数字混淆（仅用于纯数字/近数字文本）
_DIGIT_CONFUSION = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "S": "5", "s": "5", "I": "1", "l": "1", "|": "1",
    "B": "8", "Z": "2", "G": "6", "T": "7",
})
_TIME_RE = re.compile(r"([0-2]?\d)[:：](\d{2})[:：](\d{2})")
#: 价格列（1~3 位小数；小数点可能是「.」「．」或 OCR 出的「·」；两侧不得紧邻
#: 数字，避免把「17.64300」误读为 17.643）
_PRICE_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[.．·]\s*(\d{1,3})(?!\d)")
#: 价格匹配容差（1 分钱）。限价对限价；读不出行价格一律不放行（宁失败不错）。
_PRICE_TOL = 0.011
#: 行时间下限相对本次下单时间的容差（秒）：基线不可用时的退化规则，
#: 只接受「下单之后」出现的行，绝不点更早的历史残留行。
_TIME_FLOOR_TOL_SECONDS = 2

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2（Win10 1607+）
_DPI_PER_MONITOR_V2 = -4


def _dpi_ctx():
    """线程级 DPI 感知上下文（不改进程，避免影响同进程 THS 自动化）。

    Core 进程默认 DPI-unaware：在非 100% 缩放下 ``GetWindowRect`` 返回 96DPI
    「逻辑坐标」，而 ``SetCursorPos`` / ``WindowFromPoint`` 用物理像素——两者
    混用会整体偏移，点不中按钮。进入本上下文后本线程所有窗口/鼠标 API 统一为
    物理像素。系统不支持（Win7/8）或设置失败 → 空上下文，按原感知工作。
    """

    class _Null:
        def __enter__(self):
            return None

        def __exit__(self, *_a):
            return False

    try:
        setter = _user32.SetThreadDpiAwarenessContext
    except AttributeError:
        return _Null()
    setter.restype = ctypes.c_void_p
    setter.argtypes = [ctypes.c_void_p]
    try:
        prev = setter(ctypes.c_void_p(_DPI_PER_MONITOR_V2))
    except Exception:  # noqa: BLE001
        return _Null()
    if not prev:
        return _Null()

    class _Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *_a):
            try:
                setter(ctypes.c_void_p(prev))
            except Exception:  # noqa: BLE001
                pass
            return False

    return _Ctx()


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class SignalSpec:
    """本次下单在客户端「交易信号」窗口里的识别特征。"""

    code6: str
    direction: str          # buy / sell
    volume: int
    price: float = 0.0
    sent_at: Optional[datetime] = None

    @property
    def seconds(self) -> Optional[int]:
        if self.sent_at is None:
            return None
        return (self.sent_at.hour * 3600 + self.sent_at.minute * 60
                + self.sent_at.second)


@dataclass
class WindowRef:
    hwnd: int
    title: str
    rect: Tuple[int, int, int, int]   # left, top, right, bottom
    visible: bool
    has_cef: bool = False

    @property
    def width(self) -> int:
        return max(self.rect[2] - self.rect[0], 0)

    @property
    def height(self) -> int:
        return max(self.rect[3] - self.rect[1], 0)


@dataclass
class Cell:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w


@dataclass
class Row:
    cells: List[Cell] = field(default_factory=list)

    @property
    def cy(self) -> float:
        if not self.cells:
            return 0.0
        return sum(c.cy for c in self.cells) / len(self.cells)

    @property
    def code(self) -> str:
        for c in self.cells:
            if ":" in c.text or "：" in c.text:
                continue
            n = _norm_digits(c.text)
            if len(n) == 6 and n.isdigit():
                return n
        return ""

    @property
    def volume(self) -> str:
        """数量列：行内最右侧的数字词（时间/账号/代码之后的数字）。"""
        out = ""
        for c in sorted(self.cells, key=lambda x: x.x):
            if ":" in c.text or "：" in c.text:
                continue
            n = _norm_digits(c.text)
            if n.isdigit() and 1 <= len(n) <= 6:
                out = n
        return out

    @property
    def direction(self) -> str:
        for c in self.cells:
            if "卖" in c.text:
                return "sell"
            if "买" in c.text:
                return "buy"
        return ""

    @property
    def seconds(self) -> Optional[int]:
        """行内时间（OCR 常把 14:43:09 拆成 5 个词，按 x 拼接后取正则）。"""
        text = "".join(c.text for c in sorted(self.cells, key=lambda x: x.x))
        m = _TIME_RE.search(text)
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))

    @property
    def price(self) -> Optional[float]:
        """行内委托价（限价）。读不出/市价行返回 None（调用方按「不可判读」处理）。

        OCR 常把 17.64 拆成「1」「7」「·」「64」多个词，必须按 x 邻近拼接后
        再取正则（``_row_text`` 只在列间距大时才插空格）。
        """
        text = _row_text(self)
        m = _PRICE_RE.search(text)
        if not m:
            return None
        return float("%s.%s" % (m.group(1), m.group(2)))

    @property
    def sent(self) -> bool:
        """行尾按钮是否已变为「已发送」（点击成功后客户端会改按钮文案）。"""
        return any("已发送" in c.text.replace(" ", "") for c in self.cells)

    @property
    def send(self) -> Optional[Cell]:
        """本行「发送」按钮单元格（「发」「送」常被 OCR 拆成两个词）。"""
        ws = sorted(self.cells, key=lambda c: c.x)
        for i, w in enumerate(ws):
            t = w.text.replace(" ", "")
            if "发送" in t:
                return w
            if t == "发" and i + 1 < len(ws):
                nxt = ws[i + 1]
                nt = nxt.text.replace(" ", "")
                if nt.startswith("送") and nxt.x - w.right <= 4 and \
                        abs(nxt.cy - w.cy) <= 3:
                    return Cell(
                        text="发送", x=w.x, y=min(w.y, nxt.y),
                        w=max(nxt.right, w.right) - w.x,
                        h=max(w.h, nxt.h),
                    )
        return None


@dataclass
class ConfirmOutcome:
    ok: bool
    detail: str
    clicks: int = 0
    method: str = ""
    stale: int = 0


class _WindowEnumProc:
    """EnumWindows 回调保持引用（防 GC）。"""

    def __init__(self):
        self.rows: List[Tuple[int, str, int, bool]] = []
        self._proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(self._cb)

    def _cb(self, hwnd, _lparam):
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = ""
        try:
            from .win_dialog_scope import window_image_name
            image = window_image_name(pid.value)
        except Exception:  # noqa: BLE001
            image = ""
        if image != TdxSignalConfirmer.PROCESS_IMAGE:
            return True
        self.rows.append((
            int(hwnd), _window_text(int(hwnd)), int(pid.value),
            bool(_user32.IsWindowVisible(hwnd)),
        ))
        return True

    def run(self) -> List[Tuple[int, str, int, bool]]:
        _user32.EnumWindows(self._proc, 0)
        return self.rows


# ---------------------------------------------------------------- 纯逻辑辅助


def _norm_digits(text: str) -> str:
    """OCR 数字混淆归一：0020S6 → 002056；仅保留数字。"""
    s = (text or "").translate(_DIGIT_CONFUSION)
    return re.sub(r"\D", "", s)


def _seconds_diff(a: int, b: int) -> int:
    d = abs(a - b)
    return min(d, 86400 - d)


def _window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wt.HWND(hwnd), buf, 512)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wt.HWND(hwnd), buf, 256)
    return buf.value


def _window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    r = wt.RECT()
    _user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def _children(hwnd: int) -> List[int]:
    out: List[int] = []
    child = _user32.GetWindow(wt.HWND(hwnd), 5)   # GW_CHILD
    while child:
        out.append(int(child))
        child = _user32.GetWindow(wt.HWND(child), 2)  # GW_HWNDNEXT
    return out


def _walk(hwnd: int, depth: int, max_depth: int, out: List[tuple]) -> None:
    if depth > max_depth:
        return
    rect = _window_rect(hwnd)
    out.append((
        depth, hwnd, _window_class(hwnd), _window_text(hwnd), rect,
        bool(_user32.IsWindowVisible(wt.HWND(hwnd)), ),
        _user32.GetDlgCtrlID(wt.HWND(hwnd)),
    ))
    for c in _children(hwnd):
        _walk(c, depth + 1, max_depth, out)


def _has_cef_child(hwnd: int) -> bool:
    """窗口内是否含 CEF 渲染窗口（判断「这是网页版交易界面」）。"""
    targets = ("Chrome_RenderWidgetHostHWND", "CefBrowserWindow",
               "Chrome_WidgetWin_0", "Chrome_WidgetWin_1")
    stack = [(hwnd, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > 6:
            continue
        for c in _children(cur):
            if _window_class(c) in targets:
                return True
            stack.append((c, depth + 1))
    return False


def _pick_window_impl(items: Sequence[WindowRef]) -> Optional[WindowRef]:
    """从候选中挑本次要操作的「交易信号」窗口。

    优先 可见+CEF，其次 可见，再次 CEF；同档取 **枚举顺序最靠前**（EnumWindows
    自上而下 = 最顶层）。全部隐藏也返回唯一 CEF 候选（调用方可强制显示）。
    """
    if not items:
        return None
    for pred in (
        lambda w: w.visible and w.has_cef,
        lambda w: w.visible,
        lambda w: w.has_cef,
    ):
        for w in items:
            if pred(w):
                return w
    return None


def _build_rows(words: Iterable[tuple]) -> List[Row]:
    """把 OCR 词（text, x, y, w, h；窗口坐标）按 y 聚成行。

    单元格不在这里合并——列与列之间可能只差几个像素（数量「300」与按钮
    「发」实测会重叠），贪心合并会把不同列粘成「300发送」。时间/按钮等需要
    合并的语义改由 ``Row.seconds`` / ``Row.send`` 按 x 邻近关系精确处理。
    """
    items = [Cell(str(t), float(x), float(y), float(w), float(h))
             for (t, x, y, w, h) in words if t]
    items.sort(key=lambda c: c.cy)
    rows: List[Row] = []
    for cell in items:
        for row in rows:
            if abs(row.cy - cell.cy) <= _ROW_TOL:
                row.cells.append(cell)
                break
        else:
            rows.append(Row(cells=[cell]))
    for row in rows:
        row.cells.sort(key=lambda c: c.x)
    return rows


def _row_text(row: Row, gap_px: float = 8.0) -> str:
    """按 x 顺序拼接行内文本。

    同一语义词常被 OCR 拆成多个碎片（如价格「1」「7」「·」「64」），相邻
    单元格间距 ≤ gap_px 时直接相连；列间距（数量/按钮）更大，插空格分隔，
    避免「17.64300」这类跨列粘连。
    """
    out: List[str] = []
    prev: Optional[Cell] = None
    for c in sorted(row.cells, key=lambda x: x.x):
        if prev is not None and c.x - prev.right > gap_px:
            out.append(" ")
        out.append(c.text.replace(" ", ""))
        prev = c
    return "".join(out)


def _row_fingerprint(row: Row) -> tuple:
    """行指纹（基线扣除用）：代码/方向/数量/价格/行时间 全同视为同一旧行。

    时间精确到秒、价格精确到分；基线在 order_stock 之前拍，新行时间必然
    不同，因此不会把本次新行误当旧行排除。
    """
    price = row.price
    return (row.code, row.direction, row.volume,
            round(price, 2) if price is not None else None, row.seconds)


def _subtract_baseline(rows: Sequence[Row],
                       baseline: Sequence[tuple]) -> List[Row]:
    """扣除基线中已存在的旧行（多重集扣除，同指纹多行逐条抵消）。"""
    if not baseline:
        return list(rows)
    left = Counter(baseline)
    kept: List[Row] = []
    for row in rows:
        fp = _row_fingerprint(row)
        if left.get(fp, 0) > 0:
            left[fp] -= 1
            continue
        kept.append(row)
    return kept


def _match_row(rows: Sequence[Row], spec: SignalSpec,
               tol_seconds: int = _TIME_TOL_SECONDS,
               baseline: Optional[Sequence[tuple]] = None) -> Optional[Row]:
    """定位「本次委托」对应的待发送行（找不到 → None，宁可不点也不误点）。

    硬条件：代码 6 位一致 + 数量一致 + 方向一致（读不出方向则放行）+
    **价格一致**（限价对限价，±1 分钱；读不出价格一律不放行）。
    基线条件：``baseline``（下单前已存在的行指纹）里的行全部排除——只点
    基线之后新出现的行，哪怕旧行时间更接近本次下单。
    时间条件：行时间不得早于本次下单前 ``_TIME_FLOOR_TOL_SECONDS`` 秒，且
    与下单时间差 ≤ tol_seconds；**没有任何可读时间**时，只有唯一候选才放行。
    多行命中取时间最接近、其次最下方（最新）。
    """
    if not spec.code6:
        return None
    vol = str(int(spec.volume))
    want_price = float(spec.price or 0.0)
    floor = (spec.seconds - _TIME_FLOOR_TOL_SECONDS
             if spec.seconds is not None else None)
    cands: List[Tuple[float, float, Row]] = []
    for row in _subtract_baseline(rows, baseline or ()):
        if row.code != spec.code6:
            continue
        if row.volume != vol:
            continue
        if row.direction and row.direction != spec.direction:
            continue
        if row.send is None:
            continue
        if want_price > 0:
            row_price = row.price
            if row_price is None or abs(row_price - want_price) > _PRICE_TOL:
                continue
        t = row.seconds
        if t is None:
            if spec.seconds is not None:
                continue
        else:
            if floor is not None and t < floor:
                continue
            if spec.seconds is not None and \
                    _seconds_diff(t, spec.seconds) > tol_seconds:
                continue
        delta = (_seconds_diff(t, spec.seconds)
                 if (t is not None and spec.seconds is not None) else 0)
        cands.append((delta, row.cy, row))
    if not cands:
        return None
    if spec.seconds is None:
        # 无时间基准：仅唯一候选放行（历史残留行可能真的报单）
        return cands[0][2] if len(cands) == 1 else None
    return min(cands, key=lambda c: (c[0], -c[1]))[2]


def _image_has_content(img) -> bool:
    """截图是否含可读内容（全黑/全白 = CEF 硬件渲染没被 PrintWindow 捕获）。"""
    try:
        lo, hi = img.convert("L").resize((64, 64)).getextrema()
        return (hi - lo) >= 25
    except Exception:  # noqa: BLE001
        return True


def _screen_grab(window: WindowRef):
    """按窗口屏幕矩形抓屏（PrintWindow 空白时的兜底，窗口可见才有效）。"""
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    try:
        if not _user32.IsWindowVisible(wt.HWND(window.hwnd)):
            return None
        img = ImageGrab.grab(bbox=window.rect, all_screens=True)
        return img.convert("RGB")
    except Exception:  # noqa: BLE001
        logger.exception("tdx 交易信号窗口屏幕截图失败")
        return None


def _default_dump_dir() -> Path:
    root = Path(__file__).resolve().parents[4]   # repo root / main/core/engine/live
    candidates = [root / "logs" / "tdx_confirm", Path(tempfile.gettempdir()) / "tdx_confirm"]
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:  # noqa: BLE001
            continue
    return Path(tempfile.gettempdir()) / "tdx_confirm"


# ---------------------------------------------------------------- 主类


class TdxSignalConfirmer:
    """定位「交易信号」窗口 → OCR 匹配本次委托行 → 点『发送』→ 等委托表出现。"""

    PROCESS_IMAGE = "tdxw.exe"
    TITLE_KEYWORD = "交易信号"

    def __init__(self, timeout: float = 12.0, dump_dir: Optional[str] = None,
                 enable_ocr: bool = True):
        self.timeout = float(timeout)
        self.dump_dir = Path(dump_dir) if dump_dir else _default_dump_dir()
        self.enable_ocr = bool(enable_ocr)
        self._ocr_engine = None
        self._last_img = None

    # ---- 公开入口

    def confirm(self, spec: SignalSpec,
                is_submitted: Optional[Callable[[], bool]] = None,
                deadline: Optional[float] = None,
                on_clicked: Optional[Callable[[], None]] = None,
                baseline: Optional[Sequence[tuple]] = None) -> ConfirmOutcome:
        """定位并点击本次委托对应的「发送」，然后等 ``is_submitted`` 为真。

        ``baseline`` 是 ``order_stock`` **之前**由 ``snapshot_rows()`` 拍下的
        旧行指纹；传 None 时在入口现拍（独立调用路径）。基线里的行一律不点，
        只点基线之后新出现的行。返回 ok=True 仅当委托表已见该单（由调用方回调
        确认）；点击本身不作为成功依据。任何异常都收敛为失败结果，不向引擎抛。
        """
        with _dpi_ctx():   # 窗口坐标与鼠标坐标统一到物理像素（见 _dpi_ctx）
            return self._confirm_impl(spec, is_submitted, deadline, on_clicked,
                                      baseline)

    def _confirm_impl(self, spec: SignalSpec,
                      is_submitted: Optional[Callable[[], bool]] = None,
                      deadline: Optional[float] = None,
                      on_clicked: Optional[Callable[[], None]] = None,
                      baseline: Optional[Sequence[tuple]] = None) -> ConfirmOutcome:
        start = time.time()
        end = float(deadline) if deadline else start + self.timeout
        locate_end = min(end, start + self.timeout * 0.65)
        detail = "未找到「交易信号」窗口"
        window: Optional[WindowRef] = None
        clicked = 0
        clicked_row: Optional[Row] = None
        stale = 0
        if baseline is None:
            baseline = self._snapshot_rows_impl()
        base_n = len(baseline or ())

        while time.time() < locate_end:
            window = self._pick_window()
            if window is None:
                detail = ("未找到标题含「交易信号」的 tdxw.exe 窗口"
                          "（客户端未打开交易信号窗口？）")
                time.sleep(0.35)
                continue
            if not window.visible:
                self._force_show(window)
                time.sleep(0.2)
            rows = self._read_rows(window)
            if not rows:
                detail = "「交易信号」窗口截图/OCR 无有效文本（渲染未完成或被遮挡）"
                time.sleep(0.4)
                continue
            stale = self._count_matching(rows, spec)
            hit = _match_row(rows, spec, baseline=baseline)
            if hit is None:
                if self._scroll_down(window, rows):
                    detail = "待发送行不在可视区，已向下滚动列表"
                    continue
                detail = ("可视区未见基线之后新出现的本次委托行"
                          "（已排除基线 %d 行；代码/方向/数量/价格/时间均需一致，"
                          "等待渲染）" % base_n)
                time.sleep(0.35)
                continue
            # 点击前最后一次核对坐标落在窗口内
            sx = window.rect[0] + hit.send.cx
            sy = window.rect[1] + hit.send.cy
            method = self._click(window, sx, sy, hit)
            if method == "busy":
                # 跨引擎 UI 互斥被 THS 占用（>2s）：本轮未点击，下一轮重试
                detail = "UI 互斥锁被其它通道占用，本轮未点击（下轮重试）"
                time.sleep(0.3)
                continue
            if not method:
                detail = "『发送』按钮位置被其它窗口遮挡，放弃点击（避免误点）"
                break
            clicked = 1
            clicked_row = hit
            detail = "已点击『发送』，等待委托表出现该单"
            logger.info(
                "tdx 交易信号自动确认：点击 %s %s x%s @%s 行（行时间 %s，%s）",
                spec.direction, spec.code6, spec.volume, spec.price,
                hit.seconds, method)
            if on_clicked:
                time.sleep(0.5)
                try:
                    on_clicked()
                except Exception:  # noqa: BLE001
                    logger.exception("tdx 下单后置确认钩子异常")
            break

        if clicked and clicked_row is not None:
            self._verify_clicked_row(window, clicked_row)

        if clicked and is_submitted is not None:
            while time.time() < end:
                try:
                    if is_submitted():
                        return ConfirmOutcome(True, "委托表已见该单", clicked,
                                              stale=stale)
                except Exception as e:  # noqa: BLE001
                    logger.debug("tdx 确认后回查委托表异常：%s", e)
                time.sleep(0.4)
            detail = "已点击『发送』，但超时内委托表仍未出现该单"
        elif is_submitted is None and clicked:
            detail = "已点击『发送』（调用方未提供委托表回调）"

        self._dump(spec, detail, window, stale)
        return ConfirmOutcome(False, detail, clicked, stale=stale)

    # ---- 窗口

    def _pick_window(self) -> Optional[WindowRef]:
        try:
            enum = _WindowEnumProc()
            items = []
            for hwnd, title, _pid, visible in enum.run():
                if self.TITLE_KEYWORD not in title:
                    continue
                ref = WindowRef(hwnd, title, _window_rect(hwnd), visible,
                                _has_cef_child(hwnd))
                if ref.width <= 0 or ref.height <= 0:
                    continue
                items.append(ref)
            pick = _pick_window_impl(items)
            if pick is not None and len(items) > 1:
                logger.debug("tdx 交易信号窗口候选 %d 个，选 hwnd=%s（可见=%s cef=%s）",
                             len(items), pick.hwnd, pick.visible, pick.has_cef)
            return pick
        except Exception:  # noqa: BLE001
            logger.exception("tdx 枚举交易信号窗口失败")
            return None

    def _force_show(self, window: WindowRef) -> None:
        try:
            if _user32.IsIconic(wt.HWND(window.hwnd)):
                _user32.ShowWindow(wt.HWND(window.hwnd), 9)   # SW_RESTORE
            else:
                _user32.ShowWindow(wt.HWND(window.hwnd), 4)   # SW_SHOWNOACTIVATE
            time.sleep(0.2)
            window.visible = bool(_user32.IsWindowVisible(wt.HWND(window.hwnd)))
            logger.info("tdx 交易信号窗口已隐藏，已强制显示 hwnd=%s", window.hwnd)
        except Exception:  # noqa: BLE001
            logger.exception("tdx 强制显示交易信号窗口失败")

    # ---- 截图 / OCR

    def _grab(self, window: WindowRef):
        try:
            from PIL import Image
        except ImportError:
            return None
        w, h = window.width, window.height
        if w <= 0 or h <= 0:
            return None
        hwnd = wt.HWND(window.hwnd)
        hwnd_dc = _user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = _gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = _gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = _gdi32.SelectObject(mem_dc, bmp)
        img = None
        try:
            ok = _user32.PrintWindow(hwnd, mem_dc, 2)   # PW_RENDERFULLCONTENT
            if not ok:
                _gdi32.BitBlt(mem_dc, 0, 0, w, h, hwnd_dc, 0, 0, 0x00CC0020)
            bi = _BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bi.biWidth = w
            bi.biHeight = -h          # 负数 = 自上而下
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            got = _gdi32.GetDIBits(mem_dc, bmp, 0, h, buf,
                                   ctypes.byref(bi), 0)
            if got:
                img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
                img = img.copy()
        except Exception:  # noqa: BLE001
            logger.exception("tdx 交易信号窗口截图失败")
            img = None
        finally:
            try:
                _gdi32.SelectObject(mem_dc, old)
                _gdi32.DeleteObject(bmp)
                _gdi32.DeleteDC(mem_dc)
                _user32.ReleaseDC(hwnd, hwnd_dc)
            except Exception:  # noqa: BLE001
                pass
        if img is not None and not _image_has_content(img):
            # PrintWindow 对 CEF/DWM 加速窗口常「返回成功但内容全黑」：退化抓
            # 屏幕对应区域（窗口不可见/被遮挡时读不到正确内容 → 定位失败，
            # 点击前 _ensure_topmost 还会再校验一次，不会误点）。
            alt = _screen_grab(window)
            if alt is not None:
                img = alt
        if img is not None:
            self._last_img = img
        return img

    def _ocr(self, img) -> List[tuple]:
        if not self.enable_ocr or img is None:
            return []
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return []
        try:
            eng = self._ensure_ocr_engine()
            if eng is None:
                return []
            rgb = img.convert("RGB")
            gray = rgb.convert("L")
            hist_mean = sum(gray.resize((32, 32)).getdata()) / 1024.0
            if hist_mean < 128:          # 深色主题（通达信默认）→ 反色
                rgb = ImageOps.invert(rgb)
            big = rgb.resize((rgb.width * _OCR_SCALE, rgb.height * _OCR_SCALE),
                             Image.LANCZOS)
            bin_img = big.convert("L").point(
                lambda p: 255 if p > _OCR_THRESHOLD else 0).convert("RGB")
            return self._run_ocr(eng, bin_img)
        except Exception:  # noqa: BLE001
            logger.exception("tdx 交易信号窗口 OCR 失败")
            return []

    def _ensure_ocr_engine(self):
        if self._ocr_engine is not None:
            return self._ocr_engine
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        eng = None
        try:
            eng = OcrEngine.try_create_from_user_profile_languages()
        except Exception:  # noqa: BLE001
            eng = None
        if eng is None:
            for tag in ("zh-Hans-CN", "zh-CN", "en-US"):
                try:
                    eng = OcrEngine.try_create_from_language(Language(tag))
                except Exception:  # noqa: BLE001
                    continue
                if eng is not None:
                    break
        self._ocr_engine = eng
        return eng

    def _run_ocr(self, eng, img) -> List[tuple]:
        import asyncio
        import tempfile as _tf

        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage import FileAccessMode, StorageFile

        fd, path = _tf.mkstemp(suffix=".png")
        os.close(fd)
        try:
            img.save(path, "PNG")

            async def _go():
                f = await StorageFile.get_file_from_path_async(path)
                stream = await f.open_async(FileAccessMode.READ)
                dec = await BitmapDecoder.create_async(stream)
                bmp = await dec.get_software_bitmap_async()
                return await eng.recognize_async(bmp)

            loop = asyncio.new_event_loop()
            try:
                res = loop.run_until_complete(_go())
            finally:
                loop.close()
            out = []
            for line in res.lines:
                for w in line.words:
                    b = w.bounding_rect
                    out.append((
                        w.text,
                        float(b.x) / _OCR_SCALE,
                        float(b.y) / _OCR_SCALE,
                        float(b.width) / _OCR_SCALE,
                        float(b.height) / _OCR_SCALE,
                    ))
            return out
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _read_rows(self, window: WindowRef) -> List[Row]:
        img = self._grab(window)
        if img is None:
            return []
        return _build_rows(self._ocr(img))

    def snapshot_rows(self) -> Tuple[tuple, ...]:
        """``order_stock`` **之前**拍「交易信号」窗口基线（旧行指纹集合）。

        确认流程只允许点基线之后新出现的行；返回空元组表示没拍到（窗口未开/
        无文本/OCR 不可用）——此时匹配退化为「行时间 ≥ 下单时间-2s」，同样
        不会点更早的历史残留行（宁失败不错）。
        """
        try:
            with _dpi_ctx():
                return self._snapshot_rows_impl()
        except Exception:  # noqa: BLE001
            logger.exception("tdx 交易信号窗口基线采集失败")
            return ()

    def _snapshot_rows_impl(self) -> Tuple[tuple, ...]:
        window = self._pick_window()
        if window is None:
            return ()
        if not window.visible:
            self._force_show(window)
            time.sleep(0.2)
        return tuple(_row_fingerprint(r) for r in self._read_rows(window))

    def _verify_clicked_row(self, window: Optional[WindowRef],
                            hit: Row) -> None:
        """点击后校验该行行尾按钮是否变为「已发送」（OCR 可判定时）。

        三类结果写日志区分判定路径：True=已发送；False=仍为『发送』（可疑，
        最终仍以委托表认领为准）；None=判不了（行已消失/OCR 读不到），退回
        委托表认领。
        """
        if window is None:
            logger.info("tdx 确认：点击后无窗口句柄，退回委托表认领判定")
            return
        try:
            time.sleep(0.5)
            rows = self._read_rows(window)
        except Exception:  # noqa: BLE001
            logger.warning("tdx 确认：点击后 OCR 复核异常，退回委托表认领判定")
            return
        fp = _row_fingerprint(hit)
        same = [r for r in rows if _row_fingerprint(r) == fp]
        if not same:
            logger.info("tdx 确认：OCR 判定路径=行已消失（可能已发送并移除），"
                        "退回委托表认领")
            return
        if any(r.sent for r in same):
            logger.info("tdx 确认：OCR 判定路径=行尾按钮已变『已发送』")
        else:
            logger.warning("tdx 确认：OCR 判定路径=行尾按钮仍为『发送』"
                           "（以委托表认领为准）")

    # ---- 滚动

    def _scroll_down(self, window: WindowRef, rows_before: Sequence[Row]) -> bool:
        """向下滚动列表；内容有变化返回 True。

        先向 CEF host 投递 WM_MOUSEWHEEL（不抢焦点、不动鼠标），无效再走
        真实滚轮（需窗口在前台，且校验未被遮挡）。
        """
        sig_before = self._row_sig(rows_before)
        host = self._deepest_at(window.hwnd,
                                window.rect[0] + window.width // 2,
                                window.rect[1] + int(window.height * 0.45))
        if host:
            x = window.rect[0] + window.width // 2
            y = window.rect[1] + int(window.height * 0.45)
            lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
            wparam = (((-120 * 3) & 0xFFFF) << 16)
            try:
                _user32.PostMessageW(wt.HWND(host), WM_MOUSEWHEEL,
                                     wt.WPARAM(wparam), wt.LPARAM(lparam))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.35)
            rows = self._read_rows(window)
            if self._row_sig(rows) != sig_before:
                return True
        sx = window.rect[0] + window.width // 2
        sy = window.rect[1] + int(window.height * 0.45)
        root = _user32.GetAncestor(
            _user32.WindowFromPoint(wt.POINT(sx, sy)) or window.hwnd, GA_ROOT)
        if int(root) != window.hwnd:
            try:
                _user32.SetForegroundWindow(wt.HWND(window.hwnd))
                time.sleep(0.15)
            except Exception:  # noqa: BLE001
                pass
            root = _user32.GetAncestor(
                _user32.WindowFromPoint(wt.POINT(sx, sy)) or window.hwnd, GA_ROOT)
            if int(root) != window.hwnd:
                return False
        from .win_dialog_scope import UI_AUTO_LOCK
        with UI_AUTO_LOCK:
            _user32.SetCursorPos(int(sx), int(sy))
            _user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -120 * 3, 0)
        time.sleep(0.4)
        rows = self._read_rows(window)
        return self._row_sig(rows) != sig_before

    @staticmethod
    def _row_sig(rows: Sequence[Row]) -> tuple:
        return tuple((r.code, r.volume, r.seconds) for r in rows)

    # ---- 点击

    def _click(self, window: WindowRef, sx: float, sy: float,
               hit: Row) -> str:
        """点击『发送』：真控件 BM_CLICK 优先，否则真实鼠标（CEF 无控件）。"""
        btn = self._button_at(window.hwnd, sx, sy)
        if btn:
            try:
                _user32.SendMessageW(wt.HWND(btn), BM_CLICK, 0, 0)
                return "hwnd_bmclick"
            except Exception:  # noqa: BLE001
                logger.exception("tdx BM_CLICK 失败，改真实点击")
        if not self._ensure_topmost(window, sx, sy):
            return ""
        from .win_dialog_scope import UI_AUTO_LOCK
        # 有超时获取：THS 下单在执行 UI 自动化时最多持有 ~10s，这里不能无限
        # 阻塞把整个 5s 确认预算吃光——锁忙就返回 "busy"，由调用方下一轮重试。
        if not UI_AUTO_LOCK.acquire(timeout=2.0):
            logger.warning("tdx 交易信号点击跳过：UI 互斥锁被占用（>2s），下轮重试")
            return "busy"
        try:
            _user32.SetCursorPos(int(sx), int(sy))
            time.sleep(0.05)
            _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        finally:
            try:
                UI_AUTO_LOCK.release()
            except Exception:  # noqa: BLE001
                pass
        return "mouse"

    def _ensure_topmost(self, window: WindowRef, sx: float, sy: float) -> bool:
        """确认点击点最上层就是我们自己的窗口（防点到遮挡窗口 / 误点其它客户端）。"""
        try:
            if _user32.IsIconic(wt.HWND(window.hwnd)):
                _user32.ShowWindow(wt.HWND(window.hwnd), 9)
                time.sleep(0.2)
            root = int(_user32.GetAncestor(
                _user32.WindowFromPoint(wt.POINT(int(sx), int(sy))),
                GA_ROOT) or 0)
            if root == window.hwnd:
                return True
            _user32.SetForegroundWindow(wt.HWND(window.hwnd))
            time.sleep(0.25)
            root = int(_user32.GetAncestor(
                _user32.WindowFromPoint(wt.POINT(int(sx), int(sy))),
                GA_ROOT) or 0)
            if root == window.hwnd:
                return True
            logger.warning("tdx 交易信号窗口点 (%s,%s) 处被其它窗口遮挡（root=%s）",
                           sx, sy, root)
            return False
        except Exception:  # noqa: BLE001
            logger.exception("tdx 点击前遮挡校验失败")
            return False

    def _button_at(self, hwnd: int, sx: float, sy: float) -> Optional[int]:
        """点位内若存在真实 Button 型子控件，返回其句柄（新版/其它主题可能提供）。"""
        stack = [(hwnd, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > 6:
                continue
            for c in _children(cur):
                r = _window_rect(c)
                if not (r[0] <= sx <= r[2] and r[1] <= sy <= r[3]):
                    continue
                cls = _window_class(c)
                if cls in ("Button", "CCmdButton", "CButton") and \
                        _window_text(c).strip().startswith("发"):
                    return c
                stack.append((c, depth + 1))
        return None

    def _deepest_at(self, hwnd: int, sx: float, sy: float) -> Optional[int]:
        best = None
        stack = [(hwnd, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > 6:
                continue
            for c in _children(cur):
                r = _window_rect(c)
                if r[0] <= sx <= r[2] and r[1] <= sy <= r[3]:
                    best = c
                    stack.append((c, depth + 1))
        return best

    # ---- 辅助

    def _count_matching(self, rows: Sequence[Row], spec: SignalSpec) -> int:
        return sum(1 for r in rows
                   if r.code == spec.code6 and r.volume == str(int(spec.volume))
                   and (not r.direction or r.direction == spec.direction))

    def _dump(self, spec: SignalSpec, detail: str,
              window: Optional[WindowRef], stale: int) -> None:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder = self.dump_dir / (
                "%s_%s_%s_%s" % (stamp, spec.code6, spec.direction, spec.volume))
            folder.mkdir(parents=True, exist_ok=True)
            meta = [
                "code6=%s direction=%s volume=%s price=%s sent_at=%s"
                % (spec.code6, spec.direction, spec.volume, spec.price,
                   spec.sent_at),
                "detail=%s" % detail,
                "stale_matching_rows=%d" % stale,
                "window=%r" % (window,),
                "",
                "== tdxw top-level windows ==",
            ]
            try:
                enum = _WindowEnumProc()
                for hwnd, title, pid, visible in enum.run():
                    meta.append("hwnd=%s pid=%s visible=%s class=%r title=%r rect=%s"
                                % (hwnd, pid, visible, _window_class(hwnd),
                                   title, _window_rect(hwnd)))
                    if visible and hwnd == (window.hwnd if window else -1):
                        tree: List[tuple] = []
                        _walk(hwnd, 0, 4, tree)
                        meta.append("  == control tree ==")
                        for d, h, k, t, r, v, cid in tree:
                            meta.append("  %s%s class=%r text=%r id=%s rect=%s vis=%s"
                                        % ("  " * d, hex(h), k, t, cid, r, v))
            except Exception as e:  # noqa: BLE001
                meta.append("window tree dump failed: %s" % e)
            (folder / "meta.txt").write_text("\n".join(meta), encoding="utf-8")
            if window is not None:
                img = self._grab(window)
                if img is not None:
                    img.save(folder / "screenshot.png")
                rows = self._read_rows(window)
                (folder / "ocr_rows.txt").write_text(
                    "\n".join("cy=%.1f code=%r vol=%r dir=%r sec=%s send=%r text=%s"
                              % (r.cy, r.code, r.volume, r.direction, r.seconds,
                                 bool(r.send), " | ".join(c.text for c in r.cells))
                              for r in rows),
                    encoding="utf-8")
            logger.warning("tdx 交易信号自动确认失败，诊断已落盘：%s", folder)
        except Exception:  # noqa: BLE001
            logger.exception("tdx 交易信号失败诊断落盘异常")
