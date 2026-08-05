# 创懿量化交易平台 — AGENTS.md

## 项目状态

**Greenfield** — 仅存在设计文档 `docs/system-plan-draft.md`，无实际代码。所有实现均需从零开始。

**单用户系统**：无用户鉴权，所有接口无需登录。

## 仓库

`github.com/nigelming/tq-iquant-web`

## 核心架构（4 模块）

```
Core (main, Python 3.13) ──HTTP──→ iQuant 桥策略 (live/bridge, iQuant 客户端内 Python 3.6)
  └─ 同进程嵌入 TQ 模块（通达信，直接函数调用）
Web 前端 (Vue 3 + Vite + Pinia) ←HTTP/WebSocket→ Core (FastAPI)
```

**关键约束**：两个独立的 uv 虚拟环境。`main/` 用 Python 3.13；`live/` 桥策略在 iQuant 客户端自带 Python 3.6.8 内运行（桥代码须兼容 3.6，纯标准库实现）。

> **Python 版本说明**：国信 iQuant 自带 Python 3.6.8，桥策略直接在该解释器内运行（不引入 nats-py 等需 3.7+ 的第三方库，HTTP 层用标准库 `socket` 手写）。

> **架构变更（2026-08）**：原 NATS 网关方案已废弃，Core↔iQuant 改为 HTTP 桥（iQuant 客户端内策略，`init` 阻塞主循环）。详见 [docs/plans/0009-iquant-http-bridge.md](docs/plans/0009-iquant-http-bridge.md)。原 `live/iguant_gateway/` NATS mock 网关已删除。

> **shared 包兼容性**：`shared/` 包被 main(3.13) 和 live(3.7) 共同引用，代码必须兼容 Python 3.7（不可用 walrus `:=`、match、pydantic v2 等）。数据结构用 dataclasses 或 pydantic v1。

> **时区**：所有时间按 Asia/Shanghai 处理，bar 时间判断基于上海时间。

> **数据库迁移**：使用 Alembic 管理 schema 变更，禁止手动改表结构。

## 开发命令

| 命令 | 说明 |
|---|---|
| `uv run pytest tests/` | 后端测试（main 环境） |
| `uv run pytest tests/unit/` | 仅单元测试 |
| `uv run pytest tests/integration/` | 仅集成测试 |
| `npx vitest` | 前端测试（web/ 目录） |
| `uv run uvicorn core.main:app --reload` | 启动后端 dev server |
| `npm run dev` | 启动前端 dev server（web/ 目录） |

开发期前端通过 Vite proxy 代理 API 到 FastAPI；生产期由 FastAPI 直接托管 `web/dist/` 静态文件。

## 开发范式

**TDD**：先写测试，再写实现。pytest（后端）+ vitest（前端）。

**开发顺序**（在 `docs/system-plan-draft.md` 第 10 章有完整依赖图）：

1. 基础设施 → 2. 数据层 → 3. TQ 模块 → 4. 核心引擎 → 5. 回测 → 6. 实盘 → 7. 收尾

## API 规范

- 基础路径：`/api`
- 无鉴权，API 直接可访问
- 统一返回格式：`{ "code": 0, "message": "ok", "data": { ... } }`（code=0 成功，非 0 错误）
- 44 个 HTTP 接口 + 1 个 WebSocket，9 组（详见设计文档 5.6 节）
- 实盘实时推送通过 SSE（`GET /api/live/sessions/{id}/stream`），非轮询

## 数据库（SQLite 开发 / PostgreSQL 生产）

详见设计文档 5.4 节。关键点：
- **开发期**：SQLite（`main/data/dev.db`），零配置
- **生产期**：PostgreSQL，通过 MVCC 解决回测子进程并发写入冲突
- 切换方式：修改 `alembic.ini` 的 `sqlalchemy.url`
- `config.yaml`（项目根目录）存储系统路径配置，**不存数据库密码**（密码从环境变量 `TQ_DB_PASSWORD` 读取）
- 每日快照 `backtest_daily_snapshots` 是评估指标的原始数据来源
- `backtest_evaluations` 18 个指标由 Evaluator 从快照序列计算
- `backtest_records.params_snapshot` 冻结回测时的策略参数，确保结果可复现
- 实盘交易记录：`live_orders`（订单状态跟踪）+ `live_trades`（成交记录）
- 多组合策略实盘：`live_session_portfolios` 关联表，一个 session 可含多个组合策略，共享 iQuant 账户，各组合策略独立维护虚拟持仓和虚拟现金

## 并发模型

- **回测**：`ProcessPoolExecutor` 提交子进程（CPU 密集），前端轮询获取状态。同一时刻最多 1 个回测运行。
- **实盘**：TQ 回调线程 → `asyncio.run_coroutine_threadsafe` → 主事件循环处理信号。
- FastAPI 主事件循环不能被 TQ 回调阻塞。

## 业务规则（容易遗漏）

- **股票代码格式**：统一带后缀（如 `000001.SZ`），通达信规范
- **复权方式**：统一前复权
- **TQ 数据传递**：polars DataFrame 进程内传递，通过 tqcenter SDK 直连运行中通达信
- **Core↔iQuant 通信**：HTTP 桥（`127.0.0.1:8790`），iQuant 客户端内桥策略受理下单/查单/持仓/资金 + 行情拉取，端点见计划 0009
- **信号优先级**：风控信号（止损/止盈/移动止损）> 公式信号（OPEN/ADD/REDUCE/CLOSE）
- **资金模型**：策略资金占比是持仓上限（非预分），多策略上限之和可超过 100%
- **T+1 约束**：回测强制 T+1，实盘实时查可用股票
- **熔断规则**：max_drawdown 触发 → 次日恢复（累计触发 3 次后转手动恢复）；daily_loss_limit 触发 → 当日暂停，次日恢复。熔断期间不清仓，仅暂停新开仓
- **主从策略**：从策略只能买入主策略当前持有的同一只股票；主策略清仓（含该股）后从策略不可新开仓但存量可自行卖出
- **一个 5m bar 可能同时触发多个周期**（如 10:30 同时触发 5m+30m+60m 公式）
- **多组合策略实盘**：一个 live session 可含多个组合策略，共享 iQuant 账户下单，各组合策略独立维护虚拟持仓（Per Portfolio）和虚拟现金（基于成本，非市值）。Core 重启时从 `live_trades` 按 `portfolio_strategy_id` 聚合重算虚拟持仓和虚拟现金
- **回测仅支持单组合策略**：每次回测针对一个 `portfolio_strategy_id`，多组合策略交互行为仅在实盘体现

## 模块复用

引擎层约 97% 的代码回测/实盘共用（风控、信号排序、资金审批、持仓更新）。差异通过策略模式隔离：

| 接口 | 回测实现 | 实盘实现 |
|------|----------|----------|
| `OrderDispatcher` | `SimulatedDispatcher` 按 next_bar.open 模拟成交 | `HttpBridgeDispatcher` 通过 HTTP 桥向 iQuant 下单 |
| `T1Checker` | `SimulatedT1Checker` 直接返回持仓量 | `LiveT1Checker` 查 iQuant 实际可用股数 |

执行引擎 `ExecutionEngine` 持有这两个接口，不感知具体实现。

## 关键路径与配置文件

| 路径 | 说明 |
|---|---|
| `main/core/main.py` | FastAPI 入口 |
| `main/core/engine/` | 自研回测/交易框架（事件驱动，polars 核心） |
| `main/core/tq/` | 通达信 TQ 模块（嵌入 Core 同进程） |
| `live/iguant_gateway/` | 国信 iQuant 网关（Python 3.7） |
| `web/` | Vue 3 + Vite 前端 |
| `config.yaml` | 系统配置（TDX 路径、iQuant 路径等） |

## 测试注意事项

- 后端测试需要 `conftest.py` 提供测试数据库 fixtures（使用独立 PostgreSQL 测试库或内存 SQLite）
- 前端测试在 `web/src/__tests__/` 目录
- 引擎模块的单元测试应使用 Mock 数据，无需连接通达信
- 集成测试需小数据集端到端验证