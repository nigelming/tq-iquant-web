# 回测 UI 四项修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复回测进度 10000% bug、路径质量补第 6 指标、浅色热力图适配、主标题样式调整。

**Architecture:** 4 个独立小修复，每个 1-2 文件。后端 evaluator 加字段 + 前端消费；Dashboard 进度计算修正；热力图颜色适配；style.css 主标题样式。

**Tech Stack:** Python 3.13 / SQLAlchemy / Vue 3.5 / TypeScript / Vitest / pytest

## Global Constraints

- 禁止新增 npm 依赖
- `style.css` 保持 UTF-8 带 BOM + LF
- `Backtest.vue` / `Backtest.test.ts` 禁止全文重写，必须用 `edit` 定点替换
- 测试基线：`npx vitest run` = 14 files / 206 passed；`npx vue-tsc -b` = exit 0；`uv run pytest main/core/tests/ -q` = 819 passed
- 提交 message 用中文

---

## Task 1: 修复回测进度 10000% bug

**Files:**
- Modify: `web/src/views/Dashboard.vue:113`

**Interfaces:**
- Consumes: 后端 `progress` 字段（0-100 整数）
- Produces: 无

- [ ] **Step 1: 定位并修复**

把 `Dashboard.vue` 第 113 行：
```vue
<td>{{ r.progress != null ? Math.round(r.progress * 100) + '%' : '-' }}</td>
```
替换为：
```vue
<td>{{ r.progress != null ? r.progress + '%' : '-' }}</td>
```

- [ ] **Step 2: 验证**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```
Expected: 14 files / 206 passed, vue-tsc exit 0

- [ ] **Step 3: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/views/Dashboard.vue
git commit -m "fix: 回测进度显示 10000% → 100%（移除多余的 *100）"
```

---

## Task 2: 路径质量补第 6 指标（最长连盈天数）

**Files:**
- Modify: `main/core/engine/evaluator.py:524-581`（`_compute_path_metrics`）
- Modify: `web/src/views/Backtest.vue:1644-1649`（`buildMetrics` 路径质量组）
- Test: `main/core/tests/unit/test_evaluator.py`（追加断言）

**Interfaces:**
- Consumes: 无
- Produces: `max_consecutive_profit_days: int | None`（后端 evaluation dict 新字段）

- [ ] **Step 1: 后端加字段**

在 `evaluator.py` 的 `_compute_path_metrics` 方法中，现有循环追踪 `streak` / `best_streak`（连亏）。在同一循环内加连盈追踪：

在 `streak = best_streak = 0` 之后加：
```python
        win_streak = best_win_streak = 0
```

在 `for r in daily_returns:` 循环内，现有 `if r < 0:` 块之后加：
```python
            else:
                win_streak += 1
                if win_streak > best_win_streak:
                    best_win_streak = win_streak
```

在 return dict 中加：
```python
            "max_consecutive_profit_days": best_win_streak,
```

- [ ] **Step 2: 后端测试**

在 `test_evaluator.py` 中找到 path metrics 相关测试，追加断言验证 `max_consecutive_profit_days` 存在且类型正确。如果现有测试用 `assert result["max_consecutive_loss_days"] == 3` 模式，追加类似 `assert result["max_consecutive_profit_days"] == 2`。

- [ ] **Step 3: 前端加指标行**

在 `Backtest.vue` 的 `buildMetrics` 函数中，路径质量组（`_group('路径质量')` 之后）加一行：
```typescript
{ label: '最长连盈天数', value: num(m.max_consecutive_profit_days, 0), cls: 'text-orange' },
```

- [ ] **Step 4: 验证**

```bash
cd D:\project\tq-iquant-web
uv run pytest main/core/tests/unit/test_evaluator.py -q
cd web
npx vitest run
npx vue-tsc -b
```
Expected: pytest 全过, vitest 14 files / 206 passed, vue-tsc exit 0

- [ ] **Step 5: 提交**

```bash
cd D:\project\tq-iquant-web
git add main/core/engine/evaluator.py main/core/tests/unit/test_evaluator.py web/src/views/Backtest.vue
git commit -m "feat: 路径质量补第 6 指标 — 最长连盈天数"
```

---

## Task 3: 浅色热力图适配

**Files:**
- Modify: `web/src/views/Backtest.vue`（热力图 visualMap 和 label 配置）

**Interfaces:**
- Consumes: `cssVar('--ink-soft')`
- Produces: 无

- [ ] **Step 1: 定位热力图配置**

在 `Backtest.vue` 中找到月度热力图的 `visualMap` 配置（约第 2009-2017 行）和 `label` 配置（约第 2020-2027 行）。

- [ ] **Step 2: 给 visualMap.textStyle 加颜色**

把：
```typescript
textStyle: { fontSize: 11 },
```
替换为：
```typescript
textStyle: { fontSize: 11, color: cssVar('--ink-soft') },
```

- [ ] **Step 3: 给 label 加颜色**

把：
```typescript
label: {
  show: true,
  formatter: (params: any) => {
    const [ci, ri] = params.data
    return text[ri]?.[ci] || ''
  },
  fontSize: 10,
},
```
替换为：
```typescript
label: {
  show: true,
  formatter: (params: any) => {
    const [ci, ri] = params.data
    return text[ri]?.[ci] || ''
  },
  fontSize: 10,
  color: cssVar('--ink-soft'),
},
```

- [ ] **Step 4: 验证**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```
Expected: 14 files / 206 passed, vue-tsc exit 0

- [ ] **Step 5: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/views/Backtest.vue
git commit -m "fix: 浅色模式热力图文字颜色适配 --ink-soft"
```

---

## Task 4: 主标题样式调整

**Files:**
- Modify: `web/src/style.css`（`.sidebar-header h1`）

**Interfaces:**
- Consumes: `--accent` token
- Produces: 无

- [ ] **Step 1: 修改样式**

把 `style.css` 中：
```css
.sidebar-header h1{font-size:17px;font-weight:650;color:var(--ink);letter-spacing:.2px}
```
替换为：
```css
.sidebar-header h1{font-size:19px;font-weight:700;color:var(--accent);letter-spacing:.2px}
```

- [ ] **Step 2: 验证**

```bash
cd D:\project\tq-iquant-web\web
npx vitest run
npx vue-tsc -b
```
Expected: 14 files / 206 passed, vue-tsc exit 0

- [ ] **Step 3: 提交**

```bash
cd D:\project\tq-iquant-web
git add web/src/style.css
git commit -m "feat(ui): 主标题 19px/700/--accent，更醒目"
```

---

## Self-Review

**1. Spec coverage:**
- §1 进度 bug → Task 1 ✓
- §2 路径质量第 6 指标 → Task 2 ✓
- §3 热力图适配 → Task 3 ✓
- §4 主标题样式 → Task 4 ✓

**2. Placeholder scan:** 无 TBD/TODO。

**3. Type consistency:** `max_consecutive_profit_days` 后端返回 `int`，前端 `num()` 接收 `number | null`，类型匹配。
