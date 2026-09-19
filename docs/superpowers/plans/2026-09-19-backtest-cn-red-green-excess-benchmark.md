# 回测报告 A 股红涨绿跌扩展 + 超额收益曲线 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在回测详情报告页落地 A 股红涨绿跌（策略对比收益指标、回撤曲线、摘要卡）并给净值曲线新增「超额收益（相对基准）」线。

**Architecture:** 纯前端 `web/src/views/Backtest.vue` + 测试 `web/src/__tests__/Backtest.test.ts`。后端零改动——基准配置链路（Portfolios.vue `benchmark_index` → TQ 拉指数日线 → 快照 `benchmark_value`）已存在。所有改动通过 TDD 进行。

**Tech Stack:** Vue 3 `<script setup lang="ts">`、ECharts（echarts 在测试中整体 mock 成桩）、vitest + @vue/test-utils、SCSS（组件内 `<style>`）。

## Global Constraints

- 分支 `feat/backtest-return-heatmap`。
- 本计划开工前，工作区已有**未提交**改动（`Backtest.vue`、`Backtest.test.ts`）：收益分布数值 4 位小数、月度热力图/年收益柱红涨绿跌。本计划在这批改动之上继续。第 1 步先把它们连带提交，使基线干净。
- 测试命令（`web/` 目录）：全量 `npx vitest run`；指定文件 `npx vitest run src/__tests__/Backtest.test.ts`。
- 类型检查：`npx vue-tsc --noEmit`。构建：`npm run build`。
- **A 股红涨绿跌约定**：正收益/涨=红 `#ef4444`，负收益/跌=绿 `#10b981`。仅应用于收益类语义；风险类硬编码 `text-red`（最大回撤/VaR/CVaR）与交易明细买卖方向色不动。
- 净值曲线**不加左右轴**（所有曲线同为「收益率(%)」量纲）。
- 不新增后端代码、不新增依赖、不改数据库。

---

### Task 1: 提交既有未提交改动（基线清理）

**Files:**
- Modify: `web/src/views/Backtest.vue`（已改）
- Modify: `web/src/__tests__/Backtest.test.ts`（已改）

**Interfaces:**
- Consumes: —（无前置依赖）
- Produces: 干净的 git 基线，后续任务 diff 只含本计划的新改动。

- [ ] **Step 1: 确认改动内容仅含收益分布精度 + 红涨绿跌**

Run: `git status --short && git diff --stat`
Expected: 仅 `M web/src/views/Backtest.vue` 与 `M web/src/__tests__/Backtest.test.ts`，约 169 insertions / 9 deletions。

- [ ] **Step 2: 跑一遍现有全量前端测试确保基线绿**

Run: `npx vitest run`
Expected: 11 files passed, 121 tests passed（含既有基准线测试 L294、收益分布配色测试、数值精度测试）。

- [ ] **Step 3: 提交基线**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 收益分布数值 4 位小数 + 月度热力图/年收益柱 A股红涨绿跌"
```

---

### Task 2: 策略对比收益类指标 + 摘要卡红涨绿跌

**Files:**
- Modify: `web/src/views/Backtest.vue`（`valueClass` 约 L170-173；`.summary-card.positive` 约 L958）
- Test: `web/src/__tests__/Backtest.test.ts`（新增 describe「回测报告 — 策略对比/摘要卡红涨绿跌」）

**Interfaces:**
- Consumes: `strategy_evaluations` 详情字段（后端已返回：`{strategy_id, strategy_name, total_return, annual_return, ...}`）；`mockDetail`（现有测试 mock，见 Test L46-62）。
- Produces: `valueClass(v)` 现返回正→`'text-red'`、负→`'text-green'`、null→`''`；`.summary-card.positive` 边框 `border-left-color: #ef4444`，`.summary-card.danger` 保持 `#ef4444`。

- [ ] **Step 1: 写失败测试**

在 `web/src/__tests__/Backtest.test.ts` 文件末尾追加：

```ts
// --- 回测报告 — 策略对比/摘要卡红涨绿跌 ---
describe('Backtest.vue — 策略对比/摘要卡红涨绿跌', () => {
  it('策略对比收益指标正红负绿（valueClass 反转）', async () => {
    ;(getBacktestDetail as any).mockResolvedValue({
      ...mockDetail,
      strategy_evaluations: [
        { strategy_id: 1, strategy_name: '正收益策略', total_return: 0.05, annual_return: 0.08 },
        { strategy_id: 2, strategy_name: '负收益策略', total_return: -0.03, annual_return: -0.05 },
      ],
    })
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('策略对比分析')
    expect(w.text()).toContain('正收益策略')
    expect(w.text()).toContain('负收益策略')

    // 组合整体卡：总收益 5.00%（正）→ text-red
    const allRows = w.findAll('.metric-row')
    const posRow = allRows.find(r => r.text().includes('5.00%'))!
    expect(posRow.find('span:last-child').classes()).toContain('text-red')
    // 负收益策略卡：总收益 -3.00%（负）→ 任一 .strategy-card 内含该值且值为 text-green
    const negCard = w.findAll('.strategy-card').find(c => c.text().includes('-3.00%'))!
    const negSpan = negCard.findAll('span').find(s => s.text().includes('-3.00%'))!
    expect(negSpan.classes()).toContain('text-green')
  })

  it('摘要卡结构：positive（总收益/年化收益）+ danger（最大回撤）存在且各自取值', async () => {
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const positive = w.find('.summary-card.positive')
    expect(positive.exists()).toBe(true)
    expect(positive.text()).toContain('总收益')
    const danger = w.find('.summary-card.danger')
    expect(danger.exists()).toBe(true)
    expect(danger.text()).toContain('最大回撤')
    // 注意：SFC scoped 样式在 vitest(happy-dom) 不注入，边框色无法经 getComputedStyle 断言；
    // 颜色改动（positive→#ef4444、danger 保持 #ef4444）由实现步骤的显式 CSS 修改 + 最终构建验证保证。
  })
})
```

- [ ] **Step 2: 跑测试验证失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: 
- 第一个用例失败于 `expect(posRow.find('span:last-child').classes()).toContain('text-red')`——当前 `valueClass` 对正收益返回 `text-green`。
- 第二个用例（摘要卡结构，仅回归保护）实现前后均通过：边框色是 SFC scoped CSS，happy-dom 不注入、无法经 `getComputedStyle` 断言，故该颜色改动靠 Step 3 显式 CSS 修改 + Step 5 构建验证保证（见用例内注释）。

- [ ] **Step 3: 实现最小改动**

`valueClass`（此时约 L170）：

```ts
// A 股红涨绿跌：正收益→红、负收益→绿
function valueClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return Number(v) >= 0 ? 'text-red' : 'text-green'
}
```

`.summary-card.positive`（此时约 L958，改为红边框；`.danger` 保持 `#ef4444` 不变）：

```css
.summary-card.positive { border-left-color: #ef4444; } /* 正收益 → 红（A股红涨绿跌） */
.summary-card.danger { border-left-color: #ef4444; }   /* 最大回撤 → 风险警示红，保持不变 */
```

- [ ] **Step 4: 跑测试验证通过**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部通过（23 个既有 + 新增 2 = 25）。

- [ ] **Step 5: 全量回归 + 类型检查**

Run: `npx vitest run && npx vue-tsc --noEmit`
Expected: 11 files passed, 123 tests passed；vue-tsc 无输出（通过）。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 策略对比/摘要卡收益指标 A股红涨绿跌（正红负绿）"
```

---

### Task 3: 回撤曲线改绿（跌幅→绿）

**Files:**
- Modify: `web/src/views/Backtest.vue`（`initDrawdownChart` 约 L457-465）
- Test: `web/src/__tests__/Backtest.test.ts`（在「图表数值精度」describe 之后追加新 describe，或在既有「回撤曲线数据 round 到 4 位」测试基础上新增独立用例）

**Interfaces:**
- Consumes: `drawdownCurve` computed（已存在，`initDrawdownChart` 数据源）。
- Produces: `initDrawdownChart` series `itemStyle.color === '#10b981'`，面积渐变 `rgba(16, 185, 129, 0.3/0.05)`。

- [ ] **Step 1: 写失败测试**

在测试文件末尾追加：

```ts
// --- 回测报告 — 回撤曲线 A股红涨绿跌 ---
describe('Backtest.vue — 回撤曲线红涨绿跌', () => {
  it('回撤曲线（跌幅）为绿色', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const dd = options.find((o: any) => o.yAxis?.name === '回撤(%)')
    expect(dd).toBeTruthy()
    const series = dd.series.find((s: any) => s.name === '回撤')
    expect(series.itemStyle.color).toBe('#10b981')
  })
})
```

- [ ] **Step 2: 跑测试验证失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "回撤曲线"`（会同时跑「round 到 4 位」与新的绿色用例）
Expected: 新用例失败于 `expected '#ef4444' to be '#10b981'`。

- [ ] **Step 3: 实现最小改动**

在 `initDrawdownChart`（此时约 L457-465）中改色：

```ts
    series: [{
      name: '回撤', type: 'line', data: drawdownCurve.value, smooth: true,
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(16, 185, 129, 0.3)' },
          { offset: 1, color: 'rgba(16, 185, 129, 0.05)' },
        ]),
      },
      itemStyle: { color: '#10b981' }, // A股红涨绿跌：回撤=跌幅→绿
    }],
```

- [ ] **Step 4: 跑测试验证通过**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部通过（26 个用例：25 + 回撤曲线新用例）。

- [ ] **Step 5: 全量回归 + 类型检查**

Run: `npx vitest run && npx vue-tsc --noEmit`
Expected: 11 files passed, 124 tests passed；vue-tsc 无输出（通过）。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 回撤曲线按 A股红涨绿跌改绿色（跌幅→绿）"
```

---

### Task 4: 净值曲线新增「超额收益（相对基准）」曲线

**Files:**
- Modify: `web/src/views/Backtest.vue`
  - 新增 `excessCurve` computed：建议放在 `portfolioReturnPct` 之后（约 L298 后）。
  - `initEquityChart`（约 L411-422）：追加 series + `legendData` pit élément。
- Test: `web/src/__tests__/Backtest.test.ts`（末尾新增 describe）

**Interfaces:**
- Consumes:
  - `hasBenchmark` computed（L257，基准非全 null 时为 true）。
  - `portfolioReturnPct` computed（L294，组合累计收益率%，base=首日 total_value）。
  - `benchmarkCurve` computed（L258，基准归一化%，base=首个非空 benchmark_value，null 处为 null）。
- Produces:
  - `excessCurve: ComputedRef<Array<number | null>>`：逐点 `portfolioReturnPct[i] − benchmarkCurve[i]`，基准为 null/undefined 处结果 `null`，其余 `Number((pv - bv).toFixed(4))`。
  - `initEquityChart` 新增 series `name: '超额收益（相对基准）'`、`type: 'line'`、`smooth: true`、`symbol: 'none'`、金色 `lineStyle: { width: 1.5, color: '#f59e0b' }`、`itemStyle: { color: '#f59e0b' }`，仅在 `hasBenchmark.value` 时 push。
  - `legendData`：在 `'基准'` 之后追加 `'超额收益（相对基准）'`（同样仅当 `hasBenchmark`）。

- [ ] **Step 1: 写失败测试**

在测试文件末尾追加：

```ts
// --- 回测报告 — 净值曲线超额收益 ---
describe('Backtest.vue — 净值曲线超额收益（相对基准）', () => {
  it('有基准数据时净值曲线含超额收益线（组合%−基准%）', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const option = (init as any).mock.results[0].value.setOption.mock.calls[0][0]
    expect(option.legend.data).toContain('超额收益（相对基准）')
    const excess = option.series.find((s: any) => s.name === '超额收益（相对基准）')
    expect(excess).toBeTruthy()
    expect(excess.type).toBe('line')
    expect(excess.itemStyle.color).toBe('#f59e0b')
    // 手算：组合收益% [0,1,-1] − 基准% [0,2,1] = [0,-1,-2]
    expect(excess.data).toEqual([0, -1, -2])
  })

  it('无基准数据（benchmark_value 全 null）时不画超额收益线', async () => {
    ;(getBacktestDetail as any).mockResolvedValue({
      ...mockDetail,
      snapshots: mockDetail.snapshots.map((s: any) => ({ ...s, benchmark_value: null })),
    })
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const option = (init as any).mock.results[0].value.setOption.mock.calls[0][0]
    expect(option.legend.data).not.toContain('超额收益（相对基准）')
    expect(option.series.find((s: any) => s.name === '超额收益（相对基准）')).toBeUndefined()
  })
})
```

- [ ] **Step 2: 跑测试验证失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: 第一用例失败于 `expect(option.legend.data).toContain('超额收益（相对基准）')`；第二用例可通过（当前本来就没有该曲线）。

- [ ] **Step 3: 实现最小改动**

新增 `excessCurve`（`portfolioReturnPct` 之后，此时约 L298-299 之间）：

```ts
// 超额收益 = 组合累计收益率% − 基准累计收益率%（逐点）；基准 null 处超额也为 null
const excessCurve = computed<Array<number | null>>(() => {
  const p = portfolioReturnPct.value
  const b = benchmarkCurve.value
  return p.map((pv, i) => {
    const bv = b[i]
    if (bv === null || bv === undefined) return null
    return Number((pv - bv).toFixed(4))
  })
})
```

`initEquityChart`（此时约 L411-422）：基准 series push 之后追加：

```ts
  // 超额收益曲线（金色，题别组合紫/策略自动色/基准灰虚线）；无基准数据则不画
  if (hasBenchmark.value) {
    series.push({
      name: '超额收益（相对基准）', type: 'line', data: excessCurve.value, smooth: true,
      symbol: 'none',
      lineStyle: { width: 1.5, color: '#f59e0b' },
      itemStyle: { color: '#f59e0b' },
    })
  }
```

`legendData`（此时约 L421-422）：

```ts
  const legendData = ['组合', ...strategyCurves.value.map((s) => s.name)]
  if (hasBenchmark.value) {
    legendData.push('基准')
    legendData.push('超额收益（相对基准）')
  }
```

- [ ] **Step 4: 跑测试验证通过**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: 全部通过（28 个：26 + 超额收益 2 个用例）。既有「净值曲线含基准线」用例（L294）仍绿（断言 `legend.data` 含「基准」不受影响）。

- [ ] **Step 5: 全量回归 + 类型检查 + 构建**

Run: `npx vitest run && npx vue-tsc --noEmit && npm run build`
Expected: 11 files, 126 tests passed；vue-tsc 无输出；`built in` 成功。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 净值曲线新增超额收益（相对基准）曲线"
```

---

### Task 5: 最终验证

**Files:**
- 不发生代码改动；仅验证。

- [ ] **Step 1: 全量后端 + 前端测试**

后端起测试（确认后端无改动、仍绿）：
Run: `uv run pytest core/tests/unit/`
Expected: 全部通过。

前端：
Run: `npx vitest run`
Expected: 11 files, 126 tests passed。

- [ ] **Step 2: 类型检查 + 构建**

Run: `npx vue-tsc --noEmit && npm run build`
Expected: 无输出 + built in 成功。

- [ ] **Step 3: 确认提交历史**

Run: `git log --oneline -6`
Expected: 依次可见 4 个本计划提交：Task 1 基线、Task 2 策略对比/摘要卡、Task 3 回撤绿、Task 4 超额收益。

- [ ] **Step 4: 确认工作区干净**

Run: `git status --short`
Expected: 无输出（clean）。