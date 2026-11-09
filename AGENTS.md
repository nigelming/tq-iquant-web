# 创懿私募基金量化投资平台 — AGENTS.md

## 项目状态

**已实现（脚手架 + 主链路已通）** — 24 个模型文件、48 个引擎源码文件、9 个 API 路由模块、10 个前端视图、41 个 Alembic 迁移均已落地；回测/实盘主链路已接线。完整实现状态见 [CLAUDE.md](CLAUDE.md)「实现状态」小节。本文档记录业务规则与设计依据，实现细节以代码与 CLAUDE.md 为准。

**单用户系统**：无用户鉴权，所有接口无需登录。

## 仓库

`github.com/nigelming/tq-iquant-web`

## 核心架构（4 模块）

```
Core (main, Python 3.13) ──HTTP──→ QMT 桥策略 (live/bridge/iquant_bridge.py, 8790/STOCK, QMT 客户端内 Python 3.6.8)
                          ──HTTP──→ 掘金桥策略 (live/bridge/goldminer_bridge.py, 掘金终端进程内, 127.0.0.1:7801)
  ├─ 同进程嵌入 TQ 模块（通达信，直接函数调用；tdx/ths/alert 取数 + 掘金行情快照合成）
  └─ 同花顺 UI 自动化（xiadan.exe，键鼠 + 文件交接）
Web 前端 (Vue 3 + Vite + Pinia) ←HTTP/SSE→ Core (FastAPI)
```

**关键约束**：两个独立的 uv 虚拟环境。`main/` 用 Python 3.13；`live/` 目录下 QMT 桥在 QMT 客户端自带 Python 3.6.8 内运行（桥代码须兼容 3.6，纯标准库实现），掘金桥在掘金终端使用的系统 Python 内运行。

> **Python 版本说明**：QMT 通道自带 Python 3.6.8，桥策略直接在该解释器内运行（不引入 nats-py 等需 3.7+ 的第三方库，HTTP 层用标准库 `socket` 手写）。

> **架构变更（2026-08）**：原 NATS 网关方案已废弃，Core↔QMT 改为 HTTP 桥。现行为 `init` 只做初始化并注册 `C.schedule_run(tick, ...)` 定时回调后立即返回（禁止在 `init` 内写阻塞死循环，否则客户端会挂起桥）。详见 [docs/plans/0009-iquant-http-bridge.md](docs/plans/0009-iquant-http-bridge.md)。原 `live/iguant_gateway/` NATS mock 网关已删除。

> **shared 包兼容性**：`shared/` 包被 main(3.13) 和 live(3.7) 共同引用，代码必须兼容 Python 3.7（不可用 walrus `:=`、match、pydantic v2 等）。数据结构用 dataclasses 或 pydantic v1。

> **时区**：所有时间按 Asia/Shanghai 处理，bar 时间判断基于上海时间。

> **数据库迁移**：使用 Alembic 管理 schema 变更，禁止手动改表结构。

## 开发命令

| 命令 | 说明 |
|---|---|
| `uv run pytest core/tests/` | 后端测试（main 环境） |
| `uv run pytest core/tests/unit/` | 仅单元测试 |
| `uv run pytest core/tests/integration/` | 仅集成测试 |
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

## 数据库（SQLite）

详见设计文档 5.4 节。关键点：
- 纯单用户本地工具，统一用 SQLite（`main/data/dev.db`），零配置，不切 PostgreSQL
- `config.yaml`（项目根目录）存储系统路径配置，无数据库密码（SQLite 本地文件）
- 每日快照 `backtest_daily_snapshots` 是评估指标的原始数据来源
- `backtest_evaluations` 18 个指标由 Evaluator 从快照序列计算
- `backtest_records.params_snapshot` 冻结回测时的策略参数，确保结果可复现
- 实盘交易记录：`live_orders`（订单状态跟踪）+ `live_trades`（成交记录）
- 多组合策略实盘：`live_session_portfolios` 关联表，一个 session 可含多个组合策略，共享同一资金账户（实际账户由所选引擎通道决定），各组合策略独立维护虚拟持仓和虚拟现金

## 并发模型

- **回测**：同步内联执行（在请求处理线程内跑 `BacktestEngine.run`）+ 全局锁 `_BACKTEST_LOCK`，同一时刻最多 1 个回测运行（并发启动返回 HTTP 409）。前端通过记录状态轮询获取进度。
- **实盘**：`BarPoller` 同步轮询所选引擎行情源（`tdx` / `ths` / 预警 / `goldminer` 走通达信快照合成，`iquant` 走 QMT HTTP 桥），`_loop`（60s 主循环：bar/approve/submit/超时收割）与 `_deals_loop`（5s 成交回填）各自投**独立单 worker `ThreadPoolExecutor`**（非 `run_coroutine_threadsafe`——TQ 回调线程方案已废弃，实际为 HTTP 拉取轮询），两池真并发。
- **实盘记账锁**：跨池记账（`_pending_orders` 增删/遍历、backfill/apply、持仓现金更新）由引擎级 `_book_lock`（`threading.RLock`）在调用点串行化——覆盖 `_tick_main` 超时收割段（expire + cancel_unfilled + commit）、收盘清扫（`_maybe_close_sweep`）、deals 回填（`_tick_deals`）与 `_handle_bar` 的在途单遍历；锁序恒为 `_book_lock` → `get_tdx_lock`，无反向获取，无死锁。start/stop 接口另有 `_START_LOCK`（asyncio）串行化 check-then-act 窗口，防双引擎并发下单。
- FastAPI 主事件循环不能被同步 I/O 阻塞（行情/订单轮询走线程池 worker）。

## 业务规则（容易遗漏）

- **股票代码格式**：统一带后缀（如 `000001.SZ`），通达信规范
- **复权方式**：回测支持三档复权模式（`dividend_mode`：`front` 前复权 / `back` 后复权 / `dual` 前复权信号+后复权记账，推荐）。TDX 前复权为「加性」且锚定查询 end 日期，长区间下高分红股（如 601225.SH）2020 年前 Close 被整体减到 ≤0/近零（最低 −5.44）——取数层已过滤 `Open/Close≤0` 与近零价（<0.01）伪影行，长区间回测建议用 `back`/`dual` 而非 `front`
- **TQ 数据传递**：polars DataFrame 进程内传递，通过 tqcenter SDK 直连运行中通达信
- **Core↔交易通道通信**：QMT 通道走 HTTP 桥（`127.0.0.1:8790`，账号 410038214257 / `STOCK`；QMT 是四条生产通道之一、平台按生产标准维护，但**该账户是 QMT 仿真账户、账户内为仿真资金**，不是真实资金；切真实资金须改桥文件 `ACCOUNT` / `ACCOUNT_TYPE`（必要时 `TICK_TAG` / `DRY_RUN`）并重跑实盘验证。`iquant_bridge_live.py` 的 `8791` + 信用账户 `CREDIT` 保留但**非生产、未启用**），掘金走 HTTP 桥（`127.0.0.1:7801`）；桥策略受理下单/查单/持仓/资金，QMT 桥另提供行情拉取（掘金行情由平台走通达信快照合成）。端点见 [live/bridge/README.md](live/bridge/README.md)
- **信号优先级**：风控信号（止损/止盈/移动止损）> 公式信号（OPEN/ADD/REDUCE/CLOSE）
- **ADD 加仓阈值特殊值**：`add_position_threshold = -1` 表示跳过回撤检查，只要有 ADD 信号即加仓（含上涨/大涨）；`max_add_count` 加仓次数上限仍生效。正常取值 1%~20%（逢跌加仓，现价较成本下跌 ≥ 阈值才加）。0 表示"不涨即加"（drop ≥ 0）。风控优先级仍高于 ADD（涨到止盈点先止盈清仓，不加）
- **资金模型**：策略资金占比是持仓上限（非预分），多策略上限之和可超过 100%
- **T+1 约束**：回测强制 T+1，实盘实时查可用股票
- **熔断规则**：max_drawdown 触发 → 次日恢复（累计触发 3 次后转手动恢复）；daily_loss_limit 触发 → 当日暂停，次日恢复。熔断期间不清仓，仅暂停新开仓
- **主从策略**：从策略只能买入主策略当前持有的同一只股票；主策略清仓（含该股）后从策略不可新开仓但存量可自行卖出
- **一个 5m bar 可能同时触发多个周期**（如 10:30 同时触发 5m+30m+60m 公式）
- **多组合策略实盘**：一个 live session 可含多个组合策略，共享同一资金账户下单（实际账户由所选引擎通道决定），各组合策略独立维护虚拟持仓（Per Portfolio）和虚拟现金（基于成本，非市值）。Core 重启时从 `live_trades` 按 `portfolio_strategy_id` 聚合重算虚拟持仓和虚拟现金
- **回测仅支持单组合策略**：每次回测针对一个 `portfolio_strategy_id`，多组合策略交互行为仅在实盘体现

## 模块复用

引擎层核心逻辑（风控、信号优先级排序、资金审批、持仓更新）回测/实盘共用，集中在 `portfolio.py`/`risk_manager.py`/`strategy_context.py`。实盘独有逻辑（对账告警、熔断次日恢复、SSE 事件流、周期边界 1m/1d 14:30/1w·1mon 处理）在 `live_engine.py`。差异通过策略模式隔离下单与 T+1 检查：

| 接口 | 回测实现 | 实盘实现 |
|------|----------|----------|
| `OrderDispatcher` | `SimulatedDispatcher` 按 next_bar.open 模拟成交 | `HttpBridgeDispatcher`（QMT `iquant` / 掘金 `goldminer`，同构协议）/ `TdxTqDispatcher`（通达信 TQ）/ `ThsUiDispatcher`（同花顺 UI 自动化），按 `live_sessions.engine` 选择 |
| `T1Checker` | `SimulatedT1Checker` 直接返回持仓量 | `LiveT1Checker` 查实际可用股数（各引擎同一口径：持仓表的「可用」列，0 为合法锁定值） |

执行引擎 `ExecutionEngine` 持有这两个接口，不感知具体实现。

**实盘交易引擎按会话可选**：`live_sessions.engine` ∈ `goldminer` / `tdx` / `ths` / `iquant`（旧 miniQMT 的 `qmt` 键已不在白名单；历史 `qmt` 会话与 NULL 会话均由 `session_engine` 回退到全局默认）。全局默认解析顺序：`tq_trade.default_engine` 优先（SystemConfig 可配，默认 tdx）；未配置/非法时回退旧行为（`ths_ui.enabled` 且依赖可用 → ths，否则 iquant）。旧 `qmt` 会话不会自动改指 `iquant`（miniQMT 与 QMT 是不同终端/账户，避免打到非预期账户）。引擎未就绪（客户端未启动/未登录/依赖缺失、桥策略未运行、未配置路径账号）时 `/sessions/{id}/start` 直接返回 503 并给出可操作中文提示，**不会静默回退到其它通道**。委托认领：`supports_remark=False` 的通道（`TdxTqDispatcher`、`ThsUiDispatcher`）走 legacy 模糊匹配（代码+方向+数量+提交时间窗；陌生 ref 补记的时间标记 `insert_fabricated`，匹配时降级排除，防遗留单误绑）；`supports_remark=True` 的 HTTP 桥（`iquant`、`goldminer`，同一 `HttpBridgeDispatcher`）把确定性 bridge_order_id 写进柜台 remark，按 remark 精确认领。撤单能力：`TdxTqDispatcher` / `ThsUiDispatcher` 实现 `cancel_order`；`HttpBridgeDispatcher` 未实现，QMT/掘金超时在途单只走 expire（柜台可能仍挂单，需人工撤），`requote` 追价重下对桥通道不生效。

**同花顺文件交接层（THS file_builtin，默认）**：同花顺通道是纯 UI 自动化，`ThsUiDispatcher` 双模式 `execute_mode` ∈ `file_builtin`（默认）/ `direct`（同步直呼，原行为）。file_builtin 下：引擎 signal/decision/risk 事件同步落 `{ths_ui.work_dir}/{session_id}/` 下 `signals.jsonl`/`risk.jsonl`（LiveEngine `_emit` 钩子，写失败仅告警）；`place_order` 只做参数校验后把指令追加进 `orders.jsonl` 并**秒回 None**（LiveEngine 按 `last_enqueued_order_id` 把 None 识别为「已入队」，订单保持 submitted 等回填，不判 approval failed）；内置后台执行线程（daemon，惰性启动，`execute_interval_seconds` 默认 5s）按游标（`cursor.json`：消费行数 + done 幂等表）读指令逐单执行 UI 自动化，回执落 `results.jsonl`（accepted+合同编号 / rejected+错误详情），成功单补记 `_known_refs`/`_submit_times`（批次 A 口径）。OCR/验证码守卫移入执行器（file 模式 place_order 不做 OCR 前置拒单，direct 模式维持），OCR 未启用不再阻断 file 会话启动。THS 撤单已自动化（撤单页定位目标委托行后双击；失败 fail-closed，不误报成功）：file 模式超时在途单仍走 180s expire rejected；读后游标即推进（at-most-once），执行前崩溃的单不重复执行，由 expire 兜底。执行器 rejected 回执由引擎 deals 轮询即时消费（`execution_receipts()` 弹出 → 在途单即时 rejected + order 事件，工作台秒级可见），不等 180s expire。**执行契约 schema 文档**（四文件/游标逐字段定义、seq 语义、执行器要求、通道接入指南、版本策略）见 [docs/execution-contract.md](docs/execution-contract.md)。

**同花顺 UI 键入真机校准（2026-10-28 实证，勿回退）**：xiadan 下单页字段为自绘控件（WM_GETTEXT/ValuePattern/剪贴板均不可读、UIA 焦点报告与可见字段不对应），键入必须走「**鼠标点击进字段 + kb_send（SendInput）**」：①字段定位 UIA control_id 实测 代码=1032、价格=1033、数量=1034；②代码框是「输入区+下拉按钮」组合，点击必须落**输入区左起 8px**（中心点点到下拉按钮、键入丢失，像素差异扫描校准）；③代码键入后**等待 4s**——THS 异步用最新价覆盖价格框（真机实证委托价 4.415≠键入 4.153）；④`_key` 每次 `_focus()` 重置主窗口焦点打掉字段焦点、`Timings.fast()` 丢字符——键入路径禁用；⑤撤单=撤单页**双击委托行**（真机实证有效，页面提示「双击或回车即可撤单」；开盘前委托未报不可撤）；⑥窗口级键入会被 THS 热键层吃掉（整窗 OCR 实证屏幕零变化）。

## 关键路径与配置文件

| 路径 | 说明 |
|---|---|
| `main/core/main.py` | FastAPI 入口 |
| `main/core/engine/` | 自研回测/交易框架（事件驱动，polars 核心） |
| `main/core/tq/` | 通达信 TQ 模块（嵌入 Core 同进程） |
| `main/core/engine/http_bridge_dispatcher.py` | HTTP 桥下单通道（通用：QMT 桥/掘金桥同构协议） |
| `main/core/engine/tdx_tq_dispatcher.py` | 通达信 TQ 下单通道（与回测取数同一 TQ 连接；确认弹窗自动处置） |
| `main/core/engine/ths_ui_dispatcher.py` | 同花顺独立交易 UI 自动化通道（file_builtin 文件交接层 + direct 直呼双模式） |
| `main/core/engine/live/file_handoff.py` | 执行契约文件交接层（通道无关；signals/risk/orders/results JSONL + cursor.json 游标） |
| `main/core/engine/live/win_dialog_scope.py` | 双引擎弹窗归属识别 + UI 自动化互斥锁（tdx/ths 并行防串扰） |
| `main/core/engine/live/snapshot_bars.py` | 快照合成 1m bar（TQ 盘中分钟线不可得的盘中数据源） |
| `live/bridge/` | HTTP 桥策略：`iquant_bridge.py`（QMT 通道生产桥，`127.0.0.1:8790`，账号 410038214257 / `STOCK`，**在用**；账户为 **QMT 仿真账户/仿真资金**，切真实资金须改 `ACCOUNT` / `ACCOUNT_TYPE` 并重跑实盘验证）/ `iquant_bridge_live.py`（`8791` + 信用账户 `CREDIT` 410009993017，**非生产、未启用**，仓库保留以维持镜像护栏）/ `goldminer_bridge.py`（掘金量化，掘金终端内运行，`127.0.0.1:7801`）。QMT 与掘金、同花顺、通达信并列为四条生产通道；Core 的 `simulation` / `live` 两个模式位均指向 8790 |
| `web/` | Vue 3 + Vite 前端 |
| `config.yaml` | 系统配置（TDX 路径、`ths_ui` / `tq_trade` / `goldminer` / `iquant_bridge` 交易通道） |

## 掘金量化桥（2026-11-04 接入，与 QMT 桥并存）

- **架构**：桥策略 `goldminer_bridge.py` 跑在**掘金终端进程内**（掘金终端用系统
  Python 解释器执行策略，gm SDK 装在**系统 Python 的 site-packages**，与终端
  本地服务 gmterm-serv v5.2.158 协议天然匹配），实现 QMT 桥同构 HTTP 协议
  （`127.0.0.1:7801`），平台 HttpBridgeDispatcher 零改动接入。
- **环境纯净规则（强制）**：**禁止把 gm SDK 安装进平台 venv（main/.venv）**——
  PyPI gm 3.0.192 的 C SDK（NATS 协议）与 goldminer3 终端 v5.2 服务协议不匹配
  （get_cash 无限挂起实证），且其依赖链（protobuf==3.20.x / onnxruntime 降级 /
  typing / wheel / cachetools）会污染平台环境（2026-11-05 清理实证，备份
  `main/data/backup/pyproject.toml.*.bak`）。掘金集成只允许「终端内桥策略」模式。
- **启动方式规则（强制）**：桥策略**只能由掘金终端启动**（终端内点击运行）。
  **禁止用 `uv run` / 平台 venv 启动掘金桥**——那会把桥拉进项目 venv，下次
  `uv sync` 会把 gm 删掉，桥真断（Core 只 HTTP 访问 7801，从不 import gm，
  平台 venv 无 gm 不影响桥与 Core）。
- 引擎键 `goldminer`；桥策略必须保持运行（掘金终端里不停，终端关闭 = 桥停），
  停止 = 平台报单 rejected（expire 兜底）。

## 测试注意事项

- 后端测试需要 `conftest.py` 提供测试数据库 fixtures（内存 SQLite）
- 前端测试在 `web/src/__tests__/` 目录
- 引擎模块的单元测试应使用 Mock 数据，无需连接通达信
- 集成测试需小数据集端到端验证
## 修复与部署协作规则（2026-11-04 起）

- **代码修复后只做冒烟测试**：运行本次改动涉及的聚焦 pytest（`uv run pytest core/tests/unit/test_<涉及模块>.py`）/ `npx vitest run <相关测试>` 确认 pass 即止。
- **禁止由 AI 执行重启/部署**：杀后端进程、启动 `uvicorn`、重开实盘会话一律由用户择时手动执行（启动预热与 TQ 调用会冻结通达信客户端，AI 不掌握用户盘中节奏）。
- **AI 汇报格式**：改动文件清单 + 冒烟测试结果 + 「需要重启生效」标注；用户重启后如有验证需求，只做只读 API/日志检查。
- **全量测试**（`uv run pytest core/tests/unit/`，~80s，部分用例触真实 TQ）同样会卡通达信——仅在用户明确要求或停会话窗口执行。
