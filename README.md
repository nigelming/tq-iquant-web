# 创懿量化交易平台

连接通达信(TDX)回测数据与国信 iQuant 实盘交易的单用户量化系统。无鉴权，所有接口直接可访问。

## 架构

```
Core (main/, Python 3.13) ──HTTP──→ iQuant 桥策略 (live/bridge/, iQuant 客户端内 Python 3.6)
  └─ 同进程嵌入 TQ 模块（通达信，直接函数调用 + polars DataFrame）
Web 前端 (web/, Vue 3 + Vite) ←HTTP/SSE→ Core (FastAPI)
shared/ — tq_iquant_shared 包，被 main 和 live 共同引用（须兼容 Python 3.7）
```

- **`main/`** — FastAPI Core + 嵌入式 TQ 模块。事件驱动引擎（polars 核心），`uv` 管理依赖。
- **`live/`** — iQuant 客户端内运行的桥策略（HTTP 桥 `127.0.0.1:8790`，端点 `/ping`/`/order`/`/positions`/`/account`/`/orders`/`/deals`/`/quote`）。iQuant 自带 Python 3.6.8，桥代码须兼容 3.6（纯标准库，无 f-string）。
- **`shared/`** — `tq_iquant_shared` 包，通过 `[tool.uv.sources]` 路径依赖引入。
- **`web/`** — Vue 3 + Vite + TypeScript 前端，白色侧边栏 + 顶栏布局。

## 快速开始

### 后端（`main/`）

```bash
uv run uvicorn core.main:app --reload   # 启动 dev server（端口 8000）
uv run pytest                           # 全部测试
uv run alembic upgrade head             # 应用数据库迁移（SQLite: main/data/dev.db）
```

### 前端（`web/`）

```bash
npm install
npm run dev       # dev server（端口 5173，Vite proxy /api → localhost:8000）
npx vitest        # 前端测试
npm run build     # 构建到 dist/（生产期由 FastAPI 托管）
```

一键管理前后端：`./manage.ps1 start | stop | restart | status`

## 关键约定

- 统一响应格式 `{ "code": 0, "message": "ok", "data": {...} }`
- 股票代码统一带后缀（`000001.SZ`，通达信规范）；复权统一前复权；时间按 Asia/Shanghai
- 回测强制 T+1；实盘实时查可用股数
- 数据库：纯单用户本地 SQLite，零配置
- 通信拓扑：Core↔iQuant 为 HTTP 桥（原 NATS 网关方案已废弃），Core↔通达信同进程调用，Web↔Core 走 HTTP + SSE

## 文档索引

- [AGENTS.md](AGENTS.md) — 权威业务规则、API 规范、并发模型与数据库设计
- [CLAUDE.md](CLAUDE.md) — 实现状态、关键约定、开发范式
- [docs/system-plan-draft.md](docs/system-plan-draft.md) — 系统总体设计（44 个接口定义、数据库 schema）
- [docs/implementation-plan.md](docs/implementation-plan.md) — 实施计划
- [docs/live-flow-checklist.md](docs/live-flow-checklist.md) — 实盘流程清单
- [docs/plans/0009-iquant-http-bridge.md](docs/plans/0009-iquant-http-bridge.md) — iQuant HTTP 桥计划
