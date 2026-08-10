# 0008 实盘引擎 — 模拟撮合全链路打通

> 状态：设计已确认 + SDK 接口已验证，待实施
> 日期：2026-08-04
> 接续：回测链路已端到端打通（commit f6381e9/53e6865，174 后端测试绿）→ 本计划为实盘首期
> 验证：[0008-verify-results.md](0008-verify-results.md)（SDK 接口链路已实测确认，2026-08-04 盘中）
> 依据：[system-plan-draft.md](../system-plan-draft.md) §2.4 实盘信号流程 / §5.3.3 并发模型 / §5.3.2 第9条虚拟持仓 / 实盘恢复机制；用户 4 项决策（见下）

> ⚠️ **次期方案已变更（见 0009）**：本计划原定「次期 `NatsDispatcher` + iQuant NATS 网关」**已废弃**，改为「iQuant 客户端内 HTTP 桥（`HttpBridgeDispatcher`）」。NATS 通信拓扑整体移除。下文凡涉及 `NatsDispatcher`/NATS 网关的内容仅作历史记录，以 [0009-iquant-http-bridge.md](0009-iquant-http-bridge.md) 为准。

> ⚠️ **通达信分版已废弃（2026-08-06）**：本计划及验证记录中提到的「实盘版通达信 `D:\new_tdx64_live`」**已不再使用**。全系统统一用回测版 `D:\new_tdx64`，`tdx_live_path` 配置项已删除。下文凡涉及 `new_tdx64_live`/live 版/`tdx_live_path` 的内容仅作历史记录。

## 1. 目标

打通「TQ 实时通知 → 主动拉数 → 内存注入公式 → 信号 → 风控 → 模拟撮合 → 落库 → SSE 推送 → 前端监控」实盘全链路。**首期成交用 `SimulatedDispatcher` 本地撮合**（与回测同），不接 iQuant 真实下单。一个会话跑多个组合（1:N），各组合虚拟持仓/虚拟现金隔离。落地后实盘具备「能跑能看能恢复」的产品级状态，与回测对称。

**不在本计划范围（次期）**：
- `NatsDispatcher` + iQuant 网关真实化 + 真实下单/撤单/持仓查询
- 多周期合成（1m→5m→30m/60m 整除点合成，§2.4 c/d/e）—— 首期单周期
- 1d/1w 周期实盘（15:00 收盘合成 + 跨日 pending）—— 首期仅分钟级
- 实际账户现金 NATS 查询、虚拟持仓 vs 实际持仓交叉验证（依赖 iQuant）

## 2. 已确认决策（用户）

| 决策点 | 选择 | 落地方式 |
|---|---|---|
| 行情/信号驱动 | **TQ 实时回调 → 按周期触发 TQ 公式**（原表述「bar 追加本地文件」经验证改为 `formula_set_data` 内存注入，见[验证结论](0008-verify-results.md)风险点2/3） | `TQData.subscribe_bars` 注册 `subscribe_hq` 回调；回调线程只做 `run_coroutine_threadsafe(on_hq(code))` 投递；主循环 `on_hq` 收通知后 `run_in_executor` 调 `get_market_data`+`formula_set_data`+`formula_process_mul_zb`（持 `_tdx_lock`） |
| 成交方式 | **先模拟撮合，后接 iQuant** | 首期复用 `SimulatedDispatcher`；`OrderDispatcher` 接口预留，次期实现 `NatsDispatcher` |
| 会话×组合 | **1:N 一个会话跑多个组合** | 一个 `LiveEngine` 实例持有 `List[Portfolio]`；`LiveSessionPortfolio` 表关联；虚拟持仓按 `portfolio_strategy_id` 隔离 |
| 首期范围 | **打通模拟实盘全链路** | 4 切片：引擎主链路 → 多组合落库 → SSE → 前端监控；收尾加恢复机制 |

## 3. 核心架构与数据流

> ⚠️ 本节已按 [验证结论](0008-verify-results.md) 修订：实盘驱动是「**通知驱动 + 主动拉数**」，非 bar 推送、非写本地文件。`subscribe_hq` 仅发 `{Code,ErrorId}` 更新通知，真正 OHLCV 需收到通知后主动 `get_market_data` 拉；公式数据经 `formula_set_data` **内存注入**，非写 `.day` 文件。

```
通达信进程（live 模式 TDX 连接，config.yaml: tdx_live_path）
    │
    │ tq.subscribe_hq(stock_list=并集, callback=on_hq)  ← TQData.subscribe_bars
    ▼
TQ 回调线程（通达信驱动，微秒级返回，不做业务）:
    1. 收到 hq 通知 {"Code":"000001.SZ","ErrorId":"0"}  ← 仅告知该股票行情有更新，无 OHLCV
    2. asyncio.run_coroutine_threadsafe(engine.on_hq(code), main_loop)
    3. 立即返回
    │
    ▼
主事件循环 on_hq(code):  ← LiveEngine.on_hq（新增，替代桩 on_bar）
    1. loop.run_in_executor(None, pull_and_compute, code)  ← 线程池，持 _tdx_lock
       a. get_market_data(code, period, count=N) → 拉最新 bar（OHLCV DataFrame）
       b. formula_format_data → formula_set_data(type=0 内存注入) → formula_process_mul_zb 算公式
          → 得 signal_cache 片段 {(sid, code, bar_time): [{name,value}]}
    2. 构造 BarEvent（用 get_market_data 拉回的最新 bar）
    3. 先撮合 pending_orders：用本 bar 的 open 价 → SimulatedDispatcher → TradeEvent
       （上一 bar 触发的订单在本 bar open 成交，与回测 §5.3.2 第8c条同构）
    4. for portfolio in self.portfolios:
         orders = portfolio.on_bar(bar, signal_cache)  ← 复用回测核心，纯内存
         pending_orders.extend(orders)
    5. 成交的 TradeEvent → 写 live_trades + live_orders → 推 SSE 事件
    6. 日终（15:00 或 bar 跨日）→ snapshot 落库 + risk_manager.update
```

**与回测的对称性**：回测 `BacktestEngine.run` 的 times 循环「①撮合 pending → ③on_bar 产新 pending → ④快照 → ⑤熔断」五步，在实盘被「hq 通知 → 主动拉 bar」驱动，**同构**。区别仅在于：
- 数据来源：回测预加载 klines，实盘 hq 通知后 `get_market_data` 拉最新 bar
- 信号来源：回测预计算 signal_cache，实盘实时 `formula_set_data` 内存注入 + `formula_process_mul_zb` 算
- 成交：回测 `SimulatedDispatcher`，实盘首期同（次期 `NatsDispatcher`）
- 持久化：回测跑完批量落库，实盘每笔成交即时落库

## 4. 业务规则（出处：[system-plan-draft.md](../system-plan-draft.md) §5.3.2 / §2.4 / §5.3.3）

### 4.1 复用回测已实现（不改）
- **信号优先级**：风控（止损/止盈/移动止损）> 公式；公式内 CLOSE>REDUCE>ADD>OPEN —— 已在 `Portfolio.on_bar`/`_signal_priority`
- **主从策略**：从策略 OPEN 只能买主策略当前持有的同一只股票；主清仓后从不可新开仓但存量可卖 —— 已在 `Portfolio._signal_to_order`（§89 修复）
- **资金模型**：策略资金占比=持仓上限（非预分），多策略上限之和可超 100%；资金不足按剩余金额等比例缩减，不足 1 手放弃 —— 已在 `Account.approve_order`
- **熔断**：max_drawdown 次日恢复（累计 3 次转手动）、daily_loss_limit 当日暂停次日恢复、熔断期间不清仓仅暂停新开仓 —— 已在 `PortfolioRiskManager` + `Portfolio.on_bar` 剥 BUY
- **T+1**：模拟撮合阶段复用 `SimulatedT1Checker`（按 `Position.buy_time` 判断）；次期接 iQuant 用真实可用股数查询
- **信号来源透传**：`signal_name`（公式变量名/风控名）已端到端透传 SignalEvent→OrderEvent→TradeEvent（commit f6381e9），实盘沿用，落 `live_orders.signal_name`/`live_trades`（表已带列）

### 4.2 实盘新增规则
- **成交时机**：信号在当前 bar 结束时触发 → 下一 bar 的 open 成交（§5.3.2 第3条/第8c条）。`LiveEngine` 维护 `pending_orders` 队列，收到新 bar 时先用其 open 撮合上一 bar 的 pending
- **通知驱动 + 主动拉数**（验证结论风险点3）：`subscribe_hq` 回调仅 `{Code,ErrorId}` 更新通知，无 OHLCV。`LiveEngine.on_hq(code)` 收到通知后调 `get_market_data(code, period, count=N)` 主动拉最新 bar，再喂公式。这是「拉模式」而非「推模式」，每次通知多一次 RPC，但拿到的是完整 OHLCV（更可靠）
- **公式数据内存注入**（验证结论风险点2）：`formula_format_data`（OHLCV DataFrame → 公式格式）+ `formula_set_data(type=0)`（`dll.TdxFuncMain` 内存注入）+ `formula_process_mul_zb(type=4)`（算公式）。**SDK 无写本地 `.day` 文件 API**；用户「写文件触发公式」意图对应的是 `formula_set_data` 内存注入
- **虚拟持仓**（§5.3.2 第9条，仅实盘）：每个组合独立维护 `(stock_code, quantity, avg_cost)` 于 `Portfolio.account`/各 `StrategyContext.positions` 内存中，不持久化；以 `live_trades` 为唯一数据源，恢复时 SQL 聚合重算
- **虚拟现金**：`virtual_cash = initial_capital - Σ(买入金额+费用) + Σ(卖出金额-费用)`，每组合独立，内存维护，恢复时从 `live_trades` 重算。**首期模拟撮合不接实际账户**，虚拟现金即权威
- **多组合股票订阅并集**（§2.3 第9条）：所有组合×所有策略涉及的股票取并集，去重后统一 `subscribe_hq`；收到通知后按组合→策略分发。**100 只硬上限**（验证结论附）：并集超 100 需分批订阅，首期不触及
- **mode 级互斥**（§2.3 第8条 + 验证结论风险点1）：实盘用 `tdx_live_path`，回测用 `tdx_backtest_path`。`tqcenter.tq` 是单例 `@classmethod`，同进程只能持一个 TDX 目录连接，**import 后不可运行时切换**。回测在 `ProcessPoolExecutor` 子进程（独立 `sys.path`）天然隔离；实盘在主进程持 live 连接。**现有架构满足**，无需额外隔离机制
- **回调线程约束**（§5.3.3）：只做「投递协程」，不做公式计算/拉数/业务逻辑，确保微秒级返回；拉数 + 公式计算 `run_in_executor` 到线程池，所有 TQ 调用持 `_tdx_lock`

## 5. 关键复用 vs 新增

| 组件 | 来源 | 处置 |
|---|---|---|
| `Portfolio.on_bar` | 已实现 | **直接复用**，回测/实盘共用核心 |
| `_assemble_portfolio` | [backtest.py:453](main/core/api/backtest.py#L453) | **抽取到共享处**，live 加载组合复用（避免循环依赖，移至 `core/engine/loader.py` 或 live api 内复刻） |
| `SimulatedDispatcher`/`SimulatedT1Checker` | 已实现 | **直接复用**，首期模拟撮合 |
| `Evaluator` | 已实现 | **复用**，实盘日终快照评估（次期按需） |
| `StrategyContext.get_signal` | 已实现 | **复用**，`tq_compute` 回调兜底实时算公式 |
| `TQData.subscribe_bars` | 已实现 | **复用**入口；回调签名对齐（见 §7 风险点3：`subscribe_hq` 回调仅 `{Code,ErrorId}` 通知） |
| `TQFormula.compute` | 已实现 | **复用**，实时算公式（持锁） |
| `LiveEngine` | 桩 4 方法 | **重写**：`start/stop/on_hq/recover`（`on_hq` 收通知后拉数+算公式+撮合+on_bar） |
| `NatsDispatcher` | 不存在 | **次期实现**；首期不建，`OrderDispatcher` 接口已就绪 |
| `start_session` API | 只改状态 | **重写**：起 `LiveEngine` 协程 |
| `session_stream` SSE | 只发 ping | **重写**：推行情/订单/净值事件 |
| `LiveSessions.vue` | 69 行骨架 | **重写**：补详情/监控/日志 |
| `SignalEngine`/`EventBus` | 搁置未用 | **不引入**，`Portfolio.on_bar` 已内联优先级逻辑且经回测验证 |
| `live_snapshots` 表 | 不存在 | **首期不加**，日终快照暂存内存 + 前端轮询；若需净值曲线历史，次期加表 |
| `tdx mode 切换` | `get_tq` 单例无 mode | **首期补**：`get_tq(mode)` 按 mode 注 `tdx_backtest_path`/`tdx_live_path`（验证结论已确认子进程隔离满足，无需运行时切换） |
| `TQData.pull_bar_and_compute` | 不存在 | **首期新增**：`get_market_data`→`formula_format_data`→`formula_set_data`→`formula_process_mul_zb` 组合调用（验证结论风险点2/3） |

## 6. 关键文件

**后端**
- 重写 [main/core/engine/live_engine.py](main/core/engine/live_engine.py) — 桩改真实 `LiveEngine`（`start/stop/on_hq/recover` + `pending_orders` 队列 + 多组合持有；`on_hq` 收 `{Code,ErrorId}` 通知后拉数+算公式+撮合+on_bar）
- 新建 `main/core/engine/live_runtime.py` — `LiveEngineManager`：单例，管理多 session 的引擎生命周期、TQ 订阅并集、回调分发（Core 启动时调 `recover`）
- 改 [main/core/tq/utils.py](main/core/tq/utils.py) — `get_tq(mode="backtest")` 按 mode 注入对应 `tdx_path`；`_tdx_lock` 保留全局（验证结论已确认回测子进程隔离满足，无需运行时切换目录）
- 改 [main/core/tq/data.py](main/core/tq/data.py) — `subscribe_bars` 回调签名对齐（回调收到的是 `{Code,ErrorId}` 通知，非 bar）；**新增 `pull_bar_and_compute(code, period)`**：`get_market_data`→`formula_format_data`→`formula_set_data`→`formula_process_mul_zb` 组合，返回 `(BarEvent, signal_cache片段)`
- 改 [main/core/api/live.py](main/core/api/live.py) — `start_session`/`stop_session` 接 `LiveEngineManager`；`session_stream` 推真实事件；补 `GET /sessions/{id}/trades`、`GET /sessions/{id}/positions`（前端监控用）
- 新建 `main/core/engine/loader.py`（或 live api 内）— 从 `backtest.py` 抽取 `_assemble_portfolio`/`_portfolio_strategies`/`_signal_type_from_str` 供回测+实盘共用
- 不改模型（`LiveSession`/`LiveSessionPortfolio`/`LiveOrder`/`LiveTrade` 已完备）；**不加 Alembic 迁移**（无 schema 变更）

**后端测试**
- 新建 [main/core/tests/unit/test_live_engine.py](main/core/tests/unit/test_live_engine.py) — Mock TQ 回调，验证 handle_bar 主链路
- 新建 `main/core/tests/unit/test_live_runtime.py` — 多 session/多组合生命周期、订阅并集、恢复重算
- 改 [main/core/tests/integration/test_live_api.py](main/core/tests/integration/test_live_api.py)（若不存在则新建）— start/stop/trades/positions/stream 端到端

**前端**
- 重写 [web/src/views/LiveSessions.vue](web/src/views/LiveSessions.vue) — 列表+新建(选多组合)+启停+详情监控(净值/持仓/订单/日志/SSE 接收)
- 改 [web/src/api/index.ts](web/src/api/index.ts) — 封装 live 端点（当前裸 axios），补 `LiveSession`/`LiveTrade`/`LivePosition` 类型
- 新建 [web/src/__tests__/LiveSessions.test.ts](web/src/__tests__/LiveSessions.test.ts) — 列表/启停/详情三态

## 7. 风险点与待验证

> 验证脚本已确认风险点 1/2/3，详见 [0008-verify-results.md](0008-verify-results.md)。验证日期 2026-08-04 盘中。

1. **TDX mode 切换与互斥** — ✅ **已定论**（验证结论风险点1）：`tqcenter.tq` 是单例 `@classmethod`，`initialize(path)` 的 `path` 是连接标识非 TDX 目录，TDX 目录由 `sys.path` 注入哪个 `tqcenter.py` 决定，**import 后不可运行时切换**。**现有架构已满足**：回测在 `ProcessPoolExecutor` 子进程（独立 `sys.path`）持 backtest mode，实盘在主进程持 live mode，天然隔离。落地：`get_tq(mode)` 按 mode 注入对应 `tdx_path`，`_tdx_lock` 仍全局串行。
2. **公式数据喂数据闭环** — ✅ **已定论**（验证结论风险点2）：SDK **无写本地 `.day` 文件 API**。真实闭环是 `formula_format_data`（OHLCV DataFrame → 公式格式）+ `formula_set_data(type=0，dll.TdxFuncMain 内存注入)` + `formula_process_mul_zb(type=4 算公式)`。用户「写文件触发公式」意图对应 `formula_set_data` 内存注入。落地：`TQData.pull_bar_and_compute(code, period)` 封装此三步组合调用。
3. **`subscribe_hq` 回调签名与频率** — ✅ **已实测**（验证结论风险点3）：回调参数是 JSON 字符串 `{"Code":"000001.SZ","ErrorId":"0"}`，**仅更新通知，无 OHLCV/Price/Volume**。盘中约每 6-7 秒推送一次（9 次/60 秒，三只股票轮询）。`subscribe_quote`（单股 K 线回调）源码注释「暂无实际功能」不可用。落地：实盘驱动改为「**通知驱动 + 主动拉数**」——收到 hq 通知后 `get_market_data` 拉最新 bar。回调线程只做 `run_coroutine_threadsafe(on_hq(code))`，微秒级返回。
4. **pending_orders 跨 bar 时机**：实盘「下一 bar open 成交」——收到 bar(t) 时用其 open 撮合 pending。但若 pending 的股票在 bar(t) 无数据（停牌/未订阅），订单滞留。**处置**：pending 带过期时间（如 1 个交易日），超时撤销并记 `live_orders.status=expired`。
5. **时区与交易时段**：所有时间 Asia/Shanghai；`trading_session`（full/am/pm）约束 on_hq 仅在时段内处理。回调可能推送盘前/盘后数据，需按 `trading_session` 过滤。
6. **Core 重启漏 bar**（§恢复机制表）：通达信无历史实时 bar 查询接口，漏掉的 bar 不补算，从当前最新 bar 继续。**首期接受此限制**，恢复机制切片只做「虚拟持仓重算 + 重建订阅」，不补历史 bar。
7. **get_market_data 盘中实时性** — ⚠️ **环境问题**（验证结论风险点4）：实盘版通达信 `D:\new_tdx64_live\TdxW.exe` 进程在跑但今天未接收实时 1m 行情（数据停在 7/24），属环境问题非 SDK 接口问题（接口链路 ErrorId=0 已通）。**联调前置条件**：实盘版通达信需手动登录行情服务器、接收当日数据。开发期不依赖实时数据，单测用 Mock。
8. **subscribe_hq 100 只上限**（验证结论附）：源码硬上限 `订阅数大于100`。多组合股票并集超 100 需分批订阅或按优先级裁剪。**首期不触及**（单组合/少股票场景）。

## 8. 实现范围（全程 TDD，4 切片 + 收尾）

每切片：先写失败测试 → 实现 → 绿 → 回归。切片按依赖顺序，每切片产出可独立运行/验证。

### 切片 1：LiveEngine 单组合单周期主链路 + 模拟撮合（纯单测，Mock TQ）

**目标**：`LiveEngine.handle_bar` 跑通「撮合 pending → on_bar → 产新 pending → 落 live_trades」主链路，与回测 `BacktestEngine.run` 对称。不接 TQ、不接 API，纯引擎单测。

> **方法分层**：`on_hq(code)` 是 TQ 耦合入口（收通知 → `get_market_data` 拉数 → `formula_set_data` 算公式 → 调 `handle_bar`）；`handle_bar(bar, signal_cache)` 是纯 bar 处理内核（撮合+on_bar+落库），**可直接单测**（mock bar+cache 即可，不碰 TQ）。切片1只测 `handle_bar`，`on_hq` 的 TQ 拉数部分在切片3集成测。

测试 [test_live_engine.py](main/core/tests/unit/test_live_engine.py)，Mock 数据复用 [test_backtest_engine.py](main/core/tests/unit/test_backtest_engine.py) 的 `_klines`/`_portfolio_with_strategy` 模式：

| 测试 | 要点 |
|---|---|
| `test_handle_bar_buy_then_next_open_fill` | 推 bar1（触发 OPEN）→ pending 产 BUY 订单；推 bar2（open 价）→ 撮合 pending → TradeEvent 落 live_trades；assert 2 笔 trade、持仓正确 |
| `test_handle_bar_stop_loss_triggers` | 预置持仓 + 推 bar 触发止损 → pending 产 SELL；下一 bar 撮合 → 落库 |
| `test_pending_fill_skips_when_no_data` | pending 的股票在下一 bar 停牌（无数据）→ 订单滞留 pending，不撮合 |
| `test_signal_name_propagates_to_live_trade` | 实盘沿用透传：BUY 的 signal_name=open_sig，SELL 的 signal_name=stop_loss（与回测 commit f6381e9 对称） |
| `test_t1_blocks_same_day_sell` | 模拟撮合 T+1：当日买入当日卖 → 阻止 |
| `test_circuit_breaker_strips_new_buy` | 回撤破阈值 → 熔断 → 后续 OPEN 的 BUY 被剥 |

实现 `LiveEngine`：
```python
class LiveEngine:
    def __init__(self, session_id, portfolios: List[Portfolio], dispatcher: OrderDispatcher, t1_checker, db_factory, tq_data=None):
        self.session_id, self.portfolios, self._dispatcher, self._t1, self._db_factory = ...
        self._tq_data = tq_data   # 切片3接入；切片1单测传 None，直接调 handle_bar
        self.pending_orders: List[OrderEvent] = []
        self._running = False

    async def handle_bar(self, bar: BarEvent, signal_cache: dict) -> None:
        # 1. 撮合 pending（用 bar 的 open 价）—— 复用 ExecutionEngine
        # 2. for port in portfolios: orders = port.on_bar(bar, signal_cache) → pending.extend
        # 3. 成交落 live_trades/live_orders（通过 db_factory 拿 session 写库）
        # 4. 日终 → snapshot + risk_manager.update

    async def on_hq(self, code: str) -> None:
        # TQ 耦合入口（切片3实现）：tq_data.pull_bar_and_compute(code) → (bar, cache) → handle_bar

    async def start(self): self._running = True; await self._subscribe()
    async def stop(self): self._running = False; await self._unsubscribe()
    async def recover(self): 从 live_trades 聚合重算虚拟持仓/现金（切片5）
```
- `dispatcher` 注入 `SimulatedDispatcher`（首期）；接口已就绪，次期换 `NatsDispatcher` 不改 `handle_bar`
- 单测直接调 `await engine.handle_bar(bar, cache)`，不跑真实 TQ

### 切片 2：1:N 多组合 + 虚拟持仓隔离 + 即时落库

**目标**：一个 `LiveEngine` 持多个 `Portfolio`，各组合虚拟持仓/现金独立；每笔成交即时写 `live_trades`/`live_orders`。

| 测试 | 要点 |
|---|---|
| `test_multi_portfolio_positions_isolated` | 2 组合各持不同股，on_bar 后各组合持仓独立，互不串 |
| `test_virtual_cash_per_portfolio` | 组合A买入扣 A 虚拟现金，组合B现金不变 |
| `test_trade_persisted_to_live_trades` | 成交后 DB 有 `live_trades` 行，字段（portfolio_strategy_id/strategy_id/signal_name/amount）正确 |
| `test_order_persisted_with_status` | 订单落 `live_orders`，status=filled；模拟撮合 filled_quantity=quantity |
| `test_subscribe_union_stocks` | 2 组合股票池 {A,B,C} + {B,D} → 订阅并集 {A,B,C,D}（在 LiveEngineManager 测） |

实现：
- `LiveEngine.handle_bar` 遍历 `self.portfolios`，各组合独立 `on_bar`
- 落库函数 `_persist_live_trade(db, session_id, trade)` / `_persist_live_order`（复用回测 `_persist_result` 模式，改写 live 表）
- `LiveEngineManager.subscribe_union()`：聚合所有 session 所有组合的股票 → `TQData.subscribe_bars`

### 切片 3：start/stop API 接引擎 + SSE 真实推送

**目标**：`POST /sessions/{id}/start` 真起 `LiveEngine`（通过 `LiveEngineManager`）；`session_stream` 推真实事件（订单/成交/净值/日志）。

| 测试 | 要点 |
|---|---|
| `test_start_session_runs_engine` | POST start → session.status=running；`LiveEngineManager` 中有该 session 引擎 |
| `test_stop_session_halts_engine` | POST stop → status=stopped；引擎 `_running=False`；TQ 取消订阅 |
| `test_start_already_running_409` | 重复 start → code:409 |
| `test_stream_pushes_trade_event` | 撮合成交 → SSE 推 `event: trade` JSON；客户端能收到 |
| `test_stream_disconnect_stops_generator` | 客户端断开 → generator 退出 |
| `test_get_session_trades` | `GET /sessions/{id}/trades` 返回 live_trades 列表 |
| `test_get_session_positions` | `GET /sessions/{id}/positions` 返回各组合当前虚拟持仓 |

实现：
- `LiveEngineManager` 单例（FastAPI app lifespan 启动）：`start(session_id, portfolio_ids)` → 加载组合（复用 `_assemble_portfolio`）→ 建 `LiveEngine`（注入 `tq_data`）→ `subscribe_union` → `engine.start()`
- `on_hq` TQ 拉数集成：`LiveEngineManager` 注册 `subscribe_hq` 回调，回调线程只做 `run_coroutine_threadsafe(engine.on_hq(code), main_loop)`；`on_hq` 内 `tq_data.pull_bar_and_compute(code)`（`get_market_data`→`formula_format_data`→`formula_set_data`→`formula_process_mul_zb`）→ `(bar, signal_cache片段)` → `handle_bar`
- SSE：`LiveEngine` 持 `asyncio.Queue`，`handle_bar` 成交/快照后 `put` 事件；`session_stream` 消费队列推 `text/event-stream`
- 事件类型：`trade`/`order`/`snapshot`/`log`/`ping`（替代当前 30s ping）

### 切片 4：前端 LiveSessions 监控页

**目标**：`LiveSessions.vue` 从 69 行骨架升级为列表+新建(选多组合)+启停+详情监控（净值/持仓/订单/日志 + SSE 实时接收）。

测试 [LiveSessions.test.ts](web/src/__tests__/LiveSessions.test.ts)，模式同 [Backtest.test.ts](web/src/__tests__/Backtest.test.ts)：

| 测试 | 要点 |
|---|---|
| 列表渲染 | name/mode/status badge/组合数 |
| 新建弹窗 | 选多个组合（checkbox 多选，复用 getPortfolios） |
| 启停 | 点启动 → 调 startSession，状态变 running；点停止 → stopSession |
| 详情监控 | 切详情态：净值卡 + 持仓表 + 订单表 + 日志流 |
| SSE 接收 | mock EventSource，收到 trade 事件 → 订单表新增一行 |

实现：
- `web/src/api/index.ts` 封装 `listLiveSessions/createLiveSession/startLive/stopLive/getLiveTrades/getLivePositions` + 类型
- 视图三态（list/form/detail，复用 Backtest.vue 的 `v-if` 切换模式）
- 详情用 `EventSource('/api/live/sessions/{id}/stream')` 接 SSE，`onmessage` 按事件类型更新持仓/订单/日志

### 收尾切片 5：恢复机制 + 全量回归

**目标**：Core 重启自动恢复 `status=running` 的 session。

| 测试 | 要点 |
|---|---|
| `test_recover_rebuilds_virtual_positions` | 预置 live_trades → `LiveEngine.recover` → 各组合虚拟持仓/现金与 SQL 聚合一致 |
| `test_recover_skips_on_tdx_unavailable` | TQ 未启动 → session 标 stopped，记错误 |
| `test_recover_restarts_subscription` | 恢复后 TQ 订阅重建（并集股票） |

实现：
- `LiveEngineManager.recover_on_startup()`：扫 `status=running` session → 逐个 `LiveEngine.recover()` → 重建订阅
- `LiveEngine.recover()`：SQL 聚合 `live_trades`（§恢复机制 SQL 示例）→ 重算各组合 `Portfolio.account.cash` + 各 `StrategyContext.positions` → `start()`
- Core lifespan：`startup` 调 `recover_on_startup`，`shutdown` 停所有引擎

## 9. 验证

- **后端**：`uv run pytest` 全绿（新 test_live_engine/test_live_runtime/test_live_api + 既有 174 例无回归）
- **前端**：`npx vitest run` + `npm run build` 无类型错误
- **E2E（用户本地，需通达信 live 版运行并登录行情）**：
  1. `./manage.ps1 restart` → `/live` 页面 → 新建 session 选 1-2 个组合
  2. 点启动 → 详情页看到 SSE 推送的实时 bar/订单/持仓
  3. 盘中观察公式触发 → 模拟撮合成交 → live_trades 落库 → 前端订单表实时新增
  4. 重启 Core → session 自动恢复，持仓与重启前一致
- **切片1前验证脚本** — ✅ **已完成**（[0008-verify-results.md](0008-verify-results.md)）：源码探查 + 真机连接已确认 SDK 接口链路（`subscribe_hq` 通知 + `get_market_data` 拉数 + `formula_set_data` 内存注入 + `formula_process_mul_zb` 算公式全通，ErrorId=0）。联调时仅需确保实盘版通达信已登录行情接收当日数据。

## 10. 次期预告（不在本计划）

- `NatsDispatcher`：实现 `OrderDispatcher.place_order` → `NatsClient.request("iquant.iguant.order.place")` → 网关真实下单；订单状态轮询/推送
- iQuant 网关 5 个 mock handler 真实化（需 iQuant 实盘/模拟环境）
- 多周期合成（1m/5m → 30m/60m 整除点，§2.4 c/d/e）
- 1d/1w 周期实盘（15:00 合成 + 跨日 pending）
- 虚拟持仓 vs 实际账户交叉验证（§恢复机制第4步）
- `live_snapshots` 表 + 实盘净值历史曲线（若前端需要）
