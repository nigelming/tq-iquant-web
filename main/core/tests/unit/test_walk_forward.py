"""C8 walk-forward 窗口生成纯函数单测。

滚动 train_years 年训练 + test_years 年验证逐段前移：
窗口 i = [start + i*test_years 年, +train+test 年)，右端 ≤ end 才收。
"""
from datetime import date

from core.services.walk_forward_service import build_walk_forward_windows


class TestBuildWalkForwardWindows:
    def test_rolling_3y_train_1y_test(self):
        ws = build_walk_forward_windows(date(2020, 1, 1), date(2026, 1, 1), 3, 1)
        assert len(ws) == 3
        # 窗 1：2020~2024（训 2020-2023 / 验 2023-2024）
        assert ws[0]["index"] == 1
        assert ws[0]["start"] == date(2020, 1, 1)
        assert ws[0]["end"] == date(2024, 1, 1)
        assert ws[0]["train"] == {"start": date(2020, 1, 1), "end": date(2023, 1, 1)}
        assert ws[0]["test"] == {"start": date(2023, 1, 1), "end": date(2024, 1, 1)}
        # 逐段前移 1 年
        assert ws[1]["start"] == date(2021, 1, 1)
        assert ws[1]["end"] == date(2025, 1, 1)
        # 末窗右端 = end 仍收
        assert ws[2]["start"] == date(2022, 1, 1)
        assert ws[2]["end"] == date(2026, 1, 1)

    def test_exact_fit_single_window(self):
        ws = build_walk_forward_windows(date(2020, 1, 1), date(2024, 1, 1), 3, 1)
        assert len(ws) == 1

    def test_insufficient_range_no_window(self):
        # 不足一个完整窗口（3+1 年）→ 空
        ws = build_walk_forward_windows(date(2020, 1, 1), date(2023, 12, 31), 3, 1)
        assert ws == []

    def test_two_year_test_step(self):
        ws = build_walk_forward_windows(date(2020, 1, 1), date(2027, 1, 1), 2, 2)
        assert [(w["start"].year, w["end"].year) for w in ws] == [
            (2020, 2024), (2022, 2026),
        ]
        assert ws[0]["test"] == {"start": date(2022, 1, 1), "end": date(2024, 1, 1)}

    def test_leap_day_start_falls_back_to_feb28(self):
        ws = build_walk_forward_windows(date(2020, 2, 29), date(2025, 2, 28), 3, 1)
        assert ws[0]["start"] == date(2020, 2, 29)
        assert ws[0]["end"] == date(2024, 2, 29)  # 2024 也是闰年
        assert ws[0]["train"]["end"] == date(2023, 2, 28)  # 2023 非闰年回退 28 日

    def test_indices_are_1_based(self):
        ws = build_walk_forward_windows(date(2020, 1, 1), date(2026, 1, 1), 3, 1)
        assert [w["index"] for w in ws] == [1, 2, 3]
