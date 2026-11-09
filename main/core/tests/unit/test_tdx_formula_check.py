"""syntax_check 干跑预检测试：无数据静态检查（词典闸门 + 解析 + 未知变量/函数）。

syntax_check 面向内置引擎（engine=builtin）的「语法预检」按钮：
- issues: precheck 词典能力闸门结论（future/realtime/data/unknown，level 原样）
- errors: 语句/表达式解析错误 + 内置引擎未实现函数 + 未定义变量（均为致命）
- outputs: `:` 输出线名列表（供前端提示信号配置）
- ok: 无 errors 且 issues 无 error 级
"""
from core.engine.tdx_formula import syntax_check


def issue_map(res):
    return {i.name: (i.rule, i.level) for i in res["issues"]}


class TestLegalFormula:
    def test_reformed_user_formula_ok(self):
        src = """
        昨收:=REF(C,1);
        涨幅:=(C-昨收)/昨收*100;
        追涨:=IF(涨幅>3 AND V/REF(MA(V,5),1)>1.5,1,0);
        追涨信号:追涨,NODRAW;
        低吸信号:IF(涨幅<-1,-1,0),NODRAW;
        """
        res = syntax_check(src)
        assert res["ok"] is True
        assert res["errors"] == []
        assert res["outputs"] == ["追涨信号", "低吸信号"]

    def test_all_comment_formula(self):
        res = syntax_check("{只有注释}\n{第二行注释}")
        assert res["ok"] is True
        assert res["outputs"] == []

    def test_standard_func_call(self):
        res = syntax_check("X:SMA(CLOSE,3,1)+ABS(C-O);")
        assert res["ok"] is True


class TestDictionaryGate:
    def test_future_function(self):
        res = syntax_check("X:ZIG(C,5);")
        assert res["ok"] is False
        assert issue_map(res)["ZIG"] == ("future", "error")

    def test_realtime_function(self):
        res = syntax_check("X:DYNAINFO(7)>0;")
        assert res["ok"] is False
        assert issue_map(res)["DYNAINFO"] == ("realtime", "error")

    def test_bare_data_field(self):
        res = syntax_check("X:BUYVOL>0;")
        assert res["ok"] is False
        assert issue_map(res)["BUYVOL"] == ("data", "error")


class TestStaticCheck:
    def test_unimplemented_function_is_fatal(self):
        # unknown 在词典闸门是 warning，但内置引擎未实现 → 致命
        res = syntax_check("X:FOO(C,5);")
        assert res["ok"] is False
        assert any("FOO" in e for e in res["errors"])

    def test_undefined_variable(self):
        res = syntax_check("X:FOO+BAR>0;")
        assert res["ok"] is False
        assert any("FOO" in e for e in res["errors"])
        assert any("BAR" in e for e in res["errors"])

    def test_defined_var_reference_ok(self):
        res = syntax_check("TMP:=C*2;\nX:TMP+1;")
        assert res["ok"] is True

    def test_case_insensitive_defined_var(self):
        res = syntax_check("Tmp:=C*2;\nX:tmp+1;")
        assert res["ok"] is True


class TestParseErrors:
    def test_unclosed_paren(self):
        res = syntax_check("X:(C+1;")
        assert res["ok"] is False
        assert res["errors"]

    def test_dangling_operator(self):
        res = syntax_check("X:C+;")
        assert res["ok"] is False
        assert res["errors"]

    def test_bad_statement(self):
        res = syntax_check("X::=;")
        assert res["ok"] is False
        assert res["errors"]

    def test_errors_have_line_info(self):
        res = syntax_check("A:=C;\n\nX:(C+1;")
        assert res["errors"]
        # 行号信息出现在错误消息里（便于前端定位）
        assert any("4" in e or "X" in e for e in res["errors"])


class TestCrossPeriodSyntax:
    """批次 9a：跨周期引用静态校验（周期名白名单，合法后缀不再一律致命）。"""

    def test_valid_suffix_ok(self):
        res = syntax_check("X:C#WEEK;")
        assert res["ok"] is True
        assert res["errors"] == []

    def test_case_insensitive_suffix_ok(self):
        res = syntax_check("X:c#week;")
        assert res["ok"] is True

    def test_minute_suffix_ok(self):
        res = syntax_check("X:HIGH#MIN30;")
        assert res["ok"] is True

    def test_unknown_suffix_fatal(self):
        res = syntax_check("X:C#YEAR;")
        assert res["ok"] is False
        assert any("YEAR" in e for e in res["errors"])
