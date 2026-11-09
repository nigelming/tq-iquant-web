"""0016 信号标准化向导 normalize_formula 单元测试（TDD）。

覆盖三类规则的生成源码快照、`:` 线降级、绘图属性丢弃、注释保留、
命名冲突（XG2/XD 序号）、多线顺序、大小写不敏感、非法输入报错。
"""

import pytest

from core.engine.tdx_formula import syntax_check
from core.engine.tdx_formula.normalizer import normalize_formula

SRC_MACD = "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);\nDEA:EMA(DIF,9);\nMACD:(DIF-DEA)*2;"


def _norm(src, plans):
    return normalize_formula(src, plans)


# ---------------------------------------------------------------- 三类规则快照
class TestRuleSnapshots:
    def test_cross_zero_both_generates_xg_xd_pair(self):
        res = _norm(SRC_MACD, [{"line": "MACD", "rule": "cross_zero",
                                "params": {"direction": "both"}}])
        assert res["new_content"] == (
            "DIF:=EMA(CLOSE,12)-EMA(CLOSE,26);\n"
            "DEA:=EMA(DIF,9);\n"
            "MACD:=(DIF-DEA)*2;\n"
            "XG:CROSS(MACD,0);\n"
            "XD:CROSS(0,MACD);"
        )
        assert [(o["name"], o["expr"]) for o in res["output_lines"]] == [
            ("XG", "CROSS(MACD,0)"), ("XD", "CROSS(0,MACD)")]
        # 生成的公式须过内置引擎语法预检（E7：通达信可编译的后端侧等价校验）
        assert syntax_check(res["new_content"])["ok"] is True

    def test_cross_zero_up_single_line_xg(self):
        res = _norm("M:CLOSE;", [{"line": "M", "rule": "cross_zero",
                                  "params": {"direction": "up"}}])
        assert res["new_content"] == "M:=CLOSE;\nXG:CROSS(M,0);"
        assert [o["name"] for o in res["output_lines"]] == ["XG"]

    def test_cross_zero_down_single_line_xg(self):
        res = _norm("M:CLOSE;", [{"line": "M", "rule": "cross_zero",
                                  "params": {"direction": "down"}}])
        assert res["new_content"] == "M:=CLOSE;\nXG:CROSS(0,M);"

    def test_above_threshold_gt(self):
        res = _norm("MACD:(C-REF(C,1))/REF(C,1);", [
            {"line": "MACD", "rule": "above_threshold",
             "params": {"threshold": 0.02, "direction": "gt"}}])
        assert res["new_content"] == "MACD:=(C-REF(C,1))/REF(C,1);\nXG:MACD>0.02;"

    def test_above_threshold_lt_int_threshold(self):
        res = _norm("M:CLOSE;", [{"line": "M", "rule": "above_threshold",
                                  "params": {"threshold": 2, "direction": "lt"}}])
        assert res["new_content"] == "M:=CLOSE;\nXG:M<2;"

    def test_threshold_float_tail_zeros_trimmed(self):
        res = _norm("M:CLOSE;", [{"line": "M", "rule": "above_threshold",
                                  "params": {"threshold": 0.30000000000000004}}])
        assert "XG:M>0.3;" in res["new_content"]

    def test_map_values_if_chain_with_zero_fallback(self):
        res = _norm("V1:CLOSE;", [{"line": "V1", "rule": "map_values",
                                   "params": {"mappings": [
                                       {"from": 1, "to": 1},
                                       {"from": -1, "to": -1}]}}])
        assert res["new_content"] == "V1:=CLOSE;\nXG:IF(V1=1,1,IF(V1=-1,-1,0));"
        assert syntax_check(res["new_content"])["ok"] is True
        assert "→1" in res["output_lines"][0]["meaning"]
        assert "其他=0" in res["output_lines"][0]["meaning"]

    def test_map_values_four_pairs_nested_depth(self):
        res = _norm("V1:CLOSE;", [{"line": "V1", "rule": "map_values",
                                   "params": {"mappings": [
                                       {"from": 1, "to": 2}, {"from": -1, "to": -2},
                                       {"from": 2, "to": 1}, {"from": 0.5, "to": -1}]}}])
        assert res["output_lines"][0]["expr"] == \
            "IF(V1=1,2,IF(V1=-1,-2,IF(V1=2,1,IF(V1=0.5,-1,0))))"


# ---------------------------------------------------------------- 降级与清理
class TestDegrade:
    def test_all_output_lines_degraded(self):
        res = _norm(SRC_MACD, [{"line": "MACD", "rule": "cross_zero",
                                "params": {"direction": "up"}}])
        content = res["new_content"]
        assert "DIF:=" in content and "DEA:=" in content and "MACD:=" in content
        # 不再残留 `:` 输出线头（生成线自身除外）
        for stmt in content.split("\n"):
            if stmt.startswith(("XG", "XD")):
                continue
            assert ":=" in stmt or not stmt.strip()

    def test_draw_attrs_dropped_with_warning(self):
        src = "A:CLOSE,COLORRED,LINETHICK2;"
        res = _norm(src, [{"line": "A", "rule": "cross_zero",
                           "params": {"direction": "up"}}])
        assert res["new_content"] == "A:=CLOSE;\nXG:CROSS(A,0);"
        assert any("COLORRED" in w and "LINETHICK2" in w for w in res["warnings"])

    def test_attrs_before_paren_end_not_eaten(self):
        # 表达式内逗号不能误当属性分隔（括号平衡保护）
        src = "A:IF(C>O,1,0),COLORRED;"
        res = _norm(src, [{"line": "A", "rule": "above_threshold",
                           "params": {"threshold": 1}}])
        assert res["new_content"].split("\n")[0] == "A:=IF(C>O,1,0);"

    def test_full_line_comment_preserved(self):
        src = "{MACD 主体}\nDIF:EMA(C,12); {快线}\nDEA:=EMA(DIF,9)"
        res = _norm(src, [{"line": "DIF", "rule": "cross_zero",
                           "params": {"direction": "down"}}])
        assert res["new_content"] == (
            "{MACD 主体}\n"
            "DIF:=EMA(C,12);\n"
            "{快线}\n"
            "DEA:=EMA(DIF,9);\n"
            "XG:CROSS(0,DIF);"
        )

    def test_inline_comment_kept_in_statement(self):
        src = "A:CLOSE {收盘};"
        res = _norm(src, [{"line": "A", "rule": "above_threshold",
                           "params": {"threshold": 1}}])
        assert res["new_content"] == "A:=CLOSE {收盘};\nXG:A>1;"

    def test_trailing_comment_statement_kept(self):
        src = "A:CLOSE;\n{尾部注释}"
        res = _norm(src, [{"line": "A", "rule": "cross_zero",
                           "params": {"direction": "up"}}])
        assert res["new_content"].endswith("{尾部注释}\nXG:CROSS(A,0);")


# ---------------------------------------------------------------- 命名与顺序
class TestNamingAndOrder:
    def test_xg_conflict_falls_to_xg2(self):
        src = "XG:CLOSE;V:CLOSE;"
        res = _norm(src, [{"line": "V", "rule": "cross_zero",
                           "params": {"direction": "both"}}])
        assert [o["name"] for o in res["output_lines"]] == ["XG2", "XD"]
        assert res["new_content"].endswith("XG2:CROSS(V,0);\nXD:CROSS(0,V);")

    def test_multiple_plans_alloc_in_order(self):
        src = "A:CLOSE;B:CLOSE;"
        res = _norm(src, [
            {"line": "A", "rule": "cross_zero", "params": {"direction": "up"}},
            {"line": "B", "rule": "cross_zero", "params": {"direction": "up"}},
        ])
        assert [o["name"] for o in res["output_lines"]] == ["XG", "XG2"]
        assert res["new_content"].endswith("XG:CROSS(A,0);\nXG2:CROSS(B,0);")

    def test_map_values_prefers_xg(self):
        res = _norm("V1:CLOSE;", [{"line": "V1", "rule": "map_values",
                                   "params": {"mappings": [{"from": 1, "to": 1}]}}])
        assert res["output_lines"][0]["name"] == "XG"

    def test_line_name_case_insensitive_uses_canonical(self):
        res = _norm("MACD:CLOSE;", [{"line": "macd", "rule": "cross_zero",
                                     "params": {"direction": "up"}}])
        assert "XG:CROSS(MACD,0);" in res["new_content"]


# ---------------------------------------------------------------- 非法输入
class TestValidation:
    def test_empty_plans_error(self):
        with pytest.raises(ValueError, match="plans 不能为空"):
            _norm(SRC_MACD, [])

    def test_no_defs_error(self):
        with pytest.raises(ValueError, match="没有可识别的定义语句"):
            _norm("MA(C,5);", [{"line": "X", "rule": "cross_zero", "params": {}}])

    def test_line_not_found_error(self):
        with pytest.raises(ValueError, match="输出线 NOPE 不存在"):
            _norm(SRC_MACD, [{"line": "NOPE", "rule": "cross_zero", "params": {}}])

    def test_intermediate_variable_not_allowed(self):
        src = "A:=CLOSE;B:A+1;"
        with pytest.raises(ValueError, match="不是 `:` 输出线"):
            _norm(src, [{"line": "A", "rule": "cross_zero", "params": {}}])

    def test_duplicate_line_error(self):
        plan = {"line": "MACD", "rule": "cross_zero", "params": {"direction": "up"}}
        with pytest.raises(ValueError, match="重复配置"):
            _norm(SRC_MACD, [plan, dict(plan)])

    def test_unknown_rule_error(self):
        with pytest.raises(ValueError, match="未知规则"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "zig", "params": {}}])

    def test_cross_zero_bad_direction(self):
        with pytest.raises(ValueError, match="both/up/down"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "cross_zero",
                              "params": {"direction": "side"}}])

    def test_above_threshold_missing_threshold(self):
        with pytest.raises(ValueError, match="threshold"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "above_threshold", "params": {}}])

    def test_map_values_empty_mappings(self):
        with pytest.raises(ValueError, match="mappings"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "map_values",
                              "params": {"mappings": []}}])

    def test_map_values_too_many_pairs(self):
        mappings = [{"from": i, "to": 1} for i in range(5)]
        with pytest.raises(ValueError, match="最多 4 个"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "map_values",
                              "params": {"mappings": mappings}}])

    def test_map_values_bad_target(self):
        with pytest.raises(ValueError, match="-2/-1/1/2"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "map_values",
                              "params": {"mappings": [{"from": 1, "to": 3}]}}])

    def test_map_values_target_zero_rejected(self):
        with pytest.raises(ValueError, match="-2/-1/1/2"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "map_values",
                              "params": {"mappings": [{"from": 1, "to": 0}]}}])

    def test_map_values_duplicate_from(self):
        with pytest.raises(ValueError, match="重复"):
            _norm(SRC_MACD, [{"line": "MACD", "rule": "map_values",
                              "params": {"mappings": [
                                  {"from": 1, "to": 1}, {"from": 1.0, "to": -1}]}}])
