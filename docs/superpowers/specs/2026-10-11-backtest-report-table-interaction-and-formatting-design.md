# 报告页表格交互层 + 数值精度策略 — 设计文档

**日期**：2026-10-11
**范围**：回测报告页（`web/src/views/Backtest.vue`）长表格的通用交互层；全站数值精度/取舍（显示 + 文案层）
**非目标**：不改 API 数值字段精度、不改 DB schema/迁移、不改导出的 HTML 报告（`html_report.py`）的交互性

---

## 背景

回测报告页是单页超长页面（`Backtest.vue` 约 3820 行），其中「随数据变长」的表格（交易明细达数千行、下跌周明细数百行、决策事件下钻上万行等）目前**只有客户端固定 20 条/页的上一页/下一页**，**无排序、无搜索/筛选**，导致只能逐页翻看、无法快速定位（如「某只个股的完整交易记录」）。

同时，全站**数值取舍不统一**：后端大量 message/reason 用 `%s` 直接格式化 `Decimal`，产生如 `-62322.46875000000000（基准 1369882.72750000000000）` 的超长尾数；前端 `.toFixed()` / `toLocaleString()` / 裸插值混用。

现有数据均**一次性全量**由 `GET /api/backtest/records/{id}` 返回并常驻前端内存，因此排序/筛选/分页可**纯客户端**完成，无需改后端接口。

---

## Part 1：通用表格交互层

### 1. 架构与文件

采用**组合式函数（逻辑层）+ 两个轻量展示组件**方案，保留各表现有 `<table>` 标记与单元格（徽章/颜色/下钻）不动，按表增量接入。

新增文件：

- `web/src/composables/useTable.ts` — 排序 / 筛选 / 分页逻辑 + 类型定义
- `web/src/components/TableToolbar.vue` — 顶部关键词 + 筛选控件 + 重置 + 计数
- `web/src/components/TablePager.vue` — 底部分页控件

### 2. `useTable` API

```ts
interface ColumnSpec<T> {
  key: string                                     // 唯一键（对应列）
  label: string                                   // 表头文字
  sortable?: boolean                              // 默认 false
  sortValue?: (row: T) => number | string | null  // 原始排序值（解格式化字符串，如 "+0.90%"→0.009）
  searchable?: boolean                            // 是否参与关键词搜索，默认 true
}

type FilterSpec<T> =
  | { kind: 'select';    key: string; label: string;
      get: (r: T) => string;
      options: { value: string; label: string }[] | (() => { value: string; label: string }[]) }
  | { kind: 'dateRange'; key: string; label: string; get: (r: T) => string }   // ISO 日期
  | { kind: 'numRange';  key: string; label: string; get: (r: T) => number | null; unit?: string }

useTable<T>(rows: Ref<T[]>, opts: {
  columns: ColumnSpec<T>[]
  filters?: FilterSpec<T>[]
  keyword?: { label: string; placeholder?: string }
  pageSize?: number                                // 默认 20
  initialSort?: { key: string; dir: 'asc' | 'desc' }
}): {
  pagedRows: ComputedRef<T[]>
  displayedCount: ComputedRef<number>              // 过滤后行数
  totalCount: ComputedRef<number>                  // 原始行数
  columns: ComputedRef<ColumnSpec<T>[]>
  filterSpecs: FilterSpec<T>[]
  filterState: Ref<Record<string, unknown>>
  searchText: Ref<string>
  page: Ref<number>
  pageSize: Ref<number>
  totalPages: ComputedRef<number>
  sortKey: Ref<string | null>
  sortDir: Ref<'asc' | 'desc' | null>
  toggleSort(key: string): void                    // 无→升→降→无
  setPage(n: number): void
  setPageSize(n: number): void
  reset(): void                                    // 清空搜索/筛选/排序并回第 1 页
}
```

**处理流水线**：`rows → 关键词（遍历 searchable 列）→ 各 filter → 排序 → 分页`。

**排序**：`sortValue` 优先，缺省回退到该列文本；数值优先、稳定比较；`sortDir=null` 时保持原始顺序。

**响应式规则**：搜索/筛选/排序任一变化时自动重置 `page=1`；`pageSize=0` 表示「全部」（不分页）。

### 3. 展示组件

**`TableToolbar.vue`**
- 关键词输入框（防抖 200ms，`v-model` 绑定 `searchText`）
- 按 `spec.kind` 渲染控件：`select`（下拉，静态或由数据去重生成选项）、`dateRange`（两个 `date` 输入）、`numRange`（两个 `number` 输入，可带单位）
- 「重置」按钮（调用 `reset()`）；右侧「显示 N / 共 M 条」
- 无筛选且无关键词时不渲染空壳

**`TablePager.vue`**
- 上一页 / 下一页、页码跳转输入、「第 x / y 页 · 共 N 条」
- 每页条数下拉：20 / 50 / 100 / 全部
- 复用现有 `.pagination` / `.page-btn` 与设计 token（`var(--ink)`、`var(--accent)` 等）

### 4. 各表接入配置

| 表 | 可排序列 | 筛选器 | 关键词覆盖 |
|---|---|---|---|
| 交易明细 | 时间、数量、价格、金额、佣金、印花税（策略/信号来源按文本） | 买卖、信号类型、时间范围、金额范围 | 代码 / 信号来源 / 策略名 |
| 模块十二 下跌周明细 | 日期、基准、策略、超额 | 分类、时间范围、基准区间、超额区间 | 分类 |
| 决策事件·聚合摘要表 | 触发次数、首次、末次、涉及股票、请求/实发 | 层、动作 | 闸门名 / 参数名 |
| 决策事件·下钻明细表 | 时间、阈值、实际 | 时间范围 | 代码 / 原因 |
| 危机 Alpha（自然回撤/大跌月/年度） | 数值列 | 时间范围 + 该表分类 | 相关文本列 |
| 市场状态各周频表 | 数值列 | 时间范围 | — |
| 行业归因 / 稳健性表 | 数值列（小表，可选接入） | 视列而定 | — |

固定行数的汇总表（关键指标、评分、汇总 KPI 等）**不接入**。

### 5. 边界情况

- 过滤后 0 行：显示空状态（复用现有 `.empty-state` 文案）
- 页码越界：`page` 上限钳制到 `totalPages`
- 切换回测记录：`useTable` 实例随记录重载重建（state 重置），不跨记录保留筛选
- 格式化字符串排序：所有可能含 `+/-/%` 的列必须提供 `sortValue` 原始数值

---

## Part 2：数值精度策略

### 6. 统一精度表

| 数据类 | 精度 | 示例 |
|---|---|---|
| 金额 / 资金（元） | 2 位 + 千分位 | `1,369,882.73` |
| 价格 | 3 位（前复权） | `7.420` |
| 数量（股） | 整数 + 千分位 | `27,100` |
| 比率 / 收益率 / 回撤 / 胜率 / 占比 | 百分比 2 位 | `6.23%` |
| 阈值 / 比例参数 | 百分比 2 位 | `5.00%` |
| 指标（夏普/卡玛/索提诺/PF/Beta/IR/波动率） | 2 位 | `1.24` |
| 相关系数 | 2 位 | `-0.31` |
| 天数 | 0～1 位整数 | `37` |
| 佣金 / 印花税 | 2 位 | `50.27` |

四舍五入（显示层 ROUND_HALF_UP）。

### 7. 后端

新增 `main/core/engine/format.py`（纯函数）：`fmt_money`、`fmt_price`、`fmt_qty`、`fmt_pct`、`fmt_ratio`、`fmt_signed_pct`。

将所有 `%s` 拼 `Decimal` 的 message/reason 字符串替换为 `fmt_*()`，例如：

`main/core/engine/risk_manager.py:232`
```python
# 现在
"日内盈亏 %s（基准 %s）亏损 %.2f%% 触发当日暂停" % (daily_pnl, self.prev_close_value, float(loss) * 100)
# 改为
"日内盈亏 %s（基准 %s）亏损 %s 触发当日暂停" % (
    fmt_money(daily_pnl), fmt_money(self.prev_close_value), fmt_pct(loss))
# → 日内盈亏 -62,322.47（基准 1,369,882.73）亏损 6.23% 触发当日暂停
```

已知落点（后续实现时全量扫描补全）：
- `risk_manager.py`（`:177` max_drawdown、`:232` daily_loss）
- `portfolio.py`（`:223` 止损、`:240` 止盈、`:260` 移动止损、`:320` REDUCE、`:370` OPEN、`:388` ADD、`:411` ADD）
- `execution_engine.py`（`:158`、`:169`、`:237`、`:246`）
- `live/breaker.py`（`:107`、`:269`）

**不动**：API 数值字段（`_f()` 保持原始精度）、DB 存储。

### 8. 前端

新增 `web/src/utils/format.ts`：`fmtMoney`、`fmtPrice`、`fmtQty`、`fmtPct`、`fmtRatio`、`fmtSignedPct`。

- 将 `Backtest.vue` 现有本地助手 `pct` / `pct1` / `pctSigned` / `num` 迁移到 `format.ts` 并统一调用
- 收敛散落的 `.toFixed()` / `.toLocaleString()` / 裸 `{{ }}` 数字渲染到 `format.ts` 助手
- 交易明细列改为：价格 `fmtPrice`、金额 `fmtMoney`、数量 `fmtQty`、佣金/印花税 `fmtMoney`（去掉行内硬编码 `toFixed`）

### 9. 测试

- 后端：新增 `main/core/tests/unit/test_format.py`（边界：None/0/负数/大额千分位/比率）；新增/扩展 message 断言，正则 `\.\d{4,}` 确保关键 message 不含超长小数
- 前端：新增 `web/src/__tests__/format.test.ts`；新增 `useTable` 单测（排序三态、各类筛选、分页钳制、重置）
- 回归：`uv run pytest core/tests/`、`npx vitest run` 全绿

---

## 验证

- `uv run pytest core/tests/` 全绿
- `npx vitest run` 全绿
- 手动：交易明细可按金额排序、按代码搜索、按买卖/信号过滤；下跌周明细可按超额排序/按分类过滤；决策事件原因列不再出现超长小数
