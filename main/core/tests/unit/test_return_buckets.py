"""build_return_buckets 纯函数单测。

直接构造 SimpleNamespace 模拟 BacktestDailySnapshot（snap_date, total_value），
不依赖内存 SQLite / ORM / Evaluator。覆盖：

- 正常多月数据：月度矩阵行列数 + 首行/末行值
- 日内多条快照同日：取最后一条净值（日收盘）
- 跨年边界：12月→1月正确分组
- 数据不足（<2 个不同日期）：返回空结构
- 月度矩阵含缺失月（只有部分月有数据）→ None 填充
"""
from datetime import date
from types import SimpleNamespace

import pytest

from core.services.backtest_service import build_return_buckets


def _snap(d: str, v: str) -> SimpleNamespace:
    """快捷构造模拟快照（snap_date=str→date, total_value=str）。"""
    return SimpleNamespace(
        snap_date=date.fromisoformat(d),
        total_value=v,
    )


# ---------------------------------------------------------------------------
# 正常路径：每月末一个快照，3 年数据
# ---------------------------------------------------------------------------
class TestNormalPath:
    """构造 3 年 × 12 月 = 36 个月末快照，净值从 100 按固定月收益递增。"""

    @pytest.fixture
    def snaps_3year(self):
        """净值按 +1%/月 递增（100 → 101 → 102…），连续 36 个月。"""
        snaps = []
        base = 100.0
        for year in (2021, 2022, 2023):
            for month in range(1, 13):
                snaps.append(_snap(f"{year}-{month:02d}-28", f"{base:.2f}"))
                base *= 1.01
        return snaps

    def test_monthly_matrix_dimensions(self, snaps_3year):
        """月度矩阵应有 3 行（2021/2022/2023）× 12 列。"""
        result = build_return_buckets(snaps_3year)
        assert result["monthly_matrix"]["years"] == [2021, 2022, 2023]
        assert result["monthly_matrix"]["months"] == [
            f"{m}月" for m in range(1, 13)
        ]
        assert len(result["monthly_matrix"]["matrix"]) == 3

    def test_monthly_matrix_first_value(self, snaps_3year):
        """2021 年 2 月（第二行第二列）收益 ≈ +1%。"""
        result = build_return_buckets(snaps_3year)
        feb_2021 = result["monthly_matrix"]["matrix"][0][1]
        jan_2021 = result["monthly_matrix"]["matrix"][0][0]
        assert jan_2021 is None  # 首月无上月可比
        assert feb_2021 is not None
        assert abs(feb_2021 - 1.0) < 0.01  # ≈ 1%

    def test_yearly_returns_len(self, snaps_3year):
        """每年有 daily_returns（至少月末快照数的天数）+ annual_return 非空。"""
        result = build_return_buckets(snaps_3year)
        assert len(result["yearly"]) == 3
        for yr in result["yearly"]:
            assert "year" in yr
            assert "annual_return" in yr
            assert yr["annual_return"] is not None


# ---------------------------------------------------------------------------
# 日内多条快照同日：取最后一条
# ---------------------------------------------------------------------------
class TestIntradayMultipleSnaps:
    """同一天有多条快照（日内 1m/30m 回测），应取最后一条的 total_value。"""

    def test_same_day_last_wins(self):
        snaps = [
            _snap("2024-03-15", "100.00"),
            _snap("2024-03-15", "102.00"),  # 同日第二条 → 覆盖
            _snap("2024-03-16", "103.00"),
        ]
        result = build_return_buckets(snaps)
        # 日净值序列：102.00(03-15), 103.00(03-16)
        daily = result["yearly"][0]["daily_returns"] if result["yearly"] else []
        assert len(daily) >= 1
        expected_r = (103.00 - 102.00) / 102.00
        assert abs(daily[-1] - expected_r) < 1e-6


# ---------------------------------------------------------------------------
# 跨年边界
# ---------------------------------------------------------------------------
class TestCrossYearBoundary:
    """12月末 → 1月初的分组不混淆。"""

    def test_dec_jan_split(self):
        snaps = [
            _snap("2023-12-29", "100.00"),
            _snap("2024-01-02", "105.00"),  # 年收益 ≈ +5%
        ]
        result = build_return_buckets(snaps)
        assert len(result["yearly"]) == 2
        years = [yr["year"] for yr in result["yearly"]]
        assert 2023 in years
        assert 2024 in years

    def test_cross_year_annual_return(self):
        """2023-12-29 100 → 2024-01-02 105，2024 年 annual_return ≈ 5%。"""
        snaps = [
            _snap("2023-12-29", "100.00"),
            _snap("2024-01-02", "105.00"),
        ]
        result = build_return_buckets(snaps)
        yr_2024 = [y for y in result["yearly"] if y["year"] == 2024][0]
        assert abs(yr_2024["annual_return"] - 0.05) < 1e-4


# ---------------------------------------------------------------------------
# 数据不足
# ---------------------------------------------------------------------------
class TestInsufficientData:
    """快照不足（<2 个不同日期）→ 返回空结构。"""

    def test_empty_snapshots(self):
        result = build_return_buckets([])
        assert result["monthly_matrix"]["years"] == []
        assert result["yearly"] == []

    def test_single_snapshot(self):
        snaps = [_snap("2024-01-15", "100.00")]
        result = build_return_buckets(snaps)
        assert result["monthly_matrix"]["years"] == []
        assert result["yearly"] == []

    def test_two_same_day(self):
        snaps = [
            _snap("2024-01-15", "100.00"),
            _snap("2024-01-15", "101.00"),
        ]
        # 同一天两条 → 只有 1 个不同日期 → 返回空
        result = build_return_buckets(snaps)
        assert result["monthly_matrix"]["years"] == []


# ---------------------------------------------------------------------------
# 月度矩阵缺失月填充
# ---------------------------------------------------------------------------
class TestMissingMonths:
    """只有部分月份有快照 → 其余月份 matrix 值为 None，text 为 ''。"""

    def test_partial_months(self):
        snaps = [
            _snap("2024-03-29", "100.00"),
            _snap("2024-04-29", "103.00"),  # 4 月收益 +3%
            _snap("2024-06-28", "104.00"),  # 5 月无快照
        ]
        result = build_return_buckets(snaps)
        mat = result["monthly_matrix"]["matrix"]
        assert len(mat) == 1  # 2024 年一行
        row = mat[0]
        # 1-2 月无数据(None), 3 月无上月(None), 4 月+3%, 5 月无数据(None), 6 月有
        assert row[0] is None   # 1 月
        assert row[2] is None   # 3 月（无上月）
        assert row[3] is not None  # 4 月
        assert abs(row[3] - 3.0) < 0.1
        assert row[4] is None   # 5 月
