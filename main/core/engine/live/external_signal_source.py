"""外部信号注入源 — 通达信条件预警文本文件信号源（0011 外部信号注入链路）。

通达信客户端「条件预警」可配置公式触发时动态输出文本文件（GBK 编码、追加写入、
制表符分隔，每行一条）：

    行号\\t代码(6位)\\t名称\\tYYYY-MM-DD HH:MM\\t价格\\t涨跌幅%\\t现量\\t公式名
    1\\t920718\\t六合高科\\t2026-10-30 09:50\\t 9.83\\t-1.90%\\t    1\\t分时突击

平台定时增量读取（时间戳游标 + lookback 时间窗口），公式名匹配策略后注入
signal_cache 走 Portfolio.on_bar 全套（风控/审批/在途单闸不绕过）。所有周期
公式信号均可经此链路注入——包括 1m（绕过 TQ 公式引擎 1m 返回全空的限制）。

时间窗口模型（批次1 外部信号改造）：通达信条件预警启动时会**补齐全部历史信号**
写入文件——按行号游标处理会消费所有旧信号（旧信号股价已拉升/条件已消失，
不应交易）。改为双重过滤：

    ① 时间戳游标（_last_read_time，初始=启动时当前时间）——只处理时间 > 游标
       的行，启动前存量（含窗口内近端补齐行）全挡；
    ② lookback 窗口（默认 300s）——晚到的陈旧行（时间 > 游标但早于
       now - lookback_seconds）不返回，信号新鲜度兜底。

本源是通道无关的：文件由外部（通达信客户端）产生，平台只读，不回写不删除。
"""
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 列序（真机样本）：0 行号 / 1 代码 / 2 名称 / 3 时间 / 4 价格 / 5 涨跌幅 / 6 现量 / 7 公式名
_COL_CODE = 1
_COL_NAME = 2
_COL_TIME = 3
_COL_PRICE = 4
_COL_FORMULA = 7


def _exchange_for(code: str) -> str:
    """6 位代码推断交易所后缀（与 tdx_tq_dispatcher._exchange_for / live_service
    _normalize_external_symbol 同口径：92→BJ（北交所新股段）、11→SH（沪可转债）、
    6/5/9→SH、4/8→BJ、其余→SZ）。"""
    if code.startswith("92"):
        return "BJ"
    if code.startswith("11"):
        return "SH"
    if code.startswith(("6", "5", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def normalize_external_symbol(code: str) -> str:
    """预警文件 6 位代码 → 平台统一带后缀格式（920718 → 920718.BJ）。

    已带后缀原样返回；非 6 位数字且无后缀返回空串（调用方丢弃该行）。
    """
    text = str(code or "").strip()
    if "." in text:
        return text
    code6 = re.sub(r"\D", "", text)[:6]
    if len(code6) != 6:
        return ""
    return "%s.%s" % (code6, _exchange_for(code6))


class ExternalSignalFileSource:
    """通达信条件预警文本文件信号源。

    定时读取预警文件 → 解析（时间戳游标 + lookback 窗口增量）→ 返回新信号列表。
    文件由通达信客户端条件预警功能动态输出（追加写入），游标语义适配增量：
    每轮只消费晚于游标的时间窗口内新行，旧行不再重放。公式名不在
    strategy_formula_names 集合中的行不返回（文件里可能混有其它公式的预警）。
    """

    def __init__(
        self,
        file_path: str,
        strategy_formula_names: Set[str],
        lookback_seconds: int = 300,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._path = file_path
        self._formula_names = set(strategy_formula_names or ())
        self._lookback_seconds = max(0, int(lookback_seconds))
        self._clock = clock or datetime.now
        # 时间戳游标：初始 = 启动时当前时间。通达信条件预警启动会补齐全部历史
        # 信号写入文件，游标把启动前存量（含窗口内近端补齐行）全部挡掉，
        # 只消费启动后新触发的信号。
        self._last_read_time: datetime = self._clock()
        self._seen_codes: Set[str] = set()  # 本日已处理代码（防重复）

    def read_new_signals(self) -> List[Dict]:
        """增量读取新信号行，解析为标准化信号 dict 列表。

        返回: [{"code": "920718", "name": "六合高科", "time": datetime,
                "price": Decimal("9.83"), "formula_name": "分时突击"}]
        （code 为文件中的 6 位原码，带后缀归一由引擎注入时完成。）

        双重过滤（批次1）：
        ① 时间戳游标——只处理 signal_time > _last_read_time 的行；
        ② lookback 窗口——只返回 signal_time >= now - lookback_seconds 的信号。
        游标推进：本轮文件全部可解析行中最晚 signal_time（单调不回退，防补齐
        旧行把游标拉回过去）。编码 GBK（通达信输出）。解析失败的行跳过 +
        warning 留痕。去重：同代码（_seen_codes）同日只返回第一条；公式名不
        匹配的行不占去重名额（同代码换匹配公式后仍可触发）。
        """
        now = self._clock()
        cutoff = now - timedelta(seconds=self._lookback_seconds)
        lines = self._read_complete_lines()
        signals: List[Dict] = []
        latest: Optional[datetime] = None
        for raw in lines:
            line = raw.strip().lstrip("﻿").strip()
            if not line:
                continue  # 空行
            sig = self._parse_line(line)
            if sig is None:
                continue
            if latest is None or sig["time"] > latest:
                latest = sig["time"]
            # ① 时间戳游标：启动前存量行（含通达信补齐历史）不消费
            if sig["time"] <= self._last_read_time:
                continue
            # ② lookback 窗口：晚到的陈旧行不交易（股价已拉升/条件已消失）
            if sig["time"] < cutoff:
                continue
            if sig["code"] in self._seen_codes:
                continue
            self._seen_codes.add(sig["code"])
            signals.append(sig)
        # 游标单调推进 = 文件中最晚信号时间（仅前进，防补齐行把游标拉回过去）
        if latest is not None and latest > self._last_read_time:
            self._last_read_time = latest
        return signals

    def reset_daily(self):
        """每日重置：清空 seen_codes，游标归位当前时间。

        供调用方在新交易日（预警文件按日轮转/换新文件路径）调用；时间戳游标
        归位当前时间，与「启动后新信号才消费」同语义——新文件的行只要时间晚于
        重置时点且落在 lookback 窗口内即消费。
        """
        self._seen_codes = set()
        self._last_read_time = self._clock()

    # ------------------------------------------------------------------
    def _read_complete_lines(self) -> List[str]:
        """读文件并按 \\n 切行。编码 GBK；末段无换行符视为同花顺正在写半行，
        不消费（待补全后下一轮读出）。文件不存在/读失败 → 空列表。"""
        try:
            with open(self._path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("外部信号文件读取失败：%s", self._path, exc_info=True)
            return []
        try:
            text = raw.decode("gbk", errors="replace")
        except Exception:  # noqa: BLE001 — decode(errors=replace) 实际不抛，兜底留痕
            logger.warning("外部信号文件解码失败：%s", self._path, exc_info=True)
            return []
        parts = text.split("\n")
        parts = [p.rstrip("\r") for p in parts[:-1]]  # 末段=完整空串或半行，均不消费
        return parts

    def _parse_line(self, line: str):
        """单行解析：列数校验 → 代码 6 位 → 公式名匹配 → 时间/价格解析。
        任何一步失败返回 None（跳过 + warning）。"""
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) <= _COL_FORMULA:
            logger.warning("外部信号行列数不足（%d 列），跳过：%r", len(cols), line)
            return None
        code = re.sub(r"\D", "", cols[_COL_CODE])
        if len(code) != 6:
            logger.warning("外部信号代码无效（%r），跳过：%r", cols[_COL_CODE], line)
            return None
        formula = cols[_COL_FORMULA]
        if formula not in self._formula_names:
            return None  # 未匹配公式名：正常静默跳过（文件可能混有其它公式预警）
        try:
            ts = datetime.strptime(cols[_COL_TIME], "%Y-%m-%d %H:%M")
            price = Decimal(cols[_COL_PRICE])
        except (ValueError, InvalidOperation):
            logger.warning("外部信号行时间/价格解析失败，跳过：%r", line)
            return None
        return {
            "code": code,
            "name": cols[_COL_NAME],
            "time": ts,
            "price": price,
            "formula_name": formula,
        }
