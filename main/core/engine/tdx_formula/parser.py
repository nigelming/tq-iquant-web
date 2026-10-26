"""通达信公式语句解析层：定义提取与绘图属性剥离。

parse_defs 移植自「通达信指标源码整理工具.html」TdxSigNorm.parseDefs：
按语句边界（顶层分号）切分而非按行切分——通达信允许一行多条语句，
按行切分会把后续定义整条吞进第一条。注释（{...}，可跨行）与字符串
（'...'）内的分号不参与切分。

strip_draw_attrs 移植自 TdxOptimizer.stripDrawAttrs：输出线尾部的
COLORxxx/LINETHICK/NODRAW 等绘图属性不是表达式的一部分，必须先剥离，
且只在括号平衡处剥离（避免把函数参数误当属性）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 标识符：字母/下划线/中文开头，可含数字（通达信允许中文变量名）
_ID_CHARS = "A-Za-z0-9_\u4e00-\u9fa5"
_DEF_HEAD_RE = re.compile(rf"^([{_ID_CHARS}]+)\s*(:=|:)")

# 绘图属性名（输出线尾部、顶层逗号之后）
_DRAW_ATTR_NAMES = (
    r"COLOR[A-Z0-9]*|LINETHICK\d|CIRCLEDOT|CROSSDOT|DOTLINE|POINTDOT|"
    r"POINTLINE|STICK|VOLSTICK|NODRAW|NOTEXT|PRECISION\d|SHOWCOLOR"
)
_DRAW_ATTR_TAIL_RE = re.compile(rf",\s*({_DRAW_ATTR_NAMES})\s*;?\s*$", re.IGNORECASE)


@dataclass
class Definition:
    """一条「名称 := 表达式」或「名称 : 表达式」定义。"""

    name: str
    op: str  # ":=" 中间变量 / ":" 输出线
    expr: str
    attrs: list[str] = field(default_factory=list)  # 仅输出线：剥离的绘图属性
    line: int = 1
    raw: str = ""


def _balance_ok(s: str) -> bool:
    """括号平衡检查（防止在未闭合的括号内剥属性）。"""
    depth = 0
    for c in s:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def strip_draw_attrs(expr: str) -> tuple[str, list[str]]:
    """剥离表达式尾部的绘图属性，返回 (纯表达式, [属性, ...])。

    例："IF(C>O,1,0),COLORRED,LINETHICK2" → ("IF(C>O,1,0)", ["COLORRED", "LINETHICK2"])
    """
    e = str(expr or "").strip()
    attrs: list[str] = []
    changed = True
    while changed:
        changed = False
        m = _DRAW_ATTR_TAIL_RE.search(e)
        if m and _balance_ok(e[: m.start()]):
            attrs.insert(0, m.group(1).strip())
            e = e[: m.start()].rstrip()
            changed = True
    return e, attrs


def _strip_comments(segment: str) -> str:
    """去掉 {...} 注释（保留字符串内的花括号），其余原样返回。"""
    out: list[str] = []
    i, n = 0, len(segment)
    in_quote = False
    while i < n:
        c = segment[i]
        if in_quote:
            out.append(c)
            if c in ("'", '"'):
                in_quote = False
            i += 1
            continue
        if c in ("'", '"'):
            in_quote = True
            out.append(c)
            i += 1
            continue
        if c == "{":
            close = segment.find("}", i)
            i = n if close < 0 else close + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_defs(src: str) -> list[Definition]:
    """按语句边界切分源码，提取全部定义。

    返回顺序即源码顺序。name 支持中文；op 为 ":="（中间变量）或
    ":"（输出线，已剥离绘图属性）。
    """
    text = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    statements: list[tuple[int, int, int]] = []  # (start, end, line)
    n = len(text)
    i = 0
    line = 1
    depth = 0
    in_quote = False
    stmt_start = -1
    stmt_start_line = 1
    seen_code = False

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
        if in_quote:
            if c in ("'", '"'):
                in_quote = False
            i += 1
            continue
        if c in ("'", '"'):
            if stmt_start < 0:
                stmt_start, stmt_start_line = i, line
            seen_code = True
            in_quote = True
            i += 1
            continue
        if c == "{":  # 块注释可跨行，整体跳过
            if stmt_start < 0:
                stmt_start, stmt_start_line = i, line
            close = text.find("}", i)
            if close < 0:
                break
            line += text.count("\n", i, close)
            i = close + 1
            continue
        if c == "(":
            depth += 1
            if stmt_start < 0:
                stmt_start, stmt_start_line = i, line
            seen_code = True
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            seen_code = True
            i += 1
            continue
        if c == ";" and depth == 0:
            if stmt_start < 0:
                stmt_start, stmt_start_line = i, line
            statements.append((stmt_start, i, stmt_start_line))
            stmt_start = -1
            seen_code = False
            i += 1
            continue
        if not c.isspace():
            if stmt_start < 0:
                stmt_start, stmt_start_line = i, line
            seen_code = True
        i += 1

    # 末尾无分号的语句（须含有效代码，纯注释不算）
    if stmt_start >= 0 and seen_code:
        statements.append((stmt_start, n, stmt_start_line))

    defs: list[Definition] = []
    for start, end, ln in statements:
        cleaned = _strip_comments(text[start:end]).strip()
        if not cleaned:
            continue
        m = _DEF_HEAD_RE.match(cleaned)
        if not m:
            continue
        expr = cleaned[m.end() :]
        attrs: list[str] = []
        if m.group(2) == ":":  # 输出线：剥离尾部绘图属性
            expr, attrs = strip_draw_attrs(expr)
        expr = re.sub(r";?\s*$", "", expr).strip()
        if not expr:
            continue
        defs.append(
            Definition(
                name=m.group(1),
                op=m.group(2),
                expr=expr,
                attrs=attrs,
                line=ln,
                raw=text[start:end],
            )
        )
    return defs
