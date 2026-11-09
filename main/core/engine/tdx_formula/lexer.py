"""通达信公式词法层：注释/字符串掩码、全角标点归一、不可见字符扫描。

移植自「通达信指标源码整理工具.html」的 TdxLint.mask / FW_MAP / INVISIBLE，
并合入 TdxRulesV2 的全角补全表与不可见字符码位表。
"""

from __future__ import annotations

from typing import NamedTuple

# 全角 → 半角（通达信只认半角符号；全部为 1:1 单字符映射，归一后长度不变）
FW_MAP: dict[str, str] = {
    "：": ":", "，": ",", "（": "(", "）": ")", "；": ";",
    "“": "'", "”": "'", "‘": "'", "’": "'",
    "＋": "+", "－": "-", "＊": "*", "／": "/", "＝": "=",
    "＜": "<", "＞": ">", "％": "%", "｜": "|", "＿": "_",
    "！": "!", "＃": "#", "＆": "&", "￥": "Y", "？": "?",
    # TdxRulesV2 补全（原表缺失的 12+ 项）
    "【": "[", "】": "]", "［": "[", "］": "]",
    "｛": "{", "｝": "}", "～": "~", "＄": "$", "＠": "@", "｀": "`",
    "〈": "<", "〉": ">", "《": "<", "》": ">",
}

# 不可见字符表（码位准确版，从网页/PDF 复制粘贴时最常见）
INVISIBLE: dict[str, str] = {
    "\u200b": "零宽空格 U+200B",
    "\u200c": "零宽不连字 U+200C",
    "\u200d": "零宽连字 U+200D",
    "\ufeff": "BOM U+FEFF",
    "\u00a0": "不换行空格 U+00A0",
    "\u3000": "全角空格 U+3000",
    "\u2028": "行分隔符 U+2028",
    "\u2029": "段分隔符 U+2029",
    "\u2060": "元音连接符 U+2060",
    "\u180e": "蒙古文元音分隔符 U+180E",
    "\u00ad": "软连字 U+00AD",
    "\u200e": "方向标记 LRM U+200E",
    "\u200f": "方向标记 RLM U+200F",
    "\u2061": "函数应用 U+2061",
    "\u2062": "不可见乘号 U+2062",
    "\u2063": "不可见分隔符 U+2063",
    "\u2064": "不可见加号 U+2064",
}


class MaskResult(NamedTuple):
    """mask() 结果：code 中注释/字符串已替换为空格，但绝对位置不变。

    strings/comments 记录 (start, end, content) 区间，content 可从原文还原。
    """

    code: str
    strings: list[tuple[int, int, str]]
    comments: list[tuple[int, int, str]]


def mask(src: str) -> MaskResult:
    """把 {注释} 与 '字符串' 内容替换为空格（换行保留），位置不变。

    解析器在掩码文本上做语句切分/调用扫描，避免注释里的分号、
    字符串里的括号被误判。未闭合字符串掩到行尾/文末。
    """
    out = list(src)
    strings: list[tuple[int, int, str]] = []
    comments: list[tuple[int, int, str]] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "{":
            j = src.find("}", i + 1)
            end = n if j < 0 else j + 1
            comments.append((i, end, src[i + 1 : (n if j < 0 else j)]))
            for k in range(i, end):
                if out[k] not in ("\n", "\r"):
                    out[k] = " "
            i = end
        elif ch in ("'", '"'):
            # 单引号字符串 + 双引号字符串（TDX 跨公式引用 "公式名.线名"）。
            # 与 parser.parse_defs（两种引号都当字符串）保持同一口径——否则
            # 双引号内文本会被当作代码参与标识符扫描（precheck 误报未来函数）。
            quote = ch
            j = i + 1
            while j < n and src[j] != quote and src[j] != "\n":
                j += 1
            stop = min(j + 1, n)
            strings.append((i, stop, src[i + 1 : j]))
            for k in range(i, stop):
                if out[k] != "\n":
                    out[k] = " "
            i = stop
        else:
            i += 1
    return MaskResult("".join(out), strings, comments)


def normalize_fullwidth(src: str) -> str:
    """全角标点 → 半角。1:1 映射，归一后长度与原文一致（行号定位不受影响）。"""
    return "".join(FW_MAP.get(ch, ch) for ch in src)


def find_invisible(src: str) -> list[tuple[int, str]]:
    """扫描不可见字符，返回 (下标, 描述) 列表。"""
    return [(i, INVISIBLE[ch]) for i, ch in enumerate(src) if ch in INVISIBLE]


def line_col(src: str, idx: int) -> tuple[int, int]:
    """绝对下标 → (行号, 列号)，均从 1 起。"""
    line = src.count("\n", 0, idx) + 1
    last = src.rfind("\n", 0, idx) + 1
    return line, idx - last + 1
