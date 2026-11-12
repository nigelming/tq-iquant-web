# 0018 hermes session 模式 —— 前端页面配置「哪些走 hermes」

> 状态：**已实施**（2026-11-12，后端 38 项相关测试 + 前端 428 全绿，真机验收待实盘日）
> 日期：2026-11-12
> 接续：0017 hermes 日频实盘信号（本计划是它的标记粒度延伸）
> 用户定调：session 级标记（推荐）；hermes 接口默认只返回 hermes session（推荐）

## 1. 背景与目标

0017 落地后，hermes 接口返回**所有** session 的信号，前端也没有「哪些走 hermes」的配置。
本计划把「通过 hermes 人工执行」做成 session 级执行模式：新建实盘时选
`hermes`（新建默认值），该 session 的信号被标记并可被 hermes 接口单独拉取。

## 2. 方案设计

### 2.1 执行模式扩展（session 级）
- `execution_mode` 新增第三个合法值 `hermes`（`String(10)` 列，无迁移）。
- **行为同 alert**：AlertDataDispatcher（TQ 行情、禁止下单）、只算信号落库 + SSE、
  跳过 recover。所有 `== "alert"` 分支收敛到 `core/engine/live/modes.py` 的
  `is_signal_only()` 谓词（`SIGNAL_ONLY_MODES = ("alert", "hermes")`）。
- 引擎 key：signal-only 模式用模式名本身（快照表 `alert`/`hermes` 可区分）。
- preflight：`hermes` 走 alert 同款（公式试算保留；send_warn 探测按 0017 开关跳过）。
- 新建默认值：`create_session` 缺省 `execution_mode` 由 `alert` → `hermes`
  （前端表单默认同步），老会话不受影响。

### 2.2 hermes 接口过滤
- `GET /api/hermes/signals` 默认只返回 `execution_mode == "hermes"` session 的信号
  （auto 会话信号混入会让 hermes 端误下单）。
- `include_all=1` 时返回全部 session（alert/auto 也含），供排查/审计。

### 2.3 前端（LiveSessions.vue）
- `LiveExecutionMode` 类型加 `'hermes'`；标签映射 `hermes: 'Hermes'`。
- 新建表单：默认 `hermes`，下拉首项「Hermes（人工执行…）」，alert/auto 说明文案更新
  （alert 注明 send_warn 受 0017 开关控制）。
- 会话列表：模式徽章 title 按 hermes/alert 区分；配置指南补 hermes 段。

## 3. 验收

- [x] preflight 接受 hermes / 拒绝非法值（400，提示 alert/auto/hermes）
- [x] hermes 启动 → AlertDataDispatcher + `execution_mode` 保持 `hermes` + 引擎 key `hermes`
- [x] 引擎层：hermes tick 走 `_tick_alert`（不拉持仓/委托/成交）、bar 回调走 `_handle_alert_bar`
- [x] create 默认 hermes
- [x] hermes API 默认过滤非 hermes session；`include_all=1` 返回全部
- [x] 前端：默认值/徽章/选项/指南（vitest 428 全绿）
- [ ] 真机：新建 hermes session → 14:20 信号落库（execution_mode=hermes）→ Mac
      `curl /api/hermes/signals` 只见 hermes session 信号

## 4. 风险与边界

- 历史 alert session 的信号**不再**出现在 hermes 接口默认结果里（需要时 `include_all=1`，
  或把 session 改为 hermes 模式重建）。
- `live_signals.execution_mode` 列随信号写入模式名（hermes 信号该列为 `hermes`），
  前端信号 tab 与对账查询注意区分。
- 无迁移：`execution_mode` 为 `String(10)` 无 CHECK 约束，`"hermes"` 直接可存。
