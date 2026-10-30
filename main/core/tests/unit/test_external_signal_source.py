"""外部信号注入源单测 — 同花顺条件预警文本文件解析。

文件形态（真机样本）：GBK 编码、追加写入、制表符分隔，每行一条预警：
  行号\t代码(6位)\t名称\tYYYY-MM-DD HH:MM\t价格\t涨跌幅%\t现量\t公式名
ExternalSignalFileSource 增量读取（行号游标）→ 公式名匹配过滤 → 标准化信号 dict。
"""
import logging
from datetime import datetime
from decimal import Decimal

import pytest

from core.engine.live.external_signal_source import ExternalSignalFileSource

NAMES = {"分时突击"}


def _line(no, code, name, ts, price, pct, vol, formula):
    return "%d\t%s\t%s\t%s\t %s\t %s\t %s\t%s" % (
        no, code, name, ts, price, pct, vol, formula)


def _write_bytes(path, data: bytes, append=False):
    if append:
        with open(path, "ab") as f:
            f.write(data)
    else:
        path.write_bytes(data)


def _write_lines(tmp_path, lines, append=False):
    data = "".join(l + "\n" for l in lines).encode("gbk")
    p = tmp_path / "alert.txt"
    _write_bytes(p, data, append=append)
    return p


def _source(path, names=NAMES):
    return ExternalSignalFileSource(str(path), names)


# ---------------- 基本解析 ----------------

def test_parse_normal_lines(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
        _line(2, "605377", "科兴科技", "2026-10-30 09:51", "7.74", "0.91%", "12", "分时突击"),
    ])
    sigs = _source(p).read_new_signals()
    assert len(sigs) == 2
    s0, s1 = sigs
    assert s0["code"] == "920718"
    assert s0["name"] == "六合高科"
    assert s0["time"] == datetime(2026, 10, 30, 9, 50)
    assert s0["price"] == Decimal("9.83")
    assert s0["formula_name"] == "分时突击"
    assert s1["code"] == "605377"
    assert s1["time"] == datetime(2026, 10, 30, 9, 51)
    assert s1["price"] == Decimal("7.74")


def test_missing_file_returns_empty(tmp_path):
    p = tmp_path / "alert.txt"
    src = _source(p)
    assert src.read_new_signals() == []
    # 文件随后出现（同花顺客户端启动后才开始写）→ 正常读到
    _write_lines(tmp_path, [
        _line(1, "600000", "浦发银行", "2026-10-30 09:50", "9.83", "0.1%", "1", "分时突击"),
    ])
    sigs = src.read_new_signals()
    assert len(sigs) == 1
    assert sigs[0]["code"] == "600000"


def test_empty_file_and_blank_lines(tmp_path):
    p = _write_lines(tmp_path, ["", _line(1, "600000", "浦发银行", "2026-10-30 09:50", "9.83", "0.1%", "1", "分时突击"), ""])
    sigs = _source(p).read_new_signals()
    assert len(sigs) == 1
    assert sigs[0]["code"] == "600000"


def test_cursor_incremental_read(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
    ])
    src = _source(p)
    assert len(src.read_new_signals()) == 1
    # 追加写入（同花顺动态输出）→ 只读到新增行
    _write_lines(tmp_path, [
        _line(2, "605377", "科兴科技", "2026-10-30 09:51", "7.74", "0.91%", "12", "分时突击"),
    ], append=True)
    sigs = src.read_new_signals()
    assert len(sigs) == 1
    assert sigs[0]["code"] == "605377"
    # 无新增 → 空列表（游标不回退）
    assert src.read_new_signals() == []


def test_incomplete_trailing_line_not_consumed(tmp_path):
    """同花顺正在写半行（无换行符结尾）→ 不消费，待写完整后下一轮读出。"""
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
    ])
    src = _source(p)
    assert len(src.read_new_signals()) == 1
    # 半行（未写完，无换行）
    _write_bytes(p, "3\t600000".encode("gbk"), append=True)
    assert src.read_new_signals() == []
    # 补全换行
    _write_bytes(p, "\t浦发银行\t2026-10-30 09:52\t 9.84\t 0.10%\t 1\t分时突击\n".encode("gbk"),
                 append=True)
    sigs = src.read_new_signals()
    assert len(sigs) == 1
    assert sigs[0]["code"] == "600000"


# ---------------- 异常行 / 编码 ----------------

def test_malformed_rows_skipped_with_warning(tmp_path, caplog):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
        "bad-line-no-tabs",
        _line(2, "605377", "科兴科技", "2026-10-30 09:51", "7.74", "0.91%", "12", "分时突击"),
        _line(3, "ABC123", "非法代码", "2026-10-30 09:52", "9.9", "0.1%", "1", "分时突击"),
        _line(4, "600000", "浦发银行", "不是时间", "9.9", "0.1%", "1", "分时突击"),
        _line(5, "600000", "浦发银行", "2026-10-30 09:53", "abc", "0.1%", "1", "分时突击"),
    ])
    src = _source(p)
    with caplog.at_level(logging.WARNING):
        sigs = src.read_new_signals()
    assert [s["code"] for s in sigs] == ["920718", "605377"]
    assert caplog.text  # 异常行有告警留痕
    # 异常行同样推进游标（不会重复出现）
    # 异常行同样推进游标（不会重复出现）
    assert src.read_new_signals() == []


# ---------------- 公式名匹配过滤 ----------------

def test_formula_name_filter(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "别的公式"),
        _line(2, "605377", "科兴科技", "2026-10-30 09:51", "7.74", "0.91%", "12", "分时突击"),
    ])
    sigs = _source(p, {"分时突击"}).read_new_signals()
    assert [s["code"] for s in sigs] == ["605377"]
    # 不匹配的行不占去重名额：同代码换匹配公式后可再触发
    src = _source(p, {"别的公式"})
    sigs2 = src.read_new_signals()
    assert [s["code"] for s in sigs2] == ["920718"]


# ---------------- 信号去重 / 每日重置 ----------------

def test_seen_codes_dedup(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
        _line(2, "920718", "六合高科", "2026-10-30 09:55", "9.90", "-1.20%", "2", "分时突击"),
    ])
    sigs = _source(p).read_new_signals()
    # 同代码同日只触发一次（电平信号反复预警防重复开仓）
    assert [s["code"] for s in sigs] == ["920718"]
    assert sigs[0]["time"] == datetime(2026, 10, 30, 9, 50)


def test_reset_daily_clears_dedup_and_cursor(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
    ])
    src = _source(p)
    assert len(src.read_new_signals()) == 1
    src.reset_daily()
    assert src._seen_codes == set()
    assert src._last_line_count == 0
    # 重置后重读（文件按日轮转场景：新一天的新文件从行 0 开始）
    assert len(src.read_new_signals()) == 1


def test_empty_formula_names_matches_nothing(tmp_path):
    p = _write_lines(tmp_path, [
        _line(1, "920718", "六合高科", "2026-10-30 09:50", "9.83", "-1.90%", "1", "分时突击"),
    ])
    assert _source(p, set()).read_new_signals() == []
