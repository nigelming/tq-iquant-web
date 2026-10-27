# 组合管理页「实盘预警调度」表单字段 — 设计

日期：2026-10-27
状态：设计已确认

## 背景

实盘信号预警（alert 半自动模式）已实现，策略级信号调度字段 `signal_frequency` /
`signal_at` / `signal_weekday` / `signal_data_end` 后端模型、API 校验、序列化均已就绪，
但 **Web 组合管理页（Portfolios.vue）未暴露这些字段**，用户只能直接调 API 配置
（例如「日线 + 收盘后 15:05」以匹配回测「信号次日开盘价成交」口径）。本次补齐前端入口。

## 目标

1. 组合管理页子策略表单可编辑 4 个预警调度字段，与后端 `validate_signal_config`
   同口径校验。
2. 子策略列表行展示当前调度摘要（如「信号 1d@15:05」），无需打开表单即可确认。
3. 后端零改动。

## 非目标

- 不改预警运行时（SignalScheduler / SignalMonitor / send_warn 等）。
- 不在实盘交易页新增字段联动（执行模式选择已有 alert/auto）。
- 不改回测侧频率门控。

## 字段语义（与后端一致）

| 字段 | 类型 | NULL 语义 | 说明 |
|---|---|---|---|
| `signal_frequency` | String(10) nullable | 用 `period`（跟随周期） | 信号推送频率：1m/5m/15m/30m/1h/1d/1w/1mon，不得细于 period |
| `signal_at` | String(5) nullable | 按频率默认 | `HH:MM`；1d 默认 14:30、1w/1mon 默认 09:00、日内频率忽略 |
| `signal_weekday` | Integer nullable | 1（周一） | 仅 1w 使用，1-5 |
| `signal_data_end` | String(10) nullable | 按频率默认 | `current`（1d/日内默认）/ `completed`（1w/1mon 强制） |

## 改动

### 1. 表单：`STRATEGY_GROUPS` 新增分组「实盘预警调度」

放在「策略增强」分组之后。字段：

- `signal_frequency`：select。空选项 label「跟随周期（period）」；选项 1m/5m/15m/30m/1h/1d/1w/1mon。
- `signal_at`：text。placeholder「按频率默认（如 15:05）」；校验 `HH:MM` 正则。
- `signal_weekday`：number。placeholder「周一（1-5，仅 1w）」。校验 1-5。
- `signal_data_end`：select。`current`（当前，默认）/ `completed`（已完成，1w/1mon 强制）。
- 分组 hint：「实盘预警（alert 半自动）按此调度推送；每日收盘后发信号请设 15:05，次日开盘操作。」

`emptyStrategyForm()` 默认值：
- `signal_frequency: null`
- `signal_at: ''`（text 空串，提交时归一 null）
- `signal_weekday: null`
- `signal_data_end: ''`（text 空串，提交时归一 null）

`openEditStrategy()` 从 `StrategyDetail` 回填 4 字段。

### 2. 提交归一与校验

- `buildStrategyPayload()`：`signal_at` / `signal_data_end` 空串 → `null`（沿用
  `NULLABLE_NUMERIC_FIELDS` 模式，text 字段在 `payloadValue` 里特判）。
- `validateStrategyPayload()` 追加：
  - `signal_at` 非空时须匹配 `/^\d{2}:\d{2}$/`；
  - `signal_weekday` 非空时须 1-5；
  - `signal_frequency` 非空时 rank 不得低于 `period` rank（rank 表与后端
    `_RANK` 一致：1m<5m<15m<30m<1h<1d<1w<1mon）。

### 3. 列表行摘要

新增 helper `signalScheduleLabel(s)`，在子策略子行 `.sub-strategy-meta` 追加显示：

- `freq = signal_frequency || period`
- 有 `signal_at` → `${freq}@${signal_at}`；否则 `${freq}`
- `freq === '1w'` 且有 `signal_weekday` → 附「周X」
- `signal_data_end === 'completed'` → 附「已完成」
- 全部空（跟随周期、无 at、非 1w、current）→ 显示 `信号 ${period}`（无附加标注）

### 4. 类型（`web/src/api/index.ts`）

`StrategyDetail` / `StrategyRequest` 各加 4 个可选字段：
`signal_frequency: string | null`、`signal_at: string | null`、
`signal_weekday: number | null`、`signal_data_end: string | null`。

## 测试

前端 vitest（`web/src/__tests__/`）：

1. `signalScheduleLabel`：`1d@15:05`、`1w@09:00 周一 已完成`、跟随周期无标注、`1d` current 无标注。
2. 表单打开回填 4 字段；提交时 `signal_at`/`signal_data_end` 空串归一 null。
3. 频率校验：`period=1d, signal_frequency=1m` → 拦截提示；`signal_at=9:5` → 拦截；
   `signal_weekday=6` → 拦截。

## 实施清单

- [ ] `web/src/api/index.ts`：类型加 4 字段。
- [ ] `web/src/views/Portfolios.vue`：STRATEGY_GROUPS 新分组、emptyStrategyForm、
      openEditStrategy 回填、buildStrategyPayload 归一、validateStrategyPayload 校验、
      signalScheduleLabel + 列表行展示。
- [ ] `web/src/__tests__/`：新增测试（Portfolios 表单 + helper）。
