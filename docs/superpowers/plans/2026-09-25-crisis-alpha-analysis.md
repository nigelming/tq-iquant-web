# 危机Alpha 专题分析迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `E:\宏观模型回测引擎\宏观模型回测引擎增强版.py` 的「🌪️ 危机Alpha 专题分析」（12 模块 + 9 维加权综合危机保护评分）完整迁移到本项目回测详情页，排除「全部策略危机保护评分横向对比」。

**Architecture:** 分析器（pandas，1:1 移植源 L2815-3277）落在 `core/engine/crisis_alpha.py`；回测主事务 commit 之后独立计算并落库到新表 `backtest_crisis_alphas`（任何失败都不影响回测状态）；详情接口透出 `crisis_alpha` 键；前端在「整体表现指标」后新增独立 section（8 卡上提 + 12 模块 ECharts 复刻）。

**Tech Stack:** Python 3.13 / pandas 3.0.5 / SQLAlchemy 2.x / Alembic / FastAPI / SQLite；Vue 3 + TypeScript + ECharts + Vitest。

**设计依据:** `docs/superpowers/specs/2026-09-25-crisis-alpha-analysis-design.md`（已获用户批准）

## Global Constraints

- **零注释**：本计划产出的所有代码（Python / TS / Vue / 迁移）**不写任何 `#` 注释与 docstring**。算法口径以 spec 文档为准，命名承载语义。
- **不新增运行依赖**：`pandas` 已在 `backtest_service.py:20` import（pytdx 传递依赖，lock 解析为 3.0.5），需在 `main/pyproject.toml` **显式声明** `pandas>=3.0.5`（lock 已有该版本，不触发新下载）。**禁止**引入 `numpy` 直接 import、`akshare`、任何新前端库。
- **算法 1:1**：所有阈值与判定口径照搬源文件，不得"优化"：`CRISIS_THRESHOLD=0.0`、`EXTREME_TOP_N=10`、`DEEP_DD_THRESHOLD=-0.05`、`ROLLING_CORR_WINDOW=52`、`DEEP_WEEK_TESTS=(-0.03,-0.05)`、`MONTH_BIG_MOVE=0.05`、`WEEKS_PER_YEAR=52`、`MIN_COMMON_DAYS=30`、`FULL_YEAR_WEEKS=40`；四档 ratio 边界 `<0.5` / `<=1.0+1e-12` / `>1.0+1e-12`；年度完整判定 `周数>=40`；无深跌周 / 无回撤事件 → 该维度 `50.0`。
- **JSON 安全（硬性）**：任何落库/出参的浮点必须经 `sanitize_for_json`；`NaN`/`Inf` → `None`。FastAPI 默认 `allow_nan=True` 会吐裸 `NaN`，JS `JSON.parse` 会抛错。测试必须用 `json.dumps(..., allow_nan=False)` 自检。
- **A 股红涨绿跌**：四档配色固定 `#d6403a`（逆势上涨）/`#f0a429`（跌得少）/`#9dc88d`（跟着跌）/`#1f6e4f`（跌更多），不得改动既有风险色与既有图表配色。
- **单 y 轴**：所有危机Alpha 图表各自单 y 轴，不设左右轴（与已完成的红涨绿跌计划一致）。
- **事务边界**：危机Alpha 计算必须在 `run_backtest` 主 `db.commit()` **之后**执行，用**独立事务**；异常一律吞掉并落 `status='failed'`，回测记录必须保持 `completed`。
- **主基准** = 组合 `benchmark_index`（空则回落 `000300.SH`）；6 宽基固定内置，取数失败跳过并记入 `skipped_benchmarks`，主基准失败才置 `failed`。
- **TDD**：每个任务先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。禁止先写实现后补测试。
- **验证命令**（后端在 `main/`，前端在 `web/`）：
  - `uv run pytest core/tests/unit/test_crisis_alpha.py -v`
  - `uv run pytest core/tests/unit/test_backtest_service.py -v`
  - `uv run pytest core/tests/integration/test_backtest_api.py -v`
  - `uv run pytest core/tests/ -q`（收尾全量）
  - `npx vitest run src/__tests__/Backtest.test.ts`
  - `npx vue-tsc --noEmit` / `npm run build`（收尾全量）

## 执行前置（一次性，不算任务）

当前工作区有上轮遗留的未提交改动，且分支需切换。

```powershell
# 1) 确认工作区只有那一个改动
git status --short
# 期望输出仅： M main/core/tests/unit/test_backtest_service.py

# 2) 提交遗留单测（本特性第一个提交）
git add main/core/tests/unit/test_backtest_service.py
git commit -m "test: 补充自定义基准指数取数单测"

# 3) 切新分支
git checkout -b feat/crisis-alpha-analysis
```

若第 1 步输出与期望不符，**停止**并向用户报告，不要自行处理其他改动。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `main/core/engine/crisis_alpha.py` | 新建 | 分析器 + 编排 + JSON 净化，纯函数无 IO |
| `main/core/tests/unit/test_crisis_alpha.py` | 新建 | 分析器单测（Task 1-3） |
| `main/core/models/backtest_crisis_alpha.py` | 新建 | ORM 模型（1:1 记录） |
| `main/core/models/__init__.py` | 修改 | 导出新模型 |
| `main/alembic/versions/f2a3b4c5d6e7_add_backtest_crisis_alphas.py` | 新建 | 建表迁移（down_revision=`e5f6a7b8c9d0`） |
| `main/core/services/backtest_service.py` | 修改 | 指数批量取数、快照→净值、计算/落库/重算、详情透出、run_backtest 接线 |
| `main/core/api/backtest.py` | 修改 | 新增 recompute 路由 + re-export |
| `main/pyproject.toml` | 修改 | 显式声明 pandas |
| `main/core/tests/unit/test_backtest_service.py` | 修改 | 指数取数/三态落库/重算/详情 单测 |
| `main/core/tests/integration/test_backtest_api.py` | 修改 | recompute 路由 + 详情含 crisis_alpha |
| `web/src/api/index.ts` | 修改 | 12 模块 TS 接口 + `crisis_alpha` 字段 + recompute client |
| `web/src/views/Backtest.vue` | 修改 | 8 卡上提 + section 骨架 + 12 模块 + 9 个 ECharts |
| `web/src/__tests__/Backtest.test.ts` | 修改 | 危机Alpha 渲染/空态/重算 单测 |

---

### Task 1: 分析器基础层（四档分类 / 四象限 / 条件检验 / TOP10 / 周明细）

**Files:**
- Create: `main/core/engine/crisis_alpha.py`
- Test: `main/core/tests/unit/test_crisis_alpha.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - 常量 `CRISIS_THRESHOLD, EXTREME_TOP_N, DEEP_DD_THRESHOLD, ROLLING_CORR_WINDOW, DEEP_WEEK_TESTS, MONTH_BIG_MOVE, WEEKS_PER_YEAR, MIN_COMMON_DAYS, FULL_YEAR_WEEKS, DEFAULT_BENCHMARK_CODE`
  - `CRISIS_CATS: list[str]`（4 项）、`CRISIS_COLORS: dict[str, str]`（4 项）、`CRISIS_BENCHMARKS: tuple[dict, ...]`（6 项，键 `key/name/code`）
  - `class CrisisAlphaAnalyzer`：`__init__(strategy_nav: pd.Series, benchmark_price: pd.Series, strategy_name: str = "策略", benchmark_name: str = "基准")`；属性 `s_week_ret, b_week_ret, week_labels, s_full_nav, b_full_nav, s_orig_nav, b_orig_nav, s_month_ret, b_month_ret`；方法 `_nav(), drawdown_series(which='b'), quadrant_analysis(crisis_threshold=None), condition_tests(), extreme_drops_top_n(n=None), weekly_detail_df(), down_week_df()`
  - 模块级 `_classify_crisis(b_values, s_values) -> list[str]`、`_rating(score) -> str`、`alpha_rating(ratio) -> str`、`_mean(values) -> float`、`_clip(value) -> float`
  - 测试侧 helper（供 Task 2/3 复用，放在测试文件里）：`_series_from_weekly(weekly_returns) -> pd.Series`、`_analyzer(b_weeks, s_weeks, **kw) -> CrisisAlphaAnalyzer`、常量 `WEEKS_B / WEEKS_S / WEEKS_B_DISTINCT / WEEKS_S_DISTINCT`

- [ ] **Step 1: 写失败测试（新建测试文件）**

`main/core/tests/unit/test_crisis_alpha.py`：

```python
"""危机Alpha 分析器单测（1:1 移植源 E:\\宏观模型回测引擎\\宏观模型回测引擎增强版.py L2815-3277）。

周频收益通过「每 5 个交易日一周、周内线性插值」构造，使 W-FRI 重采样后的
pct_change 精确等于给定周收益；序列首周收益会被 pct_change 丢弃，故调用方
统一传 [0.0] + rets 作为种子周。
"""
import math

import pandas as pd
import pytest

from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, CRISIS_CATS, CRISIS_COLORS, CrisisAlphaAnalyzer,
    _classify_crisis, _rating, alpha_rating,
)

WEEKS_B = [-0.10, -0.10, -0.10, -0.10, 0.05, 0.05, 0.05, 0.05, -0.04, 0.04, -0.08, 0.02]
WEEKS_S = [0.02, -0.02, -0.06, -0.12, 0.03, 0.06, -0.01, 0.04, -0.03, 0.02, -0.09, 0.01]
WEEKS_B_DISTINCT = [-0.10, -0.09, -0.08, -0.07, 0.05, 0.06, 0.05, 0.04, -0.04, 0.03, -0.11, 0.02]
WEEKS_S_DISTINCT = [0.01, -0.01, -0.02, -0.03, 0.04, 0.05, 0.03, 0.02, -0.01, 0.01, -0.05, 0.01]


def _series_from_weekly(weekly_returns, start="2024-01-01", days_per_week=5):
    dates = pd.bdate_range(start=start, periods=len(weekly_returns) * days_per_week)
    values = []
    level = 100.0
    for r in weekly_returns:
        base = level
        for d in range(days_per_week):
            level = base * (1 + r * (d + 1) / days_per_week)
            values.append(level)
    return pd.Series(values, index=dates, dtype=float)


def _analyzer(b_weeks, s_weeks, **kw):
    return CrisisAlphaAnalyzer(
        _series_from_weekly([0.0] + s_weeks),
        _series_from_weekly([0.0] + b_weeks),
        **kw,
    )


def test_week_labels_follow_source_boundaries():
    b = [0.0, -0.10, -0.10, -0.10, -0.10]
    s = [0.01, -0.04, -0.05, -0.06, -0.11]
    assert _classify_crisis(b, s) == ["逆势上涨", "跌得少", "跟着跌", "跟着跌", "跌更多"]


def test_classify_crisis_zero_benchmark_with_negative_strategy_is_worst():
    assert _classify_crisis([0.0], [-0.01]) == ["跌更多"]


def test_rating_and_alpha_rating_thresholds():
    assert _rating(85) == "卓越" and _rating(84.9) == "优秀"
    assert _rating(70) == "优秀" and _rating(69.9) == "一般"
    assert alpha_rating(0.80) == "卓越" and alpha_rating(0.60) == "优秀"
    assert alpha_rating(0.40) == "一般" and alpha_rating(0.39) == "较弱"


def test_cats_colors_and_benchmarks_are_complete():
    assert CRISIS_CATS == ["逆势上涨", "跌得少", "跟着跌", "跌更多"]
    assert CRISIS_COLORS == {
        "逆势上涨": "#d6403a",
        "跌得少": "#f0a429",
        "跟着跌": "#9dc88d",
        "跌更多": "#1f6e4f",
    }
    codes = [b["code"] for b in CRISIS_BENCHMARKS]
    assert codes == ["000985.CSI", "000300.SH", "000905.SH", "000852.SH", "399006.SZ", "000688.SH"]
    assert [b["name"] for b in CRISIS_BENCHMARKS] == [
        "中证全指", "沪深300", "中证500", "中证1000", "创业板指", "科创50",
    ]


def test_quadrant_counts_alpha_ratio_and_odds():
    an = _analyzer(WEEKS_B, WEEKS_S)
    q = an.quadrant_analysis()
    assert q["n_total"] == 12
    assert q["n_down"] == 6
    assert q["counts"] == {"逆势上涨": 1, "跌得少": 1, "跟着跌": 2, "跌更多": 2}
    assert q["alpha_ratio"] == pytest.approx(2 / 6)
    assert q["up_rate"] == pytest.approx(1 / 6)
    assert q["follow_ratio"] == pytest.approx(4 / 6)
    assert q["down_avg_s"] == pytest.approx(-0.05)
    assert q["down_avg_b"] == pytest.approx(-0.52 / 6)
    assert q["win_avg"] == pytest.approx(0.02)
    assert q["loss_avg"] == pytest.approx(0.064)
    assert q["odds"] == pytest.approx(0.02 / 0.064)
    assert q["avg_ret"]["逆势上涨"] == pytest.approx(0.02)
    assert q["avg_ret"]["跌得少"] == pytest.approx(-0.02)
    assert q["avg_ret"]["跟着跌"] == pytest.approx(-0.045)
    assert q["avg_ret"]["跌更多"] == pytest.approx(-0.105)


def test_quadrant_on_flat_upward_benchmark_has_no_down_weeks():
    an = _analyzer([0.01] * 12, [0.02] * 12)
    q = an.quadrant_analysis()
    assert q["n_down"] == 0
    assert q["alpha_ratio"] == 0.0
    assert q["up_rate"] == 0.0
    assert q["odds"] == 0.0
    assert q["down_avg_s"] == 0.0
    assert q["down_avg_b"] == 0.0


def test_condition_tests_two_deep_thresholds():
    an = _analyzer(WEEKS_B, WEEKS_S)
    rows = an.condition_tests()
    assert [r["threshold"] for r in rows] == [-0.03, -0.05]
    assert rows[0]["n"] == 6
    assert rows[0]["b_avg"] == pytest.approx(-0.52 / 6)
    assert rows[0]["s_avg"] == pytest.approx(-0.05)
    assert rows[0]["up_rate"] == pytest.approx(1 / 6)
    assert rows[1]["n"] == 5
    assert rows[1]["b_avg"] == pytest.approx(-0.48 / 5)
    assert rows[1]["s_avg"] == pytest.approx(-0.27 / 5)
    assert rows[1]["up_rate"] == pytest.approx(1 / 5)


def test_condition_tests_zero_sample_returns_zeros():
    an = _analyzer([0.01] * 12, [0.01] * 12)
    for row in an.condition_tests():
        assert row["n"] == 0
        assert row["b_avg"] == 0.0
        assert row["s_avg"] == 0.0
        assert row["up_rate"] == 0.0


def test_extreme_drops_top_n_picks_worst_benchmark_weeks():
    an = _analyzer(WEEKS_B_DISTINCT, WEEKS_S_DISTINCT)
    top = an.extreme_drops_top_n(n=3)
    assert [str(d.date()) for d in top.index] == ["2024-01-19", "2024-01-12", "2024-03-22"]
    assert list(top["基准"]) == pytest.approx([-0.09, -0.10, -0.11])
    assert list(top["策略"]) == pytest.approx([-0.01, 0.01, -0.05])
    assert list(top["超额"]) == pytest.approx([0.08, 0.11, 0.06])
    assert list(top["分类"]) == ["跌得少", "逆势上涨", "跌得少"]


def test_weekly_detail_and_down_weeks():
    an = _analyzer(WEEKS_B, WEEKS_S)
    detail = an.weekly_detail_df()
    assert list(detail.columns) == ["基准收益", "策略收益", "超额", "分类"]
    assert len(detail) == 12
    down = an.down_week_df()
    assert len(down) == 6
    assert (down["基准收益"] < 0).all()
    assert down.index.is_monotonic_decreasing


def test_drawdown_series_is_zero_at_peak():
    an = _analyzer(WEEKS_B, WEEKS_S)
    dd = an.drawdown_series("b")
    assert dd.iloc[0] == pytest.approx(0.0)
    assert dd.min() < 0


def test_insufficient_common_days_raises():
    s = _series_from_weekly([0.0, 0.01, 0.01])
    b = _series_from_weekly([0.0, 0.01, 0.01])
    with pytest.raises(ValueError, match="共同交易日不足"):
        CrisisAlphaAnalyzer(s, b)


def test_non_series_input_raises_type_error():
    s = _series_from_weekly([0.0] * 13)
    with pytest.raises(TypeError):
        CrisisAlphaAnalyzer(s, [1, 2, 3])


def test_nav_normalizes_each_series_to_one():
    an = _analyzer(WEEKS_B, WEEKS_S)
    s_nav, b_nav = an._nav()
    assert s_nav.iloc[0] == pytest.approx(1.0)
    assert b_nav.iloc[0] == pytest.approx(1.0)
    assert len(s_nav) == len(an.s_orig_nav)
    assert len(b_nav) == len(an.b_orig_nav)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'core.engine.crisis_alpha'`

- [ ] **Step 3: 写实现（新建分析器文件）**

`main/core/engine/crisis_alpha.py`：

```python
import math
from typing import List, Optional, Sequence

import pandas as pd

CRISIS_THRESHOLD = 0.0
EXTREME_TOP_N = 10
DEEP_DD_THRESHOLD = -0.05
ROLLING_CORR_WINDOW = 52
DEEP_WEEK_TESTS = (-0.03, -0.05)
MONTH_BIG_MOVE = 0.05
WEEKS_PER_YEAR = 52
MIN_COMMON_DAYS = 30
FULL_YEAR_WEEKS = 40

DEFAULT_BENCHMARK_CODE = "000300.SH"

CRISIS_CATS = ["逆势上涨", "跌得少", "跟着跌", "跌更多"]

CRISIS_COLORS = {
    "逆势上涨": "#d6403a",
    "跌得少": "#f0a429",
    "跟着跌": "#9dc88d",
    "跌更多": "#1f6e4f",
}

CRISIS_BENCHMARKS = (
    {"key": "csi_all", "name": "中证全指", "code": "000985.CSI"},
    {"key": "hs300", "name": "沪深300", "code": "000300.SH"},
    {"key": "csi500", "name": "中证500", "code": "000905.SH"},
    {"key": "csi1000", "name": "中证1000", "code": "000852.SH"},
    {"key": "cyb", "name": "创业板指", "code": "399006.SZ"},
    {"key": "kc50", "name": "科创50", "code": "000688.SH"},
)

_HEDGE_CATS = ("逆势上涨", "跌得少")


def _mean(values: Sequence[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _clip(value: float) -> float:
    return float(max(0.0, min(100.0, value)))


def _classify_crisis(b_values, s_values) -> List[str]:
    labels: List[str] = []
    for b, s in zip(b_values, s_values):
        if s >= 0:
            labels.append("逆势上涨")
            continue
        denom = abs(b)
        ratio = abs(s) / denom if denom > 1e-12 else math.inf
        if ratio < 0.5:
            labels.append("跌得少")
        elif ratio <= 1.0 + 1e-12:
            labels.append("跟着跌")
        else:
            labels.append("跌更多")
    return labels


def _rating(score: float) -> str:
    if score >= 85:
        return "卓越"
    if score >= 70:
        return "优秀"
    return "一般"


def alpha_rating(ratio: float) -> str:
    if ratio >= 0.80:
        return "卓越"
    if ratio >= 0.60:
        return "优秀"
    if ratio >= 0.40:
        return "一般"
    return "较弱"


class CrisisAlphaAnalyzer:
    def __init__(
        self,
        strategy_nav: pd.Series,
        benchmark_price: pd.Series,
        strategy_name: str = "策略",
        benchmark_name: str = "基准",
    ):
        if not isinstance(strategy_nav, pd.Series) or not isinstance(benchmark_price, pd.Series):
            raise TypeError("strategy_nav 与 benchmark_price 必须是 pd.Series")
        s = strategy_nav.copy().sort_index()
        b = benchmark_price.copy().sort_index()
        s.index = pd.to_datetime(s.index).normalize()
        b.index = pd.to_datetime(b.index).normalize()
        self.s_orig_nav = s.copy()
        self.b_orig_nav = b.copy()
        common = s.index.intersection(b.index)
        if len(common) < MIN_COMMON_DAYS:
            raise ValueError(
                f"策略与基准共同交易日不足{MIN_COMMON_DAYS}天（{len(common)}），无法分析"
            )
        s = s.loc[common]
        b = b.loc[common]
        self.s_daily_ret = s.pct_change().dropna()
        self.b_daily_ret = b.pct_change().dropna()
        common_r = self.s_daily_ret.index.intersection(self.b_daily_ret.index)
        self.s_daily_ret = self.s_daily_ret.loc[common_r]
        self.b_daily_ret = self.b_daily_ret.loc[common_r]
        s_w = s.resample("W-FRI").last().dropna()
        b_w = b.resample("W-FRI").last().dropna()
        common_w = s_w.index.intersection(b_w.index)
        self.s_week_ret = s_w.loc[common_w].pct_change().dropna()
        self.b_week_ret = b_w.loc[common_w].pct_change().dropna()
        common_wr = self.s_week_ret.index.intersection(self.b_week_ret.index)
        self.s_week_ret = self.s_week_ret.loc[common_wr]
        self.b_week_ret = self.b_week_ret.loc[common_wr]
        s_m = s.resample("ME").last().dropna()
        b_m = b.resample("ME").last().dropna()
        common_m = s_m.index.intersection(b_m.index)
        self.s_month_ret = s_m.loc[common_m].pct_change().dropna()
        self.b_month_ret = b_m.loc[common_m].pct_change().dropna()
        common_mr = self.s_month_ret.index.intersection(self.b_month_ret.index)
        self.s_month_ret = self.s_month_ret.loc[common_mr]
        self.b_month_ret = self.b_month_ret.loc[common_mr]
        self.strategy_name = strategy_name
        self.benchmark_name = benchmark_name
        self.s_full_nav = s
        self.b_full_nav = b
        self.week_labels = pd.Series(
            _classify_crisis(self.b_week_ret.values, self.s_week_ret.values),
            index=self.b_week_ret.index,
        )

    def _nav(self):
        return (
            self.s_orig_nav / self.s_orig_nav.iloc[0],
            self.b_orig_nav / self.b_orig_nav.iloc[0],
        )

    def drawdown_series(self, which: str = "b") -> pd.Series:
        nav = self.b_full_nav if which == "b" else self.s_full_nav
        nav = nav / nav.iloc[0]
        return nav / nav.cummax() - 1

    def quadrant_analysis(self, crisis_threshold: Optional[float] = None) -> dict:
        thr = CRISIS_THRESHOLD if crisis_threshold is None else crisis_threshold
        s = self.s_week_ret
        b = self.b_week_ret
        down = b < thr
        n_down = int(down.sum())
        n_total = int(len(b))
        labels = self.week_labels[down]
        s_c = s[down]
        b_c = b[down]
        counts = {cat: int((labels == cat).sum()) for cat in CRISIS_CATS}
        avg_ret = {}
        for cat in CRISIS_CATS:
            v = s_c[labels == cat]
            avg_ret[cat] = float(v.mean()) if len(v) > 0 else 0.0
        n_up = counts["逆势上涨"]
        n_alpha = counts["逆势上涨"] + counts["跌得少"]
        n_follow = counts["跟着跌"] + counts["跌更多"]
        up_rate = n_up / n_down if n_down else 0.0
        alpha_ratio = n_alpha / n_down if n_down else 0.0
        follow_ratio = n_follow / n_down if n_down else 0.0
        win_avg = float(s_c[s_c > 0].mean()) if n_up > 0 else 0.0
        loss_vals = s_c[s_c <= 0]
        loss_avg = float(abs(loss_vals.mean())) if len(loss_vals) > 0 else 0.0
        odds = (win_avg / loss_avg) if loss_avg > 1e-12 else 0.0
        return {
            "n_crisis": n_down,
            "n_down": n_down,
            "n_total": n_total,
            "hit_rate": up_rate,
            "up_rate": up_rate,
            "alpha_ratio": alpha_ratio,
            "follow_ratio": follow_ratio,
            "odds": odds,
            "counts": counts,
            "avg_ret": avg_ret,
            "down_avg_s": float(s_c.mean()) if n_down else 0.0,
            "down_avg_b": float(b_c.mean()) if n_down else 0.0,
            "win_avg": win_avg,
            "loss_avg": loss_avg,
        }

    def condition_tests(self) -> List[dict]:
        out = []
        for thr in DEEP_WEEK_TESTS:
            mask = self.b_week_ret < thr
            n = int(mask.sum())
            if n == 0:
                out.append(
                    {"threshold": thr, "n": 0, "b_avg": 0.0, "s_avg": 0.0, "up_rate": 0.0}
                )
                continue
            out.append(
                {
                    "threshold": thr,
                    "n": n,
                    "b_avg": float(self.b_week_ret[mask].mean()),
                    "s_avg": float(self.s_week_ret[mask].mean()),
                    "up_rate": float((self.s_week_ret[mask] > 0).mean()),
                }
            )
        return out

    def extreme_drops_top_n(self, n: Optional[int] = None) -> pd.DataFrame:
        count = n or EXTREME_TOP_N
        df = pd.DataFrame({"基准": self.b_week_ret, "策略": self.s_week_ret})
        df = df.nsmallest(count, "基准").sort_values("基准", ascending=False)
        df["超额"] = df["策略"] - df["基准"]
        df["分类"] = self.week_labels.loc[df.index]
        return df

    def weekly_detail_df(self) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "基准收益": self.b_week_ret,
                "策略收益": self.s_week_ret,
                "超额": self.s_week_ret - self.b_week_ret,
            }
        )
        df["分类"] = self.week_labels
        return df

    def down_week_df(self) -> pd.DataFrame:
        df = self.weekly_detail_df()
        return df[df["基准收益"] < 0].sort_index(ascending=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v`
Expected: PASS，14 passed

若 `test_extreme_drops_top_n_picks_worst_benchmark_weeks` 的日期不符，说明 `bdate_range` 起始周与 W-FRI 分箱未对齐——把 `_series_from_weekly` 的 `start` 改为 `"2024-01-01"`（周一）即可，不要改断言里的期望日期。

- [ ] **Step 5: 提交**

```powershell
git add main/core/engine/crisis_alpha.py main/core/tests/unit/test_crisis_alpha.py
git commit -m "feat(engine): 危机Alpha分析器基础层（四档分类/四象限/条件检验）"
```

---

### Task 2: 分析器统计层（回撤事件 / 相关性 / 代价 / 月度 / 年度）

**Files:**
- Modify: `main/core/engine/crisis_alpha.py`（在 `CrisisAlphaAnalyzer` 末尾、`down_week_df` 之后追加方法）
- Test: `main/core/tests/unit/test_crisis_alpha.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `CrisisAlphaAnalyzer`、测试 helper `_analyzer/_series_from_weekly/WEEKS_*`
- Produces: 新方法 `deep_drawdown_events(threshold=None) -> list[dict]`、`drawdown_shade_spans(threshold=None) -> list[tuple]`、`rolling_correlation(window=None) -> pd.Series`、`expanding_correlation(min_periods=8) -> pd.Series`、`cost_analysis() -> dict`、`performance_stats() -> dict`、`monthly_analysis() -> dict`、`annual_stats() -> list[dict]`

- [ ] **Step 1: 写失败测试（追加到测试文件末尾）**

```python
def test_deep_drawdown_events_peak_to_trough_single_event():
    an = _analyzer(WEEKS_B, WEEKS_S)
    events = an.deep_drawdown_events()
    assert len(events) == 1
    ev = events[0]
    assert str(ev["start"].date()) == "2024-01-05"
    assert str(ev["end"].date()) == "2024-02-02"
    assert ev["benchmark_ret"] == pytest.approx(-0.3439)
    assert ev["strategy_ret"] == pytest.approx(-0.17313088)
    assert ev["excess"] == pytest.approx(ev["strategy_ret"] - ev["benchmark_ret"])
    assert ev["hedged"] is False
    assert ev["verdict"] == "同步下跌"
    assert ev["max_dd"] == pytest.approx(-0.3439)
    assert ev["days"] == 28


def test_deep_drawdown_events_flags_hedged_event():
    b = [-0.10] * 6
    s = [0.02, 0.02, 0.02, 0.02, 0.02, 0.02]
    an = _analyzer(b, s)
    events = an.deep_drawdown_events()
    assert len(events) == 1
    assert events[0]["hedged"] is True
    assert events[0]["verdict"] == "逆势对冲"
    assert events[0]["excess"] > 0


def test_deep_drawdown_events_empty_when_shallow():
    an = _analyzer([-0.01] * 12, [0.0] * 12)
    assert an.deep_drawdown_events() == []


def test_drawdown_shade_spans_merges_contiguous_region():
    an = _analyzer(WEEKS_B, WEEKS_S)
    spans = an.drawdown_shade_spans()
    assert len(spans) == 1
    start, end = spans[0]
    assert str(pd.Timestamp(start).date()) == "2024-01-08"
    assert str(pd.Timestamp(end).date()) == "2024-03-29"


def test_drawdown_shade_spans_empty_when_no_deep_drawdown():
    an = _analyzer([-0.01] * 12, [0.0] * 12)
    assert an.drawdown_shade_spans() == []


def test_rolling_correlation_needs_full_window():
    an = _analyzer(WEEKS_B, WEEKS_S)
    assert len(an.rolling_correlation()) == 0
    assert len(an.rolling_correlation(window=4)) == 9
    assert len(an.expanding_correlation()) == 5
    assert len(an.expanding_correlation(min_periods=2)) == 11


def test_cost_analysis_up_week_price_and_tail_correlation():
    an = _analyzer(WEEKS_B, WEEKS_S)
    cost = an.cost_analysis()
    assert cost["n_up"] == 6
    assert cost["drag"] == pytest.approx(1 / 6)
    assert cost["sync_up"] == pytest.approx(5 / 6)
    b_up = [WEEKS_B[i] for i in (4, 5, 6, 7, 9, 11)]
    s_up = [WEEKS_S[i] for i in (4, 5, 6, 7, 9, 11)]
    assert cost["elasticity"] == pytest.approx(
        (sum(s_up) / 6) / (sum(b_up) / 6)
    )
    b_down = [WEEKS_B[i] for i in (0, 1, 2, 3, 8, 10)]
    s_down = [WEEKS_S[i] for i in (0, 1, 2, 3, 8, 10)]
    assert cost["tail_corr"] == pytest.approx(
        float(pd.Series(s_down).corr(pd.Series(b_down)))
    )
    assert -1.0 <= cost["q1_corr"] <= 1.0


def test_cost_analysis_zero_when_no_up_weeks():
    an = _analyzer([-0.01] * 12, [0.0] * 12)
    cost = an.cost_analysis()
    assert cost["n_up"] == 0
    assert cost["drag"] == 0.0
    assert cost["sync_up"] == 0.0
    assert cost["elasticity"] == 0.0


def test_performance_stats_cumulative_annual_vol_mdd_and_corr():
    an = _analyzer(WEEKS_B, WEEKS_S)
    perf = an.performance_stats()
    assert set(perf) == {"s", "b", "corr", "start", "end"}
    for side in ("s", "b"):
        assert set(perf[side]) == {"cum", "ann", "vol", "mdd"}
        assert perf[side]["mdd"] <= 0
        assert perf[side]["vol"] >= 0
    assert perf["s"]["cum"] == pytest.approx(
        float(an.s_orig_nav.iloc[-1] / an.s_orig_nav.iloc[0] - 1)
    )
    assert perf["b"]["cum"] == pytest.approx(
        float(an.b_orig_nav.iloc[-1] / an.b_orig_nav.iloc[0] - 1)
    )
    assert -1.0 <= perf["corr"] <= 1.0


def test_monthly_analysis_state_buckets_and_hedge_ratio():
    an = _analyzer(WEEKS_B, WEEKS_S)
    mo = an.monthly_analysis()
    assert set(mo["states"]) == {"大涨", "小涨", "小跌", "大跌"}
    assert mo["states"]["大涨"]["n"] == 1
    assert mo["states"]["大跌"]["n"] == 1
    assert mo["states"]["小涨"]["n"] == 0
    assert mo["states"]["小跌"]["n"] == 0
    assert mo["states"]["大涨"]["win_rate"] == pytest.approx(1.0)
    assert mo["states"]["大跌"]["win_rate"] == pytest.approx(0.0)
    assert mo["n_down_month"] == 1
    assert mo["hedge_ratio"] == pytest.approx(0.0)
    assert len(mo["big_down"]) == 1


def test_monthly_analysis_flat_upward_benchmark_lands_in_small_up():
    an = _analyzer([0.001] * 12, [0.001] * 12)
    mo = an.monthly_analysis()
    assert mo["states"]["小涨"]["n"] == 2
    assert mo["states"]["大涨"]["n"] == 0
    assert mo["states"]["小跌"]["n"] == 0
    assert mo["states"]["大跌"]["n"] == 0
    assert mo["n_down_month"] == 0
    assert mo["hedge_ratio"] == 0.0
    assert len(mo["big_down"]) == 0


def test_annual_stats_single_partial_year():
    an = _analyzer(WEEKS_B, WEEKS_S)
    rows = an.annual_stats()
    assert len(rows) == 1
    row = rows[0]
    assert row["年份"] == 2024
    assert row["下跌周"] == 6
    assert row["危机Alpha"] == pytest.approx(2 / 6)
    assert row["完整"] is False
    assert row["策略收益"] == pytest.approx(math.prod(1 + r for r in WEEKS_S) - 1)
    assert row["基准收益"] == pytest.approx(math.prod(1 + r for r in WEEKS_B) - 1)


def test_annual_stats_marks_full_year_when_40_plus_weeks():
    weeks = 41
    b = ([-0.01] * (weeks // 2)) + ([0.01] * (weeks - weeks // 2))
    s = ([0.01] * (weeks // 2)) + ([0.01] * (weeks - weeks // 2))
    an = _analyzer(b, s)
    rows = an.annual_stats()
    assert rows[0]["完整"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v -k "deep_drawdown or shade or correlation or cost_analysis or performance_stats or monthly_analysis or annual_stats"`
Expected: FAIL —— `AttributeError: 'CrisisAlphaAnalyzer' object has no attribute 'deep_drawdown_events'`

- [ ] **Step 3: 写实现（追加方法到 `crisis_alpha.py` 的 `CrisisAlphaAnalyzer` 内，末尾）**

```python
    def deep_drawdown_events(self, threshold: Optional[float] = None) -> List[dict]:
        thr = DEEP_DD_THRESHOLD if threshold is None else threshold
        b_nav = self.b_full_nav / self.b_full_nav.iloc[0]
        dd = b_nav / b_nav.cummax() - 1
        in_dd = dd < -1e-10
        events = []
        idx = b_nav.index
        arr = in_dd.values
        i = 0
        n_arr = len(arr)
        while i < n_arr:
            if not arr[i]:
                i += 1
                continue
            j = i
            while j + 1 < n_arr and arr[j + 1]:
                j += 1
            peak_pos = i - 1
            if peak_pos < 0:
                i = j + 1
                continue
            peak_date = idx[peak_pos]
            seg = b_nav.iloc[i : j + 1]
            trough_date = seg.idxmin()
            min_dd = float(seg.loc[trough_date] / b_nav.loc[peak_date] - 1)
            if min_dd < thr:
                b_ret = float(
                    self.b_full_nav.loc[trough_date] / self.b_full_nav.loc[peak_date] - 1
                )
                s_ret = float(
                    self.s_full_nav.loc[trough_date] / self.s_full_nav.loc[peak_date] - 1
                )
                hedged = s_ret > 0
                events.append(
                    {
                        "start": peak_date,
                        "end": trough_date,
                        "benchmark_ret": b_ret,
                        "strategy_ret": s_ret,
                        "excess": s_ret - b_ret,
                        "hedged": hedged,
                        "verdict": "逆势对冲" if hedged else "同步下跌",
                        "max_dd": min_dd,
                        "days": int((trough_date - peak_date).days),
                    }
                )
            i = j + 1
        return events

    def drawdown_shade_spans(self, threshold: Optional[float] = None) -> List[tuple]:
        thr = DEEP_DD_THRESHOLD if threshold is None else threshold
        dd = self.drawdown_series("b")
        mask = dd < thr
        spans = []
        if not mask.any():
            return spans
        idx = mask.index
        arr = mask.values
        i = 0
        n_arr = len(arr)
        while i < n_arr:
            if not arr[i]:
                i += 1
                continue
            j = i
            while j + 1 < n_arr and arr[j + 1]:
                j += 1
            spans.append((idx[i], idx[j]))
            i = j + 1
        return spans

    def rolling_correlation(self, window: Optional[int] = None) -> pd.Series:
        w = window or ROLLING_CORR_WINDOW
        return self.s_week_ret.rolling(w).corr(self.b_week_ret).dropna()

    def expanding_correlation(self, min_periods: int = 8) -> pd.Series:
        return self.s_week_ret.expanding(min_periods=min_periods).corr(
            self.b_week_ret
        ).dropna()

    def cost_analysis(self) -> dict:
        s = self.s_week_ret
        b = self.b_week_ret
        up = b > 0
        n_up = int(up.sum())
        if n_up:
            drag = float((s[up] < 0).mean())
            sync_up = float((s[up] > 0).mean())
            b_up_mean = float(b[up].mean())
            elasticity = float(s[up].mean() / b_up_mean) if abs(b_up_mean) > 1e-12 else 0.0
        else:
            drag = 0.0
            sync_up = 0.0
            elasticity = 0.0
        down = b < 0
        if int(down.sum()) >= 5:
            tail_corr = float(s[down].corr(b[down]))
        else:
            tail_corr = 0.0
        q1_cut = b.quantile(0.25)
        q1_mask = b <= q1_cut
        if int(q1_mask.sum()) >= 5:
            q1_corr = float(s[q1_mask].corr(b[q1_mask]))
        else:
            q1_corr = 0.0
        return {
            "drag": drag,
            "sync_up": sync_up,
            "elasticity": elasticity,
            "tail_corr": tail_corr,
            "q1_corr": q1_corr,
            "n_up": n_up,
        }

    def performance_stats(self) -> dict:
        s_nav, b_nav = self._nav()
        days = max((s_nav.index[-1] - s_nav.index[0]).days, 1)
        years = days / 365.25

        def _stats(nav: pd.Series, week_ret: pd.Series) -> dict:
            cum = float(nav.iloc[-1] / nav.iloc[0] - 1)
            ann = (
                float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1)
                if years > 0
                else 0.0
            )
            vol = float(week_ret.std() * math.sqrt(WEEKS_PER_YEAR))
            dd = nav / nav.cummax() - 1
            mdd = float(dd.min())
            return {"cum": cum, "ann": ann, "vol": vol, "mdd": mdd}

        return {
            "s": _stats(s_nav, self.s_week_ret),
            "b": _stats(b_nav, self.b_week_ret),
            "corr": float(self.s_week_ret.corr(self.b_week_ret)),
            "start": s_nav.index[0],
            "end": s_nav.index[-1],
        }

    def monthly_analysis(self) -> dict:
        s_m = self.s_month_ret
        b_m = self.b_month_ret
        big = MONTH_BIG_MOVE
        states = pd.Series(index=b_m.index, dtype="object")
        states[b_m <= -big] = "大跌"
        states[(b_m > -big) & (b_m < 0)] = "小跌"
        states[(b_m >= 0) & (b_m < big)] = "小涨"
        states[b_m >= big] = "大涨"
        out = {}
        for state in ("大涨", "小涨", "小跌", "大跌"):
            mask = states == state
            n = int(mask.sum())
            if n == 0:
                out[state] = {"n": 0, "s_avg": 0.0, "b_avg": 0.0, "win_rate": 0.0}
            else:
                out[state] = {
                    "n": n,
                    "s_avg": float(s_m[mask].mean()),
                    "b_avg": float(b_m[mask].mean()),
                    "win_rate": float((s_m[mask] > 0).mean()),
                }
        m_labels = _classify_crisis(b_m.values, s_m.values)
        down_mask = (b_m.values < 0).tolist()
        hedged = [lbl in _HEDGE_CATS for lbl, is_down in zip(m_labels, down_mask) if is_down]
        hedge_ratio = _mean([1.0 if h else 0.0 for h in hedged]) if hedged else 0.0
        big_mask = (b_m <= -big).tolist()
        big_down = pd.DataFrame(
            {
                "日期": [i for i, flag in zip(s_m.index, big_mask) if flag],
                "基准": [float(v) for v, flag in zip(b_m.values, big_mask) if flag],
                "策略": [float(v) for v, flag in zip(s_m.values, big_mask) if flag],
            }
        )
        if len(big_down):
            big_down = big_down.sort_values("日期", ascending=False)
        return {
            "states": out,
            "hedge_ratio": hedge_ratio,
            "n_down_month": int(sum(down_mask)),
            "big_down": big_down,
        }

    def annual_stats(self) -> List[dict]:
        rows = []
        s_g = self.s_week_ret.groupby(self.s_week_ret.index.year)
        b_g = self.b_week_ret.groupby(self.b_week_ret.index.year)
        lab_g = self.week_labels.groupby(self.week_labels.index.year)
        years = sorted(set(s_g.groups.keys()) & set(b_g.groups.keys()))
        for yr in years:
            s_y = s_g.get_group(yr)
            b_y = b_g.get_group(yr)
            lab_y = lab_g.get_group(yr)
            down = (b_y < 0).tolist()
            n_down = int(sum(down))
            if n_down:
                flags = [
                    1.0 if lbl in _HEDGE_CATS else 0.0
                    for lbl, is_down in zip(lab_y.tolist(), down)
                    if is_down
                ]
                alpha = _mean(flags)
            else:
                alpha = 1.0
            rows.append(
                {
                    "年份": int(yr),
                    "下跌周": n_down,
                    "危机Alpha": alpha,
                    "策略收益": float((1 + s_y).prod() - 1),
                    "基准收益": float((1 + b_y).prod() - 1),
                    "完整": len(b_y) >= FULL_YEAR_WEEKS,
                }
            )
        return rows
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v`
Expected: PASS，27 passed

- [ ] **Step 5: 提交**

```powershell
git add main/core/engine/crisis_alpha.py main/core/tests/unit/test_crisis_alpha.py
git commit -m "feat(engine): 危机Alpha统计层（回撤事件/相关性/代价/月度/年度）"
```

---

### Task 3: 综合评分 + payload 组装 + JSON 净化

**Files:**
- Modify: `main/core/engine/crisis_alpha.py`
- Test: `main/core/tests/unit/test_crisis_alpha.py`

**Interfaces:**
- Consumes: Task 1/2 的 `CrisisAlphaAnalyzer`
- Produces:
  - `CrisisAlphaAnalyzer.composite_score(multi_bench_alpha_ratios=None) -> dict`（`{total_score, rating, dimensions[{维度,类型,权重,得分,评级,贡献}]}`）
  - `CrisisAlphaAnalyzer.summary(multi_bench_alpha_ratios=None) -> dict`（12 键）
  - `_frame_records(df: pd.DataFrame) -> list[dict]`（索引落为 `日期` 键）
  - `sanitize_for_json(value) -> Any`
  - `build_crisis_alpha_payload(strategy_nav, main_code, main_name, main_closes, benchmark_closes, strategy_name) -> dict`（已 sanitize，可直接 `json.dumps(..., allow_nan=False)`）

- [ ] **Step 1: 写失败测试**

先扩展测试文件顶部的 import 块（原 import 块替换为）：

```python
import json
import math
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.engine.crisis_alpha import (
    CRISIS_BENCHMARKS, CRISIS_CATS, CRISIS_COLORS, CrisisAlphaAnalyzer,
    _classify_crisis, _frame_records, _rating, alpha_rating,
    build_crisis_alpha_payload, sanitize_for_json,
)
```

再追加到测试文件末尾：

```python
def test_composite_score_shape_weights_and_contributions():
    an = _analyzer(WEEKS_B, WEEKS_S)
    sc = an.composite_score()
    assert len(sc["dimensions"]) == 9
    assert [d["维度"] for d in sc["dimensions"]] == [
        "危机Alpha占比", "深跌周抗跌性", "下尾负相关", "回撤独立性",
        "多宽基一致性", "月度危机Alpha", "极端事件保护", "上涨周代价", "整体低相关",
    ]
    assert [d["类型"] for d in sc["dimensions"]] == [
        "核心", "核心", "对冲", "对冲", "稳健", "稳健", "核心", "代价", "对冲",
    ]
    assert [d["权重"] for d in sc["dimensions"]] == [
        0.20, 0.15, 0.12, 0.12, 0.10, 0.10, 0.10, 0.06, 0.05,
    ]
    assert sum(d["权重"] for d in sc["dimensions"]) == pytest.approx(1.0)
    for d in sc["dimensions"]:
        assert 0.0 <= d["得分"] <= 100.0
        assert d["贡献"] == pytest.approx(round(d["得分"] * d["权重"], 1), abs=0.05)
        assert d["评级"] in {"卓越", "优秀", "一般"}
    assert sc["total_score"] == pytest.approx(
        sum(d["得分"] * d["权重"] for d in sc["dimensions"]), abs=0.05
    )
    assert sc["rating"] in {"卓越", "优秀", "一般"}


def test_composite_score_neutral_fallbacks_without_deep_weeks_or_events():
    an = _analyzer([-0.01] * 12, [0.0] * 12)
    dims = {d["维度"]: d for d in an.composite_score()["dimensions"]}
    assert dims["深跌周抗跌性"]["得分"] == pytest.approx(50.0)
    assert dims["回撤独立性"]["得分"] == pytest.approx(50.0)


def test_composite_score_multi_benchmark_ratios_drive_fifth_dimension():
    an = _analyzer(WEEKS_B, WEEKS_S)
    base = {d["维度"]: d["得分"] for d in an.composite_score()["dimensions"]}
    boosted = {
        d["维度"]: d["得分"]
        for d in an.composite_score(multi_bench_alpha_ratios=[1.0] * 6)["dimensions"]
    }
    assert base["多宽基一致性"] == pytest.approx(15.0)
    assert boosted["多宽基一致性"] == pytest.approx(100.0)
    assert base["深跌周抗跌性"] == pytest.approx(boosted["深跌周抗跌性"])


def test_summary_exposes_all_module_inputs():
    an = _analyzer(WEEKS_B, WEEKS_S)
    s = an.summary()
    assert set(s) == {
        "n_weeks", "quadrant", "extreme_top", "deep_dd_events", "rolling_corr",
        "monthly", "annual", "cost", "performance", "conditions", "score",
        "weekly_detail",
    }
    assert s["n_weeks"] == 12


def test_frame_records_puts_index_into_date_key():
    df = pd.DataFrame(
        {"基准": [-0.1, -0.2]},
        index=pd.to_datetime(["2024-01-12", "2024-01-19"]),
    )
    assert _frame_records(df) == [
        {"日期": pd.Timestamp("2024-01-12"), "基准": -0.1},
        {"日期": pd.Timestamp("2024-01-19"), "基准": -0.2},
    ]


def test_build_payload_aligns_nav_and_flags_main_benchmark():
    s_nav = _series_from_weekly([0.0] + WEEKS_S)
    b_nav = _series_from_weekly([0.0] + WEEKS_B)
    payload = build_crisis_alpha_payload(
        s_nav,
        "000300.SH",
        "沪深300",
        b_nav,
        {"000300.SH": b_nav, "000905.SH": b_nav * 1.01},
        "组合A",
    )
    assert payload["strategy_name"] == "组合A"
    assert payload["benchmark_code"] == "000300.SH"
    assert payload["benchmark_name"] == "沪深300"
    assert payload["n_weeks"] == 12
    assert payload["alpha_rating"] == "较弱"
    nav = payload["nav"]
    assert len(nav["dates"]) == 65
    assert len(nav["strategy"]) == 65
    assert len(nav["benchmark"]) == 65
    assert nav["strategy"][0] == pytest.approx(1.0)
    assert nav["benchmark"][0] == pytest.approx(1.0)
    assert nav["dates"][0] == "2024-01-01"
    assert nav["dates"][-1] == "2024-03-29"
    assert len(nav["shade_spans"]) == 1
    assert len(payload["multi_bench"]) == 2
    assert payload["multi_bench"][0]["is_main"] is True
    assert payload["multi_bench"][0]["name"] == "沪深300"
    assert payload["multi_bench"][1]["is_main"] is False
    assert payload["multi_bench"][1]["name"] == "中证500"
    assert len(payload["down_weeks"]) == 6
    assert payload["down_weeks"][0]["日期"] > payload["down_weeks"][-1]["日期"]
    assert len(payload["weekly_detail"]) == 12
    assert set(payload["weekly_detail"][0]) == {"日期", "基准收益", "策略收益", "超额", "分类"}
    assert payload["score"]["total_score"] > 0
    json.dumps(payload, allow_nan=False)


def test_build_payload_ignores_unusable_extra_benchmarks():
    s_nav = _series_from_weekly([0.0] + WEEKS_S)
    b_nav = _series_from_weekly([0.0] + WEEKS_B)
    short = b_nav.iloc[:10]
    payload = build_crisis_alpha_payload(
        s_nav, "000300.SH", "沪深300", b_nav, {"000905.SH": short}, "组合A"
    )
    assert [r["name"] for r in payload["multi_bench"]] == ["沪深300"]


def test_sanitize_converts_non_json_types():
    np_float = pd.DataFrame({"a": [1.5]})["a"].values[0]
    np_int = pd.DataFrame({"a": [3]})["a"].values[0]
    out = sanitize_for_json(
        {
            "nan": float("nan"),
            "inf": float("inf"),
            "ninf": float("-inf"),
            "np_f": np_float,
            "np_i": np_int,
            "ts": pd.Timestamp("2024-03-22"),
            "nat": pd.NaT,
            "dec": Decimal("2.50"),
            "d": date(2024, 1, 2),
            "series": pd.Series(
                [1.0, 2.0], index=pd.to_datetime(["2024-01-05", "2024-01-12"])
            ),
            "frame": pd.DataFrame(
                {"基准": [-0.1, -0.2]},
                index=pd.to_datetime(["2024-01-05", "2024-01-12"]),
            ),
            "nested": [{"x": float("nan")}, [float("inf")]],
            "flag": True,
        }
    )
    assert out["nan"] is None
    assert out["inf"] is None
    assert out["ninf"] is None
    assert out["np_f"] == 1.5 and isinstance(out["np_f"], float)
    assert out["np_i"] == 3 and isinstance(out["np_i"], int)
    assert out["ts"] == "2024-03-22"
    assert out["nat"] is None
    assert out["dec"] == pytest.approx(2.5)
    assert out["d"] == "2024-01-02"
    assert out["flag"] is True
    assert out["series"] == [
        {"date": "2024-01-05", "value": 1.0},
        {"date": "2024-01-12", "value": 2.0},
    ]
    assert out["frame"] == [
        {"日期": "2024-01-05", "基准": pytest.approx(-0.1)},
        {"日期": "2024-01-12", "基准": pytest.approx(-0.2)},
    ]
    assert out["nested"] == [{"x": None}, [None]]
    json.dumps(out, allow_nan=False)


def test_sanitize_passes_through_plain_values():
    assert sanitize_for_json({"a": 1, "b": "x", "c": [1, 2]}) == {
        "a": 1, "b": "x", "c": [1, 2],
    }
    assert sanitize_for_json(None) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v -k "composite or summary or frame_records or payload or sanitize"`
Expected: FAIL —— `ImportError: cannot import name 'build_crisis_alpha_payload'`

- [ ] **Step 3: 写实现**

先改 `crisis_alpha.py` 顶部 import：

```python
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
```

再在 `CrisisAlphaAnalyzer` 末尾（`annual_stats` 之后）追加：

```python
    def composite_score(self, multi_bench_alpha_ratios: Optional[List[float]] = None) -> dict:
        q = self.quadrant_analysis()
        cost = self.cost_analysis()
        events = self.deep_drawdown_events()
        top = self.extreme_drops_top_n()
        mo = self.monthly_analysis()
        tests = self.condition_tests()
        corr = float(self.s_week_ret.corr(self.b_week_ret))
        r = q["alpha_ratio"]
        s_alpha = _clip(50 + (r - 0.5) * 118)
        deep_items = [t for t in tests if t["n"] > 0]
        if deep_items:
            up_part = _mean([t["up_rate"] for t in deep_items])
            alpha_part_vals = []
            for t in deep_items:
                mask = self.b_week_ret < t["threshold"]
                lab = self.week_labels[mask]
                alpha_part_vals.append(
                    _mean([1.0 if lbl in _HEDGE_CATS else 0.0 for lbl in lab.tolist()])
                )
            alpha_part = _mean(alpha_part_vals)
            s_deep = _clip(30 + up_part * 45 + alpha_part * 30)
        else:
            s_deep = 50.0
        s_tail = _clip(50 - cost["tail_corr"] * 175)
        if events:
            s_dd = _clip(
                100 * _mean([1.0 if e["hedged"] else 0.0 for e in events])
            )
        else:
            s_dd = 50.0
        if multi_bench_alpha_ratios:
            m = _mean(multi_bench_alpha_ratios)
            s_multi = _clip(40 + (m - 0.5) * 150)
        else:
            s_multi = _clip(40 + (r - 0.5) * 150)
        s_month = _clip(100 * mo["hedge_ratio"])
        if len(top):
            beat = float((top["策略"] > top["基准"]).mean())
            upr = float((top["策略"] > 0).mean())
            s_extreme = _clip(60 * beat + 40 * upr)
        else:
            s_extreme = 50.0
        sc_sync = _clip((cost["sync_up"] - 0.3) / 0.6 * 100)
        sc_elas = _clip(cost["elasticity"] / 0.3 * 100)
        s_cost = _clip(0.4 * sc_sync + 0.6 * sc_elas)
        s_low = _clip(50 - corr * 50)
        dims_raw = [
            ("危机Alpha占比", "核心", 0.20, s_alpha),
            ("深跌周抗跌性", "核心", 0.15, s_deep),
            ("下尾负相关", "对冲", 0.12, s_tail),
            ("回撤独立性", "对冲", 0.12, s_dd),
            ("多宽基一致性", "稳健", 0.10, s_multi),
            ("月度危机Alpha", "稳健", 0.10, s_month),
            ("极端事件保护", "核心", 0.10, s_extreme),
            ("上涨周代价", "代价", 0.06, s_cost),
            ("整体低相关", "对冲", 0.05, s_low),
        ]
        dimensions = []
        for name, typ, weight, score in dims_raw:
            score_r = round(score, 1)
            dimensions.append(
                {
                    "维度": name,
                    "类型": typ,
                    "权重": weight,
                    "得分": score_r,
                    "评级": _rating(score_r),
                    "贡献": round(score_r * weight, 1),
                }
            )
        total = round(sum(d["得分"] * d["权重"] for d in dimensions), 1)
        return {"total_score": total, "rating": _rating(total), "dimensions": dimensions}

    def summary(self, multi_bench_alpha_ratios: Optional[List[float]] = None) -> dict:
        return {
            "strategy": self.strategy_name,
            "benchmark": self.benchmark_name,
            "n_weeks": int(len(self.b_week_ret)),
            "quadrant": self.quadrant_analysis(),
            "extreme_top": self.extreme_drops_top_n(),
            "deep_dd_events": self.deep_drawdown_events(),
            "rolling_corr": self.rolling_correlation(),
            "monthly": self.monthly_analysis(),
            "annual": self.annual_stats(),
            "cost": self.cost_analysis(),
            "performance": self.performance_stats(),
            "conditions": self.condition_tests(),
            "score": self.composite_score(multi_bench_alpha_ratios),
            "weekly_detail": self.weekly_detail_df(),
        }
```

在文件末尾追加模块级函数：

```python
def _frame_records(df: pd.DataFrame) -> List[dict]:
    out = []
    for idx, row in zip(df.index, df.to_dict(orient="records")):
        rec = {"日期": idx}
        rec.update(row)
        out.append(rec)
    return out


def sanitize_for_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Series):
        return [
            {"date": sanitize_for_json(idx), "value": sanitize_for_json(v)}
            for idx, v in value.items()
        ]
    if isinstance(value, pd.DataFrame):
        return _frame_records(value)
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize_for_json(item())
        except (TypeError, ValueError):
            pass
    return value


def build_crisis_alpha_payload(
    strategy_nav: pd.Series,
    main_code: str,
    main_name: str,
    main_closes: pd.Series,
    benchmark_closes: Dict[str, pd.Series],
    strategy_name: str,
) -> dict:
    an = CrisisAlphaAnalyzer(
        strategy_nav, main_closes,
        strategy_name=strategy_name, benchmark_name=main_name,
    )
    q_main = an.quadrant_analysis()
    multi_rows = [
        {
            "key": "main",
            "name": main_name,
            "code": main_code,
            "alpha_ratio": q_main["alpha_ratio"],
            "n_down": q_main["n_down"],
            "up_rate": q_main["up_rate"],
            "is_main": True,
        }
    ]
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == main_code:
            continue
        series = benchmark_closes.get(spec["code"])
        if series is None or len(series) == 0:
            continue
        try:
            an_b = CrisisAlphaAnalyzer(
                strategy_nav, series,
                strategy_name=strategy_name, benchmark_name=spec["name"],
            )
        except (TypeError, ValueError):
            continue
        q = an_b.quadrant_analysis()
        multi_rows.append(
            {
                "key": spec["key"],
                "name": spec["name"],
                "code": spec["code"],
                "alpha_ratio": q["alpha_ratio"],
                "n_down": q["n_down"],
                "up_rate": q["up_rate"],
                "is_main": False,
            }
        )
    summary = an.summary([row["alpha_ratio"] for row in multi_rows])
    s_nav, b_nav = an._nav()
    s_map = {idx: float(v) for idx, v in s_nav.items()}
    b_map = {idx: float(v) for idx, v in b_nav.items()}
    dates = sorted(set(s_map) | set(b_map))
    payload = {
        "strategy_name": strategy_name,
        "benchmark_code": main_code,
        "benchmark_name": main_name,
        "alpha_rating": alpha_rating(summary["quadrant"]["alpha_ratio"]),
        "n_weeks": summary["n_weeks"],
        "quadrant": summary["quadrant"],
        "conditions": summary["conditions"],
        "extreme_top": _frame_records(summary["extreme_top"]),
        "deep_dd_events": summary["deep_dd_events"],
        "rolling_corr": summary["rolling_corr"],
        "expanding_corr": an.expanding_correlation(),
        "monthly": {
            "states": summary["monthly"]["states"],
            "hedge_ratio": summary["monthly"]["hedge_ratio"],
            "n_down_month": summary["monthly"]["n_down_month"],
            "big_down": _frame_records(summary["monthly"]["big_down"]),
        },
        "annual": summary["annual"],
        "cost": summary["cost"],
        "performance": summary["performance"],
        "score": summary["score"],
        "weekly_detail": _frame_records(summary["weekly_detail"]),
        "down_weeks": _frame_records(an.down_week_df()),
        "nav": {
            "dates": dates,
            "strategy": [s_map.get(d) for d in dates],
            "benchmark": [b_map.get(d) for d in dates],
            "shade_spans": [[s, e] for s, e in an.drawdown_shade_spans()],
        },
        "multi_bench": multi_rows,
    }
    return sanitize_for_json(payload)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd main && uv run pytest core/tests/unit/test_crisis_alpha.py -v`
Expected: PASS，35 passed

- [ ] **Step 5: 提交**

```powershell
git add main/core/engine/crisis_alpha.py main/core/tests/unit/test_crisis_alpha.py
git commit -m "feat(engine): 危机Alpha九维评分与payload组装（含JSON净化）"
```

---

### Task 4: 存储层（ORM 模型 + Alembic 迁移 + 显式声明 pandas）

**Files:**
- Create: `main/core/models/backtest_crisis_alpha.py`
- Create: `main/alembic/versions/f2a3b4c5d6e7_add_backtest_crisis_alphas.py`
- Modify: `main/core/models/__init__.py`、`main/pyproject.toml`

**Interfaces:**
- Consumes: 无
- Produces: `core.models.BacktestCrisisAlpha`（表 `backtest_crisis_alphas`），列：`id, backtest_record_id(unique FK cascade), benchmark_code, benchmark_name, status, error, skipped_benchmarks_json, payload_json, computed_at`

- [ ] **Step 1: 写模型文件**

`main/core/models/backtest_crisis_alpha.py`：

```python
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestCrisisAlpha(Base):
    __tablename__ = "backtest_crisis_alphas"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(
        Integer,
        ForeignKey("backtest_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    benchmark_code = Column(String(20), nullable=False)
    benchmark_name = Column(String(20), nullable=False)
    status = Column(String(12), nullable=False)
    error = Column(Text, nullable=True)
    skipped_benchmarks_json = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("backtest_record_id", name="uq_backtest_crisis_alphas_record"),
    )
```

- [ ] **Step 2: 导出模型**

`main/core/models/__init__.py`：在 `from .backtest_evaluation import BacktestEvaluation` 下一行插入 `from .backtest_crisis_alpha import BacktestCrisisAlpha`，并在 `__all__` 的 `"BacktestEvaluation",` 下一行插入 `"BacktestCrisisAlpha",`。

- [ ] **Step 3: 写迁移**

`main/alembic/versions/f2a3b4c5d6e7_add_backtest_crisis_alphas.py`：

```python
"""add backtest_crisis_alphas table

Revision ID: f2a3b4c5d6e7
Revises: e5f6a7b8c9d0
Create Date: 2026-09-25 10:00:00.000000

危机Alpha 专题分析结果：回测结束后以独立事务落库，1:1 关联回测记录，
随父记录级联删除。payload_json 存 12 模块完整结果（已净化为 JSON 安全值）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backtest_crisis_alphas',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('backtest_record_id', sa.Integer(), nullable=False),
        sa.Column('benchmark_code', sa.String(length=20), nullable=False),
        sa.Column('benchmark_name', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('skipped_benchmarks_json', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['backtest_record_id'], ['backtest_records.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('backtest_record_id', name='uq_backtest_crisis_alphas_record'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backtest_crisis_alphas')
```

- [ ] **Step 4: 显式声明 pandas**

`main/pyproject.toml`：在 dependencies 列表中 `"openpyxl>=3.1.5",` 下一行插入 `"pandas>=3.0.5",`（保持字母序）。

- [ ] **Step 5: 验证（不触碰 dev.db）**

```powershell
cd main
uv run alembic heads
uv run alembic upgrade head --sql
```

Expected: `heads` 输出单个 `(head, ..., 'f2a3b4c5d6e7')`；`--sql` 打印 `CREATE TABLE backtest_crisis_alphas (...)` 且退出码 0。

若 `uv` 因新增依赖要求重新解析 lock，允许它执行（pandas 3.0.5 已在 lock 中，不会下载新包）。**禁止**用不带 `--sql` 的 `alembic upgrade head` 去改 `main/data/dev.db`。

- [ ] **Step 6: 跑既有测试确认未破坏**

Run: `cd main && uv run pytest core/tests/ -q`
Expected: 全部通过（数量 ≥ 现有 473）

- [ ] **Step 7: 提交**

```powershell
git add main/core/models/backtest_crisis_alpha.py main/core/models/__init__.py main/alembic/versions/f2a3b4c5d6e7_add_backtest_crisis_alphas.py main/pyproject.toml main/uv.lock
git commit -m "feat(db): 新增 backtest_crisis_alphas 表与显式 pandas 依赖"
```

---

### Task 5: 服务层（指数批量取数 / 计算落库 / 重算 / 回测接线 / 详情透出）

**Files:**
- Modify: `main/core/services/backtest_service.py`（imports、`:304-339` `build_benchmark_data`、新增函数、`run_backtest` `:1109` 后、`get_record_detail` 返回）
- Test: `main/core/tests/unit/test_backtest_service.py`

**Interfaces:**
- Consumes: `core.engine.crisis_alpha.{CRISIS_BENCHMARKS, DEFAULT_BENCHMARK_CODE, build_crisis_alpha_payload}`、`core.models.BacktestCrisisAlpha`
- Produces:
  - `build_index_close_series(codes: list[str], start: date, end: date) -> dict[str, dict[date, Decimal]]`
  - `build_benchmark_data(ps, start, end, db=None) -> dict[date, Decimal]`（改为委托上面，行为不变 + 异常吞掉返回 `{}`）
  - `_crisis_benchmark_code(ps) -> str`、`_crisis_benchmark_name(code) -> str`、`_crisis_nav_from_snapshots(snapshots) -> pd.Series`
  - `_upsert_crisis_alpha(db, record_id, benchmark_code, benchmark_name, status, error=None, skipped=None, payload=None) -> BacktestCrisisAlpha`
  - `compute_and_store_crisis_alpha(db, record_id, ps, snapshots, start, end) -> BacktestCrisisAlpha`
  - `safe_compute_crisis_alpha(db, record_id, ps, snapshots, start, end) -> BacktestCrisisAlpha | None`（永不抛）
  - `recompute_crisis_alpha(db, record_id) -> dict | None`（`{'status','error','computed_at'}`）
  - `get_record_detail(...)` 返回值新增 `crisis_alpha` 键

- [ ] **Step 1: 写失败测试（追加到 `test_backtest_service.py` 末尾）**

先在该文件的 import 区补三处：`import json`（加在 `from datetime import ...` 之前）、`import pandas as pd`（加在 `import polars as pl` 之前）、在 `from core.models import (...)` 列表的 `"BacktestDecisionEvent,"` 下一行加 `BacktestCrisisAlpha,`。

```python
_CA_WEEKS_B = [-0.10, -0.10, -0.10, -0.10, 0.05, 0.05, 0.05, 0.05, -0.04, 0.04, -0.08, 0.02]
_CA_WEEKS_S = [0.02, -0.02, -0.06, -0.12, 0.03, 0.06, -0.01, 0.04, -0.03, 0.02, -0.09, 0.01]


def _ca_daily_points(weekly_returns, start="2024-01-01"):
    dates = pd.bdate_range(start=start, periods=len(weekly_returns) * 5)
    points = {}
    idx = 0
    level = 100.0
    for r in weekly_returns:
        base = level
        for _ in range(5):
            level = base * (1 + r * (_ + 1) / 5)
            points[dates[idx].date()] = level
            idx += 1
    return points


def _seed_record(db):
    ps, _ = _seed(db)
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id, name="ca_test",
        start_date=date(2024, 1, 1), end_date=date(2024, 3, 31),
        status="completed", progress=100,
    )
    db.add(rec)
    db.commit()
    return rec, ps


class _FakeIndexTQ:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def get_history_raw(self, stocks, periods, start="", end="",
                        dividend_type="front", count=100):
        self.calls.append({
            "stocks": list(stocks), "periods": list(periods), "start": start,
            "end": end, "dividend_type": dividend_type, "count": count,
        })
        rows = {}
        for code in stocks:
            series = self.frames.get(code)
            if not series:
                continue
            for d, v in series.items():
                rows.setdefault(d, {})[code] = v
        keys = sorted(rows)
        frame = pd.DataFrame([rows[d] for d in keys], index=keys)
        return {"1d": {"Close": frame}}


def _ca_snapshots(points):
    return [{"snap_date": d, "total_value": v} for d, v in points.items()]


def _ca_decimal_frames(points):
    return {d: Decimal(str(v)) for d, v in points.items()}


def test_build_index_close_series_batches_codes_and_skips_missing(monkeypatch):
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    fake = _FakeIndexTQ({
        "000300.SH": {days[0]: 3000.0, days[1]: 3010.0},
        "000905.SH": {days[0]: 5000.0, days[1]: 4990.0},
    })
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.build_index_close_series(
        ["000300.SH", "000905.SH", "000985.CSI"], date(2026, 1, 1), date(2026, 1, 31)
    )
    assert set(out) == {"000300.SH", "000905.SH"}
    assert float(out["000300.SH"][days[0]]) == pytest.approx(3000.0)
    assert float(out["000905.SH"][days[1]]) == pytest.approx(4990.0)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["stocks"] == ["000300.SH", "000905.SH", "000985.CSI"]
    assert call["periods"] == ["1d"]
    assert call["start"] == "20260101"
    assert call["end"] == "20260131"
    assert call["dividend_type"] == "front"
    assert call["count"] == -1


def test_build_index_close_series_swallows_tq_error(monkeypatch):
    class _Boom:
        def get_history_raw(self, **kw):
            raise RuntimeError("tdx down")

    monkeypatch.setattr(svc, "TQData", lambda: _Boom())
    assert svc.build_index_close_series(
        ["000300.SH"], date(2026, 1, 1), date(2026, 1, 31)
    ) == {}


def test_build_index_close_series_empty_codes_skips_tq(monkeypatch):
    def _boom(**kw):
        raise AssertionError("不应调用 TQ")

    monkeypatch.setattr(svc, "TQData", lambda: SimpleNamespace(get_history_raw=_boom))
    assert svc.build_index_close_series([], date(2026, 1, 1), date(2026, 1, 31)) == {}
    assert svc.build_index_close_series([""], date(2026, 1, 1), date(2026, 1, 31)) == {}


def test_build_benchmark_data_returns_empty_on_tq_error(monkeypatch):
    class _Boom:
        def get_history_raw(self, **kw):
            raise RuntimeError("tdx down")

    monkeypatch.setattr(svc, "TQData", lambda: _Boom())
    ps = SimpleNamespace(benchmark_index="000300.SH")
    assert svc.build_benchmark_data(ps, date(2026, 1, 1), date(2026, 1, 31)) == {}


def test_build_benchmark_data_uses_default_code_when_unset(monkeypatch):
    fake = _FakeIndexTQ({"000300.SH": {date(2026, 1, 5): 3000.0}})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.build_benchmark_data(
        SimpleNamespace(benchmark_index=None), date(2026, 1, 1), date(2026, 1, 31)
    )
    assert fake.calls[0]["stocks"] == ["000300.SH"]
    assert list(out) == [date(2026, 1, 5)]


def test_compute_and_store_crisis_alpha_completed(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({
        "000300.SH": _ca_decimal_frames(bm_pts),
        "000905.SH": _ca_decimal_frames(bm_pts),
    })
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert row.status == "completed"
    assert row.benchmark_code == "000300.SH"
    assert row.benchmark_name == "沪深300"
    assert row.error is None
    data = json.loads(row.payload_json)
    assert data["n_weeks"] == 12
    assert data["alpha_rating"] == "较弱"
    assert data["strategy_name"] == ps.name
    assert [r["name"] for r in data["multi_bench"]] == ["沪深300", "中证500"]
    assert [r["is_main"] for r in data["multi_bench"]] == [True, False]
    assert len(data["nav"]["dates"]) == 65
    assert len(data["weekly_detail"]) == 12
    skipped = json.loads(row.skipped_benchmarks_json)
    assert "中证500" not in skipped
    assert set(skipped) == {"中证全指", "中证1000", "创业板指", "科创50"}
    assert row.computed_at is not None


def test_compute_and_store_crisis_alpha_insufficient_when_common_days_short(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    pts = _ca_daily_points([0.0, 0.01, 0.01])
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(pts),
        date(2024, 1, 1), date(2024, 1, 31),
    )
    assert row.status == "insufficient"
    assert "共同交易日不足" in row.error
    assert row.payload_json is None


def test_compute_and_store_crisis_alpha_failed_when_main_benchmark_missing(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000905.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    row = svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert row.status == "failed"
    assert "主基准" in row.error
    assert "沪深300" in json.loads(row.skipped_benchmarks_json)
    assert row.payload_json is None


def test_compute_and_store_crisis_alpha_upserts_single_row(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    args = (
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    svc.compute_and_store_crisis_alpha(*args)
    svc.compute_and_store_crisis_alpha(*args)
    rows = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).all()
    assert len(rows) == 1


def test_safe_compute_crisis_alpha_swallows_and_marks_failed(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "compute_and_store_crisis_alpha", _boom)
    row = svc.safe_compute_crisis_alpha(
        db_session, rec.id, ps, [], date(2024, 1, 1), date(2024, 3, 31)
    )
    assert row is not None
    assert row.status == "failed"
    assert "boom" in row.error


def test_run_backtest_still_completed_when_crisis_alpha_fails(db_session, monkeypatch):
    ps, strat_id = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0")},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100995"),
         "cash": Decimal("99995"), "market_value": Decimal("1000")},
    ]
    fake_result = {
        "trades": [], "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.00995")},
        "strategy_snapshots": {}, "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)

    result = svc.run_backtest(db_session, ps, _req())
    rec = db_session.get(BacktestRecord, result["record_id"])
    assert rec.status == "completed"
    assert rec.progress == 100
    assert db_session.query(BacktestCrisisAlpha).count() == 0


def test_run_backtest_calls_crisis_alpha_after_main_commit(db_session, monkeypatch):
    ps, _ = _seed(db_session)
    monkeypatch.setattr(svc, "build_klines", lambda ps, start, end, db=None: _mock_klines())
    monkeypatch.setattr(svc, "build_signal_cache", lambda ps, klines, db=None, start=None: {})
    monkeypatch.setattr(svc, "build_open_prices", lambda ps, klines: {})
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    snapshots = [
        {"snap_date": date(2026, 7, 29), "total_value": Decimal("100000"),
         "cash": Decimal("100000"), "market_value": Decimal("0")},
        {"snap_date": date(2026, 7, 30), "total_value": Decimal("100995"),
         "cash": Decimal("99995"), "market_value": Decimal("1000")},
    ]
    fake_result = {
        "trades": [], "snapshots": snapshots,
        "evaluations": {"total_return": Decimal("0.00995")},
        "strategy_snapshots": {}, "strategy_evaluations": {},
    }
    monkeypatch.setattr(svc.BacktestEngine, "run", lambda self, portfolio, **kw: fake_result)
    seen = {}

    def _capture(db, record_id, ps_arg, snaps, start, end):
        seen["record_id"] = record_id
        seen["status_at_call"] = db.get(BacktestRecord, record_id).status
        seen["snapshot_count"] = len(snaps)
        seen["start"] = start
        seen["end"] = end

    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", _capture)
    result = svc.run_backtest(db_session, ps, _req())
    assert seen["record_id"] == result["record_id"]
    assert seen["status_at_call"] == "completed"
    assert seen["snapshot_count"] == 2
    assert seen["start"] == _req().start_date
    assert seen["end"] == _req().end_date


def test_recompute_crisis_alpha_rebuilds_from_db_snapshots(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    for d, v in nav_pts.items():
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=Decimal(str(round(v, 2))),
            cash=Decimal("0"), market_value=Decimal("0"),
        ))
    db_session.commit()
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "completed"
    assert out["error"] is None
    assert out["computed_at"] is not None
    row = db_session.query(BacktestCrisisAlpha).filter_by(
        backtest_record_id=rec.id).one()
    data = json.loads(row.payload_json)
    assert data["n_weeks"] == 12
    assert len(data["nav"]["dates"]) == 65


def test_recompute_crisis_alpha_returns_none_for_missing_record(db_session):
    assert svc.recompute_crisis_alpha(db_session, 9999) is None


def test_recompute_crisis_alpha_without_snapshots_returns_failed(db_session):
    rec, _ = _seed_record(db_session)
    out = svc.recompute_crisis_alpha(db_session, rec.id)
    assert out["status"] == "failed"
    assert "无组合日快照" in out["error"]


def test_get_record_detail_includes_crisis_alpha(db_session, monkeypatch):
    rec, ps = _seed_record(db_session)
    nav_pts = _ca_daily_points([0.0] + _CA_WEEKS_S)
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    fake = _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)})
    monkeypatch.setattr(svc, "TQData", lambda: fake)
    svc.compute_and_store_crisis_alpha(
        db_session, rec.id, ps, _ca_snapshots(nav_pts),
        date(2024, 1, 1), date(2024, 3, 31),
    )
    detail = svc.get_record_detail(db_session, rec.id)
    ca = detail["crisis_alpha"]
    assert ca["status"] == "completed"
    assert ca["benchmark_code"] == "000300.SH"
    assert ca["data"]["n_weeks"] == 12
    assert "中证全指" in ca["skipped_benchmarks"]
    assert ca["computed_at"] is not None


def test_get_record_detail_crisis_alpha_is_null_without_row(db_session):
    rec, _ = _seed_record(db_session)
    detail = svc.get_record_detail(db_session, rec.id)
    assert detail["crisis_alpha"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && uv run pytest core/tests/unit/test_backtest_service.py -v -k "index_close or crisis"`
Expected: FAIL —— `AttributeError: module 'core.services.backtest_service' has no attribute 'build_index_close_series'`

- [ ] **Step 3: 改 imports**

`main/core/services/backtest_service.py`：
- `from datetime import ...` 上一行加 `import json`
- `from core.models import (...)` 列表的 `"BacktestDecisionEvent,"` 下一行加 `BacktestCrisisAlpha,`
- `from core.engine.backtest_engine import BacktestEngine` 下一行加
  `from core.engine.crisis_alpha import (`
  `    CRISIS_BENCHMARKS, DEFAULT_BENCHMARK_CODE, build_crisis_alpha_payload,`
  `)`

- [ ] **Step 4: 用批量取数替换 `build_benchmark_data`**

把 `backtest_service.py:304-339` 的整个 `build_benchmark_data` 函数替换为：

```python
def build_index_close_series(
    codes: List[str], start: date, end: date
) -> Dict[str, Dict[date, Decimal]]:
    """批量拉多个指数的日线收盘：{code: {date: close}}。

    一次 get_history_raw 取全部 code（主基准 + 6 宽基），按 code 拆分。
    TQ 异常/无数据返回空 dict；单个 code 缺列则该 code 不出现在结果里（视作跳过）。
    """
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return {}
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    try:
        raw = TQData().get_history_raw(
            stocks=wanted,
            periods=["1d"],
            start=start_str,
            end=end_str,
            dividend_type="front",
            count=-1,
        )
    except Exception:
        return {}
    if not isinstance(raw, dict) or not raw:
        return {}
    period = raw.get("1d")
    if not isinstance(period, dict):
        return {}
    close_df = period.get("Close")
    if close_df is None:
        return {}
    columns = getattr(close_df, "columns", [])
    out: Dict[str, Dict[date, Decimal]] = {}
    for code in wanted:
        if code not in columns:
            continue
        series: Dict[date, Decimal] = {}
        for ts in close_df.index:
            val = close_df.loc[ts, code]
            if _is_nan(val):
                continue
            d = ts.date() if hasattr(ts, "date") else ts
            if isinstance(d, datetime):
                d = d.date()
            series[d] = _to_decimal(val)
        if series:
            out[code] = series
    return out


def build_benchmark_data(
    ps, start: date, end: date, db: Session = None
) -> Dict[date, Decimal]:
    """拉基准指数收盘价：{snap_date: close_value}。

    基准代码取 ps.benchmark_index（默认 000300.SH），委托 build_index_close_series
    批量取数（单 code）。取数失败/无数据返回空 dict（前端据此隐藏基准线）。
    """
    index_code = getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE
    return build_index_close_series([index_code], start, end).get(index_code, {})
```

- [ ] **Step 5: 新增危机Alpha 服务函数**

紧接在 `build_benchmark_data` 之后追加：

```python
def _crisis_benchmark_code(ps) -> str:
    return getattr(ps, "benchmark_index", None) or DEFAULT_BENCHMARK_CODE


def _crisis_benchmark_name(code: str) -> str:
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == code:
            return spec["name"]
    return code


def _crisis_nav_from_snapshots(snapshots: List[dict]) -> pd.Series:
    points = {}
    for snap in snapshots:
        d = snap.get("snap_date")
        v = snap.get("total_value")
        if d is None or v is None:
            continue
        if isinstance(d, datetime):
            d = d.date()
        points[d] = float(v)
    if not points:
        return pd.Series(dtype=float)
    return pd.Series(points, dtype=float).sort_index()


def _to_close_series(points: Dict[date, Decimal]) -> pd.Series:
    return pd.Series(
        {d: float(v) for d, v in points.items()}, dtype=float
    ).sort_index()


def _upsert_crisis_alpha(
    db: Session,
    record_id: int,
    benchmark_code: str,
    benchmark_name: str,
    status: str,
    error: Optional[str] = None,
    skipped: Optional[List[str]] = None,
    payload: Optional[dict] = None,
) -> BacktestCrisisAlpha:
    row = (
        db.query(BacktestCrisisAlpha)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    if row is None:
        row = BacktestCrisisAlpha(backtest_record_id=record_id)
        db.add(row)
    row.benchmark_code = benchmark_code
    row.benchmark_name = benchmark_name
    row.status = status
    row.error = error
    row.skipped_benchmarks_json = json.dumps(skipped or [], ensure_ascii=False)
    row.payload_json = (
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if payload is not None
        else None
    )
    row.computed_at = datetime.now()
    db.commit()
    return row


def compute_and_store_crisis_alpha(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    start: date,
    end: date,
) -> BacktestCrisisAlpha:
    """算 12 模块结果并落库（独立事务）。主基准缺数→failed，共同交易日不足→insufficient。"""
    benchmark_code = _crisis_benchmark_code(ps)
    benchmark_name = _crisis_benchmark_name(benchmark_code)
    codes = [benchmark_code] + [spec["code"] for spec in CRISIS_BENCHMARKS]
    closes = build_index_close_series(codes, start, end)
    skipped = [
        spec["name"] for spec in CRISIS_BENCHMARKS if spec["code"] not in closes
    ]
    main_closes = closes.get(benchmark_code) or {}
    if not main_closes:
        return _upsert_crisis_alpha(
            db, record_id, benchmark_code, benchmark_name,
            status="failed",
            error="主基准指数无数据（TQ 取数失败或代码不被支持）",
            skipped=skipped,
        )
    benchmark_closes = {}
    for spec in CRISIS_BENCHMARKS:
        got = closes.get(spec["code"])
        if got:
            benchmark_closes[spec["code"]] = _to_close_series(got)
    try:
        payload = build_crisis_alpha_payload(
            _crisis_nav_from_snapshots(snapshots),
            benchmark_code,
            benchmark_name,
            _to_close_series(main_closes),
            benchmark_closes,
            getattr(ps, "name", "组合"),
        )
    except ValueError as exc:
        return _upsert_crisis_alpha(
            db, record_id, benchmark_code, benchmark_name,
            status="insufficient", error=str(exc), skipped=skipped,
        )
    return _upsert_crisis_alpha(
        db, record_id, benchmark_code, benchmark_name,
        status="completed", skipped=skipped, payload=payload,
    )


def safe_compute_crisis_alpha(
    db: Session,
    record_id: int,
    ps: PortfolioStrategy,
    snapshots: List[dict],
    start: date,
    end: date,
) -> Optional[BacktestCrisisAlpha]:
    """compute_and_store 的兜底包装：任何异常都只落 failed 行，绝不向回测主链路抛出。"""
    try:
        return compute_and_store_crisis_alpha(db, record_id, ps, snapshots, start, end)
    except Exception as exc:
        db.rollback()
        code = _crisis_benchmark_code(ps)
        try:
            return _upsert_crisis_alpha(
                db, record_id, code, _crisis_benchmark_name(code),
                status="failed", error=str(exc),
            )
        except Exception:
            db.rollback()
            return None


def recompute_crisis_alpha(db: Session, record_id: int) -> Optional[dict]:
    """从库内组合日快照重算并覆盖写（旧回测补算入口）。记录不存在返回 None。"""
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return None
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date)
        .all()
    )
    if not snaps:
        return {"status": "failed", "error": "该回测无组合日快照，无法计算"}
    ps = db.get(PortfolioStrategy, rec.portfolio_strategy_id)
    if ps is None:
        return {"status": "failed", "error": "组合策略已删除，无法确定基准指数"}
    row = safe_compute_crisis_alpha(
        db, record_id, ps,
        [{"snap_date": s.snap_date, "total_value": s.total_value} for s in snaps],
        rec.start_date, rec.end_date,
    )
    if row is None:
        return {"status": "failed", "error": "危机Alpha 结果落库失败"}
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }
```

- [ ] **Step 6: 接入 run_backtest（主 commit 之后）**

在 `run_backtest` 的 `db.commit()`（`:1109`）之后、`return` 之前插入：

```python
    safe_compute_crisis_alpha(
        db, rec.id, ps, result["snapshots"], req.start_date, req.end_date
    )
```

- [ ] **Step 7: 详情接口透出 crisis_alpha**

在 `get_record_detail` 内、`return {...}` 之前插入：

```python
    ca = (
        db.query(BacktestCrisisAlpha)
        .filter_by(backtest_record_id=record_id)
        .one_or_none()
    )
    crisis_alpha = None
    if ca is not None:
        crisis_alpha = {
            "status": ca.status,
            "error": ca.error,
            "skipped_benchmarks": (
                json.loads(ca.skipped_benchmarks_json)
                if ca.skipped_benchmarks_json else []
            ),
            "computed_at": ca.computed_at.isoformat() if ca.computed_at else None,
            "benchmark_code": ca.benchmark_code,
            "benchmark_name": ca.benchmark_name,
            "data": json.loads(ca.payload_json) if ca.payload_json else None,
        }
```

并在返回字典中加一行 `"crisis_alpha": crisis_alpha,`。

- [ ] **Step 8: 给既有 run_backtest 测试补桩（避免触碰真实 TQ）**

`build_benchmark_data` 被既有测试整体替换后，危机Alpha 路径仍会直连 `build_index_close_series`。在 `test_backtest_service.py` 中，把每一行
`monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})`
替换为同样内容**再加一行**
`monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)`
（用 replaceAll，两行紧邻）。

- [ ] **Step 9: 跑测试确认通过**

Run: `cd main && uv run pytest core/tests/unit/test_backtest_service.py -v`
Expected: 全部 PASS（含新增 16 个）

- [ ] **Step 10: 提交**

```powershell
git add main/core/services/backtest_service.py main/core/tests/unit/test_backtest_service.py
git commit -m "feat(backtest): 危机Alpha计算落库/重算/回测接线/详情透出"
```

---

### Task 6: 重算 API 路由 + 集成测试

**Files:**
- Modify: `main/core/api/backtest.py`（re-export 列表 + 新路由）
- Test: `main/core/tests/integration/test_backtest_api.py`

**Interfaces:**
- Consumes: Task 5 的 `recompute_crisis_alpha`
- Produces: `POST /api/backtest/records/{record_id}/crisis-alpha/recompute` → `ok({status, error, computed_at})`；记录不存在 → `err(404, "回测记录不存在")`

- [ ] **Step 1: 写失败测试（追加到 `test_backtest_api.py` 末尾）**

```python
def test_post_recompute_crisis_alpha_returns_status(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    out = _fake_crisis_payload()
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
    monkeypatch.setattr(
        svc, "recompute_crisis_alpha",
        lambda db, rid: {"status": "completed", "error": None, "computed_at": "2026-09-25T10:00:00"},
    )
    body = c.post(f"/api/backtest/records/{record_id}/crisis-alpha/recompute").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "completed"
    assert body["data"]["computed_at"] == "2026-09-25T10:00:00"
    assert out is not None


def test_post_recompute_crisis_alpha_not_found(client):
    c, Session = client
    body = c.post("/api/backtest/records/999999/crisis-alpha/recompute").json()
    assert body["code"] == 404


def test_get_record_detail_includes_crisis_alpha(client, monkeypatch):
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()
    _mock_data(monkeypatch)
    record_id = _post_backtest(c, ps_id).json()["data"]["record_id"]
    monkeypatch.setattr(svc, "recompute_crisis_alpha", lambda db, rid: {
        "status": "completed", "error": None, "computed_at": "2026-09-25T10:00:00",
    })
    c.post(f"/api/backtest/records/{record_id}/crisis-alpha/recompute")
    body = c.get(f"/api/backtest/records/{record_id}").json()
    assert body["code"] == 0
    assert "crisis_alpha" in body["data"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && uv run pytest core/tests/integration/test_backtest_api.py -v -k crisis`
Expected: FAIL —— 路由 404（`POST` 路径未注册）

- [ ] **Step 3: 实现路由**

`main/core/api/backtest.py`：
- 在 re-export import 列表的 `run_backtest as _svc_run_backtest,` 下一行加 `recompute_crisis_alpha as _svc_recompute_crisis_alpha,`
- 在 `delete_record` 路由之后插入：

```python
@router.post("/records/{record_id}/crisis-alpha/recompute")
def recompute_record_crisis_alpha(record_id: int, db: Session = Depends(get_db)):
    """重算危机Alpha（同步执行，pull 7 段指数日线）。落库失败以 status='failed' 返回，
    不抛 500，便于前端展示原因。记录不存在返回 code=404。"""
    result = _svc_recompute_crisis_alpha(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)
```

- [ ] **Step 4: 给集成 fixture 补桩**

`test_backtest_api.py` 的 `_mock_data(monkeypatch)` 末尾追加两行：

```python
    monkeypatch.setattr(svc, "build_benchmark_data", lambda ps, start, end, db=None: {})
    monkeypatch.setattr(svc, "safe_compute_crisis_alpha", lambda *a, **kw: None)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd main && uv run pytest core/tests/integration/test_backtest_api.py -v`
Expected: 全部 PASS（含新增 3 个）

- [ ] **Step 6: 提交**

```powershell
git add main/core/api/backtest.py main/core/tests/integration/test_backtest_api.py
git commit -m "feat(api): 新增危机Alpha重算路由并补集成测试"
```

---

### Task 7: 前端 API 类型 + 重算方法 + 整体表现指标 6→8 卡

**Files:**
- Modify: `web/src/api/index.ts`（`:235-246` 之后加类型、`:246` 之后加 `crisis_alpha` 字段、`:497` 之后加方法）
- Modify: `web/src/views/Backtest.vue`（imports、`:228-240` `overallStats`、`:160-174` 之后加格式化/scroll 函数、`:744-747` 模板、`:985-991` 与 `:1186-1195` 样式）
- Test: `web/src/__tests__/Backtest.test.ts`

**Interfaces:**
- Consumes: Task 5 的 `get_record_detail` 返回的 `crisis_alpha` 键
- Produces:
  - `web/src/api/index.ts`：`CrisisAlphaQuadrant / CrisisAlphaCondition / CrisisAlphaWeekRow / CrisisAlphaEvent / CrisisAlphaPoint / CrisisAlphaMonthState / CrisisAlphaAnnual / CrisisAlphaCost / CrisisAlphaPerfSide / CrisisAlphaPerformance / CrisisAlphaDimension / CrisisAlphaScore / CrisisAlphaMultiBench / CrisisAlphaNav / CrisisAlphaData / BacktestCrisisAlphaItem`；`BacktestDetailItem.crisis_alpha: BacktestCrisisAlphaItem | null`；`recomputeCrisisAlpha(id: number): Promise<{status: string; error: string | null; computed_at: string | null}>`
  - `Backtest.vue`：`pct1(v)`、`pctSigned(v)`、`scrollToSection(anchor: string)`

- [ ] **Step 1: 写失败测试**

先改测试文件顶部的 mock 工厂与 import（原 `:4-10` 与 `:27-30` 替换为）：

```ts
vi.mock('../api', () => ({
  getBacktestRecords: vi.fn(),
  getBacktestDetail: vi.fn(),
  runBacktest: vi.fn(),
  getPortfolios: vi.fn(),
  deleteBacktestRecord: vi.fn(),
  recomputeCrisisAlpha: vi.fn(),
}))

import * as echarts from 'echarts'
import Backtest from '../views/Backtest.vue'
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord, recomputeCrisisAlpha,
} from '../api'

// jsdom 无 scrollIntoView 实现，组件的滚动到 section 需要桩
const scrollTargets: any[] = []
```

再把原 `beforeEach`（`:64-69`）替换为：

```ts
beforeEach(() => {
  vi.clearAllMocks()
  scrollTargets.length = 0
  ;(Element.prototype as any).scrollIntoView = vi.fn(function (this: any) {
    scrollTargets.push(this)
  })
  ;(getBacktestRecords as any).mockResolvedValue(mockRecords)
  ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
  ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
})

// 挂载并点第 1 条[查看]进入详情视图
async function mountDetail(detailPayload: any) {
  ;(getBacktestDetail as any).mockResolvedValue(detailPayload)
  const w = mount(Backtest)
  await flushPromises()
  const viewBtn = w.findAll('button').filter((b) => b.text().includes('查看'))[0]
  await viewBtn.trigger('click')
  await flushPromises()
  return w
}
```

在测试文件末尾追加 fixture 与用例：

```ts
const crisisAlphaCompleted = {
  status: 'completed',
  error: null,
  skipped_benchmarks: ['中证全指', '科创50'],
  computed_at: '2026-09-25T10:00:00',
  benchmark_code: '000300.SH',
  benchmark_name: '沪深300',
  data: {
    strategy_name: '稳健组合',
    benchmark_code: '000300.SH',
    benchmark_name: '沪深300',
    alpha_rating: '优秀',
    n_weeks: 12,
    quadrant: {
      n_crisis: 6, n_down: 6, n_total: 12,
      hit_rate: 0.5, up_rate: 0.5, alpha_ratio: 0.6667, follow_ratio: 0.3333, odds: 1.2,
      counts: { 逆势上涨: 3, 跌得少: 1, 跟着跌: 1, 跌更多: 1 },
      avg_ret: { 逆势上涨: 0.02, 跌得少: -0.03, 跟着跌: -0.08, 跌更多: -0.15 },
      down_avg_s: -0.0283, down_avg_b: -0.07, win_avg: 0.02, loss_avg: 0.08,
    },
    conditions: [
      { threshold: -0.03, n: 4, b_avg: -0.06, s_avg: -0.01, up_rate: 0.5 },
      { threshold: -0.05, n: 2, b_avg: -0.09, s_avg: 0.0, up_rate: 0.5 },
    ],
    extreme_top: [
      { 日期: '2024-02-02', 基准: -0.12, 策略: 0.01, 超额: 0.13, 分类: '逆势上涨' },
      { 日期: '2024-02-09', 基准: -0.1, 策略: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-26', 基准: -0.1, 策略: -0.08, 超额: 0.02, 分类: '跌得少' },
    ],
    deep_dd_events: [
      { start: '2024-01-12', end: '2024-02-02', benchmark_ret: -0.12, strategy_ret: 0.01, excess: 0.13, hedged: true, verdict: '逆势对冲', max_dd: -0.14, days: 21 },
      { start: '2024-02-16', end: '2024-02-23', benchmark_ret: -0.08, strategy_ret: -0.09, excess: -0.01, hedged: false, verdict: '同步下跌', max_dd: -0.09, days: 7 },
    ],
    rolling_corr: [
      { date: '2024-01-05', value: 0.4 },
      { date: '2024-01-12', value: 0.3 },
      { date: '2024-01-19', value: 0.2 },
    ],
    expanding_corr: [
      { date: '2023-12-29', value: 0.6 },
      { date: '2024-01-05', value: 0.4 },
    ],
    monthly: {
      states: {
        大涨: { n: 1, s_avg: 0.03, b_avg: 0.06, win_rate: 1 },
        小涨: { n: 2, s_avg: 0.01, b_avg: 0.02, win_rate: 0.5 },
        小跌: { n: 3, s_avg: -0.01, b_avg: -0.02, win_rate: 0.3333 },
        大跌: { n: 1, s_avg: 0.005, b_avg: -0.07, win_rate: 1 },
      },
      hedge_ratio: 0.5,
      n_down_month: 4,
      big_down: [{ 日期: '2024-02-29', 基准: -0.07, 策略: 0.005 }],
    },
    annual: [
      { 年份: 2024, 下跌周: 6, 危机Alpha: 0.6667, 策略收益: 0.05, 基准收益: -0.08, 完整: false },
    ],
    cost: { drag: 0.25, sync_up: 0.75, elasticity: 0.4, tail_corr: -0.2, q1_corr: -0.15, n_up: 4 },
    performance: {
      s: { cum: 0.05, ann: 0.06, vol: 0.12, mdd: -0.05 },
      b: { cum: -0.08, ann: -0.09, vol: 0.2, mdd: -0.15 },
      corr: 0.25,
      start: '2024-01-01',
      end: '2024-03-29',
    },
    score: {
      total_score: 72.3,
      rating: '优秀',
      dimensions: [
        { 维度: '危机Alpha占比', 类型: '核心', 权重: 0.2, 得分: 78.7, 评级: '优秀', 贡献: 15.7 },
        { 维度: '深跌周抗跌性', 类型: '核心', 权重: 0.15, 得分: 65.0, 评级: '一般', 贡献: 9.8 },
        { 维度: '下尾负相关', 类型: '对冲', 权重: 0.12, 得分: 85.0, 评级: '卓越', 贡献: 10.2 },
      ],
    },
    weekly_detail: [
      { 日期: '2024-01-05', 基准收益: -0.1, 策略收益: 0.02, 超额: 0.12, 分类: '逆势上涨' },
      { 日期: '2024-01-12', 基准收益: -0.1, 策略收益: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-19', 基准收益: -0.1, 策略收益: -0.06, 超额: 0.04, 分类: '跌得少' },
      { 日期: '2024-01-26', 基准收益: -0.1, 策略收益: -0.12, 超额: -0.02, 分类: '跌更多' },
      { 日期: '2024-02-02', 基准收益: 0.05, 策略收益: 0.03, 超额: -0.02, 分类: '跟着跌' },
      { 日期: '2024-02-09', 基准收益: 0.05, 策略收益: -0.01, 超额: -0.06, 分类: '跟着跌' },
    ],
    down_weeks: [
      { 日期: '2024-01-26', 基准收益: -0.1, 策略收益: -0.12, 超额: -0.02, 分类: '跌更多' },
      { 日期: '2024-01-19', 基准收益: -0.1, 策略收益: -0.06, 超额: 0.04, 分类: '跌得少' },
      { 日期: '2024-01-12', 基准收益: -0.1, 策略收益: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-05', 基准收益: -0.1, 策略收益: 0.02, 超额: 0.12, 分类: '逆势上涨' },
    ],
    nav: {
      dates: ['2024-01-01', '2024-02-01', '2024-03-29'],
      strategy: [1, 1.03, 1.05],
      benchmark: [1, 0.95, 0.92],
      shade_spans: [['2024-01-12', '2024-02-02']],
    },
    multi_bench: [
      { key: 'main', name: '沪深300', code: '000300.SH', alpha_ratio: 0.6667, n_down: 6, up_rate: 0.5, is_main: true },
      { key: 'csi500', name: '中证500', code: '000905.SH', alpha_ratio: 0.6, n_down: 6, up_rate: 0.45, is_main: false },
    ],
  },
}

describe('Backtest.vue — 危机Alpha 上提到整体表现指标（8 卡）', () => {
  it('crisis_alpha 为 null：共 8 张卡，危机两卡为 —', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards.length).toBe(8)
    const labels = cards.map((c) => c.find('.label').text())
    expect(labels[6]).toBe('危机Alpha占比')
    expect(labels[7]).toBe('综合危机保护评分')
    expect(cards[6].find('.value').text()).toBe('—')
    expect(cards[7].find('.value').text()).toBe('—')
  })

  it('status=completed：显示「占比 · 评级」与「评分 · 评级」', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards[6].find('.value').text()).toBe('66.7% · 优秀')
    expect(cards[7].find('.value').text()).toBe('72.3 · 优秀')
  })

  it('status=failed：危机两卡降级为 —', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: '主基准指数无数据', skipped_benchmarks: [],
        computed_at: '2026-09-25T10:00:00', benchmark_code: '000300.SH',
        benchmark_name: '沪深300', data: null,
      },
    })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards[6].find('.value').text()).toBe('—')
    expect(cards[7].find('.value').text()).toBe('—')
  })

  it('点击危机卡滚动到危机Alpha section', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    await w.findAll('.stats-grid .stat-card')[6].trigger('click')
    expect(scrollTargets.length).toBe(1)
    expect(String(scrollTargets[0].className)).toContain('ca-section')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts -t "危机Alpha 上提"`
Expected: FAIL —— `expect(cards.length).toBe(8)` 实到 6

- [ ] **Step 3: 写 API 类型与重算方法**

`web/src/api/index.ts`：在 `BacktestReturnsBucket` 接口之后（`:233` 空行处）插入以下类型定义（键名必须与后端 payload 完全一致，含中文键）：

```ts
// 危机Alpha 专题分析（12 模块；键名对齐后端 build_crisis_alpha_payload）
export interface CrisisAlphaQuadrant {
  n_crisis: number
  n_down: number
  n_total: number
  hit_rate: number
  up_rate: number
  alpha_ratio: number
  follow_ratio: number
  odds: number
  counts: Record<string, number>
  avg_ret: Record<string, number>
  down_avg_s: number
  down_avg_b: number
  win_avg: number
  loss_avg: number
}

export interface CrisisAlphaCondition {
  threshold: number
  n: number
  b_avg: number
  s_avg: number
  up_rate: number
}

export interface CrisisAlphaWeekRow {
  日期: string
  基准?: number
  策略?: number
  基准收益?: number
  策略收益?: number
  超额?: number
  分类: string
}

export interface CrisisAlphaEvent {
  start: string
  end: string
  benchmark_ret: number
  strategy_ret: number
  excess: number
  hedged: boolean
  verdict: string
  max_dd: number
  days: number
}

export interface CrisisAlphaPoint {
  date: string
  value: number | null
}

export interface CrisisAlphaMonthState {
  n: number
  s_avg: number
  b_avg: number
  win_rate: number
}

export interface CrisisAlphaAnnual {
  年份: number
  下跌周: number
  危机Alpha: number
  策略收益: number
  基准收益: number
  完整: boolean
}

export interface CrisisAlphaCost {
  drag: number
  sync_up: number
  elasticity: number
  tail_corr: number
  q1_corr: number
  n_up: number
}

export interface CrisisAlphaPerfSide {
  cum: number
  ann: number
  vol: number
  mdd: number
}

export interface CrisisAlphaPerformance {
  s: CrisisAlphaPerfSide
  b: CrisisAlphaPerfSide
  corr: number
  start: string
  end: string
}

export interface CrisisAlphaDimension {
  维度: string
  类型: string
  权重: number
  得分: number
  评级: string
  贡献: number
}

export interface CrisisAlphaScore {
  total_score: number
  rating: string
  dimensions: CrisisAlphaDimension[]
}

export interface CrisisAlphaMultiBench {
  key: string
  name: string
  code: string
  alpha_ratio: number
  n_down: number
  up_rate: number
  is_main: boolean
}

export interface CrisisAlphaNav {
  dates: string[]
  strategy: (number | null)[]
  benchmark: (number | null)[]
  shade_spans: string[][]
}

export interface CrisisAlphaData {
  strategy_name: string
  benchmark_code: string
  benchmark_name: string
  alpha_rating: string
  n_weeks: number
  quadrant: CrisisAlphaQuadrant
  conditions: CrisisAlphaCondition[]
  extreme_top: CrisisAlphaWeekRow[]
  deep_dd_events: CrisisAlphaEvent[]
  rolling_corr: CrisisAlphaPoint[]
  expanding_corr: CrisisAlphaPoint[]
  monthly: {
    states: Record<string, CrisisAlphaMonthState>
    hedge_ratio: number
    n_down_month: number
    big_down: Array<{ 日期: string; 基准: number; 策略: number }>
  }
  annual: CrisisAlphaAnnual[]
  cost: CrisisAlphaCost
  performance: CrisisAlphaPerformance
  score: CrisisAlphaScore
  weekly_detail: CrisisAlphaWeekRow[]
  down_weeks: CrisisAlphaWeekRow[]
  nav: CrisisAlphaNav
  multi_bench: CrisisAlphaMultiBench[]
}

export interface BacktestCrisisAlphaItem {
  status: 'completed' | 'insufficient' | 'failed'
  error: string | null
  skipped_benchmarks: string[]
  computed_at: string | null
  benchmark_code: string
  benchmark_name: string
  data: CrisisAlphaData | null
}
```

在 `BacktestDetailItem` 末尾（`returns_buckets` 那一行之后）插入：

```ts
  crisis_alpha: BacktestCrisisAlphaItem | null  // 危机Alpha专题分析（无记录行时 null）
```

在 `deleteBacktestRecord`（`:494-497`）之后插入：

```ts
// 重算危机Alpha（历史回测补算；同步执行，失败以 status='failed' 返回而非 500）
export async function recomputeCrisisAlpha(id: number) {
  const res = await api.post<ApiResponse<{
    status: string
    error: string | null
    computed_at: string | null
  }>>(`/backtest/records/${id}/crisis-alpha/recompute`)
  return res.data.data
}
```

- [ ] **Step 4: 改 Backtest.vue（8 卡 + 滚动）**

1) 顶部 import 块（`:4-11`）替换为：

```ts
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord,
} from '../api'
import type {
  BacktestRecordItem, BacktestDetailItem, PortfolioItem,
  BacktestEvaluationItem, DecisionSummaryItem, DecisionEventItem,
  CrisisAlphaData,
} from '../api'
```

2) 在 `valueClass`（`:171-174`）之后插入格式化与滚动函数：

```ts
// 危机Alpha/KPI 用 1 位小数百分比（对齐源 Streamlit 展示）
function pct1(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(1)}%`
}
// 带正负号（+1.23% / -1.23%）
function pctSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(digits)}%`
}
```

3) 把 `overallStats`（`:228-240`）整体替换为（带显式类型，`anchor` 供点击滚动）：

```ts
// 整体表现 8 渐变卡（含 2 张危机Alpha核心结论卡，点击滚动到专题 section）
const overallStats = computed<Array<{ label: string; value: string; anchor?: string }>>(() => {
  const e = detail.value?.evaluations
  if (!e) return []
  const ca = detail.value?.crisis_alpha
  const cd = ca?.status === 'completed' ? ca.data : null
  return [
    { label: '总收益', value: pct(e.total_return) },
    { label: '年化收益', value: pct(e.annual_return) },
    { label: '最大回撤', value: pct(e.max_drawdown) },
    { label: '年化波动率', value: pct(e.volatility) },
    { label: '夏普比率', value: num(e.sharpe_ratio, 2) },
    { label: '卡玛比率', value: num(e.calmar_ratio, 2) },
    {
      label: '危机Alpha占比',
      value: cd ? `${pct1(cd.quadrant.alpha_ratio)} · ${cd.alpha_rating}` : '—',
      anchor: 'crisis-alpha',
    },
    {
      label: '综合危机保护评分',
      value: cd ? `${num(cd.score.total_score, 1)} · ${cd.score.rating}` : '—',
      anchor: 'crisis-alpha',
    },
  ]
})
```

4) 在 `openDetail`（`:127-140`）之后插入滚动函数：

```ts
function scrollToSection(anchor: string) {
  if (anchor === 'crisis-alpha') {
    crisisAlphaSectionEl.value?.scrollIntoView({ behavior: 'smooth' })
  }
}
```

5) 模板 `stats-grid`（`:744-747`）替换为（加 class + click）：

```html
      <div v-for="s in overallStats" :key="s.label" class="stat-card"
           :class="{ clickable: !!s.anchor }"
           @click="s.anchor && scrollToSection(s.anchor)">
        <div class="value">{{ s.value }}</div>
        <div class="label">{{ s.label }}</div>
      </div>
```

6) 样式：`:985` 注释 `/* 整体表现 6 渐变卡 */` → `/* 整体表现 8 渐变卡 */`；`:988` 的 `repeat(6, 1fr)` → `repeat(4, 1fr)`；在 `.stat-card .label` 规则之后追加：

```css
.stat-card.clickable { cursor: pointer; }
```

响应式断点（`:1188`/`:1192`）保持 `repeat(3,1fr)` / `repeat(2,1fr)` 不变。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部 PASS（既有 126 个 + 新增 4 个）

- [ ] **Step 6: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0，无输出

- [ ] **Step 7: 提交**

```powershell
git add web/src/api/index.ts web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 整体表现指标上提危机Alpha占比与综合评分（6→8卡）"
```

---

### Task 8: 危机Alpha section 骨架（状态条 / 4 KPI / 三态 / 重算按钮）

**Files:**
- Modify: `web/src/views/Backtest.vue`（imports 加 `recomputeCrisisAlpha`、`crisisAlphaSectionEl` 声明、危机Alpha computed 块、`initCharts()` 末尾、`handleResize`、`onUnmounted`、模板 `:748` 之后、样式）
- Test: `web/src/__tests__/Backtest.test.ts`

**Interfaces:**
- Consumes: Task 7 的 `BacktestCrisisAlphaItem` / `recomputeCrisisAlpha`
- Produces（Task 9/10 依赖）：
  - `ca: ComputedRef<CrisisAlphaData | null>`（`status==='completed' && data` 才非空）
  - `caStatus: ComputedRef<'completed' | 'insufficient' | 'failed' | null>`
  - `caSkipped: ComputedRef<string[]>`、`recomputing: Ref<boolean>`、`recomputeMsg: Ref<string>`、`crisisAlphaSectionEl: Ref<HTMLDivElement | null>`
  - `caKpis: ComputedRef<Array<{label,value,sub,color}>>`
  - `initCrisisAlphaCharts(): void`（Task 9/10 填充实现）
  - `CRISIS_CATS` / `CRISIS_COLORS` / `CRISIS_SCORE_COLORS` / `MONTH_STATES` 常量
  - `recomputeCa(): Promise<void>`

- [ ] **Step 1: 写失败测试（追加到测试文件末尾）**

```ts
describe('Backtest.vue — 危机Alpha section 骨架', () => {
  it('crisis_alpha=null：显示未计算提示 + 重新计算按钮', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    expect(w.find('.ca-section').exists()).toBe(true)
    expect(w.text()).toContain('🌪️ 危机Alpha 专题分析')
    expect(w.text()).toContain('本次回测未包含危机Alpha分析')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(1)
  })

  it('status=failed：显示失败原因 + 重新计算按钮', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: '主基准指数无数据（TQ 取数失败或代码不被支持）',
        skipped_benchmarks: ['中证全指', '中证500'], computed_at: '2026-09-25T10:00:00',
        benchmark_code: '000300.SH', benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.text()).toContain('主基准指数无数据')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(1)
  })

  it('status=insufficient：显示样本不足说明且无重算按钮', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'insufficient',
        error: '策略与基准共同交易日不足30天（12），无法分析',
        skipped_benchmarks: [], computed_at: '2026-09-25T10:00:00',
        benchmark_code: '000300.SH', benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.text()).toContain('样本不足')
    expect(w.text()).toContain('30')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(0)
  })

  it('status=completed：4 张 KPI 卡 + 状态条（跳过基准 + 计算时间 + 重新计算）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const kpis = w.findAll('.ca-kpi')
    expect(kpis.length).toBe(4)
    expect(kpis[0].text()).toContain('危机Alpha占比')
    expect(kpis[0].text()).toContain('66.7%')
    expect(kpis[1].text()).toContain('综合危机保护评分')
    expect(kpis[1].text()).toContain('72.3')
    expect(kpis[2].text()).toContain('下跌周逆势上涨比例')
    expect(kpis[2].text()).toContain('3 / 6 周')
    expect(kpis[3].text()).toContain('策略累计收益')
    expect(kpis[3].text()).toContain('年化 6.0%')
    expect(kpis[3].text()).toContain('MDD -5.0%')
    const status = w.find('.ca-status').text()
    expect(status).toContain('已跳过基准：中证全指、科创50')
    expect(status).toContain('2026-09-25 10:00:00')
    expect(w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算')).length).toBe(1)
  })

  it('点击重新计算 → 调 recomputeCrisisAlpha → 成功后重载详情并渲染 KPI', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(recomputeCrisisAlpha).toHaveBeenCalledWith(1)
    expect(getBacktestDetail).toHaveBeenCalledTimes(2)
    expect(w.find('.ca-msg').text()).toContain('重新计算完成')
    expect(w.findAll('.ca-kpi').length).toBe(4)
    expect(w.text()).toContain('66.7%')
  })

  it('重算返回 failed：显示失败原因，不抛错', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'failed', error: '主基准指数无数据', computed_at: null,
    })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.find('.ca-msg').text()).toContain('主基准指数无数据')
  })

  it('重算接口抛错：显示失败提示', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockRejectedValue(new Error('boom'))
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.find('.ca-msg').text()).toContain('重新计算失败')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts -t "危机Alpha section 骨架"`
Expected: FAIL —— `Cannot read properties of null` / 找不到 `.ca-section`

- [ ] **Step 3: 写 computed 块**

1) import 块：第 7 行 `deleteBacktestRecord,` 之后加 `recomputeCrisisAlpha,`：

```ts
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord, recomputeCrisisAlpha,
} from '../api'
```

2) 在 `annualBarChart` 声明之后（`:43` 后）追加 section 锚点与常量：

```ts
// ===== 危机Alpha 专题分析 =====
const crisisAlphaSectionEl = ref<HTMLDivElement | null>(null)
const recomputing = ref(false)
const recomputeMsg = ref('')

// 四档分类与配色（照搬源 CRISIS_CATS / CRISIS_COLORS；红=对冲好，绿=跟跌差）
const CRISIS_CATS = ['逆势上涨', '跌得少', '跟着跌', '跌更多']
const CRISIS_COLORS: Record<string, string> = {
  逆势上涨: '#d6403a', 跌得少: '#f0a429', 跟着跌: '#9dc88d', 跌更多: '#1f6e4f',
}
const CRISIS_SCORE_COLORS: Record<string, string> = {
  卓越: '#d6403a', 优秀: '#f0a429', 一般: '#b0a890',
}
const MONTH_STATES = ['大涨', '小涨', '小跌', '大跌']

const crisisAlpha = computed(() => detail.value?.crisis_alpha ?? null)
const caStatus = computed(() => crisisAlpha.value?.status ?? null)
const caSkipped = computed(() => crisisAlpha.value?.skipped_benchmarks || [])
const ca = computed<CrisisAlphaData | null>(() => {
  const item = crisisAlpha.value
  if (!item || item.status !== 'completed' || !item.data) return null
  return item.data
})

const caKpis = computed(() => {
  const d = ca.value
  if (!d) return []
  const q = d.quadrant
  const s = d.performance.s
  return [
    {
      label: '危机Alpha占比',
      value: pct1(q.alpha_ratio),
      sub: `逆势上涨+跌得少 / 下跌周 · ${d.alpha_rating}`,
      color: CRISIS_COLORS.逆势上涨,
    },
    {
      label: '综合危机保护评分',
      value: num(d.score.total_score, 1),
      sub: d.score.rating,
      color: CRISIS_SCORE_COLORS[d.score.rating] || '#6b7280',
    },
    {
      label: '下跌周逆势上涨比例',
      value: pct1(q.up_rate),
      sub: `${q.counts.逆势上涨 ?? 0} / ${q.n_down} 周`,
      color: CRISIS_COLORS.逆势上涨,
    },
    {
      label: '策略累计收益',
      value: pct1(s.cum),
      sub: `年化 ${pct1(s.ann)} ｜ MDD ${pct1(s.mdd)}`,
      color: '#1f3a68',
    },
  ]
})

async function recomputeCa() {
  const id = currentRecord.value?.id
  if (!id) return
  recomputing.value = true
  recomputeMsg.value = ''
  try {
    const res = await recomputeCrisisAlpha(id)
    recomputeMsg.value = res.status === 'completed'
      ? '重新计算完成'
      : `重新计算未完成：${res.error || res.status}`
    detail.value = await getBacktestDetail(id)
    await nextTick()
    initCharts()
  } catch (e) {
    recomputeMsg.value = `重新计算失败：${errMsg(e)}`
  } finally {
    recomputing.value = false
  }
}

// Task 9/10 填充 9 个 ECharts 初始化；无数据时直接返回
function initCrisisAlphaCharts() {
  if (!ca.value) return
}
```

3) `initCharts()`（`:384-391`）末尾追加 `initCrisisAlphaCharts()`：

```ts
function initCharts() {
  initEquityChart()
  initDrawdownChart()
  initMonthlyHeatChart()
  initDailyBoxChart()
  initMonthlyBoxChart()
  initAnnualBarChart()
  initCrisisAlphaCharts()
}
```

- [ ] **Step 4: 写模板**

在 `stats-grid` 结束（`:748` `</div>`）之后、「策略对比分析」注释（`:750`）之前插入：

```html
    <!-- 🌪️ 危机Alpha 专题分析（组合净值在 A 股宽基系统性下跌阶段的对冲能力） -->
    <div ref="crisisAlphaSectionEl" class="ca-section">
      <div class="section-title">🌪️ 危机Alpha 专题分析</div>

      <!-- 未计算 -->
      <div v-if="caStatus === null" class="card" style="padding:12px">
        <p style="color:#6b7280;margin:0 0 8px">本次回测未包含危机Alpha分析。</p>
        <button class="btn btn-sm btn-primary" :disabled="recomputing" @click="recomputeCa">
          {{ recomputing ? '计算中…' : '重新计算' }}
        </button>
        <span v-if="recomputeMsg" class="ca-msg">{{ recomputeMsg }}</span>
      </div>

      <!-- 计算失败 -->
      <div v-else-if="caStatus === 'failed'" class="card" style="padding:12px">
        <p style="color:#c0392b;margin:0 0 8px">危机Alpha分析失败：{{ crisisAlpha?.error || '未知原因' }}</p>
        <button class="btn btn-sm btn-primary" :disabled="recomputing" @click="recomputeCa">
          {{ recomputing ? '计算中…' : '重新计算' }}
        </button>
        <span v-if="recomputeMsg" class="ca-msg">{{ recomputeMsg }}</span>
      </div>

      <!-- 样本不足 -->
      <div v-else-if="caStatus === 'insufficient'" class="card" style="padding:12px">
        <p style="color:#6b7280;margin:0">样本不足：{{ crisisAlpha?.error || '共同交易日不足 30 天' }}</p>
      </div>

      <!-- 完整分析（Task 9/10 填充 12 模块） -->
      <template v-else>
        <div class="ca-banner">
          <div class="t">{{ ca?.strategy_name }} · 危机Alpha专项分析</div>
          <div class="d">
            {{ ca?.strategy_name }} 在 A 股宽基系统性下跌阶段的对冲能力评估
          </div>
          <div class="d">
            数据区间 {{ ca?.performance.start }} ~ {{ ca?.performance.end }} ｜ 主基准：{{ ca?.benchmark_name }}
            ｜ 周频 {{ ca?.n_weeks }} 个收益周 ｜ 下跌 {{ ca?.quadrant.n_down }} 周
          </div>
        </div>

        <div class="ca-kpi-grid">
          <div v-for="k in caKpis" :key="k.label" class="ca-kpi">
            <div class="v" :style="{ color: k.color }">{{ k.value }}</div>
            <div class="l">{{ k.label }}</div>
            <div class="s">{{ k.sub }}</div>
          </div>
        </div>

        <div class="ca-status">
          <span>统计口径：策略与主基准共同交易日内的周频收益</span>
          <span v-if="caSkipped.length">已跳过基准：{{ caSkipped.join('、') }}</span>
          <span v-if="crisisAlpha?.computed_at">
            计算于 {{ crisisAlpha?.computed_at?.replace('T', ' ').slice(0, 19) }}
          </span>
          <button class="btn btn-sm" :disabled="recomputing" @click="recomputeCa">
            {{ recomputing ? '计算中…' : '重新计算' }}
          </button>
          <span v-if="recomputeMsg" class="ca-msg">{{ recomputeMsg }}</span>
        </div>
      </template>
    </div>
```

- [ ] **Step 5: 写样式**

在 `/* 分页 */`（`:1166`）之前插入：

```css
/* 危机Alpha 专题分析 */
.ca-section { scroll-margin-top: 16px; }
.ca-banner {
  background: #1f3a68;
  border-radius: 12px;
  padding: 18px 24px;
  color: #fff;
  margin-bottom: 6px;
}
.ca-banner .t { font-size: 20px; font-weight: 800; }
.ca-banner .d { font-size: 12px; opacity: .85; margin-top: 6px; }
.ca-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin: 14px 0 10px;
}
.ca-kpi {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 16px 10px;
  text-align: center;
}
.ca-kpi .v { font-size: 26px; font-weight: 800; }
.ca-kpi .l { font-size: 13px; color: #6b7280; margin-top: 6px; }
.ca-kpi .s { font-size: 11px; color: #9ca3af; margin-top: 4px; }
.ca-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 10px;
}
.ca-msg { font-size: 12px; color: #1f3a68; }
.ca-sub { font-size: 14px; font-weight: 700; color: #333; margin: 14px 0 8px; }
.ca-note { font-size: 12px; color: #6b7280; margin: 6px 0 10px; }
.ca-tags { margin: 10px 0; }
.ca-tag {
  display: inline-block;
  border-radius: 14px;
  padding: 5px 14px;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  margin-right: 8px;
}
.ca-chart { width: 100%; min-height: 400px; }
```

并在两个响应式断点内各加一行 `.ca-kpi-grid { grid-template-columns: repeat(2, 1fr); }`（`max-width: 1200px`）与 `.ca-kpi-grid { grid-template-columns: 1fr; }`（`max-width: 768px`）。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部 PASS（+7）

- [ ] **Step 7: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0

- [ ] **Step 8: 提交**

```powershell
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 危机Alpha专题section骨架（状态条/4KPI/三态/重算）"
```

---

### Task 9: 危机Alpha 模块一~六（核心结论 / 走势 / 四档 / TOP10 / 四象限 / 回撤事件）

**Files:**
- Modify: `web/src/views/Backtest.vue`（4 个 computed 表/序列 + 4 个 chart 初始化 + 模板 + 生命周期接线）
- Test: `web/src/__tests__/Backtest.test.ts`

**Interfaces:**
- Consumes: Task 8 的 `ca`、`initCrisisAlphaCharts`、`CRISIS_CATS`、`CRISIS_COLORS`
- Produces: `caPerfRows` / `caCatRows` / `caCondRows` / `caEventRows` / `caEventStat` / `caTopStat` / `caPie` / `caTop` / `caScatter`；refs+instances `caNavEl/caPieEl/caTopEl/caScatterEl` + `caNavChart/caPieChart/caTopChart/caScatterChart`；函数 `initCaNavChart/initCaPieChart/initCaTopChart/initCaScatterChart`

- [ ] **Step 1: 写失败测试（追加到测试文件末尾）**

```ts
describe('Backtest.vue — 危机Alpha 模块一~六', () => {
  it('渲染模块一~六标题（含 TOP 数量与下跌周数）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    for (const t of [
      '一、核心结论', '二、产品与基准走势', '三、危机Alpha四档分类',
      '四、极端下跌周 TOP3', '五、全周期四象限分布', '六、深度回撤事件独立性',
    ]) {
      expect(w.text()).toContain(t)
    }
  })

  it('核心结论段落含下跌周均值 / 相关系数 / 回撤事件对冲数 + 两个评级标签', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('在 6 个沪深300下跌周中')
    expect(text).toContain('66.7%')
    expect(text).toContain('50.0%')
    expect(text).toContain('-2.83%')
    expect(text).toContain('-7.00%')
    expect(text).toContain('0.250')
    expect(text).toContain('最深的 2 次基准回撤中有 1 次逆势录得正收益')
    expect(text).toContain('危机Alpha 66.7% · 优秀')
    expect(text).toContain('综合评分 72.3 · 优秀')
  })

  it('二：净值走势图容器 + 绩效表（5 行，含相关系数解读）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.findAll('.ca-chart').length).toBe(4)
    const text = w.text()
    for (const t of ['累计收益', '年化收益', '年化波动', '最大回撤', '相关系数']) {
      expect(text).toContain(t)
    }
    expect(text).toContain('低/负相关，对冲属性显著')
  })

  it('三：分类统计表（4 档 + 危机Alpha合计）与深跌周条件检验（2 行）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    for (const t of ['逆势上涨', '跌得少', '跟着跌', '跌更多', '危机Alpha合计']) {
      expect(text).toContain(t)
    }
    expect(text).toContain('深跌周条件检验（更严苛）')
    expect(text).toContain('沪深300周跌 >3%')
    expect(text).toContain('沪深300周跌 >5%')
    expect(w.findAll('.ca-cond-table tbody tr').length).toBe(2)
  })

  it('四：TOP3 说明含逆势正收益周数与跑赢周数', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('沪深300跌幅最大的 3 周中')
    expect(text).toContain('1 周逆势录得正收益')
    expect(text).toContain('2 周跑赢基准')
  })

  it('五：四象限图例含 4 档分类', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.text()).toContain('股跌+组合涨')
    expect(w.text()).toContain('同涨')
    expect(w.text()).toContain('同跌')
  })

  it('六：事件表 2 行 + 回撤独立性说明', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(w.findAll('.ca-event-table tbody tr').length).toBe(2)
    expect(text).toContain('2024-01-12')
    expect(text).toContain('2024-02-02')
    expect(text).toContain('✓ 逆势对冲')
    expect(text).toContain('✗ 同步下跌')
    expect(text).toContain('回撤独立性 50%')
  })

  it('无数据时（status=failed）不渲染 12 模块与图表容器', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: 'x', skipped_benchmarks: [],
        computed_at: null, benchmark_code: '000300.SH',
        benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.findAll('.ca-chart').length).toBe(0)
    expect(w.findAll('.ca-kpi').length).toBe(0)
    expect(w.text()).not.toContain('一、核心结论')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts -t "危机Alpha 模块一~六"`
Expected: FAIL —— `.ca-chart` 数量 0，标题缺失

- [ ] **Step 3: 写 computed（插在 `caKpis` 之后）**

```ts
// 二：绩效对照表（照搬源 L4850-4856）
const caPerfRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const s = d.performance.s
  const b = d.performance.b
  const corr = d.performance.corr
  return [
    { 指标: '累计收益', 策略: pct1(s.cum), 基准: pct1(b.cum) },
    { 指标: '年化收益', 策略: pct1(s.ann), 基准: pct1(b.ann) },
    { 指标: '年化波动', 策略: pct1(s.vol), 基准: pct1(b.vol) },
    { 指标: '最大回撤', 策略: pct1(s.mdd), 基准: pct1(b.mdd) },
    { 指标: '相关系数', 策略: '—', 基准: corr < 0.3 ? `${num(corr, 3)}（低/负相关，对冲属性显著）` : num(corr, 3) },
  ]
})

// 三：四档分类统计（照搬源 L4885-4893）
const caCatRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const q = d.quadrant
  const n = Math.max(q.n_down, 1)
  const rows = CRISIS_CATS.map((c) => ({
    分类: c,
    周数: q.counts[c] ?? 0,
    占比: pct1((q.counts[c] ?? 0) / n),
    平均周收益: pctSigned(q.avg_ret[c] ?? 0),
    color: CRISIS_COLORS[c],
  }))
  rows.push({
    分类: '危机Alpha合计',
    周数: (q.counts.逆势上涨 ?? 0) + (q.counts.跌得少 ?? 0),
    占比: pct1(q.alpha_ratio),
    平均周收益: '—',
    color: '#111827',
  })
  return rows
})

// 三：深跌周条件检验（照搬源 L4910-4917）
const caCondRows = computed(() => {
  const d = ca.value
  if (!d) return []
  return d.conditions.map((t) => ({
    条件: `${d.benchmark_name}周跌 >${(Math.abs(t.threshold) * 100).toFixed(0)}%`,
    周数: t.n,
    股票平均: pctSigned(t.b_avg),
    策略平均: pctSigned(t.s_avg),
    收涨比例: pct1(t.up_rate),
  }))
})

// 三：环形图数据
const caPie = computed(() => {
  const d = ca.value
  if (!d) return null
  const q = d.quadrant
  const values = CRISIS_CATS.map((c) => q.counts[c] ?? 0)
  if (values.reduce((a, b) => a + b, 0) === 0) return null
  const n = Math.max(q.n_down, 1)
  return {
    labels: CRISIS_CATS.map((c, i) => `${c}  ${(values[i] / n * 100).toFixed(1)}%`),
    shortLabels: CRISIS_CATS.map((c) => `${c} ${values[CRISIS_CATS.indexOf(c)]}周`),
    values,
    colors: CRISIS_CATS.map((c) => CRISIS_COLORS[c]),
    alpha: (q.alpha_ratio * 100).toFixed(1),
  }
})

// 四：极端下跌周 TOP N
const caTop = computed(() => {
  const rows = ca.value?.extreme_top ?? []
  if (!rows.length) return null
  return {
    labels: rows.map((r) => String(r.日期).slice(2)),
    b: rows.map((r) => +((r.基准 ?? 0) * 100).toFixed(4)),
    s: rows.map((r) => +((r.策略 ?? 0) * 100).toFixed(4)),
  }
})
const caTopStat = computed(() => {
  const rows = ca.value?.extreme_top ?? []
  return {
    n: rows.length,
    up: rows.filter((r) => (r.策略 ?? 0) > 0).length,
    beat: rows.filter((r) => (r.策略 ?? 0) > (r.基准 ?? 0)).length,
  }
})

// 五：四象限散点（按分类拆 4 组）
const caScatter = computed(() => {
  const d = ca.value
  if (!d) return null
  return CRISIS_CATS.map((cat) => ({
    name: cat,
    color: CRISIS_COLORS[cat],
    data: d.weekly_detail
      .filter((r) => r.分类 === cat)
      .map((r) => [
        +(((r.基准收益 ?? 0)) * 100).toFixed(4),
        +(((r.策略收益 ?? 0)) * 100).toFixed(4),
      ]),
  })).filter((s) => s.data.length > 0)
})

// 六：深度回撤事件
const caEventRows = computed(() => (ca.value?.deep_dd_events ?? []).map((e) => ({
  回撤起点: e.start,
  回撤低点: e.end,
  基准: pctSigned(e.benchmark_ret),
  策略: pctSigned(e.strategy_ret),
  超额: pctSigned(e.excess),
  结果: `${e.hedged ? '✓ ' : '✗ '}${e.verdict}`,
  hedged: e.hedged,
})))
const caEventStat = computed(() => {
  const ev = ca.value?.deep_dd_events ?? []
  const hedged = ev.filter((e) => e.hedged).length
  return { n: ev.length, hedged, pct: ev.length ? Math.round(hedged / ev.length * 100) : 0 }
})
```

- [ ] **Step 4: 写 4 个 chart ref / instance（追加在 `annualBarChart` 声明之后、危机Alpha 区块之后）**

```ts
const caNavEl = ref<HTMLDivElement | null>(null)
const caPieEl = ref<HTMLDivElement | null>(null)
const caTopEl = ref<HTMLDivElement | null>(null)
const caScatterEl = ref<HTMLDivElement | null>(null)
let caNavChart: echarts.ECharts | null = null
let caPieChart: echarts.ECharts | null = null
let caTopChart: echarts.ECharts | null = null
let caScatterChart: echarts.ECharts | null = null
```

- [ ] **Step 5: 写 4 个 init 函数（替换 Task 8 的空 `initCrisisAlphaCharts`）**

```ts
function initCrisisAlphaCharts() {
  if (!ca.value) return
  initCaNavChart()
  initCaPieChart()
  initCaTopChart()
  initCaScatterChart()
}

function initCaNavChart() {
  if (!caNavEl.value || !ca.value) return
  const d = ca.value
  const nav = d.nav
  caNavChart?.dispose()
  caNavChart = echarts.init(caNavEl.value)
  const mark = nav.shade_spans.length
    ? [{
        silent: true,
        itemStyle: { color: 'rgba(214,64,58,0.08)' },
        data: nav.shade_spans.map(([s, e]) => [{ xAxis: s }, { xAxis: e }]),
      }]
    : undefined
  caNavChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: [d.strategy_name, d.benchmark_name], bottom: 0 },
    grid: { left: 56, right: 24, bottom: 34, top: 40, containLabel: true },
    xAxis: { type: 'category', data: nav.dates, boundaryGap: false },
    yAxis: {
      type: 'value', name: '累计净值(起点=1.0)', scale: true,
      axisLabel: { formatter: (v: number) => v.toFixed(2) },
      splitLine: { lineStyle: { type: 'dashed' } },
    },
    series: [
      {
        name: d.strategy_name, type: 'line', data: nav.strategy, smooth: true, symbol: 'none',
        lineStyle: { color: '#d6403a', width: 2.2 }, itemStyle: { color: '#d6403a' },
        markArea: mark,
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#9ca3af', type: 'dashed' },
          data: [{ yAxis: 1 }],
        },
      },
      {
        name: d.benchmark_name, type: 'line', data: nav.benchmark, smooth: true, symbol: 'none',
        lineStyle: { color: '#4a7ec4', width: 1.8 }, itemStyle: { color: '#4a7ec4' },
      },
    ],
  })
}

function initCaPieChart() {
  if (!caPieEl.value || !caPie.value) return
  const p = caPie.value
  caPieChart?.dispose()
  caPieChart = echarts.init(caPieEl.value)
  caPieChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c}周' },
    legend: { orient: 'vertical', right: 12, top: 'center', textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['55%', '75%'],
      center: ['38%', '50%'],
      sort: false,
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: true, position: 'inner', color: '#fff', fontSize: 12,
        formatter: (x: any) => String(x.name).split('  ')[0] },
      labelLine: { show: false },
      data: p.labels.map((lb, i) => ({
        name: lb, value: p.values[i], itemStyle: { color: p.colors[i] },
      })),
    }],
    graphic: [
      { type: 'text', left: '30%', top: '42%',
        style: { text: `${p.alpha}%`, fontSize: 28, fontWeight: 800, fill: '#d6403a', textAlign: 'center' } },
      { type: 'text', left: '31%', top: '60%',
        style: { text: '危机Alpha', fontSize: 13, fill: '#1f3a68', textAlign: 'center' } },
    ],
  })
}

function initCaTopChart() {
  if (!caTopEl.value || !caTop.value) return
  const t = caTop.value
  caTopChart?.dispose()
  caTopChart = echarts.init(caTopEl.value)
  caTopChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['基准周收益', '策略周收益'], top: 0 },
    grid: { left: 52, right: 24, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'category', data: t.labels },
    yAxis: { type: 'value', name: '周收益(%)' },
    series: [
      { name: '基准周收益', type: 'bar', data: t.b, barMaxWidth: 20, itemStyle: { color: '#2e8b57' },
        label: { show: true, position: 'bottom', fontSize: 10, formatter: (x: any) => Number(x.value).toFixed(1) } },
      { name: '策略周收益', type: 'bar', data: t.s, barMaxWidth: 20, itemStyle: { color: '#d6403a' },
        label: { show: true, position: 'top', fontSize: 10, formatter: (x: any) => Number(x.value).toFixed(2) } },
    ],
  })
}

function initCaScatterChart() {
  if (!caScatterEl.value || !caScatter.value) return
  const sc = caScatter.value
  if (!sc.length) return
  caScatterChart?.dispose()
  caScatterChart = echarts.init(caScatterEl.value)
  const shortName = ca.value && ca.value.strategy_name.length > 7
    ? `${ca.value.strategy_name.slice(0, 7)}…`
    : (ca.value?.strategy_name || '')
  caScatterChart.setOption({
    tooltip: {
      trigger: 'item',
      formatter: (x: any) => `${x.seriesName}<br/>基准周收益 ${x.value[0]}%<br/>策略周收益 ${x.value[1]}%`,
    },
    legend: { orient: 'vertical', right: 12, top: 40, textStyle: { fontSize: 12 } },
    grid: { left: 58, right: 110, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'value', name: '基准周收益(%)', scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
    yAxis: { type: 'value', name: '策略周收益(%)', scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
    series: sc.map((s, i) => ({
      name: s.name, type: 'scatter', data: s.data, symbolSize: 8,
      itemStyle: { color: s.color, opacity: 0.78 },
      markLine: i === 0 ? {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#4b5563', width: 1.2 },
        data: [{ xAxis: 0 }, { yAxis: 0 }],
      } : undefined,
    })),
    graphic: [
      { type: 'text', left: 60, top: 46, style: { text: `危机Alpha\n（股跌+${shortName}涨）`, fill: '#d6403a', fontSize: 12 } },
      { type: 'text', right: 150, top: 46, style: { text: '同涨', fill: '#b8860b', fontSize: 12 } },
      { type: 'text', left: 60, bottom: 50, style: { text: '同跌', fill: '#2b6cb0', fontSize: 12 } },
      { type: 'text', right: 130, bottom: 50, style: { text: `股涨+${shortName}跌`, fill: '#1f6e4f', fontSize: 12 } },
    ],
  })
}
```

- [ ] **Step 6: 写模板（插在 Task 8 的 `</template>` 之后、`</div>` 之前）**

```html
        <!-- 一、核心结论 -->
        <div class="section-title" style="font-size:14px">一、核心结论</div>
        <p class="ca-note">
          在 {{ ca?.quadrant.n_down }} 个{{ ca?.benchmark_name }}下跌周中，{{ ca?.strategy_name }}
          有 <strong>{{ pct1(ca?.quadrant.alpha_ratio) }}</strong> 的周次实现危机对冲（其中
          <strong>{{ pct1(ca?.quadrant.up_rate) }}</strong> 逆势上涨、
          <strong>{{ pct1((ca?.quadrant.counts.跌得少 ?? 0) / Math.max(ca?.quadrant.n_down ?? 1, 1)) }}</strong>
          跌幅不足股票一半），而真正“跟着跌”或“跌更多”的周次合计仅
          <strong>{{ pct1(ca?.quadrant.follow_ratio) }}</strong>。下跌周平均收益
          <strong :class="valueClass(ca?.quadrant.down_avg_s)">{{ pctSigned(ca?.quadrant.down_avg_s) }}</strong>
          （同期{{ ca?.benchmark_name }}平均 {{ pctSigned(ca?.quadrant.down_avg_b) }}），
          与基准整体相关仅 <strong>{{ num(ca?.performance.corr, 3) }}</strong>，
          且在最深的 {{ caEventStat.n }} 次基准回撤中有 {{ caEventStat.hedged }} 次逆势录得正收益。
        </p>
        <div class="ca-tags">
          <span class="ca-tag" style="background:#d6403a">
            危机Alpha {{ pct1(ca?.quadrant.alpha_ratio) }} · {{ ca?.alpha_rating }}
          </span>
          <span class="ca-tag" style="background:#f0a429">
            综合评分 {{ num(ca?.score.total_score, 1) }} · {{ ca?.score.rating }}
          </span>
        </div>

        <!-- 二、产品与基准走势 -->
        <div class="section-title" style="font-size:14px">二、产品与基准走势</div>
        <div class="chart-box">
          <div ref="caNavEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">
          淡红色阴影为 {{ ca?.benchmark_name }} 深度回撤区间（自高点回撤超过 5%）；
          危机Alpha各项统计均在策略与基准的共同交易日内计算。
        </p>
        <div class="table-container">
          <table>
            <thead><tr><th>指标</th><th>{{ ca?.strategy_name }}</th><th>{{ ca?.benchmark_name }}</th></tr></thead>
            <tbody>
              <tr v-for="r in caPerfRows" :key="r.指标">
                <td>{{ r.指标 }}</td>
                <td :class="valueClass(0)">{{ r.策略 }}</td>
                <td>{{ r.基准 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 三、危机Alpha四档分类 -->
        <div class="section-title" style="font-size:14px">三、危机Alpha四档分类</div>
        <div class="chart-box">
          <div ref="caPieEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table>
            <thead><tr><th>分类</th><th>周数</th><th>占比</th><th>平均周收益</th></tr></thead>
            <tbody>
              <tr v-for="r in caCatRows" :key="r.分类">
                <td :style="{ color: r.color, fontWeight: 700 }">{{ r.分类 }}</td>
                <td>{{ r.周数 }}</td>
                <td>{{ r.占比 }}</td>
                <td :class="r.平均周收益 === '—' ? '' : valueClass(Number(r.平均周收益))">{{ r.平均周收益 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="ca-sub">深跌周条件检验（更严苛）</div>
        <div class="table-container">
          <table class="ca-cond-table">
            <thead><tr><th>条件</th><th>周数</th><th>股票平均</th><th>策略平均</th><th>收涨比例</th></tr></thead>
            <tbody>
              <tr v-for="r in caCondRows" :key="r.条件">
                <td>{{ r.条件 }}</td>
                <td>{{ r.周数 }}</td>
                <td class="text-green">{{ r.股票平均 }}</td>
                <td :class="valueClass(Number(r.策略平均))">{{ r.策略平均 }}</td>
                <td>{{ r.收涨比例 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 四、极端下跌周 TOP N -->
        <div class="section-title" style="font-size:14px">四、极端下跌周 TOP{{ caTopStat.n }}</div>
        <div class="chart-box">
          <div ref="caTopEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">
          {{ ca?.benchmark_name }}跌幅最大的 {{ caTopStat.n }} 周中，{{ ca?.strategy_name }}
          有 {{ caTopStat.up }} 周逆势录得正收益，{{ caTopStat.beat }} 周跑赢基准。
        </p>

        <!-- 五、全周期四象限分布 -->
        <div class="section-title" style="font-size:14px">五、全周期四象限分布</div>
        <div class="chart-box">
          <div ref="caScatterEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">左上象限（股跌+策略涨）即“危机Alpha”区。</p>

        <!-- 六、深度回撤事件独立性 -->
        <div class="section-title" style="font-size:14px">六、深度回撤事件独立性</div>
        <div v-if="caEventRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内未触发基准深度回撤事件。</p></div>
        </div>
        <template v-else>
          <div class="table-container">
            <table class="ca-event-table">
              <thead><tr><th>回撤起点</th><th>回撤低点</th><th>基准</th><th>策略</th><th>超额</th><th>结果</th></tr></thead>
              <tbody>
                <tr v-for="r in caEventRows" :key="r.回撤起点">
                  <td>{{ r.回撤起点 }}</td>
                  <td>{{ r.回撤低点 }}</td>
                  <td class="text-green">{{ r.基准 }}</td>
                  <td :class="valueClass(Number(r.策略))">{{ r.策略 }}</td>
                  <td :class="valueClass(Number(r.超额))">{{ r.超额 }}</td>
                  <td :style="{ color: r.hedged ? '#d6403a' : '#6b7280', fontWeight: 700 }">{{ r.结果 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="ca-note">
            基准深度回撤（&gt;5%）事件共 {{ caEventStat.n }} 次，{{ ca?.strategy_name }}
            有 {{ caEventStat.hedged }} 次逆势录得正收益，回撤独立性 {{ caEventStat.pct }}%。
          </p>
        </template>
```

- [ ] **Step 7: 接线生命周期**

`handleResize`（`:633-640`）末尾追加：

```ts
  caNavChart?.resize()
  caPieChart?.resize()
  caTopChart?.resize()
  caScatterChart?.resize()
```

`onUnmounted`（`:646-653`）末尾追加：

```ts
  caNavChart?.dispose()
  caPieChart?.dispose()
  caTopChart?.dispose()
  caScatterChart?.dispose()
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部 PASS（+8）

- [ ] **Step 9: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0

- [ ] **Step 10: 提交**

```powershell
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 危机Alpha模块一~六（结论/走势/四档/TOP10/四象限/回撤事件）"
```

---

### Task 10: 危机Alpha 模块七~十二（多宽基 / 月度 / 相关性年度 / 代价 / 评分 / 下跌周明细）

**Files:**
- Modify: `web/src/views/Backtest.vue`（5 个 chart + 6 组表 + 模板 + 生命周期）
- Test: `web/src/__tests__/Backtest.test.ts`

**Interfaces:**
- Consumes: Task 8/9 的 `ca`、`initCrisisAlphaCharts`
- Produces: refs+instances `caMultiEl/caMonthEl/caCorrEl/caAnnualEl/caDimEl` + 对应 5 个 chart 与 init 函数；computed `caMultiRows/caMultiSeries/caMonthRows/caBigDownRows/caMonthSeries/caCorrRows/caAnnualRows/caAnnualSeries/caCostRows/caDimRows/caDimSeries/caDownRows`

- [ ] **Step 1: 写失败测试（追加到测试文件末尾）**

```ts
describe('Backtest.vue — 危机Alpha 模块七~十二', () => {
  it('渲染模块七~十二标题，累计 9 个图表容器', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    for (const t of [
      '七、多宽基一致性', '八、月度视角', '九、动态相关性与年度趋势',
      '十、代价检验（上涨周表现）', '十一、综合危机保护评分', '十二、全部下跌周明细（6 周）',
    ]) {
      expect(w.text()).toContain(t)
    }
    expect(w.findAll('.ca-chart').length).toBe(9)
  })

  it('12 个模块标题按源顺序渲染', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const titles = [
      '一、核心结论', '二、产品与基准走势', '三、危机Alpha四档分类', '四、极端下跌周 TOP3',
      '五、全周期四象限分布', '六、深度回撤事件独立性', '七、多宽基一致性', '八、月度视角',
      '九、动态相关性与年度趋势', '十、代价检验（上涨周表现）', '十一、综合危机保护评分',
      '十二、全部下跌周明细（6 周）',
    ]
    const text = w.text()
    let last = -1
    for (const t of titles) {
      const i = text.indexOf(t)
      expect(i, `标题缺失或顺序错误: ${t}`).toBeGreaterThan(last)
      last = i
    }
  })

  it('七：多宽基明细表含主基准标记与 2 个基准', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('沪深300（主基准）')
    expect(text).toContain('中证500')
    expect(w.findAll('.ca-multi-table tbody tr').length).toBe(2)
  })

  it('八：月度 4 状态表 + 大跌月明细（1 行）+ 月度对冲比例', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    for (const t of ['大涨', '小涨', '小跌', '大跌']) expect(text).toContain(t)
    expect(text).toContain('大跌月明细（股票月跌 >5%）')
    expect(w.findAll('.ca-bigdown-table tbody tr').length).toBe(1)
    expect(text).toContain('下跌月份共 50.0% 实现危机对冲（月度口径）')
  })

  it('九：年度表带 * 标记（非完整年）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.text()).toContain('2024*')
    expect(w.text()).toContain('标 * 的年度为数据区间首尾的非完整年度')
  })

  it('十：代价检验 5 行', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.findAll('.ca-cost-table tbody tr').length).toBe(5)
    const text = w.text()
    for (const t of ['上涨周拖后腿比例', '上涨周同步上涨比例', '收益弹性比', '下尾相关（下跌周内）', 'Q1分位相关（最差25%周）']) {
      expect(text).toContain(t)
    }
  })

  it('十一：评分维度表 3 行 + 综合评分行', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-dim-table tbody tr')
    expect(rows.length).toBe(4)  // 3 维 + 综合评分
    expect(w.text()).toContain('综合评分')
  })

  it('十二：下跌周明细表按日期倒序（2024-01-26 在 2024-01-05 之前）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-down-table tbody tr')
    expect(rows.length).toBe(6)
    const text = w.text()
    expect(text.indexOf('2024-01-26')).toBeLessThan(text.indexOf('2024-01-05'))
  })

  it('四档配色为红/橙/浅绿/深绿（与红涨绿跌一致）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const html = w.html()
    expect(html).toContain('#d6403a')
    expect(html).toContain('#f0a429')
    expect(html).toContain('#9dc88d')
    expect(html).toContain('#1f6e4f')
  })

  it('重算后重新初始化 9 个危机Alpha 图表', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const before = w.findAll('.ca-chart').length
    expect(before).toBe(9)
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
    })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.findAll('.ca-chart').length).toBe(9)
    expect(w.text()).toContain('重新计算完成')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts -t "危机Alpha 模块七~十二"`
Expected: FAIL —— `.ca-chart` 数量 4，模块七~十二标题缺失

- [ ] **Step 3: 写 computed（追加在 Task 9 的 computed 之后）**

```ts
// 七：多宽基一致性
const caMultiRows = computed(() => (ca.value?.multi_bench ?? []).map((m) => ({
  基准: `${m.name}${m.is_main ? '（主基准）' : ''}`,
  下跌周数: m.n_down,
  危机Alpha: pct1(m.alpha_ratio),
  逆势上涨比例: pct1(m.up_rate),
})))
const caMultiSeries = computed(() => {
  const rows = ca.value?.multi_bench ?? []
  if (rows.length < 2) return null
  return {
    labels: rows.map((m) => `${m.name}${m.is_main ? '（主基准）' : ''}`),
    values: rows.map((m) => +(m.alpha_ratio * 100).toFixed(4)),
    colors: rows.map((m) => (m.is_main ? '#d6403a' : '#f0a429')),
    main: +(((ca.value?.quadrant.alpha_ratio ?? 0)) * 100).toFixed(4),
  }
})

// 八：月度视角
const caMonthRows = computed(() => {
  const d = ca.value
  if (!d) return []
  return MONTH_STATES.map((s) => {
    const st = d.monthly.states[s]
    return {
      市场状态: s,
      月数: st?.n ?? 0,
      股票月均: pctSigned(st?.b_avg ?? 0, 1),
      策略月均: pctSigned(st?.s_avg ?? 0),
      正收益比例: pct1(st?.win_rate ?? 0),
    }
  })
})
const caBigDownRows = computed(() => (ca.value?.monthly.big_down ?? []).map((r) => ({
  月份: String(r.日期).slice(0, 7),
  基准: pctSigned(r.基准, 1),
  策略: pctSigned(r.策略),
})))
const caMonthSeries = computed(() => {
  const d = ca.value
  if (!d) return null
  return {
    labels: MONTH_STATES.map((s) => `${s}（${d.monthly.states[s]?.n ?? 0}月）`),
    b: MONTH_STATES.map((s) => +(((d.monthly.states[s]?.b_avg ?? 0)) * 100).toFixed(4)),
    s: MONTH_STATES.map((s) => +(((d.monthly.states[s]?.s_avg ?? 0)) * 100).toFixed(4)),
  }
})

// 九：滚动/扩展相关（不足 52 周段用扩展窗口补全，接缝对齐）
const caCorrRows = computed(() => {
  const d = ca.value
  if (!d) return null
  const rc = d.rolling_corr
  if (!rc.length) return null
  const first = rc[0].date
  const early = d.expanding_corr.filter((p) => p.date < first)
  const dates: string[] = []
  const seen = new Set<string>()
  for (const p of [...early, ...rc]) {
    if (!seen.has(p.date)) { seen.add(p.date); dates.push(p.date) }
  }
  const pos = new Map(dates.map((v, i) => [v, i]))
  const earlyData: (number | null)[] = new Array(dates.length).fill(null)
  for (const p of early) earlyData[pos.get(p.date)!] = p.value
  const rcData: (number | null)[] = new Array(dates.length).fill(null)
  for (const p of rc) rcData[pos.get(p.date)!] = p.value
  if (early.length) earlyData[pos.get(first)!] = rc[0].value
  return { dates, earlyData, rcData }
})
const caAnnualRows = computed(() => (ca.value?.annual ?? []).map((r) => ({
  年度: `${r.年份}${r.完整 ? '' : '*'}`,
  下跌周: r.下跌周,
  危机Alpha: pct1(r.危机Alpha),
  策略收益: pctSigned(r.策略收益, 1),
  基准收益: pctSigned(r.基准收益, 1),
  完整: r.完整,
})))
const caAnnualSeries = computed(() => {
  const rows = ca.value?.annual ?? []
  if (!rows.length) return null
  return {
    labels: rows.map((r) => `${r.年份}${r.完整 ? '' : '*'}`),
    values: rows.map((r) => +(r.危机Alpha * 100).toFixed(4)),
    colors: rows.map((r) => (r.危机Alpha * 100 >= 85 ? '#d6403a' : '#f0a429')),
    main: +(((ca.value?.quadrant.alpha_ratio ?? 0)) * 100).toFixed(4),
  }
})

// 十：代价检验（照搬源 L5159-5170）
const caCostRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const c = d.cost
  const n = d.strategy_name
  return [
    { 指标: '上涨周拖后腿比例', 数值: pct1(c.drag), 解读: `股票上涨时${n}为负的周次占比，属低β配置型策略的正常代价` },
    { 指标: '上涨周同步上涨比例', 数值: pct1(c.sync_up), 解读: `股票上涨时${n}跟随上涨的周次占比` },
    { 指标: '收益弹性比', 数值: num(c.elasticity, 2), 解读: `${n}上涨周均收益 / 股票上涨周均收益，值低说明不依赖股市β` },
    { 指标: '下尾相关（下跌周内）', 数值: num(c.tail_corr, 2), 解读: '下跌情境下相关性为负，越跌越对冲' },
    { 指标: 'Q1分位相关（最差25%周）', 数值: num(c.q1_corr, 3), 解读: '极端下跌时相关性为负，尾部对冲有效' },
  ]
})

// 十一：9 维加权评分
const caDimRows = computed(() => (ca.value?.score.dimensions ?? []).map((x) => ({
  维度: x.维度,
  类型: x.类型,
  权重: `${(x.权重 * 100).toFixed(0)}%`,
  得分: num(x.得分, 1),
  评级: x.评级,
  贡献: num(x.贡献, 1),
})))
const caDimSeries = computed(() => {
  const dims = ca.value?.score.dimensions ?? []
  if (!dims.length) return null
  const sorted = [...dims].sort((a, b) => b.得分 - a.得分)
  return {
    names: sorted.map((x) => x.维度),
    values: sorted.map((x) => Math.max(x.得分, 3)),
    raw: sorted.map((x) => x.得分),
    colors: sorted.map((x) => CRISIS_SCORE_COLORS[x.评级] || '#b0a890'),
  }
})

// 十二：全部下跌周明细（后端已按日期倒序）
const caDownRows = computed(() => (ca.value?.down_weeks ?? []).map((r) => ({
  日期: r.日期,
  基准: pctSigned(r.基准收益 ?? 0),
  策略: pctSigned(r.策略收益 ?? 0),
  超额: pctSigned(r.超额 ?? 0),
  分类: r.分类,
  color: CRISIS_COLORS[r.分类] || '#111827',
})))
```

- [ ] **Step 4: 写 5 个 chart ref / instance**

```ts
const caMultiEl = ref<HTMLDivElement | null>(null)
const caMonthEl = ref<HTMLDivElement | null>(null)
const caCorrEl = ref<HTMLDivElement | null>(null)
const caAnnualEl = ref<HTMLDivElement | null>(null)
const caDimEl = ref<HTMLDivElement | null>(null)
let caMultiChart: echarts.ECharts | null = null
let caMonthChart: echarts.ECharts | null = null
let caCorrChart: echarts.ECharts | null = null
let caAnnualChart: echarts.ECharts | null = null
let caDimChart: echarts.ECharts | null = null
```

- [ ] **Step 5: 写 5 个 init 函数**

在 `initCaScatterChart` 之后追加（并把 `initCrisisAlphaCharts` 内的调用补全为 9 个）：

```ts
function initCaMultiChart() {
  if (!caMultiEl.value || !caMultiSeries.value) return
  const m = caMultiSeries.value
  caMultiChart?.dispose()
  caMultiChart = echarts.init(caMultiEl.value)
  caMultiChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 52, right: 24, bottom: 60, top: 30, containLabel: true },
    xAxis: { type: 'category', data: m.labels, axisLabel: { rotate: 20, fontSize: 11 } },
    yAxis: { type: 'value', name: '危机Alpha(%)', max: 112 },
    series: [{
      type: 'bar', barMaxWidth: 40,
      data: m.values.map((v, i) => ({ value: v, itemStyle: { color: m.colors[i] } })),
      label: { show: true, position: 'top', formatter: (x: any) => `${Number(x.value).toFixed(1)}%` },
      markLine: {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#1f3a68', type: 'dashed', width: 1.4 },
        data: [{ yAxis: m.main }],
      },
    }],
  })
}

function initCaMonthChart() {
  if (!caMonthEl.value || !caMonthSeries.value) return
  const m = caMonthSeries.value
  caMonthChart?.dispose()
  caMonthChart = echarts.init(caMonthEl.value)
  caMonthChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['基准月均', '策略月均'], top: 0 },
    grid: { left: 52, right: 24, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'category', data: m.labels },
    yAxis: { type: 'value', name: '月均收益(%)' },
    series: [
      { name: '基准月均', type: 'bar', data: m.b, barMaxWidth: 26, itemStyle: { color: '#9db3d2' },
        label: { show: true, position: 'top', fontSize: 11, formatter: (x: any) => Number(x.value).toFixed(1) } },
      { name: '策略月均', type: 'bar', data: m.s, barMaxWidth: 26, itemStyle: { color: '#d6403a' },
        label: { show: true, position: 'top', fontSize: 11, formatter: (x: any) => Number(x.value).toFixed(2) } },
    ],
  })
}

function initCaCorrChart() {
  if (!caCorrEl.value || !caCorrRows.value) return
  const c = caCorrRows.value
  caCorrChart?.dispose()
  caCorrChart = echarts.init(caCorrEl.value)
  caCorrChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['扩展窗口相关（不足52周）', '滚动52周相关'], top: 0 },
    grid: { left: 52, right: 24, bottom: 36, top: 40, containLabel: true },
    xAxis: { type: 'category', data: c.dates, boundaryGap: false },
    yAxis: { type: 'value', name: '相关系数', min: -1.05, max: 1.05 },
    series: [
      { name: '扩展窗口相关（不足52周）', type: 'line', data: c.earlyData, symbol: 'none', connectNulls: true,
        lineStyle: { color: '#9ca3af', width: 1.4, type: 'dashed' } },
      { name: '滚动52周相关', type: 'line', data: c.rcData, symbol: 'none', connectNulls: true,
        lineStyle: { color: '#1f3a68', width: 2 }, areaStyle: { color: 'rgba(31,58,104,0.10)' },
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#6b7280', type: 'dashed' },
          data: [{ yAxis: 0 }],
        } },
    ],
  })
}

function initCaAnnualChart() {
  if (!caAnnualEl.value || !caAnnualSeries.value) return
  const a = caAnnualSeries.value
  caAnnualChart?.dispose()
  caAnnualChart = echarts.init(caAnnualEl.value)
  caAnnualChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 52, right: 24, bottom: 30, top: 30, containLabel: true },
    xAxis: { type: 'category', data: a.labels },
    yAxis: { type: 'value', name: '危机Alpha(%)', max: 112 },
    series: [{
      type: 'bar', barMaxWidth: 40,
      data: a.values.map((v, i) => ({ value: v, itemStyle: { color: a.colors[i] } })),
      label: { show: true, position: 'top', formatter: (x: any) => Number(x.value).toFixed(0) },
      markLine: {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#1f3a68', type: 'dashed' },
        data: [{ yAxis: a.main }],
      },
    }],
  })
}

function initCaDimChart() {
  if (!caDimEl.value || !caDimSeries.value) return
  const m = caDimSeries.value
  caDimChart?.dispose()
  caDimChart = echarts.init(caDimEl.value)
  caDimChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps: any) => `${ps[0].name}<br/>得分 ${m.raw[ps[0].dataIndex].toFixed(1)}` },
    grid: { left: 104, right: 40, bottom: 30, top: 20, containLabel: true },
    xAxis: { type: 'value', name: '得分(0-100)', max: 112 },
    yAxis: { type: 'category', data: m.names, inverse: true, axisTick: { show: false } },
    series: [{
      type: 'bar', barMaxWidth: 22,
      data: m.values.map((v, i) => ({ value: v, itemStyle: { color: m.colors[i] } })),
      label: { show: true, position: 'right', formatter: (x: any) => m.raw[x.dataIndex].toFixed(0) },
    }],
  })
}
```

`initCrisisAlphaCharts`（Task 9 版本）替换为：

```ts
function initCrisisAlphaCharts() {
  if (!ca.value) return
  initCaNavChart()
  initCaPieChart()
  initCaTopChart()
  initCaScatterChart()
  initCaMultiChart()
  initCaMonthChart()
  initCaCorrChart()
  initCaAnnualChart()
  initCaDimChart()
}
```

- [ ] **Step 6: 写模板（插在 Task 9 的「六」块之后、Task 8 的 `</template>` 之前）**

```html
        <!-- 七、多宽基一致性 -->
        <div class="section-title" style="font-size:14px">七、多宽基一致性</div>
        <div v-if="!caMultiSeries" class="table-container">
          <div class="empty-state"><p>可用宽基不足 2 个，跳过多宽基一致性检验。</p></div>
        </div>
        <template v-else>
          <div class="chart-box">
            <div ref="caMultiEl" class="ca-chart"></div>
          </div>
          <div class="table-container">
            <table class="ca-multi-table">
              <thead><tr><th>基准</th><th>下跌周数</th><th>危机Alpha</th><th>逆势上涨比例</th></tr></thead>
              <tbody>
                <tr v-for="r in caMultiRows" :key="r.基准">
                  <td>{{ r.基准 }}</td>
                  <td>{{ r.下跌周数 }}</td>
                  <td>{{ r.危机Alpha }}</td>
                  <td>{{ r.逆势上涨比例 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="ca-note">各宽基均在同一时间窗口内统计；深色虚线为主基准的危机Alpha占比。</p>
        </template>

        <!-- 八、月度视角 -->
        <div class="section-title" style="font-size:14px">八、月度视角</div>
        <div class="chart-box">
          <div ref="caMonthEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table>
            <thead><tr><th>市场状态</th><th>月数</th><th>股票月均</th><th>策略月均</th><th>正收益比例</th></tr></thead>
            <tbody>
              <tr v-for="r in caMonthRows" :key="r.市场状态">
                <td>{{ r.市场状态 }}</td>
                <td>{{ r.月数 }}</td>
                <td class="text-green">{{ r.股票月均 }}</td>
                <td :class="valueClass(Number(r.策略月均))">{{ r.策略月均 }}</td>
                <td>{{ r.正收益比例 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="ca-sub">大跌月明细（股票月跌 &gt;5%）</div>
        <div v-if="caBigDownRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内无大跌月。</p></div>
        </div>
        <div v-else class="table-container">
          <table class="ca-bigdown-table">
            <thead><tr><th>月份</th><th>基准</th><th>策略</th></tr></thead>
            <tbody>
              <tr v-for="r in caBigDownRows" :key="r.月份">
                <td>{{ r.月份 }}</td>
                <td class="text-green">{{ r.基准 }}</td>
                <td :class="valueClass(Number(r.策略))">{{ r.策略 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="ca-note">下跌月份共 {{ pct1(ca?.monthly.hedge_ratio) }} 实现危机对冲（月度口径）。</p>

        <!-- 九、动态相关性与年度趋势 -->
        <div class="section-title" style="font-size:14px">九、动态相关性与年度趋势</div>
        <div v-if="!caCorrRows" class="table-container">
          <div class="empty-state"><p>周收益样本不足 52 周，无法计算滚动相关性。</p></div>
        </div>
        <template v-else>
          <div class="chart-box">
            <div ref="caCorrEl" class="ca-chart"></div>
          </div>
          <p class="ca-note">
            实线为 52 周滚动相关系数；数据开头不足 52 周的部分以灰色虚线（扩展窗口相关）补全。0 轴以下表示两类资产反向、具对冲性。
          </p>
        </template>
        <template v-if="caAnnualRows.length">
          <div class="chart-box">
            <div ref="caAnnualEl" class="ca-chart"></div>
          </div>
          <div class="table-container">
            <table>
              <thead><tr><th>年度</th><th>下跌周</th><th>危机Alpha</th><th>策略收益</th><th>基准收益</th></tr></thead>
              <tbody>
                <tr v-for="r in caAnnualRows" :key="r.年度">
                  <td>{{ r.年度 }}</td>
                  <td>{{ r.下跌周 }}</td>
                  <td>{{ r.危机Alpha }}</td>
                  <td :class="valueClass(Number(r.策略收益))">{{ r.策略收益 }}</td>
                  <td :class="valueClass(Number(r.基准收益))">{{ r.基准收益 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="caAnnualRows.some(r => !r.完整)" class="ca-note">
            注：标 * 的年度为数据区间首尾的非完整年度（有效收益周不足 40 周），指标仅供参考。
          </p>
        </template>

        <!-- 十、代价检验（上涨周表现） -->
        <div class="section-title" style="font-size:14px">十、代价检验（上涨周表现）</div>
        <div class="table-container">
          <table class="ca-cost-table">
            <thead><tr><th>指标</th><th>数值</th><th>解读</th></tr></thead>
            <tbody>
              <tr v-for="r in caCostRows" :key="r.指标">
                <td>{{ r.指标 }}</td>
                <td>{{ r.数值 }}</td>
                <td style="color:#6b7280">{{ r.解读 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 十一、综合危机保护评分 -->
        <div class="section-title" style="font-size:14px">十一、综合危机保护评分</div>
        <div class="chart-box">
          <div ref="caDimEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table class="ca-dim-table">
            <thead><tr><th>维度</th><th>类型</th><th>权重</th><th>得分</th><th>评级</th><th>贡献</th></tr></thead>
            <tbody>
              <tr v-for="r in caDimRows" :key="r.维度">
                <td>{{ r.维度 }}</td>
                <td>{{ r.类型 }}</td>
                <td>{{ r.权重 }}</td>
                <td>{{ r.得分 }}</td>
                <td :style="{ color: CRISIS_SCORE_COLORS[r.评级] || '#d6403a', fontWeight: 700 }">{{ r.评级 }}</td>
                <td>{{ r.贡献 }}</td>
              </tr>
              <tr>
                <td style="font-weight:700">综合评分</td>
                <td>—</td>
                <td>100%</td>
                <td>—</td>
                <td :style="{ color: CRISIS_SCORE_COLORS[ca?.score.rating || ''] || '#d6403a', fontWeight: 700 }">
                  {{ ca?.score.rating }}
                </td>
                <td>{{ num(ca?.score.total_score, 1) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 十二、全部下跌周明细 -->
        <div class="section-title" style="font-size:14px">十二、全部下跌周明细（{{ ca?.quadrant.n_down }} 周）</div>
        <div v-if="caDownRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内无基准下跌周。</p></div>
        </div>
        <div v-else class="table-container">
          <table class="ca-down-table">
            <thead><tr><th>日期</th><th>基准</th><th>策略</th><th>超额</th><th>分类</th></tr></thead>
            <tbody>
              <tr v-for="r in caDownRows" :key="r.日期">
                <td>{{ r.日期 }}</td>
                <td class="text-green">{{ r.基准 }}</td>
                <td :class="valueClass(Number(r.策略))">{{ r.策略 }}</td>
                <td :class="valueClass(Number(r.超额))">{{ r.超额 }}</td>
                <td :style="{ color: r.color, fontWeight: 700 }">{{ r.分类 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
```

- [ ] **Step 7: 接线生命周期**

`handleResize` 追加：

```ts
  caMultiChart?.resize()
  caMonthChart?.resize()
  caCorrChart?.resize()
  caAnnualChart?.resize()
  caDimChart?.resize()
```

`onUnmounted` 追加：

```ts
  caMultiChart?.dispose()
  caMonthChart?.dispose()
  caCorrChart?.dispose()
  caAnnualChart?.dispose()
  caDimChart?.dispose()
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部 PASS（+9）

- [ ] **Step 9: 类型检查 + 构建**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0

Run: `cd web && npm run build`
Expected: 构建成功（`vue-tsc -b && vite build` 均通过）

- [ ] **Step 10: 提交**

```powershell
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 危机Alpha模块七~十二（多宽基/月度/相关性年度/代价/评分/下跌周）"
```

---

### Task 11: 全量验证与收尾

**Files:** 无新增（只验证；仅在发现问题时修）

- [ ] **Step 1: 后端全量测试**

Run: `cd main && uv run pytest core/tests/ -q`
Expected: 全部 PASS，数量 ≥ 现有 473 + 新增（27 分析器 + 16 service + 3 集成）

- [ ] **Step 2: 迁移链验证（不碰 dev.db）**

Run: `cd main && uv run alembic heads`
Expected: 恰好一个 head：`f2a3b4c5d6e7`

Run: `cd main && uv run alembic upgrade head --sql`
Expected: 输出含 `CREATE TABLE backtest_crisis_alphas`，退出码 0

- [ ] **Step 3: 前端全量测试**

Run: `cd web && npx vitest run`
Expected: 全部 PASS，数量 = 126 + 4（Task 7）+ 7（Task 8）+ 8（Task 9）+ 9（Task 10）= 154

- [ ] **Step 4: 前端类型检查与构建**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0

Run: `cd web && npm run build`
Expected: 成功

- [ ] **Step 5: 人工核对（对照源项目）**

打开某条 `status='completed'` 的回测详情，逐项确认：
1. 「整体表现指标」8 张卡，危机两卡值与「专题分析」4 KPI 一致；
2. 12 个模块标题顺序与源 L4804/4824/4864/4927/4952/4982/5013/5049/5096/5158/5174/5221 一致；
3. 「一、核心结论」中的下跌周数/占比/相关系数与后端 payload 数值一致；
4. 四档配色为红/橙/浅绿/深绿（`:986-991` 之外未改动既有风险色）；
5. 切回列表再进入其他回测，9 个图表实例被正确 dispose（无重复初始化导致的 canvas 叠加）。

- [ ] **Step 6: 检查工作区与提交历史**

```powershell
git status --short
git log --oneline -12
```

Expected: `git status --short` 无输出（或仅剩计划文档本身的改动，若未随任务提交）；`git log` 可见本特性提交序列（首个为 `test: 补充自定义基准指数取数单测`，其后为 Task 4~10 的 9 条）。

若 `docs/superpowers/plans/2026-09-25-crisis-alpha-analysis.md` 尚未提交：

```powershell
git add docs/superpowers/plans/2026-09-25-crisis-alpha-analysis.md docs/superpowers/specs/2026-09-25-crisis-alpha-analysis-design.md
git commit -m "docs: 危机Alpha专题分析设计与实施计划"
```

---

## 计划自审

**1. 规格覆盖**

| 规格条目 | 对应任务 |
|---|---|
| 分析器 1:1 移植（常量/四档/四象限/条件检验/TOP10/周明细） | Task 1 |
| 统计层（回撤事件/阴影区间/滚动+扩展相关/代价/月度/年度/绩效） | Task 2 |
| 9 维加权评分 + 12 键 summary + payload + `sanitize_for_json` | Task 3 |
| `backtest_crisis_alphas` 表 / 迁移 / 显式 pandas 依赖 | Task 4 |
| `build_index_close_series` 批量取数 + `build_benchmark_data` 重构 | Task 5 |
| 回测主 commit 后独立事务计算 + 三态落库 + 异常不污染回测 | Task 5 |
| `recompute_crisis_alpha` 从库内快照重建 + upsert 覆盖 | Task 5 |
| `get_record_detail` 透出 `crisis_alpha`（有/无两态） | Task 5 |
| `POST .../crisis-alpha/recompute` 路由 + re-export + 集成测试 | Task 6 |
| 前端 TS 类型（含中文键）+ `recomputeCrisisAlpha` | Task 7 |
| 整体表现 6→8 卡 + 滚动锚点 + `.stats-grid` 4 列 | Task 7 |
| section 骨架 / 状态条 / 4 KPI / 三态 / 重算按钮 | Task 8 |
| 模块一~六（4 图） | Task 9 |
| 模块七~十二（5 图） | Task 10 |
| 生命周期接线（initCharts / resize / onUnmounted / 重算后重取详情） | Task 9、Task 10 |
| 降级矩阵（<30 天 / 主基准失败 / 跳过宽基 / 无深跌周 / 无回撤事件 / <52 周 / 无下跌月 / 无基准配置） | Task 2、Task 3、Task 5、Task 8 |
| 全量验证命令（pytest / vitest / vue-tsc / build） | Task 11 |
| 明确不迁移：L5238-5273 全部策略横向对比 | 计划中无任何任务产出该 UI，确认排除 |

**2. 占位符扫描**：全文无 TBD/TODO/「类似 Task N」/「补充错误处理」类模糊步骤；每个代码步骤都给出可直接落盘的代码。

**3. 类型一致性核对**
- `build_crisis_alpha_payload(strategy_nav, main_code, main_name, main_closes, benchmark_closes, strategy_name)` —— Task 3 定义，Task 5 `compute_and_store_crisis_alpha` 按此顺序调用，Task 3 测试同序调用；三处一致。
- `composite_score(multi_bench_alpha_ratios=None)` / `summary(multi_bench_alpha_ratios=None)` —— Task 3 定义并被 `build_crisis_alpha_payload` 传 `[row["alpha_ratio"] for row in multi_rows]`，Task 3 测试以位置/关键字调用，一致。
- payload 键名 → TS 键名 → Vue 读取路径三者逐一对应：`quadrant.n_down/up_rate/alpha_ratio/follow_ratio/down_avg_s/down_avg_b/counts.跌得少`、`performance.{start,end,corr,s.*}`、`score.{total_score,rating,dimensions[]}`、`nav.{dates,strategy,benchmark,shade_spans}`、`monthly.{states,hedge_ratio,big_down[]}`、`annual[]`、`cost`、`extreme_top[]`、`deep_dd_events[]`、`multi_bench[]`、`down_weeks[]`、`rolling_corr[]`、`expanding_corr[]`、`conditions[]`、`weekly_detail[]`、`alpha_rating`、`n_weeks`、`strategy_name`、`benchmark_name`。
- `BacktestCrisisAlphaItem.status` 三态字面量与后端 `_upsert_crisis_alpha(status=...)` 的三个取值一致；Vue `caStatus` 与 `v-if`/`v-else-if` 分支覆盖三态 + `null`。
- chart ref 名 `caNavEl`/`caPieEl`/`caTopEl`/`caScatterEl`/`caMultiEl`/`caMonthEl`/`caCorrEl`/`caAnnualEl`/`caDimEl` 与实例名一一对应，模板 `ref`、init 函数、`handleResize`、`onUnmounted` 四处名字一致，共 9 个。
- `caMultiEl` 模板 `ref` 位于 `<template v-else>` 内，条件为 `caMultiSeries` 非空；init 函数的守卫与之相同，故 ref 存在时 `caMultiSeries` 必然非空，不会出现空图。
- 测试 fixture 的 `score.dimensions` 只放 3 维，故 Task 10 断言 `.ca-dim-table tbody tr` 长度为 4（3 维 + 综合评分行），Task 3 的后端单测断言完整 9 维——两者口径不同是刻意的（前端测渲染、后端测算法）。

**4. 已知取舍（写入计划而非隐藏）**
- 单个回测的 payload 约 60-120KB（`weekly_detail` + `rolling_corr` 各约 260 行），SQLite 单用户可接受；若后续记录数过多再裁剪，本期不做（与规格「风险与对策」一致）。
- 模块五象限文字标注、模块十一类型标签内嵌等 Plotly 专属装饰在 ECharts 下做了等价简化（`graphic`/`markLine`），语义与配色不变。


