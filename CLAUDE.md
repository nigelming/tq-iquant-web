# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概况

创懿量化交易平台 — 桥接通达信(TDX)回测数据与国信 iQuant 实盘交易的单用户量化系统。无鉴权，所有接口直接可访问。

**权威业务规则文档**：[AGENTS.md](AGENTS.md) 记录了完整的业务规则、API 规范、并发模型与数据库设计。当前实现状态（模型、API 路由、引擎骨架、前端视图均已落地）见下方"实现状态"。

**完整设计文档**（实现时的依据）：
- [docs/system-plan-draft.md](docs/system-plan-draft.md) — 系统总体设计（81KB，含 44 个接口定义、数据库 schema、依赖图）
- [docs/implementation-plan.md](docs/implementation-plan.md) — 实施计划

## 核心架构（4 模块，2 个 Python 环境）

```
Core (main/, Python 3.13) ──HTTP──→ iQuant 桥策略 (live/bridge/, iQuant 客户端内 Python 3.6)
  └─ 同进程嵌入 TQ 模块（通达信，直接函数调用 + polars DataFrame）
Web 前端 (web/, Vue 3 + Vite) ←HTTP/SSE→ Core (FastAPI)
shared/ — tq_iquant_shared 包，被 main 和 live 共同引用
```

- **`main/`** — FastAPI Core + 嵌入式 TQ 模块。事件驱动引擎（polars 核心）。用 `uv` 管理依赖。
- **`live/`** — iQuant 客户端内运行的桥策略（HTTP 桥，见 [docs/plans/0009-iquant-http-bridge.md](docs/plans/0009-iquant-http-bridge.md)）。iQuant 自带 Python 3.6.8，桥代码须兼容 3.6（无 f-string 等需谨慎，纯标准库实现 HTTP 层）。用 `uv` 管理依赖（仅开发/验证脚本用，桥策略本身无第三方依赖）。
- **`shared/`** — `tq_iquant_shared` 包，通过 `[tool.uv.sources]` 路径依赖 `../shared` 引入。
- **`web/`** — Vue 3 + Vite + Pinia + axios + TypeScript。白色侧边栏+顶栏布局。

> **架构变更（2026-08）**：原 NATS 网关方案已废弃，Core↔iQuant 改为 HTTP 桥（形态二：iQuant 客户端内策略，`init` 阻塞主循环）。详见计划 0009。原 `live/iguant_gateway/` NATS mock 网关已删除。

### ⚠️ shared/ 兼容性硬约束

`shared/` 被 main(3.13) 和 live(3.7) 共同引用，**代码必须兼容 Python 3.7**：不可用海象运算符 `:=`、`match` 语句、pydantic v2。用 dataclasses 或 pydantic v1。改 `shared/` 时务必同时考虑两个环境。

### 通信拓扑

- **Core ↔ iQuant**：HTTP + JSON（`127.0.0.1:8790`，iQuant 客户端内桥策略），端点 `/ping`/`/order`/`/positions`/`/account`/`/orders`/`/deals`/`/quote`，鉴权 + 白名单 + 限额 + 审计。详见 [docs/plans/0009-iquant-http-bridge.md](docs/plans/0009-iquant-http-bridge.md)
- **Core ↔ 通达信**：同进程直接调用，polars DataFrame 进程内传递
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

> 数据库：纯单用户本地工具，统一用 SQLite（`main/data/dev.db`），零配置，不切 PostgreSQL。

> 测试路径注意：AGENTS.md 写的是 `tests/`，实际位于 `main/core/tests/`。

## 实现状态（更新：2026-08-10）

**已实现（较完整）**：
- 14 个 SQLAlchemy 模型（[main/core/models/](main/core/models/)）+ 7 个 Alembic 迁移（init 14 表 + `circuit_breaker_count`/`formula_count`/数据完整性约束等后续迁移；head = `d4a5b6c7d8e9`）
- 7 个 API 路由模块已注册到 [main/core/main.py](main/core/main.py)：stock-pools/formulas/strategies/backtest/live/system/status 全部可用（回测、实盘 CRUD + start/stop + SSE 流）
- iQuant HTTP 桥 `HttpBridgeDispatcher`（[main/core/engine/http_bridge_dispatcher.py](main/core/engine/http_bridge_dispatcher.py)）+ 行情通道 `BarPoller`（[main/core/engine/bar_poller.py](main/core/engine/bar_poller.py)）+ 桥策略 `live/bridge/iquant_bridge.py`（真机验证过，订单匹配键 `m_strOrderRef` 等已定案）
- 引擎层全 TDD 实现：`BacktestEngine.run` 完整逐 bar 回测；`LiveEngine` 实盘主链路（订单状态机 + /deals 回填 + 三段式周期 1m 边界/1d 14:30/1w·1mon 通达信 + B5 SSE 五类事件流 + E5/E6 熔断 + D3 对账告警）已接线。`SignalEngine` 为未接线的冗余壳（信号走 `strategy_context` + `Portfolio.on_bar` 直接求值）
- TQ 模块（[main/core/tq/](main/core/tq/)）data/formula/utils 已对接通达信真机可用；`tdx_path` 从 config.yaml 读取
- 前端 6 个视图 + 路由 + API 客户端 + 布局（Backtest/Formulas/LiveSessions/Portfolios/StockPools/SystemConfig）

**当前缺口**（实时状态见 [docs/live-flow-checklist.md](docs/live-flow-checklist.md) 与 [docs/implementation-plan.md](docs/implementation-plan.md)）：
- F9 印花税仍 0（`/deals` 印花税字段待真机验证）；D3 对账自动校准待真机跑顺放开
- 首页仪表盘前端页、监控/告警骨架、conftest `dependency_overrides` 字符串 key、`data_feed.py`（功能由 backtest.py 直接调 `TQData`/`TQFormula` 覆盖）

**配置链（已修复）**：[main/core/db.py](main/core/db.py) 与 [main/alembic/env.py](main/alembic/env.py) 均从 [core.config.load_config()](main/core/config.py) 读 `database.sqlite_path`（默认 `data/dev.db`，相对 `main/` 解析为 `main/data/dev.db`），与 `config.yaml` 一致。db.py 逐连接开启 `PRAGMA foreign_keys=ON` 让 ondelete 生效。纯单用户本地工具，数据库就 SQLite 一条路，不切 PostgreSQL。

## 关键约定（非显而易见，易遗漏）

- **统一响应格式**：`{ "code": 0, "message": "ok", "data": {...} }`，code=0 成功，非 0 错误。前端 `ApiResponse<T>` 已定义于 [web/src/api/index.ts](web/src/api/index.ts)。
- **股票代码**：统一带后缀（如 `000001.SZ`），通达信规范；校验见 [shared/tq_iquant_shared/stock_utils.py](shared/tq_iquant_shared/stock_utils.py)
- **复权**：统一前复权
- **时区**：所有时间按 Asia/Shanghai 处理
- **T+1**：回测强制 T+1，实盘实时查可用股数
- **信号优先级**：风控信号（止损/止盈/移动止损）> 公式信号（OPEN/ADD/REDUCE/CLOSE）
- **资金模型**：策略资金占比是持仓上限（非预分），多策略上限之和可超 100%
- **熔断**：max_drawdown 触发次日恢复（累计 3 次转手动）；daily_loss_limit 当日暂停次日恢复。熔断期间不清仓，仅暂停新开仓
- **主从策略**：从策略只能买入主策略当前持有的同一只股票；主策略清仓（含该股）后从策略不可新开仓但存量可自行卖出
- **数据库迁移**：用 Alembic，禁止手动改表结构。
- **并发**：回测同步内联执行 + 全局锁 `_BACKTEST_LOCK`，同一时刻最多 1 个（并发启动返回 HTTP 409）；实盘全局限 1 个 session（`_ENGINES` 非空即拒，B6）。回测与实盘互不互斥，共享的通达信 TQ（C 扩展非线程安全）由 `core/tq/utils.py` 全局锁串行化；实盘 TQ 回调线程经 `asyncio.run_coroutine_threadsafe` 转入主事件循环，主循环不可被 TQ 回调阻塞

## 开发范式

**TDD**：先写测试再写实现（pytest 后端 + vitest 前端）。引擎单元测试用 Mock 数据，无需连接通达信。

**开发顺序**（设计文档第 10 章有依赖图）：基础设施 → 数据层 → TQ 模块 → 核心引擎 → 回测 → 实盘 → 收尾。
