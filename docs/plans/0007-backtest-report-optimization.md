# 回测详情页优化（参考 quant-cy）

## Context（背景与目标）
当前回测详情页（`web/src/views/Backtest.vue`）只展示组合层 12 个指标，而后端评估器实际产出 19 项（漏掉 sortino / var_95 / cvar_95 / avg_recovery_days / max_recovery_days / ulcer_index / return_stability 共 7 项）；净值曲线是手写 SVG（无渐变、无零轴标线、tooltip 简陋），无回撤曲线，交易明细全量渲染无分页。

参考 `D:\project\tdx\quant-cy\src\web\frontend\src\views\portfolios\PortfolioReport.vue`，将回测详情页升级为专业级回测报告：
- 关键指标摘要（4 大卡：总收益 / 年化 / 最大回撤 / 夏普）
- 整体表现指标（6 渐变卡）
- 策略对比分析（每个子策略独立 18 项指标卡 + 组合整体卡）
- echarts 净值曲线（组合 + 各策略 + 基准，渐变面积 + 零轴标线 + tooltip）
- echarts 回撤曲线（红渐变面积）
- 交易明细分页

用户已确认：① 引入 echarts；② 一并实现按策略拆分指标；③ 策略净值按"资金占比分摊现金"定义。

## 数据基础（已具备，无需迁移）
- `BacktestDailySnapshot` / `BacktestEvaluation` 表均已有 `target_type` + `target_id` 字段（portfolio/strategy 通用），表结构无需改、Alembic 迁移无需加。
- `Evaluator.evaluate(snapshots, trades, benchmark)` 是纯函数，接受任意快照序列 → 策略层评估直接复用，零改造。
- 共享现金账户模型下，策略净值 = 该策略持股市值 + 按 capital_ratio 比例分摊的组合现金；归一化后 Σ策略净值 = 组合总净值（可加性成立）。

---

## 实现方案

### 一、后端

#### 1. `BacktestEngine.run` — 产出策略层快照（`main/core/engine/backtest_engine.py`）
当前日终只产出 1 个组合快照。改为同时产出 N 个策略快照：
- 在「# 4. 日终快照」处，遍历 `portfolio.strategies`，对每个 ctx：
  - 策略持股市值 = Σ(quantity>0 且在 bar.stocks 中的 close × quantity)
  - Σcapital_ratio = Σ(所有 ctx.capital_ratio)
  - 策略分摊现金 = portfolio.account.cash × (ctx.capital_ratio / Σcapital_ratio)（归一化，保证可加性）
  - 策略总净值 = 持股市值 + 分摊现金
  - 追加 `{target_type:"strategy", target_id:ctx.strategy_id, snap_date, total_value, cash, market_value}`
- `run` 返回值新增 `strategy_snapshots: Dict[int, List[dict]]`（strategy_id → 快照列表）。
- 该策略的 trades 已在 `trades` 列表中带 `strategy_id`，按 id 过滤即得策略交易序列，喂给 Evaluator。

#### 2. `Evaluator` — 复用（`main/core/engine/evaluator.py`，无改动）
对每个策略的快照序列 + 该策略 trades 调用 `evaluate`，得策略层 19 项评估。

#### 3. `_persist_result` — 持久化策略快照 + 评估（`main/core/api/backtest.py`）
- 策略快照写 `BacktestDailySnapshot`（target_type="strategy", target_id=strategy_id）
- 策略评估写 `BacktestEvaluation`（target_type="strategy", target_id=strategy_id）
- trades 已带 strategy_id，无需改

#### 4. `get_record` API — 返回策略层数据（`main/core/api/backtest.py`）
扩展返回 `data` 新增：
- `strategy_evaluations`: [{strategy_id, strategy_name, ...19 项}]
- `strategy_snapshots`: [{strategy_id, strategy_name, curve:[{snap_date, total_value}]}]
- 策略名通过 `Strategy.name`（按 strategy_id 查）映射；trades 已有 strategy_id，前端可关联

#### 5. 序列化：新增 `_serialize_strategy_evaluation`、策略快照精简序列化（`{snap_date, total_value}` 供曲线）。

### 二、前端

#### 1. 安装 echarts：`cd web && npm install echarts`

#### 2. 重写 `Backtest.vue` 详情视图（`web/src/views/Backtest.vue`）
保留列表视图 + 发起弹窗（已可用），重写 `v-else` 详情视图为分区报告：
- **报告头**：返回 + 名称 + 起止 + 交易天数/年数副标题
- **关键指标摘要**：4 卡（总收益/年化/最大回撤/夏普），左色条（绿/绿/红/蓝）
- **整体表现指标**：6 渐变卡（总收益/年化/最大回撤/年化波动率/夏普/卡玛）
- **策略对比分析**：每策略 1 卡（18 项指标 2 列）+ 组合整体卡（边框高亮）；≥3 策略横向滚动
- **净值曲线**：echarts，series = 组合(渐变面积 + 零轴标线) + 各策略(细线) + 基准(虚线)；axis tooltip；legend bottom
- **回撤曲线**：echarts，红渐变面积，yAxis max:0；由组合净值序列前端算（peak − current）
- **交易明细**：表格 + 分页（pageSize=20），列：时间/策略(badge)/买卖/代码/数量/价格/金额/收益率/盈亏；按值正负着色

#### 3. 指标完整化：组合层从 12 项补到 19 项（补 sortino/var_95/cvar_95/avg+max_recovery_days/ulcer/return_stability）。

#### 4. echarts 生命周期：onMounted 加载后 `nextTick` init；onUnmounted dispose；window resize 监听。

### 三、测试
- 后端：`test_backtest_engine.py` 新增多策略用例，断言每策略有快照、Σ策略净值 ≈ 组合净值；`cd main && uv run pytest` 全绿。
- 前端：`web/src/__tests__/Backtest.test.ts` 适配新结构（若依赖旧渲染）。

---

## 关键文件
| 文件 | 改动 |
|---|---|
| `main/core/engine/backtest_engine.py` | 产出策略层快照 |
| `main/core/api/backtest.py` | 持久化 + get_record 返回策略数据 + 序列化 |
| `web/src/views/Backtest.vue` | 重写详情视图（echarts + 分区报告） |
| `web/package.json` | 加 echarts 依赖 |
| `main/core/tests/unit/test_backtest_engine.py` | 策略快照测试 |

## 验证
1. `cd main && uv run pytest -q` 全绿
2. `cd web && npm run build` 通过
3. `./manage.ps1 start`，前端发起一次多策略组合回测，详情页应显示：19 项组合指标、各策略 18 项指标卡、echarts 净值曲线（组合+策略+基准）、回撤曲线、分页交易明细

## 归档
批准后，本计划将复制到 `docs/plan/` 目录。
