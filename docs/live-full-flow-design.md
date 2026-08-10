# 实盘全流程详细设计

> 本文描述创懿量化平台**实盘交易**从启动到停盘的完整链路设计,基于 0009(HTTP 桥)+ 0010(公式注入)已提交代码的现状,标注每一步的【已实现/缺口/待验证】。
> 日期:2026-08-05
> 关联:[AGENTS.md](../AGENTS.md) 业务规则、[docs/plans/0009-iquant-http-bridge.md](plans/0009-iquant-http-bridge.md)、[docs/plans/0010-live-formula-inject.md](plans/0010-live-formula-inject.md)、[docs/open-questions.md](open-questions.md)

---

## 0. 架构总览

```
Web 前端 ──HTTP/SSE──→ Core (FastAPI, main/, Py3.13)
                         │
                         ├── 实盘引擎 LiveEngine (进程内,长期持有内存态)
                         │     ├── BarPoller ──HTTP /quote──→ iQuant 桥 (127.0.0.1:8790)
                         │     ├── Portfolio[] (每组合一个,复用回测逻辑)
                         │     │     └── StrategyContext[] (每子策略一个,持有 positions)
                         │     ├── ExecutionEngine (复用回测,注入 HttpBridgeDispatcher + LiveT1Checker)
                         │     └── TQFormula.compute_injected (内存注入算公式)
                         │
                         ├── DB (SQLite/PG): LiveSession / LiveOrder / LiveTrade ...
                         └── 嵌入式 TQ 模块 (通达信,get_tq() 单例 + get_tdx_lock 串行)
                                    ↑
                         iQuant 桥 ──HTTP /order──→ iQuant 客户端 (Py3.6, passorder 真实下单)
```

**两个 Python 环境**:
- Core(`main/`, Py3.13):引擎、API、公式计算、回测(同进程)
- iQuant 桥(`live/bridge/`, Py3.6.8):HTTP 服务 + `passorder` 真实下单,`init` 阻塞主循环=单线程事件循环

**复用原则**:实盘引擎核心(Portfolio/StrategyContext/ExecutionEngine/risk_manager)97% 复用回测,仅通过策略模式注入隔离:
- `OrderDispatcher`:回测 `SimulatedDispatcher`(open 价模拟成交) / 实盘 `HttpBridgeDispatcher`(桥真实下单)
- `T1Checker`:回测 `SimulatedT1Checker`(按日分桶严格 T+1) / 实盘 `LiveT1Checker`(首期全量放行,见 §6)

---

## 1. 会话生命周期

### 1.1 创建会话

**端点**:`POST /api/live/sessions`
**已实现**:`api/live.py:72`

请求体:`{name, mode, portfolio_ids: [组合策略ID...]}`
- `mode`:默认 `simulation`(模拟模式,桥 `DRY_RUN=True` 不真下单);`live`(真金白银)
- 建 `LiveSession` 行(status=`stopped`)+ 每个组合一条 `LiveSessionPortfolio` 关联

### 1.2 启动会话

**端点**:`POST /api/live/sessions/{id}/start`
**已实现**:`api/live.py:173`,调用 `_build_engine` + `engine.recover` + `asyncio.create_task(engine.start)`

启动顺序(关键,不可乱):

```
1. _build_engine(session_id, db)   ← 组装引擎(见 §2)
2. engine.recover(db)              ← 持仓恢复(见 §3)
3. asyncio.create_task(engine.start())  ← 起后台循环(见 §4)
4. _ENGINES[session_id] = engine   ← 进程内注册表
5. session.status = "running", session.started_at = now → db.commit
```

**幂等**:`session_id in _ENGINES` → 直接返回 `running`,不重复建引擎

### 1.3 停止会话

**端点**:`POST /api/live/sessions/{id}/stop`
**已实现**:`api/live.py:194`

- `_ENGINES.pop(session_id)` 取出引擎,`asyncio.create_task(engine.stop())` 取消循环任务
- `session.status = "stopped"`, `stopped_at = now` → commit

### 1.4 桥状态查询

**端点**:`GET /api/live/sessions/{id}/bridge-status`
**已实现**:`api/live.py:209`

- 引擎未运行 → `{online: None, status: "not_running"}`
- 运行中 → `{online: dispatcher.heartbeat(), status: session.status}`

**【缺口·切片5】**:`bridge-status` 是独立端点,0009 §11.5 要求并入 `/api/live/sessions/{id}` 详情响应。

### 1.5 SSE 实时推送

**端点**:`GET /api/live/sessions/{id}/stream`
**已实现**:`api/live.py:237`,当前仅发 `ping` 心跳。

**【缺口】**:实盘的成交/信号/持仓变化尚未通过 SSE 推前端,前端无法实时看到下单。待切片5/后续补。

---

## 2. 引擎组装(_build_engine)

**已实现**:`api/live.py:113`

### 2.1 组装步骤

对 session 关联的每个 `LiveSessionPortfolio`:

1. 查 `PortfolioStrategy`(组合策略)→ `assemble_portfolio(ps, strategies, db)` 组装 `Portfolio`(`portfolio_builder.py`,回测/实盘共用)
2. `_resolve_stock_codes(db, ps.id)` 取股票池成分股代码 → 汇总到 `stock_codes` 集合(BarPoller 行情订阅范围)
3. 遍历组合的 `Strategy` 行,收集 `formula_id` → 建 `{strategy_id: formula_id}` 映射
4. 批量查 `Formula` 表 → 转成 `{strategy_id: formula_name}`(公式名,供 `compute_injected` 调用)

### 2.2 构造组件

```python
dispatcher = HttpBridgeDispatcher(base_url, token)   # 桥地址来自 config.iquant_bridge
poller = BarPoller(dispatcher, sorted(stock_codes) or ["000001.SZ"],
                   period="1m", count=10)
engine = LiveEngine(
    session_id, portfolios, dispatcher, poller,
    db_session_factory=SessionLocal,
    tq_formula=TQFormula(),
    formula_by_strategy=formula_by_strategy,   # {strategy_id: formula_name}
    formula_count=200,                          # 注入历史根数,1m/5m 默认 200 够均线预热
)
```

**【缺口·open-questions Q1】**:`formula_count` 全策略统一 200,未按公式实际需求区分。多组合多策略时,所有策略共用一个 `dispatcher` + `poller`,股票池是所有组合成分股的并集——**组合间股票池隔离在 `_handle_bar` 层面没有显式约束**(on_bar 内每个 ctx 各自处理,但行情是全量广播)。

---

## 3. 持仓恢复(recover)

**已实现**:`live_engine.py:367`

Core 重启后(进程崩溃/手动重启),从 `live_trades` 重放重建虚拟持仓,保证恢复后与在线时一致。

### 3.1 重放逻辑

```
1. 查本 session 全部 LiveTrade,按 trade_time, id 排序
2. 预取关联 LiveOrder 的 signal_type(LiveTrade 无此列,需关联查)
3. 逐笔重放:
   - 按 portfolio_strategy_id 找 Portfolio
   - 按 strategy_id 找 StrategyContext
   - 构造 TradeEvent(portfolio_id/strategy_id/stock_code/trade_type/price/quantity/amount/commission/stamp_duty/trade_time/signal_type)
   - port.account.apply_trade(trade)  ← 重建虚拟现金
   - pos.apply_trade(trade)           ← 重建虚拟持仓(含 _lots 分桶、avg_cost、add_count)
```

### 3.2 虚拟现金以成本计(§93)

恢复的现金 = 初始资金 - Σ(BUY 金额+佣金+印花税) + Σ(SELL 金额-佣金-印花税),按历史成交**实际成本**计,不用市值。这与在线时 `Account.apply_trade` 的记账方式一致。

### 3.3 【缺口·open-questions Q1/Q2】对账未实现

`recover` 只重建虚拟持仓,**不与桥 `/positions` 真实持仓对账**。若重启前有桥拒单未回填、部分成交、手动操作,虚拟持仓与真实持仓已背离,`recover` 会把这个错误状态固化。

**需要补**:recover 后立即查桥 `/positions`,按 `code` 聚合虚拟持仓 vs 真实持仓比对,不一致则告警/以真实为准修正。依赖切片5 `/deals` 回填先正确。

---

## 4. 主循环(_loop)

**已实现**:`live_engine.py:119`

### 4.1 循环结构

```python
async def _loop(self):
    while self._running:
        try:
            if not self._dispatcher.heartbeat():       # ① 心跳
                self._bridge_online = False
                await asyncio.sleep(self._poll_interval); continue
            self._bridge_online = True
            self._bar_poller.poll()                    # ② 拉 bar → 触发 _on_bar
            # 【缺口·切片5】③ 轮询 /deals 回填未完结订单
            # 【缺口】④ 风控日终 update(熔断/日内亏损检测)
        except BridgeUnavailableError:
            self._bridge_online = False
        except asyncio.CancelledError: raise
        except Exception: logger.exception(...)
        await asyncio.sleep(self._poll_interval)       # 默认 15s
```

### 4.2 心跳(①)

`dispatcher.heartbeat()` = `GET /ping` 返回 200。离线 → 标 `_bridge_online=False`,暂停本轮下单(但**不停止循环**,持续重试心跳)。

### 4.3 拉 bar(②)

`BarPoller.poll()` 拉 `/quote`,用**两次拉取的相对变化**判定 bar 完成(不依赖绝对时钟),对每根新完成 bar 触发 `self._on_bar(bar)`。

- 首次 poll 建立基线不触发(不回放历史)
- 多股票按 code 独立判定完成,同一时间戳合并为一根 `BarEvent`
- 5m 原生周期直接拉,不做 1m→5m 聚合

### 4.4 【缺口·重大】风控日终 update(④)

回测 `backtest_engine.py:84` 每根 bar 调 `portfolio.risk_manager.update(total_value, t.date(), initial_capital)` 推进熔断/日内亏损检测。**实盘 `_loop`/`_handle_bar` 完全没调** → **熔断(§88)和日内亏损暂停当前是哑火的**。

**需补**:每根 bar 处理后,计算组合总市值(现金 + Σ持仓×close),调 `risk_manager.update`。但实盘 bar 是分钟级,`update` 的「日终」语义在分钟级下需调整:
- `current_date` 用 `bar.bar_time.date()`
- 同一日内多根 bar 多次调 `update`,`peak_value` 更新没问题
- `daily_pnl` = 当前总市值 - 当日开盘总市值(需记录当日首根 bar 的总市值作 `prev_close`,不是上一根 bar)
- 熔断/日内亏损触发后,`is_trading_halted()` 在 `Portfolio.on_bar:70` 已接(剥掉 BUY),但**次日恢复时序**靠 `current_date > trigger_date`,分钟级下跨日才推进——逻辑可用,但要确认日切边界

### 4.5 【缺口·切片5】/deals 回填(③)

见 §7。

---

## 5. 单根 bar 处理(_on_bar → _handle_bar)

**已实现**:`live_engine.py:141`(on_bar)、`157`(handle_bar)

### 5.1 _on_bar:分发到各组合

```python
def _on_bar(self, bar):
    for portfolio in self.portfolios:
        try:
            self._handle_bar(portfolio, bar)
        except BridgeUnavailableError:  # 下单时桥离线
            self._bridge_online = False; return
        except Exception: logger.exception(...)
```

### 5.2 _handle_bar:取信号 → 下单 → 落库

```python
def _handle_bar(self, portfolio, bar):
    self._fill_signal_cache(portfolio, bar)              # ① 公式信号注入(见 §5.3)
    orders = portfolio.on_bar(bar, signal_cache=self.signal_cache)  # ② 取信号+风控+优先级
    if not orders: return
    db = self._db_session_factory()
    try:
        for order in orders:
            ctx = self._find_strategy(portfolio, order.strategy_id)
            pos = ctx.positions.get(order.stock_code)
            if pos is None and order.trade_type == BUY:  # 首次建仓建 Position
                pos = Position(order.stock_code); ctx.positions[order.stock_code] = pos
            trade = self._engine.execute(order, portfolio.account, pos)  # ③ 下单+成交
            if trade is None: continue
            self._persist_trade(db, order, trade)        # ④ 落库
        db.commit()
    except: db.rollback(); raise
    finally: db.close()
```

### 5.3 公式信号注入(_fill_signal_cache)

**已实现(0010)**:`live_engine.py:199`

对每个策略 × bar.stocks 每只股票:
```
bridge query_quote(code, period, count=formula_count)  → 拉历史 N 根 bar
→ _bars_to_formula_df(bars, code)                      → 转 OHLCV DataFrame
→ TQFormula.compute_injected(formula_name, df, ...)    → 内存注入算公式
→ _extract_latest_signal(raw, code)                    → 取最后一条(当前 bar 信号)
→ signal_cache[(strategy_id, code, bar.bar_time)] = outputs
```

- 无 `tq_formula` / 策略无公式映射 / 拉取为空 / 算失败 → 跳过(该股该 bar 无公式信号)
- `get_tdx_lock()` 串行化,与回测互不并发(同进程共用 `get_tq()` 单例)

**内存注入链路**(已真机验证等价,2026-08-06,1m/5m/15m/30m 均通过):
`formula_format_data` → 逐股票 `formula_set_data(dividend_type=0)` → `formula_process_mul_zb(count=-1)`,注入数据被公式引擎完全等价采用,不写本地 `.lc1`。
注:50m/120m 通达信 SDK 不支持(`periodstr error`),非注入问题。详见 open-questions Q4。

### 5.4 取信号+风控+优先级(Portfolio.on_bar)

**已实现**:`portfolio.py:54`,复用回测逻辑

```
for ctx in strategies:
    signals = ctx.get_signal(bar, signal_cache)   # 公式信号,cache 优先
    risk_signals = self._check_risks(ctx, bar)    # 风控:止损/止盈/移动止损
    all_signals = risk_signals + signals
    all_signals.sort(key=_signal_priority)        # 风控 > 公式;公式内 CLOSE>REDUCE>ADD>OPEN
    for sig in all_signals:
        if sig.stock_code in cleared: continue    # 全平后抑制同股后续信号
        order = self._signal_to_order(ctx, sig, bar)
        orders.append(order)
if self.risk_manager.is_trading_halted():         # 熔断/日内亏损暂停:剥 BUY,留 SELL
    orders = [o for o in orders if o.trade_type != BUY]
```

**信号优先级**:风控(止损/止盈/移动止损) > 公式(CLOSE > REDUCE > ADD > OPEN)
**主从联动**:从策略 OPEN 只能买主策略当前持有的同一只股票;主策略清仓后从策略不可新开仓(存量可卖)

### 5.5 下单+成交(ExecutionEngine.execute)

**已实现**:`execution_engine.py:95`,复用回测,注入 `HttpBridgeDispatcher` + `LiveT1Checker`

```
if BUY:
    approved, qty = account.approve_order(...)     # 资金审批(现金 + 策略上限)
    if not approved or qty < 100: return None
    order.quantity = qty
else:  # SELL
    available = t1_checker.get_available_shares(pos, order.bar_time.date())
    qty = min(order.quantity, available)
    if qty < 100: return None
    order.quantity = qty

trade = dispatcher.place_order(order)              # 桥下单(见 §5.6)
if not trade: return None
account.apply_trade(trade)                         # 记账:买扣/卖加
position.apply_trade(trade)                        # 持仓:_lots 分桶/avg_cost
return trade
```

**【缺口·open-questions Q2/Q3】卖出量上限**:
- 当前 `LiveT1Checker` 全量放行(`return position.quantity`),不挡 T+1
- 应改为 `min(本策略持有量, 券商可用量)`,券商 `available` 体现 T+0/T+1(不建字段方案,待 `m_dAvailable` 真机验证)
- 同 bar 多策略超卖需「bar 内可用量递减记账」

### 5.6 桥下单(HttpBridgeDispatcher.place_order)

**已实现**:`http_bridge_dispatcher.py:67`

```
payload = {order_id(MD5), code, op(buy/sell), volume, price(bar.close), pr_type=14}
POST /order → 桥 passorder(op_type, 1101, account, code, 14, price, volume, ...)
桥返回 {ok: True} → 构造 TradeEvent(成交价=请求价近似, commission=0, stamp_duty=0)
桥返回 {ok: False} → return None(业务拒绝,不抛)
桥网络不可用 → 抛 BridgeUnavailableError(上层暂停)
```

**【缺口·切片5】**:受理即视为成交,真实成交价/量未回填。`prType=14` 对手价实际成交是盘口一档价(≠ `bar.close`),佣金/印花税首期为 0(真实成本从 `/deals` 回报取)。

### 5.7 落库(_persist_trade)

**已实现**:`live_engine.py:325`

```
LiveOrder(live_session_id, portfolio_strategy_id, strategy_id, stock_code,
          trade_type, order_type="limit", price, quantity,
          filled_quantity=trade.quantity, filled_price=trade.price,
          status="accepted", signal_name, signal_type, bar_time)
LiveTrade(live_session_id, live_order_id, portfolio_strategy_id, strategy_id,
          stock_code, trade_type, price, quantity, amount, commission, stamp_duty, trade_time)
```

**【缺口·切片5】**:`status="accepted"` 应拆为 `submitted`,后续 `/deals` 回填推进到 `partial`/`filled`/`rejected`。

---

## 6. T+1 / T+0 处理

### 6.1 回测(严格 T+1)

`SimulatedT1Checker` + `Position._lots` 按买入日分桶:
- `_lots: Dict[date, int]` = {买入日期: 股数}
- `available_shares_on(query_date)` = `d < query_date` 的桶之和(严格小于,不含当日)
- sell 按 FIFO 从最早可卖桶扣减
- **命门**:回测成交在下一 bar,`order.bar_time = t+1`(成交日,覆盖信号日),T+1 判定基准是成交日——`backtest_engine.py:50` 这行是回测 T+1 正确性的命门

### 6.2 实盘(当前:不挡)

`LiveT1Checker.get_available_shares` = `return position.quantity`(全量放行),T+1 交券商端。隐患:Core 账面已减仓,券商端拒单 → 虚拟与真实背离。

### 6.3 实盘(目标:靠券商 available)

**【待验证·open-questions Q3】** 不建品种字段,靠桥 `query_positions(code).available`(即 `m_dAvailable`)体现 T+0/T+1:
- T+1 品种当日买 → 券商 `available=0` → Core 不下单
- T+0 ETF → 券商 `available=全量` → 正常卖
- 卖出量 = `min(order.quantity, 本策略持有量, bridge_available[code])`

**前置验证(未做)**:`m_dAvailable` 是否准确反映 T+1、ETF T+0 是否全量、是否实时刷新。休市可查历史持仓验字段存在性,实时性/T+1 生效须开盘验。

---

## 7. 成交回报回填(切片5,未实现)

### 7.1 订单状态机

```
submitted(受理未成交) → partial(部分成交) → filled(全部成交)
                                         → rejected(拒单/撤单)
```

- `place_order` 桥返回 ok → `LiveOrder.status = "submitted"`
- 主循环轮询 `/deals` → 推进状态

### 7.2 回填机制(已定:主循环轮询)

`LiveEngine._loop` 每轮额外:
```
1. 查 status in (submitted, partial) 的未完结 LiveOrder
2. 调桥 query_deals() 拉成交回报
3. 按匹配键对应到订单(见 §7.3)
4. 回填 filled_quantity / filled_price / status
5. 修正虚拟持仓:拒单/部分成交反向调整 Position/Account(当前按全量成交记账)
```

### 7.3 订单匹配键(2026-08-10 真机定案)

**匹配键 = `m_strOrderRef`(委托引用号)**:真机验证 DEAL 与 ORDER 对象**共享同一 `m_strOrderRef`**,3 笔真实成交(BRIDGE×2 + GUI×1)全部对上;`m_strOrderSysID`(合同号)同样 ORDER↔DEAL 一致。**对象上无 `m_nOrderID` 字段**(桥原取它导致 order_id 一直 null)。

`passorder` 只返回 `0`,**无法预知券商分配的 OrderRef**,故匹配流程:
```
1. Core 下单 → LiveOrder(status=submitted, 带 Core order_id)
2. 轮询 /orders → 用组合键(股票+方向+数量+下单后时间窗口)定位自己的委托
   —— 全局限 1 session 串行下单,取最新未匹配 ORDER 即自己的单,可靠
3. 取该 ORDER 的 m_strOrderRef 回写 LiveOrder
4. 轮询 /deals → 按 m_strOrderRef 关联成交 → 回填
```
方向字段 `m_nDirection`(48买/49卖)、时间 `m_strInsertTime`/`m_strTradeTime`、状态 `m_nOrderStatus`(54撤/56成)均已在 ORDER/DEAL 对象上确认。桥 `query_deals`/`query_orders` 已补全这些字段(见 checklist G3/G4 行)。

### 7.4 修正虚拟持仓

回填发现 `rejected`:该笔 SELL 没成交,但 `_handle_bar` 已 `account.apply_trade`/`pos.apply_trade` → 需反向退回(加回现金、加回持仓、还原 _lots)。BUY 拒单同理(退回现金、减持仓)。

`partial`:按实际成交量修正,差额部分反向退回。

这是切片5 最复杂的部分,涉及 `Position`/`Account` 的反操作,现有 `apply_trade` 只有正向,需补反向或重放修正。

---

## 8. 风控与熔断(§88)

### 8.1 策略层风控(已实现)

`StrategyRiskManager`(`risk_manager.py:8`),每根 bar 对每只持仓检查:
- `check_stop_loss`:亏损 ≥ `stop_loss_ratio` → STOP_LOSS 信号(全平)
- `check_take_profit`:盈利 ≥ `take_profit_ratio` → TAKE_PROFIT 信号(全平)
- `check_trailing_stop`:从 `highest_price` 回撤 ≥ `trailing_stop_ratio` → TRAILING_STOP 信号(全平)

`_signal_to_order` 里全平类量 = `pos.quantity`,REDUCE 量 = `pos.quantity × reduce_position_ratio`。

### 8.2 组合层熔断(已实现逻辑,实盘未接线)

`PortfolioRiskManager`(`risk_manager.py:40`):
- `max_drawdown`:从 `peak_value` 回撤 ≥ `max_drawdown` → 触发熔断,次日恢复,累计 3 次转 `manual_recovery`
- `daily_loss_limit`:日内亏损 ≥ `daily_loss_limit` → 当日暂停,次日恢复
- 熔断/暂停期间 `is_trading_halted()=True` → `Portfolio.on_bar` 剥掉 BUY,保留 SELL(不清仓,仅暂停新开仓)
- `update(total_value, current_date, initial_capital)`:每根 bar 日终调,推进峰值/检测/次日恢复时序

**【缺口·重大·§4.4】**:实盘 `_loop`/`_handle_bar` 未调 `risk_manager.update` → 熔断和日内亏损检测哑火。需在每根 bar 后补调,并处理分钟级下「日终」语义(当日首根 bar 总市值作 `prev_close` 基准)。

### 8.3 熔断计数持久化

`LiveSessionPortfolio.circuit_breaker_count` 字段已存在(模型层),但**引擎层未读写**。重启后熔断次数丢失,累计 3 次转手动的逻辑会重置。需在 `update` 触发熔断时持久化,`recover` 时读回。

---

## 9. 数据模型

| 表 | 作用 | 关键字段 |
|---|---|---|
| `live_sessions` | 实盘会话 | id, name, mode, status, started_at, stopped_at |
| `live_session_portfolios` | 会话-组合关联 | session_id, portfolio_strategy_id, status, circuit_breaker_count |
| `live_orders` | 下单意图+受理 | live_session_id, portfolio_strategy_id, strategy_id, stock_code, trade_type, price, quantity, filled_quantity, filled_price, status, signal_name, signal_type, bar_time |
| `live_trades` | 成交回报 | live_session_id, live_order_id, portfolio_strategy_id, strategy_id, stock_code, trade_type, price, quantity, amount, commission, stamp_duty, trade_time |

**归属链**:`LiveTrade.portfolio_strategy_id + strategy_id + stock_code` 是持仓映射到组合/策略的唯一来源(见 open-questions Q1)。

**【缺口·切片5】**:`live_orders.status` 当前只用 `accepted`,需支持 `submitted/partial/filled/rejected`;可能需加 `broker_order_id` 列存券商订单号(若 §7.3 验证后需要桥端建映射)。

---

## 10. 并发与线程模型

- **LiveEngine 主循环**:asyncio Task,跑在 FastAPI 事件循环,单线程
- **TQ 公式计算**:`get_tdx_lock()` 串行化,回测同步在请求线程(同进程),与实盘共用 `get_tq()` 单例,不并发
- **桥**:iQuant 客户端内单线程事件循环(`init` 阻塞),HTTP 请求排队处理
- **DB**:每根 bar 一个独立 Session(`_db_session_factory`),用完即关,不跨 bar 持有

**约束**:主循环不可被 TQ 回调阻塞(回测同步已避免);单用户系统,实盘 session 同一时刻最多一个引擎实例在跑(但代码层未强制,`_ENGINES` 是 dict,理论支持多 session,需确认是否要限制)。

---

## 11. 待明确问题(见 open-questions.md)

- **Q1**:实盘持仓如何映射多组合多策略(对账未实现)
- **Q2**:实盘卖出如何处理(量与隔离,同 bar 多策略超卖)
- **Q3**:T+0/T+1 判定(`m_dAvailable` 前置未验证)

---

## 12. 实现进度与下一步

| 模块 | 状态 |
|---|---|
| 会话 CRUD + start/stop | ✅ 已实现 |
| 引擎组装 `_build_engine` | ✅ 已实现(含公式预加载) |
| 持仓恢复 `recover` | ✅ 已实现(对账缺口) |
| 主循环 `_loop` + 心跳 | ✅ 已实现 |
| 公式信号注入 `_fill_signal_cache` | ✅ 已实现(0010) |
| 单 bar 处理 `_handle_bar` | ✅ 已实现 |
| 桥下单 `place_order` | ✅ 已实现(受理即成交近似) |
| 落库 `_persist_trade` | ✅ 已实现(status=accepted) |
| T+1 实盘处理 | ⚠️ 全量放行,待改 |
| 熔断/日内亏损接线 | ❌ 未接 `risk_manager.update` |
| 成交回报回填 `/deals` | ❌ 切片5 未做 |
| 订单状态机 | ❌ 未做 |
| 持仓对账 | ❌ 未做 |
| SSE 成交推送 | ❌ 未做 |
| 桥状态并入 session API | ❌ 切片5 未做 |

**下一步(切片5,等开盘)**:
1. 验证桥 `/deals` + `/positions` 真实行为(订单匹配键 + `m_dAvailable`)
2. 写 0011 切片5 计划
3. 实现订单状态机 + `/deals` 回填 + 虚拟持仓修正
4. 补熔断/日内亏损接线(可与切片5 并行或紧随)
5. 全量回归

---

## 13. 已知缺口汇总(按严重度)

1. **【高·§4.4】熔断/日内亏损实盘哑火**:`_loop` 未调 `risk_manager.update`,组合层风控不生效。这是业务规则(§88)硬要求,优先级高于切片5。
2. **【高·§7】成交回报未回填**:账面价不准,拒单/部分成交导致虚拟与真实持仓背离。
3. **【中·§6.3】T+1 实盘不挡**:当日买的 T+1 品种可能被 Core 下卖单→券商拒→背离。靠 `m_dAvailable` 修,前置未验证。
4. **【中·§3.3】持仓对账未做**:重启后虚拟持仓可能与真实背离无法发现。
5. **【低·§1.5】SSE 未推成交**:前端看不到实时下单。
6. **【低·§8.3】熔断计数未持久化**:重启丢失累计次数。
