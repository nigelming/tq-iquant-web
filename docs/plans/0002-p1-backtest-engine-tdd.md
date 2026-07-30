# P1 BacktestEngine 逐 bar 引擎设计与 TDD 实施计划

> 状态：设计已确认，待实施
> 日期：2026-07-30
> 接续：对话 a61223a4（P0 完成）→ 本计划为 P1 第一项
> 依据：[system-plan-draft.md](../system-plan-draft.md) §5.3.2/§5.3.3/§5.4/§5.5 + TQ 真实返回格式（见 [tq-market-data-return-format](../../README.md) 记忆）

## 1. 目标

按 TDD 实现回测引擎真实逐 bar 逻辑，打通 `POST /api/backtest` 端到端。最小主链路切片优先：单股票 × 单日线策略 × 少量 bar，验证「信号 → 风控 → 下单 → 快照」主链路。组合级熔断、多策略资金竞争、主从策略、多周期推进留作后续切片。

## 2. 已确认决策

| 决策点 | 选择 | 依据 |
|---|---|---|
| TDD 切入方向 | **自底向上** | 先实现底层组件，再组装 BacktestEngine |
| `StrategyContext.get_signal` 信号来源 | **内部调 TQFormula，signal_cache 优先、TQ 兜底** | 单测注入预填 cache 绕过 TQ |
| `klines` 结构 | `Dict[str, Dict[str, pl.DataFrame]]` = `{stock_code: {period: pl.DataFrame}}` | 与 TQ 多股票×单周期返回对齐 |
| polars DataFrame 列 | `datetime` + `Open/High/Low/Close/Volume/Amount`（首字母大写，保留时间列） | TQ 真实字段名 |
| `BarEvent.stocks` 结构 | `{stock_code: {"open":Decimal,"high":Decimal,"low":Decimal,"close":Decimal,"volume":int}}`（小写键） | OHLCV Decimal 字典 |
| 首个切片范围 | **最小主链路**（单股票×单策略×止损止盈×几 bar） | 早验证，失败易定位 |
| 成交时机 | **下一 bar 的 open 成交**（设计 §5.3.2 第3条） | 引擎维护跨 bar 待执行订单队列 |

## 3. 数据流与逐 bar 循环

```
主进程：TQData.get_history → klines {stock:{period:pl.DataFrame}}  +  signal_cache 预计算
        ↓ ProcessPoolExecutor
子进程 BacktestEngine.run(portfolio, klines, signal_cache, benchmark_data, progress_callback):
    1. 按组合内所有策略的最小周期，合并所有 (stock, period) 的时间轴 → 全局时间点序列 times[]
    2. 维护 pending_orders: List[OrderEvent]（上一 bar 触发、待本 bar open 成交）
    3. for t in times:
         a. 先处理 pending_orders：用 t 的 open 价成交 → ExecutionEngine.execute → 产出 TradeEvent → 更新 Account/Position
         b. 构造 BarEvent(stocks={code:{open,high,low,close,volume}}, bar_time=t)  # 从 polars 取 t 行
         c. portfolio.on_bar(bar):  # 见 §4
              - 各 StrategyContext.get_signal(bar)  # cache 优先 TQ 兜底 → List[SignalEvent]
              - StrategyRiskManager 检查止损/止盈/移动止损 → List[RiskEvent]
              - 信号优先级排序：风控 > 公式；同策略内 CLOSE>REDUCE>ADD>OPEN（§5.3.2 第8g条）
              - 逐信号转 OrderEvent，资金审批 → 入队 pending_orders（下 bar 成交）
         d. 若 t 是某交易日最后一根 bar：portfolio.snapshot() → 记录 daily_snapshot
    4. Evaluator.evaluate(snapshots) → 18 指标
    5. 返回 {trades, snapshots, evaluations}
```

**T+1 实现**：`SimulatedT1Checker` 改为按 Position 的买入时间判断——当天及之前买入的可卖（当前桩返回固定 999999，需修正）。

## 4. 组件职责（自底向上）

### 4.1 Position（已部分实现，补 apply_trade）
- 字段：`stock_code, quantity, avg_cost, highest_price`（齐）
- 现有 `buy/sell/market_value` 保留
- **新增** `apply_trade(trade: TradeEvent)`：成交后统一更新（买→加权 avg_cost + 更新 highest_price；卖→减仓），供 ExecutionEngine 调用
- **新增** `buy_time: datetime`（用于 T+1 判断，首次买入时间）

### 4.2 Account（已部分实现，补双层卡控 + apply_trade）
- 现有 `cash/initial_capital/insufficient_count/approve_order/deduct_cash/add_cash` 保留
- `approve_order` 现 only 检查组合现金；**补策略持仓上限**（capital_ratio × initial_capital 作上限，设计 §5.3.2 第8a条）
- **新增** `apply_trade(trade)`：买扣 cash(amount+commission+stamp_duty)，卖加 cash(amount-commission-stamp_duty)
- `market_value` 当前返回 0；改为需传入持仓市值或由 Portfolio 汇总（见 §4.5）

### 4.3 StrategyRiskManager（已实现，无需改）
- `check_stop_loss/take_profit/trailing_stop` 已正确

### 4.4 PortfolioRiskManager（已部分实现，最小切片暂不启用组合级）
- 最小切片**不触发熔断**（circuit_breaker_active 恒 False），但保留接口
- 后续切片补 max_drawdown/daily_loss_limit 熔断 + 次日恢复 + 累计 3 次转手动

### 4.5 StrategyContext.get_signal（核心新增）
```python
def get_signal(self, bar: BarEvent, signal_cache: dict) -> List[SignalEvent]:
    # 1. cache 优先：key=(strategy_id, stock_code, bar.bar_time)
    # 2. miss → 调 TQFormula.compute（实盘/真实回测）→ 结果填入 signal_cache
    # 3. 按 formula_signals 配置（signal_name/trigger_value/signal_type）转 SignalEvent
```
- 单测注入预填 signal_cache，不调 TQ

### 4.6 Portfolio.on_bar（核心新增）
- 遍历 self.strategies → 各 StrategyContext.get_signal + StrategyRiskManager 检查
- 信号优先级排序（风控 > 公式；CLOSE>REDUCE>ADD>OPEN）
- 逐信号转 OrderEvent，调 ExecutionEngine 资金审批，**入队 pending_orders**（不立即成交）
- 返回 List[OrderEvent] 给 BacktestEngine 入队

### 4.7 ExecutionEngine（已部分实现，修签名 + T+1）
- `execute` 签名补 `portfolio_id`（与设计 5.5.4 一致）
- `SimulatedDispatcher.place_order` 用传入的 open 价（已实现）
- `SimulatedT1Checker` 改按 Position.buy_time 判 T+1
- `reduce_by_ratio` 末尾返回 None 的 bug 修正（实现按比例减仓 OrderEvent）

### 4.8 BacktestEngine.run（核心新增）
- 见 §3 逐 bar 循环
- progress_callback 报真实进度（当前 bar 序号 / 总数）

## 5. TDD 实施步骤（每步：先写失败测试 → 实现 → 绿）

按依赖顺序，每步产出可独立运行的单测：

| 步 | 组件 | 测试要点 | Mock 依赖 |
|---|---|---|---|
| 1 | Position.apply_trade + buy_time | 买/卖成交后 quantity/avg_cost/highest_price/buy_time 正确；T+1 判断 | 无 |
| 2 | Account.apply_trade + 策略上限 | 买扣款/卖回款正确；超策略上限拒绝 | Position |
| 3 | SimulatedT1Checker T+1 | 当天买入不可卖、昨天买入可卖 | Position |
| 4 | StrategyContext.get_signal | cache 命中返回预填信号；miss 调 TQ（mock）；trigger_value 匹配 | signal_cache + mock TQFormula |
| 5 | ExecutionEngine.execute 端到端 | BUY 资金审批→下单→Account/Position 更新；SELL T+1→减仓；不足1手放弃 | Account/Position/Dispatcher |
| 6 | Portfolio.on_bar | 单策略单股票：触发 CLOSE 信号→产出 OrderEvent 入队；止损优先于公式 | StrategyContext(预填 cache) |
| 7 | BacktestEngine.run 最小主链路 | 3 根日线 bar：bar1 触发 BUY→bar2 open 成交→bar3 触发 STOP_LOSS→成交；产出 trades+snapshots 正确 | 全 Mock klines + signal_cache |
| 8 | Evaluator 已有测试复用 | 18 指标从 snapshots 算出 | 现有 test_evaluator.py |
| 9 | POST /api/backtest | 端到端：发起回测→写 backtest_record→进程池跑→写 trades/snapshots/evaluations→查结果 | test_client + 小 Mock |

## 6. 不在本切片范围（后续切片）

- 组合级熔断（max_drawdown/daily_loss_limit）+ 次日恢复 + 累计 3 次手动
- 多策略资金竞争（先到先得）+ 主从策略
- 多周期最小周期推进（本切片仅单周期日线）
- benchmark_data 对比 + benchmark_return 指标
- 公式信号 OPEN/ADD/REDUCE 的资金比例细节（本切片仅 CLOSE 全平 + STOP_LOSS）
- ProcessPoolExecutor 真子进程（本切片先单进程跑通，子进程封装后续加）

## 7. 风险点

1. **polars 时间列**：现有 `tq/data.py` 丢了时间维度，需在数据层补 datetime 列。但本切片用 Mock polars，数据层修正留到 TQ 对接阶段。
2. **跨 bar 待执行队列**：BacktestEngine 维护 pending_orders，首 bar 无待执行单，需处理边界。
3. **快照生成时机**：「交易日最后一根 bar」判断——日线周期下每根 bar 即一日，简单；多周期时复杂（本切片不涉及）。
