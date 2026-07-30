# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概况

创懿量化交易平台 — 桥接通达信(TDX)回测数据与国信 iQuant 实盘交易的单用户量化系统。无鉴权，所有接口直接可访问。

**权威业务规则文档**：[AGENTS.md](AGENTS.md) 记录了完整的业务规则、API 规范、并发模型与数据库设计。**但其中"项目状态: Greenfield，无实际代码"一句已过时** — opencode 已完成脚手架搭建（模型、API 路由、引擎骨架、前端视图均已存在），见下方"实现状态"。

**完整设计文档**（实现时的依据）：
- [docs/system-plan-draft.md](docs/system-plan-draft.md) — 系统总体设计（81KB，含 44 个接口定义、数据库 schema、依赖图）
- [docs/implementation-plan.md](docs/implementation-plan.md) — 实施计划

## 核心架构（4 模块，2 个 Python 环境）

```
Core (main/, Python 3.13) ──NATS──→ iQuant Gateway (live/, Python 3.7)
  └─ 同进程嵌入 TQ 模块（通达信，直接函数调用 + polars DataFrame，不走 NATS）
Web 前端 (web/, Vue 3 + Vite) ←HTTP/SSE→ Core (FastAPI)
shared/ — tq_iquant_shared 包，被 main 和 live 共同引用
```

- **`main/`** — FastAPI Core + 嵌入式 TQ 模块。事件驱动引擎（polars 核心）。用 `uv` 管理依赖。
- **`live/`** — 国信 iQuant 网关，Python 3.7（iQuant 自带 3.6.8，nats-py 最低要求 3.7）。当前为 mock 实现。用 `uv`，`nats-py` 锁定 `<2.7`。
- **`shared/`** — `tq_iquant_shared` 包，通过 `[tool.uv.sources]` 路径依赖 `../shared` 引入。
- **`web/`** — Vue 3 + Vite + Pinia + axios + TypeScript。白色侧边栏+顶栏布局。

### ⚠️ shared/ 兼容性硬约束

`shared/` 被 main(3.13) 和 live(3.7) 共同引用，**代码必须兼容 Python 3.7**：不可用海象运算符 `:=`、`match` 语句、pydantic v2。用 dataclasses 或 pydantic v1。改 `shared/` 时务必同时考虑两个环境。

### 通信拓扑

- **Core ↔ iQuant**：NATS，5 个 subject（定义在 [shared/tq_iquant_shared/nats_schemas.py](shared/tq_iquant_shared/nats_schemas.py)：`order_place`/`order_query`/`order_cancel`/`position_query`/`status`）
- **Core ↔ 通达信**：同进程直接调用，polars DataFrame 进程内传递，**不走 NATS**
- **Web ↔ Core**：HTTP（开发期 Vite proxy `/api` → `localhost:8000`）+ SSE 实盘实时推送（`GET /api/live/sessions/{id}/stream`）

## 开发命令

所有 Python 命令在 `main/` 目录下运行（`uv` 项目）；前端命令在 `web/` 目录下运行。

| 命令 | 说明 |
|---|---|
| `uv run uvicorn core.main:app --reload` | 启动后端 dev server（在 `main/`） |
| `uv run pytest` | 后端全部测试（在 `main/`，测试位于 `core/tests/`） |
| `uv run pytest core/tests/unit/` | 仅单元测试 |
| `uv run pytest core/tests/unit/test_position.py::TestName` | 运行单个测试 |
| `uv run alembic upgrade head` | 应用数据库迁移（在 `main/`） |
| `uv run alembic revision --autogenerate -m "msg"` | 生成新迁移 |
| `npm run dev` | 启动前端 dev server（在 `web/`，端口 5173） |
| `npx vitest` | 前端测试（在 `web/`，测试位于 `src/__tests__/`） |
| `npm run build` | 构建前端到 `web/dist/`（生产期由 FastAPI 托管） |
| `./manage.ps1 start` / `stop` / `restart` / `status` | 一键管理前后端（后端 8000，前端 5173） |
| `docker compose up` | 启动 PostgreSQL + NATS（生产依赖；开发期用 SQLite，NATS 可不用） |

> 测试路径注意：AGENTS.md 写的是 `tests/`，实际位于 `main/core/tests/`。

## 实现状态（opencode 已完成的部分）

**已实现（较完整）**：
- 14 个 SQLAlchemy 模型（[main/core/models/](main/core/models/)）+ Alembic init 迁移（14 张表）
- 7 个 API 路由已注册到 [main/core/main.py](main/core/main.py)；`/api/live/sessions` CRUD、`/api/stock-pools` 列表、`/api/system/configs` 读写功能可用
- NATS 客户端 [main/core/nats_client/client.py](main/core/nats_client/client.py)
- 前端 5 个视图 + 路由 + API 客户端 + 布局

**脚手架/桩代码（需补全）**：
- 引擎层（[main/core/engine/](main/core/engine/)）：`BacktestEngine.run` 是空循环桩；`LiveEngine` 方法全为 `pass`；`ExecutionEngine` 部分实现；`SignalEngine` 仅基本框架。引擎层设计目标约 97% 回测/实盘共用，通过策略模式隔离（`OrderDispatcher`/`T1Checker` 接口，见 [execution_engine.py](main/core/engine/execution_engine.py)）
- 多数 API 路由返回桩 `{"code": 0, "data": []}`（formulas、portfolios、backtest records）
- iQuant 网关 [live/iguant_gateway/main.py](live/iguant_gateway/main.py) 全为 mock 处理器
- TQ 模块（[main/core/tq/](main/core/tq/)）文件已建，需对接通达信

**已知待修复**：[main/core/db.py](main/core/db.py) 当前硬编码 `sqlite:///./dev.db`，未读取 `config.yaml`/`TQ_DB_PASSWORD`，与 [config.py](main/core/config.py) 的配置系统脱节。切换 PostgreSQL 时需打通此处（设计要求改 `alembic.ini` 的 `sqlalchemy.url`）。

## 关键约定（非显而易见，易遗漏）

- **统一响应格式**：`{ "code": 0, "message": "ok", "data": {...} }`，code=0 成功，非 0 错误。前端 `ApiResponse<T>` 已定义于 [web/src/api/index.ts](web/src/api/index.ts)。
- **股票代码**：统一带后缀（如 `000001.SZ`），通达信规范；校验见 [shared/tq_iquant_shared/stock_utils.py](shared/tq_iquant_shared/stock_utils.py)
- **复权**：统一前复权
- **时区**：所有时间按 Asia/Shanghai 处理
- **T+1**：回测强制 T+1，实盘实时查可用股数
- **信号优先级**：风控信号（止损/止盈/移动止损）> 公式信号（OPEN/ADD/REDUCE/CLOSE）
- **资金模型**：策略资金占比是持仓上限（非预分），多策略上限之和可超 100%
- **熔断**：max_drawdown 触发次日恢复（累计 3 次转手动）；daily_loss_limit 当日暂停次日恢复。熔断期间不清仓，仅暂停新开仓
- **主从策略**：从策略仅在主策略持有时可买入；主策略清仓后从策略不可新开仓但存量可自行卖出
- **数据库迁移**：用 Alembic，禁止手动改表结构。`config.yaml` 不存密码（从环境变量 `TQ_DB_PASSWORD` 读取）
- **并发**：回测用 `ProcessPoolExecutor` 子进程，同一时刻最多 1 个；实盘 TQ 回调线程经 `asyncio.run_coroutine_threadsafe` 转入主事件循环，主循环不可被 TQ 回调阻塞

## 开发范式

**TDD**：先写测试再写实现（pytest 后端 + vitest 前端）。引擎单元测试用 Mock 数据，无需连接通达信。

**开发顺序**（设计文档第 10 章有依赖图）：基础设施 → 数据层 → TQ 模块 → 核心引擎 → 回测 → 实盘 → 收尾。
