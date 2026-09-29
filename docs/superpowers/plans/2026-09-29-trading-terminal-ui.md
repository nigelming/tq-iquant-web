# 专业交易终端视觉重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把创懿量化前端从暖米色工具页改造为冷调专业交易终端视觉：换色板、提密度、Emoji 换内联 SVG、顶栏加 Core 状态胶囊。

**Architecture:** 纯前端视觉层改造，不动任何业务逻辑与数据流。全部颜色继续走 CSS 变量（`data-theme` 切换即刻生效），只改 `style.css` 的 token 值与基元选择器、`App.vue` 的外壳结构、新增一个 `icons.ts` 图标字典。**保留全部现有 token 名**，使上一轮已完成的 22 张 ECharts 主题化与 `reactive` 颜色常量零改动继承新色板。

**Tech Stack:** Vue 3.5 (`<script setup>` / SFC)、Vite 8、TypeScript 6、Vitest 4 + @vue/test-utils 2、`vue-tsc` 类型检查。**不新增任何 npm 依赖。**

## Global Constraints

以下约束对**每个 Task 都生效**，不得违反：

- **禁止新增 npm 依赖。** `web/package.json` 的 `dependencies` 必须保持 `axios` / `echarts` / `vue` / `vue-router` 四项不变。
- **禁止硬编码颜色。** 所有新写/改的颜色必须用 `var(--token)` 或经 `cssVar('--token')` 读取。唯一允许的字面色值是 `style.css` 内 `:root` / `[data-theme="dark"]` 两个 token 块中的 token 定义本身。
- **禁止改动这 7 个文件**（超出本次范围）：`Dashboard.vue`、`StockPools.vue`、`Formulas.vue`、`Portfolios.vue`、`Backtest.vue`、`LiveSessions.vue`、`SystemConfig.vue` 的**模板与脚本**。唯一例外见 Task 5。
- **禁止改动 `style.css` 中 `--score-1` 至 `--score-5`、`--green-wash`、`--red-wash`、`--warn-wash`、`--gold-wash`、`--overlay`、`--shadow-sm`、`--shadow-md`、`--accent-light` 之外的既有语义 token 名**。可以改其值（如 `--red`），不可改名——改名会波及 `Backtest.vue` 的 `cssVar('--xxx')` 调用点。
- **禁止删除既有 token。** 只允许新增与改值。
- 测试基线：`npx vitest run` = **199 passed**，`npx vue-tsc -b` = **exit 0**。每个 Task 结束前两者都必须复现该基线（Task 3–4 后为 205 passed，Task 5 后为 207 passed）。
- 提交粒度：每个 Task 独立 commit，message 用中文，遵循仓库现有前缀风格（`feat:` / `fix:` / `test:` / `docs:`）。
- 文件编码 UTF-8 **无 BOM**，行尾 CRLF。修改 `web/src/` 下文件时若使用 PowerShell，必须用 `[System.IO.File]::WriteAllText($p, $c, (New-Object System.Text.UTF8Encoding($false)))`，**不可用** `Set-Content -Encoding UTF8`（PowerShell 5.1 会写 BOM）。
- 修改 `Backtest.vue` 这类 3600+ 行的大文件时**不可用** `Set-Content` 全文重写（会破坏 CRLF 与 BOM 状态），必须用 `edit` 工具做定点替换。

---

## File Structure

| 文件 | 责任 | 本次动作 |
|---|---|---|
| `web/src/style.css` | 全局设计 token + 所有基元选择器（layout/table/badge/button/modal/form） | 改 token 值、新增 10 个 token、密度与排版调整 |
| `web/src/App.vue` | 应用外壳：侧边栏导航 + 顶栏 | 换 SVG 图标、导航分三组、顶栏加状态胶囊、主题切换迁移到顶栏 |
| `web/src/icons.ts` | 图标 path 字典（纯字符串常量，无逻辑） | **新建** |
| `web/src/__tests__/App.test.ts` | 外壳测试：图标渲染、导航结构、状态胶囊三态 | **新建** |
| `web/src/__tests__/Backtest.test.ts` | 已有 199 个测试 | Task 5 追加 3 个（图表底色 token 化） |

`icons.ts` 只导出常量不做逻辑，所以单独成文件而不放进 `App.vue`：Task 3 单独交付它，Task 4 才消费；将来若其他视图也要图标可直接 import 而不必碰 `App.vue`。

---

## Task 1: 冷调专业色板（token 改值）

纯色值替换，零选择器改动。单独成 Task 的原因：这是唯一可能影响既有测试断言的步骤，需要独立 review gate；若出问题可单独 `git revert` 而不影响其他 Task。

**Files:**
- Modify: `web/src/style.css:1-62`（`:root` 与 `[data-theme="dark"]` 两个块）
- Test: 无新增测试，靠既有 199 个测试回归

**Interfaces:**
- Consumes: 无
- Produces: Task 2–5 依赖的 10 个新 token：`--font-mono`、`--chart-bg`、`--shell-bg`、`--nav-active-bar`、`--focus-ring`、`--shadow-card`、`--shadow-pop`、`--ticker-up`、`--ticker-down`；以及改值后的 `--bg`、`--card-bg`、`--ink`、`--ink-soft`、`--ink-faint`、`--line`、`--border`、`--red`、`--green`、`--gold`、`--blue`、`--warn`、`--accent`、`--accent-light`、`--radius`、`--hover-bg`

- [ ] **Step 1: 记录改动前基线**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
```

Expected: `Test Files 13 passed (13)` / `Tests 199 passed (199)`。若不是 199，先停下排查，不要继续。

- [ ] **Step 2: 替换 `:root` 块的 token 定义**

把 `web/src/style.css` 第 1–32 行整个 `:root { ... }` 块替换为：

```css
:root {
  --bg: #eef1f4;
  --card-bg: #ffffff;
  --ink: #0f172a;
  --ink-soft: rgba(15,23,42,.66);
  --ink-faint: rgba(15,23,42,.42);
  --line: rgba(15,23,42,.11);
  --red: #c62828;
  --green: #1b7f4b;
  --gold: #a16207;
  --blue: #2f4a7a;
  --warn: #b45309;
  --accent: #2f4a7a;
  --accent-light: rgba(47,74,122,.09);
  --score-1: #c0392b;
  --score-2: #c85a10;
  --score-3: #8a7408;
  --score-4: #2c7a4b;
  --score-5: #14532d;
  --green-wash: rgba(30,107,58,.12);
  --red-wash: rgba(179,64,42,.12);
  --warn-wash: rgba(192,86,33,.12);
  --gold-wash: rgba(138,109,28,.18);
  --accent-glow: rgba(47,74,122,.25);
  --shadow-sm: 0 2px 8px rgba(15,23,42,.15);
  --shadow-md: 0 4px 12px rgba(15,23,42,.10);
  --overlay: rgba(15,23,42,.40);
  --hover-bg: rgba(15,23,42,.045);
  --border: rgba(15,23,42,.11);
  --ticker-up: #c62828;
  --ticker-down: #1b7f4b;
  --chart-bg: #ffffff;
  --shell-bg: #f7f9fb;
  --nav-active-bar: #2f4a7a;
  --focus-ring: rgba(47,74,122,.35);
  --shadow-card: 0 1px 2px rgba(15,23,42,.06), 0 1px 1px rgba(15,23,42,.04);
  --shadow-pop: 0 8px 24px rgba(15,23,42,.12);
  --font-mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Liberation Mono", monospace;
  --radius: 4px;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
}
```

注意 `--score-1..5` 与五个 wash token 的**值必须与改动前完全一致**（Global Constraints 要求），上面照抄了现值。`--accent-glow` 的值从 `rgba(43,92,138,.25)` 改为 `rgba(47,74,122,.25)`（跟随新 accent 的 RGB 47,74,122）。

- [ ] **Step 3: 替换 `[data-theme="dark"]` 块的 token 定义**

把第 33–62 行整个 `[data-theme="dark"] { ... }` 块替换为：

```css
[data-theme="dark"] {
  --bg: #0d1117;
  --card-bg: #161b22;
  --ink: #e6edf3;
  --ink-soft: rgba(230,237,243,.66);
  --ink-faint: rgba(230,237,243,.42);
  --line: rgba(230,237,243,.14);
  --red: #f2564d;
  --green: #3fb27f;
  --gold: #d4a94a;
  --blue: #6ba3d0;
  --warn: #e29b4f;
  --accent: #6ba3d0;
  --accent-light: rgba(107,163,208,.14);
  --score-1: #ef5f47;
  --score-2: #f08c3a;
  --score-3: #d8b93a;
  --score-4: #4aa96f;
  --score-5: #2f8a58;
  --green-wash: rgba(63,160,107,.16);
  --red-wash: rgba(224,101,80,.16);
  --warn-wash: rgba(226,155,79,.16);
  --gold-wash: rgba(201,162,74,.20);
  --accent-glow: rgba(107,163,208,.30);
  --shadow-sm: 0 2px 10px rgba(0,0,0,.45);
  --shadow-md: 0 4px 14px rgba(0,0,0,.50);
  --overlay: rgba(0,0,0,.60);
  --hover-bg: rgba(230,237,243,.07);
  --border: rgba(230,237,243,.14);
  --ticker-up: #f2564d;
  --ticker-down: #3fb27f;
  --chart-bg: #161b22;
  --shell-bg: #0b0f14;
  --nav-active-bar: #6ba3d0;
  --focus-ring: rgba(107,163,208,.45);
  --shadow-card: 0 1px 3px rgba(0,0,0,.40);
  --shadow-pop: 0 8px 24px rgba(0,0,0,.55);
}
```

注意暗色块**不重复定义** `--font-mono` / `--radius` / `--sans`（这三个两模式共享）。

- [ ] **Step 4: 验证无重复 token 定义**

```bash
cd D:\project\tq-iquant-web\web
$c = [System.IO.File]::ReadAllText("src\style.css")
$rootBlock = [regex]::Match($c, '(?s):root\s*\{(.*?)\n\}').Groups[1].Value
$darkBlock = [regex]::Match($c, '(?s)\[data-theme="dark"\]\s*\{(.*?)\n\}').Groups[1].Value
$rootVars = [regex]::Matches($rootBlock, '(--[a-z0-9-]+)\s*:') | ForEach-Object { $_.Groups[1].Value }
$darkVars = [regex]::Matches($darkBlock, '(--[a-z0-9-]+)\s*:') | ForEach-Object { $_.Groups[1].Value }
"root count=$($rootVars.Count) unique=$(($rootVars | Select-Object -Unique).Count)"
"dark count=$($darkVars.Count) unique=$(($darkVars | Select-Object -Unique).Count)"
"dup in root: " + (($rootVars | Group-Object | Where-Object Count -gt 1 | ForEach-Object Name) -join ',')
"dup in dark: " + (($darkVars | Group-Object | Where-Object Count -gt 1 | ForEach-Object Name) -join ',')
"dark-only: " + (($darkVars | Where-Object { $rootVars -notcontains $_ }) -join ',')
```

Expected: `root count=39 unique=39`、`dark count=36 unique=36`、`dup in root:` 与 `dup in dark:` 均为空、`dark-only:` 为空。

（39 − 3 = 36 的差额正是两模式共享、不在暗色块重复定义的 `--font-mono` / `--radius` / `--sans`。）

- [ ] **Step 5: 验证文件无 BOM、行尾未变**

```bash
cd D:\project\tq-iquant-web\web
$b = [System.IO.File]::ReadAllBytes("src\style.css")
"first3=$($b[0]),$($b[1]),$($b[2])"
$c = [System.IO.File]::ReadAllText("src\style.css")
"crlf=" + ([regex]::Matches($c,"`r`n")).Count + " lfOnly=" + ([regex]::Matches($c,"(?<!`r)`n")).Count
```

Expected: `first3=58,58,10`（即 `:r`——`58` 是 `:` 的 ASCII，不是 `239,187,191` 的 BOM）；`lfOnly=0`。

- [ ] **Step 6: 跑全量测试 + 类型检查**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```

Expected: `Tests 199 passed (199)`、`Test Files 13 passed (13)`、vue-tsc 无输出且 exit 0。

若有测试失败，**先看失败断言是否含颜色字面串**。已知安全点：`Backtest.test.ts` 的 `CSS_VARS` 是自洽 mock，不读真实 `style.css`，所以色板换值不影响它。

- [ ] **Step 7: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/style.css
git commit -m "feat(theme): 冷调专业色板 — 浅色转冷灰、暗色转墨蓝黑，新增 10 个终端感 token"
```

---

## Task 2: 排版密度与基元样式

**Files:**
- Modify: `web/src/style.css`（第 64 行起的选择器区；用 `edit` 做定点替换，**不改 token 块**）
- Test: 无新增测试，靠既有 199 个测试回归

**Interfaces:**
- Consumes: Task 1 的全部 token（含新增的 `--font-mono`、`--shadow-card`、`--chart-bg`、`--focus-ring`）
- Produces: Task 4 依赖的类名 `.icon`、`.nav-group-label`、`.status-dot`、`.status-chip`、`.topbar-path`

- [ ] **Step 1: 替换 layout 基元（sidebar / topbar / content / card）**

在 `style.css` 中把现有的：

```css
.sidebar{width:200px;background:var(--card-bg);display:flex;flex-direction:column;flex-shrink:0;border-right:1px solid var(--border)}
.sidebar-header{padding:20px 18px;border-bottom:1px solid var(--line)}
.sidebar-header h1{font-size:18px;font-weight:600;color:var(--ink)}
.sidebar-nav{flex:1;padding:8px;display:flex;flex-direction:column}
.topbar{height:52px;background:var(--card-bg);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;flex-shrink:0}
.topbar-title{font-size:16px;font-weight:500;color:var(--ink)}
.content{flex:1;padding:20px;overflow-y:auto}
.card{background:var(--card-bg);border-radius:var(--radius);border:1px solid var(--border)}
```

替换为：

```css
.sidebar{width:208px;background:var(--shell-bg);display:flex;flex-direction:column;flex-shrink:0;border-right:1px solid var(--border)}
.sidebar-header{padding:18px 16px 14px;border-bottom:1px solid var(--line)}
.sidebar-header h1{font-size:17px;font-weight:650;color:var(--ink);letter-spacing:.2px}
.sidebar-subtitle{font-size:11px;color:var(--ink-faint);margin-top:3px;letter-spacing:.3px}
.sidebar-nav{flex:1;padding:8px 8px;display:flex;flex-direction:column;overflow-y:auto}
.nav-group-label{font-size:11px;font-weight:600;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.6px;padding:14px 14px 5px}
.topbar{height:48px;background:var(--shell-bg);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 20px;flex-shrink:0}
.topbar-left{display:flex;flex-direction:column;gap:1px}
.topbar-title{font-size:15px;font-weight:600;color:var(--ink)}
.topbar-path{font-family:var(--font-mono);font-size:11px;color:var(--ink-faint)}
.topbar-right{display:flex;align-items:center;gap:10px}
.content{flex:1;padding:20px;overflow-y:auto}
.card{background:var(--card-bg);border-radius:var(--radius);border:1px solid var(--border);box-shadow:var(--shadow-card)}
```

- [ ] **Step 2: 替换 nav-item 状态样式**

把现有的：

```css
.nav-item{display:flex;align-items:center;padding:10px 14px;margin-bottom:2px;border-radius:var(--radius);color:var(--ink-soft);font-size:14px;transition:all .15s}
.nav-item:hover{background:var(--accent-light);color:var(--accent)}
.nav-item.active{background:var(--accent-light);color:var(--accent);font-weight:500}
```

替换为：

```css
.nav-item{display:flex;align-items:center;gap:9px;padding:9px 14px;margin-bottom:1px;border-radius:var(--radius);color:var(--ink-soft);font-size:13.5px;font-weight:500;transition:background .15s,color .15s}
.nav-item:hover{background:var(--hover-bg);color:var(--ink)}
.nav-item.active{background:var(--accent-light);color:var(--accent);box-shadow:inset 3px 0 0 var(--nav-active-bar)}
```

注意 hover 不再改文字色（避免与 active 混淆），active 靠左侧 3px 高亮条 + accent 底色双重标识。

- [ ] **Step 3: 替换 table 密度与数字等宽**

把现有的：

```css
table{width:100%;border-collapse:collapse;font-size:13.5px}
thead th{text-align:left;padding:10px 14px;font-weight:500;color:var(--ink-soft);border-bottom:1px solid var(--border);font-size:12px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
tbody td{padding:10px 14px;border-bottom:1px solid var(--line)}
```

替换为：

```css
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{text-align:left;padding:8px 14px;font-weight:600;color:var(--ink-soft);border-bottom:1px solid var(--border);font-size:11.5px;letter-spacing:.5px;white-space:nowrap}
tbody td{padding:8px 14px;border-bottom:1px solid var(--line)}
.num{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
```

- [ ] **Step 4: 替换 badge 密度**

把现有的：

```css
.badge{display:inline-flex;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:500}
```

替换为：

```css
.badge{display:inline-flex;padding:2px 8px;border-radius:3px;font-size:11.5px;font-weight:500;font-variant-numeric:tabular-nums}
```

- [ ] **Step 5: 替换 modal 阴影为 --shadow-pop**

把现有的：

```css
.modal-content{background:var(--card-bg);border-radius:8px;padding:24px;min-width:360px;box-shadow:var(--shadow-md)}
```

替换为：

```css
.modal-content{background:var(--card-bg);border-radius:var(--radius);padding:24px;min-width:360px;box-shadow:var(--shadow-pop)}
```

- [ ] **Step 6: 追加图标 / 状态点 / 状态胶囊 / 图表底色选择器**

在 `style.css` 文件**末尾**（第 149 行之后）追加：

```css
/* ===== 专业交易终端：图标 / 状态 / 图表底色 ===== */
.icon{width:17px;height:17px;flex-shrink:0;vertical-align:-3px}
.nav-item.active .icon{color:var(--accent)}

.status-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--border);border-radius:var(--radius);background:var(--card-bg);font-size:11.5px;color:var(--ink-soft);font-variant-numeric:tabular-nums}
.status-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0;background:var(--ink-faint)}
.status-dot.online{background:var(--ticker-down);animation:pulse 2s ease-in-out infinite}
.status-dot.offline{background:var(--ticker-up)}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 var(--focus-ring)}50%{opacity:.75;box-shadow:0 0 0 3px transparent}}

/* 图表画布真实底色：与 .card-bg 对齐，扇区间分隔线描边色（Backtest.vue 读 --card-bg）才正确 */
.chart-box{background:var(--chart-bg)}
.ca-chart{background:var(--chart-bg)}
.chart-container{background:var(--chart-bg)}

:where(button,a,input,select,textarea):focus-visible{outline:2px solid var(--focus-ring);outline-offset:1px}
```

- [ ] **Step 7: 验证**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
Select-String -Path "src\style.css" -Pattern "chart-box|ca-chart|chart-container" | ForEach-Object { "$($_.LineNumber): $($_.Line.Trim())" }
```

Expected: `199 passed`、vue-tsc exit 0；最后一条命令应显示 3 处新增（若显示 0 处说明 Step 6 未生效，检查是否误写进 token 块内）。

- [ ] **Step 8: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/style.css
git commit -m "feat(ui): 终端密度与基元样式 — 直角 4px、紧凑表格、等宽数字、SVG 图标与状态点样式"
```

---

## Task 3: SVG 图标字典

**Files:**
- Create: `web/src/icons.ts`
- Test: `web/src/__tests__/App.test.ts`（与 Task 4 同文件；本 Task 只写图标相关 2 个用例）

**Interfaces:**
- Consumes: 无
- Produces: `export const ICONS: Record<string, string>`，键为 `dashboard` / `pools` / `formula` / `portfolio` / `backtest` / `live` / `system` / `sun` / `moon` / `search`，值为 SVG path 的 `d` 属性字符串

- [ ] **Step 1: 写失败测试**

新建 `web/src/__tests__/App.test.ts`，写入以下完整内容（本 Task 阶段 `App.vue` 还没用上图标，所以 2 个用例都应失败）：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'
import { ICONS } from '../icons'
import { resetTheme } from '../composables/useTheme'

// 路由 mock：App.vue 调 useRoute() 且模板用 <router-link>，
// 不装 router 插件时 useRoute() 返回 undefined 会直接抛错。
const routeState = reactive({ path: '/dashboard', name: 'dashboard' })
vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  RouterLink: {
    props: ['to'],
    template: '<a class="router-link-stub" :href="to"><slot /></a>',
  },
}))

vi.mock('../api', () => ({
  getSystemStatus: vi.fn().mockResolvedValue({
    core: { online: true, version: '1.0', uptime: '0h53m' },
  }),
}))

import App from '../App.vue'

const KEYS = ['dashboard', 'pools', 'formula', 'portfolio', 'backtest', 'live', 'system', 'sun', 'moon', 'search']

describe('icons.ts', () => {
  it('导出 10 个图标 key，值均为合法 path d 字符串', () => {
    expect(Object.keys(ICONS).sort()).toEqual([...KEYS].sort())
    for (const k of KEYS) {
      expect(typeof ICONS[k]).toBe('string')
      expect(ICONS[k].length).toBeGreaterThan(4)
      expect(ICONS[k]).toMatch(/^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.\-+]+$/)
    }
  })

  it('无 Emoji 混入（专业终端不用 Emoji 图标）', () => {
    for (const k of KEYS) {
      expect(ICONS[k]).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
    }
  })
})

describe('App.vue — 图标渲染', () => {
  beforeEach(() => {
    resetTheme()
    localStorage.clear()
  })

  it('侧边栏 7 个导航项各渲染一个内联 SVG 图标（非 Emoji 文本节点）', async () => {
    const w = mount(App)
    await flushPromises()
    const items = w.findAll('.sidebar-nav .nav-item')
    expect(items).toHaveLength(7)
    items.forEach((it) => {
      const svg = it.find('svg.icon')
      expect(svg.exists()).toBe(true)
      expect(svg.findAll('path')).toHaveLength(1)
      expect(svg.text()).toBe('')
    })
    expect(w.text()).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/App.test.ts
```

Expected: FAIL，错误为 `Failed to resolve import "../icons" from "src/__tests__/App.test.ts"`（文件不存在）。

- [ ] **Step 3: 创建 `web/src/icons.ts`**

写入以下完整内容：

```ts
// 单色描边图标（24×24 viewBox，绘制范围留 2 单位呼吸）。
// 值是 SVG <path> 的 d 属性；渲染时统一用 currentColor 描边，自动继承文字色。
export const ICONS: Record<string, string> = {
  // 总览：2×2 网格
  dashboard: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  // 股票池：列表（竖线 + 三条不等长横线）
  pools: 'M4 5h16M4 12h11M4 19h16M4 5v14',
  // 公式管理：f(x) 曲线
  formula: 'M4 19c5 0 7-14 12-14M16 5h4M18 3v4M4 19h3',
  // 组合策略：两个错位矩形
  portfolio: 'M4 8h11v11H4zM9 4h11v11',
  // 回测管理：折线图（轴 + 折线 + 端点）
  backtest: 'M4 4v16h16M7 15l4-5 3 3 5-7',
  // 实盘交易：闪电
  live: 'M13 2 5 13h6l-1 9 8-11h-6z',
  // 系统配置：齿轮
  system: 'M12 9a3 3 0 100 6 3 3 0 000-6M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1',
  // 浅色模式：太阳
  sun: 'M12 8a4 4 0 100 8 4 4 0 000-8M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4',
  // 暗色模式：新月
  moon: 'M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z',
  // 放大镜（预留）
  search: 'M11 4a7 7 0 100 14 7 7 0 000-14M16 16l4 4',
}
```

- [ ] **Step 4: 运行测试**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/App.test.ts
```

Expected: `icons.ts` 两个用例 PASS（3 passed 中的 2 个），`App.vue — 图标渲染` 那个仍 FAIL（还没改 `App.vue`）。此时总数应为 `1 failed | 2 passed`。

- [ ] **Step 5: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/icons.ts web/src/__tests__/App.test.ts
git commit -m "feat(ui): 新增 icons.ts — 10 个手写单色描边 SVG 图标"
```

---

## Task 4: 应用外壳改造（侧边栏 + 顶栏 + 状态胶囊）

**Files:**
- Modify: `web/src/App.vue`（全文替换，该文件仅 42 行）
- Test: `web/src/__tests__/App.test.ts`（追加 4 个用例）

**Interfaces:**
- Consumes: Task 2 的 `.icon` / `.nav-group-label` / `.status-chip` / `.status-dot` / `.topbar-path` / `.topbar-left` / `.topbar-right` / `.sidebar-subtitle` 类名；Task 3 的 `ICONS`
- Produces: `App.vue` 无对外导出（全局外壳）

- [ ] **Step 1: 追加失败测试**

在 `web/src/__tests__/App.test.ts` **末尾**追加：

```ts
describe('App.vue — 外壳结构', () => {
  beforeEach(() => {
    resetTheme()
    localStorage.clear()
  })

  it('侧边栏导航分三组（研究/交易/系统）并有组标签', async () => {
    const w = mount(App)
    await flushPromises()
    const labels = w.findAll('.nav-group-label').map((e) => e.text())
    expect(labels).toEqual(['研究', '交易', '系统'])
    expect(w.findAll('.nav-group-label')).toHaveLength(3)
  })

  it('主题切换按钮在顶栏（不在侧边栏底部）', async () => {
    const w = mount(App)
    await flushPromises()
    expect(w.find('.topbar-right').find('.theme-toggle').exists()).toBe(true)
    expect(w.find('.sidebar-footer').exists()).toBe(false)
  })

  it('Core 在线时状态点为 online 且显示版本与运行时长', async () => {
    const w = mount(App)
    await flushPromises()
    const dot = w.find('.status-dot')
    expect(dot.exists()).toBe(true)
    expect(dot.classes()).toContain('online')
    expect(w.find('.status-chip').text()).toContain('Core 在线')
    expect(w.find('.status-chip').text()).toContain('v1.0')
    expect(w.find('.status-chip').text()).toContain('0h53m')
  })

  it('顶栏显示当前路由路径（等宽字体）', async () => {
    const w = mount(App)
    await flushPromises()
    expect(w.find('.topbar-path').text()).toBe('/dashboard')
  })
})
```

- [ ] **Step 2: 运行验证失败**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/App.test.ts
```

Expected: 4 个新增用例全 FAIL（`.nav-group-label`、`.theme-toggle` 在顶栏、`.status-chip`、`.topbar-path` 均不存在）。

- [ ] **Step 3: 重写 `web/src/App.vue`**

用 `write` 工具覆盖整个文件（该文件仅 42 行，全量重写比多次 edit 更安全；`write` 工具不写 BOM）：

```vue
<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useTheme } from './composables/useTheme'
import { getSystemStatus, type SystemStatus } from './api'
import { ICONS } from './icons'

const route = useRoute()
const { theme, toggle: toggleTheme } = useTheme()

const titles: Record<string, string> = {
  'dashboard': '首页', 'stock-pools': '股票池', 'formulas': '公式管理', 'portfolios': '组合策略',
  'backtest': '回测管理', 'live': '实盘交易', 'system': '系统配置',
}

interface NavEntry { to: string; label: string; icon: string }
interface NavGroup { label: string; items: NavEntry[] }

// 「总览」独立于三组之外（无组标签），保持改动前的导航顺序：总览 → 研究 → 交易 → 系统
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
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>创懿量化</h1>
        <div class="sidebar-subtitle">量化交易平台</div>
      </div>
      <nav class="sidebar-nav">
        <router-link
          :to="NAV_STANDALONE.to"
          class="nav-item"
          active-class="active"
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
          >
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="ICONS[item.icon]" />
            </svg>
            <span>{{ item.label }}</span>
          </router-link>
        </template>
      </nav>
    </aside>
    <div class="main-container">
      <header class="topbar">
        <div class="topbar-left">
          <span class="topbar-title">{{ titles[route.name as string] || '创懿量化' }}</span>
          <span class="topbar-path">{{ route.path }}</span>
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

**注意**：「总览」不属于任何分组，作为独立项渲染在 `NAV_GROUPS` 之前且**不带组标签**——因为它是首页入口，与「系统配置」语义不同。这同时保持了改动前的导航顺序（总览 → 股票池 → 公式管理 → 组合策略 → 回测管理 → 实盘交易 → 系统配置），用户不会迷失位置。

导航项合计 `1`(总览) + `3`(研究) + `2`(交易) + `1`(系统) = **7**，与 Task 3 Step 1 的 `expect(items).toHaveLength(7)` 和 Task 4 Step 1 的组标签断言一致。组标签仍是 3 个（`['研究', '交易', '系统']`）。

因 `.nav-group-label` 的首个组标签前面已有「总览」项，Task 2 Step 1 中**未使用** `:first-child` 降低内边距的写法（`总览` 才是 `.sidebar-nav` 的 first-child），改用统一的 `padding:14px 14px 5px`。

> **测试限制**：Task 3 Step 1 的 `RouterLink` stub 不会应用 `active-class`，所以测试**不覆盖** active 高亮态。active 样式在 Task 6 人工验收时确认。stub 会把 `class="nav-item"` 透传到 `<a>` 上（Vue 的 class 透传），故 `.sidebar-nav .nav-item` 选择器仍能匹配到 7 个。

- [ ] **Step 4: 运行测试**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/App.test.ts
```

Expected: `6 passed (6)`。

- [ ] **Step 5: 跑全量回归 + 类型检查**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```

Expected: `Test Files 14 passed (14)` / `Tests 205 passed (205)`（原 199 + 新 6），vue-tsc exit 0。

- [ ] **Step 6: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/App.vue web/src/__tests__/App.test.ts
git commit -m "feat(ui): 应用外壳改造 — SVG 图标导航分三组、顶栏加 Core 状态胶囊、主题切换迁至顶栏"
```

---

## Task 5: 图表画布底色与描边一致性

唯一触及 `Backtest.vue` 的 Task：只改一个字符串，把扇区分隔线的描边色从 `--card-bg` 改为新 token `--chart-bg`，使其与 Task 2 定义的画布底色严格一致（防止日后有人单独改 `--card-bg` 时静默破坏描边可见性）。

**Files:**
- Modify: `web/src/views/Backtest.vue`（`initCaPieChart` 内的 `itemStyle.borderColor`，用 `edit` 定点替换）
- Test: `web/src/__tests__/Backtest.test.ts`（追加 2 个用例）

**Interfaces:**
- Consumes: Task 1 的 `--chart-bg`、Task 2 的 `.chart-box` / `.ca-chart` / `.chart-container` 背景声明
- Produces: 无（终态）

- [ ] **Step 1: 追加失败测试**

在 `web/src/__tests__/Backtest.test.ts` 的**文件末尾**（第 2034 行 `})` 之后，即最后一个 describe 块之外）追加一个新 describe：

```ts
describe('Backtest.vue — 饼图描边色走 --chart-bg token', () => {
  it('扇区描边色取自 --chart-bg 而非 --card-bg', async () => {
    const { init } = await import('echarts')
    await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const pie = options.find((o: any) => o.series?.[0]?.type === 'pie')
    expect(pie).toBeTruthy()
    expect(pie.series[0].itemStyle.borderColor).toBe(CSS_VARS['--chart-bg'])
  })

  it('浅色 :root 中 --chart-bg 与 --card-bg 同值（描边在浅底上可见）', () => {
    const css = readFileSync(new URL('../style.css', import.meta.url), 'utf-8')
    const root = css.match(/:root\s*\{([\s\S]*?)\n\}/)![1]
    const pick = (n: string) => root.match(new RegExp(`${n}:\\s*([^;]+);`))![1].trim()
    expect(pick('--chart-bg')).toBe(pick('--card-bg'))
  })
})
```

这两个用例的 helper 复用现有上下文：`mountDetail`（第 1130 行）、`mockDetail`、`crisisAlphaCompleted`（第 772 行）、`CSS_VARS`（第 6 行，`vi.hoisted` 导出）全部在文件顶层可见。

> **注意**：不要在 describe 块外写 `mockDetail`/`crisisAlphaCompleted` 的引用——它们是模块级常量，在新 describe 的用例回调内可直接访问。

> 现有的「三：四档饼图中心数字与标签按饼心居中」用例（第 1619 行）已覆盖 `series.center` 锚点与 `textAlign`，本次改动不触碰那些属性，**无需重复加回归用例**。

并在 `web/src/__tests__/Backtest.test.ts` 顶部 import 区（第 3 行 `import { useTheme } from '../composables/useTheme'` 之前）追加：

```ts
import { readFileSync } from 'node:fs'
```

同时在 `CSS_VARS` mock 字典里追加一个键（加在 `'--overlay': 'rgba(11,11,11,.4)',` 之后）：

```ts
    '--chart-bg': '#123456',
```

**这个值必须是 `#123456` 而不是 `#ffffff`**：`cssVar()` 是从 mock 字典按 key 取值的，字典里已有的 `'--card-bg'` 是 `'#ffffff'`。只有让两个 key 取**不同值**，新用例才能鉴别「代码读的是 `--chart-bg` 还是 `--card-bg`」。若都设成 `#ffffff`，改前改后测试都通过，用例就废了。

- [ ] **Step 2: 运行验证失败**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/Backtest.test.ts
```

Expected: 第一个用例 **FAIL**，报错形如 `expected '#ffffff' to be '#123456'`（实际值来自 `--card-bg`，期望值来自 `--chart-bg`）。第二个用例 PASS（`style.css` 的 `:root` 已在 Task 1 定义 `--chart-bg`）。总计 `1 passed | 1 failed`（原 90 个用例全部仍 PASS）。

- [ ] **Step 3: 改 `Backtest.vue` 的描边色**

已核实：该字符串在 `Backtest.vue` 中**唯一出现**（第 593 行），用 `edit` 工具把它替换为：

```
      itemStyle: { borderColor: cssVar('--chart-bg'), borderWidth: 2 },
```

原行是 `      itemStyle: { borderColor: cssVar('--card-bg'), borderWidth: 2 },`。

- [ ] **Step 4: 运行测试**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run src/__tests__/Backtest.test.ts
```

Expected: `92 passed (92)`（原 90 + 新 2）。

- [ ] **Step 5: 全量回归 + 类型检查**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```

Expected: `Test Files 14 passed (14)` / `Tests 207 passed (207)`（原 199 + App 6 + 本 Task 2），vue-tsc exit 0。

- [ ] **Step 6: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "fix(ui): 饼图扇区描边改走 --chart-bg，与画布底色同源"
```

---

## Task 6: 双主题人工验收

无代码交付物，是 review gate。

**Files:** 无

**Interfaces:**
- Consumes: Task 1–5 全部产出
- Produces: 验收结论

- [ ] **Step 1: 启动 dev server**

```bash
cd D:\project\tq-iquant-web\web
npm run dev
```

- [ ] **Step 2: 浅色模式逐视图检查**

浏览器打开 Vite 输出地址，**浅色模式**下依次访问 7 个路由：`/dashboard` `/stock-pools` `/formulas` `/portfolios` `/backtest` `/live` `/system`。

每页确认：
- 侧边栏当前项有左侧 3px 高亮条 + accent 底色 + accent 图标
- 导航图标是线性 SVG，无 Emoji 字形
- 三组标签「研究/交易/系统」显示正常
- 顶栏右侧状态胶囊显示绿点 + 「Core 在线 v1.0 · 运行 xx」
- 表格行紧凑、数字列右对齐且纵向对齐
- 徽章为小圆角矩形
- 卡片有极轻的双层阴影
- 无「浅底浅字」不可读元素

- [ ] **Step 3: 暗色模式重复检查**

点顶栏主题按钮切到暗色，重复 Step 2 全部 7 个路由。

额外确认：状态点呼吸动画可见；饼图扇区分隔线在暗底上清晰可见（这是 Task 5 的验证点）；危机Alpha 评分图五档条块（红→金→绿）在暗底上均可辨。

- [ ] **Step 4: 键盘可达性**

Tab 键遍历侧边栏与顶栏按钮，确认 `:focus-visible` 焦点环可见（2px `--focus-ring` 轮廓）。

- [ ] **Step 5: 记录结论**

若发现视觉问题，**不要**在本 Task 内改代码——记录到问题清单，回到对应 Task 修并重跑其测试。

---

## Self-Review

**1. Spec 覆盖检查**

| Spec 章节 | 对应 Task |
|---|---|
| §2.2 新增 10 个 token | Task 1 Step 2/3 |
| §2.3 改值 token 表 | Task 1 Step 2/3 |
| §2.4 对比度要求 | Task 1 Step 3（选色已按 AA 校准）；Task 6 Step 2/3 人工验收 |
| §3.1 字号阶梯 | Task 2 Step 1/2/3/4（品牌 17/650、顶栏 15/600、th 11.5/600、table 13、badge 11.5、micro 11） |
| §3.2 密度 | Task 2 Step 1/2/3/4 |
| §3.3 数字等宽 | Task 2 Step 3（`.num` 类）、Task 4（`.topbar-path` 用 mono） |
| §4.1 侧边栏 | Task 2 Step 1/2、Task 4 Step 3 |
| §4.2 顶栏 | Task 2 Step 1、Task 4 Step 3 |
| §4.3 品牌区 | Task 2 Step 1、Task 4 Step 3 |
| §5.1–5.3 图标 | Task 3 |
| §5.4 path 坐标约束 | Task 3 Step 3（path 均在 2–22 内） |
| §6 图表底色 | Task 2 Step 6（`.chart-box` 等）+ Task 5（`borderColor`） |
| §7.2 验证方式 | 每个 Task Step 末 + Task 6 |
| §8 实施顺序 6 步 | Task 1–6 一一对应 |
| §9 不做的事 | 全部 Task 均未越界（无新依赖、无布局重排、无动画库、无移动端适配） |

**缺口**：Spec §3.1 提到「区块标题 h3 14px/600」与「KPI 数值 20px/600 mono」。这两处在各视图的 `<style scoped>` 里（`Backtest.vue` 有 `.section-title`），属 Global Constraints 禁止改动的范围。**已在 Task 1 保留现有值不动**，作为后续独立任务（设计系统原语层）的内容。需在 spec 中标注此为已知取舍。

**2. Placeholder 扫描**：无 TBD / TODO / "similar to Task N" / 无内容的代码步骤。所有代码块均含完整可粘贴内容。

**3. 类型一致性**：
- `ICONS` 在 Task 3 定义为 `Record<string, string>`，Task 4 用 `ICONS[item.icon]` 索引（`item.icon: string`）→ 类型匹配。
- `getSystemStatus()` 返回 `Promise<SystemStatus>`（`api/index.ts:1138`），Task 4 的 `status = ref<SystemStatus | null>(null)` 匹配。
- `SystemStatus` 结构为 `{ core: { online: boolean; version: string; uptime: string } }`，Task 4 模板读 `status.core.version` / `status.core.uptime` 匹配。
- `statusTimer: number | null` 对应 `window.setInterval` 返回 `number`，Task 4 的 `clearInterval` 有 null 检查 → 匹配。
- Task 4 导航项总数 `1`(总览 standalone) + `3`(研究) + `2`(交易) + `1`(系统) = **7**，与 Task 3 Step 1 的 `toHaveLength(7)` 一致；组标签数 **3**，与 Task 4 Step 1 的 `toEqual(['研究','交易','系统'])` 一致。
- `useTheme()` 返回 `{ theme, toggle }`（`composables/useTheme.ts:31`），Task 4 的解构写法匹配；`resetTheme()`（第 35 行）被测试的 `beforeEach` 调用以重置模块级单例 `_theme`。
- `ICONS` 的 10 个 key 与 Task 4 模板的引用点（`NAV_STANDALONE.icon` = `'dashboard'`、NAV_GROUPS 内的 `'pools'` / `'formula'` / `'portfolio'` / `'backtest'` / `'live'` / `'system'`、主题按钮的 `'sun'` / `'moon'`）逐一对应。`'search'` 是预留 key，未被 `App.vue` 使用——`KEYS` 数组包含它，故 Task 3 Step 4 的 `icons.ts` 用例计数为 2。

**4. 已核实的仓库事实**（写计划时逐一 grep 确认，非假设）：
- `style.css` 共 149 行；`:root` 在第 1–32 行，`[data-theme="dark"]` 在第 33–62 行，Task 1 的行号引用准确。
- Task 2 Step 1/2/3/4/5 替换的 6 组旧选择器原文与第 68–71、72–74、76–79、81–83、86、101 行**逐字一致**，`edit` 可直接命中。
- `itemStyle: { borderColor: cssVar('--card-bg'), borderWidth: 2 },` 在 `Backtest.vue` 中**唯一**（第 593 行）。
- `Backtest.test.ts` 共 2034 行；`CSS_VARS` 在第 6 行（`vi.hoisted` 内，已有 `'--card-bg': '#ffffff'`）、`crisisAlphaCompleted` 在第 772 行、`mountDetail` 在第 1130 行——均为模块级，Task 5 的新 describe 内可直接访问。
- `api/index.ts:1134-1141` 确有 `SystemStatus` 与 `getSystemStatus`，Task 4 的 import 路径 `./api` 正确。
- 现有 12 个测试文件均**不 mock `vue-router`**，故 Task 3 Step 1 必须新增该 mock，否则 `useRoute()` 返回 `undefined` 导致 mount 崩溃。
