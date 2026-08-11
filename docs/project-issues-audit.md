# 项目问题清单 — 全面审计

> 审计日期：2026-08-10
> 审计范围：main/（Core 引擎 + API + ORM + TQ）、live/bridge/（iQuant 桥）、shared/、web/（前端）、docs/、配置文件
> 审计方法：3 个 explore agent 并行代码审查 + 直接代码/测试/迁移检查
> 测试状态：后端 364/365 passed（1 flaky），前端 77/77 passed，vue-tsc 零错误

---

## 图例

| 级别 | 含义 | 处理时机 |
|------|------|----------|
| **P0** | 严重 — 影响正确性/安全 | 立即修复 |
| **P1** | 高 — 架构/一致性 | 近期修复 |
| **P2** | 中 — 代码质量/数据完整性 | 迭代改进 |
| **P3** | 低 — 风格/小问题 | 长期优化 |

---

## P0 — 严重（影响正确性/安全）

### #1 桥 `DRY_RUN` 注释与值矛盾

- **位置**：`live/bridge/iquant_bridge.py:42`
- **现象**：`DRY_RUN = False` 但注释写 `"safe default: only print, no real order. Flip to False when ready"`
- **影响**：默认配置下桥会发送真实订单，与注释描述的"安全默认"相反
- **修复**：将值改为 `True`（安全默认），或将注释改为 `"real order mode. Flip to True for dry run"`，确保值与注释一致

### #2 桥鉴权 fail-open

- **位置**：`live/bridge/iquant_bridge.py:106-109`
- **现象**：`check_auth()` 中 `TOKEN` 为 `None` 时返回 `True`（放行）
- **影响**：未配置 token = 无鉴权，任何本机进程可调用 `/order` 下单
- **修复**：改为 fail-closed — `TOKEN is None` 时拒绝所有非 `/ping` 请求，或强制要求部署前配置 token

### #3 阻塞同步 I/O 在异步事件循环中

- **位置**：`main/core/engine/live_engine.py:245-279`（`_loop` 方法）
- **现象**：`_loop()` 声明 `async` 但所有 I/O 用 `httpx.Client`（同步客户端）：
  - `dispatcher.heartbeat()` — 阻塞 HTTP GET `/ping`
  - `bar_poller.poll()` — 阻塞 HTTP GET `/quote`（每股票一次）
  - `_handle_bar()` — 阻塞 HTTP + DB + TDX 锁
  - `_poll_deals()` — 阻塞 HTTP + DB
  - `_maybe_daily_bars()` — 阻塞 HTTP + TDX
  - `_fill_signal_cache()` — 阻塞 TDX 锁 + 公式计算
- **影响**：每 30s bar 轮询 + 每 5s 成交轮询期间，FastAPI 事件循环被阻塞，所有 HTTP 请求和 SSE 流冻结
- **修复**：
  - 方案 A：`HttpBridgeDispatcher` 改用 `httpx.AsyncClient`，引擎方法全改真异步
  - 方案 B：`_handle_bar`/`_poll_deals` 用 `asyncio.to_thread()` 包装，避免阻塞事件循环

### #4 `_poll_deals` 吞错误不 re-raise

- **位置**：`main/core/engine/live_engine.py:1008-1010`
- **现象**：`except Exception: db.rollback(); logger.exception(...)` — catch 后不 re-raise
- **影响**：成交回填系统性错误（如桥解析 bug）被静默吞没，所有成交记录可能持续丢失而无告警
- **修复**：re-raise 或加连续失败计数器，达阈值后标记引擎异常并停止

### #5 SSE 端点 404 返回 JSON

- **位置**：`main/core/api/live.py:460-461`
- **现象**：不存在的 session 返回 `{"code": 404, "message": "资源不存在"}`（HTTP 200，content-type: application/json）
- **影响**：EventSource 无法解析 JSON 响应，静默触发 `onerror`，用户无错误提示
- **修复**：返回 HTTP 404 状态码（`raise HTTPException(404, ...)`），或在 SSE 流内发送 error 事件后关闭

### #6 `.bridge_account` 未加入 .gitignore

- **位置**：`.gitignore`
- **现象**：只忽略了 `live/bridge/.bridge_token`，未忽略 `.bridge_account`
- **影响**：券商账号文件可能被意外提交到 git
- **修复**：在 `.gitignore` 添加 `live/bridge/.bridge_account`

### #7 `config.yaml` 缺 `iquant_bridge` 段

- **位置**：`config.yaml`
- **现象**：Core 的 `api/live.py:37` 读取 `config.iquant_bridge` 获取桥地址和 token，但 `config.yaml` 中无此段
- **影响**：使用默认值 `127.0.0.1:8790` 无 token 连接 — 默认无鉴权
- **修复**：在 `config.yaml` 添加 `iquant_bridge` 段（url + token_env），或在 `config.py` 的 `_defaults()` 中补默认结构

### #8 无 PostgreSQL 配置路径

- **位置**：`main/core/db.py`, `main/core/config.py`
- **现象**：AGENTS.md 称生产用 PostgreSQL + `TQ_DB_PASSWORD` 环境变量，但 `db.py` 只构造 `sqlite:///` URL，`config.py` 的 `_defaults()` 只有 `sqlite_path`，无任何 PostgreSQL 切换代码
- **影响**：无法切换到生产数据库
- **修复**：在 `config.py` 支持 `database.url` 字段（优先于 `sqlite_path`），在 `db.py` 检测 url scheme 选择引擎；补 `TQ_DB_PASSWORD` 读取逻辑

---

## P1 — 高（架构/一致性）

### #9 Service 层完全缺失

- **位置**：`main/core/services/__init__.py`（空文件）
- **现象**：设计文档规定 6 个 service 文件（stock_pool_service / formula_service / strategy_service / backtest_service / live_service / system_service），全部不存在
- **影响**：全部业务逻辑直接在 API 路由中 — `backtest.py` 880 行（其中 ~480 行业务逻辑）、`live.py` 484 行、`strategies.py` 391 行。路由文件臃肿，逻辑不可复用，难以测试
- **修复**：提取业务逻辑到 service 层，路由仅做参数校验 + 调 service + 格式化响应

### #10 死代码：SignalEngine + EventBus

- **位置**：
  - `main/core/engine/signal_engine.py`（25 行，SignalEngine 类）
  - `main/core/engine/event_bus.py`（50 行，EventBus 类）
  - `main/core/engine/__init__.py:2,8`（导出两者）
  - `main/core/tests/unit/test_event_bus.py`（仅测试 EventBus）
- **现象**：CLAUDE.md:67 确认 "SignalEngine 为未接线的冗余壳"。实际信号处理走 `StrategyContext.get_signal()` → `Portfolio.on_bar()` → `_process_strategy()`。EventBus 的 `process_signals` 去重逻辑与生产代码的 `cleared` 集合逻辑不一致
- **影响**：混淆代码结构，维护者可能误以为它们在使用
- **修复**：删除两个文件 + `__init__.py` 导出 + `test_event_bus.py`；或移到 `legacy/` 目录

### #11 统一响应格式不一致

- **位置**：全部 API 路由文件
- **现象**：设计规定 `{code:0, message:"ok", data:{}}`。实际：
  - 成功响应普遍缺 `message`（17+ 处）：`backtest.py:652`, `formulas.py:69`, `live.py:65`, `status.py:14`, `system.py:11` 等
  - 错误响应缺 `data`：`backtest.py:659`, `formulas.py:76`, `live.py:102` 等
  - 两者都缺：`live.py:428`（`{"code": 0}`）、`system.py:16`（`{"code": 0}`）
- **影响**：前端无法依赖统一格式，`res.data.data` 在错误时返回 `undefined`
- **修复**：定义统一响应工具函数 `ok(data=None, message="ok")` 和 `err(code, message)`，所有路由统一调用

### #12 错误处理两种模式冲突

- **位置**：
  - 模式 A（多数）：返回 `{code: 非零, message: "..."}` + HTTP 200
  - 模式 B（backtest.py）：`raise HTTPException(status_code=4xx, detail="...")` — `backtest.py:761,768-770`
- **影响**：`HTTPException` 产生 FastAPI 默认 `{"detail":"..."}` 格式 + HTTP 错误状态码，与统一格式不一致。前端 axios 对 HTTP 4xx 触发 catch，对 HTTP 200 + code≠0 不触发 — 两套错误路径
- **修复**：统一用模式 A（HTTP 200 + code），或注册全局异常处理器把 `HTTPException` 转为统一格式

### #13 无全局异常处理器

- **位置**：`main/core/main.py`（35 行，无 `@app.exception_handler`）
- **现象**：未捕获异常产生 FastAPI 默认 `{"detail":"Internal Server Error"}` + HTTP 500
- **影响**：未预期异常的响应格式与统一规范不符
- **修复**：注册 `@app.exception_handler(Exception)` 把未捕获异常转为 `{code:500, message:str(e)}`

### #14 前端无 axios 拦截器

- **位置**：`web/src/api/index.ts:1-3`
- **现象**：`axios.create({ baseURL: '/api' })` 无拦截器，不检查 `code !== 0`
- **影响**：业务错误（HTTP 200 + code≠0）的 `data` 字段缺失时前端得到 `undefined`，不触发错误提示
- **修复**：添加 response 拦截器：`code !== 0` 时 reject 并带 `message`

### #15 前端多个视图无 try/catch

- **位置**：
  - `Formulas.vue` — 全文件零 try/catch（load/submit/remove）
  - `LiveSessions.vue` — create/start/stop 无 try/catch
  - `StockPools.vue` — viewStocks/syncPool/remove 无 try/catch
  - `Portfolios.vue` — loadPortfolios/toggleExpand/removePortfolio 无 try/catch
  - `Backtest.vue` — openDetail/load 无 try/catch
- **影响**：API 调用网络错误 = unhandled promise rejection，控制台报错但用户无感知
- **修复**：每个 API 调用包裹 try/catch，catch 中设置 errorMsg 并展示

### #16 设计文档 44 端点中 12 个未实现

- **位置**：`docs/system-plan-draft.md` §5.6 vs `main/core/api/` 实现
- **缺失端点**：

| # | 设计文档端点 | 状态 | 说明 |
|---|---|---|---|
| 1 | `GET /api/stock-pools/{id}` | 缺失 | 股票池详情 |
| 2 | `GET /api/formulas/{id}/signals` | 缺失 | 信号内嵌公式响应 |
| 3 | `POST /api/formulas/{id}/signals` | 缺失 | 信号随公式创建 |
| 4 | `PUT /api/formulas/{id}/signals/{sid}` | 缺失 | |
| 5 | `DELETE /api/formulas/{id}/signals/{sid}` | 缺失 | |
| 6 | `POST /api/formulas/{id}/test-run` | 缺失 | 公式试运行 |
| 7 | `GET /api/backtest/records/{id}/trades` | 缺失 | 内嵌详情响应 |
| 8 | `GET /api/backtest/records/{id}/snapshots` | 缺失 | 内嵌详情响应 |
| 9 | `GET /api/backtest/records/{id}/results` | 缺失 | 内嵌详情响应 |
| 10 | `POST /api/live/sessions/{id}/portfolios/{pid}/start` | 缺失 | 单组合启动 |
| 11 | `POST /api/live/sessions/{id}/portfolios/{pid}/stop` | 缺失 | 单组合停止 |
| 12 | `PUT /api/live/sessions/{id}` | 缺失 | 编辑实盘会话 |

- **路径偏差**：
  - 设计 `POST /api/stock-pools/{id}/sync` → 实现 `POST /api/stock-pools/sync`（body `{code}`）
  - 设计 `GET /api/stock-pools/{id}/stocks` → 实现 `GET /api/stock-pools/tdx/{code}/stocks`
- **额外端点**（设计无但已实现）：`DELETE /api/stock-pools/{id}`, `DELETE /api/backtest/records/{id}`, `GET /api/live/sessions/{id}/positions`, `GET /api/live/sessions/{id}/bridge-status`
- **缺失查询参数**：`orders` 缺 `portfolio_id` 过滤，`trades` 无任何过滤
- **修复**：确认每个缺失端点是否需要补实现，或更新设计文档标记为"不再需要"；路径偏差和额外端点更新设计文档

### #17 并发模型与设计不符

- **位置**：`AGENTS.md`（并发模型段）vs 实际实现
- **现象**：AGENTS.md 写 "实盘：TQ 回调线程 → `asyncio.run_coroutine_threadsafe` → 主事件循环处理信号"。实际实现用同步轮询（`BarPoller.poll()` → 阻塞 HTTP），无 TQ 回调线程，无 `run_coroutine_threadsafe`（grep 零命中）
- **影响**：设计文档误导维护者对并发模型的理解
- **修复**：更新 AGENTS.md 并发模型段，描述实际实现（同步轮询 + `asyncio.create_task`），说明为何偏离原设计

---

## P2 — 中（代码质量/数据完整性）

### #18 8 个外键缺 `ondelete` 规则

- **位置**：以下 8 个 FK 无 `ondelete`（默认 NO ACTION/RESTRICT）：

| 模型 | 列 | FK 目标 | 行 | 风险 |
|------|-----|---------|-----|------|
| BacktestTrade | `strategy_id` | `strategies.id` | `backtest_trade.py:12` | 策略删除被阻止 |
| BacktestTrade | `formula_signal_id` | `formula_signals.id` | `backtest_trade.py:13` | **公式删除时信号级联删除，交易记录引用悬空** |
| LiveOrder | `portfolio_strategy_id` | `portfolio_strategies.id` | `live_order.py:12` | 组合删除被阻止 |
| LiveOrder | `strategy_id` | `strategies.id` | `live_order.py:13` | 策略删除被阻止 |
| LiveTrade | `live_order_id` | `live_orders.id` | `live_trade.py:12` | 订单删除被阻止 |
| LiveTrade | `portfolio_strategy_id` | `portfolio_strategies.id` | `live_trade.py:13` | |
| LiveTrade | `strategy_id` | `strategies.id` | `live_trade.py:14` | |
| Strategy | `master_strategy_id` | `strategies.id`（自引用） | `strategy.py:16` | 主策略删除被阻止 |

- **最严重**：`BacktestTrade.formula_signal_id` — `formula_signals` 从 `formulas` 级联删除，但 `backtest_trades` 引用无 ondelete，公式删除后交易记录外键悬空
- **修复**：对历史数据表（`backtest_trades`）的 `formula_signal_id` 加 `ondelete=SET NULL`；对实盘表按业务语义补 CASCADE 或 RESTRICT

### #19 `stock_pools.code` 缺唯一约束和索引

- **位置**：`main/core/models/stock_pool.py:11`
- **现象**：`code` 列是通达信板块 code（同步 key），但无 `UniqueConstraint` 无 `Index`
- **影响**：两个池可以有相同 code，同步逻辑歧义；按 code 查询无索引
- **修复**：加 `UniqueConstraint("code")` + 生成 Alembic 迁移

### #20 多个 FK 列缺索引

- **位置**：以下列频繁用于 list/filter 查询但无索引：

| 表 | 列 | 查询场景 |
|----|-----|---------|
| `stock_pools` | `code` | 按 code 查池（同步） |
| `backtest_records` | `portfolio_strategy_id` | 列出组合的回测 |
| `strategies` | `portfolio_id` | 列出组合的策略 |
| `strategies` | `formula_id` | 查用某公式的策略 |
| `strategies` | `master_strategy_id` | 查主策略的从策略 |
| `formula_signals` | `formula_id` | 列出公式的信号 |
| `live_orders` | `strategy_id` | 按策略过滤订单 |
| `live_orders` | `stock_code` | 按股票过滤订单 |
| `live_trades` | `strategy_id` | 按策略过滤成交 |
| `live_trades` | `stock_code` | 按股票过滤成交 |

- **修复**：对高频查询列加 `Index`，生成 Alembic 迁移

### #21 `default=` 无 `server_default=`

- **位置**：`PortfolioStrategy`（所有数值默认）、`Strategy`（所有数值默认）、`BacktestRecord.progress`、`LiveOrder.filled_quantity`、`LiveSessionPortfolio.status/circuit_breaker_count`
- **现象**：仅 `Formula.formula_count` 同时有 `default=200` 和 `server_default="200"`，其余均只有 Python 端 `default=`
- **影响**：ORM 层有默认值，但 DB 层列为 `nullable=True` 且无默认 — 原始 SQL 插入无默认值
- **修复**：对关键列补 `server_default`，生成 Alembic 迁移

### #22 `init_db()` 绕过 Alembic

- **位置**：`main/core/db.py:33-36`
- **现象**：`init_db()` 用 `Base.metadata.create_all()` 直接建表，不经过 Alembic 迁移
- **影响**：建表后 Alembic 版本表为空，后续 `alembic upgrade head` 会尝试从头跑所有迁移，可能失败
- **修复**：`init_db()` 改为调 `alembic upgrade head`，或至少 `alembic stamp head` 标记当前版本

### #23 时区不一致

- **位置**：`main/core/engine/live_engine.py:201,216,306,352,1049,1123,1153`
- **现象**：用 `datetime.now()`（无时区）。`bar_poller.py:37` 正确用 `_CST = timezone(timedelta(hours=8))`
- **影响**：非 CST 服务器上 `_maybe_daily_close` 的 `(now.hour, now.minute) < (14, 30)` 判断用服务器本地时间而非上海时间
- **修复**：所有 `datetime.now()` 改为 `datetime.now(_CST)`，从 `bar_poller` 导入 `_CST` 或建共享时区工具

### #24 JSON 解析错误静默吞没

- **位置**：`main/core/engine/http_bridge_dispatcher.py:87-88,121-122,158-159`
- **现象**：
  - `place_order`：`except Exception: data = {}` — 非 JSON 响应当作业务拒绝
  - `_get_json`：`except Exception: return []` — 非 JSON 返回空列表
  - `query_quote`：`except Exception: return []` — 非 JSON 返回空列表
- **影响**：桥返回非 JSON HTTP 200 时，错误被静默吞没，无日志，与业务拒绝/空数据不可区分
- **修复**：catch 前加 `logger.warning`，区分网络错误和解析错误

### #25 代码重复

- **位置**：
  - `_total_value()`：`backtest_engine.py:178-187` 和 `live_engine.py:646-655`（完全相同）
  - `_find_strategy()`：`backtest_engine.py:170-176` 和 `live_engine.py:658-662`（完全相同）
- **修复**：提取到 `engine/engine_utils.py`，两个引擎共用

### #26 前端 `any` 类型泛滥

- **位置**：`web/src/` 共 87 处 `any`
  - API 客户端 `index.ts`：19 处 `ApiResponse<any>` / `ApiResponse<any[]>`
  - 视图：`ref<any[]>([])` 大量使用
  - 仅 `LiveOrderItem`, `LiveTradeItem`, `LivePositionItem` 有正确接口定义
- **修复**：为 StockPool, Formula, Portfolio, Strategy, BacktestRecord, BacktestDetail 等定义 TypeScript 接口

### #27 前端 API 客户端不完整

- **位置**：`web/src/api/index.ts`
- **现象**：缺实盘 session CRUD/start/stop、系统配置、status 的封装。`LiveSessions.vue` 和 `SystemConfig.vue` 绕过 API 客户端直接用 `axios`
- **修复**：补全 API 客户端函数，视图统一通过 API 客户端调用

### #28 前端缺加载状态

- **位置**：6 个视图中仅 `SystemConfig.vue` 有 `loading` 状态
- **影响**：获取数据时显示空白，用户体验差
- **修复**：每个视图加 `loading` ref + 加载中提示

### #29 `strategy_risk` 属性未在 `__init__` 声明

- **位置**：`main/core/engine/strategy_context.py`（`__init__` 中无 `strategy_risk`）
- **现象**：`portfolio_builder.py:73` 动态设置 `ctx.strategy_risk = StrategyRiskManager(...)`，`portfolio.py:107` 用 `getattr(ctx, "strategy_risk", None)` 访问
- **影响**：未用 `assemble_portfolio` 时 `strategy_risk` 不存在，`getattr` 返回 `None`，风控检查静默失效
- **修复**：在 `StrategyContext.__init__` 声明 `self.strategy_risk: Optional[StrategyRiskManager] = None`

### #30 封装破坏

- **位置**：`main/core/engine/live_engine.py:361,623,874,905,554`
- **现象**：
  - 4 处访问 `self._bar_poller._stock_codes`（BarPoller 私有属性）
  - 1 处调用 `self._dispatcher._order_id(order)`（HttpBridgeDispatcher 私有方法）
- **修复**：BarPoller 加 `stock_codes` 公共 property，HttpBridgeDispatcher 加公共 `order_id()` 方法

### #31 函数体内 import

- **位置**：
  - `live_engine.py:70` — `import math` 在 `_to_int()` 内，每信号值执行
  - `live_engine.py:797` — `import pandas as pd` 在 `_bars_to_formula_df()` 内，每股票每 bar 执行
- **修复**：移到模块顶部

### #32 `_placed` 字典无界增长

- **位置**：`live/bridge/iquant_bridge.py:55`
- **现象**：`_placed = {}` 存储每个订单结果，无清理机制
- **影响**：桥长期运行内存持续增长
- **修复**：加 TTL 清理（如保留最近 1000 条）或定期清理

### #33 硬编码值

- **位置**：

| 位置 | 值 | 说明 |
|------|-----|------|
| `evaluator.py:46` | `rf = 0.02` | 无风险利率（影响 Sharpe/Sortino） |
| `live_engine.py:307,353` | `(14, 30)` | 日终时间 |
| `live_engine.py:90` | `poll_interval = 30.0` | 轮询间隔 |
| `live_engine.py:91` | `deals_poll_interval = 5.0` | 成交轮询间隔 |
| `live_engine.py:95` | `formula_count = 200` | 默认注入根数 |
| `live_engine.py:192` | `Queue(maxsize=200)` | SSE 队列上限 |
| `http_bridge_dispatcher.py:77` | `"pr_type": 14` | iQuant 价格类型 |
| `api/live.py:185` | `["000001.SZ"]` | 空池回退股票 |
| `execution_engine.py:29-33` | `min_commission=5` 等 | 成本参数默认值 |

- **修复**：可配置值移到 `config.yaml` 或构造参数；`rf` 作为 `Evaluator.evaluate()` 参数

### #34 `AGENTS.md` 标注过时

- **位置**：`AGENTS.md` 顶部 "项目状态" 段
- **现象**：写 "Greenfield — 仅存在设计文档，无实际代码"，但项目已有大量实现（14 模型 + 17 引擎文件 + 7 API 路由 + 6 前端视图）
- **修复**：更新项目状态段，反映实际实现进度

### #35 `status.py` 残留废弃网关引用

- **位置**：`main/core/api/status.py:22-25`
- **现象**：硬编码 `iguant_gateway: {online: False}` — NATS 网关已废弃（AGENTS.md 确认）
- **修复**：删除 `iguant_gateway` 字段，或改为 `bridge_status` 反映 HTTP 桥状态

### #36 `datetime.utcnow()` 弃用警告

- **位置**：`main/core/api/backtest.py:858`
- **现象**：Python 3.13 中 `datetime.utcnow()` 已弃用，测试产生 `DeprecationWarning`
- **修复**：改为 `datetime.now(timezone.utc).replace(tzinfo=None)` 或 `datetime.now(_CST)`

---

## P3 — 低（风格/小问题）

### #37 Flaky 测试

- **位置**：`main/core/tests/unit/test_live_engine.py:1382`（`test_loop_offline_to_online_resets_baseline`）
- **现象**：单独运行通过，全量运行时因时序/状态泄漏失败
- **修复**：确保 `asyncio.run` 前后状态完全隔离，或加 `pytest.mark.asyncio` 用 fixture 管理 event loop

### #38 conftest 字符串 key bug

- **位置**：`main/core/tests/conftest.py:24`
- **现象**：`app.dependency_overrides["get_db"] = override_get_db` 用字符串 key — 无效（应为函数对象 `get_db`）
- **影响**：使用 `conftest.py` 的 `test_client` fixture 的测试实际不覆盖 DB 依赖。但集成测试有自己的 fixture（用函数 key），所以不影响测试有效性
- **修复**：改为 `app.dependency_overrides[get_db] = override_get_db`

### #39 无根 README.md

- **位置**：项目根目录
- **现象**：仅有子目录 README（`main/README.md`, `live/README.md`, `web/README.md`），无项目级 README
- **修复**：创建项目根 `README.md`，含项目简介、架构图、快速启动

### #40 Pinia 已装未用

- **位置**：`web/src/stores/`（空目录），`web/package.json` 已装 `pinia`
- **现象**：所有视图用 `ref` 管理状态，无 Pinia store
- **影响**：非问题（当前规模用 ref 足够），但 `implementation-plan.md:31` 已标注 "Pinia 已装未启用，无 stores"
- **修复**：如启用则建 stores；如不启用则从 `package.json` 移除 pinia

### #41 无首页仪表盘

- **位置**：`web/src/views/`（无 Dashboard/Home 视图）
- **现象**：设计文档规定首页展示核心后端 + 网关运行状态。`/api/status` 后端已就绪，前端无仪表盘页
- **修复**：新建 `Home.vue` 或 `Dashboard.vue`，消费 `/api/status` 展示系统状态

### #42 `deleteStrategy` 返回值不一致

- **位置**：`web/src/api/index.ts:193`
- **现象**：返回 `res.data`（完整 ApiResponse）而非 `res.data.data`（如其他函数）— 为检查 `res.code` 的 workaround
- **修复**：统一 API 客户端返回值；如需检查 code，用拦截器处理（见 #14）

### #43 桥 token 比较不安全

- **位置**：`live/bridge/iquant_bridge.py:109`
- **现象**：`headers.get("x-auth-token") == TOKEN` 用 `==`，有时序攻击风险
- **影响**：localhost 环境低风险
- **修复**：改为 `hmac.compare_digest(headers.get("x-auth-token", ""), TOKEN or "")`

### #44 "97% 代码复用" 说法误导

- **位置**：`AGENTS.md`（模块复用段）
- **现象**：核心逻辑（Portfolio/ExecutionEngine/risk_manager）确实共用，但引擎特有代码 `live_engine.py` 1319 行 vs `backtest_engine.py` 220 行（6:1）
- **修复**：更新描述为 "引擎核心逻辑（Portfolio/ExecutionEngine/风控/持仓/资金）回测/实盘共用；引擎特有逻辑（SSE/订单状态机/成交回填/持仓恢复/对账/多周期分发）为实盘独有"

### #45 14 个模型无 ORM relationship

- **位置**：全部 14 个模型文件
- **现象**：仅 `Column(ForeignKey(...))`，无 `relationship()` — 无法懒加载/预加载，所有 join 手动写
- **影响**：非 bug（设计选择），但 N+1 查询风险高，代码冗长
- **修复**：如需改进，为高频关联（如 `PortfolioStrategy.strategies`, `Formula.signals`）加 `relationship()`

---

## 亮点（做得好的地方）

1. **TDD 执行到位** — 365 后端测试 + 77 前端测试，覆盖面广
2. **架构迁移干净** — NATS → HTTP 桥的迁移无残留代码（grep 零命中）
3. **shared/ 包 Python 3.7 兼容性确认** — 无 walrus/match/3.7+ 语法
4. **桥代码 Python 3.6 兼容** — 纯标准库，无 f-string，无 3.7+ 特性
5. **Alembic 迁移链完整** — 6 个迁移线性无分支，单 head，与模型一致
6. **文档详尽** — open-questions.md / live-flow-checklist.md / implementation-plan.md 记录完整决策链
7. **前端 TypeScript 类型检查通过** — `vue-tsc --noEmit` 零错误
8. **SSE 实现可用** — 五类事件 + 心跳 + 前端 EventSource 消费

---

## 建议修复优先级

| 优先级 | 问题编号 | 主题 |
|--------|---------|------|
| 立即修复 | #1, #2, #6 | 桥安全（DRY_RUN/鉴权/gitignore） |
| 近期修复 | #3, #4, #5, #8 | 引擎正确性（异步 I/O/错误处理/SSE/PG） |
| 近期修复 | #11, #12, #13, #14, #15 | 统一响应格式 + 错误处理 + 前端拦截器 |
| 迭代改进 | #9, #10, #16, #17 | 架构（service 层/死代码/端点/文档） |
| 迭代改进 | #18, #19, #20, #21, #22 | 数据完整性（FK/索引/server_default/init_db） |
| 长期优化 | #23-#36 | 代码质量（时区/JSON 错误/重复/类型/封装/硬编码） |
| 长期优化 | #37-#45 | 风格（flaky/conftest/README/Pinia/仪表盘/relationship） |
