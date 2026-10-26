# 持续性检验（阿尔法衰减 vs 风格钟摆）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在回测详情新增「持续性检验」模块，分年度给出超额/风险/交易结构 + 滚动年化，用规则判定「阿尔法衰减 vs 风格钟摆」。

**Architecture:** 纯计算引擎 `core/engine/persistence_alpha.py`（无 DB I/O，可单测）+ 服务层 `recompute_persistence` 落库新表 `backtest_persistence_analyses`（对齐危机Alpha/稳健性模式）+ 路由 `POST /records/{id}/persistence/recompute` + 前端详情报告新折叠区块。数据全部复用：组合日快照 NAV、`backtest_trades`、`build_index_close_series` 批量拉「自身基准 + 6 宽基」。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / Alembic / Vue 3 / ECharts / vitest / pytest

## Global Constraints

- 后端测试命令：`cd main && .\.venv\Scripts\python.exe -m pytest core/tests/`
- 前端测试命令：`cd web && npx vitest run`；构建：`npm run build`
- 数据表变更一律走 Alembic 迁移，禁止手动 ALTER
- 复用 `core/engine/crisis_alpha.py` 的 `CRISIS_BENCHMARKS`（6 宽基），不新增指数集合
- 判定 caveat 文案恒显示："结论供参考，非自动判定：仅当超额持续收敛 + 风险指标同步恶化 + 交易结构退化同时成立才判为疑似衰减；若总收益随风格指数回落而超额稳定，则为风格钟摆（市场温度周期）。"
- 不提交 git（用户未授权）

---

### Task 1: 后端模型 + 注册 + Alembic 迁移

**Files:**
- Create: `main/core/models/backtest_persistence.py`
- Modify: `main/core/models/__init__.py`
- Create: `main/alembic/versions/<generated>_add_backtest_persistence.py`
- Test: 迁移后 `sqlite3` 验证表存在

**Interfaces:**
- Consumes: `core/models/base.py:Base`
- Produces: `BacktestPersistenceAnalysis`（供 Task 3 服务层使用）

- [ ] **Step 1: 创建模型**

`main/core/models/backtest_persistence.py`：
```python
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestPersistenceAnalysis(Base):
    """持续性检验（分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆）。

    与 ``backtest_crisis_alphas`` 同模式：每回测一行，唯一约束。payload_json 为
    persistence_alpha.compute_persistence 结果；失败写 status=failed + error。
    """
    __tablename__ = "backtest_persistence_analyses"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(
        Integer,
        ForeignKey("backtest_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(String(12), nullable=False)
    error = Column(Text, nullable=True)
    skipped_benchmarks_json = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("backtest_record_id", name="uq_backtest_persistence_record"),
    )
```

- [ ] **Step 2: 注册模型**

`main/core/models/__init__.py` 在 `from .backtest_robustness import BacktestRobustnessAnalysis` 之后加：
```python
from .backtest_persistence import BacktestPersistenceAnalysis
```
并在 `__all__` 列表中追加 `"BacktestPersistenceAnalysis"`。

- [ ] **Step 3: 生成并应用迁移**

Run: `cd main && .\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "add backtest_persistence_analyses"`
Expected: 生成 `alembic/versions/<hash>_add_backtest_persistence_analyses.py`，`down_revision` 为当前 head `c0d1e2f3a4b5`，`upgrade()` 仅含 `op.create_table("backtest_persistence_analyses", ...)`。

Run: `cd main && .\.venv\Scripts\python.exe -m alembic upgrade head`

- [ ] **Step 4: 验证**

Run: `cd main && .\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/dev.db'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_persistence_analyses'\")])"`
Expected: `['backtest_persistence_analyses']`

---

### Task 2: 纯计算引擎 `persistence_alpha.py`

**Files:**
- Create: `main/core/engine/persistence_alpha.py`
- Test: `main/core/tests/unit/test_persistence_alpha.py`

**Interfaces:**
- Consumes: `CRISIS_BENCHMARKS`（来自 `core/engine/crisis_alpha.py`，仅用于 `_bench_name`）
- Produces:
  - `compute_persistence(nav_series, trades, benchmark_closes, benchmark_code, benchmark_name) -> dict`（`years[]`/`rolling`/`benchmarks`/`verdict`，失败时 `{"status":"failed","error":...}`）
  - `decide_verdict(years, benchmark_code) -> dict`（可单测）

- [ ] **Step 1: 写失败测试**

`main/core/tests/unit/test_persistence_alpha.py`：
```python
import datetime
from decimal import Decimal
from core.engine.persistence_alpha import compute_persistence, decide_verdict


def _mk_year(year, factors, base=Decimal("1.0")):
    """factors 为逐交易日乘子列表 → {date: Decimal} NAV（跳过周末）。"""
    out = {}
    cur = base
    d = datetime.date(year, 1, 1)
    i = 0
    while i < len(factors):
        if d.weekday() < 5:
            cur = cur * Decimal(str(factors[i]))
            out[d] = cur
            i += 1
        d += datetime.timedelta(days=1)
    return out


def _mk_closes(year, growth):
    """单一指数当年日收盘：从 1 线性增长到 1+growth。"""
    return _mk_year(year, [1.0 + growth / 240] * 240)


def _full_years():
    """2024、2025 两个满年：2024 强势（+60%，低波动），2025 走弱（+30%，高波动、大回撤）。"""
    nav = dict(_mk_year(2024, [1.0015] * 240))
    nav.update(_mk_year(2025, [1.0010] * 200 + [0.9900] * 10 + [1.0015] * 30))
    return nav


def test_decide_insufficient():
    years = [{"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.1,
              "sharpe": 2.0, "calmar": 6.0, "win_rate": 0.6, "profit_factor": 1.8,
              "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.3},
              "bench_returns": {}}]
    assert decide_verdict(years, "000300.SH")["level"] == "insufficient"


def test_decide_alpha_decay():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.08,
         "sharpe": 2.5, "calmar": 7.0, "win_rate": 0.6, "profit_factor": 1.9,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.30}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.20,
         "sharpe": 1.0, "calmar": 1.5, "win_rate": 0.45, "profit_factor": 1.2,
         "turnover": 3.5, "cost_ratio": 0.002, "excess": {"000300.SH": 0.05}, "bench_returns": {}},
    ]
    v = decide_verdict(years, "000300.SH")
    assert v["level"] == "alpha_decay"
    assert "超额" in v["reasons"][0]


def test_decide_style_pendulum():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.12,
         "sharpe": 2.0, "calmar": 5.0, "win_rate": 0.55, "profit_factor": 1.7,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.20}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.13,
         "sharpe": 2.0, "calmar": 4.9, "win_rate": 0.55, "profit_factor": 1.7,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.22}, "bench_returns": {}},
    ]
    assert decide_verdict(years, "000300.SH")["level"] == "style_pendulum"


def test_decide_stable():
    years = [
        {"year": 2024, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.10,
         "sharpe": 1.8, "calmar": 3.0, "win_rate": 0.5, "profit_factor": 1.5,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.05}, "bench_returns": {}},
        {"year": 2025, "is_partial": False, "nav_return": 0.25, "max_drawdown": 0.10,
         "sharpe": 1.9, "calmar": 2.5, "win_rate": 0.52, "profit_factor": 1.6,
         "turnover": 2.0, "cost_ratio": 0.001, "excess": {"000300.SH": 0.06}, "bench_returns": {}},
    ]
    assert decide_verdict(years, "000300.SH")["level"] == "stable"


def test_partial_year_marked_and_excluded():
    nav = dict(_mk_year(2024, [1.002] * 200))          # 满年
    nav.update(_mk_year(2026, [1.001] * 30))           # 部分年（<60 日）
    closes = {"000300.SH": _mk_closes(2024, 0.2)}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    years = out["years"]
    assert any(y["year"] == 2024 and not y["is_partial"] for y in years)
    assert any(y["year"] == 2026 and y["is_partial"] for y in years)
    assert out["verdict"]["level"] == "insufficient"   # 仅 1 个满年


def test_excess_and_benchmark_returns():
    nav = dict(_mk_year(2024, [1.0025] * 240))         # +~81%
    closes = {"000300.SH": _mk_closes(2024, 0.3)}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    y = out["years"][0]
    assert abs(y["bench_returns"]["000300.SH"] - 0.3) < 0.01
    assert abs((y["nav_return"] or 0) - 0.3 - y["excess"]["000300.SH"]) < 0.01
    assert out["benchmarks"][0]["is_main"] is True


def test_rolling_annualized_and_nulls():
    nav = dict(_mk_year(2024, [1.001] * 240))
    nav.update(_mk_year(2025, [1.001] * 240))
    closes = {"000300.SH": dict(_mk_year(2024, [1.0005] * 240))}
    out = compute_persistence(nav, [], closes, "000300.SH", "沪深300")
    r = out["rolling"]
    assert r["dates"][0] and r["strat_12m"][0] is None      # 首个窗口历史不足
    assert any(v is not None for v in r["strat_12m"])       # 有足够历史后出现值
    assert len(r["dates"]) == len(r["strat_6m"]) == len(r["strat_12m"]) == len(r["bench_12m"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/unit/test_persistence_alpha.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'core.engine.persistence_alpha'`）

- [ ] **Step 3: 实现引擎**

`main/core/engine/persistence_alpha.py`：
```python
"""持续性检验：分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆。"""
import bisect
import math
import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from core.engine.crisis_alpha import CRISIS_BENCHMARKS

FULL_YEAR_MIN_DAYS = 60
ROLLING_6M_DAYS = 183
ROLLING_12M_DAYS = 365
TRADING_DAYS_YEAR = 252
MIN_SHARPE_OBS = 20

_CAVEAT = ("结论供参考，非自动判定：仅当超额持续收敛 + 风险指标同步恶化 + 交易结构退化"
           "同时成立才判为疑似衰减；若总收益随风格指数回落而超额稳定，则为风格钟摆（市场温度周期）。")


def _f(v: Decimal) -> float:
    return float(v)


def _ratio(a: Decimal, b: Decimal) -> Optional[float]:
    if b <= 0:
        return None
    return _f(a / b - 1)


def _max_drawdown(vals: List[Decimal]) -> float:
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        elif peak > 0:
            mdd = max(mdd, _f((peak - v) / peak))
    return round(mdd, 4)


def _sharpe(vals: List[Decimal]) -> Optional[float]:
    if len(vals) < MIN_SHARPE_OBS + 1:
        return None
    rets = []
    for i in range(1, len(vals)):
        prev = _f(vals[i - 1])
        if prev > 0:
            rets.append(_f(vals[i]) / prev - 1)
    if len(rets) < MIN_SHARPE_OBS:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    if std <= 0:
        return None
    return round(mean / std * math.sqrt(TRADING_DAYS_YEAR), 4)


def _series_year_return(closes: Dict[datetime.date, Decimal], year: int) -> Optional[float]:
    dts = sorted(d for d in closes if d.year == year)
    if len(dts) < 2:
        return None
    return _ratio(closes[dts[-1]], closes[dts[0]])


def _fifo_lots(trades: List[dict]) -> List[Tuple[float, int]]:
    """FIFO 逐笔 P&L（复用 Evaluator 口径：净 P&L 扣双边费用），按 SELL 日期归年。"""
    fifo: dict = {}
    lots: List[Tuple[float, int]] = []
    for tr in sorted(trades, key=lambda t: t["bar_time"]):
        key = (tr["strategy_id"], tr["stock_code"])
        if tr["trade_type"] == "BUY":
            cost_ps = _f((tr["commission"] + tr["stamp_duty"]) / max(tr["quantity"], 1))
            fifo.setdefault(key, []).append([_f(tr["price"]), int(tr["quantity"]), cost_ps])
        elif tr["trade_type"] == "SELL":
            sell_cost_ps = _f((tr["commission"] + tr["stamp_duty"]) / max(tr["quantity"], 1))
            remaining = int(tr["quantity"])
            queue = fifo.get(key, [])
            while remaining > 0 and queue:
                lot = queue[0]
                taken = min(remaining, lot[1])
                pnl = (_f(tr["price"]) - lot[0] - lot[2] - sell_cost_ps) * taken
                lots.append((pnl, tr["bar_time"].year))
                remaining -= taken
                if taken >= lot[1]:
                    queue.pop(0)
                else:
                    lot[1] -= taken
    return lots


def _year_turnover_cost(trades: List[dict], year: int, nav_series) -> Tuple[Optional[float], Optional[float]]:
    yr = [t for t in trades if t["bar_time"].year == year]
    if not yr:
        return None, None
    total_amount = sum(_f(t["amount"]) for t in yr)
    total_cost = sum(_f(t["commission"] + t["stamp_duty"]) for t in yr)
    dts = sorted(d for d in nav_series if d.year == year)
    if not dts:
        return None, None
    avg_nav = sum(_f(nav_series[d]) for d in dts) / len(dts)
    turnover = round(total_amount / avg_nav, 4) if avg_nav else None
    cost_ratio = round(total_cost / total_amount, 6) if total_amount else None
    return turnover, cost_ratio


def _prev_key(sorted_keys, d):
    i = bisect.bisect_right(sorted_keys, d) - 1
    return sorted_keys[i] if i >= 0 else None


def _rolling_annualized(nav, window_days):
    dates = sorted(nav)
    out_dates, out_vals = [], []
    for d in dates:
        s = _prev_key(dates, d - datetime.timedelta(days=window_days))
        if s is None or nav[s] <= 0 or nav[d] <= 0:
            out_dates.append(d.isoformat())
            out_vals.append(None)
            continue
        span = (d - s).days
        ann = (_f(nav[d]) / _f(nav[s])) ** (365.0 / span) - 1
        out_dates.append(d.isoformat())
        out_vals.append(round(ann, 4))
    return out_dates, out_vals


def _rolling_against(anchor_dates, closes, window_days):
    cs = sorted(closes)
    out = []
    for d in anchor_dates:
        s = _prev_key(cs, d - datetime.timedelta(days=window_days))
        cur = _prev_key(cs, d)
        if s is None or cur is None or s >= cur or closes[s] <= 0 or closes[cur] <= 0:
            out.append(None)
            continue
        span = (d - s).days
        ann = (_f(closes[cur]) / _f(closes[s])) ** (365.0 / span) - 1
        out.append(round(ann, 4))
    return out


def _bench_name(code: str, main_name: str, main_code: str) -> str:
    if code == main_code:
        return main_name
    for spec in CRISIS_BENCHMARKS:
        if spec["code"] == code:
            return spec["name"]
    return code


def decide_verdict(years: List[dict], benchmark_code: str) -> dict:
    full = sorted((y for y in years if not y["is_partial"]), key=lambda y: y["year"])
    n = len(full)
    if n < 2:
        return {
            "level": "insufficient",
            "reasons": ["有效满年不足 2 年，暂不判定阿尔法衰减/风格钟摆（仅展示分年度数据）"],
            "summary": "样本不足，仅展示分年度数据",
            "caveat": _CAVEAT,
        }
    last, prev = full[-1], full[-2]
    ex_now = last["excess"].get(benchmark_code)
    ex_prev = prev["excess"].get(benchmark_code)
    conv = (ex_now is not None and ex_prev is not None and ex_now < ex_prev)
    s_now, s_prev = last["sharpe"], prev["sharpe"]
    c_now, c_prev = last["calmar"], prev["calmar"]
    dd_now, dd_prev = last["max_drawdown"], prev["max_drawdown"]
    risk = (s_now is not None and s_prev is not None and s_now < s_prev
            and c_now is not None and c_prev is not None and c_now < c_prev
            and dd_now is not None and dd_prev is not None and dd_now > dd_prev)
    wr_now, wr_prev = last["win_rate"], prev["win_rate"]
    pf_now, pf_prev = last["profit_factor"], prev["profit_factor"]
    tn_now, tn_prev = last["turnover"], prev["turnover"]
    cs_now, cs_prev = last["cost_ratio"], prev["cost_ratio"]
    struct = (((wr_now is not None and wr_prev is not None and wr_now < wr_prev)
               or (pf_now is not None and pf_prev is not None and pf_now < pf_prev))
              and ((tn_now is not None and tn_prev is not None and tn_now > tn_prev)
                   or (cs_now is not None and cs_prev is not None and cs_now > cs_prev)))
    total_down = last["nav_return"] < prev["nav_return"]
    reasons = [
        f"超额(vs {benchmark_code}) 近两年{'收敛' if conv else '未收敛'}",
        f"风险指标（夏普/卡玛/回撤）近两年{'恶化' if risk else '平稳'}",
        f"交易结构（胜率/盈亏比/换手/成本）近两年{'退化' if struct else '平稳'}",
    ]
    if conv and risk and struct:
        level = "alpha_decay"
        summary = "疑似阿尔法衰减：超额收敛 + 风险恶化 + 结构退化同时成立，需进一步排查容量拥挤/因子衰减"
    elif total_down and not conv and not risk:
        level = "style_pendulum"
        summary = "风格钟摆（市场温度周期）：总收益随风格指数回落，但超额稳定、风险平稳，非策略能力衰减"
    else:
        level = "stable"
        summary = "超额与风险平稳（或信号混合）：未见明确的持续性衰减证据"
    return {"level": level, "reasons": reasons, "summary": summary, "caveat": _CAVEAT}


def compute_persistence(
    nav_series: Dict[datetime.date, Decimal],
    trades: List[dict],
    benchmark_closes: Dict[str, Dict[datetime.date, Decimal]],
    benchmark_code: str,
    benchmark_name: str,
) -> dict:
    dates = sorted(nav_series)
    if not dates:
        return {"status": "failed", "error": "组合日净值为空，无法计算持续性检验"}
    by_year: Dict[int, List[datetime.date]] = {}
    for d in dates:
        by_year.setdefault(d.year, []).append(d)
    all_codes = [benchmark_code] + sorted(c for c in benchmark_closes if c != benchmark_code)
    lots = _fifo_lots(trades)
    years = []
    for y in sorted(by_year):
        dts = by_year[y]
        full = len(dts) >= FULL_YEAR_MIN_DAYS
        vals = [nav_series[d] for d in dts]
        nav_return = _ratio(vals[-1], vals[0])
        mdd = _max_drawdown(vals)
        sharpe = _sharpe(vals)
        calmar = round(nav_return / mdd, 4) if mdd > 0 and nav_return is not None else None
        yr_lots = [pnl for pnl, ly in lots if ly == y]
        win_rate = profit_factor = None
        if yr_lots:
            winners = sum(1 for pnl in yr_lots if pnl > 0)
            win_rate = round(winners / len(yr_lots), 4)
            gross_profit = sum(p for p in yr_lots if p > 0)
            gross_loss = abs(sum(p for p in yr_lots if p < 0))
            if gross_loss > 0:
                profit_factor = round(gross_profit / gross_loss, 4)
        turnover, cost_ratio = _year_turnover_cost(trades, y, nav_series)
        excess, bench_returns = {}, {}
        for code in all_codes:
            closes = benchmark_closes.get(code)
            r = _series_year_return(closes, y) if closes else None
            if r is not None:
                bench_returns[code] = r
                if nav_return is not None:
                    excess[code] = round(nav_return - r, 4)
        years.append({
            "year": y, "is_partial": not full,
            "nav_return": nav_return, "max_drawdown": mdd,
            "sharpe": sharpe, "calmar": calmar,
            "win_rate": win_rate, "profit_factor": profit_factor,
            "turnover": turnover, "cost_ratio": cost_ratio,
            "excess": excess, "bench_returns": bench_returns,
        })
    d6, v6 = _rolling_annualized(nav_series, ROLLING_6M_DAYS)
    _d12, v12 = _rolling_annualized(nav_series, ROLLING_12M_DAYS)
    bench_closes_main = benchmark_closes.get(benchmark_code)
    b12 = _rolling_against(d6, bench_closes_main, ROLLING_12M_DAYS) if bench_closes_main else [None] * len(d6)
    rolling = {"dates": d6, "strat_6m": v6, "strat_12m": v12, "bench_12m": b12}
    verdict = decide_verdict(years, benchmark_code)
    benchmarks = [{
        "code": c, "name": _bench_name(c, benchmark_name, benchmark_code),
        "is_main": c == benchmark_code, "available": c in benchmark_closes,
    } for c in all_codes]
    return {
        "status": "completed", "error": None,
        "years": years, "rolling": rolling,
        "benchmarks": benchmarks, "verdict": verdict,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/unit/test_persistence_alpha.py -q`
Expected: 全部 PASS

---

### Task 3: 服务层 `recompute_persistence` + 详情接线

**Files:**
- Modify: `main/core/services/backtest_service.py`
- Test: `main/core/tests/integration/test_backtest_api.py`（追加）

**Interfaces:**
- Consumes: `BacktestPersistenceAnalysis`（Task 1）、`compute_persistence`（Task 2）、`CRISIS_BENCHMARKS`、`build_index_close_series`、`resolve_crisis_benchmark`
- Produces: `recompute_persistence(db, record_id) -> Optional[dict]`；`get_record_detail` 返回新增 `persistence` 字段

- [ ] **Step 1: 写失败集成测试**

`main/core/tests/integration/test_backtest_api.py` 末尾追加：
```python
def test_persistence_recompute_ok(client, monkeypatch):
    """持续性检验重算 → 200 + 落库 + 详情返回 persistence。"""
    c, Session = client
    db = Session()
    ps_id, _, _ = _seed(db)
    db.close()

    _mock_data(monkeypatch)
    resp = c.post("/api/backtest", json={
        "portfolio_strategy_id": ps_id, "name": "persist_bt",
        "start_date": "2026-07-01", "end_date": "2026-07-31",
    })
    assert resp.json()["code"] == 0
    rid = resp.json()["data"]["record_id"]

    r = c.post(f"/api/backtest/records/{rid}/persistence/recompute")
    assert r.status_code == 200
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert data["status"] in ("completed", "insufficient", "failed")

    detail = c.get(f"/api/backtest/records/{rid}").json()["data"]
    assert "persistence" in detail


def test_persistence_recompute_404(client):
    c, Session = client
    assert c.post("/api/backtest/records/99999/persistence/recompute").json()["code"] == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/integration/test_backtest_api.py::test_persistence_recompute_ok -q`
Expected: FAIL（路由不存在 → 404 或 `code != 0`）

- [ ] **Step 3: 实现服务函数**

`main/core/services/backtest_service.py` 追加（放在 `recompute_robustness` 之后）：
```python
def _upsert_persistence(
    db, record_id, status, error=None, skipped=None, payload=None,
):
    row = (
        db.query(BacktestPersistenceAnalysis)
        .filter_by(backtest_record_id=record_id).one_or_none()
    )
    if row is None:
        row = BacktestPersistenceAnalysis(backtest_record_id=record_id, status=status)
        db.add(row)
    row.status = status
    row.error = error
    row.skipped_benchmarks_json = json.dumps(skipped or [], ensure_ascii=False) if skipped else None
    row.payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
    db.commit()
    db.refresh(row)
    return row


def _serialize_persistence(row) -> dict:
    return {
        "status": row.status,
        "error": row.error,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        "skipped_benchmarks": (
            json.loads(row.skipped_benchmarks_json) if row.skipped_benchmarks_json else []
        ),
        "data": json.loads(row.payload_json) if row.payload_json else None,
    }


def recompute_persistence(db: Session, record_id: int) -> Optional[dict]:
    """离线重算持续性检验（详情页手动触发）。记录不存在返回 None（路由 404）。"""
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return None
    snaps = (
        db.query(BacktestDailySnapshot)
        .filter_by(backtest_record_id=record_id, target_type="portfolio")
        .order_by(BacktestDailySnapshot.snap_date).all()
    )
    if not snaps:
        return {"status": "failed", "error": "该回测无组合日快照，无法计算",
                "computed_at": None, "skipped_benchmarks": []}
    ps = db.get(PortfolioStrategy, rec.portfolio_strategy_id)
    if ps is None:
        return {"status": "failed", "error": "组合策略已删除，无法确定基准指数",
                "computed_at": None, "skipped_benchmarks": []}
    resolution = resolve_crisis_benchmark(rec, None, ps)
    benchmark_code = resolution["benchmark_code"]
    benchmark_name = resolution["benchmark_name"]
    codes = [benchmark_code] + [spec["code"] for spec in CRISIS_BENCHMARKS]
    closes = build_index_close_series(codes, rec.start_date, rec.end_date)
    skipped = [spec["name"] for spec in CRISIS_BENCHMARKS if spec["code"] not in closes]
    main_closes = closes.get(benchmark_code)
    if not main_closes:
        row = _upsert_persistence(
            db, record_id, "failed",
            error="主基准指数无数据（TQ 取数失败或代码不被支持）", skipped=skipped,
        )
        return _serialize_persistence(row)
    nav_series = {s.snap_date: s.total_value for s in snaps}
    trades = [{
        "bar_time": t.bar_time.date(),
        "trade_type": t.trade_type,
        "amount": t.amount, "commission": t.commission, "stamp_duty": t.stamp_duty,
        "price": t.price, "quantity": t.quantity,
        "strategy_id": t.strategy_id, "stock_code": t.stock_code,
    } for t in db.query(BacktestTrade).filter_by(backtest_record_id=record_id).all()]
    payload = compute_persistence(
        nav_series, trades,
        {c: closes[c] for c in codes if c in closes},
        benchmark_code, benchmark_name,
    )
    if payload.get("status") == "failed":
        row = _upsert_persistence(db, record_id, "failed", error=payload.get("error"), skipped=skipped)
    else:
        row = _upsert_persistence(db, record_id, "completed", skipped=skipped, payload=payload)
    return _serialize_persistence(row)
```

- [ ] **Step 4: 详情接口返回 `persistence`**

`backtest_service.py` 的 `get_record_detail`，在 robustness 序列化（`2444-2456`）之后加：
```python
    pp = (
        db.query(BacktestPersistenceAnalysis)
        .filter_by(backtest_record_id=record_id).one_or_none()
    )
    persistence = None
    if pp is not None:
        persistence = _serialize_persistence(pp)
```
并在 return dict 的 `"robustness": robustness,` 后加 `"persistence": persistence,`。

文件顶部 `from core.engine.persistence_alpha import compute_persistence`（与既有 `from core.engine.backtest_robustness import ...` 同处）。

- [ ] **Step 5: 跑集成测试确认通过**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/integration/test_backtest_api.py -q`
Expected: 新增 2 个测试 PASS

---

### Task 4: API 路由

**Files:**
- Modify: `main/core/api/backtest.py`
- Test: 复用 Task 3 集成测试（路由即测试入口）

**Interfaces:**
- Consumes: `recompute_persistence as _svc_recompute_persistence`（Task 3）
- Produces: `POST /api/backtest/records/{record_id}/persistence/recompute`

- [ ] **Step 1: 追加路由**

`main/core/api/backtest.py` 的 re-export import 块（`from core.services.backtest_service import (...)`）中加：
```python
    recompute_persistence as _svc_recompute_persistence,
```
在 `recompute_record_robustness`（`218-222`）之后加：
```python
@router.post("/records/{record_id}/persistence/recompute")
def recompute_record_persistence(record_id: int, db: Session = Depends(get_db)):
    """离线重算持续性检验（分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆）。"""
    result = _svc_recompute_persistence(db, record_id)
    if result is None:
        return err(404, "回测记录不存在")
    return ok(result)
```

- [ ] **Step 2: 跑集成测试确认通过**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/integration/test_backtest_api.py::test_persistence_recompute_ok core/tests/integration/test_backtest_api.py::test_persistence_recompute_404 -q`
Expected: 2 PASS

---

### Task 5: 前端 API 类型 + 函数

**Files:**
- Modify: `web/src/api/index.ts`

**Interfaces:**
- Consumes: `ApiResponse<T>` envelope
- Produces: `PersistenceAnalysis` 类型、`recomputePersistenceAnalysis(id)`、`BacktestDetailItem.persistence`

- [ ] **Step 1: 追加类型与函数**

`web/src/api/index.ts` 在 `BacktestRobustnessItem` 之后加：
```ts
export interface PersistenceYearRow {
  year: number
  is_partial: boolean
  nav_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  calmar: number | null
  win_rate: number | null
  profit_factor: number | null
  turnover: number | null
  cost_ratio: number | null
  excess: Record<string, number | null>
  bench_returns: Record<string, number | null>
}
export interface PersistenceRolling {
  dates: string[]
  strat_6m: (number | null)[]
  strat_12m: (number | null)[]
  bench_12m: (number | null)[]
}
export interface PersistenceBenchmarkMeta { code: string; name: string; is_main: boolean; available: boolean }
export interface PersistenceVerdict {
  level: 'alpha_decay' | 'style_pendulum' | 'stable' | 'insufficient'
  reasons: string[]
  summary: string
  caveat: string
}
export interface PersistenceAnalysis {
  status: string
  error?: string | null
  computed_at?: string | null
  skipped_benchmarks?: string[]
  data?: {
    status: string
    error: string | null
    years: PersistenceYearRow[]
    rolling: PersistenceRolling
    benchmarks: PersistenceBenchmarkMeta[]
    verdict: PersistenceVerdict
  } | null
}
export async function recomputePersistenceAnalysis(id: number) {
  const res = await api.post<ApiResponse<PersistenceAnalysis>>(`/backtest/records/${id}/persistence/recompute`)
  return res.data.data
}
```
`BacktestDetailItem`（`452-466`）的 `robustness: BacktestRobustnessItem | null` 后加：
```ts
  persistence: PersistenceAnalysis | null  // 持续性检验（阿尔法衰减 vs 风格钟摆）
```

- [ ] **Step 2: 类型检查**

Run: `cd web && npm run build`
Expected: 通过（vue-tsc 无新错误）

---

### Task 6: 前端详情报告模块

**Files:**
- Modify: `web/src/views/Backtest.vue`
- Test: `web/src/__tests__/Backtest.test.ts`（追加）

**Interfaces:**
- Consumes: `recomputePersistenceAnalysis`、`PersistenceAnalysis`（Task 5）、既有 `num/pct1/pctSigned/valueClass`、`cssVar`、`getBacktestDetail`
- Produces: 详情报告「持续性检验」折叠区块 + 判定横幅 + 两张表 + ECharts 滚动曲线

- [ ] **Step 1: 写失败前端测试**

`web/src/__tests__/Backtest.test.ts` 追加（复用既有 `mockDetail`/`mountDetail` 模式，该文件 `Backtest.vue — 稳健性检验 section` describe 附近）：
```ts
it('持续性检验区块渲染判定横幅与分年度表', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    persistence: {
      status: 'completed',
      data: {
        status: 'completed', error: null,
        years: [
          { year: 2024, is_partial: false, nav_return: 0.6, max_drawdown: 0.08, sharpe: 2.5, calmar: 7.0, win_rate: 0.6, profit_factor: 1.9, turnover: 2.0, cost_ratio: 0.001, excess: { '000300.SH': 0.3 }, bench_returns: {} },
          { year: 2025, is_partial: false, nav_return: 0.3, max_drawdown: 0.2, sharpe: 1.0, calmar: 1.5, win_rate: 0.45, profit_factor: 1.2, turnover: 3.5, cost_ratio: 0.002, excess: { '000300.SH': 0.05 }, bench_returns: {} },
        ],
        rolling: { dates: ['2024-06-01'], strat_6m: [0.12], strat_12m: [0.08], bench_12m: [0.06] },
        benchmarks: [{ code: '000300.SH', name: '沪深300', is_main: true, available: true }],
        verdict: { level: 'alpha_decay', reasons: ['超额收敛', '风险恶化', '结构退化'], summary: '疑似阿尔法衰减', caveat: '结论供参考' },
      },
    },
  })
  const w = mount(Backtest)
  await flushPromises()
  const viewBtn = w.findAll('button').filter(b => b.text().includes('查看'))[0]
  await viewBtn.trigger('click')
  await flushPromises()
  const pHeader = w.findAll('.section-title').find(h => h.text().includes('持续性检验'))
  await pHeader!.trigger('click')
  await flushPromises()
  expect(w.text()).toContain('疑似阿尔法衰减')
  expect(w.text()).toContain('分年度超额收益')
  expect(w.text()).toContain('沪深300')
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts -t "持续性检验"`
Expected: FAIL（无该区块）

- [ ] **Step 3: 实现 script 逻辑**

`web/src/views/Backtest.vue`：
- import 块加 `recomputePersistenceAnalysis,`（从 `'../api'`）
- script 状态区（`rbRecompute*` 附近）加：
```ts
const persistence = computed(() => detail.value?.persistence ?? null)
const pStatus = computed(() => persistence.value?.status ?? null)
const pData = computed(() => persistence.value?.data ?? null)
const pCollapsed = ref(true)
const pRecomputing = ref(false)
const pRecomputeMsg = ref('')
const pRecomputeWarn = ref(false)
const pRecomputeMsgClass = computed(() => (pRecomputeWarn.value ? 'ca-msg ca-warn' : 'ca-msg'))
const persistenceSectionEl = ref<HTMLDivElement | null>(null)
const pRollingEl = ref<HTMLDivElement | null>(null)
let pRollingChart: echarts.ECharts | null = null

function togglePSection() { pCollapsed.value = !pCollapsed.value }

function verdictLabel(level: string): string {
  return { alpha_decay: '疑似阿尔法衰减', style_pendulum: '风格钟摆（市场温度周期）', stable: '稳定', insufficient: '样本不足' }[level] || level
}

async function recomputePersistence() {
  const id = currentRecord.value?.id
  if (!id || pRecomputing.value) return
  pRecomputing.value = true
  pRecomputeMsg.value = ''
  pRecomputeWarn.value = false
  try {
    await recomputePersistenceAnalysis(id)
    detail.value = await getBacktestDetail(id)
    pRecomputeMsg.value = '计算完成'
  } catch (e) {
    pRecomputeWarn.value = true
    pRecomputeMsg.value = errMsg(e)
  } finally {
    pRecomputing.value = false
    nextTick(renderPRolling)
  }
}

function renderPRolling() {
  const el = pRollingEl.value
  const d = pData.value
  if (!el || !d?.rolling?.dates?.length) return
  pRollingChart?.dispose()
  pRollingChart = echarts.init(el)
  pRollingChart.setOption({
    tooltip: { trigger: 'axis', valueFormatter: (v: any) => (v == null ? '-' : (v * 100).toFixed(2) + '%') },
    legend: { data: ['策略 6M', '策略 12M', '基准 12M'] },
    grid: { left: 56, right: 24, bottom: 36, top: 40, containLabel: true },
    xAxis: { type: 'category', data: d.rolling.dates, axisLabel: { color: cssVar('--ink-soft') }, axisLine: { lineStyle: { color: cssVar('--line') } } },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => (v * 100).toFixed(0) + '%', color: cssVar('--ink-soft') }, splitLine: { lineStyle: { color: cssVar('--line') } } },
    series: [
      { name: '策略 6M', type: 'line', data: d.rolling.strat_6m, symbol: 'none', lineStyle: { color: cssVar('--blue'), width: 1.2 } },
      { name: '策略 12M', type: 'line', data: d.rolling.strat_12m, symbol: 'none', lineStyle: { color: cssVar('--red'), width: 2 } },
      { name: '基准 12M', type: 'line', data: d.rolling.bench_12m, symbol: 'none', lineStyle: { color: cssVar('--gold'), width: 1.4, type: 'dashed' } },
    ],
  })
}
```

- [ ] **Step 4: 实现模板区块**

`web/src/views/Backtest.vue` 详情报告内、「稳健性检验」区块（`4078` 行的 `</div>`）之后插入：
```html
    <!-- 持续性检验（阿尔法衰减 vs 风格钟摆） -->
    <div ref="persistenceSectionEl" class="ca-section rb-section">
      <div class="section-title ca-header" @click="togglePSection">
        <span>🔬 持续性检验</span>
        <span class="ca-caret" :class="{ open: !pCollapsed }">▶</span>
      </div>
      <template v-if="!pCollapsed">
        <div v-if="pStatus === null" class="card" style="padding:12px">
          <p style="color:var(--ink-soft);margin:0 0 8px">
            本次回测尚未计算持续性检验（分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆）。
          </p>
          <button class="btn btn-sm btn-primary" :disabled="pRecomputing" @click="recomputePersistence">
            {{ pRecomputing ? '计算中…' : '重新计算' }}
          </button>
          <span v-if="pRecomputeMsg" :class="pRecomputeMsgClass">{{ pRecomputeMsg }}</span>
        </div>

        <div v-else-if="pStatus === 'failed'" class="card" style="padding:12px">
          <p style="color:var(--red);margin:0 0 8px">持续性检验失败：{{ persistence?.error || '未知原因' }}</p>
          <button class="btn btn-sm btn-primary" :disabled="pRecomputing" @click="recomputePersistence">
            {{ pRecomputing ? '计算中…' : '重新计算' }}
          </button>
          <span v-if="pRecomputeMsg" :class="pRecomputeMsgClass">{{ pRecomputeMsg }}</span>
        </div>

        <template v-else-if="pData">
          <div class="ca-banner" :class="'p-' + pData.verdict.level">
            <div class="t">判定：{{ verdictLabel(pData.verdict.level) }}</div>
            <div class="d">{{ pData.verdict.summary }}</div>
            <ul v-if="pData.verdict.reasons.length" class="d" style="margin:6px 0 0 18px">
              <li v-for="r in pData.verdict.reasons" :key="r">{{ r }}</li>
            </ul>
            <div class="d" style="margin-top:6px;color:var(--gold)">{{ pData.verdict.caveat }}</div>
          </div>

          <div class="section-title" style="font-size:13px">分年度指标矩阵（* 未满年，仅供参考）</div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>年份</th><th>收益</th><th>最大回撤</th><th>夏普</th><th>卡玛</th>
                <th>胜率</th><th>盈亏比</th><th>换手</th><th>成本占比</th>
              </tr></thead>
              <tbody>
                <tr v-for="y in pData.years" :key="y.year">
                  <td>{{ y.year }}<span v-if="y.is_partial" class="badge badge-gray" style="margin-left:4px">*</span></td>
                  <td :class="valueClass(y.nav_return)">{{ pctSigned(y.nav_return) }}</td>
                  <td>{{ pct1(y.max_drawdown) }}</td>
                  <td>{{ num(y.sharpe, 2) }}</td>
                  <td>{{ num(y.calmar, 2) }}</td>
                  <td>{{ y.win_rate == null ? '-' : pct1(y.win_rate) }}</td>
                  <td>{{ num(y.profit_factor, 2) }}</td>
                  <td>{{ num(y.turnover, 2) }}</td>
                  <td>{{ pct1(y.cost_ratio) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="section-title" style="font-size:13px">分年度超额收益（vs 组合基准 + 宽基）</div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>年份</th>
                <th v-for="b in pData.benchmarks" :key="b.code">
                  {{ b.name }}<span v-if="b.is_main" class="badge badge-blue" style="margin-left:4px">主</span>
                  <span v-if="!b.available" class="badge badge-gray" style="margin-left:4px">无数据</span>
                </th>
              </tr></thead>
              <tbody>
                <tr v-for="y in pData.years" :key="y.year">
                  <td>{{ y.year }}</td>
                  <td v-for="b in pData.benchmarks" :key="b.code"
                      :class="valueClass(y.excess[b.code])">
                    {{ y.excess[b.code] == null ? '-' : pctSigned(y.excess[b.code]) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="section-title" style="font-size:13px">滚动年化收益（策略 6M/12M vs 基准 12M）</div>
          <div class="chart-box"><div ref="pRollingEl" class="ca-chart" style="height:340px"></div></div>
        </template>
      </template>
    </div>
```

- [ ] **Step 5: 判定横幅配色（scoped 样式）**

`web/src/views/Backtest.vue` 的 `<style scoped>` 内，`.ca-banner .ca-warn` 附近加：
```css
.ca-banner.p-alpha_decay { border-left: 4px solid var(--red); }
.ca-banner.p-style_pendulum { border-left: 4px solid var(--gold); }
.ca-banner.p-stable { border-left: 4px solid var(--green); }
.ca-banner.p-insufficient { border-left: 4px solid var(--ink-faint); }
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd web && npx vitest run src/__tests__/Backtest.test.ts`
Expected: 新增测试 PASS，无回归

---

### Task 7: 全量验证 + 构建 + 打包

**Files:** 无新增

- [ ] **Step 1: 后端全量**

Run: `cd main && .\.venv\Scripts\python.exe -m pytest core/tests/ -q`
Expected: 全绿（约 1462 passed）

- [ ] **Step 2: 前端全量 + 构建**

Run: `cd web && npx vitest run`（全绿）再 `npm run build`（EXITCODE=0）

- [ ] **Step 3: 同步打包静态目录**

Run:
```powershell
$dst = 'main\dist\tq-iquant\_internal\web\dist'
Remove-Item "$dst\*" -Recurse -Force
Copy-Item "web\dist\*" $dst -Recurse -Force
```

- [ ] **Step 4: 重打包（可选，用户确认后）**

Run: `cd main && .\.venv\Scripts\python.exe build_package.py`（约 15 分钟，CPU 满载属正常）
