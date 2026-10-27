"""_summarize_line ones 计数与实盘引擎信号判定同构性测试。

此前真机探针实证 SDK 返回 '1.00' 形态字符串，原 str(v) == "1" 比对会系统性
漏计；复用 core.engine.live.timing._to_int 后与实盘信号判定同一口径。
"""
import pytest

from core.api.formulas import _summarize_line


@pytest.mark.parametrize("val", ["1", 1.0, "1.00"])
def test_ones_counts_one_forms(val):
    """'1'/1.0/'1.00' 都计 1（引擎同构）。"""
    assert _summarize_line([val])["ones"] == 1


@pytest.mark.parametrize("val", ["0.00", "0", None])
def test_ones_ignores_zero_and_none(val):
    """'0.00'/'0'/None 不计（0 是无信号常态值）。"""
    assert _summarize_line([val])["ones"] == 0


def test_ones_mixed_sequence_and_dict_form():
    """混合序列计数正确；{Date, Value} 字典形态取 Value 判定。"""
    vals = [
        {"Date": "2026-10-23", "Value": "1.00"},
        "0.00",
        None,
        {"Date": "2026-10-24", "Value": 1.0},
        "0",
    ]
    summary = _summarize_line(vals)
    assert summary["n"] == 5
    assert summary["ones"] == 2
    assert summary["last_values"] == ["None", "1.0", "0"]
