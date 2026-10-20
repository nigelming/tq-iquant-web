"""P2b 参数微扰纯函数单测。

scaled_value：乘性缩放（整型按因子方向 floor/ceil 且 ≥1，不可扰动返回 None）。
build_perturb_rounds：按子策略参数可扰动性生成轮次。
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.services.param_perturb_service import build_perturb_rounds, scaled_value


class TestScaledValue:
    def test_ratio_fields_multiply(self):
        assert scaled_value(Decimal("0.05"), 0.9, False) == pytest.approx(0.045)
        assert scaled_value(Decimal("0.05"), 1.1, False) == pytest.approx(0.055)

    def test_zero_base_not_perturbable(self):
        # 基准 0（如 trailing_stop_ratio=0 关闭）→ 不生成轮次
        assert scaled_value(Decimal("0"), 0.9, False) is None
        assert scaled_value(Decimal("0"), 1.1, False) is None

    def test_none_base_not_perturbable(self):
        assert scaled_value(None, 0.9, False) is None
        assert scaled_value(None, 1.1, True) is None

    def test_int_floor_ceil_away_from_base(self):
        # 低方向 floor、高方向 ceil：max_positions=5 ±10% → 4 / 6
        assert scaled_value(5, 0.9, True) == 4
        assert scaled_value(5, 1.1, True) == 6

    def test_int_min_one_and_equal_base_skipped(self):
        # max_add_count=1：低方向 floor(0.9)=0→下限 1=基准 → 不可扰动；
        # 高方向 ceil(1.1)=2 → 可扰动
        assert scaled_value(1, 0.9, True) is None
        assert scaled_value(1, 1.1, True) == 2

    def test_minus_one_semantic_field_not_perturbable(self):
        # W5-O1：add_position_threshold=-1（跳过加仓回撤检查）→ 双向不可扰动
        assert scaled_value(Decimal("-1"), 0.9, False) is None
        assert scaled_value(Decimal("-1"), 1.1, False) is None
        assert scaled_value(Decimal("-1"), 1.1, True) is None

    def test_clamp_capital_ratio_high_direction_skipped(self):
        # W5-O1b(M4)：capital_ratio=1.0 高方向 ×1.1 → 钳制回 1.0 = 基准 → 不可扰动
        assert scaled_value(Decimal("1.0"), 1.1, False, "capital_ratio") is None
        # 低方向可扰动
        assert scaled_value(Decimal("1.0"), 0.9, False, "capital_ratio") == pytest.approx(0.9)

    def test_clamp_ratio_to_upper_bound(self):
        # W5-O1b(M4)：stop_loss_ratio=0.95 高方向 ×1.1=1.045 → 钳制到 1.0（≠基准）可扰动
        v = scaled_value(Decimal("0.95"), 1.1, False, "stop_loss_ratio")
        assert v == pytest.approx(1.0)

    def test_no_clamp_without_field(self):
        # 不传 field → 保持原行为（不钳制）
        assert scaled_value(Decimal("1.0"), 1.1, False) == pytest.approx(1.1)


class TestBuildPerturbRounds:
    def _strat(self, **kw):
        defaults = {
            "capital_ratio": Decimal("0.6"), "max_positions": 5,
            "single_open_ratio": Decimal("0.1"), "stop_loss_ratio": Decimal("0.05"),
            "take_profit_ratio": Decimal("0.2"), "trailing_stop_ratio": Decimal("0"),
            "add_position_threshold": Decimal("0.05"), "max_add_count": 2,
            "add_position_ratio": Decimal("0.1"), "reduce_position_ratio": Decimal("0.3"),
        }
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_full_params_generate_2_rounds_each(self):
        rounds = build_perturb_rounds([self._strat()], 0.10)
        # trailing_stop_ratio=0 跳过；其余 9 参数 × 2 方向 = 18 轮
        assert len(rounds) == 18
        assert all(r["direction"] in ("low", "high") for r in rounds)
        by_param = {}
        for r in rounds:
            by_param.setdefault(r["param"], set()).add(r["direction"])
        assert "trailing_stop_ratio" not in by_param
        assert by_param["max_positions"] == {"low", "high"}

    def test_int_edge_only_high_direction(self):
        rounds = build_perturb_rounds([self._strat(max_add_count=1)], 0.10)
        mac = [r for r in rounds if r["param"] == "max_add_count"]
        assert [r["direction"] for r in mac] == ["high"]

    def test_factor_symmetric(self):
        rounds = build_perturb_rounds([self._strat()], 0.10)
        factors = {round(r["factor"], 2) for r in rounds}
        assert factors == {0.9, 1.1}

    def test_minus_one_field_dropped_from_rounds(self):
        # W5-O1：add_position_threshold=-1（跳过加仓回撤检查）→ 双向都不生成轮次
        rounds = build_perturb_rounds([self._strat(add_position_threshold=Decimal("-1"))], 0.10)
        assert not any(r["param"] == "add_position_threshold" for r in rounds)

    def test_capital_ratio_at_max_high_direction_dropped(self):
        # W5-O1b(M4)：capital_ratio=1.0（满仓）高方向 ×1.1 钳制回 1.0 → 无实际扰动
        # → 只留低方向轮次
        rounds = build_perturb_rounds([self._strat(capital_ratio=Decimal("1.0"))], 0.10)
        cr = [r for r in rounds if r["param"] == "capital_ratio"]
        assert [r["direction"] for r in cr] == ["low"]
