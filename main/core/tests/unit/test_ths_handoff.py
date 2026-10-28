"""ThsFileHandoff 单测 — 同花顺文件交接层（THS file_builtin）。

覆盖：目录创建、orders.jsonl 追加（seq 单调）/游标消费、mark_done（results.jsonl +
done 表）、游标损坏/JSONL 损坏行容错、signal/risk/result 通用写入。
"""
import json
import os

import pytest

from core.engine.live.ths_handoff import ThsFileHandoff


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_init_creates_session_dir(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 7)
    assert h.dir_path == os.path.join(str(tmp_path), "7")
    assert os.path.isdir(h.dir_path)


def test_append_order_writes_seq_ts_session_and_read_roundtrip(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 1)
    seq1 = h.append_order({"order_id": "a1", "stock_code": "000001.SZ"})
    seq2 = h.append_order({"order_id": "a2", "stock_code": "600000.SH"})
    assert seq1 == 1 and seq2 == 2

    orders = _read_lines(h.orders_path)
    assert [o["seq"] for o in orders] == [1, 2]
    assert orders[0]["order_id"] == "a1"
    assert orders[0]["session_id"] == 1
    assert orders[0]["ts"]  # 上海时间 ISO

    pending = h.read_pending_orders()
    assert [p["order_id"] for p in pending] == ["a1", "a2"]
    # 游标消费：已读指令不再重复返回
    assert h.read_pending_orders() == []


def test_seq_monotonic_across_instances(tmp_path):
    h1 = ThsFileHandoff(str(tmp_path), 3)
    h1.append_order({"order_id": "x1"})
    h1.append_order({"order_id": "x2"})
    h2 = ThsFileHandoff(str(tmp_path), 3)
    seq = h2.append_order({"order_id": "x3"})
    assert seq == 3


def test_mark_done_writes_results_and_done_table(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 2)
    h.append_order({"order_id": "o1"})
    h.read_pending_orders()
    h.mark_done("o1", {"status": "accepted", "ref": "100001"})

    results = _read_lines(h.results_path)
    assert results[0]["order_id"] == "o1"
    assert results[0]["status"] == "accepted"
    assert results[0]["ref"] == "100001"
    assert results[0]["session_id"] == 2
    assert "o1" in h.cursor["done"]

    # 游标回退重扫（如 orders 文件被轮转/截断后 offset 归零）：
    # done 表幂等跳过已执行单，未完成单仍会返回
    h.append_order({"order_id": "o2"})
    with open(h.cursor_path, "w", encoding="utf-8") as f:
        json.dump({"orders_offset": 0, "done": {"o1": {"status": "accepted"}}}, f)
    h2 = ThsFileHandoff(str(tmp_path), 2)
    pending = h2.read_pending_orders()
    assert [p["order_id"] for p in pending] == ["o2"]


def test_corrupt_jsonl_lines_skipped(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 4)
    h.append_order({"order_id": "good1"})
    with open(h.orders_path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    h.append_order({"order_id": "good2"})

    pending = h.read_pending_orders()
    assert [p["order_id"] for p in pending] == ["good1", "good2"]
    # 损坏行同样被游标跳过（不无限重扫）
    assert h.read_pending_orders() == []


def test_corrupt_cursor_resets_from_zero(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 5)
    h.append_order({"order_id": "k1"})
    with open(h.cursor_path, "w", encoding="utf-8") as f:
        f.write("garbage")
    h2 = ThsFileHandoff(str(tmp_path), 5)
    assert h2.cursor["orders_offset"] == 0
    assert h2.cursor["done"] == {}
    assert len(h2.read_pending_orders()) == 1


def test_write_signal_and_risk(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 9)
    h.write_signal({"type": "signal", "signal_name": "open_sig"})
    h.write_risk({"type": "risk", "rule": "stop_loss"})
    h.write_risk({"type": "decision", "gate": "capital_gate"})

    signals = _read_lines(h.signals_path)
    assert signals[0]["signal_name"] == "open_sig"
    assert signals[0]["session_id"] == 9
    assert signals[0]["ts"]
    risks = _read_lines(h.risk_path)
    assert [r["type"] for r in risks] == ["risk", "decision"]


def test_write_result_generic_receipt(tmp_path):
    h = ThsFileHandoff(str(tmp_path), 6)
    h.write_result({"event": "executor_started"})
    results = _read_lines(h.results_path)
    assert results[0]["event"] == "executor_started"
    assert results[0]["session_id"] == 6


def test_read_pending_missing_dir_returns_empty(tmp_path):
    # 目录被外部删除（或尚未创建）时读容错：返回空，不抛异常
    h = ThsFileHandoff(str(tmp_path), 8)
    import shutil
    shutil.rmtree(h.dir_path)
    assert h.read_pending_orders() == []
