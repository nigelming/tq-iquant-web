# 双主题（浅色/暗色）设计文档

## 目标

参照 `D:\暗盘资金` 项目的配色风格，为当前项目实现完整的浅色/暗色双主题切换。只改颜色风格，不动布局、组件逻辑、API、路由。

## 架构

### 核心机制

纯 CSS 变量 + `data-theme` 属性，与暗盘资金完全一致：

```css
:root {
  --bg: #f2efeb;           /* 浅色页面背景（暖纸感） */
  --card-bg: #fbf9f6;      /* 浅色卡片背景 */
  --ink: #0b0b0b;          /* 浅色主文字 */
  --ink-soft: rgba(11,11,11,.62);
  --ink-faint: rgba(11,11,11,.38);
  --line: rgba(11,11,11,.14);
  --red: #b3402a;          /* 涨/正向 */
  --green: #1e6b3a;        /* 跌/负向 */
  --gold: #8a6d1c;         /* 强正向高亮 */
  --blue: #2b5c8a;         /* 中性/信息 */
  --warn: #c05621;         /* 警告 */
}

[data-theme="dark"] {
  --bg: #101418;           /* 暗色页面背景（冷蓝灰） */
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
}
```

### 主题切换器

- 位置：侧边栏底部
- 实现：按钮切换 `<html>` 的 `data-theme` 属性
- 持久化：`localStorage` 键 `theme`
- 初始化：`main.ts` 启动时读 `localStorage`；无记录时用 `prefers-color-scheme`
- ECharts：切换后 `dispose` 所有图表实例，60ms 后重建（等 DOM 更新）

### ECharts 颜色读取

```typescript
function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
```

图表颜色统一通过 `cssVar('--red')` 等读取，不硬编码。主题切换时 dispose + 重建。

## 重构范围

约 180 处硬编码颜色，分 5 个阶段：

### Phase 1 — `style.css` 全局样式（30 处）

影响所有页面。`.badge-*`、`.btn-*`、`.modal-*`、表格、策略卡片等全部改为 `var(--xxx)`。新增暗盘资金的 `--red`/`--green`/`--gold`/`--blue`/`--warn` 变量。

### Phase 2 — `Backtest.vue` scoped style（50+ 处）

报告页面的 `.section-title`、`.summary-card`、`.stat-card`、`.metric-*`、`.trade-table`、`.decision-table`、`.ca-*`（Crisis Alpha）等。

`#667eea` 紫色（约 15 处）映射到 `--blue`（`#2b5c8a`/`#5a8fc0`）。

### Phase 3 — `Backtest.vue` ECharts（60+ 处）

- 语义色（策略红 `#d6403a`、基准蓝 `#4a7ec4`、涨红跌绿）→ 映射到 `--red`/`--green`/`--blue`
- 装饰色（热力图 visualMap、散点图分类色、渐变等）→ 新增 `--chart-*` 变量
- ECharts 颜色统一通过 `cssVar()` 读取

### Phase 4 — 其他视图内联样式（42 处）

`Dashboard.vue`、`Formulas.vue`、`StockPools.vue`、`SystemConfig.vue`、`Portfolios.vue` 中的 `style="color:#888"` 等，机械替换为 `var(--ink-soft)` 等。

### Phase 5 — `LiveSessions.vue`（20 处）

内联 + scoped style，同 Phase 4 方式处理。

## 测试策略

### 前端测试（vitest）

- 新增 `web/src/__tests__/theme.test.ts`：验证 `data-theme` 切换、localStorage 持久化、CSS 变量值正确
- 更新 `Backtest.test.ts`：当前有 6 处断言硬编码颜色值（如 `#d6403a`），改为验证 CSS 变量或移除颜色断言
- ECharts 主题切换：验证 dispose + 重建后图表颜色读取 CSS 变量

### 边界情况

- **系统偏好**：首次访问无 localStorage 时，用 `prefers-color-scheme` 决定默认主题
- **ECharts 重建闪烁**：60ms `setTimeout` 等 DOM 更新完再重建
- **打印/PDF**：`@media print` 强制浅色
- **暗色下可读性**：`--ink-soft` 用 `rgba(236,231,223,.62)`，需确认与 `--card-bg` 对比度足够

## 不改动

- 后端 Python 代码零改动
- API 接口零改动
- 路由/导航结构零改动
- 组件逻辑零改动（只改 style 和 ECharts option 颜色来源）

## 风险点

- `Backtest.vue` 有 2000+ 行，scoped style 500+ 行，重构时需仔细不破坏布局
- ECharts 颜色读取 CSS 变量后，`visualMap` 的 `inRange.color` 数组格式需确认兼容性
- A 股红涨绿跌约定（`#ef4444` 涨、`#10b981` 跌）在 ECharts 中广泛使用，需确保暗色下语义正确
