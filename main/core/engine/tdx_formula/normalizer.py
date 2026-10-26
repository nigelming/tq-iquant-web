"""0016 信号标准化向导 —— 源码级改写为回测平台可接收的信号标识。

平台对公式信号的接收硬规则：仅 ``:`` 输出线可配信号、trigger_value 仅 ±1/±2
（0 是无信号常态值）、严格相等匹配。用户从通达信复制的公式输出线多为连续值
（如 MACD），直接配信号永远匹配不上。normalize_formula 选输出线 + 规则 →
生成新公式：原 ``:`` 输出线全部降级为 ``:=``（绘图属性丢弃），尾部按 plans
顺序追加离散标识值（±1/±2/0）的标准化输出线。新公式存为新记录，源公式零改动。

三类规则（口径见 docs/plans/0016-signal-normalization-wizard.md，E1/E2/E3 默认值）：
- cross_zero      上穿0：双向生成 ``XG:CROSS(线,0)`` + ``XD:CROSS(0,线)``；
                  仅上穿/仅下穿 → 单线，统一首线名 XG
- above_threshold 大于阈值：``XG:线>阈值``（direction: gt）或 ``线<阈值``（lt）
- map_values      值域映射：``XG:IF(线=v1,t1,IF(线=v2,t2,...,0))``，映射对 ≤4、
                  原值精确相等（与平台 trigger 匹配口径一致）、目标值限 ±1/±2

命名冲突（E2）：首个生成线 XG，被占用时依次 XG2/XG3…；cross_zero 双向的第二线
用 XD（被占用时 XD2/XD3…）。重建源码时注释（{...}）原样保留在原位置。
"""

from __future__ import annotations

import re

from core.engine.tdx_formula.parser import (
    _DEF_HEAD_RE,
    _DRAW_ATTR_NAMES,
    _balance_ok,
    _strip_comments,
    parse_defs,
)

VALID_RULES = ("cross_zero", "above_threshold", "map_values")
VALID_TARGETS = (-2, -1, 1, 2)

# 尾部绘图属性剥离（允许属性与结尾之间夹注释；括号平衡保护同 parser.strip_draw_attrs）
_TAIL_ATTR_RE = re.compile(
    rf",\s*({_DRAW_ATTR_NAMES})\s*(?:\{{[^}}]*\}}\s*)*$", re.IGNORECASE
)


def _fmt_num(v) -> str:
    """数值 → TDX 字面量：整数值不带小数点，浮点去掉多余尾零（0.3000…4 → 0.3）。"""
    f = float(v)
    if f.is_integer():
        return str(int(f))
    return f"{f:g}"


def _statement_slices(src: str) -> list[str]:
    """按顶层分号切分源码，返回全部语句原文切片（含纯注释语句，顺序保留）。

    与 parser.parse_defs 同一套边界规则（引号/注释/括号内的分号不切，
    未闭合注释丢弃剩余文本），但不丢弃纯注释语句——重建源码时注释
    原样保留在原位置。
    """
    text = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    slices: list[str] = []
    n, i, depth, start = len(text), 0, 0, -1
    in_quote = False
    while i < n:
        c = text[i]
        if in_quote:
            if c in ("'", '"'):
                in_quote = False
            i += 1
            continue
        if c in ("'", '"'):
            if start < 0:
                start = i
            in_quote = True
            i += 1
            continue
        if c == "{":
            if start < 0:
                start = i
            close = text.find("}", i)
            if close < 0:
                break
            i = close + 1
            continue
        if c == "(":
            depth += 1
            if start < 0:
                start = i
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            if start < 0:
                start = i
            i += 1
            continue
        if c == ";" and depth == 0:
            if start < 0:
                start = i
            slices.append(text[start:i])
            start = -1
            i += 1
            continue
        if not c.isspace() and start < 0:
            start = i
        i += 1
    if start >= 0:
        slices.append(text[start:n])
    return slices


def _degrade_slice(raw: str) -> str:
    """输出线语句降级：头部分隔冒号 ``:`` → ``:=``，尾部绘图属性丢弃。

    只动头部冒号与尾部属性，语句内的注释原样保留。
    """
    s = raw
    # 1) 头部分隔冒号（首个顶层冒号，名字不含冒号；跳过引号与注释）
    out: list[str] = []
    i, n = 0, len(s)
    replaced = False
    while i < n:
        c = s[i]
        if c in ("'", '"'):
            j = s.find(c, i + 1)
            j = n - 1 if j < 0 else j
            out.append(s[i : j + 1])
            i = j + 1
            continue
        if c == "{":
            close = s.find("}", i)
            if close < 0:
                out.append(s[i:])
                break
            out.append(s[i : close + 1])
            i = close + 1
            continue
        if c == ":" and not replaced:
            out.append(":=")
            replaced = True
            i += 1
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    # 2) 剥尾部绘图属性（可重复：COLORRED,LINETHICK2；属性与结尾间允许注释）
    while True:
        m = _TAIL_ATTR_RE.search(s)
        if not m or not _balance_ok(s[: m.start()]):
            break
        s = s[: m.start()].rstrip()
    return s


def _validate_plan(p, outputs: dict) -> tuple:
    """校验单个 plan，返回 (目标 Definition, rule, params)。"""
    if not isinstance(p, dict):
        raise ValueError("plans 元素必须为对象 {line, rule, params}")
    line = str(p.get("line") or "").strip()
    rule = str(p.get("rule") or "").strip()
    params = p.get("params") or {}
    if not line:
        raise ValueError("plans 中存在空的输出线名")
    target = outputs.get(line.upper())
    if target is None:
        raise ValueError(
            f"输出线 {line} 不存在或不是 `:` 输出线（:= 中间变量与绘图指令不可配信号）"
        )
    if rule not in VALID_RULES:
        raise ValueError(f"未知规则 {rule}，可选：cross_zero / above_threshold / map_values")
    if not isinstance(params, dict):
        raise ValueError("params 必须为对象")
    return target, rule, params


def _build_exprs(rule: str, src_name: str, params: dict) -> list[tuple[str, str, str]]:
    """按规则生成 (基准线名, 表达式, 触发含义) 列表。"""
    if rule == "cross_zero":
        direction = str(params.get("direction") or "both")
        if direction not in ("both", "up", "down"):
            raise ValueError(f"cross_zero 方向必须为 both/up/down，收到 {direction}")
        items = []
        if direction in ("both", "up"):
            items.append(("XG", f"CROSS({src_name},0)", "1=上穿0轴当根，0=其他"))
        if direction in ("both", "down"):
            base = "XD" if direction == "both" else "XG"
            items.append((base, f"CROSS(0,{src_name})", "1=下穿0轴当根，0=其他"))
        return items

    if rule == "above_threshold":
        threshold = params.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("above_threshold 需要数值参数 threshold（阈值）")
        direction = str(params.get("direction") or "gt")
        if direction not in ("gt", "lt"):
            raise ValueError(f"above_threshold 方向必须为 gt/lt，收到 {direction}")
        op = ">" if direction == "gt" else "<"
        meaning = "1=线>阈值期间，0=其他" if direction == "gt" else "1=线<阈值期间，0=其他"
        return [("XG", f"{src_name}{op}{_fmt_num(threshold)}", meaning)]

    # map_values
    mappings = params.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("map_values 需要映射对参数 mappings（非空列表）")
    if len(mappings) > 4:
        raise ValueError("映射对最多 4 个")
    pairs: list[tuple] = []
    seen_from: set[float] = set()
    for mp in mappings:
        if not isinstance(mp, dict):
            raise ValueError("映射对必须为对象 {from, to}")
        fv, tv = mp.get("from"), mp.get("to")
        if isinstance(fv, bool) or not isinstance(fv, (int, float)):
            raise ValueError(f"映射原值必须是数值，收到 {fv!r}")
        if isinstance(tv, bool) or not isinstance(tv, (int, float)) or tv not in VALID_TARGETS:
            raise ValueError(f"映射目标值必须为 -2/-1/1/2，收到 {tv!r}")
        if float(fv) in seen_from:
            raise ValueError(f"映射原值重复：{_fmt_num(fv)}")
        seen_from.add(float(fv))
        pairs.append((fv, tv))
    inner = "0"
    for fv, tv in reversed(pairs):
        inner = f"IF({src_name}={_fmt_num(fv)},{_fmt_num(tv)},{inner})"
    meaning = (
        ", ".join(f"{_fmt_num(fv)}→{_fmt_num(tv)}" for fv, tv in pairs) + "，其他=0"
    )
    return [("XG", inner, meaning)]


def normalize_formula(src: str, plans: list[dict]) -> dict:
    """生成标准化新公式源码（纯函数，不落库）。

    plans 元素：{line: 输出线名, rule: cross_zero|above_threshold|map_values, params: dict}
    返回 {new_content, output_lines, warnings}；输入非法时抛 ValueError。
    output_lines 元素：{name, source_line, rule, expr, meaning}（供前端展示
    「可用信号」对照：线名配 signal_name，含义说明 trigger_value 取值）。
    """
    if not isinstance(plans, list) or not plans:
        raise ValueError("plans 不能为空：至少为一条输出线配置标准化规则")

    defs = parse_defs(src)
    if not defs:
        raise ValueError("公式内容为空或没有可识别的定义语句")

    # 校验 plans：line 必须是源公式的 `:` 输出线（大小写不敏感，同名取最后一个）
    outputs: dict[str, object] = {}
    for d in defs:
        if d.op == ":":
            outputs[d.name.upper()] = d
    seen: set[str] = set()
    checked: list[tuple] = []
    for p in plans:
        target, rule, params = _validate_plan(p, outputs)
        if target.name.upper() in seen:
            raise ValueError(f"输出线 {target.name} 重复配置")
        seen.add(target.name.upper())
        checked.append((target, rule, params))

    # 重建源码：全部 `:` 输出线降级为 `:=`（绘图属性丢弃），其余语句原样保留
    attr_defs = {d.name.upper(): d.attrs for d in defs if d.op == ":" and d.attrs}
    parts: list[str] = []
    warnings: list[str] = []
    for raw_slice in _statement_slices(src):
        cleaned = _strip_comments(raw_slice).strip()
        if not cleaned:
            parts.append(raw_slice.rstrip())  # 纯注释语句原样保留（不补分号）
            continue
        m = _DEF_HEAD_RE.match(cleaned)
        if m and m.group(2) == ":":
            up = m.group(1).upper()
            if up in attr_defs:
                warnings.append(
                    f"输出线 {m.group(1)} 的绘图属性已丢弃：{', '.join(attr_defs[up])}"
                )
            parts.append(_degrade_slice(raw_slice).rstrip() + ";")
        else:
            parts.append(raw_slice.rstrip() + ";")
    body = "\n".join(p for p in parts if p.strip())

    # 尾部追加标准化输出线（命名冲突 E2：XG → XG2/XG3…；双向第二线 XD → XD2…）
    used = {d.name.upper() for d in defs}

    def alloc(base: str) -> str:
        if base not in used:
            used.add(base)
            return base
        k = 2
        while f"{base}{k}" in used:
            k += 1
        name = f"{base}{k}"
        used.add(name)
        return name

    gen_lines: list[str] = []
    out_lines: list[dict] = []
    for target, rule, params in checked:
        for base, expr, meaning in _build_exprs(rule, target.name, params):
            name = alloc(base)
            gen_lines.append(f"{name}:{expr};")
            out_lines.append(
                {"name": name, "source_line": target.name, "rule": rule,
                 "expr": expr, "meaning": meaning}
            )
        if rule == "map_values":
            warnings.append("值域映射按原值精确相等比较（与平台信号匹配口径一致），"
                            "浮点原值需与公式输出值完全一致")

    new_content = body + "\n" + "\n".join(gen_lines)
    return {"new_content": new_content, "output_lines": out_lines, "warnings": warnings}
