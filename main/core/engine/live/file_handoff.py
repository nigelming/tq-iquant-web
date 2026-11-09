"""执行契约文件交接层 — 决策层与执行层之间的显性文件契约（通道无关）。

决策层（信号/风控/指令）与执行层（任意通道执行器）之间的显性文件契约，
通道无关——任何无桥执行端实现「消费指令→执行→回执」适配器即可接入。
五个 JSONL/JSON 文件：

- signals.jsonl：引擎 signal 事件（引擎 _emit 钩子写入）
- risk.jsonl：引擎 decision/risk 事件（同上）
- orders.jsonl：下单指令（dispatcher place_order 秒回前追加，seq 单调递增）
- results.jsonl：执行回执（执行器 accepted/rejected，或通用诊断事件）
- cursor.json：orders 消费游标 {"orders_offset": 已消费行数, "done": {order_id: 回执}}

语义约定：
- 所有写入行统一带 ``schema_version``（当前 1，见 _SCHEMA_VERSION；版本策略
  见 docs/execution-contract.md：新增字段可选、改名/删除须升版）；
- 所有写均为「逐行 append + flush + 实例级线程锁」，每行带 seq/ts(上海时间)/session_id；
- read_pending_orders 按游标消费（读后游标推进到文件尾），done 表里的 order_id 跳过
  ——订单文件被外部截断/轮转导致游标回退时靠 done 表保证幂等（已执行单不会重复执行）；
- JSON 解析失败的行跳过并 warning（不中断消费）；cursor.json 损坏/缺失时 done 表
  一并丢失，故 offset 初始化到当前文件尾（历史指令不重放，at-most-once 不降级）。
"""

import json
import logging
import os
import threading

from .timing import now_shanghai

logger = logging.getLogger(__name__)

_SIGNALS_FILE = "signals.jsonl"
_RISK_FILE = "risk.jsonl"
_ORDERS_FILE = "orders.jsonl"
_RESULTS_FILE = "results.jsonl"
_CURSOR_FILE = "cursor.json"

#: 执行契约 schema 版本：所有 JSONL 写入行统一携带（向后兼容规则见
#: docs/execution-contract.md —— 新增字段可选；改名/删除/语义变更须升版）。
_SCHEMA_VERSION = 1


class FileHandoff:
    """一个 session 一套交接目录：{work_dir}/{session_id}/。"""

    def __init__(self, work_dir: str, session_id: int) -> None:
        self._session_id = int(session_id or 0)
        # 相对路径按服务进程 CWD（=main/）解析，如 data/ths → main/data/ths/{sid}/
        self._dir = os.path.join(os.path.abspath(str(work_dir or "data/ths")),
                                 str(self._session_id))
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.Lock()
        # seq 以文件已有物理行数起底：Core 重启后仍单调递增（损坏行也占位）。
        self._orders_seq = self._count_lines(self.orders_path)
        self._cursor = self._load_cursor()

    # ---- 路径 ----

    @property
    def dir_path(self) -> str:
        return self._dir

    @property
    def signals_path(self) -> str:
        return os.path.join(self._dir, _SIGNALS_FILE)

    @property
    def risk_path(self) -> str:
        return os.path.join(self._dir, _RISK_FILE)

    @property
    def orders_path(self) -> str:
        return os.path.join(self._dir, _ORDERS_FILE)

    @property
    def results_path(self) -> str:
        return os.path.join(self._dir, _RESULTS_FILE)

    @property
    def cursor_path(self) -> str:
        return os.path.join(self._dir, _CURSOR_FILE)

    @property
    def cursor(self) -> dict:
        return self._cursor

    # ---- 写入（引擎 signal/decision/risk + dispatcher 指令/回执）----

    def write_signal(self, entry: dict) -> None:
        self._append(self.signals_path, dict(entry))

    def write_risk(self, entry: dict) -> None:
        self._append(self.risk_path, dict(entry))

    def write_result(self, entry: dict) -> None:
        """通用回执（外部诊断），不进 done 表。"""
        self._append(self.results_path, dict(entry))

    def append_order(self, entry: dict) -> int:
        """下单指令入队（entry 含 order_id），返回单调 seq。"""
        with self._lock:
            self._orders_seq += 1
            payload = {
                "seq": self._orders_seq,
                "ts": now_shanghai().isoformat(timespec="seconds"),
                "session_id": self._session_id,
            }
            payload.update(entry)
            self._write_line(self.orders_path, payload)
        return self._orders_seq

    def mark_done(self, order_id: str, result: dict) -> None:
        """执行回执：results.jsonl 落一行 + 游标 done 表（幂等依据）。"""
        oid = str(order_id)
        with self._lock:
            payload = {
                "ts": now_shanghai().isoformat(timespec="seconds"),
                "session_id": self._session_id,
            }
            payload.update({"order_id": oid, **result})
            self._write_line(self.results_path, payload)
            self._cursor["done"][oid] = dict(result or {})
            self._save_cursor()

    # ---- 消费（内置执行器线程）----

    def read_pending_orders(self) -> list:
        """按游标读未消费指令：读后游标推进到当前文件尾（at-most-once 语义——
        读后执行前崩溃的单不会重复执行，由引擎 180s expire 兜底标 rejected）。"""
        with self._lock:
            pending = []
            total = 0
            try:
                with open(self.orders_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                return []
            except OSError as e:
                logger.warning("file handoff read orders failed: %s", e)
                return []
            total = len(lines)
            offset = int(self._cursor["orders_offset"])
            if offset > total:  # 文件被外部截断/轮转：游标回退到 0 重扫
                offset = 0
            for raw in lines[offset:]:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    logger.warning("file handoff skip corrupt order line: %.80s", line)
                    continue
                oid = str(entry.get("order_id") or "")
                if not oid or oid in self._cursor["done"]:
                    continue
                pending.append(entry)
            self._cursor["orders_offset"] = total
            self._save_cursor()
            return pending

    # ---- 内部 ----

    def _append(self, path: str, entry: dict) -> None:
        payload = {"ts": now_shanghai().isoformat(timespec="seconds"),
                   "session_id": self._session_id}
        payload.update(entry)
        with self._lock:
            self._write_line(path, payload)

    def _write_line(self, path: str, payload: dict) -> None:
        # schema_version 统一在行落盘点注入（signals/risk/orders/results 全覆盖）；
        # payload 显式携带时可覆盖（向前兼容未来按行标版的场景）。
        line = json.dumps(
            {"schema_version": _SCHEMA_VERSION, **payload},
            ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    @staticmethod
    def _count_lines(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _load_cursor(self) -> dict:
        try:
            with open(self.cursor_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("cursor not a dict")
        except (OSError, ValueError):
            # cursor.json 缺失/损坏：done 表一并丢失，若从 0 重扫会把历史
            # orders 全部重放（at-most-once 降级为重复下单）。故把 offset
            # 初始化到当前文件尾——只执行此后新增的指令。正常重启（游标
            # 可读）不走这里，行为完全不变。
            offset = self._count_lines(self.orders_path)
            if offset > 0:
                logger.warning(
                    "file handoff cursor missing/corrupt, skip %d historical order(s)",
                    offset)
            return {"orders_offset": offset, "done": {}}
        try:
            offset = max(int(data.get("orders_offset") or 0), 0)
        except (TypeError, ValueError):
            offset = 0
        done = data.get("done")
        return {"orders_offset": offset, "done": dict(done) if isinstance(done, dict) else {}}

    def _save_cursor(self) -> None:
        """cursor.json 原子写（tmp + replace）：崩溃不留半截游标（半截游标会导致
        done 表丢失 → 已执行单被重扫重执行，真钱风险）。"""
        tmp = self.cursor_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cursor, f, ensure_ascii=False)
            f.flush()
        os.replace(tmp, self.cursor_path)
