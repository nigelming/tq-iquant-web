# 前端接线 6 项模型缺陷修�?实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把后端已落地但前端零接入�?6 项「针对模型不足」能力全部接线，使用户可�?UI 使用�?
**Architecture:** 新增独立�?`BacktestAnalysis.vue`（路�?`/backtest/analysis`，两 tab）承�?walk-forward 与参数微扰；其余改动落在 `Backtest.vue` / `Portfolios.vue` / `Formulas.vue` 现有表单与详情；`api/index.ts` 补齐类型与函数。不改后端�?
**Tech Stack:** Vue 3 + Vite + TypeScript + Pinia + vue-router + vitest（前端）；后�?FastAPI/SQLAlchemy（仅对接，不改）�?
## Global Constraints

- 后端契约�?`docs/superpowers/specs/2026-10-13-frontend-wire-model-fixes-design.md`，字段名**逐字**匹配，不可改名�?- 统一响应 `{code,message,data}`；axios 拦截器已 reject `code!==0`，视图层沿用现有 `errMsg(e)`�?- 百分比字段后台存小数、UI 显示百分数（Portfolios �?`toPercent`/`fromPercent`；Backtest 表单口径比例 `oos_gate_min_ratio` 直接填小�?0.5，不走百分比转换）�?- 不新增第三方依赖；不�?mock 服务；测试用 vitest，跑�?`npx vitest`（在 `web/`）�?- 类型检�?`npx vue-tsc -b`（在 `web/`）；构建 `npm run build`（在 `web/`）�?- 中文文案，风格与现有页面一致（`section-title`、`btn btn-primary`、`badge`、`field-row` 等既�?class）�?
---

### Task 1: api 层补类型与函�?
**Files:**
- Modify: `web/src/api/index.ts`
- Test: `web/src/__tests__/api.test.ts`（追加用例到既有文件�?
**Interfaces:**
- Produces（供后续任务消费）：
  - `BacktestRecordItem` �?`evaluation_profile: string; is_oos_gate_enabled: boolean; oos_gate_min_ratio: number | null; gate_status: 'passed'|'rejected'|null; gate_reasons: string[]`
  - `AcceptanceCheck { metric: string; op: string; threshold: number; value: number|null; pass: boolean|null; label: string }`
  - `AcceptanceResult { profile: string; pass: boolean|null; checks: AcceptanceCheck[] }`
  - `BacktestDetailItem` �?`acceptance: AcceptanceResult`
  - `BacktestRequest` �?`evaluation_profile?: 'absolute'|'relative'; is_oos_gate_enabled?: boolean; oos_gate_min_ratio?: number`
  - `BacktestRunResult { record_id: number; warnings: string[]; gate: { status: 'passed'|'rejected'|null; reasons: string[]; min_ratio: number } | null }`
  - `getBacktestDetail(id: number, profile?: 'absolute'|'relative')`
  - `runBacktest(req): Promise<BacktestRunResult>`
  - walk-forward：`WalkForwardRequest`、`WalkForwardWindow`、`WalkForwardSummary`、`WalkForwardRunItem`、`runWalkForward`、`getWalkForwardRuns`、`getWalkForwardRun`
  - param-perturb：`ParamPerturbRequest`、`PerturbRound`、`PerturbParamBlock`、`ParamPerturbSummary`、`ParamPerturbRunItem`、`runParamPerturb`、`getParamPerturbRuns`、`getParamPerturbRun`
  - `FormulaRequest` �?`strict_future_check?: boolean`
  - `PortfolioItem`/`PortfolioRequest` �?`price_limit_check_enabled: boolean; volume_limit_enabled: boolean; volume_limit_ratio: number`
  - `StrategyItem`/`StrategyRequest`/`StrategyDetail` �?`rotation_enabled; rotation_momentum_bars; rotation_hysteresis; regime_filter_enabled; trend_trailing_stop_ratio; trend_max_positions; exposure_cap_enabled`

- [ ] **Step 1: 写失败测�?*

新建 `web/src/__tests__/api.test.ts`�?
```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, runWalkForward, getWalkForwardRun, runParamPerturb, getParamPerturbRun } from '../api'

vi.mock('axios', async () => {
  const actual = await vi.importActual<any>('axios')
  return { ...actual, default: { create: () => api } }
})

describe('walk-forward / param-perturb api', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('runWalkForward POST /backtest/walk-forward 并透传 payload', async () => {
    const spy = vi.spyOn(api, 'post').mockResolvedValue({ data: { code: 0, message: 'ok', data: { run_id: 1, status: 'completed', windows_passed: '2/2', overall_pass: true, error: null } } } as any)
    const req = { portfolio_strategy_id: 3, name: 'wf', start_date: '2020-01-01', end_date: '2024-01-01', train_years: 3, test_years: 1, oos_gate_min_ratio: 0.5, dividend_mode: 'front' as const, evaluation_profile: 'absolute' as const }
    const r = await runWalkForward(req)
    expect(spy).toHaveBeenCalledWith('/backtest/walk-forward', req)
    expect(r.overall_pass).toBe(true)
  })
  it('getWalkForwardRun GET /backtest/walk-forward/{id}', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValue({ data: { code: 0, message: 'ok', data: { id: 7 } } } as any)
    await getWalkForwardRun(7)
    expect(spy).toHaveBeenCalledWith('/backtest/walk-forward/7')
  })
  it('runParamPerturb POST /backtest/param-perturb', async () => {
    const spy = vi.spyOn(api, 'post').mockResolvedValue({ data: { code: 0, message: 'ok', data: { run_id: 2, status: 'completed', rounds_run: '4/4', most_sensitive: 'stop_loss_ratio', error: null } } } as any)
    const req = { portfolio_strategy_id: 3, name: 'pp', start_date: '2020-01-01', end_date: '2024-01-01', perturb_pct: 0.1, dividend_mode: 'front' as const, evaluation_profile: 'absolute' as const }
    const r = await runParamPerturb(req)
    expect(spy).toHaveBeenCalledWith('/backtest/param-perturb', req)
    expect(r.most_sensitive).toBe('stop_loss_ratio')
  })
  it('getParamPerturbRun GET /backtest/param-perturb/{id}', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValue({ data: { code: 0, message: 'ok', data: { id: 9 } } } as any)
    await getParamPerturbRun(9)
    expect(spy).toHaveBeenCalledWith('/backtest/param-perturb/9')
  })
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/api.test.ts`（workdir `web/`�?Expected: FAIL（`runWalkForward` 等未定义 / 未导出）

- [ ] **Step 3: 实现类型与函�?*

�?`web/src/api/index.ts` 中：

(a) `BacktestRecordItem`（当�?L96-107）接口体�?`completed_at` 之前追加�?
```ts
  evaluation_profile: string  // absolute|relative（历史记录回�?absolute�?  is_oos_gate_enabled: boolean
  oos_gate_min_ratio: number | null
  gate_status: 'passed' | 'rejected' | null  // null=未启用或数据不足
  gate_reasons: string[]
```

(b) �?`BacktestDetailItem`（L421）定�?*之前**新增�?
```ts
export interface AcceptanceCheck {
  metric: string
  op: string
  threshold: number
  value: number | null
  pass: boolean | null
  label: string
}
export interface AcceptanceResult {
  profile: string
  pass: boolean | null
  checks: AcceptanceCheck[]
}
```

并在 `BacktestDetailItem` 接口体内 `robustness` 之后加：`acceptance: AcceptanceResult`�?
(c) 替换 `BacktestRequest`（L705-711）与 `runBacktest`（L713-716）为�?
```ts
export interface BacktestRequest {
  portfolio_strategy_id: number
  name: string
  start_date: string  // YYYY-MM-DD
  end_date: string    // YYYY-MM-DD
  dividend_mode?: 'front' | 'back' | 'dual'
  evaluation_profile?: 'absolute' | 'relative'  // B6 考核口径（默�?absolute�?  is_oos_gate_enabled?: boolean                 // C7 IS/OOS 采纳门槛（默认关�?  oos_gate_min_ratio?: number                   // (0,2]，默�?0.5
}
export interface BacktestGateResult {
  status: 'passed' | 'rejected' | null
  reasons: string[]
  min_ratio: number
}
export interface BacktestRunResult {
  record_id: number
  warnings: string[]
  gate: BacktestGateResult | null
}
export async function runBacktest(req: BacktestRequest) {
  const res = await api.post<ApiResponse<BacktestRunResult>>('/backtest', req)
  return res.data.data
}
export async function getBacktestDetail(id: number, profile?: 'absolute' | 'relative') {
  const res = await api.get<ApiResponse<BacktestDetailItem>>(`/backtest/records/${id}`, {
    params: profile ? { profile } : undefined,
  })
  return res.data.data
}
```

(d) �?`deleteBacktestRecord`（L723-726）之后追�?WF/PP 整块�?
```ts
// ===== C8 walk-forward 滚动验证 =====
export interface WalkForwardRequest {
  portfolio_strategy_id: number
  name: string
  start_date: string
  end_date: string
  train_years: number
  test_years: number
  oos_gate_min_ratio: number
  dividend_mode: 'front' | 'back' | 'dual'
  evaluation_profile: 'absolute' | 'relative'
}
export interface WalkForwardWindow {
  index: number
  train: { start: string | null; end: string | null }
  test: { start: string | null; end: string | null }
  record_id: number | null
  is_cagr: number | null
  oos_cagr: number | null
  oos_return: number | null
  oos_benchmark_return: number | null
  gate: { status: 'passed' | 'rejected' | null; reasons: string[] }
  error?: string
}
export interface WalkForwardSummary {
  windows: WalkForwardWindow[]
  windows_passed: string  // "k/n"
  overall_pass: boolean
}
export interface WalkForwardRunItem {
  id: number
  portfolio_strategy_id: number
  name: string
  start_date: string | null
  end_date: string | null
  train_years: number
  test_years: number
  oos_gate_min_ratio: number | null
  status: string
  error_message: string | null
  summary: WalkForwardSummary | null
  created_at: string | null
  completed_at: string | null
}
export async function runWalkForward(req: WalkForwardRequest) {
  const res = await api.post<ApiResponse<{ run_id: number; status: string; windows_passed: string; overall_pass: boolean; error: string | null }>>(
    '/backtest/walk-forward', req,
  )
  return res.data.data
}
export async function getWalkForwardRuns() {
  const res = await api.get<ApiResponse<WalkForwardRunItem[]>>('/backtest/walk-forward')
  return res.data.data
}
export async function getWalkForwardRun(id: number) {
  const res = await api.get<ApiResponse<WalkForwardRunItem>>(`/backtest/walk-forward/${id}`)
  return res.data.data
}

// ===== P2b 参数微扰敏感性分�?=====
export interface ParamPerturbRequest {
  portfolio_strategy_id: number
  name: string
  start_date: string
  end_date: string
  perturb_pct: number  // (0,0.5]
  dividend_mode: 'front' | 'back' | 'dual'
  evaluation_profile: 'absolute' | 'relative'
}
export interface PerturbRoundMetrics {
  record_id: number | null
  status: string | null
  annual_return: number | null
  total_return: number | null
  max_drawdown: number | null
  win_rate: number | null
  profit_factor: number | null
  sharpe_ratio: number | null
  total_trades: number | null
  error: string | null
}
export interface PerturbRound extends PerturbRoundMetrics {
  param: string
  direction: 'low' | 'high'
  factor: number
}
export interface PerturbParamBlock {
  param: string
  low: PerturbRound | null
  high: PerturbRound | null
  delta_cagr: number | null  // 实为年化收益差值（high−low�?}
export interface ParamPerturbSummary {
  baseline: PerturbRoundMetrics
  rounds: PerturbRound[]
  params: PerturbParamBlock[]
  most_sensitive: string | null
}
export interface ParamPerturbRunItem {
  id: number
  portfolio_strategy_id: number
  name: string
  start_date: string | null
  end_date: string | null
  perturb_pct: number | null
  status: string
  error_message: string | null
  summary: ParamPerturbSummary | null
  created_at: string | null
  completed_at: string | null
}
export async function runParamPerturb(req: ParamPerturbRequest) {
  const res = await api.post<ApiResponse<{ run_id: number; status: string; rounds_run: string; most_sensitive: string | null; error: string | null }>>(
    '/backtest/param-perturb', req,
  )
  return res.data.data
}
export async function getParamPerturbRuns() {
  const res = await api.get<ApiResponse<ParamPerturbRunItem[]>>('/backtest/param-perturb')
  return res.data.data
}
export async function getParamPerturbRun(id: number) {
  const res = await api.get<ApiResponse<ParamPerturbRunItem>>(`/backtest/param-perturb/${id}`)
  return res.data.data
}
```

(e) `FormulaRequest`（L509-514）加 `strict_future_check?: boolean`�?
(f) `PortfolioItem`（L74-93）在 `trading_session` 后加�?
```ts
  price_limit_check_enabled: boolean
  volume_limit_enabled: boolean
  volume_limit_ratio: number
```

`PortfolioRequest`（L599-615）在 `trading_session` 后加�?
```ts
  price_limit_check_enabled?: boolean
  volume_limit_enabled?: boolean
  volume_limit_ratio?: number
```

(g) `StrategyItem`（L579-597）、`StrategyRequest`（L659-677）、`StrategyDetail`（L637-657）三个接口均�?`reduce_position_ratio: number` 后追加：

```ts
  rotation_enabled: boolean
  rotation_momentum_bars: number
  rotation_hysteresis: number
  regime_filter_enabled: boolean
  trend_trailing_stop_ratio: number | null
  trend_max_positions: number | null
  exposure_cap_enabled: boolean
```

（`StrategyRequest` 中这些字段可选：`rotation_enabled?: boolean` 等；`StrategyDetail`/`StrategyItem` 为必填。）

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/api.test.ts`
Expected: PASS

- [ ] **Step 5: 类型检�?*

Run: `npx vue-tsc -b`
Expected: 无错误（若视图层因新必填字段报错，属于后续任务，可先跳过；本步允�?API 自身无错�?
- [ ] **Step 6: Commit**

```bash
git add web/src/api/index.ts web/src/__tests__/api.test.ts
git commit -m "feat(web): api 层补 OOS�?评估口径/walk-forward/参数微扰/执行真实�?未来函数 类型与函�?
```

---

### Task 2: 路由 + 分析页骨�?+ 回测页入�?
**Files:**
- Modify: `web/src/router/index.ts:11`
- Create: `web/src/views/BacktestAnalysis.vue`
- Modify: `web/src/views/Backtest.vue`（顶部按钮区，L2628 附近�?- Test: `web/src/__tests__/BacktestAnalysis.test.ts`（新建）

**Interfaces:**
- Consumes: Task 1 �?`getWalkForwardRuns` 等�?- Produces: 组件�?`activeTab: 'wf' | 'pp'`�?
- [ ] **Step 1: 写失败测�?*

`web/src/__tests__/BacktestAnalysis.test.ts`�?
```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BacktestAnalysis from '../views/BacktestAnalysis.vue'

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getWalkForwardRuns: vi.fn().mockResolvedValue([]),
  getWalkForwardRun: vi.fn(),
  runWalkForward: vi.fn(),
  getParamPerturbRuns: vi.fn().mockResolvedValue([]),
  getParamPerturbRun: vi.fn(),
  runParamPerturb: vi.fn(),
}))

describe('BacktestAnalysis', () => {
  beforeEach(() => vi.clearAllMocks())
  it('默认渲染滚动前推 tab', async () => {
    const w = mount(BacktestAnalysis)
    await flushPromises()
    expect(w.text()).toContain('滚动前推')
    expect(w.text()).toContain('参数敏感�?)
  })
  it('切到参数敏感�?tab', async () => {
    const w = mount(BacktestAnalysis)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('参数敏感�?))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('扰动幅度')
  })
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/BacktestAnalysis.test.ts`
Expected: FAIL（组件不存在�?
- [ ] **Step 3: 建路由与骨架**

`web/src/router/index.ts` �?backtest 行后加：

```ts
    { path: '/backtest/analysis', name: 'backtest-analysis', component: () => import('../views/BacktestAnalysis.vue') },
```

`web/src/views/BacktestAnalysis.vue` 骨架（tab 与两子组件占位，子组件在 Task 3/4 落地时改为真实内容；本任务先内联占位以满足测试文案）�?
```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getPortfolios, getWalkForwardRuns, getParamPerturbRuns, type PortfolioItem, type WalkForwardRunItem, type ParamPerturbRunItem } from '../api'

const activeTab = ref<'wf' | 'pp'>('wf')
const portfolios = ref<PortfolioItem[]>([])
const wfRuns = ref<WalkForwardRunItem[]>([])
const ppRuns = ref<ParamPerturbRunItem[]>([])

onMounted(async () => {
  portfolios.value = await getPortfolios().catch(() => [])
  wfRuns.value = await getWalkForwardRuns().catch(() => [])
  ppRuns.value = await getParamPerturbRuns().catch(() => [])
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>回测回归验证</h2>
      <div class="tab-bar">
        <button class="btn" :class="{ 'btn-primary': activeTab === 'wf' }" @click="activeTab = 'wf'">滚动前推</button>
        <button class="btn" :class="{ 'btn-primary': activeTab === 'pp' }" @click="activeTab = 'pp'">参数敏感�?/button>
      </div>
    </div>
    <div v-show="activeTab === 'wf'"><p>滚动前推（walk-forward�?/p></div>
    <div v-show="activeTab === 'pp'"><p>参数敏感度（扰动幅度可调�?/p></div>
  </div>
</template>
```

`web/src/views/Backtest.vue` 顶部按钮区（L2628 `+ 发起回测` 按钮旁）加：

```vue
      <router-link to="/backtest/analysis" class="btn" style="margin-left:8px">回归验证（滚动前�?/ 参数敏感度）</router-link>
```

（`Backtest.vue` 需已能使用 `<router-link>`；vue-router 全局注册，无需 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/BacktestAnalysis.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/router/index.ts web/src/views/BacktestAnalysis.vue web/src/views/Backtest.vue web/src/__tests__/BacktestAnalysis.test.ts
git commit -m "feat(web): 新增回归验证页路由与骨架，回测页加入�?
```

---

### Task 3: 滚动前推 tab（发�?列表/窗口详情�?
**Files:**
- Create: `web/src/views/WalkForwardPanel.vue`
- Modify: `web/src/views/BacktestAnalysis.vue`（tab1 替换为组�?+ 回调刷新�?- Test: `web/src/__tests__/WalkForwardPanel.test.ts`（新建）

**Interfaces:**
- Consumes: `getPortfolios`、`getWalkForwardRuns`、`getWalkForwardRun`、`runWalkForward`、类�?`WalkForwardRequest/WalkForwardRunItem/WalkForwardWindow`、`PortfolioItem`�?- Produces: emits `done`（发起成功后）�?
- [ ] **Step 1: 写失败测�?*

`web/src/__tests__/WalkForwardPanel.test.ts`�?
```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import WalkForwardPanel from '../views/WalkForwardPanel.vue'

const runs = [{
  id: 1, portfolio_strategy_id: 1, name: 'WF-A', start_date: '2020-01-01', end_date: '2024-01-01',
  train_years: 3, test_years: 1, oos_gate_min_ratio: 0.5, status: 'completed', error_message: null,
  summary: { windows: [{ index: 1, train: { start: '2020-01-01', end: '2023-01-01' }, test: { start: '2023-01-01', end: '2024-01-01' }, record_id: 10, is_cagr: 0.12, oos_cagr: 0.08, oos_return: 0.09, oos_benchmark_return: 0.03, gate: { status: 'passed', reasons: [] } }], windows_passed: '1/1', overall_pass: true },
  created_at: '2026-01-01T00:00:00', completed_at: null,
}]

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getWalkForwardRuns: vi.fn().mockResolvedValue(runs),
  getWalkForwardRun: vi.fn().mockResolvedValue(runs[0]),
  runWalkForward: vi.fn(),
}))

describe('WalkForwardPanel', () => {
  beforeEach(() => vi.clearAllMocks())
  it('渲染运行列表与通过徽章', async () => {
    const w = mount(WalkForwardPanel)
    await flushPromises()
    expect(w.text()).toContain('WF-A')
    expect(w.text()).toContain('1/1')
  })
  it('点击查看展开窗口�?, async () => {
    const w = mount(WalkForwardPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('IS年化')
  })
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/WalkForwardPanel.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现 WalkForwardPanel.vue**

要求（完整实现要点，按现�?`Portfolios.vue`/`Backtest.vue` �?class 风格）：
- `<script setup lang="ts">`：props `portfolios: PortfolioItem[]`；state：`runs`、`form`（默�?`{ portfolio_strategy_id: portfolios[0]?.id ?? 0, name: '', start_date: '', end_date: '', train_years: 3, test_years: 1, oos_gate_min_ratio: 0.5, dividend_mode: 'front', evaluation_profile: 'absolute' }`）、`showForm`、`detail`（`WalkForwardRunItem | null`）、`errMsg`�?- `load()`：`runs.value = await getWalkForwardRuns()`�?- `submit()`：客户端校验（组�?名称/日期/`start_date <= end_date`；提示区间不足需 `train+test` 年）；`await runWalkForward({...form})` �?`showForm=false; load(); emit('done')`；错�?`alert(errMsg(e))`。若返回 `status==='failed'`，`alert('区间不足�? + error)`�?- `view(run)`：`detail.value = await getWalkForwardRun(run.id)`�?- 模板�?  - 顶部 `+ 发起滚动前推` 按钮与发�?Modal（字段同 form；`train_years`/`test_years` number，`oos_gate_min_ratio` number step 0.1，复�?select front/back/dual，口�?select absolute/relative）�?  - 运行列表表格列：ID / 名称 / 组合 / 区间 / 训练+验证 / 通过 / 状�?/ 操作(查看、删除仅前端列表刷新不做)�?    - 「通过」列：`summary ? badge(summary.overall_pass ? '全窗通过':'未全通过', �?�? + ' ' + summary.windows_passed : '�?`�?  - 详情区（`detail` 非空时）：overall_pass 徽章 + `error_message`（若有）+ 窗口表，列：窗口 / 训练区间 / 验证区间 / IS年化 / OOS年化 / OOS收益 / OOS基准 / 门槛 / record_id（可点击�?`/backtest?record=<id>`，若 Backtest.vue 不支持深链则纯文本）�?    - 数值走 `utils/format.ts` �?`pct`/`pct1`（若存在），否则 `((v*100).toFixed(2)+'%')`，null 显示 `—`�?    - `gate.status==='passed'` 绿徽章「过」，`'rejected'` 红徽章「未过」，`null` 灰「未评估」；`gate.reasons` �?`title` 悬浮或单元格小字展示�?    - 单窗 `error` 有值时该行标红并在操作列显示错�?icon+title�?
�?`BacktestAnalysis.vue` �?tab1 占位替换为：

```vue
    <div v-show="activeTab === 'wf'"><WalkForwardPanel :portfolios="portfolios" @done="reloadWf" /></div>
```

�?import 组件、新�?`async function reloadWf(){ wfRuns.value = await getWalkForwardRuns().catch(()=>[]) }`�?
- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/WalkForwardPanel.test.ts src/__tests__/BacktestAnalysis.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/WalkForwardPanel.vue web/src/views/BacktestAnalysis.vue web/src/__tests__/WalkForwardPanel.test.ts
git commit -m "feat(web): 滚动前推面板（发�?列表/窗口详情�?
```

---

### Task 4: 参数敏感�?tab（发�?列表/baseline+params 详情�?
**Files:**
- Create: `web/src/views/ParamPerturbPanel.vue`
- Modify: `web/src/views/BacktestAnalysis.vue`（tab2 替换为组件）
- Test: `web/src/__tests__/ParamPerturbPanel.test.ts`（新建）

**Interfaces:**
- Consumes: `getPortfolios`、`getParamPerturbRuns`、`getParamPerturbRun`、`runParamPerturb`、类�?`ParamPerturbRunItem/PerturbParamBlock`�?
- [ ] **Step 1: 写失败测�?*

`web/src/__tests__/ParamPerturbPanel.test.ts`�?
```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ParamPerturbPanel from '../views/ParamPerturbPanel.vue'

const run = {
  id: 2, portfolio_strategy_id: 1, name: 'PP-A', start_date: '2020-01-01', end_date: '2024-01-01',
  perturb_pct: 0.1, status: 'completed', error_message: null, created_at: 'x', completed_at: null,
  summary: {
    baseline: { record_id: 20, status: 'completed', annual_return: 0.11, total_return: 0.5, max_drawdown: 0.12, win_rate: 0.55, profit_factor: 1.6, sharpe_ratio: 1.2, total_trades: 40, error: null },
    rounds: [],
    params: [{ param: 'stop_loss_ratio', low: null, high: null, delta_cagr: 0.03 }],
    most_sensitive: 'stop_loss_ratio',
  },
}

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getParamPerturbRuns: vi.fn().mockResolvedValue([run]),
  getParamPerturbRun: vi.fn().mockResolvedValue(run),
  runParamPerturb: vi.fn(),
}))

describe('ParamPerturbPanel', () => {
  beforeEach(() => vi.clearAllMocks())
  it('渲染列表与最敏感参数', async () => {
    const w = mount(ParamPerturbPanel)
    await flushPromises()
    expect(w.text()).toContain('PP-A')
  })
  it('查看详情渲染 baseline 与参数块', async () => {
    const w = mount(ParamPerturbPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('基准')
    expect(w.text()).toContain('stop_loss_ratio')
  })
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/ParamPerturbPanel.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现 ParamPerturbPanel.vue**

要点�?- form 默认 `{ portfolio_strategy_id, name:'', start_date:'', end_date:'', perturb_pct: 0.1, dividend_mode:'front', evaluation_profile:'absolute' }`�?- `submit()`：校验组�?名称/日期/`0 < perturb_pct <= 0.5`；`await runParamPerturb(...)` �?`showForm=false; load(); emit('done')`；错�?alert�?- 列表列：ID / 名称 / 组合 / 区间 / 扰动幅度 / 最敏感参数 / 状�?/ 操作(查看)�?- 详情：baseline 卡片（年�?总收�?最大回�?胜率/盈亏�?夏普/交易�?状态）；params 表列：参�?/ 低方向年�?/ 高方向年�?/ 差�?年化, high−low) / 最敏感标记（`param===summary.most_sensitive` �?`☆` 或徽章）。`low`/`high` �?null 时显示「不可扰动」�?*差值列标题写「年化收益差�?*（勿�?CAGR）�?- �?`rounds` 中有 `error`，在 params 表下方列错误轮次�?
`BacktestAnalysis.vue` tab2 替换�?
```vue
    <div v-show="activeTab === 'pp'"><ParamPerturbPanel :portfolios="portfolios" @done="reloadPp" /></div>
```

新增 `reloadPp` �?`reloadWf` 模式�?
- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/ParamPerturbPanel.test.ts src/__tests__/BacktestAnalysis.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/ParamPerturbPanel.vue web/src/views/BacktestAnalysis.vue web/src/__tests__/ParamPerturbPanel.test.ts
git commit -m "feat(web): 参数敏感度面板（发起/列表/baseline+参数详情�?
```

---

### Task 5: 回测发起表单加评估口�?+ OOS �?
**Files:**
- Modify: `web/src/views/Backtest.vue`（form ref L103；submit L906-921；模�?L2656-2690�?- Test: `web/src/__tests__/Backtest.test.ts`（追加用例）

**Interfaces:**
- Consumes: `BacktestRequest` 新字段、`BacktestRunResult.gate`�?
- [ ] **Step 1: 写失败测�?*（追加到现有 `web/src/__tests__/Backtest.test.ts` 末尾；如文件无则新建，参照其既有 mock 风格�?
```ts
// 发起回测携带口径�?OOS 门字�?it('发起回测请求包含 evaluation_profile �?OOS 门字�?, async () => {
  // 复用该文件既�?mount �?mock（runBacktest mock 断言�?  // 断言 runBacktest 收到 { ..., evaluation_profile: 'relative', is_oos_gate_enabled: true, oos_gate_min_ratio: 0.5 }
})
```

（实现时以该文件现有 mock 断言写法为准；核心断言：`runBacktest` 被调用且入参含三个新字段。）

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: FAIL（新字段未传�?
- [ ] **Step 3: 实现**

form ref（L103）改为：

```ts
const form = ref({ portfolio_strategy_id: 0, name: '', start_date: '', end_date: '', dividend_mode: 'front', evaluation_profile: 'absolute', is_oos_gate_enabled: false, oos_gate_min_ratio: 0.5 })
```

`openForm`（约 L895）重�?form 时补 `evaluation_profile: 'absolute', is_oos_gate_enabled: false, oos_gate_min_ratio: 0.5`�?
`submit`（L916-921）payload 增：

```ts
      evaluation_profile: form.value.evaluation_profile as 'absolute' | 'relative',
      is_oos_gate_enabled: form.value.is_oos_gate_enabled,
      oos_gate_min_ratio: Number(form.value.oos_gate_min_ratio) || 0.5,
```

提交校验加：�?`is_oos_gate_enabled` �?`!(oos_gate_min_ratio > 0 && oos_gate_min_ratio <= 2)` �?`alert('IS/OOS 门槛比例须在 (0, 2]')`�?
提交成功后处�?`runBacktest` 返回值：新增 `gateBanner` ref（`{ status, reasons, min_ratio } | null`）；成功分支 `gateBanner.value = res.gate`；若 `res.gate?.status === 'rejected'` 在记录区顶部显示红色提示�?
模板发起 Modal（L2656-2690，复�?select 附近）加�?
```vue
        <div class="field-row"><span class="field-label">评估口径</span>
          <select v-model="form.evaluation_profile" data-field="evaluation_profile">
            <option value="absolute">绝对收益（年�?回撤/卡玛�?/option>
            <option value="relative">相对收益（信息比�?超额/下行捕获�?/option>
          </select>
        </div>
        <div class="field-row"><span class="field-label">IS/OOS 采纳门槛</span>
          <label style="display:flex;align-items:center;gap:6px">
            <input type="checkbox" v-model="form.is_oos_gate_enabled" data-field="is_oos_gate_enabled" />
            启用（OOS 年化 �?比例 × IS 年化，且 OOS 不跑输基准）
          </label>
        </div>
        <div v-if="form.is_oos_gate_enabled" class="field-row"><span class="field-label">门槛比例</span>
          <input type="number" step="0.05" min="0.05" max="2" v-model.number="form.oos_gate_min_ratio" data-field="oos_gate_min_ratio" />
        </div>
```

记录区顶部加 gate banner�?
```vue
      <div v-if="gateBanner" class="card" :style="{ padding: '12px', marginBottom: '12px', color: gateBanner.status === 'rejected' ? 'var(--red)' : 'var(--green)' }">
        IS/OOS 门槛：{{ gateBanner.status === 'passed' ? '通过' : gateBanner.status === 'rejected' ? '未通过' : '数据不足未评�? }}
        <span v-if="gateBanner.reasons.length">（{{ gateBanner.reasons.join('�?) }}�?/span>
      </div>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 回测发起表单加评估口径与 IS/OOS 采纳门槛"
```

---

### Task 6: 回测列表门状态列 + 详情采纳验收�?
**Files:**
- Modify: `web/src/views/Backtest.vue`（记录表�?�?L2638 附近；详�?section 区；detail 加载�?- Test: `web/src/__tests__/Backtest.test.ts`（追加）

**Interfaces:**
- Consumes: `BacktestRecordItem.gate_status`、`BacktestDetailItem.acceptance`、`getBacktestDetail(id, profile?)`�?- Produces: `acceptanceProfile` ref（切换口径重拉详情）�?
- [ ] **Step 1: 写失败测�?*（追加）

```ts
// 记录列表显示门状态徽�?& 详情显示采纳验收
it('记录列表渲染门状态、详情渲染采纳验收并可切换口�?, async () => {
  // mock getBacktestRecords 返回一�?gate_status: 'rejected'
  // mock getBacktestDetail 返回 acceptance { profile:'absolute', pass:false, checks:[...] }
  // 断言文本包含 '未达�? �?'采纳验收'
  // 触发口径切换�?relative 后断言 getBacktestDetail �?('absolute' 之外) �?'relative' 再次调用
})
```

（以该文件现�?mock 与断言风格实现。）

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现**

(a) 记录表头（L2638）由 `...<th>状�?/th><th>操作</th>` 改为增加 `<th>门状�?/th>`（置于状态之后）；行内加�?
```vue
        <td>
          <span v-if="r.gate_status === 'passed'" class="badge badge-green">达门�?/span>
          <span v-else-if="r.gate_status === 'rejected'" class="badge badge-red" :title="(r.gate_reasons || []).join('�?)">未达�?/span>
          <span v-else class="badge badge-gray" :title="r.is_oos_gate_enabled ? '数据不足' : '未启�?">{{ r.is_oos_gate_enabled ? '未评�? : '未启�? }}</span>
        </td>
```

（若 `badge-red` 不存在，�?`badge-danger` 或现有红色徽�?class；以文件现有 class 为准。）

(b) 详情�?
新增 `const acceptanceProfile = ref<'absolute' | 'relative'>('absolute')` �?`const acceptance = computed(() => detail.value?.acceptance ?? null)`�?
`selectRecord`/详情加载处（L934、L1378、L1408 等调 `getBacktestDetail(id)` 的位置）改为读取 `acceptanceProfile.value`，且选中记录时把 `acceptanceProfile.value = detail.value?.record.evaluation_profile === 'relative' ? 'relative' : 'absolute'`。新增切换函数：

```ts
async function switchAcceptanceProfile(p: 'absolute' | 'relative') {
  if (!currentRecord.value) return
  acceptanceProfile.value = p
  detail.value = await getBacktestDetail(currentRecord.value.id, p)
}
```

详情新增折叠 section（放在「关键指标摘要」L2734 附近，风格照抄既�?`section-title` 折叠块）�?
```vue
    <div class="section-title" @click="showAcceptance = !showAcceptance">
      �?采纳验收（{{ acceptanceProfile === 'absolute' ? '绝对收益' : '相对收益' }}�?      <span v-if="acceptance" class="badge" :class="acceptance.pass === true ? 'badge-green' : acceptance.pass === false ? 'badge-red' : 'badge-gray'">
        {{ acceptance.pass === true ? '通过' : acceptance.pass === false ? '未通过' : '数据不足' }}
      </span>
    </div>
    <div v-show="showAcceptance" class="card" style="padding:12px">
      <div style="margin-bottom:8px">
        <button class="btn btn-sm" :class="{ 'btn-primary': acceptanceProfile === 'absolute' }" @click="switchAcceptanceProfile('absolute')">绝对收益</button>
        <button class="btn btn-sm" :class="{ 'btn-primary': acceptanceProfile === 'relative' }" @click="switchAcceptanceProfile('relative')" style="margin-left:6px">相对收益</button>
      </div>
      <table v-if="acceptance">
        <thead><tr><th>指标</th><th>门槛</th><th>实际</th><th>结论</th></tr></thead>
        <tbody>
          <tr v-for="c in acceptance.checks" :key="c.metric">
            <td>{{ c.label }}</td>
            <td>{{ c.op }} {{ c.metric.includes('ratio') || c.metric.includes('return') || c.metric.includes('drawdown') ? (c.threshold * 100).toFixed(2) + '%' : c.threshold }}</td>
            <td>{{ c.value === null ? '�? : c.value.toFixed(4) }}</td>
            <td><span class="badge" :class="c.pass === true ? 'badge-green' : c.pass === false ? 'badge-red' : 'badge-gray'">{{ c.pass === true ? '�? : c.pass === false ? '未过' : '�? }}</span></td>
          </tr>
        </tbody>
      </table>
      <p v-else>无验收数�?/p>
    </div>
```

新增 `const showAcceptance = ref(false)`�?
- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/Backtest.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat(web): 回测列表门状态徽章与详情采纳验收块（口径切换�?
```

---

### Task 7: 组合/策略表单执行真实感与策略增强

**Files:**
- Modify: `web/src/views/Portfolios.vue`（常�?L52-61；GROUPS L64-114；empty* L116-138；openEdit* L245-342；template L465-523；fieldVisible L385�?- Test: `web/src/__tests__/Portfolios.test.ts`（追加）

**Interfaces:**
- Consumes: api 类型新字段�?- Produces: `switch` 字段类型；`fieldVisible` 支持布尔条件�?
- [ ] **Step 1: 写失败测�?*（追加到 `web/src/__tests__/Portfolios.test.ts`�?
```ts
// 新建组合表单展示「执行真实感」，提交 payload 含真实感字段
it('组合表单含执行真实感开关并�?payload 提交', async () => {
  // mount，mock getPortfolios/getStockPools/getFormulas；点�? 新建组合�?  // 断言文本�?'执行真实�?�?涨跌�?；存�?[data-field="price_limit_check_enabled"]
  // 勾�?volume_limit_enabled 后提交，断言 createPortfolio 入参�?volume_limit_enabled: true
})
// 子策略表单含「策略增强」组
it('子策略表单含策略增强组字�?, async () => {
  // 断言存在 [data-field="rotation_enabled"] �?[data-field="exposure_cap_enabled"]
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现**

(a) 集合与默认值：
- `PORTFOLIO_RATIO_FIELDS` �?`'volume_limit_ratio'`�?- `STRATEGY_RATIO_FIELDS` �?`'rotation_hysteresis', 'trend_trailing_stop_ratio'`�?- `emptyPortfolioForm()` �?`price_limit_check_enabled: true, volume_limit_enabled: false, volume_limit_ratio: 0.1`�?- `emptyStrategyForm()` �?`rotation_enabled: false, rotation_momentum_bars: 20, rotation_hysteresis: 0.05, regime_filter_enabled: false, trend_trailing_stop_ratio: null, trend_max_positions: null, exposure_cap_enabled: false`�?
(b) `PORTFOLIO_GROUPS` 末尾追加组：

```ts
  { title: '执行真实�?, fields: [
    { key: 'price_limit_check_enabled', label: '涨跌停约�?, span: 2, type: 'switch', hint: '涨停买不�?跌停卖不出，延后重试' },
    { key: 'volume_limit_enabled', label: '成交量约�?, span: 2, type: 'switch', hint: '�?bar 成交量占比封�? },
    { key: 'volume_limit_ratio', label: '成交量占比上�?, span: 2, type: 'percent', placeholder: '10', showIf: { key: 'volume_limit_enabled', equals: true } },
  ]},
```

(c) `STRATEGY_GROUPS` 末尾追加组：

```ts
  { title: '策略增强（默认关�?, fields: [
    { key: 'rotation_enabled', label: 'OPEN 择优轮动', span: 2, type: 'switch', hint: '满槽时强信号轮出最弱持�? },
    { key: 'rotation_momentum_bars', label: '轮动动量bars', span: 2, type: 'number', placeholder: '20', showIf: { key: 'rotation_enabled', equals: true } },
    { key: 'rotation_hysteresis', label: '轮动滞后', span: 2, type: 'percent', placeholder: '5', showIf: { key: 'rotation_enabled', equals: true } },
    { key: 'regime_filter_enabled', label: '市场状态闸', span: 2, type: 'switch', hint: '基准周线 bull 态用趋势参数' },
    { key: 'trend_trailing_stop_ratio', label: '趋势移动止损', span: 2, type: 'percent', placeholder: '3', showIf: { key: 'regime_filter_enabled', equals: true } },
    { key: 'trend_max_positions', label: '趋势最大持�?, span: 2, type: 'number', placeholder: '5', showIf: { key: 'regime_filter_enabled', equals: true } },
    { key: 'exposure_cap_enabled', label: '策略级敞口上�?, span: 2, type: 'switch', hint: '总持仓市�?�?capital_ratio × 初始资金' },
  ]},
```

(d) `fieldVisible` 扩展（保�?role 字符串兼容）�?
```ts
function fieldVisible(fld: { showIf?: string | { key: string; equals: unknown } }, form: Record<string, any>): boolean {
  if (!fld.showIf) return true
  if (typeof fld.showIf === 'string') return form.role === fld.showIf
  return form[fld.showIf.key] === fld.showIf.equals
}
```

(e) 表单渲染：两�?Modal 的字段渲染处（组�?L474-484、策�?L504-515）在 `<input v-if text/number/percent>` 之前插入�?
```vue
            <label v-if="fld.type === 'switch'" class="switch-row">
              <input type="checkbox" :data-field="fld.key" v-model="portfolioForm[fld.key]" />
              <span v-if="fld.hint" class="field-hint">{{ fld.hint }}</span>
            </label>
```

（策�?Modal 同理，用 `strategyForm[fld.key]`。）

注意：`buildPortfolioPayload`/`buildStrategyPayload` 遍历 GROUPS 生成 payload 时，switch 字段�?boolean，`fromPercent` 对非 number/percent 类型直接透传，无需改；但组�?Modal 的字段行需�?switch 行加 `v-show` 条件（组�?Modal 当前�?fieldVisible，需给字段行�?`v-show="fieldVisible(fld, portfolioForm)"`；策�?Modal 已有�?v-show，保持不变）�?
(f) `openEditPortfolio` �?`portfolioForm.value = {...}` �?`price_limit_check_enabled: detail.price_limit_check_enabled, volume_limit_enabled: detail.volume_limit_enabled, volume_limit_ratio: detail.volume_limit_ratio`（随�?`toPercent` 会处�?volume_limit_ratio）�?
`openEditStrategy` 的赋值补 7 个策略增强字段（`s.rotation_enabled` 等；null 的保�?null）�?
(g) 列表�?详情如需展示开关状态可不做（YAGNI）�?
- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/Portfolios.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Portfolios.vue web/src/__tests__/Portfolios.test.ts
git commit -m "feat(web): 组合执行真实感开关与子策略增强参数表�?
```

---

### Task 8: 公式表单严格未来函数检�?
**Files:**
- Modify: `web/src/views/Formulas.vue`（emptyForm L52、编辑回�?L87-93、submit L137-156、模�?L243-255、errorMsg 展示 L184�?- Test: `web/src/__tests__/Formulas.test.ts`（追加）

**Interfaces:**
- Consumes: `FormulaRequest.strict_future_check`�?
- [ ] **Step 1: 写失败测�?*（追加）

```ts
it('公式表单含严格未来函数检查并可提�?, async () => {
  // mount，mock getFormulas；点新建
  // 断言文本�?'严格未来函数检�?
  // 勾�?checkbox 后提交，断言 createFormula 入参 strict_future_check === true
})
it('后端 400 时顶部显示错误消�?, async () => {
  // mock createFormula reject { response: { data: { code: 400, message: '公式含未来函数：ZIG' } } }
  // 提交后断言页面文本�?'ZIG'
})
```

- [ ] **Step 2: 跑测试确认失�?*

Run: `npx vitest run src/__tests__/Formulas.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现**

- `emptyForm()` �?`strict_future_check: false`�?- 编辑回填（L87-93 �?`form.value = {...}`）加 `strict_future_check: false`（后端不回传该开关值，编辑默认关；�?`(f as any).strict_future_check ?? false`）�?- 模板（L251 formula_count 输入附近）加�?
```vue
      <label class="field-row" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" v-model="form.strict_future_check" data-field="strict_future_check" />
        严格未来函数检查（拒绝 ZIG/PEAK/BACKSET 等未来函数，拒绝后不入库�?      </label>
```

- `submit`：`form.value` 已含该字段，`createFormula(form.value)`/`updateFormula(...)` 直接带上（无需额外改，只要 emptyForm/回填含该键）。错误分支已 `errorMsg.value = ...`；确�?catch 里把后端 message 赋给 `errorMsg`（现 L152-156 若只 alert，改为同时设�?`errorMsg.value`）�?
- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/__tests__/Formulas.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Formulas.vue web/src/__tests__/Formulas.test.ts
git commit -m "feat(web): 公式表单加严格未来函数检查开�?
```

---

### Task 9: 全量验证与构�?
**Files:** 无源码改动（修复验证中发现的问题除外）�?
- [ ] **Step 1: 全量 vitest**

Run: `npx vitest run`（workdir `web/`�?Expected: 全绿（含既有 241 用例 + 新增）�?
- [ ] **Step 2: 类型检�?*

Run: `npx vue-tsc -b`（workdir `web/`�?Expected: 无错误�?
- [ ] **Step 3: 构建**

Run: `npm run build`（workdir `web/`�?Expected: 成功，产物写�?`web/dist/`�?
- [ ] **Step 4: Commit（若有构建产物纳入版本控制的约定�?*

```bash
git add web/dist
git commit -m "build(web): 重建 dist 以包含模型缺陷修复前端接�?
```

（若仓库 `.gitignore` 忽略 `web/dist`，跳过本步。）

- [ ] **Step 5: 部署提示（人工确认，不自动执行）**

- dev：重�?`uv run uvicorn core.main:app --reload`�?- 封装版：重新打包 `main/dist/tq-iquant/tq-iquant.exe`（另行确认）�?
---

## Self-Review

**Spec coverage�?*
- B6 口径：Task 1（类型）+ Task 5（发�?select�? Task 6（详情验收切换）�?- C7 门槛：Task 1 + Task 5（开关比�?响应 gate banner�? Task 6（列表徽�?详情门状态）�?- C8：Task 1 + Task 2 + Task 3 �?- P2b：Task 1 + Task 2 + Task 4 �?- 执行真实感：Task 1 + Task 7 �?- 未来函数：Task 1 + Task 8 �?- 构建/部署：Task 9 �?
**Placeholder scan�?* 测试用例中标注「以该文件现�?mock 风格实现」的部分需在实现时按现有文件补全断言；无 TBD/TODO 代码占位�?
**Type consistency�?* `AcceptanceResult`/`WalkForwardRunItem`/`ParamPerturbRunItem` 等类型名�?Task 1 定义、Task 3/4/6 消费，名称一致；`delta_cagr` 文案统一为「年化收益差」�?
**已知测试基建风险�?* �?vitest 未配�?`@vue/test-utils` �?jsdom，需�?Task 2 前确认（现有 `web/src/__tests__/` 已有组件测试则为已配置）�?
