"""TDX 函数词典加载 + 内置引擎能力闸门（precheck）。

数据来源：data/tdx_funcs.json —— 由「通达信指标源码整理工具.html」的
TDX_FUNCS（手工整理词典）与 TDX_FUNCS_EXT（TCalc64.dll 提取，379 条）
合并而来，手工条目覆盖 DLL 条目；共 396 个函数，每条带分类/签名/说明，
部分带 fl 能力标签（future=未来函数 / realtime=仅实时 / data=需分笔数据 /
l2=需Level2 / indexonly=仅指数周期有效）。

能力闸门：内置引擎用历史 OHLCV 数据求值，以下函数一律拒绝——
- 未来函数（ZIG/PEAK/WINNER 等）：回测数据漂移
- 即时行情函数（DYNAINFO/FINANCE 等）：历史 bar 上无对应值
- 需分笔/L2 数据的函数（BUYVOL/SELLVOL 等）：注入数据只有 OHLCV+Amount
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.engine.tdx_formula.lexer import line_col, mask, normalize_fullwidth

_DATA_PATH = Path(__file__).parent / "data" / "tdx_funcs.json"


@lru_cache(maxsize=1)
def _load_data() -> dict:
    with open(_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


FUNCS: dict[str, dict] = _load_data()["funcs"]
FIELDS: set[str] = {str(f).upper() for f in _load_data()["fields"]}
ATTRS: set[str] = {str(a).upper() for a in _load_data()["attrs"]}
FUTURE: set[str] = {str(f).upper() for f in _load_data()["future"]}
REALTIME: set[str] = {str(r).upper() for r in _load_data()["realtime"]}
NEED_L2: set[str] = {str(x).upper() for x in _load_data()["need_l2"]}
INDEX_ONLY: set[str] = {str(x).upper() for x in _load_data()["index_only"]}

_IDENT_RE = re.compile(r"[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*")


@dataclass
class Issue:
    """precheck 的一条结论。"""

    rule: str  # future / realtime / data / index_only / unknown
    level: str  # error（拒绝）/ warning（提示）
    name: str  # 函数名（大写）
    line: int
    col: int
    message: str


def capability(name: str) -> list[str]:
    """查函数能力标签（fl），未知函数返回空列表。"""
    entry = FUNCS.get(name.upper())
    return list(entry.get("fl") or []) if entry else []


def precheck(src: str) -> list[Issue]:
    """扫描源码中全部标识符，按词典能力标签给出拒绝/警告清单。

    - future/realtime/data 类函数无论带不带括号（BUYVOL 可作裸字段用）
      都拒绝；未知警告仅对带括号的调用触发（裸标识符大概率是变量）。
    - 在掩码文本上扫描（注释/字符串内的名字不参与），全角标点先归一
      （1:1 映射，位置不变，行号列号指向原文）。
    """
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    m = mask(normalize_fullwidth(src or ""))
    for tok in _IDENT_RE.finditer(m.code):
        name = tok.group(0).upper()
        is_call = m.code[tok.end() :].lstrip().startswith("(")
        line, col = line_col(src or "", tok.start())
        tags = capability(name)
        issue: Issue | None = None
        if name in FUTURE:
            issue = Issue("future", "error", name, line, col,
                          f"{name} 是未来函数，回测会产生数据漂移，禁止使用")
        elif name in REALTIME:
            issue = Issue("realtime", "error", name, line, col,
                          f"{name} 是即时行情函数，内置引擎（历史数据）不支持")
        elif "data" in tags or name in NEED_L2:
            issue = Issue("data", "error", name, line, col,
                          f"{name} 需要分笔/Level2 数据，内置引擎不支持")
        elif "future" in tags:
            # fl 标签的未来函数（如 XMA）不在 FUTURE 拒绝名单：TDX 语义本身
            # 引用未来 bar，内置引擎按同语义实现，给 leak 警告由用户确认
            issue = Issue("leak", "warning", name, line, col,
                          f"{name} 使用未来数据（TDX 语义，回测存在前视引用），"
                          f"请确认用途")
        elif name in INDEX_ONLY:
            issue = Issue("index_only", "warning", name, line, col,
                          f"{name} 仅在指数周期有效")
        elif is_call and name not in FUNCS and name not in FIELDS and name not in ATTRS:
            issue = Issue("unknown", "warning", name, line, col,
                          f"未知函数 {name}（若为变量引用请忽略）")
        if issue and (issue.rule, issue.name) not in seen:
            seen.add((issue.rule, issue.name))
            issues.append(issue)
    return issues
