# 回填老回测记录的每日持仓明细（positions_json）— 设计

日期：2026-09-28

## 背景

`Portfolio.positions_detail()` 写入 `backtest_daily_snapshots.positions_json` 的功能在
提交 `b7bc21c`（2026-09-27 01:33）才落地。**当前数据库 10 条回测记录的组合快照
`positions_json` 全部为 NULL**（15,022 条），因为全部早于该功能。

后果：栏目「🚀 机遇Alpha · ⚖️ 中性Alpha 专题分析」的第八节（行业归因）在
`_build_industry_attribution`（`main/core/services/backtest_service.py:702`）与
`compute_industry_attribution`（`main/core/engine/industry_attribution.py:49`）的双门禁处
被拦下，所有记录都只能显示「该记录回测时未保存持仓明细，重新回测后可用」。

历史决策曾明确「老数据不回填」（`.trae/specs/regime-alpha-suite/spec.md:20,42,62-63`）。
本设计推翻该决策：重跑 10 条长周期回测代价过高，而数据足以重建。

## 目标与验收标准

1. 10 条记录 / 15,022 条组合快照的 `positions_json` 全部补齐。
2. 每条记录回填后 `recompute_market_regime` 成功，栏目八不再是「需重新回测」。
3. **现金列 100% 精确复现**：回填重放出的现金与快照已存的 `cash` 列逐日完全一致。
4. 缩放残差 `|k−1| ≤ 5%`；超阈值的记录拒绝写入（除非 `--force`）。
5. 全程 dry-run 优先：默认不写库，报告可读。

## 实证得到的关键事实

以下均由探针在真实 `main/data/dev.db` 上验证，非推断。

### 快照含当日成交

回测里 `snap_date = D` 的快照**已包含** `bar_time.date() == D` 的成交。对 `snap_date D`
必须先应用 `bar_time.date() <= D` 的全部成交。

现金列比对（零漂移信号，`amount`/`commission`/`stamp_duty` 直接参与）：

| 记录 | 快照数 | 含当日成交 | 不含当日成交 |
|---|---|---|---|
| 11 | 662 | **662/662** | 466/662 |
| 8 | 3091 | **3091/3091** | 2219/3091 |
| 3 | 3080 | **3080/3080** | 2278/3080 |

### 表结构约定

- `backtest_trades` **无 `trade_date` 列**，交易日只能取 `bar_time.date()`
  （`main/core/models/backtest_trade.py:23`）。
- `trade_type` 取值为大写 `"BUY"` / `"SELL"`
  （`shared/tq_iquant_shared/constants.py:14-16`；落库取 `.value`，见
  `backtest_service.py:1235`）。`regime_alpha.py:236` 的 `.lower()` 是兼容写法。
- 成交归属**只有 `strategy_id`**（`backtest_trade.py:12`），无 `portfolio_strategy_id`。
- 组合快照过滤照抄 `filter_by(backtest_record_id=..., target_type="portfolio")`，
  `target_id` 不参与（`backtest_service.py:844-849`）。

### 可复用的取价函数

`build_index_close_series(codes, start, end, batch_size=0)`
（`backtest_service.py:319-367`）名字里的 "index" 只是历史命名，其 `stocks` 参数接受任意
代码列表（含个股），返回 `{code: {date: Decimal}}`。**回填取收盘价用它**，不用
`build_klines`——后者的股票来自 `ps.stock_pool_id`（老回测的股票池可能已变更），
且会附带公式预热段的额外 TQ 调用。

注意该函数**静默吞 TQ 异常**（`:342-343`），回填需自行包一层以区分「代码无数据」与
「取数异常」。

### 可复用的持仓语义

`Position.buy()` / `Position.sell()` / `Position.available_shares_on()`
（`main/core/engine/position.py:25,41,94`）可直接复用：

- T+1 分桶：`self._lots: Dict[date, int]`，`available_shares_on` 用**严格小于**
  `sum(qty for d, qty in self._lots.items() if d < query_date)`，买入当日不可卖。
- 卖出按 `sorted(self._lots.keys())` **先进先出**扣减，不检查 T+1（卖出合法性由
  `SimulatedT1Checker` 单独管）。
- `buy()` 的 `avg_cost` 用**原始成交价**加权，不扣佣金 → 重放得到的 `cost_value`
  与原始回测完全一致。
- `apply_reverse` 在回测链路中不产生 trade 记录，故 trades 序列自洽，无需处理撤回。

## 组件划分

新建 `main/core/services/positions_backfill.py`（不塞进已 1300+ 行的
`backtest_service.py`）。四个单元，I/O 边界清晰，前三个为纯函数可离线单测。

| 函数 | 职责 | 依赖 |
|---|---|---|
| `replay_cash(trades, snap_dates)` | 纯算术重放**累计**现金流，返回 `{date: Decimal}`（不依赖初始资金，见下） | 无 |
| `replay_positions(trades, snap_dates)` | 复用引擎 `Position` 重放持仓，返回 `{date: List[dict]}` | 无 |
| `value_positions(pos, close, stored_mv)` | 结转上次价 → 终态回退 `avg_cost` → 按存量市值缩放，返回 `(detail, k, notes)` | 无 |
| `backfill_record(db, record_id, force=False)` | 编排：读库 → 取价 → 调上述三步 → 校验 → 写库 | DB + TQ |

### `replay_positions` 契约

- 内部按 `(strategy_id, stock_code)` 维护独立 `Position` 实例（与引擎一致，策略层持仓
  独立于组合层），组合层再跨策略按 `stock_code` 聚合。
- 产出与 `Portfolio.positions_detail()`（`portfolio.py:459-502`）**同构**的 5 字段：
  `stock_code` / `quantity` / `sellable_quantity` / `market_value` / `cost_value`，
  Decimal 序列化为字符串，按 `stock_code` 升序，空仓写 `"[]"`。
- `market_value` / `cost_value` 本函数内**先不算**（无价格输入），由 `value_positions` 填。
  为保持字段齐全，此处填 `cost_value = avg_cost × quantity`（纯持仓数据，无漂移），
  `market_value` 留待估值步骤。

### 缺价处理（用户已确认）

1. 当天无 bar（停牌 / 非交易日）→ 结转该股**最近一个已知收盘价**。
2. 该股**全程取不到任何价格** → 回退 `avg_cost`，与 `Portfolio.positions_detail`
   现有的 `price is None or price <= 0 → price = pos.avg_cost` 口径一致。
3. 两种情况都计入报告（结转次数 / avg_cost 兜底次数 / 全程无价代码列表）。

## 估值口径（用户已确认：重算后按存量市值缩放）

事后从 TQ 取到的是**今日前复权价**，与回测当时的价格存在除权漂移（期间分红除权会
改写历史前复权价）。若直接写入重算值，行业权重会整体偏离。

`value_positions` 的处理：

1. 逐股 `market_value_i = price_i × quantity_i`（price 按上述缺价策略取得）。
2. `raw_sum = Σ market_value_i`。
3. 若 `raw_sum > 0` 且快照存了 `market_value`：缩放系数
   `k = stored_market_value / raw_sum`，所有 `market_value_i ×= k`。
4. 若 `raw_sum == 0`（空仓日）→ 写 `"[]"`，不缩放。
5. **若 `stored_market_value` 为 0 或 NULL**（理论上不该发生——有持仓就有市值）→
   跳过缩放，保留重算值，并把该日计入 `notes` 告警，不阻断。
6. 返回 `k` 供门限判定与报告。
7. **落库前把 `market_value` 量化到 2 位小数**（`quantize(Decimal("0.01"))`）。`k` 是
   无限循环小数（3000/2700），不量化会写出 `999.9999999999999999999999999` 这种值，
   也与库中 `Numeric(15,2)` 列的精度约定不符。量化后 Σ 与存量市值的残差
   ≤ 0.005 × 持仓数，对行业权重（÷`total_value`）的影响在 1e-6 量级，可忽略。
   `cost_value` **不量化**，严格用 `str(avg_cost × quantity)`，与
   `Portfolio.positions_detail()` 逐字节一致。

**为何缩放是正确而非取巧**：`_daily_industry_weights`
（`industry_attribution.py:84-103`）用 `market_value / total_value` 算日权重，绝对
标度本会被约掉，但**标度不齐会使 `Σw ≠ 1`**，导致行业占比整体偏低/偏高。缩放让
`Σ market_value` 精确等于回测自身的存量市值，权重之和为 100%，与记录其余部分自洽。
`cost_value` 不缩放（它没有对应的存量真值，且不参与行业归因）。

## 写库前的校验（双信号）

| 信号 | 期望 | 性质 |
|---|---|---|
| 现金复现 | 与快照 `cash` 列逐日完全一致（容差 0.01） | 零漂移，**硬断言**，不通过则该记录整体失败 |
| 缩放系数 `k` | `\|k−1\| ≤ 5%` | 含除权漂移，超阈值拒绝写入 |
| Σ 市值 | `= 快照 market_value` | 缩放后恒等 |
| 缺价 | 结转命中数 / avg_cost 兜底次数 / 全程无价代码 | 报告项，不阻断 |

现金复现是这道设计里最强的保证：它对**全部 15,022 个交易日**做零漂移精确校验，
一旦持仓重放的数量或时序有误，必然立刻暴露。

### 现金校验的「与初始资金无关」表述

`replay_cash` 不知道也不需要知道初始资金（回测初始现金由策略参数决定，DB 里没有
权威副本）。因此校验用**差分**表述，而非绝对值：

```
对每个 snap_date i：  stored_cash[i] - stored_cash[0]  ==  cumflow[i] - cumflow[0]
```

`cumflow[i]` 为第 0 日至第 i 日全部成交的现金流累计（买入 `-(amount+commission+stamp_duty)`，
卖出 `+(amount-commission-stamp_duty)`）。推导：`stored[i] = 初始现金 + cumflow[i]`，
两式相减即消掉初始现金。

**必须减 `cumflow[0]` 而非直接比 `cumflow[i]`**：后者隐含「首条快照不含当日成交」。
实测当前库 10 条记录的首条快照当日成交数均为 0，该前提成立；但换成含首日成交的
回测就会误判为「现金不自洽」而拒写。`cumflow[0] == 0` 时两式等价，故此修正
不改变任何现有数据的结论，只是去掉一个未验证的假设。

## CLI

新建 `main/scripts/backfill_positions_json.py`，遵循 `main/scripts/` 现有
`verify_*.py` 约定（文件头 docstring 写用途/背景/判定标准/用法、argparse、
`sys.exit(main())`、`\n + "=" * 70` 分节、`[OK]`/`[FAIL]` 前缀）。

- 默认 **dry-run**，只打印每条记录的对账表，不写库。
- `--apply` 才写库；`|k−1| > 5%` 的记录即使 `--apply` 也跳过并列出，需 `--force` 覆盖。
- `--record 11` 处理单条 / `--all` 处理全部 10 条。
- `--recompute` 在回填后逐条调 `recompute_market_regime(db, id)`，让栏目八出现
  （用户已确认自动重算）。代价：10 条记录 × 128 个行业指数取数，需通达信保持运行。
- 脚本需自行 `sys.path.insert(0, <repo>/main)` 才能 `import core.*`——仓库现有
  `main/scripts/` 下 4 个脚本都不 import `core.*`，无此先例，需在 docstring 注明。

## 测试

新建 `main/core/tests/unit/test_positions_backfill.py`：

- `replay_cash`：含当日成交口径、commission/stamp_duty 参与、跨日累积、空仓日。
- `replay_positions`：T+1 分桶（买入当日不可卖）、FIFO 扣减、跨策略同股聚合、
  `stock_code` 升序、空仓写 `"[]"`、字段与 `positions_detail` 同构。
- `value_positions`：结转上次价、`avg_cost` 兜底、按存量市值缩放、`k` 越界、
  空仓日不缩放。

另加一条集成测试：造快照 + 成交 fixture，断言 `positions_json` 落库形状与
`positions_detail` 一致、`recompute_market_regime` 能越过双门禁。

## 明确不做

- 不新增 API 端点与 UI 按钮（一次性历史数据修复，新回测不会再触发）。
- 不改回测引擎——新回测已正确落 `positions_json`。
- 不改 `NEEDS_RERUN_REASON`（`industry_attribution.py:24`）与
  `html_report.py:527` 的门禁文案：回填后这些记录自然不再命中，未回填的记录仍需
  保留该提示。
- 不删 `.trae/specs/regime-alpha-suite/spec.md` 里的历史决策记录，仅在本文档说明推翻。

## 风险

| 风险 | 缓解 |
|---|---|
| 持仓重放的数量/时序有误 | 现金列逐日硬断言，全量 15,022 日覆盖 |
| 今日前复权价与回测当时偏离过大 | `\|k−1\| ≤ 5%` 门限 + 默认 dry-run + 报告最大误差日期 |
| 停牌日市值失真 | 结转上次价（优于直接用 `avg_cost`）；报告结转次数 |
| 取价静默失败（`build_index_close_series` 吞异常） | 编排层显式探测缺失代码并列入报告 |
| TQ 长时间占用 | 逐条记录处理，单条失败不影响其余；`--record` 可分批 |
