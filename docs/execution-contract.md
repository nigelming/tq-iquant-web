# 执行契约文件交接层（file_handoff）— Schema 文档

> 实现入口：`main/core/engine/live/file_handoff.py`（`FileHandoff`）、
> `main/core/engine/ths_ui_dispatcher.py`（`ThsUiDispatcher`，第一个外部消费者）。
> 版本：`schema_version = 1`（本文档描述其完整字段契约）。

## 1. 定位与分层

执行契约文件交接层是**决策层与执行层之间的显性文件契约**，通道无关：

```
┌─ 决策层（Core 进程内，通道无关）────────────────────────────┐
│ 信号（公式/风控信号）→ 风控（止损/止盈/熔断）→ 资金审批        │
│   ↓ _emit 钩子                ↓ ThsUiDispatcher.place_order  │
│  signals.jsonl / risk.jsonl   orders.jsonl（seq 单调追加）    │
└──────────────────────────────┬──────────────────────────────┘
                               ↓ 文件系统（{work_dir}/{session_id}/）
┌─ 执行层（任意无桥执行端适配器）──────────────────────────────┐
│ 按游标消费 orders.jsonl → 执行（UI 自动化等）→ results.jsonl  │
│ 回执（accepted/rejected）+ cursor.json 幂等表                │
└─────────────────────────────────────────────────────────────┘
```

- **决策层**只负责产生「信号 / 风控事件 / 已审批指令」，不感知执行端实现；
- **执行层**只需实现「消费指令 → 执行 → 回执」适配器即可接入（见 §7）；
- **通达信（TdxTqDispatcher）是本契约的内嵌退化形式**：TQ 模块与 Core 同进程，
  决策→执行是直接函数调用，无文件中介；**同花顺（ThsUiDispatcher，file_builtin
  模式）是第一个外部消费者**——纯 UI 自动化无法秒级 HTTP 受理，故以文件交接。

## 2. 参与方与挂载门控

| 角色 | 代码 | 职责 |
|---|---|---|
| 引擎（决策层） | `LiveEngine._emit` / `_emit_file_handoff` | signal/decision/risk 事件落 signals/risk.jsonl；写失败仅告警 |
| dispatcher（指令侧） | `ThsUiDispatcher._place_order_via_file` | 参数校验后指令追加 orders.jsonl，秒回 None（`last_enqueued_order_id` 标记已入队） |
| 内置执行器 | `ThsUiDispatcher._executor_loop` / `_execute_entry` | 按 `execute_interval_seconds` 节拍消费指令，回执落 results.jsonl |
| 引擎（回执消费） | `LiveEngine._consume_execution_receipts` | deals 轮询点（记账锁内）弹出执行失败回执，在途单即时 rejected |

**挂载门控（通道无关）**：`LiveEngine` 构造时按
`getattr(dispatcher, "file_handoff_enabled", False)` 判定是否挂 `FileHandoff`
（不检查 `channel` 名）。`ThsUiDispatcher` 在 `file_builtin` 模式置
`file_handoff_enabled = True`，`direct` 模式为 `False`。任何新通道 dispatcher
置该属性即可让引擎自动挂交接层。

## 3. 文件与逐字段定义

目录：`{ths_ui.work_dir}/{session_id}/`（相对路径按 Core 服务进程 CWD 解析，
默认 `main/data/ths/{session_id}/`）。全部 JSONL 文件 UTF-8、每行一个 JSON 对象。

### 3.1 signals.jsonl（引擎 → 外部，诊断/对账副本）

引擎 `_emit("signal", payload)` 逐行落盘：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 恒 `1`（§8） |
| `ts` | str | 上海时间 ISO（秒精度） |
| `session_id` | int | 实盘 session id |
| `type` | str | 恒 `"signal"` |
| `signal_name` / `signal_type` / `stock_code` / `strategy_id` / `portfolio_id` / `bar_time` / `trigger_value` 等 | - | 引擎 signal 事件 payload 原样透传（决策层事件形状，随事件类型可含额外键） |

### 3.2 risk.jsonl（引擎 → 外部，诊断/对账副本）

引擎 `_emit("decision"|"risk", payload)` 落盘（同一文件两类事件混排，按 `type` 区分）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 恒 `1` |
| `ts` | str | 上海时间 ISO |
| `session_id` | int | session id |
| `type` | str | `"risk"`（风控事件）或 `"decision"`（决策闸门事件） |
| decision 事件 | - | `gate`/`layer`/`action`/`stock_code`/`strategy_id`/`portfolio_id`/`param_name`/`param_value`/`actual_value`/`requested_qty`/`final_qty`/`message`/`bar_time` |
| risk 事件 | - | `rule`/`message`/`portfolio_id` 等（随规则不同） |

### 3.3 orders.jsonl（决策层 → 执行层，**契约主体**）

`place_order` 秒回前追加一行（`append_order`），`seq` 由 `FileHandoff` 分配：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 恒 `1` |
| `seq` | int | 单调递增（从文件已有物理行数起底，Core 重启仍单调；损坏行也占位） |
| `ts` | str | 上海时间 ISO |
| `session_id` | int | session id |
| `order_id` | str | **确定性幂等键**：md5(strategy_id+portfolio_id+stock_code+trade_type+signal_type+signal_name+quantity+price+bar_time)；同参重发得同一 id |
| `stock_code` | str | 带后缀代码（如 `000001.SZ`） |
| `code` | str | 6 位纯数字代码（UI 自动化输入用） |
| `direction` | str | `"BUY"` / `"SELL"` |
| `quantity` | int | 股数 |
| `price` | float | 委托价 |
| `strategy_id` / `portfolio_id` | int | 归属 |
| `signal_type` | str\|null | `"OPEN"`/`"ADD"`/`"REDUCE"`/`"CLOSE"` |
| `signal_name` | str | 触发信号名 |
| `bar_time` | str\|null | 信号 bar 时间 ISO |

### 3.4 results.jsonl（执行层 → 决策层，执行回执）

两类行混排：

**a. 指令回执**（`mark_done`，带 `order_id`，同时进 cursor 幂等表）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 恒 `1` |
| `ts` | str | 上海时间 ISO |
| `session_id` | int | session id |
| `order_id` | str | 对应 orders.jsonl 指令 |
| `status` | str | `"accepted"`（执行成功）或 `"rejected"`（执行失败） |
| `ref` | str | accepted 时：柜台合同编号（用于委托表认领/成交回填） |
| `error` | str | rejected 时：失败原因（OCR 未就绪 / UI 异常 / 确认框未确认等） |

**b. 通用诊断事件**（`write_result`，无 `order_id`、不进 done 表）：
`schema_version`/`ts`/`session_id` + 事件自定义键（如 `event`）。

**引擎侧即时消费（工作台可见性）**：执行器 rejected 同时暂存内存
`_file_rejects`，`LiveEngine._tick_deals` 在记账锁内经
`dispatcher.execution_receipts()` 弹出（单消费者、弹出即清），按
`order_id`（=确定性 `bridge_order_id`）匹配在途 submitted 单即时标
`rejected`（`error_message` = 回执 `error`）+ 发 order 事件；order_id 匹配不上
回退按 entry 形状（`stock_code`+`direction`+`quantity`）匹配无 `order_ref`
的在途单；匹配不上记 warning 丢弃（由 180s expire 兜底）。此前拒单只进
results.jsonl，在途单停留 submitted 直到超时——即时消费让工作台秒级可见。

### 3.5 cursor.json（执行层消费游标，执行器私有）

```json
{"orders_offset": 12, "done": {"<order_id>": {"status": "accepted", "ref": "100001"}}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `orders_offset` | int | 已消费的 orders.jsonl 物理行数 |
| `done` | map | order_id → 回执摘要（幂等依据） |

## 4. seq / 游标语义

- **seq 单调递增**：以 orders.jsonl 已有物理行数起底（含损坏行占位），Core 重启
  后继续递增，不回退、不复用；
- **at-most-once 消费**：`read_pending_orders` 读后游标立即推进到文件尾（先推进
  再执行）——执行前崩溃的单不会重复执行，由引擎 180s expire 兜底标 rejected；
- **断点续读**：执行器重启后从 `orders_offset` 继续读，不重放历史指令；
- **幂等 done 表**：orders.jsonl 被外部截断/轮转导致 offset 回退重扫时，`done`
  表里的 order_id 跳过——已执行单不会因重扫而重复执行（真钱安全的关键不变量）。

## 5. 原子性与损坏容错

- **写入**：逐行 `append + flush + 实例级线程锁`（`_write_line`）；`schema_version`
  在落盘点统一注入；
- **cursor.json 原子写**：tmp 文件 + `os.replace`——崩溃不留半截游标（半截游标
  会导致 done 表丢失 → 已执行单被重扫重执行，真钱风险）；
- **坏行跳过**：orders.jsonl 中 JSON 解析失败的行跳过并 warning（不中断消费，
  行仍占游标位）；cursor.json 损坏/缺失时 done 表一并丢失，offset 初始化到当前
  文件尾——历史指令不重放（at-most-once 不降级为重复下单）；
- **目录缺失**：读容错返回空（不抛异常），`FileHandoff` 构造时重建目录。

## 6. 执行器实现要求清单

1. **消费节拍**：轮询间隔取自配置 `ths_ui.execute_interval_seconds`（默认 5s），
   daemon 线程惰性启动（首次 place_order 或 connect 成功时）；
2. **幂等**：必须维护 done 表（cursor.json），任何重扫不得重复执行已回执单；
3. **回执**：成功 `{"status":"accepted","ref":<合同编号>}`；失败
   `{"status":"rejected","error":<原因>}`——UI 异常必须捕获落 rejected 回执，
   不得杀消费线程；同时暂存内存回执供引擎即时消费（`execution_receipts()`）；
4. **无 cancel 通道的超时兜底**：执行器不提供撤单时，超时在途单由引擎 expire
   （180s）标 rejected 收口——执行器可依赖该兜底，不必自行实现超时逻辑；
5. **读写纪律**：只消费 `read_pending_orders()` 返回的指令（游标已推进，禁止
   自行回扫）；results 追加行必须带 `order_id` 才能进 done 表与被引擎匹配；
6. **跨引擎 UI 互斥**：UI 自动化通道（tdx / ths）的物理键鼠操作必须串行，
   共用 `core/engine/live/win_dialog_scope.py` 的 `UI_AUTO_LOCK`
   （`threading.RLock`）。**该锁只保证同一后端进程内互斥**：另一个后端进程、
   独立探针脚本、用户手工操作都不排队。因此任何 UI 安全结论都必须配「点击前
   遮挡校验」（`WindowFromPoint` + `GetAncestor(GA_ROOT)`，被挡则拉前台复检，
   仍被挡就 fail-closed 中止），不能只依赖锁。锁序恒定
   `_GRID_LOCK`（通道内 GUI 锁）→ `UI_AUTO_LOCK`（跨通道）；获取一律带超时，
   超时即「本轮不点、下轮重试」。

## 7. 通道接入指南

新通道接入只需一个适配器 dispatcher：

```python
class MyChannelDispatcher(OrderDispatcher):
    file_handoff_enabled = True          # 引擎据此自动挂 FileHandoff（事件落文件）
    supports_remark = False              # 可选：无 remark 时走 legacy 模糊匹配

    def place_order(self, order) -> None:
        # 参数校验后：self._handoff.append_order({...契约 §3.3 字段...})
        # 返回 None；引擎按 last_enqueued_order_id 识别「已入队」
        ...

    def execution_receipts(self) -> list:  # 可选：即时拒单（否则等 180s expire）
        ...

    # 真实回报读取二选一：
    #   a) query_orders/query_deals/query_positions/query_account（读真实柜台表，
    #      /orders 形状见 ThsUiDispatcher.query_orders）——推荐；
    #   b) 仅写 results.jsonl 回执（无柜台回报可读的执行端）。
```

引擎侧零改动：挂载门控、入队识别（`last_enqueued_order_id`）、回执消费、
expire 兜底全部复用。

## 8. 版本策略

`schema_version = 1` 的向后兼容规则：

- **新增字段**：可选字段随时可加（读取方必须容忍未知键）——**不升版**；
- **改名 / 删除 / 语义变更**：必须升版（`_SCHEMA_VERSION = 2`），并保留旧版
  读取兼容或提供迁移说明；
- **读取方义务**：按「缺键回退默认值」解析，不得假设字段恒在（除本文档标注
  恒定的 `schema_version`/`type`/`status` 外）；
- 游标 `cursor.json` 不携带 schema_version：其结构由实现内聚（`_load_cursor`
  容错解析），不对外承诺兼容。
