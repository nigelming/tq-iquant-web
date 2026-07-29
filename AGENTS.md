# 创懿量化交易平台 — AGENTS.md

## 项目状态

**Greenfield** — 仅存在设计文档 `docs/system-plan-draft.md`，无实际代码。所有实现均需从零开始。

## 仓库

`github.com/nigelming/tq-iquant-web`

## 核心架构（4 模块）

```
Core (main, Python 3.13) ──NATS──→ iQuant Gateway (live, Python 3.6.8)
  └─ 同进程嵌入 TQ 模块（通达信，直接函数调用，不走 NATS）
Web 前端 (Vue 3 + Vite) ←HTTP→ Core (FastAPI)
```

**关键约束**：两个独立的 uv 虚拟环境。`main/` 用 Python 3.13，`live/` 用 Python 3.6.8。

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
- 认证：Session（cookie 自动携带）
- 统一返回格式：`{ "code": 0, "message": "ok", "data": { ... } }`（code=0 成功，非 0 错误）
- 43 个接口，11 组（详见设计文档 5.6 节）

## 数据库（SQLAlchemy ORM，13 张表）

详见设计文档 5.4 节。关键点：
- `config.yaml`（项目根目录）存储系统路径配置，**不存数据库**
- 每日快照 `backtest_daily_snapshots` 是评估指标的原始数据来源
- `backtest_evaluations` 18 个指标由 Evaluator 从快照序列计算

## 并发模型

- **回测**：`ProcessPoolExecutor` 提交子进程（CPU 密集），前端轮询获取状态。同一时刻最多 1 个回测运行。
- **实盘**：TQ 回调线程 → `asyncio.run_coroutine_threadsafe` → 主事件循环处理信号。
- FastAPI 主事件循环不能被 TQ 回调阻塞。

## 业务规则（容易遗漏）

- **股票代码格式**：统一带后缀（如 `000001.SZ`），通达信规范
- **复权方式**：统一前复权
- **TQ 数据传递**：polars DataFrame 进程内传递（不走 NATS）
- **NATS 仅用于 Core↔iQuant 通信**，3 个 subject（下单/持仓查询/状态）
- **信号优先级**：风控信号（止损/止盈/移动止损）> 公式信号（OPEN/ADD/REDUCE/CLOSE）
- **资金模型**：策略资金占比是持仓上限（非预分），多策略上限之和可超过 100%
- **T+1 约束**：回测强制 T+1，实盘实时查可用股票
- **熔断规则**：max_drawdown 触发 → 次日恢复；daily_loss_limit 触发 → 当日暂停，次日恢复。熔断期间不清仓，仅暂停新开仓
- **主从策略**：从策略只能在主策略持有股票时买入
- **一个 5m bar 可能同时触发多个周期**（如 10:30 同时触发 5m+30m+60m 公式）

## 关键路径与配置文件

| 路径 | 说明 |
|---|---|
| `main/core/main.py` | FastAPI 入口 |
| `main/core/engine/` | 自研回测/交易框架（事件驱动，polars 核心） |
| `main/core/tq/` | 通达信 TQ 模块（嵌入 Core 同进程） |
| `live/iguant_gateway/` | 国信 iQuant 网关（Python 3.6.8） |
| `web/` | Vue 3 + Vite 前端 |
| `config.yaml` | 系统配置（TDX 路径、iQuant 路径等） |

## 测试注意事项

- 后端测试需要 `conftest.py` 提供测试数据库 fixtures（独立的 SQLite）
- 前端测试在 `web/src/__tests__/` 目录
- 引擎模块的单元测试应使用 Mock 数据，无需连接通达信
- 集成测试需小数据集端到端验证