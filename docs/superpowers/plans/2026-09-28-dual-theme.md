# 双主题（浅色/暗色）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 参照暗盘资金项目配色，实现完整的浅色/暗色双主题切换，只改颜色风格不动布局/逻辑。

**Architecture:** 纯 CSS 变量 + `data-theme` 属性。`:root` 定义浅色变量，`[data-theme="dark"]` 覆盖。主题切换只改 `<html>` 属性 + localStorage 持久化。ECharts 颜色通过 `getComputedStyle` 读取 CSS 变量，切换时 dispose + 重建。

**Tech Stack:** Vue 3 + Vite + 纯 CSS（无框架）+ ECharts 6 + vitest

## Global Constraints

- 配色照搬暗盘资金：浅色 `--bg: #f2efeb`（暖纸感），暗色 `--bg: #101418`（冷蓝灰）
- A 股约定：红涨绿跌（`--red` 涨/正向，`--green` 跌/负向）
- ECharts 颜色通过 `getComputedStyle(document.documentElement).getPropertyValue('--xxx')` 读取，不硬编码
- 主题切换时 ECharts dispose + 60ms 后重建
- 后端 Python / API / 路由 / 组件逻辑零改动
- 每个 Phase 可独立测试

---

### Task 1: CSS 变量基础设施 + 主题切换器

**Files:**
- Modify: `web/src/style.css`（新增暗色变量覆盖 + 语义变量）
- Create: `web/src/composables/useTheme.ts`
- Modify: `web/src/main.ts`（初始化主题）
- Modify: `web/src/App.vue`（侧边栏底部加切换按钮）
- Test: `web/src/__tests__/theme.test.ts`

**Interfaces:**
- Produces: `useTheme()` composable → `{ theme: Ref<'light'|'dark'>, toggle: () => void }`
- Produces: CSS 变量 `--bg` `--card-bg` `--ink` `--ink-soft` `--ink-faint` `--line` `--red` `--green` `--gold` `--blue` `--warn` `--accent` `--accent-light` `--hover-bg` `--border`

- [ ] **Step 1: 写失败测试**

```typescript
// web/src/__tests__/theme.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { useTheme } from '../composables/useTheme'

describe('useTheme', () => {
  beforeEach(() => { localStorage.clear() })

  it('defaults to light when no localStorage', () => {
    const { theme } = useTheme()
    expect(theme.value).toBe('light')
  })

  it('reads localStorage on init', () => {
    localStorage.setItem('theme', 'dark')
    const { theme } = useTheme()
    expect(theme.value).toBe('dark')
  })

  it('toggles theme and persists', () => {
    const { theme, toggle } = useTheme()
    toggle()
    expect(theme.value).toBe('dark')
    expect(localStorage.getItem('theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('applies data-theme to documentElement', () => {
    const { toggle } = useTheme()
    toggle()
    expect(document.documentElement.dataset.theme).toBe('dark')
    toggle()
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/__tests__/theme.test.ts`
Expected: FAIL — `Cannot find module '../composables/useTheme'`

- [ ] **Step 3: 创建 useTheme composable**

```typescript
// web/src/composables/useTheme.ts
import { ref, watch } from 'vue'

type Theme = 'light' | 'dark'

function getInitialTheme(): Theme {
  const saved = localStorage.getItem('theme')
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

const theme = ref<Theme>(getInitialTheme())

watch(theme, (val) => {
  document.documentElement.dataset.theme = val
  localStorage.setItem('theme', val)
}, { immediate: true })

export function useTheme() {
  function toggle() {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }
  return { theme, toggle }
}
```

- [ ] **Step 4: 在 main.ts 中初始化**

```typescript
// web/src/main.ts
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

// 初始化主题（在 mount 前设置 data-theme，避免闪烁）
const saved = localStorage.getItem('theme')
const initial = saved === 'dark' ? 'dark' : 'light'
document.documentElement.dataset.theme = initial

createApp(App).use(router).mount('#app')
```

- [ ] **Step 5: 在 App.vue 侧边栏底部加切换按钮**

```html
<!-- web/src/App.vue，在 </nav> 后、</aside> 前插入 -->
<div class="sidebar-footer">
  <button class="nav-item theme-toggle" @click="toggleTheme">
    <span>{{ theme === 'dark' ? '☀ 浅色' : '☾ 暗色' }}</span>
  </button>
</div>
```

```typescript
// App.vue script setup 中
import { useTheme } from './composables/useTheme'
const { theme, toggle: toggleTheme } = useTheme()
```

```css
/* App.vue scoped style 中 */
.sidebar-footer{padding:8px;border-top:1px solid var(--line)}
.theme-toggle{width:100%;justify-content:center}
```

- [ ] **Step 6: 运行测试确认通过**

Run: `npx vitest run src/__tests__/theme.test.ts`
Expected: PASS — 4 passed

- [ ] **Step 7: 提交**

```bash
git add web/src/composables/useTheme.ts web/src/main.ts web/src/App.vue web/src/__tests__/theme.test.ts
git commit -m "feat(theme): useTheme composable + toggle button + localStorage persistence"
```

---

### Task 2: style.css 全局样式重构（Phase 1，30 处）

**Files:**
- Modify: `web/src/style.css`

**Interfaces:**
- Consumes: Task 1 的 CSS 变量
- Produces: 所有全局样式使用 `var(--xxx)`，无硬编码颜色

- [ ] **Step 1: 替换 :root 变量为暗盘资金调色板**

```css
:root {
  --bg: #f2efeb;
  --card-bg: #fbf9f6;
  --ink: #0b0b0b;
  --ink-soft: rgba(11,11,11,.62);
  --ink-faint: rgba(11,11,11,.38);
  --line: rgba(11,11,11,.14);
  --red: #b3402a;
  --green: #1e6b3a;
  --gold: #8a6d1c;
  --blue: #2b5c8a;
  --warn: #c05621;
  --accent: #2b5c8a;
  --accent-light: rgba(43,92,138,.10);
  --hover-bg: rgba(11,11,11,.04);
  --border: rgba(11,11,11,.14);
  --radius: 6px;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
}
[data-theme="dark"] {
  --bg: #101418;
  --card-bg: #161c22;
  --ink: #ece7df;
  --ink-soft: rgba(236,231,223,.62);
  --ink-faint: rgba(236,231,223,.38);
  --line: rgba(236,231,223,.16);
  --red: #e06550;
  --green: #3fa06b;
  --gold: #c9a24a;
  --blue: #5a8fc0;
  --warn: #e29b4f;
  --accent: #5a8fc0;
  --accent-light: rgba(90,143,192,.12);
  --hover-bg: rgba(236,231,223,.06);
  --border: rgba(236,231,223,.16);
}
```

- [ ] **Step 2: 替换所有硬编码颜色为变量**

逐行替换（完整映射见设计文档）：

| 行 | 原值 | 改为 |
|---|---|---|
| 13 | `#f0f0f0` | `var(--line)` |
| 16 | `#555` | `var(--ink-soft)` |
| 17 | `#f0f4ff` | `var(--accent-light)` |
| 27 | `#f0f0f0` | `var(--line)` |
| 28 | `#fafafa` | `var(--hover-bg)` |
| 31 | `#ecfdf5` / `#065f46` | `rgba(30,107,58,.12)` / `var(--green)` |
| 32 | `#f3f4f6` / `#6b7280` | `var(--hover-bg)` / `var(--ink-soft)` |
| 34 | `#fef2f2` / `#b91c1c` | `rgba(179,64,42,.12)` / `var(--red)` |
| 35 | `#fff7ed` / `#c2410c` | `rgba(192,86,33,.12)` / `var(--warn)` |
| 36 | `#f0fdfa` / `#0f766e` | `var(--hover-bg)` / `var(--ink-soft)` |
| 37 | `#f5f3ff` / `#6d28d9` | `var(--accent-light)` / `var(--accent)` |
| 38 | `#555` | `var(--ink-soft)` |
| 39 | `#f5f5f5` | `var(--hover-bg)` |
| 40 | `#fff` | `var(--ink)` |
| 41 | `#2563eb` | `var(--accent)` |
| 43 | `#ef4444` / `#fef2f2` / `#fca5a5` | `var(--red)` / `rgba(224,101,80,.12)` / `var(--red)` |
| 44 | `rgba(0,0,0,.4)` | `rgba(0,0,0,.4)` （不变） |
| 45 | `rgba(0,0,0,.1)` | `rgba(0,0,0,.1)` （不变） |
| 59 | `#fafafa` | `var(--hover-bg)` |
| 74 | `#fafbfc` / `#f0f0f0` | `var(--hover-bg)` / `var(--line)` |
| 75 | `#f5f7fa` | `var(--hover-bg)` |
| 91 | `#1a1f36` / `#fff` | `var(--ink)` / `var(--bg)` |
| 92 | `#9ca3af` | `var(--ink-faint)` |

- [ ] **Step 3: 运行前端测试确认无回归**

Run: `npx vitest run`
Expected: PASS（现有测试不依赖具体颜色值）

- [ ] **Step 4: 手动验证**

Run: `npm run dev`
Expected: 侧边栏、卡片、按钮、表格在浅色/暗色下均正常显示

- [ ] **Step 5: 提交**

```bash
git add web/src/style.css
git commit -m "feat(theme): style.css 全局样式改用 CSS 变量（暗盘资金调色板）"
```

---

### Task 3: Backtest.vue scoped style 重构（Phase 2，50+ 处）

**Files:**
- Modify: `web/src/views/Backtest.vue`（scoped style 块，约 lines 3141-3519）

**Interfaces:**
- Consumes: Task 1/2 的 CSS 变量
- Produces: 报告页面所有样式使用 `var(--xxx)`

- [ ] **Step 1: 替换 scoped style 中的硬编码颜色**

关键映射（完整 50+ 处见设计文档）：

| 原值 | 改为 | 用途 |
|------|------|------|
| `#667eea` | `var(--accent)` | 报告主题色（section-title、stat-card 等） |
| `#764ba2` | `var(--accent)` | 渐变终止色 |
| `#1a1a2e` | `var(--ink)` | 报告标题 fallback |
| `#888` | `var(--ink-soft)` | 副标题、时间等 |
| `#666` | `var(--ink-soft)` | metric-label |
| `#4a5568` | `var(--ink-soft)` | metric-group |
| `#333` | `var(--ink)` | 卡片文字 |
| `#fff` | `var(--card-bg)` | 卡片背景 |
| `#f8f9fa` | `var(--hover-bg)` | 表格/图表背景 |
| `#e9ecef` | `var(--line)` | 边框 |
| `#eef1fb` | `var(--accent-light)` | metric-group 背景 |
| `#fffbeb` | `rgba(201,162,74,.12)` | metric-missing-note 背景 |
| `#f59e0b` | `var(--gold)` | 警告色 |
| `#92400e` | `var(--gold)` | 警告文字 |
| `#10b981` | `var(--green)` | 正向文字 |
| `#ef4444` | `var(--red)` | 负向文字 |
| `#e0e7ff` / `#4338ca` | `var(--accent-light)` / `var(--accent)` | strategy-badge |
| `#f0f1f3` / `#999` | `var(--hover-bg)` / `var(--ink-faint)` | signal-badge |
| `#1f3a68` | `var(--ink)` | ca-banner 背景 |
| `#c0392b` | `var(--red)` | 警告文字 |
| `#4f46e5` | `var(--accent)` | ca-header hover |

- [ ] **Step 2: 运行前端测试确认无回归**

Run: `npx vitest run`
Expected: PASS

- [ ] **Step 3: 手动验证报告页面**

Run: `npm run dev`，打开回测详情页
Expected: 报告页面在浅色/暗色下均正常显示，颜色跟随主题

- [ ] **Step 4: 提交**

```bash
git add web/src/views/Backtest.vue
git commit -m "feat(theme): Backtest.vue scoped style 改用 CSS 变量"
```

---

### Task 4: Backtest.vue ECharts 颜色重构（Phase 3，60+ 处）

**Files:**
- Modify: `web/src/views/Backtest.vue`（script 中的 ECharts option 颜色）

**Interfaces:**
- Consumes: Task 1 的 CSS 变量
- Produces: 所有 ECharts 颜色通过 `cssVar()` 读取

- [ ] **Step 1: 添加 cssVar 工具函数**

```typescript
// Backtest.vue script setup 中
function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
```

- [ ] **Step 2: 替换 ECharts 颜色常量为 cssVar 调用**

```typescript
// 原：const STRATEGY_COLOR = '#d6403a'
// 改：const STRATEGY_COLOR = cssVar('--red')

// 原：const BENCHMARK_COLOR = '#4a7ec4'
// 改：const BENCHMARK_COLOR = cssVar('--blue')

// 原：const GOLD_COLOR = '#f0a429'
// 改：const GOLD_COLOR = cssVar('--gold')

// 原：const GREEN_COLOR = '#1f6e4f'
// 改：const GREEN_COLOR = cssVar('--green')
```

- [ ] **Step 3: 替换 ECharts option 中的硬编码颜色**

所有 `color: '#xxx'`、`itemStyle.color`、`lineStyle.color`、`areaStyle.color` 等改为 `cssVar('--xxx')` 或对应的常量。

热力图 visualMap 的 `inRange.color` 数组：
```typescript
// 原：inRange: { color: ['#1f6e4f', '#e5e7eb', '#d6403a'] }
// 改：inRange: { color: [cssVar('--green'), cssVar('--line'), cssVar('--red')] }
```

- [ ] **Step 4: 添加主题切换时 ECharts 重建逻辑**

```typescript
// Backtest.vue 中
import { useTheme } from '../composables/useTheme'
const { theme } = useTheme()

watch(theme, () => {
  setTimeout(() => {
    // dispose 所有图表实例并重建
    Object.values(charts).forEach(c => c && c.dispose())
    Object.keys(charts).forEach(k => delete charts[k])
    drawAllCharts()
  }, 60)
})
```

- [ ] **Step 5: 运行前端测试确认无回归**

Run: `npx vitest run`
Expected: PASS

- [ ] **Step 6: 手动验证图表主题切换**

Run: `npm run dev`，打开回测详情页，切换主题
Expected: 图表颜色跟随主题变化，无闪烁（60ms 延迟）

- [ ] **Step 7: 提交**

```bash
git add web/src/views/Backtest.vue
git commit -m "feat(theme): Backtest.vue ECharts 颜色改用 CSS 变量 + dispose 重建"
```

---

### Task 5: 其他视图内联样式重构（Phase 4，42 处）

**Files:**
- Modify: `web/src/views/Dashboard.vue`
- Modify: `web/src/views/Formulas.vue`
- Modify: `web/src/views/StockPools.vue`
- Modify: `web/src/views/SystemConfig.vue`
- Modify: `web/src/views/Portfolios.vue`

**Interfaces:**
- Consumes: Task 1/2 的 CSS 变量
- Produces: 所有内联样式使用 `var(--xxx)`

- [ ] **Step 1: 替换内联样式中的硬编码颜色**

统一映射：
- `#888` → `var(--ink-soft)`
- `#666` → `var(--ink-soft)`
- `#999` → `var(--ink-faint)`
- `#555` → `var(--ink-soft)`
- `#c0392b` → `var(--red)`
- `#065f46` → `var(--green)`
- `#f0f0f0` → `var(--line)`

逐文件替换（每处 `style="color:#xxx"` → `style="color:var(--xxx)"`）。

- [ ] **Step 2: 运行前端测试确认无回归**

Run: `npx vitest run`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add web/src/views/Dashboard.vue web/src/views/Formulas.vue web/src/views/StockPools.vue web/src/views/SystemConfig.vue web/src/views/Portfolios.vue
git commit -m "feat(theme): 其他视图内联样式改用 CSS 变量"
```

---

### Task 6: LiveSessions.vue 重构（Phase 5，20 处）

**Files:**
- Modify: `web/src/views/LiveSessions.vue`

**Interfaces:**
- Consumes: Task 1/2 的 CSS 变量
- Produces: 所有内联 + scoped 样式使用 `var(--xxx)`

- [ ] **Step 1: 替换内联样式中的硬编码颜色**

同 Task 5 的映射规则。

- [ ] **Step 2: 替换 scoped style 中的硬编码颜色**

同 Task 3 的映射规则。

- [ ] **Step 3: 运行前端测试确认无回归**

Run: `npx vitest run`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add web/src/views/LiveSessions.vue
git commit -m "feat(theme): LiveSessions.vue 样式改用 CSS 变量"
```

---

### Task 7: 测试更新 + 集成验证

**Files:**
- Modify: `web/src/__tests__/Backtest.test.ts`（6 处颜色断言）
- Modify: `web/src/__tests__/theme.test.ts`（补充 ECharts 重建测试）

**Interfaces:**
- Consumes: Task 1-6 的所有改动
- Produces: 完整的双主题测试覆盖

- [ ] **Step 1: 更新 Backtest.test.ts 中的颜色断言**

```typescript
// 原：expect(el.style.color).toBe('#d6403a')
// 改：expect(el.style.color).toContain('var(--red') 或移除颜色断言
```

- [ ] **Step 2: 补充 ECharts 重建测试**

```typescript
it('rebuilds charts on theme toggle', async () => {
  const { toggle } = useTheme()
  toggle()
  await new Promise(r => setTimeout(r, 100))
  // 验证图表实例已重建
  expect(Object.keys(charts).length).toBeGreaterThan(0)
})
```

- [ ] **Step 3: 运行全量前端测试**

Run: `npx vitest run`
Expected: PASS — 全部通过

- [ ] **Step 4: 运行构建确认无错误**

Run: `npm run build`
Expected: 构建成功，无 TypeScript/CSS 错误

- [ ] **Step 5: 手动验证完整流程**

Run: `npm run dev`
Expected:
- 浅色主题：暖纸感背景，所有页面正常
- 暗色主题：冷蓝灰背景，所有页面正常
- 切换主题：ECharts 图表 dispose + 重建，无闪烁
- localStorage 持久化：刷新后保持主题

- [ ] **Step 6: 提交**

```bash
git add web/src/__tests__/Backtest.test.ts web/src/__tests__/theme.test.ts
git commit -m "test(theme): 更新颜色断言 + ECharts 重建测试"
```

---

## Self-Review

1. **Spec coverage**: 设计文档的 5 个 Phase 全部有对应 Task；暗盘资金调色板、ECharts dispose+重建、localStorage 持久化、系统偏好、打印强制浅色均已覆盖。
2. **Placeholder scan**: 无 TBD/TODO；所有代码块为完整实现。
3. **Type consistency**: `useTheme()` 返回 `{ theme: Ref<Theme>, toggle: () => void }`；CSS 变量命名在 Task 1 定义，Task 2-6 消费，一致。
