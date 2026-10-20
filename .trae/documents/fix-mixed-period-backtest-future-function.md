# 混合周期组合回测未来函数修复 + 多模式支持

## Context

混合周期组合回测存在未来函数：日线信号 bar_time 为当日 00:00:00，在时间轴排序后排在当日分钟 bar 之前，导致日线信号在当日 09:30 分钟 bar 的 open 价成交——等于"开盘时就知道全天日线信号"。

同时用户需要支持三种混合组合回测模式（独立并行 / 不同股票池独立 / 主从增强），其中第三种模式需要子策略跟随主策略卖出的联动机制。

**用户确认的决策**：
1. 策略级股票池（Strategy 表加 stock_pool_id，空=继承组合级）
2. 增加 slave_exit_mode 字段（follow_master / independent）
3. 资金模型不变（capital_ratio 仍是持仓上限）
4. 未来函数修复：OrderEvent 携带 signal_bar_date，日线信号延迟到次日成交

---

## 修改计划

### 1. 数据模型 + Alembic 迁移

**[main/core/models/strategy.py](file:///d:/project/tq-iquant-web/main/core/models/strategy.py)**：Strategy 新增两字段
- `stock_pool_id = Column(Integer, ForeignKey("stock_pools.id", ondelete="RESTRICT"), nullable=True, index=True)` — 空=继承组合级
- `slave_exit_mode = Column(String(15), nullable=False, default="independent", server_default="independent")` — 仅 role=slave 时有意义

**新建迁移** `main/alembic/versions/e6f7a8b9c0d1_add_strategy_pool_and_slave_exit.py`：revision 接 `d5e6f7a8b9c0`（当前 head），加两列 + FK + 索引

### 2. 未来函数修复（引擎核心）

**[main/core/engine/event.py](file:///d:/project/tq-iquant-web/main/core/engine/event.py)**：
- `OrderEvent` 新增 `signal_bar_date: Optional[date] = None`
- import 增加 `date`

**[main/core/engine/portfolio.py](file:///d:/project/tq-iquant-web/main/core/engine/portfolio.py)**：
- 顶部新增 `_DAILY_PERIODS = {"1d", "1w", "1mon"}`
- `_signal_to_order`（L401-411）：构造 OrderEvent 时 `signal_bar_date = bar.bar_time.date() if ctx.period in _DAILY_PERIODS else None`

**[main/core/engine/backtest_engine.py](file:///d:/project/tq-iquant-web/main/core/engine/backtest_engine.py)** L46-66 成交步：
- 拆分 pending_orders：`signal_bar_date is not None and t.date() <= order.signal_bar_date` 的订单延后（deferred），其余成交
- `pending_orders = deferred`（保留未成交订单到下一 bar）

### 3. 子策略卖出模式（follow_master 联动）

**[main/core/engine/strategy_context.py](file:///d:/project/tq-iquant-web/main/core/engine/strategy_context.py)**：
- `__init__` 新增 `slave_exit_mode: str = "independent"` 参数，存 `self.slave_exit_mode`

**[main/core/engine/portfolio.py](file:///d:/project/tq-iquant-web/main/core/engine/portfolio.py)** `on_bar` 方法（L84-141）：
- 第一遍处理各策略时收集 `master_sells_this_bar: Dict[int, set]`（master_id → 卖出股票集合）和 `slave_sells_this_bar`（去重用）
- 第一遍后增加第二遍 `_follow_master_sells(bar, master_sells, slave_sells, period)`：
  - 遍历 `role="slave" and slave_exit_mode="follow_master"` 的策略
  - master 本 bar 卖出的股票 - slave 本 bar 已自行卖出的股票 = 需联动卖出的集合
  - 对每只 slave 持有的该集合股票，生成 SELL OrderEvent（全平，signal_name="follow_master"）
  - `signal_bar_date` 按 slave 自身 period 标注

### 4. 策略级股票池

**[main/core/engine/portfolio_builder.py](file:///d:/project/tq-iquant-web/main/core/engine/portfolio_builder.py)** `assemble_portfolio`（L49-68）：
- 组合级池改为 `portfolio_pool_codes`（继承用）
- 每个 strategy：`strat.stock_pool_id is not None` → 查独立池成分；None → 继承 `portfolio_pool_codes`
- `StrategyContext(..., stock_pool=strategy_pool_codes, slave_exit_mode=strat.slave_exit_mode)`

**[main/core/services/backtest_service.py](file:///d:/project/tq-iquant-web/main/core/services/backtest_service.py)**：
- 新增 `_all_stocks_for_backtest(ps, strategies, db)`：组合级池 ∪ 各策略级独立池（去重保序），替代 `build_klines` 中仅查组合级的逻辑
- `build_signal_cache`（L1189-1210）：每个策略按自己的 `stock_pool_id` 过滤 stocks 列表后跑公式（None=全部 klines 股票=继承组合级）

### 5. API 层

**[main/core/api/strategies.py](file:///d:/project/tq-iquant-web/main/core/api/strategies.py)**：
- `StrategyItem` 加 `stock_pool_id: int | None = None`、`slave_exit_mode: str = "independent"`
- 新增 `VALID_SLAVE_EXIT_MODES = {"independent", "follow_master"}`
- `_validate_portfolio` / `_validate_strategy` 加校验：slave_exit_mode 合法性、stock_pool_id 存在性、非 slave 角色的 slave_exit_mode 必须为 independent

**[main/core/services/strategy_service.py](file:///d:/project/tq-iquant-web/main/core/services/strategy_service.py)**：
- `serialize_strategy` 加两字段输出
- `_apply_strategy_fields` / `_create_strategies_two_step` 加两字段写入

### 6. 前端

**[web/src/api/index.ts](file:///d:/project/tq-iquant-web/web/src/api/index.ts)**：StrategyItem/StrategyDetail/StrategyRequest 三处加 `stock_pool_id?: number | null` 和 `slave_exit_mode?: string`

**[web/src/views/Portfolios.vue](file:///d:/project/tq-iquant-web/web/src/views/Portfolios.vue)**：
- 新增 `SLAVE_EXIT_MODES` 常量
- STRATEGY_GROUPS "基本信息"组加 `stock_pool_id`（select，含"继承组合级"选项）和 `slave_exit_mode`（select，showIf: 'slave'）
- `emptyStrategyForm` 加两字段默认值
- `openEditStrategy` 回填两字段
- `optionsOf` 加 `strategyPools` 和 `slaveExitModes` 分支

---

## TDD 测试清单

### 后端单元测试

**未来函数**（`test_backtest_engine.py`）：
1. 日线 OPEN 信号 OrderEvent 有 signal_bar_date
2. 分钟 OPEN 信号 signal_bar_date is None
3. 混合 1d+5m 时间轴：1d 信号 D 日 00:00 生成，D 日 09:30 不可成交，D+1 日才成交
4. 1w/1mon 信号延迟到下一交易日
5. 日线 STOP_LOSS 订单也延迟

**策略级股票池**（`test_portfolio_builder.py` 或 `test_portfolio.py`）：
6. strategy.stock_pool_id 非空 → StrategyContext.stock_pool 仅含该池成分
7. stock_pool_id=None → 继承组合级池
8. build_klines 取并集

**slave_exit_mode**（`test_portfolio.py`）：
9. follow_master：master CLOSE 卖 X，slave 持 X → 自动追加 SELL X
10. follow_master：slave 未持 X → 不出追加订单
11. follow_master：slave 本 bar 已自行 SELL X → 去重不再追加
12. independent：master 卖 X → slave 不出追加订单（回归保护）
13. 日线 slave 的 follow_master 订单也带 signal_bar_date

**API 层**（`test_portfolio_api.py`）：
14. 创建策略带 stock_pool_id
15. 创建 slave 带 follow_master
16. 非法 slave_exit_mode → 400
17. 非 slave 角色 + follow_master → 400

### 前端测试（`Portfolios.test.ts`）
18. Modal 含股票池下拉（继承组合级选项）
19. role=slave 时显示卖出模式字段
20. 提交 payload 含两字段

### 集成测试（`test_backtest_api.py`）
21. 混合周期回测无未来函数（1d 信号次日成交）
22. 多策略独立股票池回测不串池
23. 主从 follow_master 回测联动卖出

---

## 验证

1. `uv run alembic upgrade head` — 迁移成功
2. `uv run pytest core/tests/ -q` — 全量后端测试通过
3. `npx vitest run` — 前端测试通过
4. `npx vue-tsc -b` — 类型检查无错误

---

## 实施顺序

1. 迁移 + 模型层（加字段）
2. API 层（字段透传 + 校验 + 单测 14-17）
3. 引擎层核心：event.py → portfolio.py（signal_bar_date + _follow_master_sells）→ backtest_engine.py（成交闸门）→ strategy_context.py → portfolio_builder.py（单测 1-13）
4. 服务层：backtest_service.py（build_klines 并集 + build_signal_cache 按策略池）
5. 前端：api/index.ts + Portfolios.vue（测试 18-20）
6. 集成测试 21-23
7. 最终全量验证
