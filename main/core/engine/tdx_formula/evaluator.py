"""通达信公式求值层：表达式 AST + polars 向量化求值。

输入 parse_defs 产出的定义列表，按顺序求值（:= 存入环境，: 为输出线），
输出 {输出线名: polars.Float64 Series}，结构与 TQ 按 formula_name 取回的
输出映射同构，可直接对接 _match_signals 的 trigger_value 匹配链路。

当前实现 134 个函数（TDX 语义），按族划分：
- 均线族：MA/EMA/SMA/DMA/WMA/MEMA/EXPMEMA/TMA/AMA
- 引用族：REF/REFV/VALUEWHEN/CONST/SUMBARS/SUMBARSX/BARSCOUNT/BARSSINCE/BARSLAST/BARSLASTCOUNT
- 穿越逻辑：CROSS/LONGCROSS/NDAY/UPNDAY/DOWNNDAY/EXIST/EXISTR/BARSSINCE/BARSLASTS/BARSSINCEN
- 极值窗口：HHV/LLV/HHVBARS/LLVBARS/FINDHIGH/FINDLOW/FINDHIGHBARS/FINDLOWBARS
- 条件/累计：IF/IFF/FILTER/FILTERX/BACKSET/SUM/COUNT/EVERY/LAST
- 统计回归：STD/STDP/VAR/VARP/AVEDEV/DEVSQ/SLOPE/FORCAST/COVAR/RELATE
- 数学：ABS/SQRT/LOG/LN/EXP/POW/SIGN/MAX/MIN/SIN/COS/TAN/ASIN/ACOS/ATAN/
        CEILING/FLOOR/INTPART/FRACPART/ROUND/ROUND2/MOD/REVERSE
- 位置排名：CURRBARSCOUNT/TOTALBARSCOUNT/ISLASTBAR/BARSTATUS（裸标识符）/HOD/LOD
- 逻辑：BETWEEN/RANGE/NOT
- 形态：SAR/SARTURN
- 时间分量（裸标识符，批次 5）：YEAR/MONTH/DAY/HOUR/MINUTE/WEEKDAY/DATE/TIME
        （依赖 df 的 datetime 列）
- 批次 6：别名 EXPMA=EMA/SGN=SIGN；排名跨度 TOPRANGE/LOWRANGE（单调栈）；
  涨跌停 ZTPRICE/DTPRICE（四舍五入到 2 位）；累乘 MULAR；时间过滤
  TFILT/REFDATE/TIMETOSEC；条件取反 IFN；裸标识符 FROMOPEN/TOTALFZNUM/
  USEDDATANUM/TOTALBARCOUNT
- 批次 7：动态窗口——REF/SUM/LLV/HHV 的窗口参数可为序列（逐 bar 变窗口，
  TDX 倒推写法如 REF(C,BARSLAST(cond))）；其余窗口族仍要求常量
- 批次 8：XMA/HHVLLV/MEDIAN/UPDOWN/MAX6/NEWSAR/DRAWNULL/TFILTER/TTFILTER；
  时间周期 WEEKOFYEAR/PERIOD（PERIOD 需 period 上下文；SEASON/MIN1..MIN60 为
  跨周期引用锚点、WEEK 不在 iQuant 运行时函数表，作为裸函数仍显式报错；
  星期序号用 WEEKDAY）；
  大盘引用 INDEXC/O/H/L/V/A（需 index_frames 注入对齐后的指数 OHLCV）
- 批次 9a：跨周期引用 FIELD#PERIOD（如 C#WEEK/HIGH#DAY）——需 period_frames
  注入上级周期 K 线（回测接线按公式实际引用附加拉取）；默认 TDX 语义（所属
  上级 bar 整根终值，周内含未来引用），no_leak=True 仅引用已完成上级 bar；
  仅支持引用更粗周期，更细周期显式报错

设计约束：所有序列统一 Float64；布尔结果一律 1.0/0.0（null→0，即"无信号"）；
算术运算保留 null 传播。未知变量/函数、跨周期数据缺失/更细周期引用显式报错，
不静默返回 0——错误必须暴露给语法预检与 diagnostics.errors。
"""

from __future__ import annotations

import inspect
import math
import re
from collections import deque
from datetime import timedelta
from typing import Any

import polars as pl

from core.engine.tdx_formula.parser import _DEF_HEAD_RE, Definition, parse_defs

# polars 1.21 起 rolling 系列的 min_periods 更名为 min_samples，导入时探测一次
_ROLLING_MIN_KW = (
    "min_samples"
    if "min_samples" in inspect.signature(pl.Series.rolling_mean).parameters
    else "min_periods"
)


def _rolling(fn, w: int, min_periods: int | None = None, **kw) -> pl.Series:
    # fill_nan 统一 NaN → null（如 STD N=1 的 0/0），TDX 无 NaN 概念。
    # min_periods 默认 = w（全 warmup）；TDX 部分窗口语义的函数（HHV/LLV/SUM/
    # COUNT/EVERY/EXIST）传 1——窗口未满时用已有数据出值（8471 根实算校验实证）。
    mp = w if min_periods is None else min_periods
    return fn(window_size=w, **{_ROLLING_MIN_KW: mp}, **kw).fill_nan(None)

_SUPPORTED_FUNCS = (
    "MA", "EMA", "SMA", "DMA", "REF", "CROSS", "HHV", "LLV",
    "HHVBARS", "LLVBARS", "IF", "IFF", "BARSLAST", "BARSLASTCOUNT", "FILTER",
    "SUM", "COUNT", "EVERY", "LAST", "VALUEWHEN", "CONST",
    "ABS", "SQRT", "MAX", "MIN",
    # 扩展批次 2：统计回归 / 逻辑引用 / 数学 / 加权均线
    # 注：STDDEV 已移除——TDX zb 引擎对该函数恒返回 0（bar19 起全 0.0），
    # 实际未实现；STD（样本标准差）在 TDX 正常，保留。
    "STD", "VAR", "VARP", "AVEDEV", "DEVSQ", "SLOPE", "FORCAST",
    "EXIST", "BARSSINCE", "BARSCOUNT", "LONGCROSS", "NDAY", "UPNDAY", "DOWNNDAY",
    "SUMBARS", "LOG", "LN", "EXP", "POW", "SIGN", "WMA",
    # 扩展批次 3：数学逐点 / 裸位置 / 排名 / 逻辑 / 统计 / 均线
    "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
    "CEILING", "FLOOR", "INTPART", "FRACPART", "ROUND", "ROUND2", "MOD", "REVERSE",
    "CURRBARSCOUNT", "TOTALBARSCOUNT", "ISLASTBAR", "BARSTATUS",
    "HOD", "LOD", "BETWEEN", "RANGE", "NOT",
    "STDP", "COVAR", "RELATE", "MEMA", "EXPMEMA",
    # 扩展批次 4：寻找极值 / 均线 / 逻辑引用 / 形态
    "FINDHIGH", "FINDLOW", "FINDHIGHBARS", "FINDLOWBARS",
    "TMA", "AMA",
    "BARSLASTS", "BARSSINCEN", "EXISTR", "REFV", "BACKSET", "FILTERX", "SUMBARSX",
    "SAR", "SARTURN",
    # 扩展批次 5：时间分量（裸标识符，_BARE_FUNCS 分派；依赖 df 的 datetime 列）
    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "WEEKDAY", "DATE", "TIME",
    # 扩展批次 6：别名 / 排名跨度 / 涨跌停 / 累乘 / 时间过滤 / 条件取反
    "EXPMA", "SGN", "TOPRANGE", "LOWRANGE", "ZTPRICE", "DTPRICE",
    "MULAR", "TFILT", "REFDATE", "TIMETOSEC", "IFN",
    # 扩展批次 8：核心 / 信号过滤 / 时间周期 / 大盘引用（裸标识符见 _BARE_FUNCS）
    "XMA", "HHVLLV", "MEDIAN", "UPDOWN", "MAX6", "NEWSAR", "TFILTER", "TTFILTER",
)

# 裸标识符函数（无括号形式）：
# 位置类 CURRBARSCOUNT/TOTALBARSCOUNT/ISLASTBAR/BARSTATUS；
# 时间类 YEAR/MONTH/DAY/HOUR/MINUTE/WEEKDAY/DATE/TIME（依赖 df 的 datetime 列）；
# 批次 6：FROMOPEN/TOTALFZNUM（时间类）/USEDDATANUM。
# 注：ISUP/ISDOWN/ISEQUAL 等通用 TDX 文档函数不在本通达信运行时函数表内
#（TQVERIFY5 实测报「未知字符串」）；DAYOFWEEK/TOTALBARCOUNT 同（TQVERIFY6
# 导入实测报「未知字符串」，后者区别于标准统计函数 TOTALBARSCOUNT），
# 为保持回测/实盘词表一致不实现。
_BARE_FUNCS = {
    "CURRBARSCOUNT", "TOTALBARSCOUNT", "ISLASTBAR", "BARSTATUS",
    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "WEEKDAY", "DATE", "TIME",
    "FROMOPEN", "TOTALFZNUM", "USEDDATANUM",
    # 批次 8：时间分量补全 / 周期编号 / 大盘引用 / null 占位
    # 注 1：SEASON 为「跨周期季线引用符号」（引用函数族），非季度函数；MIN1..MIN60
    # 同为跨周期引用锚点且不在 iQuant 运行时函数表（TQVERIFY8B 导入实测报
    # 未知字符串）——均显式报错，待批次 9 跨周期引用实现后按锚点语义接入。
    # 注 2：WEEK 不在国信 iQuant 运行时函数表（TQVERIFY8B 导入实测报未知字符串，
    # 同 DAYOFWEEK 先例），为保持回测/实盘词表一致已移除；星期序号用 WEEKDAY。
    "WEEKOFYEAR", "PERIOD",
    "INDEXC", "INDEXO", "INDEXH", "INDEXL", "INDEXV", "INDEXA",
    "DRAWNULL",
}

# TDX K 线周期编号（TQVERIFY8B 各周期实测：1m=0, 5m=1, 15m=2, 30m=3, 60m=4,
# 1d=5, 1w=6, 1mon=7；平台周期键 60m/1h 并存，backtest_service _MINUTE_PERIODS 用 "1h"）：
_PERIOD_CODES = {
    "1m": 0, "5m": 1, "15m": 2, "30m": 3, "60m": 4, "1h": 4,
    "1d": 5, "1w": 6, "1mon": 7,
}

# 大盘引用族 → 指数 K 线列名（调用方按个股所属市场取指数数据并按 base
# 时间轴行对齐后经 index_frames 注入；行数必须与 base df 一致）
_INDEX_FIELDS = {
    "INDEXO": "Open", "INDEXH": "High", "INDEXL": "Low",
    "INDEXC": "Close", "INDEXV": "Volume", "INDEXA": "Amount",
}

# 行情字段别名（大小写不敏感）→ TQ 数据列名
_FIELD_ALIAS = {
    "C": "Close", "CLOSE": "Close",
    "O": "Open", "OPEN": "Open",
    "H": "High", "HIGH": "High",
    "L": "Low", "LOW": "Low",
    "V": "Volume", "VOL": "Volume",
    "AMO": "Amount", "AMOUNT": "Amount",
}


class FormulaEvalError(Exception):
    """公式解析/求值错误（message 面向语法预检 UI）。"""


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<str>\"[^\"]*\")"
    r"|(?P<name>[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*)"
    r"|(?P<hash>#)"
    r"|(?P<op>>=|<=|<>|!=|==|&&|\|\||[><=+\-*/(),!.])"
    r")"
)

_CMP_OPS = {">", "<", ">=", "<=", "=", "==", "!=", "<>"}


# ---------------------------------------------------------------- 批次 9a 跨周期引用

# FIELD#PERIOD 后缀锚点 → klines 周期键（"60m"/"1h" 双写法均收，查 period_frames 依次试）
_CROSS_PERIOD_MAP: dict[str, tuple[str, ...]] = {
    "MIN1": ("1m",), "MIN5": ("5m",), "MIN15": ("15m",), "MIN30": ("30m",),
    "MIN60": ("60m", "1h"),
    "DAY": ("1d",), "WEEK": ("1w",), "MONTH": ("1mon",),
}

# 周期粒度（分钟）：判定「引用更细周期」（1:N 无法对齐，显式报错）
_PERIOD_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60,
    "1d": 1440, "1w": 10080, "1mon": 43200,
}


def _tokenize(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos, n = 0, len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            if src[pos:].strip() == "":
                break
            raise FormulaEvalError(f"语法错误：无法识别的字符 {src[pos:pos + 1]!r}（位置 {pos}）")
        pos = m.end()
        if m.lastgroup == "num":
            tokens.append(("num", m.group("num")))
        elif m.lastgroup == "str":
            tokens.append(("str", m.group("str")))
        elif m.lastgroup == "name":
            tokens.append(("name", m.group("name")))
        elif m.lastgroup == "hash":
            tokens.append(("op", "#"))
        else:
            tokens.append(("op", m.group("op")))
    return tokens


# ---------------------------------------------------------------- 语法（递归下降）

# 优先级从低到高：OR < AND < NOT < 比较 < 加减 < 乘除 < 一元 < 原子
class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.toks = tokens
        self.i = 0

    def _peek_op(self, *ops: str) -> str | None:
        if self.i < len(self.toks):
            kind, val = self.toks[self.i]
            if kind == "op" and val in ops:
                return val
        return None

    def _eat_op(self, *ops: str) -> str | None:
        v = self._peek_op(*ops)
        if v is not None:
            self.i += 1
        return v

    def parse(self):
        node = self._or()
        if self.i != len(self.toks):
            raise FormulaEvalError(f"语法错误：多余的内容 {self.toks[self.i]!r}")
        return node

    def _or(self):
        node = self._and()
        while self._eat_op("||") or self._word_op("OR"):
            node = ("bin", "or", node, self._and())
        return node

    def _word_op(self, word: str) -> bool:
        if self.i < len(self.toks):
            kind, val = self.toks[self.i]
            if kind == "name" and val.upper() == word:
                self.i += 1
                return True
        return False

    def _and(self):
        node = self._not()
        while self._eat_op("&&") or self._word_op("AND"):
            node = ("bin", "and", node, self._not())
        return node

    def _not(self):
        if self._eat_op("!") or self._word_op("NOT"):
            return ("un", "not", self._not())
        return self._cmp()

    def _cmp(self):
        node = self._add()
        op = self._peek_op(*_CMP_OPS)
        if op:
            self.i += 1
            node = ("bin", op, node, self._add())
        return node

    def _add(self):
        node = self._mul()
        while True:
            op = self._eat_op("+", "-")
            if not op:
                return node
            node = ("bin", op, node, self._mul())

    def _mul(self):
        node = self._unary()
        while True:
            op = self._eat_op("*", "/")
            if not op:
                return node
            node = ("bin", op, node, self._unary())

    def _unary(self):
        if self._eat_op("-"):
            return ("un", "-", self._unary())
        if self._eat_op("+"):
            return self._unary()
        return self._atom()

    def _atom(self):
        if self.i >= len(self.toks):
            raise FormulaEvalError("语法错误：表达式意外结束")
        kind, val = self.toks[self.i]
        if kind == "num":
            self.i += 1
            return ("num", float(val))
        if kind == "name":
            self.i += 1
            if self._peek_op("."):  # 批次 9b 跨公式引用 公式名.线名
                self.i += 1
                if self.i < len(self.toks) and self.toks[self.i][0] == "name":
                    line = self.toks[self.i][1]
                    self.i += 1
                    if self._peek_op("#"):  # 组合语法 公式名.线名#周期
                        self.i += 1
                        if self.i < len(self.toks) and self.toks[self.i][0] == "name":
                            period = self.toks[self.i][1]
                            self.i += 1
                            return ("xref_cross", val.upper(), line.upper(),
                                    period.upper())
                        raise FormulaEvalError(f"语法错误：{val}.{line}# 后缺少周期名")
                    return ("xref", val.upper(), line.upper())
                raise FormulaEvalError(f"语法错误：{val}. 后缺少线名")
            if self._peek_op("#"):  # 跨周期引用 HIGH#DAY
                self.i += 1
                if self.i < len(self.toks) and self.toks[self.i][0] == "name":
                    period = self.toks[self.i][1]
                    self.i += 1
                    return ("cross", val.upper(), period.upper())
                raise FormulaEvalError(f"语法错误：{val}# 后缺少周期名")
            if self._peek_op("("):
                self.i += 1
                args: list = []
                if not self._peek_op(")"):
                    args.append(self._or())
                    while self._eat_op(","):
                        args.append(self._or())
                if not self._eat_op(")"):
                    raise FormulaEvalError(f"语法错误：{val}(...) 括号未闭合")
                return ("call", val.upper(), args)
            return ("id", val)
        if kind == "str":  # 通达信原生双引号跨公式引用 "公式名.线名[#周期]"
            self.i += 1
            inner = val[1:-1]
            if "." not in inner:
                raise FormulaEvalError(
                    f"语法错误：跨公式引用 {val!r} 需 公式名.线名 形式")
            form, _, line_part = inner.partition(".")
            if not form or not line_part:
                raise FormulaEvalError(
                    f"语法错误：跨公式引用 {val!r} 需 公式名.线名 形式")
            if "#" in line_part:
                line, _, period = line_part.partition("#")
                if not line or not period:
                    raise FormulaEvalError(
                        f"语法错误：跨公式引用 {val!r} 周期格式错误")
                return ("xref_cross", form.upper(), line.upper(),
                        period.upper())
            return ("xref", form.upper(), line_part.upper())
        if self._eat_op("("):
            node = self._or()
            if not self._eat_op(")"):
                raise FormulaEvalError("语法错误：括号未闭合")
            return node
        raise FormulaEvalError(f"语法错误：意外的符号 {val!r}")


# ---------------------------------------------------------------- 求值

def _as_bool(v: Any) -> Any:
    """值 → 布尔（Series[Boolean] null→False / 标量 bool）。"""
    if isinstance(v, pl.Series):
        if v.dtype == pl.Boolean:
            return v.fill_null(False)
        return (v != 0).fill_null(False)
    return v != 0


def _bool_f64(b: Any) -> Any:
    """布尔 → 1.0/0.0（"无信号"即 0）。"""
    if isinstance(b, pl.Series):
        return b.cast(pl.Float64)
    return 1.0 if b else 0.0


def _broadcast(v: Any, n: int) -> pl.Series:
    if isinstance(v, pl.Series):
        return v
    return pl.Series("v", [v] * n, dtype=pl.Float64)


def _window(v: Any, fname: str) -> int:
    if isinstance(v, pl.Series):
        raise FormulaEvalError(f"{fname} 的周期参数必须为常量，不能是序列")
    n = int(v)
    if n < 0:
        raise FormulaEvalError(f"{fname} 的周期参数不能为负数: {n}")
    return n


def _period_window_start(ts, period_key: str):
    """批次 9a：上级 bar 时间戳 → 其覆盖区间起点（日历归算）。

    免疫上级周期时间戳标法差异（TQ 周线实证为周五标，TDX 图上习惯周一标）：
    周线一律归算到周一 00:00、月线归算到当月 1 日、日线归算到当日 00:00、
    分钟线时间戳即 bar 起点不变。
    """
    if period_key == "1w":
        return ts - timedelta(days=ts.weekday())
    if period_key == "1mon":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period_key == "1d":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts


def _align_cross(base_df: pl.DataFrame, higher_df: pl.DataFrame,
                 no_leak: bool, period_key: str) -> list:
    """批次 9a 对齐器：base bar → 所属上级 bar 行号。

    由上级 df 时间戳序列构造区间 [t_i, t_{i+1})，base bar datetime 落入哪根
    区间即「所属」该上级 bar；末根区间延至 +∞。输出长度恒等于 base_df 高度。

    - base bar 早于上级首根 → None（预热不足，无信号常态）
    - no_leak=False（默认 TDX 语义）：取所属 bar 整根终值（周内含未来引用）
    - no_leak=True：所属上级 bar 未完成（区间内非末根恒未完成；末根视为进行中）
      → 取上一根已完成 bar，无则 None（引用自身周期时即 REF(X,1) 语义）

    上级区间按 _period_window_start 日历归算（WEEK/MONTH 免疫时间戳标法差异）。
    假定两侧 datetime 列均升序（回测 klines 与 TQ 输出口径）。
    """
    if "datetime" not in base_df.columns or "datetime" not in higher_df.columns:
        raise FormulaEvalError("跨周期引用对齐需要两侧数据含 datetime 列")
    base_ts = base_df["datetime"].to_list()
    hi_ts = [_period_window_start(t, period_key)
             for t in higher_df["datetime"].to_list()]
    m = len(hi_ts)
    out: list = [None] * len(base_ts)
    if m == 0:
        return out
    j = 0
    for i, t in enumerate(base_ts):
        while j + 1 < m and hi_ts[j + 1] <= t:
            j += 1
        if hi_ts[j] > t:
            continue  # 早于上级首根 → 保持 None
        if no_leak:
            out[i] = j - 1 if j >= 1 else None
        else:
            out[i] = j
    return out


class _Evaluator:
    def __init__(self, df: pl.DataFrame, period: str | None = None,
                 index_df: pl.DataFrame | None = None,
                 period_frames: dict[str, pl.DataFrame] | None = None,
                 no_leak: bool = False,
                 xref_resolver=None):
        self.df = df
        self.n = df.height
        # 批次 8：PERIOD 周期编号上下文（None → PERIOD 求值报错）
        self.period = period
        # 批次 8：大盘引用族注入的指数 OHLCV（行数须与 df 一致；None → INDEX* 报错）
        self.index_df = index_df
        # 批次 9a：跨周期引用上级周期数据 {周期键: df}（None → FIELD#PERIOD 报错）
        self.period_frames = period_frames
        # 批次 9a：无泄露开关（默认 false=TDX 语义，所属上级 bar 未完成取其整根
        # 终值，周内含未来引用；true=仅引用已完成上级 bar）
        self.no_leak = no_leak
        # 批次 9b：跨公式引用解析器 callable(formula_name, period_key|None)
        #   → {线名: Series}（period_key=None 为当前周期；None → NAME.LINE 报错）。
        #   memo/环检测/被引公式缺失校验由接线层负责
        self.xref_resolver = xref_resolver

    def field(self, name: str) -> pl.Series:
        col = _FIELD_ALIAS.get(name.upper())
        if col is None or col not in self.df.columns:
            return None
        return self.df[col].cast(pl.Float64)

    def _datetime_col(self) -> pl.Series:
        """时间函数的 datetime 列（缺失显式报错；Date 类型提升为 Datetime）。"""
        if "datetime" not in self.df.columns:
            raise FormulaEvalError(
                "时间函数需要喂入数据含 datetime 列（回测 klines 自带）")
        dt = self.df["datetime"]
        if dt.dtype == pl.Date:
            dt = dt.cast(pl.Datetime("us"))
        return dt

    def _bare_func(self, name: str) -> pl.Series:
        """裸标识符函数（无括号）：位置 / 时间分量 / K线状态。"""
        n = self.n
        if name == "CURRBARSCOUNT":
            return pl.Series("v", [float(n - i) for i in range(n)], dtype=pl.Float64)
        if name in ("TOTALBARSCOUNT", "USEDDATANUM"):
            # USEDDATANUM 暂按同值（TQVERIFY6 K12 实证）
            return pl.Series("v", [float(n)] * n, dtype=pl.Float64)
        if name == "ISLASTBAR":
            return pl.Series("v", [0.0] * (n - 1) + [1.0], dtype=pl.Float64)
        # ---- 时间分量族（依赖 df 的 datetime 列）
        if name in ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE"):
            dt = self._datetime_col()
            fn = {"YEAR": "year", "MONTH": "month", "DAY": "day",
                  "HOUR": "hour", "MINUTE": "minute"}[name]
            return getattr(dt.dt, fn)().cast(pl.Float64)
        if name == "WEEKDAY":
            # TDX 口径：周日=0，周一=1 … 周六=6（ISO Mon=1..Sun=7 → %7）
            return (self._datetime_col().dt.weekday() % 7).cast(pl.Float64)
        if name in ("FROMOPEN", "TOTALFZNUM"):
            # 距开盘分钟数：9:30 前 0；上午 t-570；午休冻结 120；
            # 下午跳过午休按连续交易时间计（13:00=120，15:00=240）。
            # TOTALFZNUM 词典描述与 FROMOPEN 相同（依赖 FROMOPEN），暂按同值
            #（TQVERIFY6 K10 实证）。午休段口径待实证，日线不受影响。
            # 纯 Series 算术实现（pl.when 传纯 Series 会降级为 lit Expr，
            # 收集期 supertype 解析失败）
            dt = self._datetime_col()
            t = dt.dt.hour().cast(pl.Int64) * 60 + dt.dt.minute().cast(pl.Int64)
            pm = (t >= 780).cast(pl.Float64)
            lunch = ((t > 690) & (t < 780)).cast(pl.Float64)
            # 日线 bar 时间戳为 00:00（TQ 日线索引口径）：TDX 按整日上下文
            # 返回全天 240 分钟（TQVERIFY6 K09/K10 实证）
            return (pm * (t - 660).cast(pl.Float64)
                    + lunch * 120.0
                    + (1.0 - pm - lunch) * (t - 570).clip(0).cast(pl.Float64)
                    + (t == 0).cast(pl.Float64) * 240.0)
        if name == "DATE":
            # (year-1900)*10000 + month*100 + day（TDX「1900 以来的年月日」口径，
            # TQVERIFY5 J05 在线实证）
            # dt.year/month/day 返回 Int8/Int32，先统一转 Int64 防溢出
            dt = self._datetime_col()
            return ((dt.dt.year().cast(pl.Int64) - 1900) * 10000
                    + dt.dt.month().cast(pl.Int64) * 100
                    + dt.dt.day().cast(pl.Int64)).cast(pl.Float64)
        if name == "TIME":
            # hour*10000 + minute*100 + second（TQVERIFY5 J08 在线实证日线口径）
            # dt.hour/minute/second 返回 Int8，先转 Int64 防溢出
            dt = self._datetime_col()
            return (dt.dt.hour().cast(pl.Int64) * 10000
                    + dt.dt.minute().cast(pl.Int64) * 100
                    + dt.dt.second().cast(pl.Int64)).cast(pl.Float64)
        # ---- 批次 8：时间周期 / 周期常量与编号 / 大盘引用 / null 占位
        if name == "WEEKOFYEAR":
            # TQVERIFY8B 实证（N01，000001.SZ 全历史 8471 根）：
            # - 周一为一周起始；周日 bar 归入下一周（+1 天后再算，
            #   1991-1993 周日调休交易日 +1 周实证）
            # - 第 1 周 = 含 1 月 1 日的整周（周一~周日）；但 1/1 恰为周日时，
            #   该周日归上一年末周、第 1 周从 1/2（周一）开始
            #   （1995/2006/2012/2017/2023 五个 1/1 周日年份整年 -1 实证）
            # w = (rd - rd 所年锚点周一).days // 7 + 1
            d = self._datetime_col().cast(pl.Date)
            # 周日 → 次日（下周一）；polars weekday: 周一=1…周日=7
            rd = (d.cast(pl.Int32) + (d.dt.weekday() == 7).cast(pl.Int32)).cast(pl.Date)
            jan1 = rd.dt.replace(month=1, day=1)
            # 锚点周一 = jan1 前移 (weekday-1) 天；1/1 恰为周日 → 锚点改 1/2
            #（jan1-6+7 = jan1+1，delta 恒 7，纯 Series 算术——when/then 会
            # 把 Series 提升为 Expr 破坏求值上下文）
            anchor = (jan1.cast(pl.Int32) - (jan1.dt.weekday().cast(pl.Int32) - 1)
                      + (jan1.dt.weekday() == 7).cast(pl.Int32) * 7)
            return ((rd.cast(pl.Int32) - anchor) // 7 + 1).cast(pl.Float64)
        if name == "PERIOD":
            # 当前 K 线周期编号（1m=1…1mon=8），需 evaluate_outputs 注入 period
            code = _PERIOD_CODES.get(self.period)
            if code is None:
                raise FormulaEvalError(
                    "PERIOD 需要周期上下文（evaluate_outputs(period=...)，"
                    f"回测接线自动注入），当前 period={self.period!r}")
            return pl.Series("v", [float(code)] * self.n, dtype=pl.Float64)
        if name in _INDEX_FIELDS:
            if self.index_df is None:
                raise FormulaEvalError(
                    f"{name} 需要大盘指数数据（回测接线按个股市场注入 index_frames）")
            if self.index_df.height != self.n:
                raise FormulaEvalError(
                    f"{name} 指数数据行数({self.index_df.height})与"
                    f"个股({self.n})不一致，接线层须先按时间轴对齐")
            col = _INDEX_FIELDS[name]
            if col not in self.index_df.columns:
                raise FormulaEvalError(f"{name} 指数数据缺少 {col} 列")
            return self.index_df[col].cast(pl.Float64)
        if name == "DRAWNULL":
            # 恒 null 占位（配合 IF 控制绘制/信号输出）
            return pl.Series("v", [None] * self.n, dtype=pl.Float64)
        # BARSTATUS：第一根=1，最后一根=2，中间=0
        out = [0.0] * n
        if n:
            out[0] = 1.0
            out[-1] = 2.0
        return pl.Series("v", out, dtype=pl.Float64)

    def _cross_series(self, field: str, suffix: str) -> pl.Series:
        """批次 9a：FIELD#PERIOD 跨周期引用求值。

        查 period_frames 取上级 df（兼容 60m/1h 双键；引用自身周期且未注入时
        用喂入 df 恒等对齐）→ 对齐器转回 base 周期序列 → 取目标列。
        """
        keys = _CROSS_PERIOD_MAP.get(suffix)
        if keys is None:
            raise FormulaEvalError(
                f"跨周期引用 {field}#{suffix}：未知周期后缀"
                f"（支持 {'/'.join(_CROSS_PERIOD_MAP)}）")
        hf, hf_key = None, None
        for k in keys:
            if self.period_frames and k in self.period_frames:
                hf, hf_key = self.period_frames[k], k
                break
        if hf is None and self.period is not None and self.period in keys:
            hf, hf_key = self.df, self.period
        if hf is None:
            raise FormulaEvalError(
                f"跨周期引用 {field}#{suffix} 缺少上级周期数据"
                f"（回测接线未提供 {'/'.join(keys)} 周期 K 线）")
        if self.period is not None:
            base_m = _PERIOD_MINUTES.get(self.period)
            tgt_m = _PERIOD_MINUTES.get(hf_key)
            if base_m is not None and tgt_m is not None and base_m > tgt_m:
                raise FormulaEvalError(
                    f"跨周期引用 {field}#{suffix} 不支持引用更细周期"
                    f"（当前 {self.period}，目标 {hf_key}；1:N 无法对齐）")
        col = _FIELD_ALIAS.get(field.upper())
        if col is None or col not in hf.columns:
            raise FormulaEvalError(
                f"跨周期引用 {field}#{suffix}：上级数据缺少 {field} 列")
        if col == "Volume":
            return self._cross_volume_series(hf, hf_key)
        idx = _align_cross(self.df, hf, self.no_leak, hf_key)
        hi = hf[col].cast(pl.Float64).to_list()
        return pl.Series("v", [None if j is None else hi[j] for j in idx],
                         dtype=pl.Float64)

    def _cross_volume_series(self, hf: pl.DataFrame, hf_key: str) -> pl.Series:
        """VOL 跨周期特殊语义（TQVERIFY9 在线实证 L05）：TDX 的 V#WEEK/V#MONTH
        在 base 周期上为「上级窗口内逐日累计」（不含未来，周一=当日量、周五=周
        总量），而非上级 bar 终值；价格字段（C/O/H/L）才是整根终值口径。

        no_leak=True：所属窗口未完成 → 取上一根已完成上级 bar 的总量（上级 df
        的 Volume 本身即完整周期总量）。
        """
        base_ts = self.df["datetime"].to_list()
        base_vol = self.df["Volume"].cast(pl.Float64).to_list()
        hi_ts = [_period_window_start(t, hf_key)
                 for t in hf["datetime"].to_list()]
        m = len(hi_ts)
        out: list = [None] * len(base_ts)
        if m == 0:
            return pl.Series("v", out, dtype=pl.Float64)
        if self.no_leak:
            hf_vol = hf["Volume"].cast(pl.Float64).to_list()
            j = 0
            for i, t in enumerate(base_ts):
                while j + 1 < m and hi_ts[j + 1] <= t:
                    j += 1
                if hi_ts[j] > t:
                    continue  # 早于上级首根 → None
                out[i] = hf_vol[j - 1] if j >= 1 else None
            return pl.Series("v", out, dtype=pl.Float64)
        j = 0
        acc, prev_win = 0.0, None
        for i, t in enumerate(base_ts):
            while j + 1 < m and hi_ts[j + 1] <= t:
                j += 1
            if hi_ts[j] > t:
                continue  # 早于上级首根 → None
            w = hi_ts[j]
            if w != prev_win:
                acc, prev_win = 0.0, w
            acc += base_vol[i]
            out[i] = acc
        return pl.Series("v", out, dtype=pl.Float64)

    def _xref_series(self, form: str, line: str, period_suffix: str | None) -> pl.Series:
        """批次 9b：公式名.线名[#周期] 跨公式引用求值。

        经 xref_resolver 在同 code 的目标周期上求值被引公式 → 取输出线；
        带 #周期 后缀时对齐器折回 base 周期（与 FIELD#PERIOD 同语义）。
        """
        if self.xref_resolver is None:
            raise FormulaEvalError(
                f"跨公式引用 {form}.{line}：当前求值上下文未提供 xref_resolver"
                f"（回测接线未启用跨公式引用）")
        if period_suffix is None:
            outs = self.xref_resolver(form, None)
        else:
            keys = _CROSS_PERIOD_MAP.get(period_suffix)
            if keys is None:
                raise FormulaEvalError(
                    f"跨公式引用 {form}.{line}#{period_suffix}：未知周期后缀"
                    f"（支持 {'/'.join(_CROSS_PERIOD_MAP)}）")
            hf, hf_key = None, None
            for k in keys:
                if self.period_frames and k in self.period_frames:
                    hf, hf_key = self.period_frames[k], k
                    break
            if hf is None and self.period is not None and self.period in keys:
                hf, hf_key = self.df, self.period
            if hf is None:
                raise FormulaEvalError(
                    f"跨公式引用 {form}.{line}#{period_suffix} 缺少上级周期数据"
                    f"（回测接线未提供 {'/'.join(keys)} 周期 K 线）")
            if self.period is not None:
                base_m = _PERIOD_MINUTES.get(self.period)
                tgt_m = _PERIOD_MINUTES.get(hf_key)
                if base_m is not None and tgt_m is not None and base_m > tgt_m:
                    raise FormulaEvalError(
                        f"跨公式引用 {form}.{line}#{period_suffix} 不支持引用更细周期"
                        f"（当前 {self.period}，目标 {hf_key}；1:N 无法对齐）")
            outs = self.xref_resolver(form, hf_key)
        if outs is None or line not in outs:
            raise FormulaEvalError(f"跨公式引用 {form}.{line}：公式 {form} 无输出线 {line}")
        s = outs[line].cast(pl.Float64)
        if period_suffix is None:
            return s
        idx = _align_cross(self.df, hf, self.no_leak, hf_key)
        hi = s.to_list()
        return pl.Series("v", [None if j is None else hi[j] for j in idx],
                         dtype=pl.Float64)

    def eval(self, node, env: dict[str, Any]) -> Any:
        tag = node[0]
        if tag == "num":
            return node[1]
        if tag == "cross":
            return self._cross_series(node[1], node[2])
        if tag == "xref":
            return self._xref_series(node[1], node[2], None)
        if tag == "xref_cross":
            return self._xref_series(node[1], node[2], node[3])
        if tag == "id":
            name = node[1]
            if name.upper() in env:
                return env[name.upper()]
            f = self.field(name)
            if f is not None:
                return f
            if name.upper() in _BARE_FUNCS:
                return self._bare_func(name.upper())
            raise FormulaEvalError(f"未定义的变量或字段: {name}")
        if tag == "un":
            v = self.eval(node[2], env)
            if node[1] == "-":
                return -v
            return _bool_f64(~_as_bool(v))  # not
        if tag == "bin":
            op = node[1]
            l = self.eval(node[2], env)
            r = self.eval(node[3], env)
            if op in ("and", "or"):
                lb, rb = _as_bool(l), _as_bool(r)
                b = (lb & rb) if op == "and" else (lb | rb)
                return _bool_f64(b)
            if op in _CMP_OPS:
                py_op = {"=": "__eq__", "==": "__eq__", "!=": "__ne__", "<>": "__ne__",
                         ">": "__gt__", "<": "__lt__", ">=": "__ge__", "<=": "__le__"}[op]
                return _cmp_f64(l, py_op, r)
            if op == "+":
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                # TDX 除零得无效值 → null（标量除零也按 null 语义返回整列 null 太激进，
                # 标量直接抛错提示公式问题）
                if isinstance(l, pl.Series):
                    return (l / r).fill_nan(None)
                if isinstance(r, pl.Series):
                    return (l / r).fill_nan(None)
                if r == 0:
                    raise FormulaEvalError("除数为 0")
                return l / r
            raise FormulaEvalError(f"不支持的运算符: {op}")
        if tag == "call":
            return self._call(node[1], [self.eval(a, env) for a in node[2]], env)
        raise FormulaEvalError(f"内部错误：未知节点 {tag}")

    def _call(self, name: str, args: list, env: dict[str, Any]) -> Any:
        try:
            # 裸标识符函数的括号空参形式兼容（如 YEAR()）
            if name in _BARE_FUNCS and not args:
                return self._bare_func(name)
            if name == "MA":
                x, n = args
                return _ma_tdx(_require_series(x, name), _window(n, name))
            if name in ("EMA", "EXPMA"):
                x, n = args
                return _require_series(x, name).ewm_mean(
                    span=_window(n, name), adjust=False)
            if name == "REF":
                x, n = args
                xs = _require_series(x, name)
                if isinstance(n, pl.Series):
                    # 批次 7：动态偏移（如 REF(C,BARSLAST(cond))，TDX 倒推写法）
                    return _ref_dyn(xs, n.cast(pl.Int64, strict=False))
                return xs.shift(_window(n, name))
            if name in ("HHV", "LLV"):
                x, n = args
                x = _require_series(x, name)
                if isinstance(n, pl.Series):
                    # 批次 7：动态窗口（如 LLV(C,BARSLAST(cond)+1)）
                    return _hll_dyn(x, n.cast(pl.Int64, strict=False), name == "HHV")
                w = _window(n, name)
                if w == 0:
                    return x.cum_max() if name == "HHV" else x.cum_min()
                fn = x.rolling_max if name == "HHV" else x.rolling_min
                # TDX 部分窗口：窗口未满用已有数据（F21/F23 实证）
                return _rolling(fn, w, min_periods=1)
            if name == "CROSS":
                a, b = args
                if not isinstance(a, pl.Series) and not isinstance(b, pl.Series):
                    raise FormulaEvalError("CROSS 的参数必须是序列（如 CROSS(C, MA(C,5))）")
                n = a.len() if isinstance(a, pl.Series) else b.len()
                a_s, b_s = _broadcast(a, n), _broadcast(b, n)
                now = _cmp_f64(a_s, "__gt__", b_s)
                prev = _cmp_f64(a_s.shift(1), "__le__", b_s.shift(1))
                return now * prev  # 双方均为 0/1（null→0），相乘即 AND
            if name == "IF":
                c, a, b = args
                return _if(c, a, b)
            if name == "BARSLAST":
                (x,) = args
                return _barslast(_require_series(x, name))
            if name in ("SUM", "COUNT", "EVERY"):
                x, n = args
                x = _require_series(x, name)
                if name == "SUM" and isinstance(n, pl.Series):
                    # 批次 7：动态窗口（如 SUM(X,BARSLAST(cond))）；COUNT/EVERY 仍要求常量
                    return _sum_dyn(x, n.cast(pl.Int64, strict=False))
                w = _window(n, name)
                if name == "SUM":
                    return x.cum_sum() if w == 0 else _rolling(x.rolling_sum, w, min_periods=1)
                b = (x.fill_null(0) != 0).cast(pl.Float64)
                if name == "COUNT":
                    return b.cum_sum() if w == 0 else _rolling(b.rolling_sum, w, min_periods=1)
                return _every_series(b, w)
            if name == "LAST":
                x, a, b = args
                wa, wb = _window(a, name), _window(b, name)
                w = wa - wb + 1
                if w < 1:
                    raise FormulaEvalError(
                        f"LAST 参数需满足 A >= B（窗口 = A-B+1 根），实际 ({wa},{wb})")
                # TDX 语义：窗口 [i-A .. i-B] 共 A-B+1 根全真；空窗（i<B）真空真=1
                bx = (_require_series(x, name).fill_null(0) != 0).cast(pl.Float64)
                return _every_series(bx, w).shift(wb).fill_null(1.0)
            if name == "BARSLASTCOUNT":
                (x,) = args
                return _barslastcount(_require_series(x, name))
            if name == "FILTER":
                x, n = args
                return _filter(_require_series(x, name), _window(n, name))
            if name == "SMA":
                x, nn, m = args
                big, small = _window(nn, name), _window(m, name)
                if not 0 < small <= big:
                    raise FormulaEvalError(f"SMA 参数不合法: ({big},{small})，需 0 < M <= N")
                return _require_series(x, name).ewm_mean(alpha=small / big, adjust=False)
            if name == "DMA":
                x, a = args
                if isinstance(a, pl.Series):
                    raise FormulaEvalError("DMA 的平滑因子暂不支持序列（仅常量）")
                if not 0 < a <= 1:
                    raise FormulaEvalError(f"DMA 平滑因子需在 (0,1] 内: {a}")
                return _require_series(x, name).ewm_mean(alpha=a, adjust=False)
            if name in ("HHVBARS", "LLVBARS"):
                x, n = args
                return _bars_extreme(
                    _require_series(x, name), _window(n, name), name == "HHVBARS")
            if name == "VALUEWHEN":
                c, x = args
                return _valuewhen(c, x, self.n)
            if name == "CONST":
                (x,) = args
                return _const(_require_series(x, name))
            if name == "IFF":
                return self._call("IF", args, env)
            if name in ("ABS", "SQRT"):
                (x,) = args
                if isinstance(x, pl.Series):
                    return x.abs() if name == "ABS" else x.sqrt()
                return abs(x) if name == "ABS" else _sqrt_scalar(x)
            if name in ("MAX", "MIN"):
                a, b = args
                return _elementwise_maxmin(a, b, name)
            if name in ("STD", "STDP", "VAR", "VARP", "AVEDEV", "DEVSQ"):
                x, n = args
                return _stats_rolling(_require_series(x, name), name, _window(n, name))
            if name == "SLOPE":
                x, n = args
                return _slope(_require_series(x, name), _window(n, name))
            if name == "FORCAST":
                x, n = args
                return _linreg(_require_series(x, name), _window(n, name))
            if name == "EXIST":
                x, n = args
                b = (_require_series(x, name).fill_null(0) != 0).cast(pl.Float64)
                w = _window(n, name)
                # TDX 部分窗口（F20 实证）
                return b.cum_max() if w == 0 else _rolling(b.rolling_max, w, min_periods=1)
            if name in ("BARSSINCE", "BARSCOUNT"):
                (x,) = args
                return _bars_since(
                    _require_series(x, name), "since" if name == "BARSSINCE" else "count")
            if name == "LONGCROSS":
                a, b, nn = args
                if not isinstance(a, pl.Series) and not isinstance(b, pl.Series):
                    raise FormulaEvalError("LONGCROSS 的参数必须包含序列")
                m = a.len() if isinstance(a, pl.Series) else b.len()
                a_s, b_s = _broadcast(a, m), _broadcast(b, m)
                lt_prev = _cmp_f64(a_s, "__lt__", b_s).shift(1)
                # 截至 上根 的连续 w 根 a<b（窗口未满/含 null → 不成立）
                cond = _as_bool(_rolling(lt_prev.rolling_min, _window(nn, name)) == 1)
                now = _as_bool(_cmp_f64(a_s, "__gt__", b_s))
                return _bool_f64(cond & now)
            if name in ("NDAY", "UPNDAY", "DOWNNDAY"):
                if name == "NDAY":
                    x, y, n = args
                    op = "__gt__"
                else:
                    x, n = args
                    y = _require_series(x, name).shift(1)
                    op = "__gt__" if name == "UPNDAY" else "__lt__"
                gt = _cmp_f64(_broadcast(x, self.n), op, _broadcast(y, self.n))
                return _rolling(gt.rolling_min, _window(n, name))
            if name == "SUMBARS":
                x, a = args
                return _sumbars(_require_series(x, name), a)
            if name in ("LOG", "LN", "EXP", "SIGN", "SGN"):
                (x,) = args
                if isinstance(x, pl.Series):
                    if name == "LOG":
                        return x.log(10.0).fill_nan(None)
                    if name == "LN":
                        return x.log().fill_nan(None)
                    return x.exp() if name == "EXP" else x.sign()
                if name == "LOG":
                    return math.log10(x)
                if name == "LN":
                    return math.log(x)
                return math.exp(x) if name == "EXP" else float((x > 0) - (x < 0))
            if name == "POW":
                x, y = args
                if isinstance(x, pl.Series) and isinstance(y, pl.Series):
                    return (
                        pl.DataFrame({"a": x, "b": y})
                        .select(pl.col("a").pow(pl.col("b")))
                        .to_series(0)
                    )
                if isinstance(x, pl.Series):
                    return x.pow(y)
                if isinstance(y, pl.Series):
                    return (
                        pl.DataFrame({"y": y})
                        .select(pl.lit(x, dtype=pl.Float64).pow(pl.col("y")))
                        .to_series(0)
                    )
                return x ** y
            if name == "WMA":
                x, n = args
                return _wma(_require_series(x, name), _window(n, name))
            # ------------------------------------------------ 扩展批次 3
            if name in ("SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN"):
                (x,) = args
                if isinstance(x, pl.Series):
                    if name in ("ASIN", "ACOS"):
                        # TDX 对定义域外（|x|>1）夹到边界，而非返回 null
                        x = x.clip(-1.0, 1.0)
                    fn = {"SIN": "sin", "COS": "cos", "TAN": "tan",
                          "ASIN": "arcsin", "ACOS": "arccos", "ATAN": "arctan"}[name]
                    return getattr(x, fn)().fill_nan(None)
                if name in ("ASIN", "ACOS"):
                    x = max(-1.0, min(1.0, x))
                fn = {"SIN": math.sin, "COS": math.cos, "TAN": math.tan,
                      "ASIN": math.asin, "ACOS": math.acos, "ATAN": math.atan}[name]
                try:
                    return float(fn(x))
                except ValueError:
                    return None
            if name in ("CEILING", "FLOOR", "INTPART", "FRACPART", "ROUND", "REVERSE"):
                (x,) = args
                if isinstance(x, pl.Series):
                    if name == "CEILING":
                        return x.ceil()
                    if name == "FLOOR":
                        return x.floor()
                    if name == "INTPART":
                        # 向零取整（trunc），区别于 FLOOR
                        return x.cast(pl.Int64, strict=False).cast(pl.Float64)
                    if name == "FRACPART":
                        return x - x.cast(pl.Int64, strict=False).cast(pl.Float64)
                    if name == "ROUND":
                        return _round_half_up(x, 1.0)
                    return -x  # REVERSE
                if name == "CEILING":
                    return float(math.ceil(x))
                if name == "FLOOR":
                    return float(math.floor(x))
                if name == "INTPART":
                    return float(math.trunc(x))
                if name == "FRACPART":
                    return float(x - math.trunc(x))
                if name == "ROUND":
                    x = _tdx_fixed2_scalar(x)
                    return float(math.copysign(math.floor(abs(x) + 0.5), x))
                return -x
            if name == "ROUND2":
                x, n = args
                k = 10.0 ** _window(n, name)
                if isinstance(x, pl.Series):
                    return _round_half_up(x, k)
                x = _tdx_fixed2_scalar(x)
                return float(math.copysign(math.floor(abs(x) * k + 0.5), x) / k)
            if name == "MOD":
                a, b = args
                # fmod 语义：a - trunc(a/b)*b（结果符号跟随被除数，与 C 一致）
                # TDX 价格 2 位小数定点，被除数先固定到 2 位（实证 H13）
                if isinstance(a, pl.Series) or isinstance(b, pl.Series):
                    m = a.len() if isinstance(a, pl.Series) else b.len()
                    a_s, b_s = _broadcast(a, m), _broadcast(b, m)
                    if isinstance(a, pl.Series):
                        a_s = _tdx_fixed2(a_s)
                    else:
                        a_s = _broadcast(_tdx_fixed2_scalar(a), m)
                    q = (a_s / b_s).cast(pl.Int64, strict=False).cast(pl.Float64)
                    return (a_s - q * b_s).fill_nan(None)
                if b == 0:
                    raise FormulaEvalError("MOD 的除数为 0")
                return float(math.fmod(_tdx_fixed2_scalar(a), b))
            if name in ("BETWEEN", "RANGE"):
                a, b, c = args
                # BETWEEN 闭区间 B<=A<=C；RANGE 开区间 B<A<C
                lo_op, hi_op = ("__ge__", "__le__") if name == "BETWEEN" else ("__gt__", "__lt__")
                return _cmp_f64(a, lo_op, b) * _cmp_f64(a, hi_op, c)
            if name == "NOT":
                (x,) = args
                return _bool_f64(~_as_bool(x))
            if name in ("HOD", "LOD"):
                x, n = args
                return _rank_in_window(
                    _require_series(x, name), _window(n, name), name == "HOD")
            if name in ("COVAR", "RELATE"):
                x, y, n = args
                return _covar_relate(_require_series(x, name), _require_series(y, name),
                                     _window(n, name), name)
            if name == "MEMA":
                x, n = args
                w = _window(n, name)
                if w < 1:
                    raise FormulaEvalError(f"MEMA 的周期参数需 >= 1: {w}")
                # 前 N-1 根 null，Y[N-1]=前 N 根 SMA，之后 Y=(X+(N-1)Y')/N
                return _mema_expmema(_require_series(x, name), w, "MEMA")
            if name == "EXPMEMA":
                x, n = args
                w = _window(n, name)
                if w < 1:
                    raise FormulaEvalError(f"EXPMEMA 的周期参数需 >= 1: {w}")
                # 前 N-1 根 null，Y[N-1]=前 N 根 SMA，之后 Y=(2X+(N-1)Y')/(N+1)
                return _mema_expmema(_require_series(x, name), w, "EXPMEMA")
            # ------------------------------------------------ 扩展批次 4
            if name in ("FINDHIGH", "FINDLOW", "FINDHIGHBARS", "FINDLOWBARS"):
                x, n, m, t = args
                return _find_extreme(
                    _require_series(x, name), _window(n, name), _window(m, name),
                    _window(t, name), name)
            if name == "TMA":
                x, a, b = args
                return _tma(_require_series(x, name), float(a), float(b))
            if name == "AMA":
                x, a = args
                return _ama(_require_series(x, name), float(a))
            if name == "BARSLASTS":
                x, n = args
                return _barslasts(_require_series(x, name), _window(n, name))
            if name == "BARSSINCEN":
                x, n = args
                return _barssincen(_require_series(x, name), _window(n, name))
            if name == "EXISTR":
                x, a, b = args
                return _existr(_require_series(x, name), _window(a, name), _window(b, name))
            if name == "REFV":
                x, a = args
                # 平滑处理：前段越界填充首个有效值（I10 实证：bar0-4 TDX 返 C[0]）
                return _require_series(x, name).shift(_window(a, name)).backward_fill()
            if name == "BACKSET":
                x, n = args
                return _backset(_require_series(x, name), _window(n, name))
            if name == "FILTERX":
                x, n = args
                return _filterx(_require_series(x, name), _window(n, name))
            if name == "SUMBARSX":
                x, a = args
                return _sumbarsx(_require_series(x, name), a)
            if name == "SAR":
                n, s, m = args
                return _sar(self.df, _window(n, name), float(s), float(m))
            if name == "SARTURN":
                n, s, m = args
                return _sarturn(self.df, _window(n, name), float(s), float(m))
            # ------------------------------------------------ 扩展批次 6
            if name in ("ZTPRICE", "DTPRICE"):
                x, a = args
                # 涨/跌停价 = 精确十进制四舍五入(X*(1±A), 2)（TQVERIFY6 K05/K06
                # 在线实证：.xx5 边界亦进位，且不做 ROUND 族的 3 位截断预处理）。
                # 整数定点实现：价格转分、系数转千分比，规避二进制浮点尾差；
                # 首根 REF 为 null → 传播
                xs = _require_series(x, name)
                k = 1.0 + float(a) if name == "ZTPRICE" else 1.0 - float(a)
                cents = ((xs * 100.0).round(0).cast(pl.Int64) * round(k * 1000)
                         + 500) // 1000
                # /100 会被 polars 优化为乘倒数引入尾差（3.3000000000000003），
                # round(2) 吸附到分网格
                return (cents / 100).round(2).cast(pl.Float64)
            if name in ("TOPRANGE", "LOWRANGE"):
                (x,) = args
                return _range_span(_require_series(x, name), name == "TOPRANGE")
            if name == "MULAR":
                x, n = args
                return _mular(_require_series(x, name), _window(n, name))
            if name == "TFILT":
                x, d1, t1, d2, t2 = args
                return _tfilt(_require_series(x, name), self._datetime_col(),
                              int(d1), int(t1), int(d2), int(t2))
            if name == "REFDATE":
                x, d = args
                return _refdate(_require_series(x, name), self._datetime_col(), int(d))
            if name == "TIMETOSEC":
                (tm,) = args
                if isinstance(tm, pl.Series):
                    t = tm.cast(pl.Int64, strict=False)
                    return ((t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100
                            ).cast(pl.Float64)
                t = int(tm)
                sec = (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100
                return pl.Series("v", [float(sec)] * self.n, dtype=pl.Float64)
            if name == "IFN":
                c, a, b = args
                # 条件不成立取 A，成立取 B（= IF 交换分支，词典：条件不成立时取 A）
                return _if(c, b, a)
            # ------------------------------------------------ 扩展批次 8
            if name == "XMA":
                x, n = args
                return _xma(_require_series(x, name), _window(n, name))
            if name == "HHVLLV":
                x, t, n1, n2 = args
                return _hhvllv(_require_series(x, name), _window(t, name),
                               _window(n1, name), _window(n2, name))
            if name == "MEDIAN":
                x, mm, n = args
                return _median(_require_series(x, name), _window(mm, name),
                               _window(n, name))
            if name == "UPDOWN":
                (x,) = args
                return _updown(_require_series(x, name))
            if name == "MAX6":
                if len(args) != 6:
                    raise FormulaEvalError("MAX6 需要 6 个参数")
                cols = [_broadcast(a, self.n).fill_null(0.0).to_list() for a in args]
                return pl.Series("v", [max(vs) for vs in zip(*cols)], dtype=pl.Float64)
            if name == "NEWSAR":
                n, s = args
                # 实证口径（TQVERIFY8，000001.SZ 全历史 8471 根逐 bar 零差异）：
                # 步长 S/1000、无 af 上限；触发比较在当根递推值上（非前根 SAR）
                return _newsar(self.df, _window(n, name), float(s))
            if name == "TFILTER":
                a, b, n = args
                return _tfilter(_require_series(a, name), _require_series(b, name),
                                _window(n, name))
            if name == "TTFILTER":
                k0, k1, k2, k3, n = args
                return _ttfilter(
                    [_require_series(k0, name), _require_series(k1, name),
                     _require_series(k2, name), _require_series(k3, name)],
                    _window(n, name))
        except FormulaEvalError:
            raise
        except Exception as e:  # polars 内部异常包装，避免裸栈
            raise FormulaEvalError(f"{name} 求值失败: {e}") from e
        raise FormulaEvalError(
            f"未实现的函数: {name}（内置引擎当前支持: {'/'.join(_SUPPORTED_FUNCS)}）"
        )


def _rev(op: str) -> str:
    return {"__gt__": "__lt__", "__lt__": "__gt__", "__ge__": "__le__", "__le__": "__ge__",
            "__eq__": "__eq__", "__ne__": "__ne__"}[op]


def _cmp_f64(l: Any, py_op: str, r: Any) -> Any:
    """比较 → 1.0/0.0（null → 0）。标量在前序列在后时用反射运算符。"""
    if isinstance(l, pl.Series) or isinstance(r, pl.Series):
        if isinstance(r, pl.Series) and not isinstance(l, pl.Series):
            b = getattr(r, _rev(py_op))(l)
        else:
            b = getattr(l, py_op)(r)
        return _bool_f64(b.fill_null(False))
    return _bool_f64(getattr(l, py_op)(r))


def _require_series(v: Any, fname: str) -> pl.Series:
    if not isinstance(v, pl.Series):
        raise FormulaEvalError(f"{fname} 的第一个参数必须是序列（引用了行情字段或序列变量）")
    return v


def _if(c: Any, a: Any, b: Any) -> Any:
    cond = _as_bool(c)
    if not isinstance(cond, pl.Series):
        return a if cond else b
    n = cond.len()
    a_s, b_s = _broadcast(a, n), _broadcast(b, n)
    return a_s.zip_with(cond, b_s)


def _barslast(x: pl.Series) -> pl.Series:
    """上一次 X 非零距今的周期数；首次成立之前 → null（TDX 实证，非 0）。"""
    n = x.len()
    idx = pl.int_range(0, n, dtype=pl.Int64, eager=True)
    s = x.fill_null(0)
    last = (
        pl.DataFrame({"x": s, "i": idx})
        .select(
            pl.when(pl.col("x") != 0)
            .then(pl.col("i"))
            .otherwise(None)
            .forward_fill()
        )
        .to_series(0)
    )
    return (idx - last).cast(pl.Float64)  # last 为 null → 差为 null，保留


def _every_series(b: pl.Series, w: int) -> pl.Series:
    """窗口内全真 → 1.0/0.0（b 为 0/1 序列）；w=0 为历史 expanding。部分窗口。"""
    return b.cum_min() if w == 0 else _rolling(b.rolling_min, w, min_periods=1)


def _barslastcount(x: pl.Series) -> pl.Series:
    """自当根起往前连续非零的根数（当根成立=1）。"""
    idx = pl.int_range(0, x.len(), dtype=pl.Int64, eager=True)
    b = x.fill_null(0) != 0
    last_false = (
        pl.DataFrame({"b": b, "i": idx})
        .select(
            pl.when(~pl.col("b"))
            .then(pl.col("i"))
            .otherwise(None)
            .forward_fill()
        )
        .to_series(0)
    )
    return (
        pl.DataFrame({"i": idx, "lf": last_false})
        .select(
            pl.when(pl.col("lf").is_null())
            .then(pl.col("i") + 1)
            .otherwise(pl.col("i") - pl.col("lf"))
            .cast(pl.Float64)
        )
        .to_series(0)
    )


def _filter(x: pl.Series, n: int) -> pl.Series:
    """FILTER(X,N)：成立当根出信号，其后 N 根屏蔽（有状态，小循环实现）。"""
    flags = (x.fill_null(0) != 0).to_list()
    out = [0.0] * len(flags)
    i = 0
    while i < len(flags):
        if flags[i]:
            out[i] = 1.0
            i += n + 1
        else:
            i += 1
    return pl.Series("f", out, dtype=pl.Float64)


def _bars_extreme(x: pl.Series, w: int, is_high: bool) -> pl.Series:
    """HHVBARS/LLVBARS：w 周期内极值距今天的周期数（0=当根）。

    TDX 部分窗口语义（F24/F25 实证）：窗口未满时用已有数据出值。
    并列极值取最近一次（TDX 行为）。
    """
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    for i in range(n):
        lo = max(0, i - w + 1)
        win_raw = vals[lo : i + 1]
        # 含 null 则仅比较有效值；全 null → null
        win = [v for v in win_raw if v is not None and v == v]
        if not win:
            continue
        best = max(win) if is_high else min(win)
        for off, v in enumerate(reversed(win_raw)):
            if v is not None and v == best:
                out[i] = float(off)
                break
    return pl.Series("be", out, dtype=pl.Float64)


def _valuewhen(c: Any, x: Any, n: int) -> pl.Series:
    """最近一次条件成立时 X 的值（forward_fill）。"""
    cond = _as_bool(_broadcast(c, n))
    xs = _broadcast(x, n)
    return (
        pl.DataFrame({"c": cond, "x": xs})
        .select(
            pl.when(pl.col("c"))
            .then(pl.col("x"))
            .otherwise(None)
            .forward_fill()
        )
        .to_series(0)
        .cast(pl.Float64)
    )


def _const(x: pl.Series) -> pl.Series:
    """取最后一根的值广播为常数序列（无有效值 → 全 null）。"""
    valid = x.drop_nulls()
    if len(valid) == 0:
        return pl.Series("c", [None] * x.len(), dtype=pl.Float64)
    return pl.Series("c", [valid[-1]] * x.len(), dtype=pl.Float64)


def _sqrt_scalar(v: float) -> float:
    if v < 0:
        raise FormulaEvalError(f"SQRT 的参数不能为负数: {v}")
    return math.sqrt(v)


def _elementwise_maxmin(a: Any, b: Any, name: str) -> Any:
    """MAX/MIN（二元逐元素，TDX 语义；与 HHV/LLV 的窗口极值区分）。"""
    if isinstance(a, pl.Series) or isinstance(b, pl.Series):
        n = a.len() if isinstance(a, pl.Series) else b.len()
        fn = pl.max_horizontal if name == "MAX" else pl.min_horizontal
        return (
            pl.DataFrame({"a": _broadcast(a, n), "b": _broadcast(b, n)})
            .select(fn(pl.col("a"), pl.col("b")))
            .to_series(0)
        )
    return max(a, b) if name == "MAX" else min(a, b)


def _stats_rolling(x: pl.Series, kind: str, w: int) -> pl.Series:
    """滚动统计：STD（样本，÷N-1）、VAR（样本）、VARP（总体）、AVEDEV、DEVSQ。"""
    if kind == "STD":
        return _rolling(x.rolling_std, w)  # polars 默认 ddof=1 = 样本口径
    if kind == "STDP":
        # TDX 怪癖（H24 实证）：首个满窗口（bar w-1）返回 0.0，第 w 根起正常。
        # 内置将前 w 根置 null（null vs 0.0 仅 WARN，不误判 FAIL）。
        s = _rolling(x.rolling_std, w, ddof=0)
        head = [None] * min(w, s.len())
        return pl.Series("v", head + s.to_list()[w:], dtype=pl.Float64)
    if kind == "VAR":
        return _rolling(x.rolling_var, w)
    if kind == "VARP":
        # TDX 怪癖（G16 实证）：VARP 前 w 根输出 0.0，第 w+1 根起正常。
        # 内置将前 w 根置 null（null vs 0.0 仅 WARN，不误判 FAIL）。
        s = _rolling(x.rolling_var, w, ddof=0)
        head = [None] * min(w, s.len())
        return pl.Series("v", head + s.to_list()[w:], dtype=pl.Float64)
    if kind == "AVEDEV":
        # 不能嵌套两层 rolling（偏差列前 w-1 根 null 会叠加预热段），用单层窗口循环
        return _avedev(x, w)
    # DEVSQ = Σ(x-μ)² = 总体方差 * w（单层 rolling 恒等式）
    return _rolling(x.rolling_var, w, ddof=0) * w


def _slope(x: pl.Series, w: int) -> pl.Series:
    """SLOPE：N 周期线性回归斜率。

    用绝对下标 j 做回归变量（j = 窗口内相对位置 r + 常量，斜率不变）：
    cov = Σ(j·y)/w − j̄·ȳ，j̄ = i−(w−1)/2，var(j) 窗口内 = (w²−1)/12 常量。
    全部单层 rolling 实现（本环境 polars 无 rolling_cov）。w<2 → 全 null。
    """
    if w < 2:
        return pl.Series("v", [None] * x.len(), dtype=pl.Float64)
    n = x.len()
    idx = pl.Series("j", [float(i) for i in range(n)])
    sum_jy = _rolling((idx * x).rolling_sum, w).fill_nan(None)
    mean_y = _rolling(x.rolling_mean, w)
    mean_j = pl.Series("mj", [i - (w - 1) / 2.0 for i in range(n)])
    cov = sum_jy / w - mean_j * mean_y
    return (cov / ((w * w - 1) / 12.0)).cast(pl.Float64)


def _avedev(x: pl.Series, w: int) -> pl.Series:
    """AVEDEV：窗口内平均绝对偏差 mean(|x-μ|)。窗口含 null → null。O(n*w) 循环。"""
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    for i in range(w - 1, n):
        win = vals[i - w + 1 : i + 1]
        if any(v is None or v != v for v in win):
            continue
        m = sum(win) / w
        out[i] = sum(abs(v - m) for v in win) / w
    return pl.Series("v", out, dtype=pl.Float64)


def _linreg(x: pl.Series, w: int) -> pl.Series:
    """N 周期线性回归 FORCAST：回归线在当根（t=w-1）的拟合值 = ȳ + 斜率*(w-1)/2。

    与 _slope 同恒等式实现（绝对下标 j 做回归变量）。w<2 → 全 null。
    """
    if w < 2:
        return pl.Series("v", [None] * x.len(), dtype=pl.Float64)
    n = x.len()
    idx = pl.Series("j", [float(i) for i in range(n)])
    sum_jy = _rolling((idx * x).rolling_sum, w).fill_nan(None)
    mean_y = _rolling(x.rolling_mean, w)
    mean_j = pl.Series("mj", [i - (w - 1) / 2.0 for i in range(n)])
    b = (sum_jy / w - mean_j * mean_y) / ((w * w - 1) / 12.0)
    return mean_y + b * (w - 1) / 2.0


def _bars_since(x: pl.Series, mode: str) -> pl.Series:
    """BARSSINCE：首次非零距今（从未成立 → 全 null）；BARSCOUNT：首个有效值至今（0 起）。

    BARSCOUNT offset=0 为 TDX 实证（F10/F11）：首个有效 bar 计 0，而非 1。
    """
    n = x.len()
    if mode == "since":
        hits = (x.fill_null(0) != 0).arg_true()
        base, offset = (hits[0] if hits.len() else None), 0
    else:
        hits = x.is_not_null().arg_true()
        base, offset = (hits[0] if hits.len() else None), 0
    if base is None:
        return pl.Series("v", [None] * n, dtype=pl.Float64)
    return pl.Series(
        "v",
        [float(i - base + offset) if i >= base else None for i in range(n)],
        dtype=pl.Float64,
    )


def _sumbars(x: pl.Series, a: Any) -> pl.Series:
    """SUMBARS：由近及远累加 X 首次 ≥ A 所需周期数（含当根）。

    TDX 实证（F09，8471 根逐 bar 校验）：
    - 达成且 cnt ≥ 2 → 返 cnt（含当根）；
    - 达成且 cnt == 1（当根一根即 ≥ A）→ 特判返 0；
    - 历史不足（加完整历史仍 < A）→ 返已累加根数 i（bar0 返 0）。
    """
    if isinstance(a, pl.Series):
        raise FormulaEvalError("SUMBARS 的累计目标 A 必须为常量，不能是序列")
    xv = [0.0 if v is None or v != v else v for v in x.to_list()]
    out: list[float | None] = [None] * x.len()
    for i in range(x.len()):
        s, j, cnt = 0.0, i, 0
        while j >= 0 and s < a:
            s += xv[j]
            cnt += 1
            j -= 1
        if s >= a:
            out[i] = 0.0 if cnt == 1 else float(cnt)
        else:
            out[i] = float(i)
    return pl.Series("v", out, dtype=pl.Float64)


def _ma_tdx(x: pl.Series, w: int) -> pl.Series:
    """TDX MA：整数分定点算术复现 TDX 平局行为。

    TDX 内部价格按整数分（×100）存储，MA = 整数和 / (w·100)。
    数学平局 → float 精确相等，polars rolling_mean 在 MA5/MA10 平局点
    产生 ±1e-15 尾差导致 CROSS 提前一根触发；整数分定点后不符从 19 降到 2
    （剩余 2 处 bar1222/bar5990 为已记录环境差异，spread 明显为正但 TDX 未触发）。
    """
    cents = (x * 100).round(0)
    s = _rolling(cents.rolling_sum, w)
    return (s / (w * 100.0)).fill_nan(None)


def _wma(x: pl.Series, w: int) -> pl.Series:
    """WMA 线性加权均线（权重 1..N，最近最大）。窗口含 null → null。"""
    xv = x.to_list()
    total = w * (w + 1) / 2.0
    out: list[float | None] = [None] * x.len()
    for i in range(w - 1, x.len()):
        s, ok = 0.0, True
        for k in range(1, w + 1):
            v = xv[i - w + k]
            if v is None or v != v:
                ok = False
                break
            s += v * k
        if ok:
            out[i] = s / total
    return pl.Series("v", out, dtype=pl.Float64)


def _mema_expmema(x: pl.Series, w: int, kind: str) -> pl.Series:
    """MEMA/EXPMEMA：前 N-1 根 null，Y[N-1]=前 N 根 SMA，之后递推。

    TDX 实证（H27/H28）：首值为前 N 根简单均值（非首观测值），前 N-1 根不输出。
    MEMA:    Y = (X + (N-1)·Y') / N        (alpha=1/N)
    EXPMEMA: Y = (2X + (N-1)·Y') / (N+1)    (alpha=2/(N+1))
    """
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    if w < 1 or n < w:
        return pl.Series("v", out, dtype=pl.Float64)
    head = vals[:w]
    if any(v is None or v != v for v in head):
        return pl.Series("v", out, dtype=pl.Float64)
    sma = sum(head) / w
    out[w - 1] = sma
    if kind == "MEMA":
        a, b = 1.0 / w, (w - 1.0) / w
    else:
        a, b = 2.0 / (w + 1), (w - 1.0) / (w + 1)
    prev = sma
    for i in range(w, n):
        v = vals[i]
        if v is None or v != v:
            out[i] = None
            continue
        prev = a * v + b * prev
        out[i] = prev
    return pl.Series("v", out, dtype=pl.Float64)


def _tdx_fixed2(x: pl.Series) -> pl.Series:
    """模拟 TDX 2 位小数定点中间结果：截断到 3 位后舍入到 2 位（第 3 位 ≥7 进位）。

    TDX 价格为 2 位小数定点整数，与 1 位小数乘数（如 0.7）相乘得 3 位中间值，
    转回 2 位时采用「第 3 位 ≥7 进位、≤6 舍去」（实证 H11）。
    例：28.497→28.50、19.495→19.49、6.496→6.49。
    用整数位运算取第 3 位小数，避免浮点 frac 比较误差。
    """
    x1000 = x * 1000
    v3_int = x1000.floor()  # 截断到 3 位的整数形式（如 28497）
    d3 = v3_int % 10  # 第 3 位小数（0-9）
    scaled = (v3_int / 10).floor()  # 2 位小数的整数部分（如 2849）
    rounded = scaled + (d3 >= 7).cast(pl.Float64)
    return rounded / 100


def _tdx_fixed2_scalar(x: float) -> float:
    """_tdx_fixed2 的标量版：第 3 位小数 ≥ 7 进位，否则截断到 2 位。"""
    import math as _m
    v3_int = _m.floor(x * 1000)
    d3 = v3_int % 10
    scaled = _m.floor(v3_int / 10)
    return (scaled + (1.0 if d3 >= 7 else 0.0)) / 100.0


def _round_half_up(x: pl.Series, k: float) -> pl.Series:
    """TDX ROUND 四舍五入（half away from zero）：sign(x)·floor(|x|·k+0.5)/k。

    polars round 为银行家舍入（half-to-even，0.5→0、2.5→2），与 TDX 不一致，
    不可直接用。输入先经 _tdx_fixed2 模拟 TDX 2 位小数定点（实证 H11/H12）。
    末尾按 k 的位数 round 吸附：polars 的 /k 按乘倒数优化会引入浮点尾差
    （如 330/100 → 3.3000000000000003），floor 结果本就在 1/k 网格上，
    round(w) 只吸附尾差不改变真值。
    """
    x = _tdx_fixed2(x)
    w = round(math.log10(k))
    return ((x.sign() * (x.abs() * k + 0.5).floor()) / k).round(w)


def _rank_in_window(x: pl.Series, w: int, high: bool) -> pl.Series:
    """HOD/LOD：当根值在 N 周期窗口（含当根，部分窗口口径）内第几大/小（1 起）。

    严格大于（小于）当根的元素个数 + 1；并列极值同名次。w=0 为历史 expanding。
    """
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    for i in range(n):
        v = vals[i]
        if v is None or v != v:
            continue
        lo = 0 if w == 0 else max(0, i - w + 1)
        win = [u for u in vals[lo : i + 1] if u is not None and u == u]
        cnt = sum(1 for u in win if (u > v if high else u < v))
        out[i] = float(cnt + 1)
    return pl.Series("v", out, dtype=pl.Float64)


def _covar_relate(x: pl.Series, y: pl.Series, w: int, kind: str) -> pl.Series:
    """COVAR/RELATE：N 周期协方差 / 相关系数（全预热，窗口含 null → null）。

    COVAR 样本口径 ÷(N-1)（与 VAR 样本口径一致，对比验证校正）；
    RELATE = Σ(x-x̄)(y-ȳ) / √(Σ(x-x̄)²·Σ(y-ȳ)²)，常数窗分母 0 → null。
    """
    xs, ys = x.to_list(), y.to_list()
    n = len(xs)
    out: list[float | None] = [None] * n
    for i in range(w - 1, n):
        wx = xs[i - w + 1 : i + 1]
        wy = ys[i - w + 1 : i + 1]
        if any(v is None or v != v for v in wx) or any(v is None or v != v for v in wy):
            continue
        mx = sum(wx) / w
        my = sum(wy) / w
        sxy = sum((a - mx) * (b - my) for a, b in zip(wx, wy))
        if kind == "COVAR":
            if w > 1:
                out[i] = sxy / (w - 1)
            continue
        sxx = sum((a - mx) ** 2 for a in wx)
        syy = sum((b - my) ** 2 for b in wy)
        denom = math.sqrt(sxx * syy)
        if denom > 0:
            out[i] = sxy / denom
    return pl.Series("v", out, dtype=pl.Float64)


# ---------------------------------------------------------------- 批次 4 辅助函数


def _find_extreme(x: pl.Series, n: int, m: int, t: int, kind: str) -> pl.Series:
    """FINDHIGH/FINDLOW/FINDHIGHBARS/FINDLOWBARS：N周期前M周期内第T个极值。

    窗口 [i-N-M+1, i-N]（M 根，起点 N 周期前）；越界部分忽略，全越界 → null。
    T 从 1 起。并列极值取距当前最近者（TDX 实证口径待 TQVERIFY4 校正）。
    """
    vals = x.to_list()
    n_bars = len(vals)
    out: list[float | None] = [None] * n_bars
    is_high = "HIGH" in kind
    is_bars = "BARS" in kind
    for i in range(n_bars):
        end = i - n  # 窗口终点（含）
        start = end - m + 1
        if end < 0:
            continue
        # 收集窗口内有效值（bar_idx, value）
        win: list[tuple[int, float]] = []
        for j in range(max(0, start), end + 1):
            v = vals[j]
            if v is not None and v == v:
                win.append((j, v))
        if not win:
            continue
        # 排序：FINDHIGH 降序 / FINDLOW 升序；并列极值 HIGH 取最早者、LOW 取最晚者
        # （I03 实证：并列最高取最早；I04 实证：bar104 窗口 min 在 [90,97,98,99]
        #  TDX 返 5=最晚 bar 距离）
        if is_high:
            win.sort(key=lambda kv: (-kv[1], kv[0]))
        else:
            win.sort(key=lambda kv: (kv[1], -kv[0]))
        if t < 1 or t > len(win):
            continue
        j_sel, v_sel = win[t - 1]
        out[i] = float(i - j_sel) if is_bars else v_sel
    return pl.Series("v", out, dtype=pl.Float64)


def _tma(x: pl.Series, a: float, b: float) -> pl.Series:
    """TMA(X,A,B)：Y=A*Y'+B*X，初值=X0。前段遇 null 则首值为首个非 null 值。"""
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    prev: float | None = None
    for i, v in enumerate(vals):
        if v is None or v != v:
            out[i] = prev  # TDX 遇 null 延续前值（待 TQVERIFY4 校正）
            continue
        prev = v if prev is None else a * prev + b * v
        out[i] = prev
    return pl.Series("v", out, dtype=pl.Float64)


def _ama(x: pl.Series, a: float) -> pl.Series:
    """AMA(X,A)：Y=Y'+A*(X-Y')，初值=X0。"""
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    prev: float | None = None
    for i, v in enumerate(vals):
        if v is None or v != v:
            out[i] = prev
            continue
        prev = v if prev is None else prev + a * (v - prev)
        out[i] = prev
    return pl.Series("v", out, dtype=pl.Float64)


def _barslasts(x: pl.Series, n: int) -> pl.Series:
    """BARSLASTS(X,N)：倒数第 N 次成立距今周期数；不足 N 次 → null。"""
    flags = (x.fill_null(0) != 0).to_list()
    out: list[float | None] = [None] * len(flags)
    hits: list[int] = []
    for i, f in enumerate(flags):
        if f:
            hits.append(i)
        if len(hits) >= n:
            out[i] = float(i - hits[-n])
    return pl.Series("v", out, dtype=pl.Float64)


def _barssincen(x: pl.Series, w: int) -> pl.Series:
    """BARSSINCEN(X,N)：N 周期窗口内首次成立距今周期数；窗口内无成立 → null。

    全窗口：前 N-1 根窗口未满 → null（待 TQVERIFY4 校正是否部分窗口）。
    """
    flags = (x.fill_null(0) != 0).to_list()
    n = len(flags)
    out: list[float | None] = [None] * n
    for i in range(w - 1, n):
        lo = i - w + 1
        for j in range(lo, i + 1):  # 窗口内从最早向最近找首次成立
            if flags[j]:
                out[i] = float(i - j)
                break
    return pl.Series("v", out, dtype=pl.Float64)


def _existr(x: pl.Series, a: int, b: int) -> pl.Series:
    """EXISTR(X,A,B)：前 A 日到前 B 日间（[i-A, i-B]）是否存在 X 非零。

    窗口越界部分忽略（与 EXIST 部分窗口口径一致），空窗 → 0。
    """
    flags = (x.fill_null(0) != 0).to_list()
    n = len(flags)
    if a < b:
        a, b = b, a
    out: list[float | None] = [None] * n
    for i in range(n):
        hi = i - b  # 最近端
        lo = max(0, i - a)  # 最远端
        if hi < lo:
            out[i] = 0.0
            continue
        out[i] = 1.0 if any(flags[j] for j in range(lo, hi + 1)) else 0.0
    return pl.Series("v", out, dtype=pl.Float64)


def _backset(x: pl.Series, w: int) -> pl.Series:
    """BACKSET(X,N)：若 X[i] 非 0，将 [i-N+1, i] 置 1（未来函数，回测慎用）。"""
    flags = (x.fill_null(0) != 0).to_list()
    n = len(flags)
    out = [0.0] * n
    for i, f in enumerate(flags):
        if f:
            for j in range(max(0, i - w + 1), i + 1):
                out[j] = 1.0
    return pl.Series("v", out, dtype=pl.Float64)


def _filterx(x: pl.Series, w: int) -> pl.Series:
    """FILTERX(X,N)：反向过滤——从右向左扫描，成立则保留并屏蔽其前 N 根。

    保留 bar i 后，[i-N, i-1] 共 N 根被屏蔽（与 FILTER 正向屏蔽后 N 根对称）。
    """
    flags = (x.fill_null(0) != 0).to_list()
    n = len(flags)
    out = [0.0] * n
    block_lo = n  # 下标 >= block_lo 的 bar 被屏蔽（向左递减）
    for i in range(n - 1, -1, -1):
        if i >= block_lo:
            continue
        if flags[i]:
            out[i] = 1.0
            block_lo = i - w  # 屏蔽 [i-w, i-1]
    return pl.Series("v", out, dtype=pl.Float64)


def _sumbarsx(x: pl.Series, a: Any) -> pl.Series:
    """SUMBARSX(X,A)：由近及远累加 X 首次 ≥ A 所需周期数；不足 → null。

    TDX 实证口径（I13，全历史值域 {-1,1,2,...,225}，无 0）：
    - cnt==1（当根即 ≥A）→ -1.0
    - cnt>=2 → cnt-1（距离）
    与 SUMBARS 的差异：SUMBARS 不足时返回已累加根数，SUMBARSX 返回 null。
    """
    if isinstance(a, pl.Series):
        raise FormulaEvalError("SUMBARSX 的累计目标 A 必须为常量，不能是序列")
    xv = [0.0 if v is None or v != v else v for v in x.to_list()]
    out: list[float | None] = [None] * x.len()
    for i in range(x.len()):
        s, j, cnt = 0.0, i, 0
        while j >= 0 and s < a:
            s += xv[j]
            cnt += 1
            j -= 1
        if s >= a:
            out[i] = -1.0 if cnt == 1 else float(cnt - 1)
    return pl.Series("v", out, dtype=pl.Float64)


def _sar_state(df: pl.DataFrame, n: int, s: float, m: float) -> tuple[list, list]:
    """SAR 抛物转向状态机，返回 (sar_list, turn_list)。

    TDX 实证规则（TQVERIFY4，000001.SZ 全历史 8471 根逐 bar 零差异）：
    - 参数口径：af_step = S/100，af_max = M/100；前 N-1 根 null
    - 初始化：第 N-1 根恒为多头，SAR=LLV(L,N)，EP=HHV(H,N)，af=af_step；
      SARTURN 该根输出 SAR 值（怪癖，非 0/±1）
    - 每根先更新 EP/af（EP 创新极值 → af+=af_step，封顶 af_max），
      cand = SAR_{i-1} + af*(EP - SAR_{i-1})
    - 反转判定用上一根输出的 SAR（非 cand）：多头 Low_i < SAR_{i-1} → 转空；
      空头 High_i > SAR_{i-1} → 转多
    - 继续：多头 SAR=min(cand, Low_i, Low_{i-1})；空头 SAR=max(cand, High_i, High_{i-1})
    - 转多：SAR=min(Low_{i-1}, Low_i)，EP=High_i，af 重置
    - 转空：SAR=max(EP_old - af_step*(EP_old-Low_i), High_i, High_{i-1})，EP=Low_i，af 重置

    SARTURN 由全精度 SAR 序列与 Close 派生（非独立状态机）：
    +1 当 SAR_i < C_i 且 SAR_{i-1} > C_{i-1}（严格上穿到下方→向多）；
    -1 当 SAR_i > C_i 且 SAR_{i-1} <= C_{i-1}（含等值→向空）；其余 0。
    """
    highs = df["High"].to_list()
    lows = df["Low"].to_list()
    closes = df["Close"].to_list()
    nb = len(highs)
    sar: list[float | None] = [None] * nb
    turn: list[float | None] = [None] * nb
    if nb < n:
        return sar, turn
    af_step, af_max = s / 100.0, m / 100.0
    i0 = n - 1
    long = True  # 恒多初始化（TDX 实证，与价格方向无关）
    ep = max(h for h in highs[:n] if h is not None)
    cur = min(l for l in lows[:n] if l is not None)
    af = af_step
    sar[i0] = cur
    turn[i0] = cur  # 怪癖：初始化根 SARTURN 输出 SAR 值
    for i in range(i0 + 1, nb):
        h, l = highs[i], lows[i]
        if h is None or l is None:
            sar[i] = cur
            turn[i] = 0.0
            continue
        if long:
            if h > ep:
                ep = h
                af = min(af + af_step, af_max)
        else:
            if l < ep:
                ep = l
                af = min(af + af_step, af_max)
        cand = cur + af * (ep - cur)
        prev_l, prev_h = lows[i - 1], highs[i - 1]
        if long and l < cur:  # 反转向空（判定用上一根 SAR）
            base = ep - af_step * (ep - l)
            cur = max(base, h) if prev_h is None else max(base, h, prev_h)
            long = False
            ep = l
            af = af_step
        elif not long and h > cur:  # 反转向多
            cur = min(prev_l, l) if prev_l is not None else l
            long = True
            ep = h
            af = af_step
        else:
            if long:
                cur = cand if prev_l is None else min(cand, l, prev_l)
            else:
                cur = cand if prev_h is None else max(cand, h, prev_h)
        sar[i] = cur
    # SARTURN：由 SAR 与 Close 的上下穿越派生
    for i in range(i0 + 1, nb):
        s0, s1 = sar[i - 1], sar[i]
        c0, c1 = closes[i - 1], closes[i]
        if s0 is None or s1 is None or c0 is None or c1 is None:
            turn[i] = 0.0
        elif s1 < c1 and s0 > c0:
            turn[i] = 1.0
        elif s1 > c1 and s0 <= c0:
            turn[i] = -1.0
        else:
            turn[i] = 0.0
    return sar, turn


def _sar(df: pl.DataFrame, n: int, s: float, m: float) -> pl.Series:
    sar, _ = _sar_state(df, n, s, m)
    return pl.Series("v", sar, dtype=pl.Float64)


def _sarturn(df: pl.DataFrame, n: int, s: float, m: float) -> pl.Series:
    _, turn = _sar_state(df, n, s, m)
    return pl.Series("v", turn, dtype=pl.Float64)


# ---------------------------------------------------------------- 批次 6 辅助函数


def _range_span(x: pl.Series, high: bool) -> pl.Series:
    """TOPRANGE/LOWRANGE：当前值作为极值可向前追溯的前导 bar 数（不含当根）。

    即「最大窗口长度-1」：TOPRANGE = 连续前导 bar 值均**严格小于**当根的
    个数（LOWRANGE 反之严格大于），遇到 ≥（≤）当根的前导 bar 即断，
    从未遇到 → i。并列值同样断开 streak（TQVERIFY6 K03/K04 在线实证：
    bar95 前导=当根 → TDX 出 0，内置含相等值出 1）。单调栈 O(n)：弹出所有
    严格小于（大于）当根的元素后，count = i - 栈顶 ≥（≤）当根的索引 - 1；
    栈空 → i。
    """
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    stack: list[tuple[int, float]] = []  # (bar_idx, value)
    for i, v in enumerate(vals):
        if v is None or v != v:
            continue
        if high:
            while stack and stack[-1][1] < v:
                stack.pop()
        else:
            while stack and stack[-1][1] > v:
                stack.pop()
        out[i] = float(i - stack[-1][0] - 1) if stack else float(i)
        stack.append((i, v))
    return pl.Series("v", out, dtype=pl.Float64)


def _mular(x: pl.Series, w: int) -> pl.Series:
    """MULAR：N 周期累乘（部分窗口口径与 SUM 一致；窗口含 null → null）。"""
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    for i in range(n):
        lo = 0 if w == 0 else max(0, i - w + 1)
        p = 1.0
        ok = True
        for v in vals[lo : i + 1]:
            if v is None or v != v:
                ok = False
                break
            p *= v
        if ok:
            out[i] = p
    return pl.Series("v", out, dtype=pl.Float64)


def _date_code(dt: pl.Series) -> pl.Series:
    """(年-1900)*10000 + 月*100 + 日（与 DATE 函数同口径，批次 5 实证）。"""
    return ((dt.dt.year().cast(pl.Int64) - 1900) * 10000
            + dt.dt.month().cast(pl.Int64) * 100
            + dt.dt.day().cast(pl.Int64))


def _tfilt(x: pl.Series, dt: pl.Series, d1: int, t1: int, d2: int, t2: int) -> pl.Series:
    """TFILT(X,D1,M1,D2,M2)：保留 [D1/M1, D2/M2] 时间段内的数据，其余置 null。

    日期编码同 DATE（1260105=2026-01-05），时刻为 HHMM（词典示例
    1040101,1025→1040101,1345 即 10:25~13:45，区间含端点）。起点晚于终点
    （如 M1>M2）为空窗 → 全部无效。
    """
    date_code = _date_code(dt)
    time_code = dt.dt.hour().cast(pl.Int64) * 100 + dt.dt.minute().cast(pl.Int64)
    after_start = (date_code > d1) | ((date_code == d1) & (time_code >= t1))
    before_end = (date_code < d2) | ((date_code == d2) & (time_code <= t2))
    return x.zip_with(after_start & before_end,
                      pl.Series("n", [None] * x.len(), dtype=pl.Float64))


def _refdate(x: pl.Series, dt: pl.Series, d: int) -> pl.Series:
    """REFDATE(X,日期)：取指定日期（DATE 数值格式）那根的 X 值，整列平铺。

    日期不存在 → 全 null；定位根的 X 为 null → 亦平铺 null。
    """
    codes = _date_code(dt).to_list()
    xs = x.to_list()
    for i, c in enumerate(codes):
        if c == d:
            return pl.Series("v", [xs[i]] * len(xs), dtype=pl.Float64)
    return pl.Series("v", [None] * len(xs), dtype=pl.Float64)


# ---------------------------------------------------------------- 批次 7 辅助函数（动态窗口）


def _ref_dyn(x: pl.Series, n: pl.Series) -> pl.Series:
    """REF 动态偏移：out[i] = x[i - n[i]]（批次 7）。

    n 为 null → null；i-n 越界（含负偏移未来引用）→ null；取到的值 null → null。
    """
    xs = x.to_list()
    ns = n.to_list()
    m = len(xs)
    out: list[float | None] = [None] * m
    for i, ni in enumerate(ns):
        if ni is None or ni != ni:
            continue
        j = i - int(ni)
        if 0 <= j < m:
            v = xs[j]
            if v is not None and v == v:
                out[i] = v
    return pl.Series("v", out, dtype=pl.Float64)


def _sum_dyn(x: pl.Series, n: pl.Series) -> pl.Series:
    """SUM 动态窗口：out[i] = Σ x[i-n[i]+1 .. i]（批次 7）。

    部分窗口口径同静态 SUM（窗口未满用已有数据）；n=0 → expanding 累计；
    n 为 null → 按 0（expanding）处理（TQVERIFY7 L02/L06 实证，区别于
    REF/HHV/LLV 的 null→null）。前缀和 O(n)。窗口内 x 的 null 按 0 跳过
    （行情数据无 null）。
    """
    xs = x.to_list()
    ns = n.to_list()
    m = len(xs)
    out: list[float | None] = [None] * m
    cums = [0.0] * (m + 1)
    for i, v in enumerate(xs):
        cums[i + 1] = cums[i] + (v if v is not None and v == v else 0.0)
    for i, ni in enumerate(ns):
        w = 0 if (ni is None or ni != ni) else int(ni)
        lo = 0 if w == 0 else max(0, i - w + 1)
        out[i] = cums[i + 1] - cums[lo]
    return pl.Series("v", out, dtype=pl.Float64)


def _hll_dyn(x: pl.Series, n: pl.Series, high: bool) -> pl.Series:
    """LLV/HHV 动态窗口（窗口长度逐 bar 可变）：单调队列 O(n) 均摊（批次 7）。

    窗口 [i-n[i]+1, i]，部分窗口口径同静态；n=0 → 历史累计；n 为 null → null。
    """
    xs = x.to_list()
    ns = n.to_list()
    m = len(xs)
    out: list[float | None] = [None] * m
    dq: deque[tuple[int, float]] = deque()  # HHV 值递减 / LLV 值递增
    for i in range(m):
        v = xs[i]
        if v is not None and v == v:
            if high:
                while dq and dq[-1][1] <= v:
                    dq.pop()
            else:
                while dq and dq[-1][1] >= v:
                    dq.pop()
            dq.append((i, v))
        ni = ns[i]
        if ni is None or ni != ni:
            continue  # 窗口无效 → 输出 null（队列照常维护）
        w = int(ni)
        lo = 0 if w == 0 else max(0, i - w + 1)
        while dq and dq[0][0] < lo:
            dq.popleft()
        if dq:
            out[i] = dq[0][1]
    return pl.Series("v", out, dtype=pl.Float64)


# ---------------------------------------------------------------- 批次 8 辅助函数


def _xma(x: pl.Series, w: int) -> pl.Series:
    """XMA(X,N)：偏移移动平均（假设口径，待 TQVERIFY8 实证）。

    窗口 [i-M, i+M]（M=N//2），词典注明「使用未来 N/2 日数据」→ 右侧引用
    未来 bar（回测信号存在前视，precheck 已按 fl 标签给 leak 警告）；
    两端部分窗口取可用数据，窗口内 null 跳过。
    """
    vals = x.to_list()
    n = len(vals)
    m = w // 2
    out: list[float | None] = [None] * n
    for i in range(n):
        lo, hi = max(0, i - m), min(n - 1, i + m)
        win = [v for v in vals[lo:hi + 1] if v is not None and v == v]
        if win:
            out[i] = sum(win) / len(win)
    return pl.Series("v", out, dtype=pl.Float64)


def _hhvllv(x: pl.Series, t: int, n1: int, n2: int) -> pl.Series:
    """HHVLLV(X,T,N1,N2)：前 N1 日到前 N2 日时段内最高（T=0）/最低（T=1）。

    TQVERIFY8 实证：窗口 [i-N1, i-N2]；无效窗口输出 0.0（非 null）；
    N1=0 或 N1 < N2 → 整线 0.0；N2=0 含当根，bar0 无特例
    （如 HHVLLV(C,0,20,0) bar0 = C[0]）。
    预热段分路径（M03/M04 在线实证）：T=0（HHV 路径）左端截断取可用数据；
    T=1（LLV 路径）要求完整窗口，i < N1 时输出 0.0（非左截断）。
    """
    if t not in (0, 1):
        raise FormulaEvalError(f"HHVLLV 的 T 参数需为 0（最高）或 1（最低）: {t}")
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [0.0] * n
    if n1 <= 0 or n1 < n2:
        # 实证：N1=0（HHVLLV(C,1,0,0)）整线 0.0，非 expanding
        return pl.Series("v", out, dtype=pl.Float64)
    for i in range(n):
        hi = i - n2
        if hi < 0:
            continue  # 保持 0.0
        lo = i - n1
        if t == 1 and lo < 0:
            # T=1 路径要求完整窗口（M04 实证：bar<N1 输出 0.0）
            continue
        lo = max(0, lo)
        win = [v for v in vals[lo:hi + 1] if v is not None and v == v]
        if win:
            out[i] = max(win) if t == 0 else min(win)
    return pl.Series("v", out, dtype=pl.Float64)


def _median(x: pl.Series, m: int, w: int) -> pl.Series:
    """MEDIAN(X,M,N)：窗口 [i-M-N+1, i-M] 的中位数（TQVERIFY8 实证）。

    左端截断取可用数据（右端 i-M < 0 → null）；中位数取排序后 sorted[n//2]
    （偶数个取上中位，非两中位均值，如 [1,3] → 3）。
    """
    vals = x.to_list()
    n = len(vals)
    out: list[float | None] = [None] * n
    for i in range(n):
        hi = i - m
        if hi < 0 or hi > n - 1:
            continue
        win = [v for v in vals[max(0, i - m - w + 1):hi + 1]
               if v is not None and v == v]
        if not win:
            continue
        win.sort()
        out[i] = win[len(win) // 2]
    return pl.Series("v", out, dtype=pl.Float64)


def _updown(x: pl.Series) -> pl.Series:
    """UPDOWN(X)：SIGN(X - REF(X,1))，首根 null（大小变化 -1/0/1）。"""
    return (x - x.shift(1)).sign()


def _newsar_state(df: pl.DataFrame, n: int, s: float) -> list[float | None]:
    """NEWSAR 抛物转向状态机（TQVERIFY8 实证，000001.SZ 全历史 8471 根零差异）。

    与标准 SAR 的关键差异（逆向实证锁定）：
    - 参数口径：步长 = S/1000（SAR 为 S/100），无 af 上限（k 可远超 N）
    - 初始化：第 N-1 根恒为空头，SAR=HHV(H,N)，k=0
    - 每根先按「当根创新极值加一步」更新 k（空头 L 新低 / 多头 H 新高，
      严格比较；翻转后第一根固定不加步），再算递推值
      cand = SAR_{i-1} + (S/1000)*k*(当根 L 或 H - SAR_{i-1})
    - 反转判定用**当根递推值**（非上一根 SAR、非盘中 H/L 穿越）：
      空头 C_i > cand → 转多；多头 C_i < cand → 转空
    - 转空锚 = max(H[i-N:i])、转多锚 = min(L[i-N:i])（前 N 根窗口极值，
      不含当根）；翻转后 k 重置 1（保底一步）
    """
    highs = df["High"].to_list()
    lows = df["Low"].to_list()
    closes = df["Close"].to_list()
    nb = len(highs)
    sar: list[float | None] = [None] * nb
    if nb < n:
        return sar
    step = s / 1000.0
    i0 = n - 1
    short = True
    k = 0
    cur = max(h for h in highs[:n] if h is not None)
    sar[i0] = cur
    first_after_flip = False
    for i in range(i0 + 1, nb):
        h, l, c = highs[i], lows[i], closes[i]
        ph, pl = highs[i - 1], lows[i - 1]
        if h is None or l is None or c is None or ph is None or pl is None:
            sar[i] = cur
            continue
        if not first_after_flip:
            if short:
                if l < pl:
                    k += 1
            elif h > ph:
                k += 1
        ref = l if short else h
        cand = cur + step * k * (ref - cur)
        if short:
            flip = c > cand
        else:
            flip = c < cand
        if flip:
            short = not short
            if short:
                cur = max(v for v in highs[i - n:i] if v is not None)
            else:
                cur = min(v for v in lows[i - n:i] if v is not None)
            k = 1
            first_after_flip = True
        else:
            cur = cand
            first_after_flip = False
        sar[i] = cur
    return sar


def _newsar(df: pl.DataFrame, n: int, s: float) -> pl.Series:
    return pl.Series("v", _newsar_state(df, n, s), dtype=pl.Float64)


def _tfilter(a: pl.Series, b: pl.Series, n: int) -> pl.Series:
    """TFILTER(A,B,N)：开仓/平仓信号配对去抖状态机（TDX 帮助口径假设）。

    flat 状态下 A 成立 → 开仓（存续期 A 去抖忽略）；open 状态下 B 成立 →
    平仓；无开仓时 B 忽略。N=0 → 开仓位 1.0 / 平仓位 2.0；N=1 → 仅开仓 1.0；
    N=2 → 仅平仓 1.0。
    """
    if n not in (0, 1, 2):
        raise FormulaEvalError(f"TFILTER 的 N 参数需为 0/1/2: {n}")
    av = (a.fill_null(0) != 0).to_list()
    bv = (b.fill_null(0) != 0).to_list()
    out = [0.0] * len(av)
    opened = False
    for i, (fa, fb) in enumerate(zip(av, bv)):
        if not opened:
            if fa:
                opened = True
                if n != 2:
                    out[i] = 1.0
        elif fb:
            opened = False
            if n == 0:
                out[i] = 2.0
            elif n == 2:
                out[i] = 1.0
    return pl.Series("v", out, dtype=pl.Float64)


def _ttfilter(ks: list[pl.Series], n: int) -> pl.Series:
    """TTFILTER(买开,卖平,卖开,买平,N)：四态交易信号配对状态机。

    flat → 买开(1) → long → 卖平(2) → flat → 卖开(3) → short → 买平(4) → flat；
    状态不匹配的信号忽略；同 bar 按 k 顺序取首个有效信号转移状态。
    N=0 → 输出执行编码 1/2/3/4；
    N=1..4 → 第 k 路信号为真且转移前状态允许其动作即出 1.0，标记独立于
    实际执行的路（TQVERIFY8 M18 实证：第 1/3 路同信号，flat 同真只执行
    买开 code=1，但 N=3 线同样在买开位出 1.0；8B N03-N06 独立四路探测线
    下两种口径重合、全历史一致）。
    """
    if n not in (0, 1, 2, 3, 4):
        raise FormulaEvalError(f"TTFILTER 的 N 参数需为 0..4: {n}")
    kv = [(k.fill_null(0) != 0).to_list() for k in ks]
    m = len(kv[0])
    out = [0.0] * m
    state = 0  # 0=flat 1=long 2=short
    rules = ((0, 1, 1), (1, 0, 2), (0, 2, 3), (2, 0, 4))  # (需状态,新状态,编码)
    for i in range(m):
        if n >= 1:
            need = rules[n - 1][0]
            if kv[n - 1][i] and state == need:
                out[i] = 1.0
        for k_idx, (need, new, code) in enumerate(rules):
            if kv[k_idx][i] and state == need:
                if n == 0:
                    out[i] = float(code)
                state = new
                break
    return pl.Series("v", out, dtype=pl.Float64)


# ---------------------------------------------------------------- 对外入口


def cross_periods_of(src: str) -> set:
    """收集公式源码中 FIELD#PERIOD 引用的周期键集合（批次 9a 回测取数扩展用）。

    解析失败/未知后缀均容错跳过（返回子集；求值层才报错），
    供 build_klines 判断需附加拉取哪些上级周期 K 线。
    """
    periods: set = set()
    try:
        defs = parse_defs(src)
    except FormulaEvalError:
        return periods
    for d in defs:
        try:
            ast = _Parser(_tokenize(d.expr)).parse()
        except FormulaEvalError:
            continue
        _walk_cross(ast, periods)
    return periods


def _walk_cross(node, periods: set) -> None:
    if not isinstance(node, tuple):
        return
    if node[0] == "cross":
        keys = _CROSS_PERIOD_MAP.get(str(node[2]).upper())
        if keys:
            periods.update(keys)
        return
    if node[0] == "xref_cross":
        # 批次 9b：TESTA.A1#WEEK 组合语法同样需要上级周期 K 线
        keys = _CROSS_PERIOD_MAP.get(str(node[3]).upper())
        if keys:
            periods.update(keys)
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            _walk_cross(child, periods)
        elif isinstance(child, list):
            for c in child:
                if isinstance(c, tuple):
                    _walk_cross(c, periods)


def xref_names_of(src: str) -> set:
    """收集公式源码中 公式名.线名[#周期] 跨公式引用的公式名集合（批次 9b）。

    解析失败容错跳过（返回子集；求值层才报错），供接线层解析被引公式与环检测。
    """
    names: set = set()
    try:
        defs = parse_defs(src)
    except FormulaEvalError:
        return names
    for d in defs:
        try:
            ast = _Parser(_tokenize(d.expr)).parse()
        except FormulaEvalError:
            continue
        _walk_xref(ast, names)
    return names


def _walk_xref(node, names: set) -> None:
    if not isinstance(node, tuple):
        return
    if node[0] in ("xref", "xref_cross"):
        names.add(node[1])
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            _walk_xref(child, names)
        elif isinstance(child, list):
            for c in child:
                if isinstance(c, tuple):
                    _walk_xref(c, names)


def evaluate_outputs(src: str | list[Definition], df: pl.DataFrame, *,
                     period: str | None = None,
                     index_frames: pl.DataFrame | None = None,
                     period_frames: dict[str, pl.DataFrame] | None = None,
                     no_leak: bool = False,
                     xref_resolver=None) -> dict[str, pl.Series]:
    """求值公式，返回 {输出线名: Float64 Series}（仅 op==':' 的输出线）。

    - src: 公式源码（内部先 parse_defs）或 parse_defs 的返回值
    - df:  行情 DataFrame，需含 Open/High/Low/Close/Volume/Amount 列
    - period: 当前 K 线周期键（PERIOD 周期编号函数依赖；回测接线自动注入）
    - index_frames: 对齐到 df 时间轴的大盘指数 OHLCV（INDEXC/O/H/L/V/A 依赖；
      回测接线按个股所属市场注入）
    - period_frames: 批次 9a 跨周期引用上级数据 {周期键: df}（C#WEEK 等依赖；
      回测接线按公式实际引用注入，含 base 周期自身键时恒等对齐）
    - no_leak: 批次 9a 无泄露开关（默认 false=TDX 语义；true=仅引用已完成上级 bar）
    - xref_resolver: 批次 9b 跨公式引用解析器 callable(formula_name, period_key|None)
      → {线名: Series}（memo/环检测/被引公式校验由接线层负责；None → NAME.LINE 报错）
    """
    defs = parse_defs(src) if isinstance(src, str) else list(src)
    ev = _Evaluator(df, period=period, index_df=index_frames,
                    period_frames=period_frames, no_leak=no_leak,
                    xref_resolver=xref_resolver)
    env: dict[str, Any] = {}
    out: dict[str, pl.Series] = {}
    for d in defs:
        expr_src = d.expr
        try:
            ast = _Parser(_tokenize(expr_src)).parse()
            val = ev.eval(ast, env)
        except FormulaEvalError as e:
            raise FormulaEvalError(f"第 {d.line} 行 [{d.name}] {e}") from e
        env[d.name.upper()] = val
        if d.op == ":":
            out[d.name] = val if isinstance(val, pl.Series) else _broadcast(val, ev.n)
    return out


# ---------------------------------------------------------------- 静态预检

# 不可见字符：出现即报（tokenize 无法识别或造成隐蔽解析错误）
_INVISIBLE_CHARS = {
    "\u3000": "全角空格",
    "\u00a0": "不间断空格",
    "\u200b": "零宽空格",
    "\u200c": "零宽非连接符",
    "\u200d": "零宽连接符",
    "\ufeff": "BOM/零宽不换行空格",
}

# 中文标点 → 半角建议（'...' 字符串与 {...} 注释内的中文标点合法，不报）
_CN_PUNCT = {
    "，": ",", "；": ";", "：": ":", "（": "(", "）": ")",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "！": "!", "？": "?", "、": ",", "【": "[", "】": "]",
}


def source_lint(src: str) -> list[str]:
    """源码级体检（移植自「通达信指标源码整理工具.html」体检能力）。

    四类检测，报错精确到行列并附修复建议，追加进 syntax_check.errors：
    1. 不可见字符：全角空格/不间断空格/零宽字符/BOM
    2. 中文标点：字符串与注释内合法不报，代码区出现即报并给半角建议
    3. 括号平衡：字符串/注释外扫描，未闭合的 ( / 多余的 )
    4. 疑似漏分号：当前行以代码结尾且下一非空代码行是新定义头——两条
       定义会被合并成一条语句，tokenize 只会报「无法识别的字符 ':'」，
       这里给出人话提示（启发式，措辞带「疑似」）
    """
    text = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    problems: list[str] = []

    # 1) 不可见字符（含注释/字符串内——不可见字符在任何位置都有害）
    for ln, line in enumerate(text.split("\n"), 1):
        for col, ch in enumerate(line, 1):
            kind = _INVISIBLE_CHARS.get(ch)
            if kind:
                problems.append(
                    f"第 {ln} 行第 {col} 列：不可见字符（{kind}），"
                    f"建议删除或替换为普通空格"
                )

    # 2/3) 中文标点 + 括号平衡：单遍扫描，字符串与 {...} 注释内容跳过；
    #      code_chars 为「字符串/注释内容替换为空格」的副本，供漏分号启发式用
    code_chars: list[str] = []
    paren_stack: list[tuple[int, int]] = []
    in_quote = in_comment = False
    line, col = 1, 1
    for ch in text:
        if ch == "\n":
            code_chars.append("\n")
            line += 1
            col = 1
            continue
        if in_comment:
            code_chars.append(" ")
            if ch == "}":
                in_comment = False
            col += 1
            continue
        if in_quote:
            code_chars.append(" ")
            if ch in ("'", '"'):
                in_quote = False
            col += 1
            continue
        if ch in _CN_PUNCT:
            problems.append(
                f"第 {line} 行第 {col} 列：中文标点「{ch}」，建议改为半角「{_CN_PUNCT[ch]}」"
            )
        elif ch in ("'", '"'):
            in_quote = True
        elif ch == "{":
            in_comment = True
            code_chars.append(" ")
            col += 1
            continue
        elif ch == "(":
            paren_stack.append((line, col))
        elif ch == ")":
            if paren_stack:
                paren_stack.pop()
            else:
                problems.append(f"第 {line} 行第 {col} 列：多余的右括号「)」")
        code_chars.append(ch)
        col += 1
    for bl, bc in paren_stack:
        problems.append(f"第 {bl} 行第 {bc} 列：左括号「(」未闭合")

    # 4) 疑似漏分号：本行以代码结尾 且 下一非空代码行匹配定义头
    code_lines = "".join(code_chars).split("\n")
    for ln in range(len(code_lines) - 1):
        cur = code_lines[ln].rstrip()
        if not cur or cur.endswith(";"):
            continue
        for nxt in code_lines[ln + 1:]:
            nxt = nxt.strip()
            if not nxt:
                continue
            m = _DEF_HEAD_RE.match(nxt)
            if m:
                problems.append(
                    f"第 {ln + 1} 行疑似漏分号：下一行「{m.group(1)}」是新定义，"
                    f"两条语句会被合并解析报错，请检查本行结尾是否缺少「;」"
                )
            break
    return problems


def syntax_check(src: str) -> dict:
    """语法预检（无数据干跑）：词典能力闸门 + 语句/表达式静态解析。

    面向内置引擎（engine=builtin）的「语法预检」按钮：
    - issues:  precheck 词典闸门结论（future/realtime/data/unknown，level 原样）
    - errors:  源码体检（不可见字符/中文标点/括号/疑似漏分号）+ 解析错误 +
               内置引擎未实现函数 + 未定义变量 + 跨周期引用
               （致命，每条含行号与定义名）
    - outputs: `:` 输出线名列表（供信号配置对照）
    - ok:      无 errors 且 issues 中无 error 级
    """
    from core.engine.tdx_formula.dictionary import precheck

    issues = precheck(src)
    errors: list[str] = source_lint(src)
    outputs: list[str] = []
    try:
        defs = parse_defs(src)
    except FormulaEvalError as e:
        errors.append(str(e))
        defs = []
    known = {d.name.upper() for d in defs}
    supported = set(_SUPPORTED_FUNCS)
    fields = set(_FIELD_ALIAS)
    for d in defs:
        if d.op == ":":
            outputs.append(d.name)
        try:
            ast = _Parser(_tokenize(d.expr)).parse()
        except FormulaEvalError as e:
            errors.append(f"第 {d.line} 行 [{d.name}] {e}")
            continue
        _static_check(ast, known, fields, supported, errors, d.line, d.name)
    ok = not errors and not any(i.level == "error" for i in issues)
    return {"ok": ok, "issues": issues, "errors": errors, "outputs": outputs}


def _static_check(node, known, fields, supported, errors, line: int, name: str) -> None:
    """递归遍历 AST：未定义变量/未实现函数/未知跨周期后缀 → 致命错误。"""
    tag = node[0]
    if tag == "num":
        return
    if tag == "id":
        n = node[1].upper()
        if n not in known and n not in fields and n not in _BARE_FUNCS:
            errors.append(f"第 {line} 行 [{name}] 未定义的变量或字段: {node[1]}")
        return
    if tag in ("xref", "xref_cross"):
        # 批次 9b：跨公式引用 公式名.线名[#周期]——公式名/线名/被引公式存在性
        # 运行时经 xref_resolver 校验，静态检查不校验
        return
    if tag == "cross":
        # 批次 9a：合法后缀不再致命（求值层校验数据可得性/粒度）；未知后缀致命
        suffix = str(node[2]).upper()
        if suffix not in _CROSS_PERIOD_MAP:
            errors.append(
                f"第 {line} 行 [{name}] 跨周期引用 {node[1]}#{node[2]}："
                f"未知周期后缀（支持 {'/'.join(_CROSS_PERIOD_MAP)}）"
            )
        return
    if tag == "call":
        if node[1] not in supported:
            errors.append(f"第 {line} 行 [{name}] 内置引擎未实现函数: {node[1]}")
        for a in node[2]:
            _static_check(a, known, fields, supported, errors, line, name)
        return
    for child in node[2:]:
        _static_check(child, known, fields, supported, errors, line, name)
