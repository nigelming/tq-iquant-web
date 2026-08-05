# 待明确问题清单（Open Questions）

> 本文件记录开发过程中**尚未明确、需要后续决策或验证**的问题。每条问题独立编号（Q1、Q2…），
> 跨模块、跨阶段累积，不限于当前任务。问题解决后标注结论与日期，不删除（保留决策历史）。
>
> 新增问题格式见文末「模板」。

---

## Q1 实盘持仓如何映射多组合多策略

**状态**：待决策
**来源**：2026-08-05 实盘卖出链路讨论
**关联**：Q2（卖出处理）、Q3（T+0/T+1）、0009 HTTP 桥、0010 公式注入

### 背景

实盘是**一个真实券商账户对多组合多策略**，而回测是每个 `StrategyContext` 各自维护独立持仓。

- 回测模型（现状代码）：
  - 每个组合 1 个 `Portfolio` → 1 个 `Account`（独立现金）
  - 每个 `StrategyContext` 有自己的 `positions: Dict[code, Position]`——同一只股票被组合内 A、B 两个子策略各自持有，是两个独立的 `Position` 对象
  - 卖出时 `_signal_to_order` 取 `ctx.positions.get(code)` → 只动本策略那一份，天然隔离
  - `LiveOrder/LiveTrade` 表带 `portfolio_strategy_id + strategy_id`，数据层有归属
- 实盘现实：
  - 一个券商账户，桥 `passorder(1101, account, code, ...)` 只认 `code`，不知道也不在乎哪个策略/组合
  - `/positions` 返回的是**账户总持仓**：`{instrument, volume, available}`，没有策略归属

### 待明确

1. **归属来源**：`LiveTrade(portfolio_strategy_id, strategy_id, stock_code, ...)` + `recover()` 重放重建 `ctx.positions` 是否作为唯一可信归属来源？还是需要额外的账户级聚合视图？
2. **对账**：Core 的策略持仓按 `code` 聚合后，与桥 `query_positions(code).volume` 如何、何时比对？不一致（桥拒单未回填、部分成交、手动操作）时如何处理？
3. **同一只股票多策略持仓的聚合**：组合 A 策略 s1 持 600 股 + 组合 B 策略 s2 持 400 股 → 账户总持仓 1000 股。桥端只看到 1000，Core 如何维护「哪个策略能卖多少」？

### 相关代码位置

- `main/core/engine/strategy_context.py:38` — `ctx.positions: Dict[str, Position]`（每策略独立持仓）
- `main/core/engine/portfolio.py:140` — `_signal_to_order` 取 `ctx.positions.get(sig.stock_code)`（只卖本策略）
- `main/core/engine/live_engine.py:367` — `recover()` 重放 `LiveTrade` 重建持仓
- `main/core/models/live_trade.py` — `portfolio_strategy_id + strategy_id + stock_code` 归属字段
- `live/bridge/iquant_bridge.py:171` — `query_positions` 返回账户总持仓（无策略归属）

---

## Q2 实盘卖出如何处理（量与隔离）

**状态**：待决策
**来源**：2026-08-05
**关联**：Q1（持仓映射）、Q3（T+0/T+1）

### 背景

卖出信号规则：**只能卖出「本子策略（且组合匹配）对应的股票」**。回测已实现（`_signal_to_order` 取 `ctx.positions.get`），实盘需补三件事：

### 待明确

1. **卖出量上限**：取以下约束的较小值？
   - 信号要卖的量（`order.quantity`）
   - 本策略持有量（`ctx.positions[code].quantity`，组合隔离）
   - 券商可用量（桥 `query_positions(code).available`，体现 T+0/T+1，见 Q3）
2. **同 bar 多策略超卖**：同一根 bar 内，A 策略先卖 600、B 策略再卖 400，但券商 `available` 只有 800。第二次查若拿到旧缓存（仍 800）会超卖。是否需要「bar 内可用量递减记账」？
3. **桥拒单与账面背离**：Core 已记账减仓，但券商端拒单（T+1 当日买、限额等）→ 虚拟持仓与真实持仓背离。如何修正？（依赖 /deals 回填，见 0009 切片5）

### 相关代码位置

- `main/core/engine/execution_engine.py:109` — SELL 分支走 `t1_checker.get_available_shares`（回测用，实盘当前 `LiveT1Checker` 全量放行）
- `main/core/engine/portfolio.py:157` — 全平类/REDUCE 卖出量计算
- `main/core/engine/http_bridge_dispatcher.py:67` — `place_order` 桥下单（只传 code，无归属）

---

## Q3 实盘 T+0/T+1 判定（不建字段方案）

**状态**：待验证前置假设
**来源**：2026-08-05
**关联**：Q2（卖出处理）

### 背景

- **股票**：T+1（当日买不可卖）
- **ETF**：**部分 T+0、部分 T+1**——不是所有 ETF 都 T+0
  - 跨境 ETF（如 513100 纳斯达克 100）：T+0
  - 货币/债券/黄金 ETF：T+0
  - 普通宽基/行业 ETF（如 510300 沪深 300、512000 券商 ETF）：**T+1**
  - 可转债：T+0（本项目暂不涉及）
- 代码现状：**完全没有品种类型标记**。`stock_utils.validate_stock_code` 只校验 6 位代码 + 后缀；`Strategy`/`StockPoolStock` 无品种字段；桥 `passorder` 不传品种类型。
- 用户决策：**不建立字段区隔 T+0 还是 T+1**（2026-08-05）。排除了 `StockPoolStock` 加品种字段、shared 常量白名单等方案。

### 选定方向（待验证）

靠券商返回的**可用量** `m_dAvailable` 体现 T+0/T+1，Core 不自己判品种：
- T+1 品种当日买入 → 券商 `available=0` → Core 不下单
- T+0 品种 → 券商 `available=全量` → Core 正常卖
- 卖出量取 `min(order.quantity, 本策略持有量, bridge_available[code])`

### 前置验证（必须真机确认，未做）

桥 `query_positions` 返回的 `m_dAvailable`（`iquant_bridge.py:182`）是否：
1. 准确反映 T+1（当日买入不计入 available）
2. ETF 的 T+0 是否体现在 available 全量
3. 是否实时刷新（下单后立即变，还是要等成交回报）

**若 `m_dAvailable` 不准**（如恒等于 volume、延迟刷新）→ 方案塌，只能退回「券商端拒单 + /deals 回填修正」，接受账面有短暂背离窗口。

### 验证方式（待执行）

10 分钟真机测试：建一笔买入成交后立即查 `/positions`，对比买前买后 `available`；对一只 T+0 ETF 做同样测试。

### 相关代码位置

- `live/bridge/iquant_bridge.py:171` — `query_positions` 返回 `m_dAvailable`
- `main/core/engine/execution_engine.py:79` — `LiveT1Checker` 当前全量放行（待改）
- `shared/tq_iquant_shared/stock_utils.py` — 仅代码校验，无品种信息

---

## 模板

新增问题时复制以下结构：

```markdown
## Qn 问题标题

**状态**：待决策 / 待验证 / 已解决
**来源**：YYYY-MM-DD 来源（讨论/任务/PR）
**关联**：Qm、计划编号等

### 背景

问题描述、现状代码、为什么需要明确。

### 待明确 / 决策

- 列出需要回答的具体问题
- 或已选方案及其前提条件

### 相关代码位置

- `path:line` — 说明
```

问题解决后，在标题下补「**结论**：……（日期）」并改状态为「已解决」，保留原文不删除。
