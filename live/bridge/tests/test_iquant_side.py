"""桥方向归一单测：两腿（仿真/实盘）必须一致。

背景（2026-11-06 真机实证）：QMT 的 DEAL/ORDER 对象 ``m_nDirection`` 对股票
恒为 48（EEntrustBS），用它判买卖会把卖单当买单（601827 卖出 100 股返回
direction=48）。真正区分股票买卖的是 ``m_nOffsetFlag``：48=买，49-54=卖。
"""
import importlib.util
import pathlib

import pytest

_BRIDGE_DIR = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, str(_BRIDGE_DIR / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["iquant_bridge", "iquant_bridge_live"])
def test_row_side_prefers_offset_flag(name):
    mod = _load(name)
    assert mod._row_side(48, 48) == "BUY"      # 开仓/买入
    assert mod._row_side(49, 48) == "SELL"     # 平仓/卖出（direction 同为 48）
    for off in (50, 51, 52, 53, 54):
        assert mod._row_side(off, 48) == "SELL"


@pytest.mark.parametrize("name", ["iquant_bridge", "iquant_bridge_live"])
def test_row_side_never_defaults_to_buy(name):
    mod = _load(name)
    assert mod._row_side(None, 48) is None     # 旧桥形态：不可判定
    assert mod._row_side(None, 999) is None
    assert mod._row_side(999, None) is None


@pytest.mark.parametrize("name", ["iquant_bridge", "iquant_bridge_live"])
def test_row_side_direction_fallback_is_strict(name):
    mod = _load(name)
    assert mod._row_side(None, 23) == "BUY"    # opType 股票买入
    assert mod._row_side(None, 24) == "SELL"   # opType 股票卖出
    assert mod._row_side(None, 0) == "BUY"
    assert mod._row_side(None, 1) == "SELL"
