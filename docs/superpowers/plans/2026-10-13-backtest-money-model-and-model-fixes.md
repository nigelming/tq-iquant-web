# 回测资金模型与模型真实性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec 修复全模型审计发现的 W1–W6 六组问题（指标口径、动态仓位、执行现实性、前视、过拟合、风控）。

**Architecture:** 以 TDD 逐任务实现；W1–W4 合并一条 Alembic 迁移并重构 `BacktestEngine` 为按 `(时间, 周期)` 路由；W5/W6 为纯逻辑修复无 schema 变更。

**Tech Stack:** Python 3.13 (FastAPI/SQLAlchemy/Alembic/polars/pytest)、Vue 3 + Vite + Vitest。

## Global Constraints

- **不自动提交**：plan 模板的 commit 步骤一律改为「保持工作区，不 commit」（用户明确要求）。
- 后端测试：`uv run pytest core/tests/ -q`（在 `main/` 目录）；前端：`npx vitest run`（在 `web/`）。
- `shared/` 包须兼容 Python 3.7；`live/` 桥须兼容 3.6。
- schema 变更一律经 Alembic，禁止手改表。
- 1458 全量后端测试 + 258 前端测试必须保持通过；`npm run build` 必须成功。
- 金额 `Decimal`、时区 Asia/Shanghai。

---

## Phase W1 — 指标口径小修

### Task 1.1: Evaluator 产出 `closed_trades`

**Files:**
- Modify: `main/core/engine/evaluator.py:293-363`（`_compute_trade_metrics`）、`:208-239`（result 组装）
- Test: `main/core/tests/unit/engine/test_evaluator.py`

**Interfaces:**
- Produces: `evaluate()` 返回新增键 `closed_trades: int|None`；`_compute_trade_metrics()` 返回 dict 新增 `closed_trades`（= FIFO 平仓批次数 `len(lot_pnls)`）。

- [ ] **Step 1: 写失败测试**

```python
def test_closed_trades_counts_fifo_lots_not_executions():
    # 2 BUY + 1 SELL（部分平仓）→ total_trades=3，closed_trades=1
    trades = _mk_trades(...)  # 复用既有 fixture
    m = Evaluator._compute_trade_metrics(trades)
    assert m["total_trades"] == 3
    assert m["closed_trades"] == 1

def test_closed_trades_none_when_no_trades():
    m = Evaluator._compute_trade_metrics([])
    assert m["total_trades"] == 0
    assert m["closed_trades"] == 0
```

- [ ] **Step 2: 跑测试确认失败** — `uv run pytest core/tests/unit/engine/test_evaluator.py -k closed_trades -q`（KeyError）
- [ ] **Step 3: 实现**：空 `trades` 分支返回 `"closed_trades": 0`；有 trades 分支返回 `"closed_trades": len(lot_pnls)`；`evaluate()` result 增 `"closed_trades": trade_metrics["closed_trades"]`。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 保持工作区（不 commit）**

### Task 1.2: `closed_trades` 落库（模型 + 迁移 + 持久化）

**Files:**
- Modify: `main/core/models/backtest_evaluation.py`（增列）
- Create: `main/alembic/versions/<rev>_add_closed_trades.py`
- Modify: `main/core/services/backtest_service.py:1115-1129`（`_EVALUATION_FIELDS` 增 `closed_trades`）
- Modify: `main/core/api/backtest.py` 的 evaluation 序列化（若显式列字段）
- Test: `main/core/tests/...`（迁移/evaluation 序列化）

**Interfaces:**
- Consumes: Task 1.1 的 `closed_trades` 键。
- Produces: `backtest_evaluations.closed_trades INTEGER NULL`；序列化 payload 含 `closed_trades`（int/None）。

- [ ] **Step 1: 写失败测试**（口算：跑一个含交易的小回测后，`GET /api/backtest/records/{id}` 的 evaluation 含 `closed_trades` 且等于 FIFO 批次数）
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 加模型列 + 写迁移**（`op.add_column("backtest_evaluations", sa.Column("closed_trades", sa.Integer(), nullable=True))`；downgrade 删列）；`_EVALUATION_FIELDS` 增 `"closed_trades"`；序列化处透出。
- [ ] **Step 4: `alembic upgrade head` + 跑测试通过**
- [ ] **Step 5: 保持工作区**

### Task 1.3: evaluation ↔ 快照 可复现不变式测试

**Files:**
- Test: `main/core/tests/integration/test_evaluation_reproducible.py`（新建）

**Interfaces:**
- Consumes: `run_backtest` 落库 + `recompute_evaluation`。

- [ ] **Step 1: 写测试**：用固定小数据集跑一次回测；读取落库 `backtest_evaluations`；用落库 `backtest_daily_snapshots` 调 `recompute_evaluation`；断言 `total_return`/`annual_return`/`max_drawdown` 在 `1e-4` 内一致。
- [ ] **Step 2: 跑测试**：预期当前**失败**（暴露 record-4 类不一致）。
- [ ] **Step 3: 修根因**：确认 `run_backtest` 在**同一事务**写 evaluation 与 snapshots；确认 W4 的「每日一行快照」已消除同日多行漂移。如仍有偏差，修 `Evaluator.evaluate` 起点口径（`daily_values` 首值 vs `snapshots[0]`）。
- [ ] **Step 4: 通过**
- [ ] **Step 5: 保持工作区**

### Task 1.4: 前端口径标签

**Files:**
- Modify: `web/src/views/Backtest.vue`（`total_trades` 列标题 → 「成交笔数」；新增「平仓批次」列绑定 `closed_trades`；胜率 tooltip「按平仓批次」）
- Modify: `web/src/api/index.ts`（`Evaluation` 类型增 `closed_trades?: number | null`）
- Test: `web/src/__tests__/Backtest.test.ts`

- [ ] **Step 1: 写失败测试**（组件渲染含「平仓批次」表头；绑定 `closed_trades`）
- [ ] **Step 2: `npx vitest run` 确认失败**
- [ ] **Step 3: 改视图 + 类型**
- [ ] **Step 4: 通过**
- [ ] **Step 5: 保持工作区**

---

## Phase W2 — 动态仓位 + 现金管理预留

### Task 2.1: 迁移新增组合字段

**Files:**
- Modify: `main/core/models/portfolio_strategy.py`
- Create: `main/alembic/versions/<rev>_portfolio_sizing_cash_enhance.py`

**Interfaces:**
- Produces 列：`position_sizing VARCHAR(10) default 'dynamic'`；`cash_enhance_enabled BOOLEAN default 0`；`cash_enhance_equity_threshold NUMERIC(15,2) NULL`；`cash_enhance_position_ratio_threshold NUMERIC(5,4) NULL`。

- [ ] **Step 1: 写失败测试**（创建组合默认 `position_sizing=="dynamic"`，三个 cash 字段默认 0/None）
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 模型 + 迁移**（加 4 列，server_default；downgrade 删列）
- [ ] **Step 4: upgrade + 通过**
- [ ] **Step 5: 保持工作区**

### Task 2.2: `Portfolio` 动态/固定资金口径 + `_strategy_fund`

**Files:**
- Modify: `main/core/engine/portfolio.py`（`__init__` 增 `position_sizing`；`on_bar` 起始算 `_bar_equity`；`_signal_to_order` 与 `_try_rotation` 用 `_strategy_fund`）
- Test: `main/core/tests/unit/engine/test_portfolio_sizing.py`（新建）

**Interfaces:**
- Produces: `Portfolio(position_sizing: str = "dynamic", ...)`；`Portfolio._strategy_fund(ctx) -> Decimal`。
  - dynamic：`ctx.capital_ratio * self._bar_equity`
  - fixed：`ctx.capital_ratio * self.account.initial_capital`

- [ ] **Step 1: 写失败测试**

```python
def test_dynamic_sizing_scales_with_equity():
    p = _mk_portfolio(position_sizing="dynamic", initial=Decimal("1000000"))
    _open_position(p, price=Decimal("10"), gain_to=Decimal("20"))  # 净值翻倍
    fund = p._strategy_fund(p.strategies[0])
    assert fund == p.strategies[0].capital_ratio * p.total_value(_bar)

def test_fixed_sizing_ignores_equity():
    p = _mk_portfolio(position_sizing="fixed", initial=Decimal("1000000"))
    ...
    assert p._strategy_fund(ctx) == ctx.capital_ratio * Decimal("1000000")
```

- [ ] **Step 2: 确认失败**（`_strategy_fund` 不存在）
- [ ] **Step 3: 实现**：`__init__` 存 `self.position_sizing`（默认 "dynamic"）；`on_bar` 顶部 `self._bar_equity = self.total_value(bar)`；新增 `_strategy_fund`；把 `:332`/`:454`/`:566` 的 `ctx.capital_ratio * self.account.initial_capital` 替换为 `self._strategy_fund(ctx)`。
- [ ] **Step 4: 通过**（含旧测试全绿）
- [ ] **Step 5: 保持工作区**

### Task 2.3: Builder 装配 + params_snapshot 冻结

**Files:**
- Modify: `main/core/engine/portfolio_builder.py`（`assemble_portfolio` 传 `position_sizing=getattr(ps,"position_sizing","dynamic")` 及 cash 字段）
- Modify: `main/core/services/backtest_service.py:386-399`（`PARAMS_SNAPSHOT_FIELDS` 增 `position_sizing`）
- Test: `main/core/tests/unit/engine/test_portfolio_builder.py`、`.../test_backtest_service_snapshot.py`

- [ ] **Step 1: 写失败测试**（builder 产出 Portfolio.position_sizing 等于 ps 值；params_snapshot 含 position_sizing）
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 通过**
- [ ] **Step 5: 保持工作区**

### Task 2.4: 现金管理增强预留触发点

**Files:**
- Modify: `main/core/engine/portfolio.py`（`__init__` 存三字段；新增 `maybe_cash_enhancement(bar, total_value)`）
- Modify: `main/core/engine/backtest_engine.py:224-238`（日终快照后调一次，仅在日期变化时）
- Modify: `main/core/engine/live_engine.py`（日终同点，Task 6 阶段一并接入）
- Test: `main/core/tests/unit/engine/test_portfolio_cash_enhance.py`

**Interfaces:**
- Produces: `Portfolio.maybe_cash_enhancement(bar, total_value) -> None`；命中记 `decision_event(gate="cash_enhance_trigger", layer="portfolio_risk", action="trigger")`，无订单。

- [ ] **Step 1: 写失败测试**

```python
def test_cash_enhance_triggers_decision_event_no_order():
    p = _mk_portfolio(cash_enhance_enabled=True,
                      cash_enhance_equity_threshold=Decimal("1000000"))
    rec = _FakeRecorder(); p.set_recorder(rec)
    p.maybe_cash_enhancement(bar, Decimal("1200000"))
    assert any(e["gate"] == "cash_enhance_trigger" for e in rec.events)

def test_cash_enhance_disabled_no_event(): ...
def test_cash_enhance_once_per_day(): ...  # 同日二次不重复
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（OR 条件；`_cash_enhance_last_date` 去重；阈值 None 不判）
- [ ] **Step 4: 通过**
- [ ] **Step 5: 保持工作区**

### Task 2.5: 前端组合表单

**Files:**
- Modify: `web/src/views/Portfolios.vue`（新增「仓位模型」select + 「现金管理（预留）」组）
- Modify: `web/src/api/index.ts`（`PortfolioItem`/`PortfolioRequest` 增 4 字段）
- Test: `web/src/__tests__/Portfolios.test.ts`

- [ ] **Step 1–5**: 按 W1.4 节奏（测试 → 实现 → 通过 → 保持工作区）。

---

## Phase W3 — 执行现实性

### Task 3.1: 涨跌停默认开（模型 + 迁移 + 现有数据）

**Files:**
- Modify: `main/core/models/portfolio_strategy.py:26`（`default=True, server_default="1"`）
- Create: `main/alembic/versions/<rev>_price_limit_default_on.py`
- Modify: `main/core/api/portfolios.py`（创建默认 True，若硬编码）
- Modify: `main/core/engine/portfolio_builder.py`（读取字段，行为自然跟随）
- Test: 后端创建组合默认断言 + 迁移

- [ ] **Step 1: 写失败测试**（新组合 `price_limit_check_enabled == True`；已有行迁移后为 1）
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（迁移：`ALTER default` + `UPDATE portfolio_strategies SET price_limit_check_enabled=1`；downgrade 反向设 0）
- [ ] **Step 4: upgrade + 通过**
- [ ] **Step 5: 保持工作区**

### Task 3.2: 前端默认勾选

**Files:**
- Modify: `web/src/views/Portfolios.vue`（`price_limit_check_enabled` 默认 true；文案「默认开启」）
- Test: `web/src/__tests__/Portfolios.test.ts`

- [ ] Steps 1–5 同上。

---

## Phase W4 — 前视

### Task 4.1: 删除公式未来函数检查（后端）

**Files:**
- Modify: `main/core/services/formula_service.py`（删 `scan_future_functions` 及关键字表/常量）
- Modify: `main/core/api/formulas.py`（删 `_check_future_functions`、两处调用、`FormulaCreate.strict_future_check`）
- Modify: 受影响的 `main/core/tests/**`（删该功能用例）
- Test: 保留一个「创建公式不因内容含 ZIG 等被拒」的测试

- [x] **Step 1: 写/改测试**（含未来函数关键字的内容可 200 创建）
- [x] **Step 2: 确认当前失败**（因被 400 拒）
- [x] **Step 3: 删除实现**
- [x] **Step 4: 通过**
- [x] **Step 5: 保持工作区**

### Task 4.2: 删除前端 strict_future_check

**Files:**
- Modify: `web/src/views/Formulas.vue`、`web/src/api/index.ts`、`web/src/__tests__/Formulas.test.ts`

- [x] Steps 1–5 同上。

### Task 4.3: `BacktestEngine` 按周期路由（消混合周期前视）

**Files:**
- Modify: `main/core/engine/event.py`（`OrderEvent` 增 `period: Optional[str] = None`）
- Modify: `main/core/engine/portfolio.py`（`_signal_to_order`/`_try_rotation`/`_follow_master_state` 置 `period=bar.period`）
- Modify: `main/core/engine/backtest_engine.py`（`_build_timeline`→`List[Tuple[datetime,str]]`；`_build_price_index` 键 `(t,period)`；`_bar_open_prices`/`_build_bar` 带 period；成交仅匹配 `order.period==period`；`enter_day` 每日一次；`_COARSE_PERIODS` 逻辑删除）
- Test: `main/core/tests/unit/engine/test_backtest_engine_period_routing.py`（新建）

**Interfaces:**
- Produces: `on_bar(bar, signal_cache=..., period=P)`；`BarEvent.period == P`。

- [x] **Step 1: 写失败测试**

```python
def test_daily_bar_does_not_drive_minute_strategy_same_day():
    # 组合含 1d + 5m 策略；构造 D 00:00 日线 bar 与 D 09:30 5m bar。
    # 断言：5m 策略在 09:30 之前不因 D 日线 close 触发任何订单。
    result = engine.run(portfolio, klines=_mixed_klines())
    assert not [t for t in result["trades"] if t.strategy_id == minute_sid
                and t.trade_time.date() == D and t.trade_time.time() < time(9, 30)]

def test_single_period_results_unchanged():
    # 纯 1d 组合：period 路由重构前后 trades/evaluations 完全一致（golden）
```

- [x] **Step 2: 确认失败**（复现 09:30 前视）
- [x] **Step 3: 重构实现**（见 spec W4.2；注意 6b 前收表按 `(t,period)` 遍历；`_strategy_snapshots` 不变）
- [x] **Step 4: 通过**（含全部既有引擎测试）
- [x] **Step 5: 保持工作区**

### Task 4.4: 每日一行快照

**Files:**
- Modify: `main/core/engine/backtest_engine.py`（date 变化时 flush 上一日末值；循环后 flush 最后一日）
- Modify: 相关快照计数测试
- Test: `test_backtest_engine.py` 增「两次同日后仅一行」

- [x] **Step 1: 写失败测试**（混合周期输入 → `len(portfolio snapshots) == 交易日数`）
- [x] **Step 2: 确认失败**
- [x] **Step 3: 实现**（`pending_snap` + `_last_snap_date`）
- [x] **Step 4: 通过**
- [x] **Step 5: 保持工作区**

### Task 4.5: regime 用前一交易日收盘

**Files:**
- Modify: `main/core/engine/backtest_engine.py:78-82`
- Test: `main/core/tests/unit/engine/test_regime_tracker_prev_close.py`

- [x] **Step 1–5**: 测试 → 实现（跟踪 `prev_date/prev_close`，喂 `regime_tracker.update(prev_date, prev_close)`）→ 通过。

---

## Phase W5 — 过拟合 / 稳健性

### Task 5.1: O1 + M4 扰动保护（param_perturb_service）

**Files:**
- Modify: `main/core/services/param_perturb_service.py:57-71`（`add_position_threshold` 等 `-1` 语义字段跳过；值域钳制丢弃越界方向）
- Test: `main/core/tests/unit/services/test_param_perturb_service.py`

- [x] **Step 1: 写失败测试**：源 `add_position_threshold=-1` → 不产生扰动值；扰动后 `capital_ratio` ≤1。
- [x] **Step 2–4**: 实现 → 通过。
- [x] **Step 5: 保持工作区**

### Task 5.2: O4 门槛下界 + 下界校验

**Files:**
- Modify: `main/core/api/backtest.py`（`oos_gate_min_ratio` 校验 `0.1 ≤ x ≤ 2`）
- Modify: `main/core/services/oos_gate.py:22-48`（`in_cagr ≤ 0` → rejected；bench 缺失 → status None）
- Test: 后端 API + oos_gate 单测

- [x] Steps 1–5 同上。

### Task 5.3: O3/L3 CAGR 实际天数 + WF 切分统一

**Files:**
- Modify: `main/core/engine/backtest_robustness.py:70-88,121-159`（实际首末日期天数；`_rolling_cagr` 按窗口实际跨度）
- Modify: `main/core/services/walk_forward_service.py:98-159`（显式用窗口 test 段边界做 OOS 切分，或对齐 `_oos_split`）
- Test: `test_backtest_robustness.py`、`test_walk_forward_service.py`

- [x] Steps 1–5 同上。

### Task 5.4: O5/L1/L2/L5 小修

- O5：`backtest_service.run_backtest` 完成后 span<252 交易日 → `record.warnings` 增提示。
- L1：`evaluator.py:151` `max_dd==0 → calmar=None`（`calmar_ratio` 允许 None）；`acceptance` 对 None 不误拒。
- L2：`backtest_robustness._max_drawdown` 返回正号。
- L5：`walk_forward_service` summary 增 `windows_failed`/`windows_errored`。
- **Files/Tests** 各自单测。

### Task 5.5: O2 伪 OOS 明示 + M3 归一化

- `backtest_robustness.analyze_robustness` oos payload 增 `caveat`；前端 `Backtest.vue`/WF 面板显示。
- `param_perturb_service` `delta_cagr` 归一化 + 不对称敏感度标注。

---

## Phase W6 — 风控

### Task 6.1: R1 实盘 daily_loss 用市值

**Files:**
- Modify: `main/core/engine/live/breaker.py:247-258`（日终 `total` 用市值口径）
- Test: `main/core/tests/unit/engine/live/test_breaker.py`（有持仓不误触）

- [x] 已改：`on_daily_update` 用 `portfolio.account.cash + portfolio._holdings_value()`；新增 `test_daily_close_uses_market_value_does_not_false_trigger`。

### Task 6.2: R2 跳空二次审批带敞口上限

**Files:**
- Modify: `main/core/engine/execution_engine.py:279-306`
- Modify: `main/core/engine/backtest_engine.py`（成交步把 `_exposure_kwargs` 透传，使重审走策略上限）
- Test: `test_execution_engine.py` 增「跳空放量单不越策略敞口上限」

- [x] 已改：触发条件扩为「超现金 或 超策略敞口余量」，二次审批透传 `strategy_capital_limit`/`strategy_position_value`；新增 `test_gap_up_buy_secondary_approval_respects_strategy_cap` 与 `test_gap_up_buy_secondary_approval_reject_when_cap_exhausted`。

### Task 6.3: R3 实盘 daily_loss 恢复时点对齐

**Files:**
- Modify: `main/core/engine/live_engine.py`（每交易日首根 bar 调 `enter_day`）
- Modify: `main/core/engine/live/daily_closer.py:75-97`（只做 update 收尾）
- Test: live_engine 单测

- [x] 已改：`_handle_bar` 在 `on_bar_update` 之前按日首调 `enter_day`（`_enter_day_updated` 幂等）；新增 `test_daily_loss_pause_cleared_at_first_bar_next_day` 与 `test_daily_loss_pause_blocks_new_buys_until_next_day_bar`。

### Task 6.4: R4 `_calendar` 死代码

**Files:**
- Modify: `main/core/engine/live/daily_closer.py`（`_calendar` property → `__init__` 内初始化）
- Test: `test_daily_closer.py`

- [x] 已改：幂等日期标记初始化移入 `__init__`；既有 `_last_daily_date` 测试回归覆盖。

---

## 收尾

- [x] 全量后端 `uv run pytest core/tests/ -q` 全绿（1084 passed）。
- [x] 前端 `npx vitest run` 全绿（258）+ `npm run build` 成功。
- [x] `alembic upgrade head` 在干净库通过；`downgrade` 可回退。
- [ ] 更新 `CLAUDE.md` 实现状态/AGENTS.md 若需。
- [ ] 不 commit（等用户指示）。
