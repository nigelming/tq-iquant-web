# 回测 UI 四项修复 — 设计文档

**日期**：2026-09-29
**范围**：Dashboard.vue 进度 bug、Backtest.vue 路径质量指标 + 热力图 + 主标题样式

---

## 1. Bug：回测进度显示 10000%（应为 100%）

**根因**：`web/src/views/Dashboard.vue:113` 执行 `Math.round(r.progress * 100) + '%'`，但后端 `main/core/services/backtest_service.py` 已将 `progress` 赋值为 0-100 的整数（完成时 `rec.progress = 100`）。前端再乘 100 导致 100 → 10000。

**修复**：移除 `* 100`，改为 `r.progress != null ? r.progress + '%' : '-'`。

**影响文件**：`web/src/views/Dashboard.vue`

---

## 2. 路径质量指标补到 6 个：最长连盈天数

**现状**：`_group('路径质量')` 下 5 个指标：最长连亏天数、最差日收益、最差周收益、收益偏度、收益峰度。

**补充**：`最长连盈天数`（`max_consecutive_profit_days`），与「最长连亏天数」对称。

**后端改动**：`main/core/engine/evaluator.py:_compute_path_metrics` 的循环内追踪连续盈利天数，返回新字段。

**前端改动**：`web/src/views/Backtest.vue:buildMetrics` 的路径质量组加一行：
```typescript
{ label: '最长连盈天数', value: num(m.max_consecutive_profit_days, 0), cls: 'text-orange' },
```

**影响文件**：`main/core/engine/evaluator.py`、`web/src/views/Backtest.vue`

---

## 3. 浅色模式下月度热力图不适配

**根因**：`Backtest.vue` 中热力图的 `visualMap.textStyle` 和 `label` 配置没有显式设置 `color`，浅色模式下文字颜色与背景对比度不足。

**修复**：
- `visualMap.textStyle` 加 `color: cssVar('--ink-soft')`
- `label` 加 `color: cssVar('--ink-soft')`

**影响文件**：`web/src/views/Backtest.vue`

---

## 4. 主标题样式调整

**现状**：`.sidebar-header h1` = 17px / 650 / `--ink` / 0.2px 字距。

**目标**：更大更醒目 — 19px / 700 / `--accent`（靛蓝 #2f4a7a）/ 0.2px 字距。副标题 `.sidebar-subtitle` 保持不变。

**影响文件**：`web/src/style.css`

---

## 验证

- `npx vitest run` 全量通过
- `npx vue-tsc -b` exit 0
- `uv run pytest main/core/tests/ -q` 全量通过（后端 evaluator 改动需回归）
