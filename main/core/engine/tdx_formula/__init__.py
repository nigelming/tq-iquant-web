"""通达信公式子集解释器（内置引擎）—— 解析层。

目标：用户粘贴的公式文本不依赖本地通达信客户端也能算出信号。

当前进度（解析层）：
- dictionary: TDX 函数词典（396 条，含能力标签）+ future/realtime 能力闸门
- lexer:      注释/字符串掩码、全角标点归一、不可见字符扫描
- parser:     语句边界切分、定义提取（:= 中间变量 / : 输出线）、绘图属性剥离

后续：求值层（polars 向量化时序函数）→ 对接 _match_signals 信号链路。
解析层行为基线移植自「通达信指标源码整理工具.html」（TdxLint / TdxSigNorm.parseDefs /
TdxOptimizer.stripDrawAttrs），其词典覆盖了手工整理与 TCalc64.dll 提取两个来源。
"""

from core.engine.tdx_formula.dictionary import Issue, capability, precheck
from core.engine.tdx_formula.evaluator import (
    FormulaEvalError,
    cross_periods_of,
    evaluate_outputs,
    syntax_check,
    xref_names_of,
)
from core.engine.tdx_formula.lexer import (
    MaskResult,
    find_invisible,
    line_col,
    mask,
    normalize_fullwidth,
)
from core.engine.tdx_formula.parser import Definition, parse_defs, strip_draw_attrs

__all__ = [
    "Definition",
    "FormulaEvalError",
    "Issue",
    "MaskResult",
    "capability",
    "cross_periods_of",
    "evaluate_outputs",
    "find_invisible",
    "line_col",
    "mask",
    "normalize_fullwidth",
    "parse_defs",
    "precheck",
    "strip_draw_attrs",
    "syntax_check",
    "xref_names_of",
]
