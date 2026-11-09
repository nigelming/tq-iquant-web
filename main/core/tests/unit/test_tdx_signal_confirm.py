"""通达信「交易信号」下单确认自动化单测（纯逻辑 + 假窗口，不触真实客户端）。"""
import time as _time
from datetime import datetime

import pytest

from core.engine.live.tdx_signal_confirm import (
    SignalSpec,
    TdxSignalConfirmer,
    WindowRef,
    _build_rows,
    _match_row,
    _norm_digits,
    _pick_window_impl,
    _row_fingerprint,
)


def _row_words(code, vol, hh, mm, ss, y, direction="卖出", name="测试",
               price="17.05"):
    """按真机 OCR 布局合成一行词表（坐标=窗口内 1x 像素）。"""
    return [
        ("0", 30, y, 8, 11),
        (hh, 70, y, 18, 11), (":", 88, y, 3, 11),
        (mm, 91, y, 18, 11), (":", 109, y, 3, 11),
        (ss, 112, y, 18, 11),
        ("28500208", 160, y, 70, 11),
        (code, 516, y, 52, 11),
        (name, 580, y, 40, 11),
        (direction, 600, y, 20, 11),
        (price, 620, y, 30, 11),
        (vol, 660, y, 30, 11),
        ("发", 677, y, 13, 11), ("送", 689, y, 13, 11),
        ("取", 727, y, 13, 11), ("消", 739, y, 13, 11),
    ]


def _rows(*args):
    words = []
    for item in args:
        words.extend(item)
    return _build_rows(words)


SPEC = SignalSpec(code6="002056", direction="sell", volume=300,
                  price=17.05, sent_at=datetime(2026, 11, 4, 14, 52, 42))


def test_norm_digits_handles_ocr_confusions():
    assert _norm_digits("0020S6") == "002056"
    assert _norm_digits("60O888") == "600888"
    assert _norm_digits("lOO") == "100"


def test_build_rows_groups_by_y_and_exposes_row_fields():
    """只按 y 聚行（列不合并）：语义（时间/按钮）由 Row 属性按 x 邻近精确处理。"""
    rows = _rows(_row_words("0020S6", "300", "14", "43", "09", 70))
    assert len(rows) == 1
    row = rows[0]
    assert row.code == "002056"         # S→5 归一；时间单元格不参与代码判定
    assert row.volume == "300"
    assert row.direction == "sell"
    assert row.seconds == 14 * 3600 + 43 * 60 + 9   # 时间碎片按 x 拼接后取正则
    assert row.send is not None and row.send.text == "发送"


def test_match_row_prefers_time_window_and_bottom_most():
    rows = _rows(
        _row_words("0020S6", "300", "14", "43", "09", 70),
        _row_words("0020S6", "300", "14", "51", "20", 170),
        _row_words("0020S6", "300", "14", "52", "42", 220),
    )
    hit = _match_row(rows, SPEC)
    assert hit is not None
    assert hit.seconds == 14 * 3600 + 52 * 60 + 42


def test_match_row_picks_closest_time_when_two_rows_in_window():
    """同代码/数量/方向的两笔（时间窗内）→ 取时间最接近本次下单的那行。

    连续两笔止损（两个组合都卖同一只）时，不能因上一笔行还没消失就点它。
    """
    spec = SignalSpec(code6="002056", direction="sell", volume=300, price=17.05,
                      sent_at=datetime(2026, 11, 4, 14, 52, 46))
    rows = _rows(
        _row_words("0020S6", "300", "14", "52", "42", 70),
        _row_words("0020S6", "300", "14", "52", "46", 220),
    )
    hit = _match_row(rows, spec)
    assert hit is not None
    assert hit.seconds == 14 * 3600 + 52 * 60 + 46


def test_match_row_rejects_stale_rows_outside_time_window():
    rows = _rows(_row_words("0020S6", "300", "14", "49", "33", 70))
    assert _match_row(rows, SPEC) is None


def test_match_row_rejects_volume_mismatch():
    rows = _rows(_row_words("0020S6", "100", "14", "52", "42", 70))
    assert _match_row(rows, SPEC) is None


def test_match_row_rejects_wrong_direction():
    rows = _rows(_row_words("0020S6", "300", "14", "52", "42", 70,
                            direction="买入"))
    assert _match_row(rows, SPEC) is None


def test_match_row_without_time_only_accepts_single_candidate():
    spec = SignalSpec(code6="002056", direction="sell", volume=300)
    one = _rows(_row_words("0020S6", "300", "14", "52", "42", 70))
    assert _match_row(one, spec) is not None
    two = _rows(
        _row_words("0020S6", "300", "14", "43", "09", 70),
        _row_words("0020S6", "300", "14", "52", "42", 220),
    )
    assert _match_row(two, spec) is None


def test_match_row_uses_send_button_not_cancel():
    rows = _rows(_row_words("0020S6", "300", "14", "52", "42", 70))
    send = rows[0].send
    assert send is not None
    assert send.cx < 700   # 「发送」在「取消」左侧


def test_pick_window_prefers_visible_cef():
    hidden_cef = WindowRef(1, "交易信号", (0, 0, 800, 450), False, True)
    visible_no_cef = WindowRef(2, "交易信号", (0, 0, 800, 450), True, False)
    visible_cef = WindowRef(3, "交易信号", (0, 0, 800, 450), True, True)
    pick = _pick_window_impl([hidden_cef, visible_no_cef, visible_cef])
    assert pick.hwnd == 3
    assert _pick_window_impl([hidden_cef, visible_no_cef]).hwnd == 2
    assert _pick_window_impl([hidden_cef]).hwnd == 1
    assert _pick_window_impl([]) is None


def _fake_confirmer(monkeypatch, rows, visible=True, click_method="mouse",
                    dump_dir=None):
    c = TdxSignalConfirmer(timeout=0.5, dump_dir=dump_dir)
    win = WindowRef(100, "交易信号", (0, 0, 800, 500), visible, True)
    monkeypatch.setattr(c, "_pick_window", lambda: win)
    monkeypatch.setattr(c, "_read_rows", lambda _w: rows)
    monkeypatch.setattr(c, "_scroll_down", lambda _w, _r: False)
    clicks = []

    def _click(window, sx, sy, hit):
        clicks.append((sx, sy))
        return click_method

    monkeypatch.setattr(c, "_click", _click)
    monkeypatch.setattr("core.engine.live.tdx_signal_confirm.time.sleep",
                        lambda _s: None)
    return c, clicks


def test_confirm_clicks_once_and_waits_for_submission(monkeypatch, tmp_path):
    rows = _rows(_row_words("0020S6", "300", "14", "52", "42", 220))
    c, clicks = _fake_confirmer(monkeypatch, rows, dump_dir=str(tmp_path))
    state = {"n": 0}

    def submitted():
        state["n"] += 1
        return state["n"] >= 3

    out = c.confirm(SPEC, is_submitted=submitted, deadline=_time.time() + 1.0,
                    baseline=())
    assert out.ok is True
    assert len(clicks) == 1
    # 点击坐标 = 窗口内「发送」按钮中心（相对窗口偏移）
    assert 670 <= clicks[0][0] <= 700 and 210 <= clicks[0][1] <= 235


def test_confirm_does_not_click_when_occluded(monkeypatch, tmp_path):
    rows = _rows(_row_words("0020S6", "300", "14", "52", "42", 220))
    c, clicks = _fake_confirmer(monkeypatch, rows, click_method="",
                                dump_dir=str(tmp_path))
    out = c.confirm(SPEC, is_submitted=lambda: False,
                    deadline=_time.time() + 0.3, baseline=())
    assert out.ok is False
    # 假 _click 会记录「尝试坐标」，但遮挡时不得计入已点击次数
    assert out.clicks == 0
    assert "遮挡" in out.detail


def test_confirm_failure_dumps_diagnostics(monkeypatch, tmp_path):
    c = TdxSignalConfirmer(timeout=0.2, dump_dir=str(tmp_path))
    monkeypatch.setattr(c, "_pick_window", lambda: None)
    monkeypatch.setattr("core.engine.live.tdx_signal_confirm.time.sleep",
                        lambda _s: None)
    out = c.confirm(SPEC, is_submitted=lambda: False,
                    deadline=_time.time() + 0.1, baseline=())
    assert out.ok is False
    assert "交易信号" in out.detail
    dumps = list(tmp_path.glob("*/meta.txt"))
    assert dumps, "失败必须落盘诊断"
    assert "code6=002056" in dumps[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------- 加固（2026-11-05）


def test_row_price_reads_split_ocr_fragments():
    """真机 OCR 会把 17.64 拆成「1」「7」「·」「64」——必须按 x 邻近拼接。"""
    words = [
        ("0", 30, 70, 8, 11),
        ("09", 70, 70, 18, 11), ("：", 88, 70, 3, 11),
        ("51", 91, 70, 18, 11), ("：", 109, 70, 3, 11),
        ("08", 112, 70, 18, 11),
        ("28500208", 160, 70, 70, 11),
        ("0020S6", 516, 70, 52, 11),
        ("测试", 580, 70, 40, 11),
        ("卖出", 600, 70, 20, 11),
        ("1", 620, 70, 7, 11), ("7", 628, 70, 7, 11),
        ("·", 636, 70, 4, 11), ("64", 641, 70, 14, 11),
        ("300", 675, 70, 30, 11),
        ("发", 700, 70, 13, 11), ("送", 712, 70, 13, 11),
        ("取", 750, 70, 13, 11), ("消", 762, 70, 13, 11),
    ]
    row = _build_rows(words)[0]
    assert abs(row.price - 17.64) <= 1e-9


def test_match_row_excludes_baseline_even_if_old_row_time_closer():
    """基线里的旧行一律排除：即使它离 sent_at 更近，也只点基线之后的新行。"""
    old = _rows(_row_words("0020S6", "300", "14", "52", "42", 70))
    new = _rows(_row_words("0020S6", "300", "14", "52", "43", 220))
    baseline = tuple(_row_fingerprint(r) for r in old)
    rows = old + new
    hit = _match_row(rows, SPEC, baseline=baseline)
    assert hit is not None
    assert hit.seconds == 14 * 3600 + 52 * 60 + 43
    # 对照：不传基线时旧逻辑会点更近的旧行（这正是 09:51 误点的成因）
    assert _match_row(rows, SPEC).seconds == 14 * 3600 + 52 * 60 + 42


def test_match_row_requires_price_match():
    """同代码/数量/方向、不同价格的两行（分批减仓），只点价格一致的那行。"""
    rows = _rows(
        _row_words("0020S6", "300", "14", "52", "42", 70, price="17.05"),
        _row_words("0020S6", "300", "14", "52", "42", 150, price="16.98"),
    )
    hit = _match_row(rows, SPEC)
    assert hit is not None
    assert abs(hit.price - 17.05) <= 0.011
    # 价格读不出（市价/OCR 缺失）→ 不放行（宁失败不错）
    opaque = _rows(_row_words("0020S6", "300", "14", "52", "42", 70,
                              price="市价"))
    assert _match_row(opaque, SPEC) is None


def test_confirm_never_clicks_old_row_when_new_row_not_rendered(
        monkeypatch, tmp_path):
    """新行未渲染时：旧行虽在基线外的时间窗内，也不得点击，直接判失败落盘。"""
    old = _rows(_row_words("0020S6", "300", "14", "52", "42", 70))
    c, clicks = _fake_confirmer(monkeypatch, old, dump_dir=str(tmp_path))
    baseline = tuple(_row_fingerprint(r) for r in old)
    out = c.confirm(SPEC, is_submitted=lambda: False,
                    deadline=_time.time() + 0.3, baseline=baseline)
    assert out.ok is False
    assert out.clicks == 0
    assert clicks == []          # 一次都没尝试点旧行
    assert "基线" in out.detail
    dumps = list(tmp_path.glob("*/meta.txt"))
    assert dumps, "新行未渲染也必须落盘诊断"
    assert "stale_matching_rows=1" in dumps[0].read_text(encoding="utf-8")


# ------------------------------------------------ 跨引擎 UI 互斥（2026-11-06 P1）


def test_click_returns_busy_when_ui_lock_held(monkeypatch):
    """UI 互斥被 THS 占用：不阻塞、不点击，返回 "busy" 交由调用方下轮重试。"""
    from core.engine.live import win_dialog_scope

    c = TdxSignalConfirmer(timeout=0.2, dump_dir=None)
    monkeypatch.setattr(c, "_ensure_topmost", lambda w, x, y: True)
    monkeypatch.setattr(c, "_button_at", lambda hwnd, x, y: None)

    class _Lock:
        def acquire(self, timeout=None):
            assert timeout is not None and timeout <= 2.0
            return False

        def release(self):
            raise AssertionError("未取到锁不得释放")

    monkeypatch.setattr(win_dialog_scope, "UI_AUTO_LOCK", _Lock())
    win = WindowRef(100, "交易信号", (0, 0, 800, 500), True, True)
    assert c._click(win, 10, 10, None) == "busy"


def test_confirm_retries_when_ui_lock_busy(monkeypatch, tmp_path):
    """锁忙不是「被遮挡放弃」：confirm 应继续下一轮，而不是 break。"""
    rows = _rows(_row_words("0020S6", "300", "14", "52", "42", 220))
    c, clicks = _fake_confirmer(monkeypatch, rows, click_method="busy",
                               dump_dir=str(tmp_path))
    out = c.confirm(SPEC, is_submitted=lambda: False,
                    deadline=_time.time() + 0.2, baseline=())
    assert out.ok is False
    assert out.clicks == 0
    assert len(clicks) >= 2, "锁忙应重试，而不是一次就放弃"
    assert "互斥" in out.detail
