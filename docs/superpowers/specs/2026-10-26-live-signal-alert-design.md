# 实盘信号预警（通达信 send_warn + 半自动）— 设计

日期：2026-10-26
状态：设计待评审

## 背景

平台已具备回测与实盘自动交易，但实盘侧目前只有「下单附属信号」：

- `LiveEngine._handle_bar` 只在 `portfolio.on_bar` 返回订单后，遍历订单发 `signal` SSE
  （`main/core/engine/live_engine.py:764-784`）。被组合内闸门拦掉、没有生成订单的公式信号
  不会出现在信号流里，只落 `LiveDecisionEvent`。
- 实盘公式执行只按 `Formula.name` 调 TQ，忽略 `Formula.engine`
  （`main/core/services/live_service.py:420-476`）。`engine=builtin` 的公式在实盘不会生效；
  TQ 侧找不到公式/线名时 `_extract_latest_signal` 直接返回空列表，不报错、不告警
  （`main/core/engine/live/market_data.py:490-519`）。
- 没有 `live_signals` 表、没有信号查询 API、没有外部告警通道（`alert/` 为空目录）。
- `1w/1mon` 每个交易日 14:30 都重新注入/驱动一次，周线信号在周内会随未完成周 bar 变化
  （`main/core/engine/live/market_data.py:521-551`、`daily_closer.py`）。

用户已明确：

1. **信号生成只走「通达信导入」模式**，不走平台「新建公式」模式（内置解释器本地函数还不准）。
2. 信号频率可选：1m/5m/30m/1d/1w 等；「创懿动量模型」是日线公式引用周线
   （`MACD.DIF#WEEK`），**每周一 09:00 用上周已完成周线求值**。
3. **默认半自动**：预警 + 人工下单；全自动作为可选模式。
4. 半自动的人工下单走**通达信原生预警**：`send_warn` 推到 TQ 策略信号窗口，双击预警
   直接打开闪电下单。
5. 参考 `D:\project\通达信TQ量化平台完整使用手册.html` 的 `send_warn` / `send_message`
   用法。

本设计只做「信号监控 + 预警」层，不重造公式引擎，也不改变现有自动交易主链路。

## 目标与验收标准

1. **半自动 alert 模式**：信号只落库、发通达信预警、发 SSE，**不下单**。
2. **实盘只接受 TQ 导入公式**：`engine=builtin` 的会话启动直接 503，并给出可操作中文提示。
3. **频率可选**：按策略配置 `signal_frequency`（1m/5m/15m/30m/60m/1d/1w/1mon）与
   `signal_at`；动量模型默认 `1w` + 周一 09:00，使用上周已完成周线。
4. **信号可追溯**：每次匹配落 `live_signals`，唯一键防重复推送；`alert_status`、
   `tdx_run_id`、`alert_error` 可查。
5. **失败可见**：公式缺失、输出线不匹配、`send_warn` 失败都必须有明确错误，不静默。
6. **所有匹配信号发 SSE**，包括被组合闸门拦截、最终没有下单的信号。
7. **auto 模式保持现有行为**：自动下单链路不变；`send_warn` 作为审计旁路。
   P0 的 auto 模式只支持 `signal_frequency == period`（沿用现有 bar/日终驱动）；
   自定义频率在 auto 下启动直接 503，提示先用 alert 或等 P2 对齐。
8. **回测/实盘同源**：实盘绑定的策略，回测也走 TQ 导入公式；频率门控分阶段对齐。

## 用户已确认的决策

| 决策 | 结论 |
|---|---|
| 信号来源 | 仅通达信导入公式（`engine=tq`）；`builtin` 仅回测/研究 |
| 半自动下单 | 通达信原生 `send_warn`，双击预警闪电下单；平台不代下单 |
| 默认模式 | 新会话默认 `alert`（半自动）；`auto` 可选 |
| 动量模型时点 | 周一 09:00 用上周已完成周线求值，09:25/09:30 可操作 |
| 频率 | 按策略可选，公式数据周期与推送频率分离 |
| 回测 | 实盘绑定策略回测也走 TQ 导入公式；`builtin` 保留研究用途 |

### P0 范围约束

- `alert` 模式支持全部 `signal_frequency`（1m~1mon）。
- `auto` 模式 P0 只支持 `signal_frequency == period`，复用现有 `LiveEngine` 的
  bar/日终驱动；`signal_frequency != period` 时启动 503，提示「全自动自定义频率留待 P2，
  当前请用半自动 alert」。
- alert 模式的行情固定走同进程 TQ，不依赖 QMT 桥；`session.engine` 在 alert 下仅作
  记录，auto 下才决定交易通道。

## 关键事实

### 代码侧

| 事实 | 位置 |
|---|---|
| `live_sessions` 只有 `mode`（simulation/live）、`engine`、`status`，无执行模式 | `main/core/models/live_session.py:10-21` |
| 策略频率字段是 `Strategy.period`（1m/5m/15m/30m/60m/1d/1w/1mon） | `main/core/models/strategy.py:14` |
| `Formula.engine` 默认 `tq`；注释明确「builtin 仅回测接线，实盘仍走 tq」 | `main/core/models/formula.py:15-17` |
| `FormulaSignal` 按 `signal_name + trigger_value` 匹配 | `main/core/models/formula_signal.py:8-12` |
| `build_engine` 只把 `Formula.name` 传给 `TQFormula`，不检查 `engine` | `main/core/services/live_service.py:420-476` |
| 实盘逐 bar 注入只调 `compute_injected_batch`；算失败/ErrorId 静默返回空 | `main/core/engine/live/market_data.py:344-353, 490-519` |
| `1w/1mon` 走启动/日终 TQ 注入，桥端 xtdata 拉不到 | `main/core/engine/live/market_data.py:521-551` |
| `signal` SSE 只在 orders 循环内发出 | `main/core/engine/live_engine.py:764-784` |
| SSE 端点 | `main/core/api/live.py:302` |
| `live_orders` 已存 `signal_name/signal_type/bar_time`，但无独立信号表 | `main/core/models/live_order.py:23-25` |
| `main/core/tq` 没有 `send_warn` / `send_message` 包装 | `main/core/tq/__init__.py`、`formula.py` |

### 手册侧

`D:\project\通达信TQ量化平台完整使用手册.html`：

- `send_warn(stock_list, time_list, price_list, close_list, volum_list, bs_flag_list,
  warn_type_list, reason_list, count)`：把预警发到通达信客户端 TQ 策略信号窗口。
  - `time_list` 格式 `YYYYMMDDHHMMSS`。
  - `bs_flag_list`：0 买 / 1 卖 / 2 未知；长度不足自动补 2。
  - `warn_type_list`：0 常规预警（目前仅支持）。
  - `reason_list` 每条有效长度 25 个汉字（50 个英文）。
  - `stock_list` 与其他 list 一一对应；同一股票多条预警则重复加入。
  - 返回 `{Error, ErrorId, run_id}`。
  - 预警图标：0=红色 B、1=绿色 S、2=黄色双叠三角；**双击买卖预警记录可直接打开闪电下单**。
- `send_message(msg_str)`：发消息到 TQ 策略管理器界面，适合运行汇总。
- `formula_process_mul_zb`：指标公式批量求值，支持 `stock_period`、`start_time`、
  `end_time`、`count`、`dividend_type`；是现有 `TQFormula` 的底层能力。
- 手册已有「定时器每分钟预警（全市场）」与「`subscribe_hq` 每 6 秒预警（100 只以内）」
  两种调度示例。

## 总体架构

```
TDX 导入公式 (engine=tq)
  → SignalScheduler 频率门控（1m/5m/30m/1d/1w/1mon）
  → TQFormula / formula_process_mul_zb
  → SignalMonitor 匹配 FormulaSignal（所有信号，不依赖是否下单）
  → live_signals 落库（唯一键去重）
  → TQAlert.send_warn → 通达信 TQ 策略信号窗口（双击闪电下单）
  → SSE signal → Web 信号面板
  → execution_mode=auto 时，才进入 Portfolio.on_bar → 下单通道
```

职责边界：

- `TQAlert`：只负责通达信 `send_warn` / `send_message` 的参数校验与调用，不关心业务。
- `SignalScheduler`：只负责「什么时候该算/该推」，以及「数据截至哪一天」。
- `SignalMonitor`：只负责「算出来的输出线是否命中配置、是否去重、落库、推送、发 SSE」。
- `LiveEngine`：只负责把现有 bar 循环与调度器接起来；alert 模式不调用下单链路。
- `Portfolio.on_bar`：auto 模式继续复用，不改风控/信号优先级；alert 模式不调用。

## 信号源与频率调度

### 两个独立维度

| 维度 | 字段 | 含义 |
|---|---|---|
| 公式数据周期 | `Strategy.period` | 公式在什么 K 线上求值（1m/5m/.../1mon） |
| 信号推送频率 | `Strategy.signal_frequency` | 什么时候产生/推送信号（可等于或粗于 period） |
| 推送时刻 | `Strategy.signal_at` | `HH:MM`；1w/1mon 用默认日（周一/每月首个交易日） |
| 数据截至 | `Strategy.signal_data_end` | `completed`（默认，1w/1mon）或 `current`（1d/日内） |

约束：`signal_frequency` 不能细于 `Strategy.period`（例如 `period=1d` 不能配 `signal_frequency=1m`）；
`period=1d + signal_frequency=1w` 合法，表示「日线公式，按周推送」。

### 默认调度表

| `signal_frequency` | 默认时点 | 数据窗口 | 说明 |
|---|---|---|---|
| 1m/5m/15m/30m/60m | 周期边界 | 当根完成 bar | 复用现有 `periods_on_boundary`；`signal_at` 忽略 |
| 1d | 14:30 | 当日截至 `signal_at` 的最后一根 `period` bar | 可配 15:05（收盘后确认） |
| 1w | 周一 09:00 | 上周最后一个交易日 | 推送 09:25/09:30 可操作；`end_time=上周五` |
| 1mon | 每月首个交易日 09:00 | 上月最后一个交易日 | `end_time=上月最后交易日` |

非交易日顺延到下一个交易日同一时刻，数据窗口仍为上一完整周期。

### 动量模型的配置

- `Formula.engine = "tq"`，公式在通达信里导入，名称与平台 `Formula.name` 完全一致。
- `Strategy.period = "1d"`（公式本身是日线，引用 `#WEEK`）。
- `Strategy.signal_frequency = "1w"`，`signal_at = "09:00"`，`signal_data_end = "completed"`。
- 周一 09:00 调 `TQFormula.compute(formula_name, "", stocks, period="1d",
  count=formula_count, end_time=上周最后交易日)`，取每只股票最后一个输出。
- 09:25/09:30 前推送 `send_warn`，用户双击预警下单。
- 若周一休市，顺延到下一个交易日 09:00，仍使用上周完整周线。

### 调度实现

- 新增 `SignalScheduler`（`main/core/engine/live/signal_scheduler.py`）。
- `LiveEngine` 在主循环每分钟调用 `scheduler.due(now)`；返回本分钟到期的策略列表。
- 日内频率不额外调度：沿用现有 bar 边界 `fill_signal_cache`；alert 分支只把
  `signal_cache` 交给 `SignalMonitor`，不调用 `Portfolio.on_bar`。
- `1d`：在 `signal_at`（默认 14:30）按每个 distinct `Strategy.period` 构造调度 bar
  （`bar.period=Strategy.period`，`bar_time=signal_at`），复用 `fill_signal_cache`
  （`AlertDataDispatcher` 走 TQ 行情），不直接全量 `TQFormula.compute`。这样
  `period=1m + signal_frequency=1d` 是在当日最后一根 1m bar 上求值，而不是偷换成日线公式。
- `1w/1mon`：调用 `TQFormula.compute(..., period=Strategy.period,
  count=formula_count, end_time=数据窗口结束日)`，取每只股票最后一个输出；不依赖桥端
  1w/1mon 行情。
- 同一策略同一**数据窗口**只触发一次（例如 `1w` 一周一次、`1d` 一天一次）。
- 启动补算窗口：若 Core 在计划时点后 `catch_up_minutes`（默认 30）内启动，且该数据窗口
  尚未落库，则补算一次；超过窗口跳过，并用 `send_message` 汇总「已跳过 N 个过期信号」。
  只补当天/当周/当月窗口，不补历史。

### alert 模式行情源

- alert 不要求交易账号/下单通道，但必须有行情源；固定走同进程 TQ
  （`TQData` / `TdxTqDispatcher.query_quote`），与公式数据同源。
- 新增 `AlertDataDispatcher`（`main/core/engine/live/alert_dispatcher.py`）：
  - `query_quote` / `query_calendar` 委托 `TdxTqDispatcher`（account 可为空）；
  - `heartbeat()` 用 `TQData.get_history_raw` 探测 TDX/TQ 是否可用；
  - `place_order()` 抛错；`get_positions()` / `get_asset()` 返回空或抛错；
  - `channel = "alert"`，`supports_remark = False`。
- `build_engine` 在 `execution_mode=alert` 时用 `AlertDataDispatcher` 构造 `BarPoller` /
  `MarketDataService`，不注入真实交易 dispatcher。
- alert 的 tick 只做 `bar_poller.poll()` + `SignalScheduler`；跳过
  `_expire_stale_orders`、`_poll_deals`、`_maybe_daily_update`、持仓/T+1/对账等券商依赖任务。
- alert 的 `start()` 不启动 `_deals_loop`，也不调用 `inject_startup_periods` /
  `_maybe_daily_bars`；1w/1mon 完全由 `SignalScheduler` 驱动。

## 信号匹配与去重

### 匹配

- 复用 `StrategyContext.formula_signals`（`portfolio_builder` 已装配）。
- 对每个 `{name, value}`：找 `FormulaSignal(signal_name=name, trigger_value=value)`。
- 未配置 `FormulaSignal` 的输出线不产生信号（与回测一致）。
- 仅处理公式信号；alert 模式不做基于持仓的止损/止盈（手工单平台不记账，避免误报）。
- auto 模式仍由 `Portfolio.on_bar` 产生风控信号；风控单也可落 `live_signals`（P1 可选）。
- auto 模式 `SignalMonitor` 复用 `_handle_bar` 已填的 `signal_cache`，不重复调 TQ。

### 边沿与去重

- 唯一键：`(live_session_id, portfolio_strategy_id, strategy_id, stock_code,
  signal_name, bar_time)`。
- 脉冲信号（`CROSS` 类，值只在触发 bar 为 1）：按唯一键天然只发一次。
- level 型信号（值持续为 1）：按 `(strategy, code, signal_name)` 做边沿检测，只在
  `0 → 1` 时产生；进程重启后从最近一条 `live_signals` 恢复上次状态，避免重复。
- 同一 bar 多次轮询、重复 `_handle_bar` 不重复推送。
- 多组合/多策略持有同一股票同一信号：默认各自一条（保留归属）；如需要全局去重，
  P1 增加 `dedup_alerts_across_strategies` 配置。

### `bs_flag` 映射

| 信号类型 | `bs_flag` | 预警图标 |
|---|---|---|
| OPEN / ADD | 0 | 红色 B |
| REDUCE / CLOSE | 1 | 绿色 S |
| 未知/其它 | 2 | 黄色双叠三角 |

`reason` 用 `公式名-信号名`，超过 25 个汉字截断；`warn_type=0`。
`price`/`close` 取信号 bar 收盘价（周线信号用上周五收盘）；拿不到时用 `0`，不阻塞预警。

## 通达信预警（send_warn）

### 新增 `TQAlert`

文件：`main/core/tq/alert.py`，`main/core/tq/__init__.py` 导出。

```python
@dataclass
class WarnItem:
    stock_code: str
    time: str          # YYYYMMDDHHMMSS
    price: str         # 纯数字字符串
    close: str         # 纯数字字符串
    volume: str        # 纯数字字符串，默认 "0"
    bs_flag: str       # "0"/"1"/"2"
    warn_type: str     # "0"
    reason: str        # <= 25 汉字

class TQAlert:
    def send_warn(self, items: List[WarnItem]) -> dict: ...
    def send_message(self, msg: str) -> dict: ...
    def preflight(self) -> Optional[str]: ...  # None=可用，str=不可用原因
```

契约：

- 空列表直接返回 `{"ErrorId": "0", "skipped": True}`。
- 校验：各 list 等长；`time` 格式；价格/成交量纯数字；`bs_flag` ∈ 0/1/2；
  `reason` 超长截断；`count = len(items)`。
- 调用 `get_tq().send_warn(...)`，用 `get_tdx_lock()` 与公式调用串行，避免 TQ 并发。
- 解析 `{ErrorId, Error, run_id}`；`ErrorId != 0` 抛 `TQAlertError` 或返回失败结果，
  由调用方落 `alert_error`。
- 旧版 TQ 无 `send_warn` 时，`preflight()` 返回明确原因，启动 503。
- 批量上限：按 50 条/次分块（手册未给上限，保守处理）。

### 为什么用 `send_warn`

- 复用通达信原生信号窗口、买卖图标、闪电下单，不需要自建下单确认 UI。
- 与「半自动：预警 + 人工下单」直接匹配。
- Core 已嵌入 TQ，`send_warn` 与 `formula_process_mul_zb` 同源，不增加新通道。

## 执行模式

### `alert`（默认，半自动）

- 启动不要求交易账号/下单通道在线；必须有行情源（TDX 客户端运行 + TQ 可用）。
- 行情固定走 `AlertDataDispatcher`（TQ），不依赖 QMT 桥。
- 不调用 `Portfolio.on_bar`、不调用 `ExecutionEngine`、不下单。
- 仍按调度算公式、匹配信号、落 `live_signals`、`send_warn`、发 SSE。
- `AlertDataDispatcher.place_order()` 直接抛错，防止未来误调用下单。
- `LiveEngine` 使用独立 `_tick_alert`，跳过券商依赖任务（委托超时、成交回填、持仓/
  资金/T+1/日终组合更新）。

### `auto`（全自动，可选）

- 现有链路不变：`Portfolio.on_bar` → `ExecutionEngine` → 下单通道。
- `SignalMonitor` 在同一 bar 上先落库/推送所有匹配信号；被闸门拦截的信号也能看到。
- `_handle_bar` 不再自己发 `signal` SSE，统一由 `SignalMonitor` 发，避免重复。
- `send_warn` 默认也发（审计/手动旁路）；可在 `config.yaml` 关闭。
- P0 只支持 `signal_frequency == period`；自定义频率启动 503。
- `period=1w/1mon` 的 auto 会话仍沿用现有日终驱动（周内可能重绘），与 alert 的
  「周一 09:00 用上周完成周线」不一致；P2 对齐。

### 模式字段

- `live_sessions.execution_mode`：`alert` / `auto`。
- 迁移时已有会话写 `auto`，保持升级前行为；新建会话 API/UI 默认 `alert`。
- 启动预检按模式分支：
  - `alert`：检查 `AlertDataDispatcher.heartbeat()`（TQ 行情），不检查交易 heartbeat；
    再检查 TQ、公式、输出线、`send_warn`。
  - `auto`：保留现有交易 heartbeat/通道检查；额外检查 `engine=tq`、频率约束与公式预检。

## 数据模型

### `live_signals`（新表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `live_session_id` | FK live_sessions CASCADE | 索引 |
| `portfolio_strategy_id` | FK portfolio_strategies | 索引 |
| `strategy_id` | FK strategies | 索引 |
| `formula_id` | FK formulas nullable | 审计 |
| `stock_code` | String(20) | 索引 |
| `period` | String(5) | 公式数据周期 |
| `bar_time` | DateTime | 信号所属 bar/时点 |
| `formula_name` | String(100) | |
| `signal_name` | String(50) | |
| `signal_type` | String(10) | OPEN/ADD/REDUCE/CLOSE |
| `trigger_value` | Integer | |
| `price` | Numeric(10,3) nullable | 推送时现价/收盘 |
| `execution_mode` | String(10) | alert/auto |
| `status` | String(15) | new/alerted/failed/ignored |
| `alert_status` | String(15) nullable | sent/failed/skipped |
| `alert_error` | String(500) nullable | |
| `tdx_run_id` | String(30) nullable | send_warn 返回 |
| `alerted_at` | DateTime nullable | |
| `created_at` | DateTime | |

唯一索引：
`(live_session_id, portfolio_strategy_id, strategy_id, stock_code, signal_name, bar_time)`。
查询索引：`(live_session_id, bar_time)`、`(strategy_id, stock_code)`。

`bar_time` 是数据窗口时点：日内为 bar 时间，`1d` 为当日 14:30，`1w` 为上周最后交易日
15:00，`1mon` 为上月最后交易日 15:00。实际推送时刻看 `created_at` / `alerted_at`。
`status`：`new`（已匹配未推送）/ `alerted`（已推送）/ `failed` / `ignored`；
`alert_status`：`sent` / `failed` / `skipped`（如关闭了 auto 预警）。

### 策略字段

`strategies` 新增：

- `signal_frequency` String(10) nullable：NULL = 用 `period`。
- `signal_at` String(5) nullable：`HH:MM`；NULL = 按频率默认。
- `signal_weekday` Integer nullable：1-5；NULL = 1（周一），仅 `1w` 使用。
- `signal_data_end` String(10) nullable：`completed` / `current`；NULL = 按频率默认。

校验在 API/服务层：
- `signal_frequency` 必须在允许集合内。
- 不允许比 `period` 更细。
- `signal_at` 必须 `HH:MM`。
- `1w` 的 `signal_weekday` 1-5；`1mon` 忽略。
- P0 的 `1w/1mon` 只允许 `signal_data_end=completed`，禁止用未完成周/月 bar。

### 会话字段

`live_sessions` 新增：

- `execution_mode` String(10) not null default `auto`（迁移兼容）；新建 API 默认 `alert`。

## 预检与错误处理

### 启动预检

`start_session` 在 `build_engine` 后、`engine.start()` 前执行：

1. **模式**：`alert` 检查 `AlertDataDispatcher.heartbeat()`（TQ 行情），不检查交易
   heartbeat；`auto` 保留现有交易 heartbeat/通道检查。`start_session` 在
   `execution_mode=alert` 时不得调用真实交易 `dispatcher.heartbeat()`。
2. **TQ**：`TQAlert.preflight()`；TQ 未初始化/旧版无 `send_warn` → 503。
3. **公式引擎**：遍历会话内所有 `Strategy`，其 `Formula.engine` 必须为 `tq`；
   否则 503，消息列出策略/公式，提示「实盘仅支持通达信导入公式，请导入后使用」。
4. **公式存在与输出线**：对每个公式取一只股票（`count=min(formula_count, 20)`，
   `end_time=最近交易日`）调 `TQFormula.compute`：
   - `ErrorId != 0` → 503，带 `Error`。
   - 输出线名集合必须覆盖该公式所有 `FormulaSignal.signal_name`；缺失 → 503，
      列出缺失线名。
5. **调度数据**：`1w/1mon` 检查 `formula_count`/历史窗口是否足够（至少覆盖 MACD/LLV
   等最小窗口）；不足只警告，不阻断。
6. **频率**：`signal_frequency` 不得细于 `period`；`auto` 下必须等于 `period`。
7. **重复公式**：同一会话内同名公式只预检一次。

预检结果缓存到引擎，运行期不重复调用。运行期公式失败：
- 记录日志 + `send_message` 汇总 + SSE `risk`/`decision` 事件（P0 先用 `decision`）。
- 不静默；不阻断其它策略。

### `send_warn` 失败

- `live_signals.alert_status = failed`，写 `alert_error`；`status = failed`。
- SSE 事件带 `alert_status/alert_error`，前端可重发（P1）。
- 不影响 auto 模式下单；alert 模式只影响该条预警。
- 失败重试：P0 不自动重试，避免重复推送；P1 可加一次退避重试 + 手动重发。

### 重复/乱序

- 唯一键冲突视为已处理，不重复 `send_warn`。
- `send_warn` 成功但 DB 更新失败：以 `tdx_run_id` 日志为准，P1 加对账任务。
- Core 在计划时点宕机：不补发跨日历史信号；启动时仅在 `catch_up_minutes` 内补算当前
  数据窗口一次，超窗则发一条 `send_message` 汇总「已跳过 N 个过期信号」。

## API / SSE / 前端

### API

`GET /api/live/sessions/{session_id}/signals`

查询参数：

- `strategy_id`、`stock_code`、`signal_type`、`execution_mode`
- `alert_status`
- `start`、`end`（bar_time）
- `limit`（默认 100，最大 500）、`offset`

返回统一格式：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 1,
        "portfolio_id": 1,
        "strategy_id": 2,
        "stock_code": "000001.SZ",
        "signal_name": "强势金叉信号",
        "signal_type": "OPEN",
        "bar_time": "2026-10-23T15:00:00",
        "price": "12.34",
        "execution_mode": "alert",
        "status": "alerted",
        "alert_status": "sent",
        "alert_error": null,
        "tdx_run_id": "1"
      }
    ],
    "total": 1
  }
}
```

可选 P1：`POST /api/live/sessions/{id}/signals/{signal_id}/replay-alert` 重发。

### SSE

`signal` 事件 payload 扩展：

```json
{
  "type": "signal",
  "signal_id": 1,
  "session_id": 1,
  "portfolio_id": 1,
  "strategy_id": 2,
  "stock_code": "000001.SZ",
  "signal_name": "强势金叉信号",
  "signal_type": "OPEN",
  "bar_time": "2026-10-23T15:00:00",
  "price": "12.34",
  "execution_mode": "alert",
  "alert_status": "sent",
  "alert_error": null,
  "tdx_run_id": "1"
}
```

- alert 模式：只发 `signal`，不发 `order/trade`。
- auto 模式：`signal` 由 `SignalMonitor` 统一发；`order/trade/position/risk` 保持。

### 前端

P1 在 `web/src/views/LiveSessions.vue` 增加「信号」工作台：

- 列：时间、组合、策略、代码、信号名、类型、价格、模式、TDX 推送状态、错误。
- 筛选：策略/股票/类型/模式/状态。
- 实时：复用现有 SSE `signal` 事件；历史：新查询 API。
- 可选「重发预警」按钮（P1）。

P0 先保证 API 与 SSE；前端可先用现有事件日志查看。

## 回测与实盘一致性

- 实盘绑定策略的 `Formula.engine` 必须 `tq`；回测也按 `tq` 跑，避免「回测 builtin、实盘 TQ」
  两套口径。
- `SignalScheduler` 的频率门控在回测侧复用：
  - 1m/5m/30m/1d：按 `bar_time` 过滤 `signal_cache`，只保留调度时点。
  - 1w/1mon：回测需按「完成周期」求值；P0 先记录为已知差异，P2 实现
    `end_time` 对齐（例如周一信号用上周五数据）。
- 不改变回测 `build_signal_cache` 的现有 builtin 能力；只给实盘绑定策略加
  「必须 tq」的预检与调度过滤。

## 测试计划

### 单元

- `TQAlert`：
  - 参数补齐/截断/纯数字校验；`bs_flag` 长度不足补 2。
  - `ErrorId != 0` → 失败结果；旧版无 `send_warn` → `preflight` 返回原因。
  - 分块 50 条。
- `SignalScheduler`：
  - 周一 09:00 到期，08:59 不到期；`end_time` = 上周最后交易日。
  - 周一休市顺延；`1d` 14:30；`1m` 边界。
  - 启动补算窗口内补一次，超窗跳过。
  - `signal_frequency` 细于 `period` → 配置校验失败。
- `SignalMonitor`：
  - 输出线 → `FormulaSignal` 匹配；未配置线忽略。
  - 脉冲/level 边沿；唯一键去重；重复 `_handle_bar` 不重复。
  - `bs_flag` 映射；`reason` 截断；落库 + SSE + `send_warn` 调用参数。
  - alert 模式不调用 `Portfolio.on_bar`/不下单。
  - alert `_tick_alert` 不调用委托/成交/持仓/资金等券商查询。
- 预检：
  - `engine=builtin` → 503 文案。
  - 公式缺失/输出线缺失 → 503 文案。
  - alert 模式无交易账号也能启动（TQ 行情可用）；TQ 行情不可用 → 503。
  - auto + 自定义频率 → 503；auto 模式保留交易 heartbeat 检查。

### 集成

- Fake TQ formula + fake `send_warn`：跑一个周一 09:00 周期，断言
  `live_signals`、`send_warn` 入参、SSE。
- `GET /signals` 过滤/分页。
- auto 模式回归：现有下单/回填/SSE 不破。

### 真机

1. 通达信导入「创懿动量模型」，配置 `engine=tq`、`period=1d`、`signal_frequency=1w`、
   `signal_at=09:00`。
2. 周一 09:00 启动 alert 会话，确认：
   - 预检通过；
   - TQ 策略信号窗口出现 B/S 预警；
   - 双击预警打开闪电下单；
   - `live_signals` 有记录，SSE 有 `signal`；
   - 无任何自动下单。
3. 把公式删掉/改名，重启 → 503 明确提示。
4. 关闭 TDX 客户端 → 启动 503。
5. `engine=builtin` 会话启动 → 503。

## 分期

### P0（半自动最小闭环）

1. `TQAlert`（`send_warn` / `send_message` / `preflight`）。
2. `live_signals` 表 + 迁移。
3. `Strategy.signal_frequency/signal_at/signal_weekday/signal_data_end` + 迁移。
4. `live_sessions.execution_mode` + 迁移（老会话 `auto`，新会话默认 `alert`）。
5. `SignalScheduler` + `SignalMonitor`。
6. `AlertDataDispatcher` + `LiveEngine` 接入：alert `_tick_alert` 只拉行情/算信号/推送；
   auto 模式统一 `signal` 事件。
7. 启动预检：TQ 行情、TQ-only、公式存在、输出线匹配、`send_warn` 可用、auto 频率约束。
8. `GET /signals`。
9. 单元/集成测试。

### P1（可观测与运营）

1. Web 信号面板/筛选/历史。
2. `send_message` 运行汇总；无信号/跳过信号汇总。
3. `send_warn` 失败重发按钮/一次退避重试。
4. `config.yaml` 开关：`live_alert.enabled`、批量大小、是否在 auto 模式发预警。
5. 多策略同信号去重选项。

### P2（增强）

1. 回测/实盘同频门控（1w/1mon 完成周期对齐）。
2. Web 一键确认下单（如需）。
3. 持仓类风控告警（需要手工持仓同步/券商对账）。
4. 更多频率/自定义 cron。
5. 预警分级/冷却/静默时段。

## 非目标

- 不把 `builtin` 公式接到实盘。
- 不在 alert 模式自动下单。
- 不做基于手工持仓的止损/止盈预警（P0）。
- 不做 Web 确认下单（P0）。
- 不补发 Core 宕机期间的历史预警。
- 不改 QMT 桥、不改现有自动交易通道。
- 不做多用户/多通道通知路由（单用户本地工具）。

## 风险与待验证

| 风险/待验证 | 说明 | 处理 |
|---|---|---|
| TQ `#WEEK` 完成周期语义 | `period=1d, end_time=上周五` 时，`#WEEK` 是否使用上周已完成周线而非当前周 | **真机验证后再实现**；不符合则改用 `period=1w` 或专用周线公式 |
| `send_warn` 版本/行为 | 旧版 TQ 可能无此接口；不同客户端闪电下单行为可能不同 | 启动 `preflight`；真机验证双击下单 |
| `reason` 25 字限制 | 公式名+信号名可能超长 | 截断并保留关键信息 |
| 批量上限 | 手册未给 `send_warn` 最大条数 | 50 条/次分块 |
| 多组合重复预警 | 同一股票同一信号被多个策略持有时会多条 | 保留归属；P1 提供全局去重开关 |
| alert 模式无持仓 | 手工下单平台不记账，无法算止损/止盈 | P0 只做公式信号；P2 做持仓同步 |
| 周线信号重绘 | 若在周内求值，未完成周线会变 | alert 周一 09:00 用上周完成周线；auto P0 仍沿用日终驱动，P2 对齐 |
| 回测/实盘口径 | TQ 回测与实盘 `end_time` 不一致 | P0 记录差异；P2 对齐并加测试 |
| alert 行情独立 | alert 固定走 TQ，与 QMT 桥的实时 bar 可能有细微差异 | 公式与预警本就以 TQ 为准；文档明确 alert 不使用桥行情 |
| auto 自定义频率 | P0 auto 只支持 `signal_frequency == period` | 启动 503 明确提示；P2 对齐 |
| 启动晚于计划时点 | 09:00 信号在 09:05 启动会错过 | `catch_up_minutes=30` 内补算；超窗跳过并汇总 |

## 实施清单

### 后端

- [ ] `main/core/tq/alert.py`（新）+ `main/core/tq/__init__.py` 导出。
- [ ] `main/core/models/live_signal.py`（新）+ `models/__init__.py`。
- [ ] `main/core/models/live_session.py`：`execution_mode`。
- [ ] `main/core/models/strategy.py`：`signal_frequency/signal_at/signal_weekday/signal_data_end`。
- [ ] `main/core/engine/live/alert_dispatcher.py`（新）：TQ 行情 dispatcher。
- [ ] `main/core/engine/live/signal_scheduler.py`（新）。
- [ ] `main/core/engine/live/signal_monitor.py`（新）。
- [ ] `main/core/engine/live_engine.py`：alert `_tick_alert` / auto 分支、调度接入、
      signal 事件统一。
- [ ] `main/core/engine/live/market_data.py`：暴露 `compute_latest` 或复用 `TQFormula.compute`。
- [ ] `main/core/services/live_service.py`：`build_engine` 预检、模式、公式 engine 校验、
      alert 不调真实交易 heartbeat。
- [ ] `main/core/api/live.py`：`GET /signals`；SSE payload 扩展。
- [ ] `main/alembic/versions/...`：迁移。
- [ ] 测试：`main/core/tests/unit/` + `integration/`。

### 前端（P1）

- [ ] `web/src/api/live.ts`：signals 查询。
- [ ] `web/src/views/LiveSessions.vue`：信号面板/筛选/状态。
- [ ] `web/src/__tests__/`：渲染/筛选/SSE 更新。

### 文档

- [ ] 更新 `CLAUDE.md` / `AGENTS.md` 实盘信号章节。
- [ ] 更新 `docs/live-full-flow-design.md` 的信号/预警现状。
- [ ] 新增真机验证记录（周一 09:00 动量模型）。
