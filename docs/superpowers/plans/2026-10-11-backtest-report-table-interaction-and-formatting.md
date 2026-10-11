# 报告页表格交互层 + 数值精度策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给回测报告页所有数据驱动的长表加上统一的排序/筛选/分页交互，并统一全站数值精度与取舍（显示 + 文案层）。

**Architecture:** Part A 新增后端 `format.py` 与前端 `format.ts` 两个纯函数模块并收敛数值渲染；Part B 新增前端 `useTable` 组合式函数 + `TableToolbar`/`TablePager` 两个展示组件，按表增量接入（保留现有 `<table>` 标记）。两部分互不依赖，可分别提交。

**Tech Stack:** Python 3.13 / pytest（后端，`main/`）；Vue 3 + TypeScript + Vitest + happy-dom（前端，`web/`）。

## Global Constraints

- 单用户系统，无鉴权。
- 不改 API 数值字段精度（`_f()` 保持原始精度）、不改 DB schema/迁移。
- 后端测试：`uv run pytest core/tests/`（在 `main/` 目录）；前端测试：`npx vitest run`（在 `web/` 目录）。
- 前端类型检查：`npx vue-tsc -b`（在 `web/` 目录）。
- 设计 token：颜色用 `var(--ink)` / `var(--ink-soft)` / `var(--accent)` 等，禁止硬编码色值。
- 时间字符串判断按 Asia/Shanghai；前端展示统一 `replace('T',' ')`。
- 不新增第三方依赖。
- 精度规范：金额/资金 2 位+千分位；价格 3 位；数量整数+千分位；比率百分比 2 位；指标 2 位；天数 0–1 位；佣金/印花税 2 位。
- 每完成一个 Task 提交一次，commit message 用 `feat:` / `test:` / `refactor:` 前缀。

---

# Part A — 数值精度策略

## Task A1: 后端 `format.py` 精度助手

**Files:**
- Create: `main/core/engine/format.py`
- Test: `main/core/tests/unit/test_format.py`

**Interfaces:**
- Produces: `fmt_money(v) -> str`、`fmt_price(v) -> str`、`fmt_qty(v) -> str`、`fmt_pct(v) -> str`（入参为比率 0.0623 → `"6.23%"`）。均接受 `None`/`Decimal`/`int`/`float`/`str`；不可解析时返回 `"—"`。

- [ ] **Step 1: 写失败测试**

```python
# main/core/tests/unit/test_format.py
from decimal import Decimal

from core.engine.format import fmt_money, fmt_price, fmt_qty, fmt_pct


def test_fmt_money_rounds_and_thousands():
    assert fmt_money(Decimal("1369882.7275000000")) == "1,369,882.73"
    assert fmt_money(-62322.46875) == "-62,322.47"
    assert fmt_money(0) == "0.00"


def test_fmt_price_three_decimals():
    assert fmt_price(Decimal("7.42")) == "7.420"
    assert fmt_price(6.589) == "6.589"


def test_fmt_qty_integer_thousands():
    assert fmt_qty(27100) == "27,100"
    assert fmt_qty(Decimal("76300.0")) == "76,300"


def test_fmt_pct_ratio_to_percent():
    assert fmt_pct(Decimal("0.062275")) == "6.23%"
    assert fmt_pct(0.05) == "5.00%"


def test_none_and_garbage_return_dash():
    assert fmt_money(None) == "—"
    assert fmt_price(None) == "—"
    assert fmt_qty(None) == "—"
    assert fmt_pct(None) == "—"
    assert fmt_money("not-a-number") == "—"
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest core/tests/unit/test_format.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'core.engine.format'`）

- [ ] **Step 3: 实现**

```python
# main/core/engine/format.py
"""数值精度助手（显示层）：统一金额/价格/数量/百分比的取舍。

仅用于 message 文案与展示，不改变 API 数值字段的原始精度。
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DASH = "—"


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _quantize(v, places: int) -> Optional[Decimal]:
    d = _dec(v)
    if d is None:
        return None
    q = Decimal(1).scaleb(-places)
    return d.quantize(q, rounding=ROUND_HALF_UP)


def fmt_money(v) -> str:
    d = _quantize(v, 2)
    return DASH if d is None else "{:,}".format(d)


def fmt_price(v) -> str:
    d = _quantize(v, 3)
    return DASH if d is None else format(d, "f")


def fmt_qty(v) -> str:
    d = _quantize(v, 0)
    return DASH if d is None else "{:,}".format(int(d))


def fmt_pct(v) -> str:
    d = _dec(v)
    if d is None:
        return DASH
    p = (d * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(p, "f") + "%"
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest core/tests/unit/test_format.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add main/core/engine/format.py main/core/tests/unit/test_format.py
git commit -m "feat: 新增后端数值精度助手 format.py"
```

---

## Task A2: 风险/熔断 message 精度

**Files:**
- Modify: `main/core/engine/risk_manager.py:177-183`、`:229-234`
- Modify: `main/core/engine/live/breaker.py:105-108`、`:267-270`
- Test: `main/core/tests/unit/test_format_messages.py`

**Interfaces:**
- Consumes: `fmt_money`、`fmt_pct`（Task A1）

- [ ] **Step 1: 写失败测试**

```python
# main/core/tests/unit/test_format_messages.py
import re
from decimal import Decimal
from datetime import date

from core.engine.risk_manager import PortfolioRiskManager


def _mk(initial=Decimal("1000000")):
    return PortfolioRiskManager(
        initial_capital=initial,
        stop_loss_ratio=Decimal("0.05"),
        take_profit_ratio=Decimal("0.15"),
        trailing_stop_ratio=Decimal("0.03"),
        max_drawdown=Decimal("0.20"),
        daily_loss_limit=Decimal("0.05"),
    )


def test_daily_loss_message_has_no_long_decimal():
    rm = _mk()
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 24))
    rm.enter_day(date(2014, 2, 25), Decimal("1000000"))
    rm.update_daily(Decimal("1304160.2587"), Decimal("1000000"), date(2014, 2, 25))
    msgs = [e.message for e in rm.events]
    assert msgs, "应产生 daily_loss 事件"
    joined = " ".join(msgs)
    assert not re.search(r"\.\d{4,}", joined), joined
    assert "6.23%" in joined.replace(",", "")
```

> 说明：`PortfolioRiskManager` 的事件容器属性名以现有实现为准（`self.events` 或 recorder）；若为 `recorder`，测试内改用 `rm.recorder.events` 取 message。实现前先读 `main/core/engine/risk_manager.py:50-95` 确认。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest core/tests/unit/test_format_messages.py -v`
Expected: FAIL（message 含 `1369882.72750000000000` 之类长小数，正则命中）

- [ ] **Step 3: 实现**

在 `main/core/engine/risk_manager.py` 顶部加：

```python
from core.engine.format import fmt_money, fmt_pct
```

替换 `:177-183`（max_drawdown 触发）：

```python
                self._record_halt(
                    "max_drawdown", "max_drawdown", self.max_drawdown, drawdown,
                    current_date,
                    "峰值 %s 现值 %s 回撤 %s 触发熔断（第 %d 次%s）" % (
                        fmt_money(self.peak_value), fmt_money(total_value),
                        fmt_pct(drawdown), self.consecutive_drawdown_triggers, tail,
                    ),
                )
```

（`drawdown`/`total_value`/`tail` 用该处现有局部变量名，保持逻辑不变，仅改格式化。）

替换 `:229-234`（daily_loss 触发）：

```python
                self._record_halt(
                    "daily_loss", "daily_loss_limit", self.daily_loss_limit, loss,
                    current_date,
                    "日内盈亏 %s（基准 %s）亏损 %s 触发当日暂停" % (
                        fmt_money(daily_pnl), fmt_money(self.prev_close_value), fmt_pct(loss),
                    ),
                )
```

在 `main/core/engine/live/breaker.py` 顶部加 `from core.engine.format import fmt_money, fmt_pct`，替换两处 detail：

```python
                detail = (
                    " total=%s peak=%s drawdown=%s threshold=%s"
                    % (fmt_money(total_value), fmt_money(rm.peak_value),
                       fmt_pct(dd_pct / 100), fmt_pct(rm.max_drawdown))
                )
```

```python
                    detail = (
                        " total=%s prev_close=%s pnl=%s loss=%s threshold=%s"
                        % (fmt_money(total), fmt_money(prev), fmt_money(pnl),
                           fmt_pct(loss_pct / 100), fmt_pct(dpm.daily_loss_limit))
                    )
```

（注意：breaker 里 `dd_pct`/`loss_pct` 已是 0–100 的数，`fmt_pct` 吃比率，故 `/100`。）

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest core/tests/unit/test_format_messages.py core/tests/unit/test_risk_manager.py core/tests/unit/test_live_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add main/core/engine/risk_manager.py main/core/engine/live/breaker.py main/core/tests/unit/test_format_messages.py
git commit -m "feat: 风险/熔断 message 统一数值精度"
```

---

## Task A3: 组合/执行 message 精度

**Files:**
- Modify: `main/core/engine/portfolio.py:223-224`、`:240-241`、`:260-261`、`:320-321`、`:370-371`、`:388-390`、`:411-412`
- Modify: `main/core/engine/execution_engine.py:158-159`、`:169-170`、`:237-238`、`:246-247`
- Test: `main/core/tests/unit/test_format_messages.py`（追加用例）

**Interfaces:**
- Consumes: `fmt_money`、`fmt_price`、`fmt_pct`（Task A1）

- [ ] **Step 1: 写失败测试（追加到 test_format_messages.py）**

```python
import re


def test_engine_messages_no_long_decimal():
    from core.engine.format import fmt_money, fmt_price, fmt_pct
    # 直接校验替换后的模板产物（模板与实现一致）
    s = "止损：成本 %s 现价 %s 亏 %s" % (fmt_price("7.42"), fmt_price("6.99"), fmt_pct(0.0581))
    assert s == "止损：成本 7.420 现价 6.990 亏 5.81%"
    s2 = "ADD 逢跌加仓：现价较成本回撤 %s < 阈值 %s（成本 %s 现价 %s）" % (
        fmt_pct(0.021), fmt_pct(0.05), fmt_price("7.42"), fmt_price("7.27"))
    assert s2 == "ADD 逢跌加仓：现价较成本回撤 2.10% < 阈值 5.00%（成本 7.420 现价 7.270）"
    assert not re.search(r"\.\d{4,}", s + s2)
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest core/tests/unit/test_format_messages.py -v`
Expected: FAIL（当前实现未用 fmt_*，字符串不符）

- [ ] **Step 3: 实现**

`main/core/engine/portfolio.py` 顶部加 `from core.engine.format import fmt_money, fmt_price, fmt_pct`，逐处替换（仅改格式化，逻辑/变量名不变）：

```python
                    message="止损：成本 %s 现价 %s 亏 %s" % (
                        fmt_price(pos.avg_cost), fmt_price(close), fmt_pct(loss)),
```
```python
                    message="止盈：成本 %s 现价 %s 盈 %s" % (
                        fmt_price(pos.avg_cost), fmt_price(close), fmt_pct(profit)),
```
```python
                    message="移动止损：最高 %s 现价 %s 回撤 %s" % (
                        fmt_price(pos.highest_price), fmt_price(close), fmt_pct(drawdown)),
```
```python
                          message="REDUCE 减仓比例 %s × 持仓 %d 向下取整后 %d 股 <100"
                                  % (fmt_pct(ctx.reduce_position_ratio), pos.quantity, quantity))
```
```python
                          message="OPEN 单笔开仓比例 %s × 策略资金 %s / 价 %s 取整后 %d 股 <100"
                                  % (fmt_pct(ctx.single_open_ratio), fmt_money(strategy_fund),
                                     fmt_price(close), quantity))
```
```python
                              message="ADD 逢跌加仓：现价较成本回撤 %s < 阈值 %s（成本 %s 现价 %s）"
                                      % (fmt_pct(drop), fmt_pct(ctx.add_position_threshold),
                                         fmt_price(pos.avg_cost), fmt_price(close)))
```
```python
                          message="ADD 加仓比例 %s × 策略资金 %s / 价 %s 取整后 %d 股 <100"
                                  % (fmt_pct(ctx.add_position_ratio), fmt_money(strategy_fund),
                                     fmt_price(close), quantity))
```

`main/core/engine/execution_engine.py` 顶部加 `from core.engine.format import fmt_money, fmt_price`，替换：

```python
                    message="资金不足拒单：需 %s 元买 %d 股 @%s，可用现金 %s（缩量后 %d 股<1手）"
                            % (fmt_money(order.price * order.quantity if order.price else 0),
                               order.quantity, fmt_price(order.price),
                               fmt_money(account.cash), qty))
```
```python
                    message="资金不足缩量：请求 %d 股，按可用现金 %s 缩至 %d 股 @%s"
                            % (order.quantity, fmt_money(account.cash), qty, fmt_price(order.price)))
```
```python
                        message="跳空高开资金不足：成交价 %s，需 %s，可用 %s"
                                % (fmt_price(trade.price), fmt_money(cost), fmt_money(account.cash)))
```
```python
                        message="跳空高开缩量：%d → %d @ %s"
                                % (order.quantity, qty, fmt_price(trade.price)))
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest core/tests/unit/test_format_messages.py core/tests/unit/test_portfolio.py core/tests/unit/test_execution_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add main/core/engine/portfolio.py main/core/engine/execution_engine.py main/core/tests/unit/test_format_messages.py
git commit -m "feat: 组合/执行 message 统一数值精度"
```

---

## Task A4: 前端 `format.ts` 精度助手

**Files:**
- Create: `web/src/utils/format.ts`
- Test: `web/src/__tests__/format.test.ts`

**Interfaces:**
- Produces: `fmtMoney`、`fmtPrice`、`fmtQty`、`fmtPct(v, digits=2)`、`fmtSignedPct(v, digits=2)`、`fmtRatio(v, digits=2)`。`null`/`undefined`/非有限数 → `'—'`。

- [ ] **Step 1: 写失败测试**

```ts
// web/src/__tests__/format.test.ts
import { describe, it, expect } from 'vitest'
import { fmtMoney, fmtPrice, fmtQty, fmtPct, fmtSignedPct, fmtRatio } from '../utils/format'

describe('format.ts', () => {
  it('金额千分位 2 位', () => {
    expect(fmtMoney(1369882.7275)).toBe('1,369,882.73')
    expect(fmtMoney(-62322.46875)).toBe('-62,322.47')
    expect(fmtMoney(0)).toBe('0.00')
    expect(fmtMoney(null)).toBe('—')
  })
  it('价格 3 位', () => {
    expect(fmtPrice(7.42)).toBe('7.420')
    expect(fmtPrice(undefined)).toBe('—')
  })
  it('数量整数千分位', () => {
    expect(fmtQty(27100)).toBe('27,100')
    expect(fmtQty(NaN)).toBe('—')
  })
  it('百分比', () => {
    expect(fmtPct(0.0623)).toBe('6.23%')
    expect(fmtSignedPct(0.009)).toBe('+0.90%')
    expect(fmtSignedPct(-0.01)).toBe('-1.00%')
    expect(fmtRatio(1.2345)).toBe('1.23')
  })
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/format.test.ts`
Expected: FAIL（无法解析 `../utils/format`）

- [ ] **Step 3: 实现**

```ts
// web/src/utils/format.ts
const DASH = '—'

function finite(v: number | null | undefined): v is number {
  return v !== null && v !== undefined && Number.isFinite(Number(v))
}

export function fmtMoney(v: number | null | undefined): string {
  if (!finite(v)) return DASH
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function fmtPrice(v: number | null | undefined): string {
  if (!finite(v)) return DASH
  return Number(v).toFixed(3)
}

export function fmtQty(v: number | null | undefined): string {
  if (!finite(v)) return DASH
  return Math.round(Number(v)).toLocaleString('zh-CN')
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (!finite(v)) return DASH
  return `${(Number(v) * 100).toFixed(digits)}%`
}

export function fmtSignedPct(v: number | null | undefined, digits = 2): string {
  if (!finite(v)) return DASH
  const n = Number(v)
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(digits)}%`
}

export function fmtRatio(v: number | null | undefined, digits = 2): string {
  if (!finite(v)) return DASH
  return Number(v).toFixed(digits)
}
```

- [ ] **Step 4: 运行，确认通过**

Run: `npx vitest run src/__tests__/format.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/utils/format.ts web/src/__tests__/format.test.ts
git commit -m "feat: 新增前端数值精度助手 format.ts"
```

---

## Task A5: 交易明细列接入 `format.ts` 并收敛本地助手

**Files:**
- Modify: `web/src/views/Backtest.vue:1550-1588`（本地 `pct`/`num`/`pct1`/`pctSigned`）、`:3351-3364`（交易明细单元格）
- Test: `web/src/__tests__/Backtest.test.ts`（追加用例）

**Interfaces:**
- Consumes: `fmtMoney`、`fmtPrice`、`fmtQty`（Task A4）

- [ ] **Step 1: 写失败测试（追加到 Backtest.test.ts 的详情用例块）**

```ts
it('交易明细价格/金额/数量按统一精度渲染', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    trades: [{ id: 9, stock_code: '000001.SZ', trade_type: 'BUY', price: 7.42,
      quantity: 27100, amount: 201082.35, commission: 50.27, stamp_duty: 0,
      bar_time: '2019-09-17T00:00:00', signal_name: '转强', signal_type: 'OPEN' }],
  })
  const w = mount(Backtest)
  await flushPromises()
  await w.findAll('button').find((b) => b.text().includes('查看'))!.trigger('click')
  await flushPromises()
  expect(w.text()).toContain('7.420')
  expect(w.text()).toContain('27,100')
  expect(w.text()).toContain('201,082.35')
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "统一精度"`
Expected: FAIL（当前 amount 用 `toLocaleString()` 无 2 位小数 → `201,082.35` 可能为空或 `201,082.35` 不匹配，视数据；price `.toFixed(3)` 已过，数量无千分位）

- [ ] **Step 3: 实现**

`Backtest.vue` `<script setup>` 顶部（现有 import 区）加：

```ts
import { fmtMoney, fmtPrice, fmtQty, fmtPct, fmtSignedPct } from '../utils/format'
```

将本地助手改为转发（保留名称以减少改动面）：把 `:1550-1588` 的 `pct`/`num`/`pct1`/`pctSigned` 实现替换为：

```ts
function pct(v: number | null | undefined): string { return fmtPct(v, 2) }
function num(v: number | null | undefined, digits = 4): string { return v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(digits) }
function pct1(v: number | null | undefined): string { return fmtPct(v, 1) }
function pctSigned(v: number | null | undefined, digits = 2): string { return fmtSignedPct(v, digits) }
```

交易明细单元格 `:3359-3364` 改为：

```html
            <td>{{ t.stock_code }}</td>
            <td>{{ fmtQty(t.quantity) }}</td>
            <td>{{ fmtPrice(t.price) }}</td>
            <td>{{ fmtMoney(t.amount) }}</td>
            <td>{{ fmtMoney(t.commission) }}</td>
            <td>{{ fmtMoney(t.stamp_duty) }}</td>
```

- [ ] **Step 4: 运行，确认通过 + 类型检查**

Run: `npx vitest run src/__tests__/Backtest.test.ts` 然后 `npx vue-tsc -b`
Expected: PASS + exit 0

- [ ] **Step 5: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "refactor: 交易明细列接入 format.ts 统一精度"
```

---

# Part B — 报告页表格交互层

## Task B1: `useTable` 组合式函数

**Files:**
- Create: `web/src/composables/useTable.ts`
- Test: `web/src/__tests__/useTable.test.ts`

**Interfaces:**
- Produces: `useTable(rows, opts)` 与类型 `ColumnSpec<T>`、`FilterSpec<T>`、`UseTableOptions<T>`。返回 `{ pagedRows, displayedCount, totalCount, columns, filterSpecs, filterState, searchText, page, pageSize, totalPages, sortKey, sortDir, toggleSort, setPage, setPageSize, reset }`。

- [ ] **Step 1: 写失败测试**

```ts
// web/src/__tests__/useTable.test.ts
import { describe, it, expect } from 'vitest'
import { ref } from 'vue'
import { useTable } from '../composables/useTable'

const rows = ref([
  { code: '000001.SZ', type: 'BUY', amount: 100, date: '2026-01-02', tag: 'a' },
  { code: '000002.SZ', type: 'SELL', amount: 300, date: '2026-01-01', tag: 'b' },
  { code: '600000.SH', type: 'BUY', amount: 200, date: '2026-01-03', tag: 'a' },
])

const opts = {
  columns: [
    { key: 'date', label: '时间', sortable: true },
    { key: 'code', label: '代码' },
    { key: 'amount', label: '金额', sortable: true },
  ],
  keyword: { label: '搜索' },
  filters: [
    { kind: 'select' as const, key: 'type', label: '买卖', get: (r: any) => r.type,
      options: [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }] },
    { kind: 'numRange' as const, key: 'amount', label: '金额', get: (r: any) => r.amount },
    { kind: 'dateRange' as const, key: 'date', label: '时间', get: (r: any) => r.date },
  ],
  pageSize: 2,
}

describe('useTable', () => {
  it('关键词跨列过滤并计数', () => {
    const t = useTable(rows, opts)
    expect(t.displayedCount.value).toBe(3)
    t.searchText.value = '000001'
    expect(t.displayedCount.value).toBe(1)
    expect(t.totalCount.value).toBe(3)
  })
  it('select 筛选', () => {
    const t = useTable(rows, opts)
    t.filterState.value.type = 'BUY'
    expect(t.pagedRows.value.map((r) => r.code)).toEqual(['000001.SZ', '600000.SH'])
  })
  it('numRange / dateRange 筛选', () => {
    const t = useTable(rows, opts)
    t.filterState.value.amount = [150, '']
    expect(t.displayedCount.value).toBe(2)
    t.filterState.value.amount = ['', '']
    t.filterState.value.date = ['2026-01-02', '2026-01-03']
    expect(t.displayedCount.value).toBe(2)
  })
  it('排序三态切换', () => {
    const t = useTable(rows, opts)
    t.toggleSort('amount'); expect(t.sortDir.value).toBe('asc')
    expect(t.pagedRows.value.map((r) => r.amount)).toEqual([100, 200])
    t.toggleSort('amount'); expect(t.sortDir.value).toBe('desc')
    expect(t.pagedRows.value.map((r) => r.amount)).toEqual([300, 200])
    t.toggleSort('amount'); expect(t.sortDir.value).toBe(null)
    expect(t.pagedRows.value.map((r) => r.date)).toEqual(['2026-01-02', '2026-01-01'])
  })
  it('非 sortable 列点击无效', () => {
    const t = useTable(rows, opts)
    t.toggleSort('code'); expect(t.sortKey.value).toBe(null)
  })
  it('分页钳制与 pageSize=0 全部', () => {
    const t = useTable(rows, opts)
    t.setPage(99); expect(t.page.value).toBe(2)
    t.setPageSize(0); expect(t.pagedRows.value.length).toBe(3)
  })
  it('筛选变化回第 1 页', () => {
    const t = useTable(rows, opts)
    t.toggleSort('amount'); t.setPage(2)
    t.filterState.value.type = 'BUY'
    expect(t.page.value).toBe(1)
  })
  it('reset 清空', () => {
    const t = useTable(rows, opts)
    t.searchText.value = 'x'; t.filterState.value.type = 'BUY'; t.toggleSort('amount')
    t.reset()
    expect(t.searchText.value).toBe(''); expect(t.filterState.value).toEqual({})
    expect(t.sortKey.value).toBe(null); expect(t.page.value).toBe(1)
  })
  it('sortValue 解格式化字符串', () => {
    const r2 = ref([{ s: '+0.90%' }, { s: '-1.10%' }, { s: '+0.10%' }])
    const t = useTable(r2, { columns: [{ key: 's', label: 's', sortable: true, sortValue: (r: any) => parseFloat(r.s) }] })
    t.toggleSort('s')
    expect(t.pagedRows.value.map((r) => r.s)).toEqual(['-1.10%', '+0.10%', '+0.90%'])
  })
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/useTable.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```ts
// web/src/composables/useTable.ts
import { computed, ref, watch, type Ref } from 'vue'

export interface ColumnSpec<T = any> {
  key: string
  label: string
  sortable?: boolean
  sortValue?: (row: T) => number | string | null | undefined
  searchable?: boolean
}

export type SelectOptions = { value: string; label: string }[]

export type FilterSpec<T = any> =
  | { kind: 'select'; key: string; label: string; get: (r: T) => string;
      options: SelectOptions | (() => SelectOptions) }
  | { kind: 'dateRange'; key: string; label: string; get: (r: T) => string }
  | { kind: 'numRange'; key: string; label: string; get: (r: T) => number | null | undefined; unit?: string }

export interface UseTableOptions<T = any> {
  columns: ColumnSpec<T>[]
  filters?: FilterSpec<T>[]
  keyword?: { label: string; placeholder?: string }
  pageSize?: number
  initialSort?: { key: string; dir: 'asc' | 'desc' }
}

export function useTable<T extends Record<string, any>>(rows: Ref<T[]>, opts: UseTableOptions<T>) {
  const filterSpecs = opts.filters ?? []
  const searchText = ref('')
  const filterState = ref<Record<string, any>>({})
  const sortKey = ref<string | null>(opts.initialSort?.key ?? null)
  const sortDir = ref<'asc' | 'desc' | null>(opts.initialSort?.dir ?? null)
  const page = ref(1)
  const pageSize = ref(opts.pageSize ?? 20)

  const searchableKeys = opts.columns.filter((c) => c.searchable !== false).map((c) => c.key)
  const rowText = (r: T, k: string): string => {
    const v = (r as any)[k]
    return v === null || v === undefined ? '' : String(v)
  }

  const filtered = computed<T[]>(() => {
    let list = rows.value
    const kw = searchText.value.trim().toLowerCase()
    if (kw) list = list.filter((r) => searchableKeys.some((k) => rowText(r, k).toLowerCase().includes(kw)))
    for (const f of filterSpecs) {
      const v = filterState.value[f.key]
      if (v === undefined || v === null || v === '') continue
      if (f.kind === 'select') {
        list = list.filter((r) => f.get(r) === v)
      } else if (f.kind === 'dateRange') {
        const [from, to] = (v as [string, string]) ?? ['', '']
        if (!from && !to) continue
        list = list.filter((r) => {
          const d = f.get(r)
          if (!d) return false
          if (from && d < from) return false
          if (to && d > to) return false
          return true
        })
      } else {
        const [min, max] = (v as [number | '', number | '']) ?? ['', '']
        if (min === '' && max === '') continue
        list = list.filter((r) => {
          const n = f.get(r)
          if (n === null || n === undefined || !Number.isFinite(Number(n))) return false
          if (min !== '' && Number(n) < Number(min)) return false
          if (max !== '' && Number(n) > Number(max)) return false
          return true
        })
      }
    }
    return list
  })

  const sorted = computed<T[]>(() => {
    const key = sortKey.value
    if (!key || !sortDir.value) return filtered.value
    const col = opts.columns.find((c) => c.key === key)
    if (!col) return filtered.value
    const dir = sortDir.value === 'asc' ? 1 : -1
    const getVal = (r: T) => (col.sortValue ? col.sortValue(r) : (r as any)[key])
    return [...filtered.value].sort((a, b) => {
      const va = getVal(a); const vb = getVal(b)
      if (va === null || va === undefined) return 1
      if (vb === null || vb === undefined) return -1
      const na = Number(va); const nb = Number(vb)
      if (Number.isFinite(na) && Number.isFinite(nb)) return na === nb ? 0 : (na < nb ? -1 : 1) * dir
      const sa = String(va); const sb = String(vb)
      return sa === sb ? 0 : (sa < sb ? -1 : 1) * dir
    })
  })

  const displayedCount = computed(() => filtered.value.length)
  const totalCount = computed(() => rows.value.length)
  const totalPages = computed(() => (pageSize.value <= 0 ? 1 : Math.max(1, Math.ceil(displayedCount.value / pageSize.value))))
  const pagedRows = computed<T[]>(() => {
    if (pageSize.value <= 0) return sorted.value
    const p = Math.min(Math.max(1, page.value), totalPages.value)
    const start = (p - 1) * pageSize.value
    return sorted.value.slice(start, start + pageSize.value)
  })

  function toggleSort(key: string) {
    const col = opts.columns.find((c) => c.key === key)
    if (!col || !col.sortable) return
    if (sortKey.value !== key) { sortKey.value = key; sortDir.value = 'asc'; return }
    if (sortDir.value === 'asc') { sortDir.value = 'desc'; return }
    if (sortDir.value === 'desc') { sortKey.value = null; sortDir.value = null; return }
    sortDir.value = 'asc'
  }
  function setPage(n: number) { page.value = Math.min(Math.max(1, Math.floor(n) || 1), totalPages.value) }
  function setPageSize(n: number) { pageSize.value = n; page.value = 1 }
  function reset() {
    searchText.value = ''
    filterState.value = {}
    sortKey.value = opts.initialSort?.key ?? null
    sortDir.value = opts.initialSort?.dir ?? null
    page.value = 1
  }

  watch([searchText, filterState, sortKey, sortDir], () => { page.value = 1 }, { deep: true })

  return {
    columns: opts.columns, filterSpecs, filterState, searchText,
    sortKey, sortDir, page, pageSize, totalPages, pagedRows,
    displayedCount, totalCount, toggleSort, setPage, setPageSize, reset,
  }
}
```

- [ ] **Step 4: 运行，确认通过**

Run: `npx vitest run src/__tests__/useTable.test.ts`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add web/src/composables/useTable.ts web/src/__tests__/useTable.test.ts
git commit -m "feat: 新增 useTable 表格交互组合式函数"
```

---

## Task B2: `TableToolbar.vue`

**Files:**
- Create: `web/src/components/TableToolbar.vue`
- Test: `web/src/__tests__/TableToolbar.test.ts`

**Interfaces:**
- Consumes: `FilterSpec`（Task B1）
- Props: `filters: FilterSpec[]`、`keyword?: { label; placeholder? }`、`searchText: string`、`filterState: Record<string, any>`、`displayedCount: number`、`totalCount: number`
- Emits: `update:searchText`、`update:filterState`、`reset`

- [ ] **Step 1: 写失败测试**

```ts
// web/src/__tests__/TableToolbar.test.ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TableToolbar from '../components/TableToolbar.vue'

const filters = [
  { kind: 'select', key: 'type', label: '买卖', get: (r: any) => r.type,
    options: [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }] },
  { kind: 'numRange', key: 'amount', label: '金额', get: (r: any) => r.amount },
] as any

describe('TableToolbar', () => {
  it('渲染关键词框与筛选控件、计数', () => {
    const w = mount(TableToolbar, { props: { filters, keyword: { label: '搜索' },
      searchText: '', filterState: {}, displayedCount: 3, totalCount: 10 } })
    expect(w.find('input[type="text"]').exists()).toBe(true)
    expect(w.text()).toContain('买卖')
    expect(w.text()).toContain('显示 3 / 共 10 条')
  })
  it('输入关键词 emit update:searchText', async () => {
    const w = mount(TableToolbar, { props: { filters: [], keyword: { label: '搜索' },
      searchText: '', filterState: {}, displayedCount: 0, totalCount: 0 } })
    await w.find('input[type="text"]').setValue('000001')
    expect(w.emitted('update:searchText')![0]).toEqual(['000001'])
  })
  it('重置 emit reset', async () => {
    const w = mount(TableToolbar, { props: { filters: [], keyword: { label: '搜索' },
      searchText: 'x', filterState: { type: 'BUY' }, displayedCount: 0, totalCount: 0 } })
    await w.find('button.tb-reset').trigger('click')
    expect(w.emitted('reset')).toBeTruthy()
  })
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/TableToolbar.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现**

```vue
<!-- web/src/components/TableToolbar.vue -->
<script setup lang="ts">
import { computed } from 'vue'
import type { FilterSpec } from '../composables/useTable'

const props = defineProps<{
  filters: FilterSpec[]
  keyword?: { label: string; placeholder?: string }
  searchText: string
  filterState: Record<string, any>
  displayedCount: number
  totalCount: number
}>()
const emit = defineEmits<{
  'update:searchText': [string]
  'update:filterState': [Record<string, any>]
  reset: []
}>()

function optionsOf(f: Extract<FilterSpec, { kind: 'select' }>) {
  return typeof f.options === 'function' ? f.options() : f.options
}
function setFilter(key: string, value: any) {
  emit('update:filterState', { ...props.filterState, [key]: value })
}
function rangeVal(key: string, i: number): any {
  const v = props.filterState[key]
  return Array.isArray(v) ? (v[i] ?? '') : ''
}
function setRange(key: string, i: number, value: any) {
  const cur = Array.isArray(props.filterState[key]) ? [...props.filterState[key]] : ['', '']
  cur[i] = value === '' ? '' : (i === 0 || i === 1 ? value : value)
  setFilter(key, cur)
}
const hasAny = computed(() => !!props.keyword || (props.filters?.length ?? 0) > 0)
</script>

<template>
  <div v-if="hasAny" class="table-toolbar">
    <input v-if="keyword" class="tb-keyword" type="text"
      :placeholder="keyword.placeholder || keyword.label"
      :value="searchText" @input="emit('update:searchText', ($event.target as HTMLInputElement).value)" />
    <template v-for="f in filters" :key="f.key">
      <select v-if="f.kind === 'select'" class="tb-control" :value="filterState[f.key] ?? ''"
        @change="setFilter(f.key, ($event.target as HTMLSelectElement).value)">
        <option value="">{{ f.label }}：全部</option>
        <option v-for="o in optionsOf(f)" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <span v-else-if="f.kind === 'dateRange'" class="tb-range">
        <span>{{ f.label }}</span>
        <input type="date" :value="rangeVal(f.key, 0)" @input="setRange(f.key, 0, ($event.target as HTMLInputElement).value)" />
        <span>~</span>
        <input type="date" :value="rangeVal(f.key, 1)" @input="setRange(f.key, 1, ($event.target as HTMLInputElement).value)" />
      </span>
      <span v-else class="tb-range">
        <span>{{ f.label }}<template v-if="f.unit">({{ f.unit }})</template></span>
        <input type="number" :value="rangeVal(f.key, 0)" @input="setRange(f.key, 0, ($event.target as HTMLInputElement).value)" />
        <span>~</span>
        <input type="number" :value="rangeVal(f.key, 1)" @input="setRange(f.key, 1, ($event.target as HTMLInputElement).value)" />
      </span>
    </template>
    <button class="tb-reset" @click="emit('reset')">重置</button>
    <span class="tb-count">显示 {{ displayedCount }} / 共 {{ totalCount }} 条</span>
  </div>
</template>

<style scoped>
.table-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.tb-keyword, .tb-control, .tb-range input { padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg); color: var(--ink); font-size: 12px; }
.tb-range { display: inline-flex; align-items: center; gap: 4px; color: var(--ink-soft); }
.tb-reset { padding: 4px 10px; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--ink-soft); cursor: pointer; }
.tb-reset:hover { color: var(--accent); border-color: var(--accent); }
.tb-count { margin-left: auto; color: var(--ink-soft); }
</style>
```

- [ ] **Step 4: 运行，确认通过**

Run: `npx vitest run src/__tests__/TableToolbar.test.ts`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add web/src/components/TableToolbar.vue web/src/__tests__/TableToolbar.test.ts
git commit -m "feat: 新增 TableToolbar 表格筛选栏组件"
```

---

## Task B3: `TablePager.vue`

**Files:**
- Create: `web/src/components/TablePager.vue`
- Test: `web/src/__tests__/TablePager.test.ts`

**Interfaces:**
- Props: `page: number`、`pageSize: number`、`totalPages: number`、`displayedCount: number`、`totalCount: number`
- Emits: `update:page`、`update:pageSize`

- [ ] **Step 1: 写失败测试**

```ts
// web/src/__tests__/TablePager.test.ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TablePager from '../components/TablePager.vue'

describe('TablePager', () => {
  it('渲染页码与计数', () => {
    const w = mount(TablePager, { props: { page: 2, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    expect(w.text()).toContain('第 2 / 5 页')
    expect(w.text()).toContain('共 95 条')
  })
  it('上一页/下一页 emit', async () => {
    const w = mount(TablePager, { props: { page: 2, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    await w.find('button.tp-prev').trigger('click')
    expect(w.emitted('update:page')![0]).toEqual([1])
    await w.find('button.tp-next').trigger('click')
    expect(w.emitted('update:page')![1]).toEqual([3])
  })
  it('切换每页条数 emit', async () => {
    const w = mount(TablePager, { props: { page: 1, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    await w.find('select').setValue('50')
    expect(w.emitted('update:pageSize')![0]).toEqual([50])
  })
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/TablePager.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现**

```vue
<!-- web/src/components/TablePager.vue -->
<script setup lang="ts">
const props = defineProps<{
  page: number
  pageSize: number
  totalPages: number
  displayedCount: number
  totalCount: number
}>()
const emit = defineEmits<{ 'update:page': [number]; 'update:pageSize': [number] }>()
const SIZE_OPTIONS = [20, 50, 100, 0]
function onSize(e: Event) { emit('update:pageSize', Number((e.target as HTMLSelectElement).value)) }
</script>

<template>
  <div class="table-pager pagination">
    <select :value="pageSize" @change="onSize">
      <option v-for="s in SIZE_OPTIONS" :key="s" :value="s">{{ s === 0 ? '全部' : s + ' 条/页' }}</option>
    </select>
    <button class="tp-prev page-btn" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
    <span class="page-info">第 {{ page }} / {{ totalPages }} 页 · 共 {{ displayedCount }} 条</span>
    <button class="tp-next page-btn" :disabled="page >= totalPages" @click="emit('update:page', page + 1)">下一页</button>
  </div>
</template>

<style scoped>
.table-pager { display: flex; align-items: center; gap: 8px; justify-content: center; margin-top: 10px; font-size: 12px; }
.table-pager select { padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg); color: var(--ink); }
</style>
```

- [ ] **Step 4: 运行，确认通过**

Run: `npx vitest run src/__tests__/TablePager.test.ts`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add web/src/components/TablePager.vue web/src/__tests__/TablePager.test.ts
git commit -m "feat: 新增 TablePager 分页组件"
```

---

## Task B4: 交易明细接入交互层

**Files:**
- Modify: `web/src/views/Backtest.vue`（交易明细分页逻辑 `:1909-1915`、模板 `:3340-3374`）
- Test: `web/src/__tests__/Backtest.test.ts`（追加用例）

**Interfaces:**
- Consumes: `useTable`、`TableToolbar`、`TablePager`（Tasks B1–B3）

- [ ] **Step 1: 写失败测试（追加）**

```ts
it('交易明细支持关键词过滤与排序', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    trades: [
      { id: 1, stock_code: '000001.SZ', trade_type: 'BUY', price: 10, quantity: 100, amount: 1000, commission: 5, stamp_duty: 0, bar_time: '2026-07-02T09:30:00', signal_name: 'a', signal_type: 'OPEN' },
      { id: 2, stock_code: '600000.SH', trade_type: 'SELL', price: 9, quantity: 200, amount: 900, commission: 5, stamp_duty: 1, bar_time: '2026-07-03T09:30:00', signal_name: 'b', signal_type: 'CLOSE' },
    ],
  })
  const w = mount(Backtest)
  await flushPromises()
  await w.findAll('button').find((b) => b.text().includes('查看'))!.trigger('click')
  await flushPromises()
  const kw = w.find('input[type="text"].tb-keyword')
  await kw.setValue('600000')
  await flushPromises()
  const rows = w.findAll('table.trade-table tbody tr')
  expect(rows.length).toBe(1)
  expect(rows[0].text()).toContain('600000.SH')
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "关键词过滤与排序"`
Expected: FAIL（无 `.tb-keyword`）

- [ ] **Step 3: 实现**

`Backtest.vue` 加 import：

```ts
import { useTable } from '../composables/useTable'
import TableToolbar from '../components/TableToolbar.vue'
import TablePager from '../components/TablePager.vue'
```

将 `:1909-1915`（`totalPages`/`paginatedTrades`）替换为：

```ts
const tradesTable = useTable(
  computed(() => detail.value?.trades ?? []),
  {
    pageSize: 20,
    keyword: { label: '搜索', placeholder: '代码 / 信号来源 / 策略名' },
    columns: [
      { key: 'bar_time', label: '时间', sortable: true },
      { key: 'strategy_name', label: '策略' },
      { key: 'signal_name', label: '信号来源', searchable: true },
      { key: 'trade_type', label: '买卖' },
      { key: 'stock_code', label: '代码', searchable: true },
      { key: 'quantity', label: '数量', sortable: true, sortValue: (t: any) => Number(t.quantity) },
      { key: 'price', label: '价格', sortable: true, sortValue: (t: any) => Number(t.price) },
      { key: 'amount', label: '金额', sortable: true, sortValue: (t: any) => Number(t.amount) },
      { key: 'commission', label: '佣金', sortable: true, sortValue: (t: any) => Number(t.commission) },
      { key: 'stamp_duty', label: '印花税', sortable: true, sortValue: (t: any) => Number(t.stamp_duty) },
    ],
    filters: [
      { kind: 'select', key: 'trade_type', label: '买卖', get: (t: any) => t.trade_type,
        options: [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }] },
      { kind: 'select', key: 'signal_type', label: '信号', get: (t: any) => t.signal_type,
        options: () => Array.from(new Set((detail.value?.trades ?? []).map((t: any) => t.signal_type).filter(Boolean)))
          .map((v: any) => ({ value: v, label: signalTypeLabel(v) })) },
      { kind: 'dateRange', key: 'bar_time', label: '时间', get: (t: any) => (t.bar_time || '').slice(0, 10) },
      { kind: 'numRange', key: 'amount', label: '金额', unit: '元', get: (t: any) => Number(t.amount) },
    ],
  },
)
```

删除旧的 `currentPage`/`pageSize`（`:828-829`）与本地 `totalPages`/`paginatedTrades`。模板 `:3342-3374` 替换为：

```html
    <div class="table-container">
      <TableToolbar
        :filters="tradesTable.filterSpecs"
        :keyword="{ label: '搜索', placeholder: '代码 / 信号来源 / 策略名' }"
        :search-text="tradesTable.searchText.value"
        :filter-state="tradesTable.filterState.value"
        :displayed-count="tradesTable.displayedCount.value"
        :total-count="tradesTable.totalCount.value"
        @update:search-text="(v: string) => (tradesTable.searchText.value = v)"
        @update:filter-state="(v: any) => (tradesTable.filterState.value = v)"
        @reset="tradesTable.reset"
      />
      <table class="trade-table">
        <thead>
          <tr>
            <th v-for="c in tradesTable.columns" :key="c.key"
                :class="{ sortable: c.sortable }"
                @click="c.sortable && tradesTable.toggleSort(c.key)">
              {{ c.label }}
              <span v-if="tradesTable.sortKey.value === c.key">{{ tradesTable.sortDir.value === 'asc' ? '▲' : '▼' }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in tradesTable.pagedRows.value" :key="t.id">
            <td style="font-size:12px;color:var(--ink-faint)">{{ t.bar_time?.replace('T', ' ') }}</td>
            <td><span class="strategy-badge">{{ t.strategy_name || `#${t.strategy_id}` }}</span></td>
            <td>
              <span style="font-size:12px">{{ t.signal_name || '—' }}</span>
              <span v-if="t.signal_type" class="signal-badge">{{ signalTypeLabel(t.signal_type) }}</span>
            </td>
            <td :class="t.trade_type === 'BUY' ? 'text-green' : 'text-red'">{{ t.trade_type === 'BUY' ? '买入' : '卖出' }}</td>
            <td>{{ t.stock_code }}</td>
            <td>{{ fmtQty(t.quantity) }}</td>
            <td>{{ fmtPrice(t.price) }}</td>
            <td>{{ fmtMoney(t.amount) }}</td>
            <td>{{ fmtMoney(t.commission) }}</td>
            <td>{{ fmtMoney(t.stamp_duty) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="tradesTable.displayedCount.value === 0" class="empty-state"><p>无匹配交易记录</p></div>
      <TablePager
        :page="tradesTable.page.value" :page-size="tradesTable.pageSize.value"
        :total-pages="tradesTable.totalPages.value"
        :displayed-count="tradesTable.displayedCount.value" :total-count="tradesTable.totalCount.value"
        @update:page="tradesTable.setPage" @update:page-size="tradesTable.setPageSize"
      />
    </div>
```

> 注意：`useTable` 返回的是 refs/普通值，`.value` 需在 `<script setup>` 自动解包。模板里 `tradesTable.xxx.value` 不会自动解包（对象属性），故显式 `.value` 是正确的。若 `vue-tsc` 报未使用 `currentPage`，一并删除相关声明。

- [ ] **Step 4: 运行，确认通过 + 类型检查**

Run: `npx vitest run src/__tests__/Backtest.test.ts` 然后 `npx vue-tsc -b`
Expected: PASS + exit 0

- [ ] **Step 5: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 交易明细接入排序/筛选/分页交互层"
```

---

## Task B5: 模块十二 下跌周明细接入

**Files:**
- Modify: `web/src/views/Backtest.vue`（`caDownRows`/分页 `:456-473`、模板 `:2712-2730`）
- Test: `web/src/__tests__/Backtest.test.ts`（追加用例）

**Interfaces:**
- Consumes: `useTable`、`TableToolbar`、`TablePager`

- [ ] **Step 1: 写失败测试（追加）**

```ts
it('下跌周明细支持按分类筛选', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    crisis_alpha: { status: 'completed', benchmark_code: '000300.SH', benchmark_name: '沪深300',
      data: { quadrant: { n_down: 2, follow_ratio: 0.5, alpha_ratio: 0.5, down_weeks: [] },
        down_weeks: [
          { 日期: '2026-01-05', 基准收益: -0.01, 策略收益: 0.001, 超额: 0.011, 分类: '逆势上涨' },
          { 日期: '2026-01-12', 基准收益: -0.02, 策略收益: -0.005, 超额: 0.015, 分类: '跌得少' },
        ] } },
  })
  const w = mount(Backtest)
  await flushPromises()
  await w.findAll('button').find((b) => b.text().includes('查看'))!.trigger('click')
  await flushPromises()
  const sel = w.findAll('select').find((s) => s.text().includes('逆势上涨'))!
  await sel.setValue('逆势上涨')
  await flushPromises()
  const rows = w.findAll('table.ca-down-table tbody tr')
  expect(rows.length).toBe(1)
})
```

> 若危机 Alpha 数据在 payload 的字段名与 `ca.value?.down_weeks` 不符，先读 `Backtest.vue:147-152`（`ca` computed）确认正确访问点，测试数据对齐真实结构。

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "下跌周明细支持按分类筛选"`
Expected: FAIL（无筛选栏）

- [ ] **Step 3: 实现**

将 `:465-473`（下跌周分页）替换为：

```ts
const caDownTable = useTable(
  computed(() => caDownRows.value),
  {
    pageSize: 20,
    keyword: { label: '搜索', placeholder: '分类' },
    columns: [
      { key: '日期', label: '日期', sortable: true },
      { key: '基准', label: '基准', sortable: true, sortValue: (r: any) => parseFloat(String(r.基准).replace('%', '')) },
      { key: '策略', label: '策略', sortable: true, sortValue: (r: any) => parseFloat(String(r.策略).replace('%', '')) },
      { key: '超额', label: '超额', sortable: true, sortValue: (r: any) => parseFloat(String(r.超额).replace('%', '')) },
      { key: '分类', label: '分类', searchable: true },
    ],
    filters: [
      { kind: 'select', key: '分类', label: '分类', get: (r: any) => r.分类,
        options: () => Array.from(new Set(caDownRows.value.map((r: any) => r.分类).filter(Boolean)))
          .map((v: any) => ({ value: v, label: v })) },
      { kind: 'dateRange', key: '日期', label: '时间', get: (r: any) => String(r.日期).slice(0, 10) },
      { kind: 'numRange', key: '超额', label: '超额%', get: (r: any) => parseFloat(String(r.超额).replace('%', '')) },
    ],
  },
)
```

模板 `:2713-2730` 的表格头/体改为遍历 `caDownTable.columns`（同 Task B4 的 th 写法），`tbody` 用 `v-for="r in caDownTable.pagedRows.value"`，保留现有 `分类` 颜色样式（`r.color`）。表格上方插 `<TableToolbar .../>`（同 B4，绑定 `caDownTable`），下方插 `<TablePager .../>`。

- [ ] **Step 4: 运行，确认通过 + 类型检查**

Run: `npx vitest run src/__tests__/Backtest.test.ts` 然后 `npx vue-tsc -b`
Expected: PASS + exit 0

- [ ] **Step 5: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 下跌周明细接入排序/筛选/分页交互层"
```

---

## Task B6: 决策事件接入（聚合摘要 + 下钻明细）

**Files:**
- Modify: `web/src/views/Backtest.vue`（`decisionSummary` `:1941-1942`、`selectedEvents` `:1953+`、模板 `:3282-3336`）
- Test: `web/src/__tests__/Backtest.test.ts`（追加用例）

**Interfaces:**
- Consumes: `useTable`、`TableToolbar`、`TablePager`

- [ ] **Step 1: 写失败测试（追加）**

```ts
it('决策事件下钻明细支持关键词过滤', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    decision_summary: [{ gate: 'stop_loss', layer: 'strategy_risk', action: 'trigger',
      param_name: 'stop_loss_ratio', param_value: 0.05, count: 2, stock_count: 2,
      first_bar_time: '2026-07-02T09:30:00', last_bar_time: '2026-07-03T09:30:00',
      requested_qty_sum: 200, final_qty_sum: 200 }],
    decisions: [
      { id: 1, gate: 'stop_loss', bar_time: '2026-07-02T09:30:00', strategy_id: 1, stock_code: '000001.SZ', param_value: 0.05, actual_value: 0.06, requested_qty: 100, final_qty: 100, message: '止损：成本 7.42 现价 6.99 亏 5.81%' },
      { id: 2, gate: 'stop_loss', bar_time: '2026-07-03T09:30:00', strategy_id: 1, stock_code: '600000.SH', param_value: 0.05, actual_value: 0.07, requested_qty: 100, final_qty: 100, message: '止损：成本 9.0 现价 8.3 亏 7.2%' },
    ],
  })
  const w = mount(Backtest)
  await flushPromises()
  await w.findAll('button').find((b) => b.text().includes('查看'))!.trigger('click')
  await flushPromises()
  await w.find('tr.decision-row').trigger('click')
  await flushPromises()
  const kw = w.find('.drill-box input.tb-keyword')
  await kw.setValue('600000')
  await flushPromises()
  expect(w.findAll('.drill-box table.trade-table tbody tr').length).toBe(1)
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "决策事件下钻明细支持关键词过滤"`
Expected: FAIL（下钻表无筛选栏）

- [ ] **Step 3: 实现**

聚合摘要表（`:3282-3309`）：在 `.table-container` 顶部插 `<TableToolbar>`（keyword 按闸门名/参数名、filters：层 select、动作 select）。给聚合表建 `useTable(decisionSummary, {...})`，列：`闸门`(searchable, sortValue=gateLabel)、`层`、`动作`、`参数`、`阈值`、`触发次数`(sortable)、`首次`(sortable)、`末次`(sortable)、`涉及股票`(sortable)、`请求/实发`。行点击下钻逻辑（`toggleGate`）保留在 `<tr>` 上。

下钻明细表（`:3318-3336`）：给 `selectedEvents` 建 `useTable`，列：`时间`(sortable)、`策略`、`代码`(searchable)、`阈值`、`实际`、`请求/实发`、`原因`(searchable)；filters：时间范围。表格上方插 `<TableToolbar>`（限定在 `.drill-box` 内），下方插 `<TablePager>`。

```ts
const decisionDetailTable = useTable(
  computed(() => selectedEvents.value),
  {
    pageSize: 20,
    keyword: { label: '搜索', placeholder: '代码 / 原因' },
    columns: [
      { key: 'bar_time', label: '时间', sortable: true },
      { key: 'strategy_id', label: '策略' },
      { key: 'stock_code', label: '代码', searchable: true },
      { key: 'param_value', label: '阈值' },
      { key: 'actual_value', label: '实际' },
      { key: 'final_qty', label: '请求/实发' },
      { key: 'message', label: '原因', searchable: true },
    ],
    filters: [
      { kind: 'dateRange', key: 'bar_time', label: '时间', get: (e: any) => (e.bar_time || '').slice(0, 10) },
    ],
  },
)
```

（`selectedEvents` 是 computed，包装成 `computed(() => selectedEvents.value)`。）

- [ ] **Step 4: 运行，确认通过 + 类型检查**

Run: `npx vitest run src/__tests__/Backtest.test.ts` 然后 `npx vue-tsc -b`
Expected: PASS + exit 0

- [ ] **Step 5: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 决策事件摘要与下钻明细接入交互层"
```

---

## Task B7: 危机 Alpha / 市场状态动态表接入

**Files:**
- Modify: `web/src/views/Backtest.vue`（危机 Alpha：`caEventRows`/`caBigDownRows`/`caAnnualRows`；市场状态：`mrReboundRows`/`mrBigUpRows`/`mrNeutralRows`/`mrStateRows`）
- Test: `web/src/__tests__/Backtest.test.ts`（追加用例，至少覆盖一张表）

**Interfaces:**
- Consumes: `useTable`、`TableToolbar`、`TablePager`

- [ ] **Step 1: 写失败测试（追加，以「年度表」为例）**

```ts
it('危机Alpha 年度表支持排序', async () => {
  ;(getBacktestDetail as any).mockResolvedValueOnce({
    ...mockDetail,
    crisis_alpha: { status: 'completed', benchmark_code: '000300.SH', benchmark_name: '沪深300',
      data: { annual: [
        { 年度: '2014', 基准收益: -0.10, 策略收益: 0.20, 超额: 0.30 },
        { 年度: '2015', 基准收益: 0.05, 策略收益: 0.10, 超额: 0.05 },
      ] } },
  })
  const w = mount(Backtest)
  await flushPromises()
  await w.findAll('button').find((b) => b.text().includes('查看'))!.trigger('click')
  await flushPromises()
  expect(w.text()).toContain('2014')
})
```

> 该用例只验证接入后不破坏渲染；排序点击可后续补强。先读对应 computed 的真实字段名（`caAnnualRows` `:400`、`mrReboundRows` `:1284` 等）后落配置。

- [ ] **Step 2: 运行，确认通过（现状已渲染）**

Run: `npx vitest run src/__tests__/Backtest.test.ts -t "危机Alpha 年度表支持排序"`
Expected: PASS（作为接入前的基线；若失败先修数据结构对齐）

- [ ] **Step 3: 实现**

对以下每张表套用与 Task B4 相同的模式（`useTable` + `TableToolbar` + `TablePager`，`th` 遍历 `columns`）：
- 危机 Alpha：自然回撤记录（`caEventRows`）、大跌月明细（`caBigDownRows`）、年度表（`caAnnualRows`）
- 市场状态：反弹情景（`mrReboundRows`）、大涨情景（`mrBigUpRows`）、中性情景（`mrNeutralRows`）、状态效能（`mrStateRows`）
- 行业归因贡献（`iaContribRows`）、稳健性敏感性（`rbSensRows`）：可选，小表按需接入

排序键优先数值列（用 `sortValue` 解百分比/数值），筛选器按各表分类列 + 时间范围。**固定小表（KPI/评分/汇总）不接入。**

- [ ] **Step 4: 运行全量，确认通过 + 类型检查**

Run: `npx vitest run` 然后 `npx vue-tsc -b`
Expected: 全量 PASS + exit 0

- [ ] **Step 5: 提交**

```bash
git add web/src/views/Backtest.vue web/src/__tests__/Backtest.test.ts
git commit -m "feat: 危机Alpha与市场状态动态表接入交互层"
```

---

## 最终验证

- [ ] 后端：`cd main && uv run pytest core/tests/` → 全绿
- [ ] 前端：`cd web && npx vitest run` → 全绿
- [ ] 类型：`cd web && npx vue-tsc -b` → exit 0
- [ ] 手动：交易明细按金额排序、按代码搜索、按买卖/信号过滤；下跌周明细按超额排序/按分类过滤；决策事件原因列无超长小数；年度/周频表可排序
- [ ] 打包（如需）：`cd main && python build_package.py`
