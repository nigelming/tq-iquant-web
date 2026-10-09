# 回测 13 项 Bug 修复 — 设计文档

**日期**：2026-10-09
**范围**：`main/core/engine/*`（回测引擎/执行/风控）、`main/core/services/backtest_service.py`、`main/core/api/backtest.py`、`main/core/db.py`、`main/core/main.py`、`main/core/engine/live/*`
**来源**：用户《回测管理潜在 Bug 排查报告》逐条核实（全部确认，含少量细节更正）
**决策**（用户已确认，均取推荐项）：
- #7 仅修资金为负 + 记录告警，不模拟涨跌停
- #1 订单按源周期成交
- #8 daily_loss 统一为日终市值
- #6 每交易日仅末根 bar 一条快照
- #10 实现真实 progress
- #13 改注释对齐代码（不改行为）

---

## 0. 关键约束与影响面

- **无 Alembic 迁移**：全部改动复用现有列（`progress`/`status`），无 schema 变更。
- **历史回测记录与旧指标保持不变**：所有语义修复仅对**新发起**的回测生效。
- **主路径保护**：纯日线单周期回测是主路径，重构后行为须与现状一致（由现有 `main/core/tests/unit/test_backtest_engine.py` 守护）。
- **风险最高**：#1 引擎时间轴重构；其次 #8 实盘侧。

---

## 1. 引擎时间轴重构（#1 / #1b / #6 / #8，同一轮改动）

### 1.1 #1 订单按源周期成交（消除未来函数）

**现状缺陷**：
- 引擎用单一合并时间轴 + 单一价格索引，多周期同刻「后周期覆盖前周期」（`backtest_engine.py:139-168`，docstring 明示 `:144`）。
- 日线 bar 时间戳为当日 00:00，混合周期时排在当日分钟 bar 之前：日线信号（由当日收盘生成）在当日首个分钟 bar 开盘成交 → 未来函数。

**目标语义**：每笔订单在「**下一根同周期 bar**」的开盘成交；各周期时间轴相互隔离。

**设计**：
- 新增 `BacktestEngine._build_price_index_by_period(klines) -> Dict[str, Dict[datetime, Dict[str, dict]]]`：`period_index[period][t][code] = {open,high,low,close,volume}`。**以周期为第一维**，同刻不同周期各占各槽 → 顺带修复 #1b（不再覆盖）。
- `BacktestEngine.run` 主循环改为按周期驱动（复用 `Portfolio.on_bar(bar, period=P)` 的既有 period 过滤，`portfolio.py:108-111`）：
  - 全局 `times` = 各周期 bar 时间并集（升序去重，逻辑同现 `_build_timeline`）。
  - `pending_by_period: Dict[str, List[OrderEvent]]`。
  - 对每个 `t`：
    1. **成交步**：对本刻有 bar 的每个周期 `P`，用 `period_index[P][t]` 的 `open` 成交 `pending_by_period[P]` 中「该 code 本刻有 bar」的订单；其余保留（交给 #2 重试/TTL）。
    2. **信号步**：对本刻有 bar 的每个周期 `P`，构造 `BarEvent(stocks=period_index[P][t], bar_time=t, period=P)`，调 `portfolio.on_bar(bar, signal_cache, period=P)`，得到新订单，打上 `OrderEvent.period=P`，追加进 `pending_by_period[P]`。
- `OrderEvent`（`event.py:38`）新增字段 `period: Optional[str] = None`。
- `BarEvent`（`event.py:10`）新增字段 `period: Optional[str] = None`（信号步需要；`StrategyContext.get_signal` 的 cache key 仍用 `(strategy_id, stock_code, bar_time)`，因周期隔离后同一策略的 bar_time 天然属于其周期，无需改 key）。
- 删除旧的单索引助手 `_build_price_index` / `_bar_open_prices` / `_build_bar`，替换为周期版。

**说明**：`portfolio.on_bar` 已支持 `period` 参数（实盘 C6 机制），回测复用之，使回测与实盘时间轴语义一致。

### 1.2 #6 每交易日仅末根 bar 一条快照

**现状缺陷**：`backtest_engine.py:74-80` 每 bar 无条件 append `portfolio.snapshot(t.date(), ...)`；分钟回测一天写多条，且 `max_drawdown`/`ulcer_index`/`return_stability`（R²）/`recovery_days` 按 per-bar 序列计算，跨频率不可比。

**设计**：
- 在合并 `times` 上判断「当日末根 bar」：`i == len(times)-1 or times[i+1].date() != t.date()`。
- 仅当日末根 bar 执行：组合层快照 + 策略层快照（`_strategy_snapshots`）+ `risk_manager.update_daily`。
- 日线回测每 bar 即一日 → 行为不变。分钟回测快照降为日粒度 → evaluator 的回撤/Ulcer/R²/recovery 自动按日序列，跨频率可比。
- `benchmark_value` 亦仅日末赋值一次。

### 1.3 #8 熔断口径统一为日终市值

**现状缺陷**：回测每 bar 调 `risk_manager.update`（=`update_peak`+`update_daily`，`risk_manager.py:103-110`）→ 分钟回测日内亏损按每 bar 抖动判定；实盘 `daily_closer` 14:30 一次性用**成本近似**（`live/breaker.py:246-256`：`cash + Σ avg_cost*qty`）→ 时点与口径双不一致。

**设计**：
- 回测：每 bar 调 `risk_manager.update_peak(total_value, t.date())`（回撤/熔断/跨日刷新 prev_close）；**仅当日末根 bar** 调 `risk_manager.update_daily(total_value, t.date(), initial)`（日内亏损用当日末 bar 市值）。
- 实盘：`live/breaker.py::on_daily_update` 由「现金 + Σ成本」改为使用当日末 bar 的 `portfolio.total_value()`（市值口径）；由 `daily_closer.maybe_daily_close` 把当日末 bar 的总市值透传进来（现调用点 `live_engine.py:549` / `daily_closer.py:75-97`）。
- 保持两端口径一致：均「当日收盘市值」计算日内盈亏。

---

## 2. 执行 / 资金（#7 / #2）

### 2.1 #7 跳空高开不再导致现金为负

**现状缺陷**：审批按 signal bar 收盘价（`execution_engine.py:148-150`、`portfolio.py:343,409`），成交按下 bar 开盘价（`execution_engine.py:45-53`），`account.deduct_cash` 无条件扣减（`account.py:65-66`）→ 跳空高开 `fill_price > order.price` 时现金转负。

**设计**：
- `SimulatedDispatcher` 新增 `fill_price(order) -> Optional[Decimal]`：按 `open ± slippage` 返回成交价（无价返回 None）。
- `ExecutionEngine.execute` 对 **BUY** 在 `cap_quantity` 之后、`place_order` 之前按**实际成交价**重新校验资金：
  - 计算含费（佣金、印花税、min_commission）的**可买整数手** `affordable`；
  - `affordable < 100` → 拒单 + 记录 `gap_up_insufficient`（action=`reject`）；
  - `affordable < 当前量` → 缩量到 `affordable` + 记录 `gap_up_insufficient`（action=`shrink`，message 说明「跳空高开资金不足」）；
  - 最终 `order.quantity = affordable` 后再 `place_order`。
- **保证 `account.cash ≥ 0`**。仅对 `SimulatedDispatcher`（回测）生效；实盘按券商真实资金，不改。
- **不引入涨跌停约束**（按用户决策）。

### 2.2 #2 待成交订单不再静默丢弃

**现状缺陷**：`place_order` 无价返回 `None` 且无日志（`execution_engine.py:44-47`）；`backtest_engine.py:63-66` 无条件 `pending_orders=[]` → 停牌/无行情订单永久静默丢失。

**设计**（配合 #1 的 `pending_by_period`）：
- 成交步只移除「本刻该 code 有 bar 且成交成功」的订单；其余（无 bar/停牌/open=0）**保留在原周期队列**，在下一个同周期同 code 的 bar 重试。
- **TTL**：若订单在「信号日次一交易日的收盘」之后仍未成交 → 取消，并记录决策事件 `order_expired_no_fill`（action=`cancel`，含 code/原始量/原因）。
- `place_order` 无价分支不再作为唯一归宿：引擎先判断 code 是否本刻有 bar，无则不入 dispatcher、直接保留。

---

## 3. 服务层修复（#3 / #4 / #5 / #10 / #11 / #12 / #13）

### 3.1 #3 分钟信号对齐

**现状缺陷**：`_bar_times_by_code`（`backtest_service.py:1115-1132`）对每只 code 取**全周期并集**时间轴，`build_signal_cache`（`:1156-1173`）把它喂给任意分钟策略且**不按策略周期过滤** → 多周期组合下索引串位；且无输出条数校验（`:1078-1082` 越界仅 break）。

**设计**：
- 新增 `_bar_times_by_code_period(klines) -> Dict[str, Dict[str, List[datetime]]]`：`period_code → code → 升序去重时间轴`，仅含该周期。
- `build_signal_cache`：对策略 period=P（分钟级）传 `bar_times_by_period[P]`；日线仍走 Date 匹配。
- `_convert_formula_output` 增加**长度校验**：`len(val_list) != len(bar_times)` 时记 `logging.warning`（含 counts/period/code），保持钳制不崩溃。

### 3.2 #4 删除运行中回测 → HTTP 409

- `api/backtest.py` 删除端点 + `backtest_service` 删除函数：若 `record.status == "running"` → 返回 `err(409, "回测运行中，无法删除")`（对齐实盘 `live_service.py:268` / `api/live.py:186`）。

### 3.3 #5 崩溃后僵尸 running

- 新增 `backtest_service.recover_stale_running(db)`：把 `status=="running"` 的记录重置为 `failed`，`error_message="服务重启中断，已重置"`。
- `main/core/main.py` lifespan 内在 DB 初始化后调用（单进程假设成立）。
- 附带解决：彻底排除 DELETE 误删 running 后的孤儿状态。

### 3.4 #10 实现真实 progress

- `BacktestEngine.run(..., on_progress: Optional[Callable[[int], None]] = None)`：按已处理 bar 数 `(i+1)/len(times)*100` 回调，节流（每 ≥1% 或每 N bar）。
- `run_backtest` 传入回调：更新 `rec.progress` 并 `db.commit()`（回测在请求线程内同步执行，`db` 为请求 session，无并发冲突）。
- 完成仍置 100（`backtest_service.py:1831`）；失败保持失败态。
- 前端进度条已存在，无需改动。

### 3.5 #11 重算评估 running 守卫

- `recompute_evaluation`（`backtest_service.py:905-928`）在判空后加 `status=="running"` → 返回错误 → 路由转 HTTP 409。

### 3.6 #12 死参数清理

- 删除 `build_open_prices`（`backtest_service.py:297-316`，其中 `ps` 未使用）及其调用点（`:1788`）。
- 从 `BacktestEngine.run` 移除从未使用的 `open_prices` 形参（`backtest_engine.py:19,30`；引擎自建 per-period 索引）。
- 更新受影响单测（如 `test_backtest_data.py:185,199`）。

### 3.7 #13 注释失真

- 修正 `backtest_engine.py:77-78` 注释：明确「缺失 → `None` → 由 evaluator 过滤」，**无 carry-forward**。不改行为。

---

## 4. 基础设施（#9）

- `main/core/db.py`：连接事件内执行 `PRAGMA journal_mode=WAL` 与 `PRAGMA busy_timeout=5000`（保留 `foreign_keys=ON`）；`create_engine` 增 `connect_args={"timeout": 5, "check_same_thread": False}`。降低 Web 线程 + 实盘 worker 并发写「database is locked」风险。

---

## 5. 实施顺序（供计划拆分）

1. **低风险卫生项**：#9、#12、#13、#4、#5、#11、#10（互不耦合，先落地）
2. **执行/资金**：#7、#2
3. **信号对齐**：#3
4. **引擎时间轴重构**：#1 + #1b + #6 + #8（三者同属 `run` 主循环，一体提交）
5. **实盘侧 #8**：`live/breaker.py` + `daily_closer.py` + `live_engine.py`

---

## 6. 验证

- 后端：`python -m pytest main/core/tests`（DB 相关 + 引擎单测 + 服务集成），新增用例覆盖：
  - #1 混合周期日线信号不在当日成交 / 多周期同刻不覆盖；
  - #2 停牌订单重试 + 超期取消告警；
  - #3 多周期组合分钟信号对齐 + 长度不符 warning；
  - #6 分钟回测每交易日仅 1 条快照；
  - #7 跳空高开现金 ≥ 0 + 决策事件；
  - #8 回测=实盘日终市值口径；
  - #4/#5/#11 状态守卫；
  - #9 WAL/busy_timeout 生效；
  - #10 progress 单调递增至 100；
  - #12 删除后无引用。
- 前端：`npx vitest run` + `npx vue-tsc -b`（无新 UI）。
- 实盘：#8 改动后 `test_live_engine.py` / `test_risk_manager.py` 全绿。

---

## 7. 非目标（YAGNI）

- 不模拟涨跌停/一字板。
- 不回填/重算历史回测记录与新指标。
- 不改前端展示逻辑（进度条既有）。
- 不引入新第三方依赖。
