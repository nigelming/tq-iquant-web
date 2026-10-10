# 回测报告 A 股红涨绿跌扩展 + 超额收益曲线 — 设计

日期：2026-09-19
分支：`feat/backtest-return-heatmap`
范围：前端 `web/src/views/Backtest.vue`（纯前端改动，后端零改动）

## 背景与动机

收益分布区块已落地 A 股红涨绿跌（月度热力图 inRange `['#10b981','#fef3c7','#ef4444']`、年收益柱状图正收益红/负收益绿）。用户要求将同一语义扩展到「策略对比分析、回撤曲线」等收益类栏目，并为净值曲线新增「超额收益（相对基准）」参考线，同时确认基准默认沪深 300 且可自定义。

## 需求（已与用户确认）

1. **红涨绿跌扩展**（覆盖收益类指标 + 回撤；交易明细买卖方向颜色不动）：
   - 策略对比分析指标文本：收益类正红负绿
   - 回撤曲线：红色 → 绿色（跌幅 → 绿）
   - 摘要卡：positive（总收益/年化收益）边框绿 → 红；danger（最大回撤）保留红作风险警示
2. **净值曲线新增超额收益曲线**：固定色单曲线，`超额收益 = 组合累计收益率% − 基准累计收益率%`，仅在 `hasBenchmark` 时绘制。
3. **左右轴**：不需要。净值曲线所有曲线（组合/策略/基准/超额收益）均为同一「收益率(%)」量纲、0 起始、同一日期轴，单 y 轴直接对比。
4. **基准默认沪深 300 + 可自定义**：已存在于回测配置层（`Portfolios.vue` 输入框默认 `000300.SH` → `portfolio_strategy.benchmark_index` → 回测时 TQ 拉指数日线 Close → 快照 `benchmark_value`）。本次**无需改动**，仅确认该链路。不做指数代码查找列表（YAGNI）。

## 方案选择

- **方案 A（已选）**：纯前端最小改动。后端零改动。
- 方案 B（前端 A + 后端详情带基准代码展示）未选——用户暂不需要在图上显示基准代码。
- 方案 C（多基准同时对比）未选——需改拉取/存储/评估链路，YAGNI。

## 实现设计

> 详见 `web/src/views/Backtest.vue`。以下行号以当前分支为基准（2026-09-19）。

### 1. 超额收益曲线（净值曲线）

- 新增 computed：

```ts
// 超额收益 = 组合累计收益率% − 基准累计收益率%（逐点）；基准 null 处超额也为 null
const excessCurve = computed<Array<number | null>>(() => {
  const p = portfolioReturnPct.value
  const b = benchmarkCurve.value
  return p.map((pv, i) => {
    const bv = b[i]
    if (bv === null || bv === undefined) return null
    return Number((pv - bv).toFixed(4))
  })
})
```

- `initEquityChart`：在基准 series 之后追加：

```ts
if (hasBenchmark.value) {
  series.push({
    name: '超额收益（相对基准）',
    type: 'line',
    data: excessCurve.value,
    smooth: true,
    symbol: 'none',
    // 固定金色，区别于组合紫 / 策略自动色 / 基准灰虚线
    lineStyle: { width: 1.5, color: '#f59e0b' },
    itemStyle: { color: '#f59e0b' },
  })
}
```

- `legendData` 在 `'基准'` 之后 push `'超额收益（相对基准）'`。
- tooltip 现有 `params.forEach` 自动覆盖新 series，无需改动；null 值已显示 `—`。
- y 轴保持单轴「收益率(%)」，不设左右轴。

### 2. A 股红涨绿跌扩展

- `valueClass`（约 L170）：

```ts
function valueClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return Number(v) >= 0 ? 'text-red' : 'text-green'
}
```

  影响：策略对比卡片 `strategyCards` + 组合整体卡 `portfolioMetrics` 中调用 `valueClass` 的收益类指标（总收益/年化收益等）。风险指标 `最大回撤/VaR/CVaR` 仍为硬编码 `text-red`（风险警示，不改）。

- 回撤曲线（`initDrawdownChart`，约 L447-467）：`itemStyle.color` `#ef4444` → `#10b981`；面积渐变 `rgba(239, 68, 68, 0.3/0.05)` → `rgba(16, 185, 129, 0.3/0.05)`。

- 摘要卡 CSS（约 L958-959）：

```css
.summary-card.positive { border-left-color: #ef4444; } /* 正收益 → 红 */
.summary-card.danger { border-left-color: #ef4444; }   /* 最大回撤 → 风险红，保持 */
```

- 不动：交易明细表买卖方向色（L874 `BUY→text-green / SELL→text-red`）。

## 测试（TDD）

> 涉及 `web/src/__tests__/Backtest.test.ts`。现有相关测试：
> - L294「净值曲线含基准线」断言 legend/data 基准存在——新增超额收益曲线不应破坏。
> - 收益分布区块测试（L360+）、红涨绿跌配色测试（L401+）、数值精度测试（L457+）。

新增用例（先红后绿）：

1. **策略对比收益类指标红涨绿跌**：`buildMetrics` 供策略卡片/组合卡渲染，正收益 `text-red`、负收益 `text-green`。通过 mock 一个含负收益的 `strategy_evaluations` 断言渲染 class。
   - 入参：`mockDetail` 扩展 `strategy_evaluations`（如 `annual_return: -0.05`）或独立 mock。
2. **回撤曲线绿色**：`initDrawdownChart` series `itemStyle.color === '#10b981'`。（已有「回撤曲线数据 round 到 4 位」测试 L486 可参考其取 option 方式。）
3. **超额收益曲线**：`hasBenchmark`（快照带 benchmark_value）时 legend 含「超额收益（相对基准）」、series `data === [0, -1, -2]`（mockDetail 手算：组合收益% − 基准%）；无基准（benchmark_value 全 null）时不出现该 series。
4. **摘要卡边框色**：断言 `.summary-card.positive` 的边框色为 `rgb(239, 68, 68)`（`getComputedStyle`）或等价检查。

## 验证命令

前端（`web/` 目录）：

- `npx vitest run`（全量，含新增用例）
- `npx vue-tsc --noEmit`
- `npm run build`

后端不需要跑测试，无改动。

## 不做的事

- 不加左右轴（见需求 3 理由）。
- 不改交易明细买卖颜色。
- 不做指数代码下拉列表 / 展示基准代码（方案 B 未选）。
- 不改 `build_benchmark_data` / 快照 / 评估器 / 数据库。