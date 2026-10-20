# 专业交易终端视觉重构 — 设计文档

日期：2026-09-29
范围：`web/src/style.css`（设计 token + 全局基元）、`web/src/App.vue`（应用外壳）、`web/src/icons.ts`（新增，SVG 图标）

---

## 1. 目标与非目标

### 目标

把创懿量化的整体外观从当前「暖米色工具页」升级为「专业量化交易终端」，具体三点：

1. **冷调专业色板** — 浅色模式从暖米色（`#f2efeb` 底 / `#fbf9f6` 卡）改为冷灰专业色，与深色模式构成同一套系统的明暗两面。
2. **克制的密度** — 直角、紧凑行高、明确字号阶梯、等宽数字，强化终端感。
3. **专业图标** — 侧边栏与主题切换的 Emoji 全部替换为手写内联 SVG。

改造必须与已完成的浅色/深色双模式**完全匹配**：所有颜色继续走 CSS 变量，切换 `data-theme` 即刻生效，不允许出现硬编码色值或只在单一模式下可读的组合。

### 非目标

- 不改动 7 个业务视图的页面布局与信息架构（`Dashboard.vue` / `StockPools.vue` / `Formulas.vue` / `Portfolios.vue` / `Backtest.vue` / `LiveSessions.vue` / `SystemConfig.vue`）。
- 不改动 `Backtest.vue` 的 22 张 ECharts 图配置。
- 不引入任何 npm 依赖（`package.json` 保持 vue/router/echarts/axios 四项）。
- 不做设计系统原语层（`.panel` / `.stat` / `.chip` 通用类）—— 那是后续独立任务。

---

## 2. 设计 token

### 2.1 命名与结构

保留现有全部 token 名（`--bg` / `--card-bg` / `--ink` / `--red` / `--green` / `--accent` / `--score-1..5` / `--*-wash` / `--shadow-*` / `--overlay` …），**只改值、新增少量新 token**。

这样做的关键收益：上一轮刚完成的 22 张图主题化、4 个 `reactive` 颜色常量、徽章 wash token 全部无需改动即可继承新色板。若重命名 token 会波及 `Backtest.vue` 的 `cssVar('--xxx')` 调用点，收益远小于风险。

### 2.2 新增 token

| Token | 浅色 | 暗色 | 用途 |
|---|---|---|---|
| `--font-mono` | `ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Liberation Mono", monospace` | 同左 | 所有数字、等宽码、日期时间 |
| `--chart-bg` | `#ffffff` | `#161b22` | 图表画布真实底色（`.chart-box` / `.ca-chart`） |
| `--shell-bg` | `#f7f9fb` | `#0b0f14` | 侧边栏与顶栏底色，比 `--bg` 略深/略深一层，与内容区拉开层次 |
| `--nav-active-bar` | `#2f4a7a` | `#5a8fc0` | 侧边栏当前项左侧 3px 高亮条 |
| `--focus-ring` | `rgba(47,74,122,.35)` | `rgba(90,143,192,.45)` | 键盘焦点环 |
| `--shadow-card` | `0 1px 2px rgba(15,23,42,.06), 0 1px 1px rgba(15,23,42,.04)` | `0 1px 3px rgba(0,0,0,.40)` | 卡片阴影（双层，极轻） |
| `--shadow-pop` | `0 8px 24px rgba(15,23,42,.12)` | `0 8px 24px rgba(0,0,0,.55)` | 弹窗/悬浮层 |
| `--ticker-up` | `#c62828` | `#f2564d` | A 股「涨」主色，语义等同 `--red` 但更饱和 |
| `--ticker-down` | `#1b7f4b` | `#3fb27f` | A 股「跌」主色，语义等同 `--green` |

`--ticker-up/down` 与 `--red/green` 并存：`--red/green` 已被大量语义点复用（评分五档、分类色、正负着色），不改动；`--ticker-up/down` 供**新增**的终端感元素（状态点、涨跌闪烁、涨跌柱）使用，避免污染既有语义。

### 2.3 改值 token

| Token | 现值（浅） | 新值（浅） | 现值（暗） | 新值（暗） |
|---|---|---|---|---|
| `--bg` | `#f2efeb` | `#eef1f4` | `#101418` | `#0d1117` |
| `--card-bg` | `#fbf9f6` | `#ffffff` | `#161c22` | `#161b22` |
| `--ink` | `#0b0b0b` | `#0f172a` | `#ece7df` | `#e6edf3` |
| `--ink-soft` | `rgba(11,11,11,.62)` | `rgba(15,23,42,.66)` | `rgba(236,231,223,.62)` | `rgba(230,237,243,.66)` |
| `--ink-faint` | `rgba(11,11,11,.38)` | `rgba(15,23,42,.42)` | `rgba(236,231,223,.38)` | `rgba(230,237,243,.42)` |
| `--line` | `rgba(11,11,11,.14)` | `rgba(15,23,42,.11)` | `rgba(236,231,223,.16)` | `rgba(230,237,243,.14)` |
| `--border` | 同 `--line` | 同 `--line` | 同 `--line` | 同 `--line` |
| `--red` | `#b3402a` | `#c62828` | `#e06550` | `#f2564d` |
| `--green` | `#1e6b3a` | `#1b7f4b` | `#3fa06b` | `#3fb27f` |
| `--gold` | `#8a6d1c` | `#a16207` | `#c9a24a` | `#d4a94a` |
| `--blue` | `#2b5c8a` | `#2f4a7a` | `#5a8fc0` | `#6ba3d0` |
| `--warn` | `#c05621` | `#b45309` | `#e29b4f` | `#e29b4f` |
| `--accent` | `#2b5c8a` | `#2f4a7a` | `#5a8fc0` | `#6ba3d0` |
| `--accent-light` | `rgba(43,92,138,.10)` | `rgba(47,74,122,.09)` | `rgba(90,143,192,.12)` | `rgba(107,163,208,.14)` |
| `--radius` | `6px` | `4px` | 同（不区分） | 同 |
| `--hover-bg` | `rgba(11,11,11,.04)` | `rgba(15,23,42,.045)` | `rgba(236,231,223,.06)` | `rgba(230,237,243,.07)` |

`--score-1..5`（红→金→绿五阶）与 `--*-wash`、`--overlay`、`--shadow-sm/md` 保持现值不动 —— 这批是上一轮刚按红金绿五阶调校并有测试锁定的，色板基底转冷后它们仍然成立（`#c0392b` 等本来就是中性偏暖的纯色，不与冷调冲突）。

### 2.4 对比度要求

新色板下所有「文字 on 底色」组合必须满足 WCAG AA：

- `--ink` on `--bg`：≥ 12:1（实际约 15:1）
- `--ink-soft` on `--card-bg`：≥ 4.5:1
- `--ink-faint` on `--card-bg`：≥ 3:1（仅用于次要装饰，如 `#id` 列）
- `--ticker-up` on `--card-bg` / `--ticker-down` on `--card-bg`：≥ 4.5:1
- 徽章 `color` on `*-wash` 底：≥ 4.5:1（沿用上一轮已达标的组合）

---

## 3. 排版与密度

### 3.1 字号阶梯

| 用途 | 字号 | 字重 | 字族 |
|---|---|---|---|
| 品牌名 | 17px | 650 | sans |
| 顶栏页面标题 | 15px | 600 | sans |
| 区块标题（h3） | 14px | 600 | sans |
| 卡片数值 / KPI | 20px | 600 | mono |
| 表格正文 | 13px | 400 | sans |
| 表格数字列 | 13px | 400 | **mono** |
| 标签 / 轴文字 | 11.5px | 500 | sans |
| 微标签（`#id`、副说明） | 11px | 400 | sans |

### 3.2 密度

- 侧边栏 nav 项：`padding: 9px 14px`，`margin-bottom: 1px`，字号 13.5px，字重 500
- 表格行：`padding: 8px 14px`（现 10px）
- 卡片内边距：16px（现 12–14px 不统一）
- `.content` 内边距：20px 保持
- 徽章：`padding: 2px 8px`，字号 11.5px，圆角 3px（现 10px 全圆）

### 3.3 数字

全局规则：`table td` 内的数字列、`.metric-value`、日期、百分比一律加 `font-variant-numeric: tabular-nums` + `font-family: var(--font-mono)`，保证纵向对齐。这是终端感最直接的来源。

实现方式：在 `style.css` 新增 `.num` 工具类与 `tbody td.num` 选择器；给需要等宽的列加 `class="num"`（涉及的视图仅 `Dashboard.vue` 的进度列与日期列，**属超范围外，故本次只在全局基元层提供能力，不改视图**）。

---

## 4. 应用外壳（`App.vue`）

### 4.1 侧边栏

```
┌────────────────────┐
│ 创懿量化            │  ← 品牌区，17px/650，副标题「量化交易平台」11px --ink-faint
│ 量化交易平台        │
├────────────────────┤
│ ▤ 总览              │  ← 当前项：--accent-light 底 + 左侧 3px 高亮条 + 图标染色
│ ▤ 股票池            │
│ ▤ 公式管理          │  ← 分组：研究 / 交易 / 系统
│ ▤ 组合策略          │
│ ▤ 回测管理          │
│ ▤ 实盘交易          │
│ ▤ 系统配置          │
├────────────────────┤
│ ☾ 暗色              │  ← 主题切换，带图标
└────────────────────┘
```

- 宽度 200px → 208px
- 导航分三组（研究：股票池/公式/组合策略；交易：回测管理/实盘交易；系统：系统配置），组间 1px `--line` 分隔 + 8px 上下留白，组标签 11px `--ink-faint` 大写字距 `.6px`
- 当前项：`.nav-item.active` 增加 `box-shadow: inset 3px 0 0 var(--nav-active-bar)`，`--accent` 文字与图标，`--accent-light` 底
- hover：仅底色变化，不改文字色（避免与 active 混淆）
- 底部主题切换保持吸底，加 `border-top: 1px solid var(--line)`

### 4.2 顶栏

- 高度 52px → 48px，`--shell-bg` 底
- 左：页面标题（15px/600），下方 11px `--ink-faint` 显示路由路径（如 `/backtest`）
- 右：**Core 状态胶囊** — 6px 状态点 + 「Core 在线」文字，在线 `--ticker-down` 绿点带 2s 呼吸动画，离线 `--ticker-up` 红点常亮；胶囊 `1px solid var(--border)` + `--radius` + 11.5px 文字
  - 数据来源：`getSystemStatus()`，在 `App.vue` 中 `onMounted` 拉取一次，并以 30s 轮询刷新
  - 失败时显示「Core 未知」，灰点，无错误弹窗（顶栏是常驻元素，不能因后端抖动而打扰）
- 主题切换按钮从侧边栏底部移到顶栏右侧（终端惯例：全局设置在顶栏），侧边栏底部改为纯留白

### 4.3 品牌区

- 「创懿量化」17px/650 `--ink`，下方 11px `--ink-faint`「量化交易平台」
- 底部 1px `--line`

---

## 5. 图标系统（新增 `web/src/icons.ts`）

### 5.1 形态

导出 `const ICONS: Record<string, string>`，值为单色 SVG **path 的 `d` 属性字符串**（不含 `<svg>` 包裹）。渲染时统一用一个组件化写法：

```vue
<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path :d="ICONS.dashboard" />
</svg>
```

`currentColor` 使图标自动继承文字色，因此 active/hover 状态无需额外 CSS。`aria-hidden` 避免读屏重复朗读（导航项本身已有文字）。

### 5.2 图标清单（10 个）

| key | 用途 | 描边几何（24×24 网格） |
|---|---|---|
| `dashboard` | 总览 | 2×2 网格：外框矩形 + 中心十字分割 |
| `pools` | 股票池 | 三条不等长水平线 + 左侧竖线（列表语义） |
| `formula` | 公式管理 | `f(x)` 曲线：一条上升曲线 + 两点 |
| `portfolio` | 组合策略 | 两个重叠矩形（错位） |
| `backtest` | 回测管理 | 折线图：坐标轴 + 折线 + 端点 |
| `live` | 实盘交易 | 闪电填充路径 |
| `system` | 系统配置 | 齿轮外圈 + 中心圆（简化 8 齿为 4 齿 + 4 短齿） |
| `sun` | 浅色模式 | 中心圆 + 8 条放射短线 |
| `moon` | 暗色模式 | 新月路径 |
| `search` | 预留（顶栏后续用） | 放大镜圆 + 手柄斜线 |

全部手写、不引第三方图标库。每个 path 控制在 3–6 个子路径以内，保证 20px 渲染清晰。

### 5.3 尺寸

- 导航图标 17px，`vertical-align: -3px`
- 品牌区无图标
- 状态点 6px（纯 CSS 圆点，非 SVG）
- 图标颜色：默认 `--ink-soft`，active `--accent`

### 5.4 path 坐标约束

`viewBox="0 0 24 24"`，所有 path 的绘制范围限制在 `2 ~ 22` 内（留 2 单位呼吸），避免描边被裁切。圆角由 `stroke-linecap="round"` + `stroke-linejoin="round"` 提供。

---

## 6. 图表底色修正

上一轮审计发现：`Backtest.vue` 的饼图 `itemStyle.borderColor` 用 `cssVar('--card-bg')` 描边分隔扇区，但画布真实底色是 `.chart-box { background: var(--hover-bg) }`（浅色约 `#e8e5e0`、暗色约 `#1a2027`），两者不一致导致暗色下描边几乎不可见。

本次新增 `--chart-bg` token（浅 `#ffffff` / 暗 `#161b22`），并把 `.chart-box` / `.ca-chart` / `.chart-container` 的背景改为 `var(--chart-bg)`，同时**不改** `Backtest.vue` 的 `borderColor` 引用 —— 它读的 `--card-bg` 在新色板下（浅 `#ffffff`）恰好与 `--chart-bg` 一致，语义上「扇区间分隔线用画布同色」正确。

> 说明：这是唯一一处会触及 `Backtest.vue` 的改动（`borderColor` 保持原样即无需改），其余全部在 `style.css` / `App.vue` / `icons.ts` 三个文件内。

---

## 7. 兼容性与回归风险

### 7.1 已识别的破坏点

| 风险 | 说明 | 缓解 |
|---|---|---|
| 测试断言硬编码颜色 | `Backtest.test.ts` 的 `CSS_VARS` mock 内含 `'--red': '#d6403a'` 等字面值，与 `style.css` 真实值不一致（mock 自洽即可，不读真实 CSS） | 不受影响：测试只验证「用了哪个 token 名」，不验证真实色值。但**四档配色测试**断言 `html` 含 `CSS_VARS['--red']` 字面串，mock 返回的仍是 `#d6403a`，仍通过 |
| `--radius` 6px → 4px | 若有测试断言具体圆角 | 已 grep `radius\|border-radius`：12 个测试文件中**零命中** |
| `App.vue` 结构变更 | 导航项、主题按钮位置变动 | 已 grep：12 个测试文件**无任何** `App.vue` / `sidebar` / `nav-item` / `topbar` 断言 |
| Emoji 被替换 | 若有测试断言导航文本含 Emoji | 已 grep：无任何测试引用 `🏠 📋 📐 📦 📊 ⚡ ⚙ ☀ ☾ 创懿` |
| `getSystemStatus` 引入顶栏 | `App.vue` 新增 API 调用 | 已确认 12 个测试文件**均不挂载 `App.vue`**（只 import `views/*`），故新增调用不会被触发；`Dashboard.test.ts` 里的 `getSystemStatus` mock 属于 Dashboard 自身，与顶栏无关。即便将来挂载，`onMounted` 内 try/catch 兜底 |
| 冷调色板下五档评分色 | `--score-1..5` 未随基底调整 | 这五色为独立纯色，不依赖基底，视觉成立；上一轮有测试锁定映射关系，不受影响 |

### 7.2 验证方式

```
npx vitest run          # 前端全量，基线 199 passed
npx vue-tsc -b          # 类型检查，基线 exit 0
```

外加人工检查：浅色/深色各看一遍 7 个视图，确认无「浅色下不可读」「暗色下不可见」的元素。

---

## 8. 实施顺序

1. `style.css`：改 token 值 + 新增 token（不改任何选择器结构）→ 跑测试确认零破坏
2. `style.css`：改基元选择器（排版、密度、`.icon`、状态点、`.chart-box` 底色）
3. 新建 `icons.ts`：10 个 path
4. `App.vue`：换 SVG 图标、分组导航、顶栏状态胶囊、主题切换迁移
5. 人工过一遍双主题 × 7 视图
6. 全量 `vitest` + `vue-tsc -b`

每步独立可验证，第 1 步是纯色值替换，若出问题可单独回滚。

---

## 9. 明确不做的事

- 不装任何 UI 框架（Vuetify / Naive UI / Element Plus）
- 不加 Tailwind
- 不做 7 个视图的布局重排
- 不加动画库；仅用 CSS `transition`（150ms 现有节奏）与状态点呼吸
- 不做移动端适配（本地单用户工具，桌面 1280px+ 场景）
