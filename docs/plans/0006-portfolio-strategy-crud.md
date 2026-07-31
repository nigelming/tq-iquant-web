# 组合策略管理 CRUD — 组合 + 嵌套子策略（含主从配置）

## Context

公式管理（[0003](0003-formula-management-crud.md)）和股票池 v2（[0005](0005-stock-pool-v2-tdx-direct.md)）已完成，它们是组合策略的两个前置依赖：

- `PortfolioStrategy.stock_pool_id → stock_pools.id`（RESTRICT）— 选股票池
- `Strategy.formula_id → formulas.id`（RESTRICT）— 选公式

组合策略是回测的前置：`POST /api/backtest` 接 `portfolio_strategy_id`，`_assemble_portfolio`（[backtest.py:321-354](main/core/api/backtest.py#L321)）从 DB 组合 `PortfolioStrategy` + 其下 `Strategy` 列表成引擎 `Portfolio` 对象。**组合策略必须先有数据才能回测**，但当前管理功能完全缺失：

- 后端 [main/core/api/strategies.py](main/core/api/strategies.py)（注意：文件名 strategies，前缀是 `/api/portfolios`）只有一个桩 `list_portfolios` 返回 `{"code":0,"data":[]}`，无 CRUD
- 前端 [Portfolios.vue](web/src/views/Portfolios.vue) 只读列表（id/name/初始资金/状态），无新建/编辑/删除/查看详情
- 模型已完备：[PortfolioStrategy](main/core/models/portfolio_strategy.py)（父）+ [Strategy](main/core/models/strategy.py)（子），均无 `relationship`，需显式二次查询

用户决策：
- 范围 = 组合 CRUD + 子策略 CRUD（**不做主从运行时联动**，引擎都未实现）
- 新建组合/子策略时，下拉选已有股票池/公式
- 主从自引用时序 → **后端两步 commit**（先 insert 全部子策略拿 id，再 UPDATE master_strategy_id）
- 顺手修 formulas delete 500 bug（[formulas.py:117-123](main/core/api/formulas.py#L117) 没捕获 IntegrityError，删被引用公式直接 500）
- 不补 constants.py 枚举（本次后端校验用模块内常量集合，同 formulas.py 现有模式）

## 业务规则（出处：[docs/system-plan-draft.md](docs/system-plan-draft.md) §5.3.2 + [AGENTS.md](AGENTS.md) + [CLAUDE.md](CLAUDE.md)）

### 枚举可选值（实证，非猜测）
- `period` ∈ {`1m`, `5m`, `30m`, `60m`, `1d`, `1w`}（draft:222 — **无 15m**）
- `role` ∈ {`independent`, `master`, `slave`}（draft:223/512 — `shared/.../constants.py` 已有 `StrategyRole`，但本次用模块常量）
- `trading_session` ∈ {`full`, `am`, `pm`}（draft:499 — **非 morning/afternoon**）
- 组合 `status` ∈ {`active`, `archived`}（draft:500）

### 配置期校验（CRUD 必做，防脏数据）
- `role=slave` → `master_strategy_id` 必填，且指向**同 portfolio 下 role=master** 的策略（draft:255d）
- `role=master`/`independent` → `master_strategy_id` 必须为 NULL（draft:255d）
- 删 master 前 check 无 slave 引用（自引用 FK 无 ondelete，否则孤儿引用）
- `capital_ratio` 单字段 `0 < x ≤ 1` + 4 位小数（DECIMAL(5,4)）— **不跨策略求和**（draft:261a "多策略上限之和可超 100%"）
- `max_drawdown`/`daily_loss_limit` ∈ `(0,1)`；`max_holdings` 正整数

### 运行时规则（CRUD 不做，引擎/运行时状态维护）
- 主策略持有时从策略才可买入、清仓后不可新开仓但存量可卖（draft:255a/b）— 回测当前平铺所有策略，不读 role
- 熔断触发/恢复/计数（draft:211-216、636-637）— 运行时落 `live_session_portfolios`，CRUD 只存参数
- T+1、信号优先级 — 纯运行时

### 回测实际消费的字段（决定 CRUD 必落哪些，出处 [_assemble_portfolio](main/core/api/backtest.py#L321)）
- 组合必需：`stock_pool_id`（无默认）；`initial_capital`/`max_drawdown`/`daily_loss_limit` 有默认但回测 `Decimal(str())` 读，NULL 会崩 → CRUD 保证非空或依赖 DB default
- 子策略必需：`portfolio_id`/`formula_id`/`period`（NOT NULL）；`stop_loss_ratio`/`take_profit_ratio`/`trailing_stop_ratio`/`capital_ratio`/`max_positions` 有默认，同上
- 引擎**未消费**的字段（CRUD 落库即可，不做行为校验）：`single_open_ratio`/`add_position_*`/`reduce_position_ratio`/费率/滑点/基准/交易时段 — DB 有、引擎硬编码

## 关键文件

- 改 [main/core/api/strategies.py](main/core/api/strategies.py) — 桩改完整 CRUD（组合 + 子策略两层）
- 改 [main/core/api/formulas.py](main/core/api/formulas.py) — `delete_formula` 补 IntegrityError → 409
- 新建 [main/core/tests/integration/test_portfolio_api.py](main/core/tests/integration/test_portfolio_api.py) — 后端集成测试
- 改 [web/src/views/Portfolios.vue](web/src/views/Portfolios.vue) — 列表 + 新建/编辑 Modal（嵌套子策略多行，复用 signal-row）
- 改 [web/src/api/index.ts](web/src/api/index.ts) — 加 Portfolio/Strategy 接口与 CRUD 函数
- 新建 [web/src/__tests__/Portfolios.test.ts](web/src/__tests__/Portfolios.test.ts) — 前端测试
- 模型不改（已完备）；main.py 不改（路由已注册）；不加 Alembic 迁移（无 schema 变更）
- 不新增 CSS（style.css 现有类足够：modal-lg/signal-row/signal-add/badge/btn）

## 实现范围（全程 TDD）

### 第 0 步：修 formulas delete 500 bug（TDD 先复现）
- 测试：`test_delete_formula_referenced_by_strategy` — 建公式 + Strategy 引用它 → `DELETE /api/formulas/{id}` 应返 `code:409`（当前 500）
- 修 [formulas.py](main/core/api/formulas.py) `delete_formula`：`try/except IntegrityError` → rollback + `{"code":409,"message":"该公式被策略引用，无法删除"}`（同 stock_pools.py:155-160 模式）

### 第 1 步：后端组合 CRUD（TDD）
测试文件 [test_portfolio_api.py](main/core/tests/integration/test_portfolio_api.py)，fixture 同 [test_formula_api.py:18-48](main/core/tests/integration/test_formula_api.py#L18)（内存 SQLite + StaticPool + PRAGMA foreign_keys=ON + `app.dependency_overrides[get_db]`）。

端点（HTTP 恒 200，错误体现在 body.code）：
- `GET /api/portfolios` — 列表，每条含 `strategies` 子列表
- `GET /api/portfolios/{pid}` — 详情（含子策略）；不存在 → 404
- `POST /api/portfolios` — 创建（父 + 子一起）；校验失败 → 400；股票池不存在 → 400
- `PUT /api/portfolios/{pid}` — 编辑（子表全量替换：删旧建新）；不存在 → 404
- `DELETE /api/portfolios/{pid}` — 删除（子策略随 CASCADE 自动删）；被回测记录/实盘 session 引用 → 409

测试用例（每端点 happy + 边界，参考 test_formula_api.py 覆盖密度）：
- list 空 / list 返含 strategies
- detail / detail 404
- create 含子策略 / create 校验失败(非法 status/trading_session/period/role) / create 股票池不存在 / create capital_ratio 越界
- create **主从配置**：slave 指向同 portfolio master（后端两步 commit 后 master_strategy_id 正确落库）
- create **主从校验**：slave 无 master_strategy_id → 400；slave 的 master 指向不同 portfolio → 400；master/independent 带 master_strategy_id → 400
- update 子表全量替换（原 2 子策略 → 新 3 子策略，旧的删干净）
- delete CASCADE（子策略一并删）/ delete 404
- delete master 被 slave 引用 → 400（先校验后删）

实现要点：
- `_serialize_portfolio(db, p)` 显式二次查 `Strategy`（模型无 relationship，同 formulas.py:28-33）
- `_validate_portfolio(req, db)` 返回 str|None（同 formulas.py:52-59）；含主从校验
- **两步 commit**：POST/PUT 时先 `db.add(p)` + flush 拿 pid → 逐个 `db.add(Strategy)`（此时 master_strategy_id 留空）→ flush 拿所有子策略 id → 遍历从策略 `UPDATE master_strategy_id` 指向对应 master id → commit。校验阶段先在 req 内解析"slave 的 master 指向 req 里哪个子策略（按临时索引/name）"，映射到落库后的真实 id
- 删除 master 前 `db.query(Strategy).filter(Strategy.master_strategy_id == pid_to_delete).count()` > 0 → 400

### 第 2 步：前端 Portfolios.vue 重写（TDD，vitest）
测试 [Portfolios.test.ts](web/src/__tests__/Portfolios.test.ts)，模式同 [Formulas.test.ts](web/src/__tests__/Formulas.test.ts)（vi.mock('../api')、beforeEach mockResolvedValue、vi.stubGlobal('confirm')、mount+flushPromises）。

视图结构（复用 Formulas.vue + StockPools.vue 模式）：
- 顶部"+ 新建组合策略"按钮
- 列表表格：ID/名称/股票池(反查 name)/子策略数(badge-blue)/状态(badge)/操作(编辑/删除)
- Modal（modal-lg）：名称 input、股票池 select（从 stockPools 下拉）、基准指数 input、资金/风控字段、**子策略多行配置（signal-row：名称/公式 select/周期 select/角色 select/主策略 select(仅 slave 显示)/资金占比/止损止盈...）**、"+ 添加子策略"(signal-add)
- `load()` 并行拉 `getPortfolios` + `getStockPools` + `getFormulas`，单个 `.catch(()=>[])` 防阻塞（StockPools.vue:24-28 模式）
- 编辑时从 `p.strategies` 填充多行；提交时 `createPortfolio`/`updatePortfolio`

测试用例：
- 挂载渲染列表（名称/子策略数）
- 点[+新建]弹 Modal
- 点[+添加子策略]行 +1
- 填表提交 → 调 createPortfolio，参数含 name/strategies
- 编辑回填 → 调 updatePortfolio
- 删除 → 调 deletePortfolio(id)
- slave 角色时显示主策略下拉（v-if）

### 第 3 步：全量回归 + E2E
- 后端 `uv run pytest`（含新测试 + 回测/公式/股票池无回归）
- 前端 `npx vitest run` + `npm run build`
- E2E（用户本地）：`./manage.ps1 restart` → `/portfolios` 页面 → 新建组合(选股票池+加子策略选公式) → 编辑 → 删除 → 回测页用该组合跑回测

## 验证

- 后端测试全绿（新 test_portfolio_api.py + 既有 93 例无回归）
- 前端测试全绿 + build 无类型错误
- E2E：能在界面配置出"组合 + 1 主 1 从子策略"，且回测页能选该组合跑通（复用已有回测链路）
