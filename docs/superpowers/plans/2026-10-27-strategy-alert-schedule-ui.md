# 组合管理页「实盘预警调度」字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 组合管理页子策略表单可编辑实盘预警调度 4 字段（`signal_frequency`/`signal_at`/`signal_weekday`/`signal_data_end`），子策略列表行显示调度摘要；后端零改动。

**Architecture:** 前端元数据驱动表单（`STRATEGY_GROUPS`）追加「实盘预警调度」分组；提交沿用现有 `payloadValue`/`fromPercent` 归一管道（text 空串→null、可空数值清空→null）；提交前校验与后端 `validate_signal_config` 同口径（频率 rank、HH:MM、1-5）；列表行经 `signalScheduleLabel` 生成摘要。

**Tech Stack:** Vue 3 `<script setup>` + TypeScript + vitest / @vue/test-utils（happy-dom）

**Spec:** `docs/superpowers/specs/2026-10-27-strategy-alert-schedule-ui-design.md`

## Global Constraints

- 后端零改动：字段与序列化已就绪（`main/core/api/strategies.py:83-86`、`main/core/services/strategy_service.py:52-55`）
- 校验口径与 `main/core/services/signal_schedule.py` 一致：频率 rank `1m<5m<15m<30m<1h<1d<1w<1mon`；`signal_at` 须 `HH:MM`；`signal_weekday` 1-5
- 空值提交必须归一 `null`，不得发 `''`（后端 pydantic 可空字段）
- 对 spec 的实现细化：`signal_data_end` 用 null 原生 select（空选项 value=null），行为与 spec「空→null」等价；`signal_at` 是 text 输入，保留 `'' → null` 归一
- 测试命令在 `web/` 目录运行；提交信息用中文 conventional commit（`feat(web): ...`）

---

### Task 1: 表单「实盘预警调度」分组（渲染/默认/回填/提交归一）

**Files:**
- Modify: `web/src/api/index.ts`（StrategyDetail / StrategyRequest 接口）
- Modify: `web/src/views/Portfolios.vue`（常量、分组、表单函数、模板、样式）
- Test: `web/src/__tests__/Portfolios.test.ts`（追加 describe）

**Interfaces:**
- Consumes: 后端 `serialize_strategy` 返回的 `signal_frequency/signal_at/signal_weekday/signal_data_end`（均可空）
- Produces: `SIGNAL_FREQUENCIES: string[]`、`SIGNAL_FREQ_RANK: Record<string, number>`、`WEEKDAY_LABELS: Record<number, string>`（Task 2/3 复用）；payload 中 4 字段类型 `string | number | null`

- [ ] **Step 1: 追加失败测试到 `web/src/__tests__/Portfolios.test.ts` 文件末尾**

```ts
describe('Portfolios.vue — 实盘预警调度（表单）', () => {
  async function openNewStrategy(w: any) {
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
    await w.findAll('button').find((b: any) => b.text().includes('新建子策略'))!.trigger('click')
  }

  it('子策略表单含「实盘预警调度」组与四个字段', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)

    expect(w.text()).toContain('实盘预警调度')
    expect(w.text()).toContain('信号频率')
    expect(w.text()).toContain('推送时点')
    expect(w.text()).toContain('周几推送')
    expect(w.text()).toContain('数据口径')
    const freqSel = w.find('select[data-field="signal_frequency"]')
    expect(freqSel.exists()).toBe(true)
    expect(freqSel.html()).toContain('跟随周期')
    expect(w.find('input[data-field="signal_at"]').exists()).toBe(true)
    expect(w.find('input[data-field="signal_weekday"]').exists()).toBe(true)
    const deSel = w.find('select[data-field="signal_data_end"]')
    expect(deSel.exists()).toBe(true)
    expect(deSel.html()).toContain('按频率默认')
  })

  it('默认不填 → payload 四字段全 null（不发空串）', async () => {
    ;(createStrategy as any).mockResolvedValue({ id: 20 })
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)
    await w.find('input[data-field="name"]').setValue('ALERT_DEFAULT')
    await w.find('select[data-field="formula_id"]').setValue('1')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    const arg = (createStrategy as any).mock.calls[0][1]
    expect(arg.signal_frequency).toBeNull()
    expect(arg.signal_at).toBeNull()
    expect(arg.signal_weekday).toBeNull()
    expect(arg.signal_data_end).toBeNull()
  })

  it('填写 1d@15:05 + current → payload 原样携带', async () => {
    ;(createStrategy as any).mockResolvedValue({ id: 20 })
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)
    await w.find('select[data-field="signal_frequency"]').setValue('1d')
    await w.find('input[data-field="signal_at"]').setValue('15:05')
    await w.find('select[data-field="signal_data_end"]').setValue('current')
    await w.find('input[data-field="name"]').setValue('ALERT_SET')
    await w.find('select[data-field="formula_id"]').setValue('1')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    const arg = (createStrategy as any).mock.calls[0][1]
    expect(arg.signal_frequency).toBe('1d')
    expect(arg.signal_at).toBe('15:05')
    expect(arg.signal_weekday).toBeNull()
    expect(arg.signal_data_end).toBe('current')
  })

  it('编辑回填 1w@09:00/周一/completed，提交 round-trip；清空 weekday → null', async () => {
    ;(updateStrategy as any).mockResolvedValue({ id: 10 })
    ;(getStrategies as any).mockResolvedValue([{
      ...mockStrategies[0],
      signal_frequency: '1w', signal_at: '09:00', signal_weekday: 1, signal_data_end: 'completed',
    }])
    const w = mount(Portfolios)
    await flushPromises()
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
    const editBtn = w.findAll('.strategy-sub-row button').find((b: any) => b.text() === '编辑')!
    await editBtn.trigger('click')
    await flushPromises()

    expect((w.find('select[data-field="signal_frequency"]').element as HTMLSelectElement).value).toBe('1w')
    expect((w.find('input[data-field="signal_at"]').element as HTMLInputElement).value).toBe('09:00')
    expect(Number((w.find('input[data-field="signal_weekday"]').element as HTMLInputElement).value)).toBe(1)

    await w.find('input[data-field="signal_weekday"]').setValue('')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updateStrategy).toHaveBeenCalledTimes(1)
    const arg = (updateStrategy as any).mock.calls[0][2]
    expect(arg.signal_frequency).toBe('1w')
    expect(arg.signal_at).toBe('09:00')
    expect(arg.signal_weekday).toBeNull()
    expect(arg.signal_data_end).toBe('completed')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 新 describe 内 4 个用例 FAIL（找不到「实盘预警调度」/ `data-field="signal_frequency"` 不存在），既有用例 PASS

- [ ] **Step 3: 实现类型扩展（`web/src/api/index.ts`）**

`StrategyDetail` 的 `exposure_cap_enabled: boolean` 后追加：

```ts
  signal_frequency?: string | null
  signal_at?: string | null
  signal_weekday?: number | null
  signal_data_end?: string | null
```

`StrategyRequest` 的 `exposure_cap_enabled?: boolean` 后追加同样 4 行。

- [ ] **Step 4: 实现 Portfolios.vue 常量与分组**

① `DAILY_LOSS_BASES` 定义后追加：

```ts
// 实盘预警调度（alert 半自动）：NULL=跟随默认；rank 与后端 validate_signal_config 同口径
const SIGNAL_FREQUENCIES = ['1m', '5m', '15m', '30m', '1h', '1d', '1w', '1mon']
const SIGNAL_FREQ_RANK: Record<string, number> = {
  '1m': 0, '5m': 1, '15m': 2, '30m': 3, '1h': 4, '1d': 5, '1w': 6, '1mon': 7,
}
const WEEKDAY_LABELS: Record<number, string> = { 1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五' }
```

② `NULLABLE_NUMERIC_FIELDS` 的 `'trend_trailing_stop_ratio', 'trend_max_positions',` 后加一行 `'signal_weekday',`。

③ `STRATEGY_GROUPS` 类型注解改为 `{ title: string; hint?: string; fields: FieldMeta[] }[]`，并在「策略增强」分组（`exposure_cap_enabled` 行所在组）的 `]},` 之后、数组 `]` 之前追加：

```ts
  { title: '实盘预警调度', hint: '实盘预警（alert 半自动）按此调度推送；每日收盘后发信号请设 15:05，次日开盘操作。', fields: [
    { key: 'signal_frequency', label: '信号频率', span: 1, type: 'select', options: 'signalFrequencies' },
    { key: 'signal_at', label: '推送时点', span: 1, type: 'text', placeholder: 'HH:MM，按频率默认（如 15:05）' },
    { key: 'signal_weekday', label: '周几推送', span: 1, type: 'number', placeholder: '1-5（仅 1w，默认周一）' },
    { key: 'signal_data_end', label: '数据口径', span: 1, type: 'select', options: 'signalDataEnds' },
  ]},
```

- [ ] **Step 5: 实现表单函数**

① `emptyStrategyForm()` 的 `exposure_cap_enabled: false,` 后追加：

```ts
    signal_frequency: null, signal_at: '', signal_weekday: null, signal_data_end: null,
```

② `openEditStrategy()` 的 `exposure_cap_enabled: s.exposure_cap_enabled,` 后追加：

```ts
    signal_frequency: s.signal_frequency ?? null,
    signal_at: s.signal_at ?? '',
    signal_weekday: s.signal_weekday ?? null,
    signal_data_end: s.signal_data_end ?? null,
```

③ `buildStrategyPayload()` 的 role 归一块之后、`return out as unknown as StrategyRequest` 之前追加：

```ts
  // 实盘预警调度：text 空串归一 null（后端可空字段，发 '' 会 422）
  if (out.signal_at === '') out.signal_at = null
```

④ `optionsOf()` 的 `dailyLossBases` 分支后追加：

```ts
  if (src === 'signalFrequencies') return [
    { value: null, label: '跟随周期（period）' },
    ...SIGNAL_FREQUENCIES.map(f => ({ value: f, label: f })),
  ]
  if (src === 'signalDataEnds') return [
    { value: null, label: '按频率默认（1d/日内=current，1w/1mon=completed）' },
    { value: 'current', label: 'current（当前）' },
    { value: 'completed', label: 'completed（已完成）' },
  ]
```

- [ ] **Step 6: 模板与样式**

① 子策略 Modal（`v-for="g in STRATEGY_GROUPS"` 处）的分组标题改为：

```html
<label class="group-label">{{ g.title }}<span v-if="g.hint" class="group-hint">{{ g.hint }}</span></label>
```

② 文件末尾空 `<style scoped>` 块内加：

```css
.group-hint { margin-left: 8px; font-size: 11px; font-weight: 400; color: var(--ink-faint); }
```

- [ ] **Step 7: 运行测试确认通过**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 全部 PASS（含既有用例）

- [ ] **Step 8: Commit**

```bash
git add web/src/api/index.ts web/src/views/Portfolios.vue web/src/__tests__/Portfolios.test.ts
git commit -m "feat(web): 子策略表单新增实盘预警调度字段"
```

---

### Task 2: 提交前校验（频率 rank / HH:MM / 1-5）

**Files:**
- Modify: `web/src/views/Portfolios.vue`（`validateStrategyPayload`）
- Test: `web/src/__tests__/Portfolios.test.ts`（追加 describe）

**Interfaces:**
- Consumes: Task 1 的 `SIGNAL_FREQ_RANK`（模块级常量）
- Produces: `validateStrategyPayload` 对 4 个调度字段的拦截文案：「信号频率不能比周期更细」「推送时点须为 HH:MM（如 15:05）」「周几推送须为 1-5」

- [ ] **Step 1: 追加失败测试到文件末尾**

```ts
describe('Portfolios.vue — 实盘预警调度（提交校验）', () => {
  async function openNewStrategy(w: any) {
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
    await w.findAll('button').find((b: any) => b.text().includes('新建子策略'))!.trigger('click')
  }

  it('推送时点非 HH:MM（9:5）→ 拦截且不提交', async () => {
    ;(createStrategy as any).mockClear()
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)
    await w.find('input[data-field="signal_at"]').setValue('9:5')
    await w.find('input[data-field="name"]').setValue('BAD_AT')
    await w.find('select[data-field="formula_id"]').setValue('1')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()
    expect(alertMock.mock.calls[0][0]).toContain('HH:MM')
    expect(createStrategy).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('周几 6 → 拦截且不提交', async () => {
    ;(createStrategy as any).mockClear()
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)
    await w.find('select[data-field="signal_frequency"]').setValue('1w')
    await w.find('input[data-field="signal_weekday"]').setValue('6')
    await w.find('input[data-field="name"]').setValue('BAD_WD')
    await w.find('select[data-field="formula_id"]').setValue('1')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()
    expect(alertMock.mock.calls[0][0]).toContain('1-5')
    expect(createStrategy).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('period=1d 选频率 1m（更细）→ 拦截且不提交', async () => {
    ;(createStrategy as any).mockClear()
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Portfolios)
    await flushPromises()
    await openNewStrategy(w)
    await w.find('select[data-field="period"]').setValue('1d')
    await w.find('select[data-field="signal_frequency"]').setValue('1m')
    await w.find('input[data-field="name"]').setValue('BAD_FREQ')
    await w.find('select[data-field="formula_id"]').setValue('1')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()
    expect(alertMock.mock.calls[0][0]).toContain('不能比周期更细')
    expect(createStrategy).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 3 个新用例 FAIL（无校验，`createStrategy` 被调用）；其余 PASS

- [ ] **Step 3: 实现 `validateStrategyPayload`**

在比例循环之后、`return null` 之前追加：

```ts
  // 实盘预警调度（与后端 validate_signal_config 同口径）
  const freq = p.signal_frequency as string | null | undefined
  if (freq) {
    if (!(freq in SIGNAL_FREQ_RANK)) return '信号频率非法'
    const periodRank = SIGNAL_FREQ_RANK[String(p.period)]
    if (periodRank !== undefined && SIGNAL_FREQ_RANK[freq] < periodRank) return '信号频率不能比周期更细'
  }
  const at = p.signal_at as string | null | undefined
  if (at && !/^\d{2}:\d{2}$/.test(String(at))) return '推送时点须为 HH:MM（如 15:05）'
  const wd = num(p.signal_weekday)
  if (wd !== null && !(wd >= 1 && wd <= 5)) return '周几推送须为 1-5'
```

- [ ] **Step 4: 运行测试确认通过**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Portfolios.vue web/src/__tests__/Portfolios.test.ts
git commit -m "feat(web): 实盘预警调度提交校验（频率/时点/周几）"
```

---

### Task 3: 列表行调度摘要 signalScheduleLabel

**Files:**
- Modify: `web/src/views/Portfolios.vue`（helper + 子行模板）
- Test: `web/src/__tests__/Portfolios.test.ts`（追加 describe）

**Interfaces:**
- Consumes: Task 1 的 `WEEKDAY_LABELS`
- Produces: `signalScheduleLabel(s): string`（仅模板内使用）

- [ ] **Step 1: 追加失败测试到文件末尾**

```ts
describe('Portfolios.vue — 子策略列表行预警调度摘要', () => {
  it('展开后子行显示「信号 1d@15:05」与「信号 1w@09:00 周一 已完成」', async () => {
    ;(getStrategies as any).mockResolvedValue([
      { ...mockStrategies[0], signal_frequency: '1d', signal_at: '15:05', signal_weekday: null, signal_data_end: 'current' },
      { ...mockStrategies[1], signal_frequency: '1w', signal_at: '09:00', signal_weekday: 1, signal_data_end: 'completed' },
    ])
    const w = mount(Portfolios)
    await flushPromises()
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
    expect(w.text()).toContain('信号 1d@15:05')
    expect(w.text()).toContain('信号 1w@09:00 周一 已完成')
  })

  it('未配置调度 → 「信号 5m」（跟随周期，无 @ 标注）', async () => {
    ;(getStrategies as any).mockResolvedValue([{ ...mockStrategies[0], period: '5m' }])
    const w = mount(Portfolios)
    await flushPromises()
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
    expect(w.text()).toContain('信号 5m')
    expect(w.text()).not.toContain('信号 5m@')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 2 个新用例 FAIL（子行无「信号 …」文案）；其余 PASS

- [ ] **Step 3: 实现 helper 与模板**

① `optionsOf()` 函数之后追加：

```ts
// 子策略列表行的预警调度摘要：「信号 1d@15:05」「信号 1w@09:00 周一 已完成」；未配置=跟随周期
function signalScheduleLabel(s: {
  period: string
  signal_frequency?: string | null
  signal_at?: string | null
  signal_weekday?: number | null
  signal_data_end?: string | null
}): string {
  const freq = s.signal_frequency || s.period
  let label = `信号 ${freq}`
  if (s.signal_at) label += `@${s.signal_at}`
  if (freq === '1w' && s.signal_weekday) {
    label += ` ${WEEKDAY_LABELS[s.signal_weekday] || `周${s.signal_weekday}`}`
  }
  if (s.signal_data_end === 'completed') label += ' 已完成'
  return label
}
```

② 子行 meta（`sub-strategy-meta` span）末尾追加摘要：

```html
<span class="sub-strategy-meta">{{ formulaName(s.formula_id) }} · {{ s.period }} · 资金 {{ (s.capital_ratio * 100).toFixed(0) }}% · {{ signalScheduleLabel(s) }}</span>
```

- [ ] **Step 4: 运行测试确认通过**

Run（workdir `web/`）: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Portfolios.vue web/src/__tests__/Portfolios.test.ts
git commit -m "feat(web): 子策略列表行显示预警调度摘要"
```

---

### Task 4: 全量回归 + 类型检查

**Files:**
- 无新改动（验证性任务；如有修复随修复提交）

**Interfaces:**
- Consumes: Task 1-3 全部改动
- Produces: 回归通过结论

- [ ] **Step 1: 前端全量测试**

Run（workdir `web/`）: `npx vitest run`
Expected: 全部 PASS（含 LiveSessions/Backtest 等其它套件）

- [ ] **Step 2: 类型检查**

Run（workdir `web/`）: `npx vue-tsc -b`
Expected: 无类型错误（验证 api 类型与 Portfolios.vue 引用一致）

- [ ] **Step 3: 如有修复则提交**

```bash
git add -A web/src
git commit -m "fix(web): 实盘预警调度回归修复"
```
（无修复则跳过）
