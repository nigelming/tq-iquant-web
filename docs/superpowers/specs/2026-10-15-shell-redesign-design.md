# 创懿量化 · 应用外壳重构设计文档

日期：2026-10-15
状态：已与用户确认方向（品牌 B / 侧栏 S3+S2 / 可视化区 V2），待写入实现计划

## 1. 背景与目标

前端外壳（首页总览、左侧功能区、右侧可视化区）观感偏简陋，且总览页缺少「创懿量化」品牌体现；浏览器标签标题仍为占位符 `web`。

本次为**稳健增强外壳**（scope A）：保留全部路由、视图内部逻辑、API 与数据流不变，仅重构外壳与内容容器。用户已通过可视化对照选定：

- 品牌落位：**方案 B** — 侧栏保留克制品牌标识，总览页做品牌 Hero 带
- 左侧功能区：**S3 宽度（≈210px）+ S2 可折叠（≈52px 图标 rail）** 组合，底部状态栏
- 右侧可视化区：**方案 V2** — 页面头部带 + 统一区块框 + 图表卡统一头部

参考视觉：`D:\project\暗盘盘中版.html`（暖色编辑风、断字双色大标题、字距副标、衬线数字），延续既有双主题设计（`2026-09-28-dual-theme-design.md`）。

### 目标

- 总览页新增品牌 Hero 带（断字双色大标题 + 英文副标 + 关键统计 + 刷新）
- 左侧栏增强为「宽侧栏（分组/图标/激活竖条/底栏）⊕ 一键折叠图标 rail」，折叠态持久化
- 可视化区统一为「页面头部带（面包屑 + 标题 + 操作）+ 编号区块框 + 图表卡头部」
- 浏览器标题 `web` → `创懿量化回测平台`，并随路由动态更新

### 非目标

- 不改动任何后端 API、数据流、引擎、数据库
- 不重写各视图内部业务逻辑（仅在容器层套用外壳/区块框），`Backtest.vue` 的巨型分析区块**只做容器统一，不触碰计算**
- 不新增业务功能、不改路由结构、不改导航项集合

## 2. 兼容契约（硬约束）

以下来自现有前端测试，重构后必须保持（否则视为回归）：

| 约束 | 出处 |
|---|---|
| `.sidebar-nav .nav-item` 恰好 **8** 个，每个含一个 `svg.icon`（1 个 `path`） | `App.test.ts` |
| `.nav-group-label` 恰好 **3** 个且为 `研究/交易/系统` | `App.test.ts` |
| 主题切换仅在 `.topbar-right .theme-toggle`；`.sidebar-footer` **不存在** | `App.test.ts` |
| `.status-dot`（`online`）、`.status-chip` 含 `Core 在线`/版本/uptime | `App.test.ts` |
| `.topbar-path` 显示 `route.path`（如 `/dashboard`） | `App.test.ts` |
| `ICONS` 恰好 **10** 个键，值合法 path，无 Emoji | `App.test.ts` |
| Dashboard 文案含：股票池 / 回测记录 / Core 状态 / 在线 / `回测A` / `D:\new_tdx64` / `http://127.0.0.1:8790` / `部分数据加载失败` | `Dashboard.test.ts` |

**推论**：
- 侧栏底栏类名用 `.sidebar-footbar`（**不叫** `sidebar-footer`），且**不得**放入主题开关；主题开关继续留在顶栏。
- 折叠箭头等新图形用**内联 SVG**，不加入 `ICONS`，避免改变 `ICONS` 键集合。
- 视图套区块框时**保留**其既有表格/图表 class（`table-wrap`、`ca-*`、`mr-*` 等），不改变选择器。

## 3. 架构

### 3.1 外壳布局（`App.vue`）

```
.app-layout
├─ aside.sidebar[data-collapsed]
│   ├─ .sidebar-header     品牌：创懿(ink)+量化(red) · 折叠按钮(内联 svg)
│   ├─ nav.sidebar-nav     总览 + 三组；图标+标签；激活竖条
│   └─ .sidebar-footbar    Core 状态点 + 版本（折叠态仅显示点）
└─ .main-container
    ├─ header.topbar       .topbar-crumb 面包屑 + .topbar-title + 右侧(.status-chip + .theme-toggle)
    └─ main.content        <router-view/>
```

### 3.2 侧边栏（宽 + 折叠）

- 宽度：展开 `--sidebar-w:210px`，折叠 `--rail-w:52px`；`transition: width .2s ease`
- 折叠状态：`collapsed: Ref<boolean>`，持久化 `localStorage['sidebar:collapsed']`（`'1'`/`'0'`）
- 折叠态：`.sidebar` 加 `data-collapsed`；品牌仅显示「创」；`nav-item` 仅显示图标，`title` 属性提供 tooltip；分组标签隐藏（以细线分隔）；`.sidebar-footbar` 仅显示状态点
- 折叠按钮位于 `.sidebar-header` 右侧；展开/收起图标用内联 SVG（`«`/`»` 形态）
- 导航数据结构沿用 `NAV_STANDALONE` + `NAV_GROUPS`（不改），确保 8 项与 3 组标签不变

### 3.3 面包屑

- 新增计算属性：由 `route.name` 反查 `NAV_GROUPS`/`NAV_STANDALONE`，得到「组 / 项」路径
  - 例：`/portfolios` → `研究 / 组合策略`；`/dashboard` → `总览`
- 渲染于 `.topbar-crumb`（等宽/小字号、`--ink-soft`），`.topbar-title` 保留当前页名
- `.topbar-path` 保留显示 `route.path`（测试要求）

### 3.4 总览页品牌 Hero（`Dashboard.vue`）

- 新增 `.dash-hero`（位于内容顶部）：
  - 断字双色大标题：`创懿`（`--ink`，渐变裁切）+ `量化`（`--red`，`text-shadow` 微光）
  - 英文副标：`CHUANGYI QUANT · 回测平台`，`letter-spacing:.3em`，两侧渐隐细线（`::before/::after`）
  - 关键统计行：股票池 / 公式 / 组合 / 回测 / 实盘（衬线数字 `--font-serif`，实盘用 `--red` 强调）
  - 右侧「刷新」按钮：复用现有 `load()`
- 数据来源不变：现有 7 个 getter + `Promise.allSettled`；失败文案 `部分数据加载失败` 保留
- 下方 KPI 卡（保留 `股票池/公式/组合策略/回测记录/实盘会话/Core 状态` 文案）+ 最近回测表 + 系统配置，统一包裹为 V2 区块框

### 3.5 可视化区统一（V2）

新增可复用组件 `web/src/components/PageHeader.vue`：
- props：`title: string`、`crumbs?: string[]`
- 默认 slot 放右侧操作按钮
- 渲染 `.page-head`：面包屑 + 大标题 + 右侧 actions

新增全局样式类（`style.css`）：
- `.page-head` / `.page-head-crumb` / `.page-head-title` / `.page-head-actions`
- `.section-frame`（`.section-head`：`.section-num` 衬线编号 + 标题 + 细线 + 右上 slot；`.section-body`）
- `.chart-card`（统一头部：标题 + caption + toolbar slot）

落地策略（增量、保护测试）：
- **优先**：`Dashboard.vue` 全量套用（Hero + 区块框）；`App.vue` 外壳
- **增量**：`StockPools` / `Formulas` / `Portfolios` / `Backtest` / `LiveSessions` / `SystemConfig` 在内容顶部加 `PageHeader`，内容块按需包裹 `section-frame`；**不改**其内部表格/图表 class 与脚本逻辑
- **禁改**：`.ca-banner`、`.mr-section`、`metric-*` 等既有专用样式标签保持原样

### 3.6 设计令牌与样式

- 复用现有暖色双主题 tokens（`--bg/--card-bg/--ink/--red/--green/--font-serif/--radius` 等）
- 新增：`--sidebar-w:210px`、`--rail-w:52px`；Hero 渐变/字距相关局部样式内联于组件 scoped
- `.btn` / `.badge-*` / `table` 等既有全局类语义不变

### 3.7 品牌资产与标题

- `web/index.html`：`<title>创懿量化回测平台</title>`，`lang="zh-CN"`
- 路由切换时设置 `document.title = '创懿量化回测平台 · ' + 当前标题`（在 `App.vue` `watch(route)` 中）
- favicon 沿用 `web/public/favicon.svg`；Hero 默认纯排版（不强制使用 `assets/hero.png`）

## 4. 组件与文件清单

| 文件 | 改动 |
|---|---|
| `web/src/App.vue` | 外壳重构：侧栏折叠 + 底栏 + 面包屑 + 动态标题 |
| `web/src/views/Dashboard.vue` | 品牌 Hero + 区块框包裹 |
| `web/src/components/PageHeader.vue` | **新增**（页面头部带组件） |
| `web/src/style.css` | 新增 `.page-head*` / `.section-frame*` / `.chart-card` / 侧栏宽度与折叠样式 |
| `web/index.html` | 标题 + lang |
| `web/package.json` | （可选）`name` 保持 `web`（构建产物名不强制改，避免影响脚本） |
| 其余 `views/*.vue` | 增量套用 `PageHeader`（不改内部逻辑） |
| `web/src/__tests__/App.test.ts` | 新增折叠/面包屑用例（不删既有断言） |
| `web/src/__tests__/Dashboard.test.ts` | 新增 Hero 渲染用例（不删既有断言） |

## 5. 测试策略

- **必须保持全绿**：`App.test.ts`、`Dashboard.test.ts`（第 2 节全部断言）
- **新增**：
  - App：点击折叠按钮后 `.sidebar` 出现 `data-collapsed`，且 `localStorage['sidebar:collapsed']==='1'`；再点恢复
  - App：面包屑随 `route` 变化（mock route）正确显示「研究 / 组合策略」
  - Dashboard：Hero 渲染 `创懿`/`量化`/`CHUANGYI QUANT` 文案
- **构建**：`npx vitest` 全绿 + `npm run build`（vite）成功
- **后端不动**：不运行后端测试
- **手工**：浅/暗双主题下 Hero、侧栏折叠记忆、各视图头部带与区块框观感

## 6. 风险点

- `App.vue` 外壳选择器被测试强依赖：改类名前先核对第 2 节契约
- `Dashboard.vue` 文案被测试断言：Hero 新增文案不得替换既有 KPI 文案
- `Backtest.vue`（210KB）体量大：只做容器层包裹，避免触碰 script 逻辑与既有 class
- 折叠态与 scoped 样式：`.sidebar-nav .nav-item` 在折叠态仍须存在且各含 1 个 path（保持 DOM 结构，仅 CSS 隐藏文字）
- `document.title` 动态化需在路由切换与首次挂载均生效

## 7. 分阶段实施（建议）

1. 设计令牌 + 全局类（`style.css`，纯新增，零回归）
2. `App.vue` 外壳（侧栏折叠/底栏/面包屑/标题）→ 跑 `App.test.ts`
3. `Dashboard.vue` 品牌 Hero + 区块框 → 跑 `Dashboard.test.ts`
4. 新增 `PageHeader.vue` + 各视图增量套用 → 跑全量 `vitest` + `build`
5. 手工双主题验收

## 8. 不改动

- 后端 Python 全部代码、API、数据库、引擎零改动
- 路由结构与导航项集合零改动
- 各视图内部脚本逻辑（数据获取、计算、事件）零改动
- `ICONS` 键集合（仍 10 个）零改动
