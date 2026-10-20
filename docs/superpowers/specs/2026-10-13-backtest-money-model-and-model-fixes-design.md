# 回测资金模型与模型真实性修复 — 设计

日期：2026-10-13
状态：待实施
范围：全模型审计发现的 6 组问题修复（W1–W6）

## 背景

全模型审计（回测管理为重点）发现：收益统计算术正确，但风险调整指标被「固定名义仓位」系统性压低；混合周期回测存在前视；公式未来函数检查形同虚设且旗舰公式内容为空无法核验；过拟合/稳健性机制与风控存在若干口径/逻辑缺陷。本设计给出全部修复方案与实施顺序。

## 决策摘要（用户已授权由实施者裁定）

- 实施拆分：一个 spec + 一个分阶段 plan；阶段顺序 W1 → W2 → W3 → W4 → W5 → W6。
- R3（实盘 daily_loss 恢复时点）在范围内，允许改变实盘行为。
- O2（伪 OOS）无法技术根除，用明示 warning/UI 标签处理。

---

## W1 — 指标口径小修

### W1.1 `total_trades` 与胜率口径不一致
- 现象：`Evaluator._compute_trade_metrics` 返回 `total_trades=len(trades)`（BUY+SELL 成交笔数），而 `win_rate`/`profit_factor`/`avg_holding_days` 基于 FIFO 平仓批次。前端同列展示易误读。
- 方案：
  - `evaluate()` 新增键 `closed_trades` = FIFO 平仓批次数（`len(lot_pnls)`）。
  - 迁移：`backtest_evaluations` 增列 `closed_trades INTEGER NULL`。
  - `_serialize_evaluation` / `_EVALUATION_FIELDS` 增 `closed_trades`。
  - 前端：`Backtest.vue` 列标题 `total_trades` 改「成交笔数」，新增「平仓批次」列；胜率列 tooltip「按平仓批次」。
- 测试：单测 `closed_trades` = 平仓批次数；`total_trades` 不变。

### W1.2 record 4 evaluation 与快照不可复现
- 现象：record 4 存储 `total_return=0.8911`，从落库 snapshots 重算 `0.88794`；其余记录零误差。
- 根因（实施时确认）：**同一天可能写入多行快照**，`evaluate()` 用 `snapshots[-1]["snap_date"]` 算跨度、`daily_values` 按日期取末行，而落库行序/末行与内存不一致；或 evaluation 与 snapshots 非同一事务写出。
- 方案：
  - **按日快照（见 W4）** 落地后，每个 `(record, target, date)` 恰一行，消除根因。
  - 新增不变式测试：对每个已完成 record，从落库 portfolio snapshots 重算 `total_return/annual_return/max_drawdown`，与落库 evaluation 在容差内一致（round-trip）。
  - `run_backtest` 写 evaluation 与 snapshots 在同一事务提交（确认现有实现，若分离则合并）。

---

## W2 — 仓位模型（动态默认 + 固定可选 + 现金管理预留）

### W2.1 动态仓位（默认）
- 公式：
  - dynamic：`strategy_fund = ctx.capital_ratio × 组合当前总净值`（`total_value = cash + 持仓按最近已知价市值`）。
  - fixed：`strategy_fund = ctx.capital_ratio × account.initial_capital`（现值）。
  - OPEN 与 ADD 两处量计算统一走 `Portfolio._strategy_fund(ctx)`。
- `Position`：不改；仓位倍率参数不变。
- 实现要点：
  - `Portfolio.__init__` 接收 `position_sizing`（`"dynamic"`|`"fixed"`，默认 `"dynamic"`）。
  - 在 `on_bar` 起始计算一次 `self._bar_equity = self.total_value(bar)`（刷新价快照），供 `_signal_to_order` 复用，避免逐信号重算。
  - `_strategy_fund(ctx)`：`ctx.capital_ratio × (self._bar_equity if dynamic else self.account.initial_capital)`。
- `portfolio_builder` 从 `PortfolioStrategy.position_sizing` 读取。
- `params_snapshot` 冻结 `position_sizing`（组合层），保证历史可复现。
- **敞口上限 `exposure_cap_enabled` 保持现状**（`capital_ratio × initial_capital`），本次不改，避免范围蔓延。

### W2.2 现金管理增强预留触发点
- 新增 `PortfolioStrategy` 列：
  - `cash_enhance_enabled BOOLEAN default 0`
  - `cash_enhance_equity_threshold NUMERIC(15,2) NULL`
  - `cash_enhance_position_ratio_threshold NUMERIC(5,4) NULL`
- `Portfolio` 增同名字段与 `_maybe_trigger_cash_enhancement(bar, total_value)` 空洞口：
  - 触发条件（OR）：`total_value ≥ equity_threshold` 或 `holdings_value/total_value ≤ position_ratio_threshold`（阈值非 None 才判）。
  - 命中记一条 `decision_event`：`gate="cash_enhance_trigger"`, `layer="portfolio_risk"`, `action="trigger"`，message「现金管理增强为预留接口，待实现」，**不产生任何订单**。
  - 每交易日最多记一次（用 `_cash_enhance_last_date` 去重）。
- 调用点：回测 `backtest_engine` 日终快照后；实盘 `live_engine` 日终（14:30 closer）后。
- 前端 `Portfolios.vue` 增「现金管理（预留）」组：开关 + 净资产阈值 + 仓位占比阈值；`api/index.ts` `PortfolioItem`/`PortfolioRequest` 增字段。

### W2.3 迁移
- `portfolio_strategies` 增列：`position_sizing`, `cash_enhance_enabled`, `cash_enhance_equity_threshold`, `cash_enhance_position_ratio_threshold`。
- 默认值：`position_sizing` server_default `'dynamic'`；`cash_enhance_enabled` server_default `'0'`。

---

## W3 — 交易执行现实性

- `price_limit_check_enabled` 默认改 **True**：
  - 模型：`default=True, server_default="1"`。
  - 迁移：`UPDATE portfolio_strategies SET price_limit_check_enabled=1` + 改 server_default。
  - 创建 API 默认（若硬编码）改 True；前端组合表单默认勾选。
- `volume_limit_enabled` / `slippage` 保持默认关/0（按用户要求，当前资产规模小）；仅确认不受 W3 其余改动影响。
- 无其它 schema 变更。

---

## W4 — 前视

### W4.1 删除公式未来函数检查
- 后端删除：
  - `formula_service.scan_future_functions` 及关键字表、相关异常/常量。
  - `api/formulas.py` 的 `_check_future_functions` 与两处调用、`FormulaCreate.strict_future_check` 字段。
  - 相关后端测试（`test_formula_service`/`test_formulas_api` 中该功能用例）。
- 前端删除：
  - `web/src/views/Formulas.vue` 的 `strict_future_check` 表单字段/回填/提交字段。
  - `web/src/api/index.ts` `FormulaRequest.strict_future_check`。
  - `web/src/__tests__/Formulas.test.ts` 对应用例。
- 用户自行判断公式内容。

### W4.2 混合周期前视修复（回测按周期路由）
- 现状：`BacktestEngine` 合并所有周期进单一时间轴，`on_bar` 不传 `period` → 1d 00:00 bar 驱动 5m 策略并在同日 09:30 成交（前视）；且分钟周期同时间戳互相覆盖 OHLC。
- 方案：
  - `_build_price_index` 改为按 `(datetime, period)` 键：`Dict[Tuple[datetime,str], Dict[str,dict]]`。
  - `_build_timeline` 返回排序去重的 `List[Tuple[datetime, period]]`，同 `t` 下按固定周期顺序（`1mon > 1w > 1d > 60m > 30m > 15m > 5m > 1m`）。
  - 每步：填 `pending_orders`（仅 `order.period == 当前 period`）→ 构造 `BarEvent(period=P)` → `on_bar(bar, signal_cache, period=P)`。
  - `OrderEvent` 增 `period: Optional[str] = None`；`_signal_to_order` 置 `order.period = bar.period`；`_signal_bar_date_for` 逻辑保留（日线信号延后）。
  - `risk_manager.enter_day` 仅在**日期变化**时调用一次（用 `_last_enter_date`）。
  - `prev_close_map`/`volume` 取当前 period 行。
  - 删除 `_COARSE_PERIODS` 覆盖逻辑（`(t,period)` 键天然消歧），改为按周期独立行。
- **每日快照**：改为**每交易日一行**（date 变化时 flush 上一日末值），保证 `backtest_daily_snapshots` 真日频、W1.2 根因消除、robustness 用价一致。
- 单周期组合行为不变（timeline 仅该周期）。
- `regime_tracker`：用**前一交易日**收盘喂入（消除 opt-in 日内泄漏）。

### W4.3 迁移/前端
- 无 schema 变更（period 为内存字段）。

---

## W5 — 过拟合 / 稳健性

- **O1** `param_perturb_service.scaled_value`：`add_position_threshold` 等含特殊值 `-1` → 该方向返回 `None`（不可扰动）。通用规则：对 `PERTURB_PARAM_FIELDS` 中 `-1` 语义字段特判。
- **O1b（M4）**：扰动值装配前做值域钳制（`capital_ratio` ≤1、`single_open_ratio`/`add_position_ratio`/`reduce_position_ratio`/`stop_loss_ratio`/`take_profit_ratio`/`trailing_stop_ratio` ∈ (0,1]、`max_positions`/`max_add_count` ≥1），越界则丢弃该方向并标注。
- **O3** CAGR 年化按**实际日期跨度**：`backtest_robustness._segment` 与 `_rolling_cagr` 用首末 `snap_date` 的实际天数 /365.25，而非 `hi-lo+1`。WF 窗口与 `_oos_split` 切分统一：`run_walk_forward` 按窗口自身 `test` 段边界显式传入 `_segment`（或窗口起止对齐年初）。
- **O4** `api/backtest.py`：`oos_gate_min_ratio` 校验下界（`≥ 0.1`，`≤ 2`）。
- **O4b** `oos_gate.evaluate_oos_gate`：`in_cagr ≤ 0` → 直接 `rejected`（原因「样本内年化为负，不采纳」）；基准缺失（`bench is None`）→ 返回 `status=None`（未评估，原因「基准数据缺失」），不再静默跳过通过。
- **O5** 回测区间 < 252 交易日 → `warnings` 增「年化收益基于不足一年样本，仅供参考」。
- **O2** 伪 OOS 明示：`oos` payload 增 `caveat` 文本；前端 `Backtest.vue` 门槛区与 WF 面板显示「OOS 仅防时段崩塌，不防全样本调参」。
- **M3** `param_perturb_service`：`delta_cagr` 按实际扰动幅度归一化；单方向可扰动参数也计入 `most_sensitive`（标注不对称）。
- **L1** `evaluator`：`max_dd == 0` → `calmar_ratio=None`；`acceptance` 对 None 视为未知（不因 calmar 误拒）。
- **L2** `backtest_robustness._max_drawdown` 返回**正号**回撤，与 `robustness.py` 统一。
- **L3** 3 年滚动窗口按窗口实际首末日期跨度年化。
- **L5** WF summary 增 `windows_failed`/`windows_errored` 计数。

---

## W6 — 风控

- **R1** `engine/live/breaker.py`：日终 `total` 改用市值口径（`portfolio.total_value(bar)` 或 `_holdings_value + cash`），与 `prev_close_value`（市值）一致；新增「有持仓」单测验证不误触。
- **R2** `engine/execution_engine.py` 跳空高开二次审批：透传 `strategy_capital_limit`/策略持仓市值，避免绕过敞口上限。
- **R3** 实盘 daily_loss 恢复时点对齐回测：在实盘**每交易日首根 bar** 调 `risk_manager.enter_day(date, initial_capital)`（镜像回测），使暂停/恢复在日首生效；保留 14:30 closer 的 `update_daily` 收尾。
- **R4** `engine/live/daily_closer.py` `_calendar` property 死代码移入 `__init__`。

---

## 测试策略（TDD）

- 后端 pytest（`uv run pytest core/tests/`），前端 vitest（`web/`）。
- 每个工作流先写失败测试：W1（closed_trades、round-trip）、W2（dynamic/fixed 量、cash-enhance 事件）、W3（涨跌停默认、period 路由无前视、每日快照单行）、W4（删除检查后 200、混合周期不再前视）、W5（O1–O5/M3/L1–L5 各单测）、W6（R1–R4 各单测）。
- 回归：全量后端 + 前端测试必须通过；`npm run build` 通过。

## 迁移清单（W1–W4 合并一条迁移；W5/W6 无 schema）

1. `backtest_evaluations.closed_trades` INTEGER NULL。
2. `portfolio_strategies.position_sizing` VARCHAR(10) server_default 'dynamic'。
3. `portfolio_strategies.cash_enhance_enabled` BOOLEAN server_default '0'。
4. `portfolio_strategies.cash_enhance_equity_threshold` NUMERIC(15,2) NULL。
5. `portfolio_strategies.cash_enhance_position_ratio_threshold` NUMERIC(5,4) NULL。
6. `portfolio_strategies.price_limit_check_enabled` server_default 改 '1' + UPDATE 现有行为 1。

## 非目标

- 不实现现金管理增强的实际策略逻辑（仅预留触发点）。
- 不改 `exposure_cap` 上限口径。
- 不实现实盘 forming-bar 信号对齐（留待后续，风险高、需真机验证）。
- 不做参数网格搜索/多重比较校正（O2 仅明示）。
