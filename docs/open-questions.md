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

**状态**：2 已定并实现；1/3 已定，见对应实现
**来源**：2026-08-05
**关联**：Q1（持仓映射）、Q3（T+0/T+1）

### 背景

卖出信号规则：**只能卖出「本子策略（且组合匹配）对应的股票」**。回测已实现（`_signal_to_order` 取 `ctx.positions.get`），实盘需补三件事：

### 待明确

1. **卖出量上限**：取以下约束的较小值？
   - 信号要卖的量（`order.quantity`）
   - 本策略持有量（`ctx.positions[code].quantity`，组合隔离）
   - 券商可用量（桥 `query_positions(code).available`，体现 T+0/T+1，见 Q3）
   **已定**：min 三者。`cap_quantity` SELL 分支 `t1_checker.get_available_shares` 取 `min(策略持有量, 桥可用)`，信号量经此 cap（F5）。
2. **同 bar 多策略超卖**：同一根 bar 内，A 策略先卖 600、B 策略再卖 400，但券商 `available` 只有 800。第二次查若拿到旧缓存（仍 800）会超卖。是否需要「bar 内可用量递减记账」？
   **已定（2026-08-10, F6）**：需要。`_refresh_available_map` 每 bar 重设快照；`LiveT1Checker.consume_available` 在 SELL 发单成功（`_handle_bar` ③桥受理后）扣减——A 卖 600 后 B 只见 200，不超卖；拒单/失败不扣，扣过量钳到 0，券商端仍兜底。
3. **桥拒单与账面背离**：Core 已记账减仓，但券商端拒单（T+1 当日买、限额等）→ 虚拟持仓与真实持仓背离。如何修正？（依赖 /deals 回填，见 0009 切片5）
   **已定**：切片5 时序 submitted 阶段不 apply，真实成交由 `_poll_deals`/`_backfill_order` 按 `m_strOrderRef` 回填确认后 `_apply_filled_trade` 落持仓；拒单置 `status=rejected` 不 apply（G2/G6）。回填频率独立 5s（G5）。

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

## Q4 倍数周期内存注入方式与 count

**状态**：已解决（1w/1mon 桥端拉不到，改走通达信放行）
**来源**：2026-08-06 实盘流程逐点确认 C3
**关联**：0010 公式注入、live-flow-checklist C3/C6

### 背景

通达信分钟级周期:1m/5m/15m/30m 为原生支持(直接拉原生 bar);50m/120m 等 SDK 不认(`periodstr error`)。

### 已验证结论（2026-08-06 真机，回测版 D:/new_tdx64）

`verify_formula_inject.py --periods 1m,5m,15m,30m,1h,2h,4h,8h,1d,1w,1mon,1q,1y`,MACROSSPRO @ 600000.SH,count=200:

| 周期 | 结果 | 说明 |
|---|---|---|
| 1m | ✅ PASS | 1 分钟 |
| 5m | ✅ PASS | 5 分钟 |
| 15m | ✅ PASS | 15 分钟 |
| 30m | ✅ PASS | 30 分钟 |
| 1h | ✅ PASS | 1 小时(不是 `60m`) |
| 2h / 4h / 8h | ❌ FAIL | `periodstr error` —— 小时级仅支持 1h |
| 1d | ✅ PASS | 日线 |
| 1w | ✅ PASS | 周线 |
| 1mon | ✅ PASS | 月线 |
| 1q / 1y | ❌ FAIL | `periodstr error` —— 不支持季线/年线 |

**TQ 公式支持的完整周期列表(8 个)**:`1m / 5m / 15m / 30m / 1h / 1d / 1w / 1mon`,全部内存注入与正常自取**完全等价**(逐变量逐条 Value 全等,且有真实信号)。

**结论**:
1. **通达信支持的周期有边界**:分钟级 1m/5m/15m/30m,小时级仅 1h(无 2h/4h/8h),日级以上 1d/1w/1mon(无季线 1q/年线 1y)。
2. **所有支持周期内存注入与正常自取完全等价**,**链路无需特殊处理**,直接拉原生 bar 注入。
3. **count=200 对所有支持周期均足够**(分钟/小时/日线信号触发充分;月线等长周期历史根数本就少于 200,正常)。

### iQuant 桥端周期(2026-08-07 源码定论,补交集)

实盘 period 一路透传:`ctx.period` → 桥 `/quote?period=` → `xtdata.get_market_data_ex(period=)` 拉 bar → 同一 `ctx.period` → `formula_set_data` + `process_mul_zb` 喂 TQ。**两端必须用同一个 period 字符串**,故实盘可配 = TQ 支持 ∩ iQuant 桥支持。

iQuant 侧有三套 period 说法,**桥实际用 xtdata 那套**(`live/bridge/iquant_bridge.py:_fetch_quote`):

| 来源 | 周期 | 桥用? |
|---|---|---|
| `xtquant/xtdata.py` 本地读取白名单(7 处同一集合,line 310/370/448/517) | `1m, 5m, 15m, 30m, 1h, 1d`(6 个) | ✅ 桥用此 |
| `config/fun.xml` ContextInfo 声明(17 个,含 3m/1q/1hy/1y/mh/mm/md) | ContextInfo 进程内 API | ❌ 桥没用 |
| `A策略.py:41` perioddic | `1d, 1m, 5m, 15m, 30m, 60m`(含 60m 不含 1h) | ❌ ContextInfo 内部 |

**关键差异**:
- **xtdata 白名单是 `1h`,不是 `60m`** —— `60m` 在 xtdata 任何列表里都不存在
- **`1w/1mon` 不在 xtdata 白名单** —— 走 `get_market_data_ex_ori` 远程分支(非本地缓存),源码无特殊处理,**能否拉到未真机验证**(桥连不上 miniQMT 认证,等开盘)

### 实盘可配周期交集表

| 周期 | TQ 公式(真机验) | iQuant 桥 xtdata | 实盘可配? |
|---|---|---|---|
| `1m` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `5m` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `15m` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `30m` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `1h` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `1d` | ✅ | ✅ 白名单本地 | ✅ 稳 |
| `1w` | ✅ | ⚠️ 走远程分支,未真机验 | ⚠️ 待真机 |
| `1mon` | ✅ | ⚠️ 走远程分支,未真机验 | ⚠️ 待真机 |
| `60m` | ❌ `periodstr error` | ❌ 不在 xtdata 列表 | ❌ 两边都不行 |
| `3m/1q/1hy/1y` | ❌ | ⚠️ ContextInfo 有,xtdata 未验 | ❌ TQ 不支持 |

### 决策(2026-08-07)

1. **业务周期约束 — 已定(2026-08-07 更新)**:代码 `VALID_PERIODS` / 前端 `PERIODS` 统一为 **`1m, 5m, 15m, 30m, 1h, 1d, 1w, 1mon`**(8 个)。去掉 `60m`(TQ 与 xtdata 双双不支持),放开 `15m/1h`(TQ 与 xtdata 双双支持)。`1w/1mon` 改走通达信算(见决策3 更新),纳入放行。
2. **`_MINUTE_PERIODS`(索引对齐判定集)— 已定**:`1m, 5m, 15m, 30m, 1h`。语义是「TQ 输出 Date 只标到日、bar 时间带时分、需按 bar_times 索引对齐的周期」—— **1h 含在内**(1h bar 时间带时分 10:00/11:00,走 Date 匹配会落午夜 key 导致引擎查不到信号)。`1d` 走 Date 匹配(日粒度 1:1)。`60m` 移除。注:`_MINUTE_PERIODS` 是**回测**索引对齐用,1d/1w/1mon 走 Date 匹配,故 1w/1mon 不进此集合(回测 1w/1mon 也走 Date 匹配,日级以上粒度)。
3. **`1w/1mon` 改走通达信放行(2026-08-07 更新,推翻原"暂不放行")** —— 桥端真机已验**拉不到**(xtdata 远程分支 `'NoneType' object is not iterable`,见下方真机结论),但 **TQ+通达信本身支持 1w/1mon**(真机验过注入与自取等价)。故 1w/1mon **不走桥 /quote**,实盘 session 启动时直接 `TQFormula.compute(period="1w"/"1mon")`(正常自取链路,formula.py:6,让 TQ 从通达信拉数据算)算一次公式,14:30 与 1d 统一触发下单(→ live-flow-checklist C3 三段式模式 (C)、C6)。回测 1w/1mon 仍走 `compute` 自取(原本就支持)。**VALID_PERIODS 扩到 8 个**。
4. **count 按公式配(2026-08-07 定)**:count 是**公式固有属性**(公式里写了要多少 bar,如含 MA60 至少 60、含要 255 bar 的函数至少 255),非全局统一、非策略级。`Formula` 表加 `formula_count` 字段(default 200),人工按公式内容填最小 bar 数。实盘注入 count 来自该字段。**count 是公式级字段 → 同公式 count 恒定**,故 C4 去重 key 用 `(股票, 周期, 公式)` 三维即可,count 无需进 key(→ live-flow-checklist C4)。前置实现见任务 #27,C4 去重见任务 #28。

### 1w/1mon 桥端真机结论(2026-08-07,解决决策3)

`verify_quote_history.py --periods 1w,1mon --counts 200,200` 真机验 3 股(600000.SH/000001.SZ/510300.SH),桥日志:
```
[BRIDGE] xtdata FAIL 600000.SH 1w: 'NoneType' object is not iterable
[BRIDGE] xtdata FAIL 600000.SH 1mon: 'NoneType' object is not iterable
```

**根因**:`1w/1mon` 不在 xtdata 本地读取白名单 → 走 `get_market_data_ex_ori` 远程分支 → 返回 None → xtdata 内部代码迭代 None 抛 `'NoneType' object is not iterable`(`_fetch_quote` except 捕获打印 `[BRIDGE] xtdata FAIL`)。**不是"空数据"、不是"periodstr error"**。

**定论**:`1w/1mon` iQuant 桥端**拉不到**,但 **TQ+通达信本身支持**(真机验过注入与自取等价)。故实盘 **不走桥 /quote**,session 启动时改走 `TQFormula.compute(period="1w"/"1mon")`(正常自取链路,让 TQ 从通达信拉数据算),14:30 与 1d 统一触发下单(→ live-flow-checklist C3 三段式模式 (C)、C6)。`VALID_PERIODS` 据此**扩到 8 个**`{1m,5m,15m,30m,1h,1d,1w,1mon}`。决策3"等开盘验过再加" → **验过,改走通达信放行**。

### 相关代码位置

- `main/core/api/strategies.py:20` — `VALID_PERIODS` 策略创建/更新校验白名单
- `main/core/api/backtest.py:345` — `_MINUTE_PERIODS` 索引对齐判定集(含 1h)
- `web/src/views/Portfolios.vue:31` — `PERIODS` 前端下拉选项
- `web/src/api/index.ts:84,141,159` — `period` 字段注释
- `main/core/engine/live_engine.py:199` — `_fill_signal_cache` 注入入口,period 来自 `ctx.period`
- `main/core/tq/formula.py:30` — `compute_injected` 链路,`stock_period=period` 同时传 set_data 和 process_mul_zb
- `main/scripts/verify_formula_inject.py` — 验证脚本(已扩 `--periods`,已改回测版路径)
- `live/bridge/iquant_bridge.py:252` — `_fetch_quote` period 直接透传给 `xtdata.get_market_data_ex`

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
