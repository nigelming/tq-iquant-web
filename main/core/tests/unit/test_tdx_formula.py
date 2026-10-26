"""tdx_formula 内置公式引擎 —— 解析层（词典/词法/语句解析）单元测试。

行为基线移植自「通达信指标源码整理工具.html」：
- TdxLint.mask / FW_MAP / INVISIBLE          → tdx_formula.lexer
- TdxSigNorm.parseDefs / TdxOptimizer.stripDrawAttrs → tdx_formula.parser
- TDX_ALL 词典 + future/realtime 能力闸门     → tdx_formula.dictionary
"""

from core.engine.tdx_formula.dictionary import precheck
from core.engine.tdx_formula.lexer import find_invisible, mask, normalize_fullwidth
from core.engine.tdx_formula.parser import parse_defs, strip_draw_attrs


# ---------------------------------------------------------------- lexer

class TestMask:
    def test_comments_and_strings_blanked_with_positions_kept(self):
        src = "MA(C,5); {注释内容}\nX:'abc';"
        m = mask(src)
        assert len(m.code) == len(src)
        # 注释与字符串内容被空格替换，但换行保留
        assert "{注释内容}" not in m.code
        assert "'abc'" not in m.code
        assert "\n" in m.code
        # 起止位置可还原
        assert m.strings[0][0] < m.strings[0][1] <= len(src)

    def test_comment_can_span_lines(self):
        src = "A:=1;{跨行\n注释带;分号}\nB:=2;"
        m = mask(src)
        assert ";" not in m.code.split("A:=1;")[1].split("B:=2;")[0]

    def test_unclosed_string_masks_to_end(self):
        src = "X:'未闭合"
        m = mask(src)
        assert "'" not in m.code


class TestNormalizeFullwidth:
    def test_common_fullwidth_punctuation(self):
        assert normalize_fullwidth("X：MA（C，5）；") == "X:MA(C,5);"

    def test_length_preserved(self):
        src = "X：=MA（C，5）；{注释}"
        assert len(normalize_fullwidth(src)) == len(src)

    def test_extra_fullwidth_from_rules_v2(self):
        # TdxRulesV2 补全表：【】｛｝～ 等
        assert normalize_fullwidth("X【1】") == "X[1]"
        assert normalize_fullwidth("Y～2") == "Y~2"


class TestFindInvisible:
    def test_zero_width_space_detected(self):
        src = "MA(C,5)\u200b;"
        found = find_invisible(src)
        assert len(found) == 1
        idx, name = found[0]
        assert idx == 7
        assert "零宽空格" in name

    def test_clean_source_no_hit(self):
        assert find_invisible("MA(C,5);") == []


# ---------------------------------------------------------------- parser

class TestParseDefs:
    def test_one_line_multi_statement(self):
        # 通达信允许一行多条语句；按行切分会把后续定义吞进第一条
        src = "MA3:=MA(C,3);MA13:=MA(C,13);X:CROSS(MA3,MA13);"
        defs = parse_defs(src)
        assert [d.name for d in defs] == ["MA3", "MA13", "X"]
        assert [d.op for d in defs] == [":=", ":=", ":"]
        assert defs[0].expr == "MA(C,3)"
        assert defs[2].expr == "CROSS(MA3,MA13)"

    def test_chinese_identifiers(self):
        src = "条件1:=CLOSE>OPEN;共振追涨:条件1 AND VOL>0;"
        defs = parse_defs(src)
        assert [d.name for d in defs] == ["条件1", "共振追涨"]
        assert defs[1].expr == "条件1 AND VOL>0"

    def test_output_attrs_stripped(self):
        defs = parse_defs("支撑:ZC1,COLORRED;")
        assert defs[0].expr == "ZC1"
        assert defs[0].attrs == ["COLORRED"]

    def test_multi_attrs(self):
        defs = parse_defs("共振追涨: ZX AND 条件3, COLORMAGENTA, NODRAW;")
        assert defs[0].expr == "ZX AND 条件3"
        assert defs[0].attrs == ["COLORMAGENTA", "NODRAW"]

    def test_semicolon_inside_string_does_not_split(self):
        defs = parse_defs("T:'含;分号';B:=2;")
        assert [d.name for d in defs] == ["T", "B"]
        assert defs[0].expr == "'含;分号'"

    def test_comment_with_semicolon_does_not_split(self):
        defs = parse_defs("A:=1;{注释;含分号}\nB:=2;")
        assert [d.name for d in defs] == ["A", "B"]

    def test_trailing_statement_without_semicolon(self):
        defs = parse_defs("A:=1")
        assert [d.name for d in defs] == ["A"]

    def test_line_numbers_reported(self):
        defs = parse_defs("A:=1;\n\nB:=2;")
        assert defs[0].line == 1
        assert defs[1].line == 3

    def test_blank_and_pure_comment_source(self):
        assert parse_defs("") == []
        assert parse_defs("  {只有注释}  ") == []


class TestStripDrawAttrs:
    def test_simple(self):
        expr, attrs = strip_draw_attrs("CLOSE,COLORRED")
        assert expr == "CLOSE"
        assert attrs == ["COLORRED"]

    def test_nested_parens_protected(self):
        # 括号内的逗号不是属性分隔符
        expr, attrs = strip_draw_attrs("IF(C>O,1,0),COLORRED,LINETHICK2")
        assert expr == "IF(C>O,1,0)"
        assert attrs == ["COLORRED", "LINETHICK2"]

    def test_no_attrs(self):
        expr, attrs = strip_draw_attrs("CLOSE")
        assert expr == "CLOSE"
        assert attrs == []

    def test_function_name_with_comma_not_treated_as_attr(self):
        # "MA(C,5)" 尾部是括号内容，不能被误剥为属性
        expr, attrs = strip_draw_attrs("MA(C,5)")
        assert expr == "MA(C,5)"
        assert attrs == []


# ---------------------------------------------------------------- dictionary

class TestDictionaryData:
    def test_funcs_loaded(self):
        from core.engine.tdx_formula import dictionary
        assert len(dictionary.FUNCS) >= 390
        assert "MA" in dictionary.FUNCS
        assert "EMA" in dictionary.FUNCS

    def test_manual_entry_overrides_dll(self):
        # BUYVOL 同时存在于手工词典与 DLL 提取表，合并后手工条目胜出
        from core.engine.tdx_formula import dictionary
        assert dictionary.FUNCS["BUYVOL"]["cat"] == "行情数据"
        assert dictionary.FUNCS["BUYVOL"].get("src") != "dll"

    def test_capability_lists(self):
        from core.engine.tdx_formula import dictionary
        assert "ZIG" in dictionary.FUTURE
        assert "WINNER" in dictionary.FUTURE
        assert "DYNAINFO" in dictionary.REALTIME
        assert "CLOSE" in dictionary.FIELDS


class TestPrecheck:
    def test_clean_formula_no_issues(self):
        src = "MA5:MA(C,5);X:CROSS(MA5,MA(C,10));"
        assert precheck(src) == []

    def test_future_function_rejected(self):
        issues = precheck("X:ZIG(C,3);")
        assert any(i.level == "error" and i.name == "ZIG" for i in issues)

    def test_realtime_function_rejected(self):
        issues = precheck("X:DYNAINFO(7);")
        assert any(i.level == "error" and i.name == "DYNAINFO" for i in issues)

    def test_data_tagged_function_rejected(self):
        # BUYVOL 带 fl:["data"]（需分笔数据），内置引擎不支持
        issues = precheck("X:BUYVOL>1000;")
        assert any(i.level == "error" and i.name == "BUYVOL" for i in issues)

    def test_unknown_function_warns(self):
        issues = precheck("X:NOTAFUNC(C,5);")
        assert any(i.level == "warning" and i.name == "NOTAFUNC" for i in issues)

    def test_strings_and_comments_ignored(self):
        src = "X:'DYNAINFO';{ZIG(C,3)};Y:MA(C,5);"
        assert precheck(src) == []

    def test_issue_has_line_number(self):
        issues = precheck("A:=1;\n\nX:ZIG(C,3);")
        assert all(i.line == 3 for i in issues if i.name == "ZIG")

    def test_fields_not_flagged_as_unknown(self):
        # O/H/L/C/V 等行情字段不带括号使用，不应出现在调用扫描里
        assert precheck("X:O<H AND C>V;") == []
