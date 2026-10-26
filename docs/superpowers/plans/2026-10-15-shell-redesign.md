# 创懿量化 · 应用外壳重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动任何 API/数据流/业务逻辑的前提下，重构前端外壳：总览页品牌 Hero、可折叠宽侧栏、统一的可视化区头部带与区块框，并把浏览器标题改为「创懿量化回测平台」。

**Architecture:** 纯前端外壳改造。外壳状态（侧栏折叠）由 `App.vue` 持有并持久化到 `localStorage`；品牌与内容容器通过新增全局 CSS 类 + 复用组件 `PageHeader.vue` 实现；各业务视图仅在模板顶层套用 `PageHeader`，脚本逻辑零改动。

**Tech Stack:** Vue 3 `<script setup>` + TypeScript、Vue Router、Vite、Vitest + @vue/test-utils、纯 CSS 变量双主题。

## Global Constraints

- **不改后端**：`main/**` 全部零改动；不运行后端测试。
- **不改路由与导航集合**：`router/index.ts`、`NAV_GROUPS`/`NAV_STANDALONE` 项集合不变。
- **兼容契约（来自现有测试，逐条不可违反）**：
  - `.sidebar-nav .nav-item` 恰好 8 个，每个含一个 `svg.icon`（恰好 1 个 `path`）。
  - `.nav-group-label` 恰好 3 个且文本为 `研究`/`交易`/`系统`。
  - 主题切换仅在 `.topbar-right .theme-toggle`；`.sidebar-footer` 不得存在（底栏用 `.sidebar-footbar`，且**不含**主题开关）。
  - `.status-dot`（首个）在在线时为 `online`；`.status-chip` 文本含 `Core 在线`、版本、uptime。
  - `.topbar-path` 文本为 `route.path`。
  - `ICONS` 恰好 10 键，值合法 path `d`，且无 Emoji。→ **不得向 `ICONS` 新增键**；侧栏折叠箭头等新图形用**内联字面量 path**。
  - `Dashboard.test.ts` 断言 `w.text()` 含：`股票池`、`回测记录`、`Core 状态`、`在线`、`回测A`、`D:\new_tdx64`、`http://127.0.0.1:8790`；失败时含 `部分数据加载失败`。→ 这些文案必须保留。
- **禁止覆盖的既有 scoped 类**：不得新增名为 `.section-title` 的全局类（`Backtest.vue` 已用同名 scoped 类）。本计划用 `.frame-*` 前缀。
- **命名**：不复用文件名；CSS 前缀 `frame-` 表示区块框。
- 每个任务结束时测试必须全绿；提交信息遵循仓库现有风格（`feat:`/`style:` 前缀）。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `web/src/style.css` | 全局设计令牌 + 外壳/区块/图表卡样式类 | 修改 |
| `web/src/App.vue` | 外壳：侧栏（折叠+底栏）、顶栏（面包屑+标题+动态 title） | 修改 |
| `web/src/views/Dashboard.vue` | 总览：品牌 Hero + 区块框包裹 | 修改 |
| `web/src/components/PageHeader.vue` | 页面头部带组件（标题 + 面包屑 + 操作 slot） | 新增 |
| `web/src/views/StockPools.vue` | 套用 PageHeader | 修改 |
| `web/src/views/Formulas.vue` | 套用 PageHeader | 修改 |
| `web/src/views/Portfolios.vue` | 套用 PageHeader | 修改 |
| `web/src/views/LiveSessions.vue` | 套用 PageHeader | 修改 |
| `web/src/views/SystemConfig.vue` | 套用 PageHeader | 修改 |
| `web/src/views/Backtest.vue` | 列表视图套用 PageHeader（仅列表分支顶层） | 修改 |
| `web/src/__tests__/App.test.ts` | 新增折叠/面包屑用例 | 修改 |
| `web/src/__tests__/Dashboard.test.ts` | 新增 Hero 用例 | 修改 |
| `web/index.html` | 标题 + lang | 修改 |

---

## Task 1: 全局设计令牌与外壳/区块样式

**Files:**
- Modify: `web/src/style.css`

**Interfaces:**
- Produces（供后续任务使用的类名）：`.page-head`、`.page-head-crumb`、`.page-head-title`、`.page-head-actions`、`.frame`、`.frame-head`、`.frame-num`、`.frame-title`、`.frame-actions`、`.frame-body`、`.chart-card`、`.chart-card-head`、`.sidebar[data-collapsed]`、`.sidebar-footbar`、`.sidebar-collapse`、`.topbar-crumb`。

- [ ] **Step 1: 在 `:root` 内新增两个宽度令牌**

在 `web/src/style.css` 的 `:root { ... }` 块中（`--font-serif` 行之后）加入：

```css
  --sidebar-w: 210px;
  --rail-w: 52px;
```

- [ ] **Step 2: 侧栏改为使用令牌并加折叠样式**

将现有规则（当前为 `.sidebar{width:208px;background:var(--shell-bg);display:flex;flex-direction:column;flex-shrink:0;border-right:1px solid var(--border)}`）替换为：

```css
.sidebar{width:var(--sidebar-w);background:var(--shell-bg);display:flex;flex-direction:column;flex-shrink:0;border-right:1px solid var(--border);transition:width .2s ease;overflow:hidden}
.sidebar[data-collapsed]{width:var(--rail-w)}
.sidebar[data-collapsed] .sidebar-subtitle,
.sidebar[data-collapsed] .nav-item > span,
.sidebar[data-collapsed] .nav-group-label{display:none}
.sidebar[data-collapsed] .nav-item{justify-content:center;padding-left:0;padding-right:0}
.sidebar[data-collapsed] .sidebar-header{justify-content:center;padding-left:0;padding-right:0}
.sidebar[data-collapsed] .sidebar-footbar{justify-content:center;padding-left:0;padding-right:0}
.sidebar[data-collapsed] .sidebar-footbar-text{display:none}
.sidebar-header{display:flex;align-items:center;justify-content:space-between;gap:8px}
.sidebar-brand{min-width:0}
.sidebar-title{font-size:17px;font-weight:900;letter-spacing:.06em;color:var(--ink);border-left:3px solid var(--red);padding-left:10px;line-height:1.1;white-space:nowrap}
.sidebar-title .bt-2{color:var(--red)}
.sidebar-collapse{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;flex-shrink:0;border:1px solid var(--line);border-radius:var(--radius);background:transparent;color:var(--ink-soft);cursor:pointer;transition:all .15s}
.sidebar-collapse:hover{background:var(--hover-bg);color:var(--ink)}
.sidebar-footbar{display:flex;align-items:center;gap:8px;padding:10px 14px;border-top:1px solid var(--line);font-size:11px;color:var(--ink-soft);font-variant-numeric:tabular-nums}
```

> 说明：原 `.sidebar-header h1` / `.sidebar-subtitle` 规则保留（新版模板仍用 `h1.sidebar-title` 或 `.sidebar-subtitle`，见 Task 2）。折叠时标签隐藏但元素仍在 DOM（保证 `.nav-group-label` 仍为 3 个）。

- [ ] **Step 3: 顶栏面包屑样式**

在 `.topbar-title` 规则之后新增：

```css
.topbar-crumb{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.06em;color:var(--ink-faint);margin-bottom:1px}
.topbar-titles{display:flex;align-items:baseline;gap:8px}
```

- [ ] **Step 4: 页面头部带 / 区块框 / 图表卡样式**

在文件末尾（`:where(...):focus-visible` 之前或之后均可）新增：

```css
/* ===== 页面头部带（PageHeader）===== */
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding-bottom:14px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.page-head-left{display:flex;flex-direction:column;gap:4px;min-width:0}
.page-head-crumb{font-family:var(--font-mono);font-size:11px;letter-spacing:.08em;color:var(--ink-faint)}
.page-head-title{font-size:22px;font-weight:800;color:var(--ink);letter-spacing:.02em;line-height:1.2;border-left:3px solid var(--red);padding-left:11px}
.page-head-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}

/* ===== 区块框（编号 + 标题 + 细线 + 右上操作）===== */
.frame{border:1px solid var(--border);background:var(--card-bg);border-radius:var(--radius);margin-bottom:16px;overflow:hidden}
.frame-head{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line)}
.frame-num{font-family:var(--font-serif);font-size:13px;color:var(--red);letter-spacing:.02em;font-variant-numeric:tabular-nums}
.frame-title{font-size:14px;font-weight:700;color:var(--ink)}
.frame-actions{margin-left:auto;display:flex;align-items:center;gap:8px}
.frame-caption{font-size:11px;color:var(--ink-faint)}
.frame-body{padding:16px}

/* ===== 图表卡 ===== */
.chart-card{border:1px solid var(--border);background:var(--chart-bg);border-radius:var(--radius);margin-bottom:16px;overflow:hidden}
.chart-card-head{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line)}
.chart-card-title{font-size:14px;font-weight:700;color:var(--ink)}
.chart-card-actions{margin-left:auto;display:flex;align-items:center;gap:8px}
.chart-card-body{padding:12px 16px}
```

- [ ] **Step 5: 验证构建与既有测试未被样式影响**

Run（在 `web/`）：`npx vitest run`
Expected: 全部 PASS（本任务仅新增/替换 CSS，`App.test.ts` 中 8 项/3 组/主题位置/状态/路径断言仍成立）。

- [ ] **Step 6: Commit**

```bash
git add web/src/style.css
git commit -m "style(shell): 新增外壳/页面头部/区块框设计令牌与样式类"
```

---

## Task 2: 侧栏折叠与外壳重构（App.vue）

**Files:**
- Modify: `web/src/App.vue`
- Test: `web/src/__tests__/App.test.ts`

**Interfaces:**
- Consumes：Task 1 的 `.sidebar[data-collapsed]`、`.sidebar-collapse`、`.sidebar-footbar`、`.topbar-crumb` 类。
- Produces：`localStorage` 键 `sidebar:collapsed`（值 `'1'`/`'0'`）；DOM 结构 `.sidebar[data-collapsed]`、`.sidebar-collapse`、`.topbar-crumb`。

- [ ] **Step 1: 写失败测试（App.test.ts 追加）**

在 `web/src/__tests__/App.test.ts` 末尾追加（`routeState` 已在该文件顶部定义）：

```ts
describe('App.vue — 侧栏折叠', () => {
  beforeEach(() => {
    resetTheme()
    localStorage.clear()
  })

  it('默认展开；点击折叠按钮后出现 data-collapsed 并持久化', async () => {
    const w = mount(App)
    await flushPromises()
    expect(w.find('.sidebar').attributes('data-collapsed')).toBeUndefined()
    await w.find('.sidebar-collapse').trigger('click')
    expect(w.find('.sidebar').attributes('data-collapsed')).toBe('true')
    expect(localStorage.getItem('sidebar:collapsed')).toBe('1')
    await w.find('.sidebar-collapse').trigger('click')
    expect(w.find('.sidebar').attributes('data-collapsed')).toBeUndefined()
    expect(localStorage.getItem('sidebar:collapsed')).toBe('0')
  })

  it('初始化时读取 localStorage 折叠态', async () => {
    localStorage.setItem('sidebar:collapsed', '1')
    const w = mount(App)
    await flushPromises()
    expect(w.find('.sidebar').attributes('data-collapsed')).toBe('true')
  })

  it('顶栏面包屑显示「组 / 项」', async () => {
    routeState.path = '/portfolios'
    routeState.name = 'portfolios'
    const w = mount(App)
    await flushPromises()
    expect(w.find('.topbar-crumb').text()).toBe('研究 / 组合策略')
    routeState.path = '/dashboard'
    routeState.name = 'dashboard'
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run（在 `web/`）：`npx vitest run src/__tests__/App.test.ts`
Expected: FAIL —— `.sidebar-collapse` 不存在 / `data-collapsed` 未定义 / `.topbar-crumb` 不存在。

- [ ] **Step 3: 重写 `web/src/App.vue`**

```vue
<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useTheme } from './composables/useTheme'
import { getSystemStatus, type SystemStatus } from './api'
import { ICONS } from './icons'

const route = useRoute()
const { theme, toggle: toggleTheme } = useTheme()

const titles: Record<string, string> = {
  'dashboard': '首页', 'stock-pools': '股票池', 'formulas': '公式管理', 'portfolios': '组合策略',
  'backtest': '回测管理', 'backtest-analysis': '回归验证', 'live': '实盘交易', 'system': '系统配置',
}

interface NavEntry { to: string; label: string; icon: string }
interface NavGroup { label: string; items: NavEntry[] }

// 「总览」独立于三组之外（无组标签），保持导航顺序：总览 → 研究 → 交易 → 系统
const NAV_STANDALONE: NavEntry = { to: '/dashboard', label: '总览', icon: 'dashboard' }

const NAV_GROUPS: NavGroup[] = [
  {
    label: '研究',
    items: [
      { to: '/stock-pools', label: '股票池', icon: 'pools' },
      { to: '/formulas', label: '公式管理', icon: 'formula' },
      { to: '/portfolios', label: '组合策略', icon: 'portfolio' },
    ],
  },
  {
    label: '交易',
    items: [
      { to: '/backtest', label: '回测管理', icon: 'backtest' },
      { to: '/backtest/analysis', label: '回归验证', icon: 'backtest' },
      { to: '/live', label: '实盘交易', icon: 'live' },
    ],
  },
  {
    label: '系统',
    items: [
      { to: '/system', label: '系统配置', icon: 'system' },
    ],
  },
]

// 侧栏折叠（持久化）；折叠箭头用内联 path，不加入 ICONS
const collapsed = ref(localStorage.getItem('sidebar:collapsed') === '1')
watch(collapsed, (v) => { localStorage.setItem('sidebar:collapsed', v ? '1' : '0') })
function toggleCollapsed() { collapsed.value = !collapsed.value }

// 面包屑：由 route 反查「组 / 项」
const crumb = computed<string>(() => {
  const path = route.path
  if (path === NAV_STANDALONE.to) return NAV_STANDALONE.label
  for (const g of NAV_GROUPS) {
    const hit = g.items.find((it) => it.to === path)
    if (hit) return `${g.label} / ${hit.label}`
  }
  return titles[route.name as string] || '创懿量化'
})

const status = ref<SystemStatus | null>(null)
const statusFailed = ref(false)
let statusTimer: number | null = null

async function refreshStatus() {
  try {
    status.value = await getSystemStatus()
    statusFailed.value = false
  } catch {
    statusFailed.value = true
  }
}

function coreLabel(): string {
  if (statusFailed.value || !status.value) return 'Core 未知'
  return status.value.core.online ? 'Core 在线' : 'Core 离线'
}

function coreDotClass(): string {
  if (statusFailed.value || !status.value) return ''
  return status.value.core.online ? 'online' : 'offline'
}

// 浏览器标题
watch(
  () => route.name,
  () => { document.title = '创懿量化回测平台 · ' + (titles[route.name as string] || '创懿量化') },
  { immediate: true },
)

onMounted(() => {
  refreshStatus()
  statusTimer = window.setInterval(refreshStatus, 30000)
})

onUnmounted(() => {
  if (statusTimer !== null) window.clearInterval(statusTimer)
})
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar" :data-collapsed="collapsed ? 'true' : undefined">
      <div class="sidebar-header">
        <div class="sidebar-brand">
          <h1 class="sidebar-title">创懿<span class="bt-2">量化</span></h1>
          <div class="sidebar-subtitle">CHUANGYI QUANT</div>
        </div>
        <button
          class="sidebar-collapse"
          @click="toggleCollapsed"
          :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'"
          :title="collapsed ? '展开侧栏' : '折叠侧栏'"
        >
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="collapsed ? 'M9 6l6 6-6 6' : 'M15 6l-6 6 6 6'" />
          </svg>
        </button>
      </div>
      <nav class="sidebar-nav">
        <router-link
          :to="NAV_STANDALONE.to"
          class="nav-item"
          active-class="active"
          :title="NAV_STANDALONE.label"
        >
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="ICONS[NAV_STANDALONE.icon]" />
          </svg>
          <span>{{ NAV_STANDALONE.label }}</span>
        </router-link>
        <template v-for="g in NAV_GROUPS" :key="g.label">
          <div class="nav-group-label">{{ g.label }}</div>
          <router-link
            v-for="item in g.items"
            :key="item.to"
            :to="item.to"
            class="nav-item"
            active-class="active"
            :title="item.label"
          >
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="ICONS[item.icon]" />
            </svg>
            <span>{{ item.label }}</span>
          </router-link>
        </template>
      </nav>
      <div class="sidebar-footbar">
        <span class="status-dot" :class="coreDotClass()" />
        <span class="sidebar-footbar-text">{{ coreLabel() }}</span>
      </div>
    </aside>
    <div class="main-container">
      <header class="topbar">
        <div class="topbar-left">
          <span class="topbar-crumb">{{ crumb }}</span>
          <div class="topbar-titles">
            <span class="topbar-title">{{ titles[route.name as string] || '创懿量化' }}</span>
            <span class="topbar-path">{{ route.path }}</span>
          </div>
        </div>
        <div class="topbar-right">
          <span class="status-chip">
            <span class="status-dot" :class="coreDotClass()" />
            <span>{{ coreLabel() }}</span>
            <span v-if="status">v{{ status.core.version }} · {{ status.core.uptime }}</span>
          </span>
          <button class="btn btn-sm theme-toggle" @click="toggleTheme" :aria-label="theme === 'dark' ? '切换到浅色模式' : '切换到暗色模式'">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="theme === 'dark' ? ICONS.sun : ICONS.moon" />
            </svg>
            <span>{{ theme === 'dark' ? '浅色' : '暗色' }}</span>
          </button>
        </div>
      </header>
      <main class="content"><router-view /></main>
    </div>
  </div>
</template>
```

> 关键点：首个 `.status-dot` 位于侧栏底栏（DOM 早于顶栏），在线时为 `online`，满足 `App.test.ts` 的 `.status-dot` 断言；`.status-chip` 保留完整内容；`.topbar-path` 仍渲染 `route.path`；`.sidebar-footer` 从未出现。

- [ ] **Step 4: 运行 App 测试确认通过**

Run（在 `web/`）：`npx vitest run src/__tests__/App.test.ts`
Expected: PASS（既有 8 项/3 组/主题位置/状态/路径 + 新增 3 例）。

- [ ] **Step 5: Commit**

```bash
git add web/src/App.vue web/src/__tests__/App.test.ts
git commit -m "feat(shell): 侧栏可折叠+底栏、顶栏面包屑、动态浏览器标题"
```

---

## Task 3: 总览页品牌 Hero 与区块框（Dashboard.vue）

**Files:**
- Modify: `web/src/views/Dashboard.vue`
- Test: `web/src/__tests__/Dashboard.test.ts`

**Interfaces:**
- Consumes：Task 1 的 `.frame*`、`.page-head*` 类；现有 `load()`、`poolCount/formulaCount/portfolioCount/backtestCount/liveCount`、`cfg/status/recentBacktests`。
- Produces：DOM `.dash-hero`（含文案 `创懿`/`量化`/`CHUANGYI QUANT`）。

- [ ] **Step 1: 写失败测试（Dashboard.test.ts 追加）**

在 `web/src/__tests__/Dashboard.test.ts` 的 `describe('Dashboard.vue', ...)` 内追加：

```ts
  it('渲染品牌 Hero（创懿 / 量化 / CHUANGYI QUANT）', async () => {
    const w = mount(Dashboard)
    await flushPromises()
    expect(w.find('.dash-hero').exists()).toBe(true)
    expect(w.text()).toContain('创懿')
    expect(w.text()).toContain('量化')
    expect(w.text()).toContain('CHUANGYI QUANT')
    w.unmount()
  })
```

- [ ] **Step 2: 运行测试确认失败**

Run（在 `web/`）：`npx vitest run src/__tests__/Dashboard.test.ts`
Expected: FAIL —— `.dash-hero` 不存在、文本缺少 `CHUANGYI QUANT`。

- [ ] **Step 3: 在 `Dashboard.vue` 模板加入 Hero 与区块框**

将现有模板（`<template> ... </template>` 全部）替换为下述内容。**注意**：KPI 文案、最近回测表、系统配置、`部分数据加载失败` 提示均逐字保留。

```vue
<template>
  <div class="dash-hero">
    <div class="dash-hero-main">
      <h1 class="dash-brand">创懿<span class="db-2">量化</span></h1>
      <div class="dash-brand-sub">CHUANGYI QUANT · 回测平台</div>
      <div class="dash-hero-stats">
        <span><b>{{ poolCount }}</b> 股票池</span>
        <span><b>{{ formulaCount }}</b> 公式</span>
        <span><b>{{ portfolioCount }}</b> 组合</span>
        <span><b>{{ backtestCount }}</b> 回测</span>
        <span><b class="db-accent">{{ liveCount }}</b> 实盘</span>
      </div>
    </div>
    <div class="dash-hero-actions">
      <button @click="load" class="btn">刷新</button>
    </div>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:16px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:16px"><p>加载中…</p></div>
  <template v-else>
    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">01</span>
        <span class="frame-title">核心指标</span>
      </div>
      <div class="frame-body">
        <div class="metric-grid" style="gap:12px;margin-bottom:0">
          <div class="metric-card">
            <span class="metric-label">股票池</span>
            <span class="metric-value">{{ poolCount }}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">公式</span>
            <span class="metric-value">{{ formulaCount }}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">组合策略</span>
            <span class="metric-value">{{ portfolioCount }}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">回测记录</span>
            <span class="metric-value">{{ backtestCount }}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">实盘会话</span>
            <span class="metric-value">{{ liveCount }}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">Core 状态</span>
            <span class="metric-value" :style="{ fontSize: '15px' }">
              {{ status ? (status.core.online ? '在线' : '离线') : '-' }}
            </span>
            <span class="metric-label" style="font-size:11px">
              {{ status ? `v${status.core.version} · 运行 ${status.core.uptime}` : '未获取到状态' }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">02</span>
        <span class="frame-title">最近回测</span>
      </div>
      <div class="frame-body">
        <div class="table-wrap">
          <table>
            <thead><tr><th>名称</th><th>区间</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="r in recentBacktests" :key="r.id">
                <td>{{ r.name }}</td>
                <td style="color:var(--ink-soft);font-variant-numeric:tabular-nums">{{ fmtDate(r.start_date) }} ~ {{ fmtDate(r.end_date) }}</td>
                <td>
                  <span v-if="r.status === 'completed'" class="badge badge-green">完成</span>
                  <span v-else-if="r.status === 'running'" class="badge badge-blue">运行中</span>
                  <span v-else class="badge badge-gray">{{ r.status }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="recentBacktests.length === 0" class="empty-state"><p>暂无回测记录</p></div>
        </div>
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">03</span>
        <span class="frame-title">系统配置</span>
      </div>
      <div class="frame-body">
        <p v-if="cfg" style="margin:0;color:var(--ink-soft);line-height:1.8">
          通达信路径：{{ cfg.tdx_path }}<br>
          iQuant 路径：{{ cfg.iquant_path }}<br>
          仿真桥：{{ cfg.iquant_bridge.simulation.base_url }}<br>
          实盘桥：{{ cfg.iquant_bridge.live.base_url }}
        </p>
        <p v-else style="margin:0;color:var(--ink-soft)">配置加载失败</p>
      </div>
    </div>
  </template>
</template>
```

- [ ] **Step 4: 追加 Hero 样式（`Dashboard.vue` 的 `<style scoped>`）**

在 `Dashboard.vue` 底部新增（若已有 `<style scoped>` 则追加其后）：

```vue
<style scoped>
.dash-hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  padding: 4px 2px 20px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--line);
}
.dash-brand {
  font-size: 34px;
  font-weight: 900;
  letter-spacing: -.01em;
  line-height: 1.05;
  margin: 0;
  background: linear-gradient(135deg, var(--ink) 0%, var(--ink-soft) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
}
.dash-brand .db-2 { color: var(--red); -webkit-text-fill-color: var(--red); margin-left: .1em; text-shadow: 0 2px 12px var(--red-wash) }
.dash-brand-sub {
  margin-top: 10px;
  font-size: 12px;
  letter-spacing: .3em;
  color: var(--ink-faint);
  font-weight: 500;
  text-transform: uppercase;
}
.dash-brand-sub::before, .dash-brand-sub::after {
  content: '';
  display: inline-block;
  width: 30px;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--ink-faint), transparent);
  vertical-align: middle;
  margin: 0 12px;
}
.dash-hero-stats { display: flex; flex-wrap: wrap; gap: 8px 22px; margin-top: 14px; font-size: 12px; color: var(--ink-soft) }
.dash-hero-stats b { font-family: var(--font-serif); font-size: 18px; font-weight: 800; color: var(--ink); margin-right: 5px; font-variant-numeric: tabular-nums }
.dash-hero-stats .db-accent { color: var(--red) }
.dash-hero-actions { flex-shrink: 0 }
</style>
```

> 若 `Dashboard.vue` 当前没有 `<style>` 块，则按上述新增；若已存在，把选择器并入同一 `<style scoped>`。

- [ ] **Step 5: 运行 Dashboard 测试确认通过**

Run（在 `web/`）：`npx vitest run src/__tests__/Dashboard.test.ts`
Expected: PASS（既有 2 例含 `股票池`/`回测记录`/`Core 状态`/`在线`/`回测A`/路径/`部分数据加载失败` + 新增 Hero 例）。

- [ ] **Step 6: Commit**

```bash
git add web/src/views/Dashboard.vue web/src/__tests__/Dashboard.test.ts
git commit -m "feat(dashboard): 品牌 Hero 带与统一区块框"
```

---

## Task 4: PageHeader 组件与各视图增量套用

**Files:**
- Create: `web/src/components/PageHeader.vue`
- Modify: `web/src/views/StockPools.vue`、`Formulas.vue`、`Portfolios.vue`、`LiveSessions.vue`、`SystemConfig.vue`、`Backtest.vue`

**Interfaces:**
- Consumes：Task 1 的 `.page-head*` 全局类。
- Produces：组件 `PageHeader`，props `title: string`、`crumbs?: string[]`，默认插槽为右侧操作区。

- [ ] **Step 1: 新建 `web/src/components/PageHeader.vue`**

```vue
<script setup lang="ts">
defineProps<{ title: string; crumbs?: string[] }>()
</script>

<template>
  <div class="page-head">
    <div class="page-head-left">
      <span v-if="crumbs && crumbs.length" class="page-head-crumb">{{ crumbs.join(' / ') }}</span>
      <h2 class="page-head-title">{{ title }}</h2>
    </div>
    <div class="page-head-actions"><slot /></div>
  </div>
</template>
```

- [ ] **Step 2: `StockPools.vue` 套用**

在 `<script setup>` 的 import 区加入：

```ts
import PageHeader from '../components/PageHeader.vue'
```

将模板顶部：

```vue
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="load" class="btn">刷新</button>
  </div>
```

替换为：

```vue
  <PageHeader title="股票池">
    <button @click="load" class="btn">刷新</button>
  </PageHeader>
```

- [ ] **Step 3: `Formulas.vue` 套用**

加入 `import PageHeader from '../components/PageHeader.vue'`，将模板顶部右侧按钮行：

```vue
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end;gap:8px">
    <button @click="openTdx" class="btn tdx-import-btn">从通达信导入</button>
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </div>
```

替换为：

```vue
  <PageHeader title="公式管理">
    <button @click="openTdx" class="btn tdx-import-btn">从通达信导入</button>
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </PageHeader>
```

> 用编辑器打开 `Formulas.vue` 核对按钮文案现状（当前文件为 UTF-8），保持按钮的 `@click`/`class` 不变，仅改外层容器为 `PageHeader`。

- [ ] **Step 4: `Portfolios.vue` 套用**

加入 import；模板首个子元素原本为：

```vue
  <div>
    <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
      <button @click="openCreatePortfolio" class="btn btn-primary">+ 新建组合</button>
    </div>
```

替换为（保留外层 `<div>`，仅把内部动作行换成 PageHeader）：

```vue
  <div>
    <PageHeader title="组合策略">
      <button @click="openCreatePortfolio" class="btn btn-primary">+ 新建组合</button>
    </PageHeader>
```

> 确认外层 `<div>` 的闭合标签仍在文件末尾对应位置，未破坏结构。

- [ ] **Step 5: `LiveSessions.vue` 套用**

加入 import；将模板顶部：

```vue
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="openCreate" class="btn btn-primary">+ 新建实盘</button>
  </div>
```

替换为：

```vue
  <PageHeader title="实盘交易">
    <button @click="openCreate" class="btn btn-primary">+ 新建实盘</button>
  </PageHeader>
```

- [ ] **Step 6: `SystemConfig.vue` 套用（根节点下方插入）**

加入 import；该视图模板根为 `<div class="card" ...>`。在 `<template>` 打开后、该 `div` 之前插入：

```vue
  <PageHeader title="系统配置" />
```

（Vue 3 允许多根节点。）

- [ ] **Step 7: `Backtest.vue` 列表视图套用（仅列表分支）**

加入 `import PageHeader from '../components/PageHeader.vue'`。在模板中，列表分支 `<div v-if="!currentRecord">` 内、原动作行之前插入：

```vue
    <PageHeader title="回测管理" />
```

保留其后的原动作行（含「回归验证 / 发起回测」等按钮）不变。

- [ ] **Step 8: 运行这些视图的测试**

Run（在 `web/`）：
`npx vitest run src/__tests__/StockPools.test.ts src/__tests__/Formulas.test.ts src/__tests__/Portfolios.test.ts src/__tests__/LiveSessions.test.ts src/__tests__/SystemConfig.test.ts src/__tests__/Backtest.test.ts`
Expected: 全部 PASS。若某用例失败（例如断言顶层结构），检查是否误删既有元素或 class；仅调整包裹方式，不改视图脚本。

- [ ] **Step 9: Commit**

```bash
git add web/src/components/PageHeader.vue web/src/views/StockPools.vue web/src/views/Formulas.vue web/src/views/Portfolios.vue web/src/views/LiveSessions.vue web/src/views/SystemConfig.vue web/src/views/Backtest.vue
git commit -m "feat(views): 新增 PageHeader 组件并在各视图套用页面头部带"
```

---

## Task 5: 浏览器标题与 lang + 全量验证

**Files:**
- Modify: `web/index.html`

- [ ] **Step 1: 修改 `web/index.html`**

将 `lang="en"` 改为 `lang="zh-CN"`；将 `<title>web</title>` 改为 `<title>创懿量化回测平台</title>`。

- [ ] **Step 2: 全量前端测试**

Run（在 `web/`）：`npx vitest run`
Expected: 全部 PASS（含 App/Dashboard 新增用例与既有 263 例）。

- [ ] **Step 3: 生产构建**

Run（在 `web/`）：`npm run build`
Expected: 构建成功，无类型/模板错误。

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "chore(web): 标题改为「创懿量化回测平台」并设置 lang=zh-CN"
```

- [ ] **Step 5: 手工验收（浏览器）**

启动前端 dev（`web/` 下 `npm run dev`；后端 dev server 已在 `127.0.0.1:8000`，若未启动见 AGENTS.md）。逐项确认：
- 浏览器标签标题为「创懿量化回测平台」，切换路由后标题随之变化。
- 总览页顶部显示品牌 Hero（创懿·量化 / CHUANGYI QUANT · 回测平台 / 关键统计），下方三块区块框编号 01/02/03。
- 侧栏宽约 210px，点击折叠按钮收成 52px 图标栏；刷新页面折叠状态保持。
- 顶栏显示「组 / 项」面包屑；主题切换在顶栏，浅/暗两主题下 Hero 与区块框观感正常。
- 股票池/公式/组合/实盘/回测/系统各页顶部出现页面头部带，功能按钮可点。

---

## 自检记录（Self-Review）

- **Spec 覆盖**：品牌 Hero（Task 3）、侧栏 S3+S2（Task 1+2）、可视化区 V2（Task 1+3+4）、`index.html` 标题/lang（Task 5）、兼容契约（Global Constraints + 各任务测试）均有对应任务。
- **占位符**：无 TBD/TODO；所有代码步骤含完整代码或精确查找/替换文本。
- **类型/命名一致性**：`PageHeader` props 在 Task 4 定义并在各视图以 `title` 使用；`sidebar:collapsed` 键在 Task 2 测试与实现中一致；`.frame-*` / `.page-head*` 类名在 Task 1 定义、Task 3/4 使用一致。
- **与 spec 命名的偏差（有意）**：spec 3.5 写作 `.section-frame/.section-head/.section-num`，因 `Backtest.vue` 已存在 scoped `.section-title`，本计划改用 `.frame*` 前缀避免覆盖冲突。
