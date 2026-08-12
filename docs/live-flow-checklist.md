# 实盘流程细节逐点确认清单

> 按**时间轴**把实盘从启动到停盘的复杂细节钉到 9 个阶段,每项标注「状态/确定方式/说明/确认结论」,
> 逐点推进、逐点落结论。和 [open-questions.md](open-questions.md)(待明确问题)、
> [live-full-flow-design.md](live-full-flow-design.md)(全流程设计)交叉引用。
> 日期:2026-08-06(更新:2026-08-10)
>
> **用法**:逐行过,能定的把结论写进「确认结论」列并标 ✅;需真机的标 🔬 待开盘;需人定的标 🧠 等决策。
> 一项的结论可能同时更新本表 + open-questions.md + 全流程设计对应章节。
>
> **2026-08-10 状态更新**:切片5 订单状态机 + /deals 回填(G1/G2/G6)、G7 桥状态并入、C6 三段式实盘周期链路(1m 边界分发 + 1d 14:30 快照 + 1w/1mon 通达信注入)、E8 离线恢复不补 bar、F10 submitted 拆分、I4 挂回未完结单、B6 全局限 1 session、F5 接桥 available、C4 三维去重(#28)+ Formula.formula_count(#27)、D4/H4 熔断计数读回/持久化、F6 同 bar 可用量递减记账 + G5 独立 5s 回填轮询、D3 对账(仅告警不修正)、A2 账号改配置 + 桥部署 README、B5 SSE 事件流(signal/order/trade/position/risk 五类推送)、B4 前端实盘工作台(SSE 事件日志 + 持仓/委托/成交三表 + 历史加载 + B4b orders/trades/positions 历史查询端点),均已 TDD 实现并提交(eb4bc40/9e46869/c2e1482/3c826e8/3b74cbf/d5d8e90/1d60d4f/5d5dd20/411127b/d0b66d4/本提交),对应行已标 ✅。🧠 决策已清零,🔲 待实现表已清空。

---

## 图例

| 标记 | 含义 |
|---|---|
| ✅ | 已实现/已定(读码即知现状) |
| ❌ | 缺口,未实现 |
| ⚠️ | 已实现但有隐患/待改/待定 |
| ❓ | 未查证(需先读码确认现状) |
| 📖 | 读码可定 — 代码已有 |
| 🔬 | 真机验证 — 须开盘跑脚本 |
| 🧠 | 设计决策 — 需人定 |
| ✅(方式) | TDD 实现 — 明确要写的代码 |

---

## A. 启动前准备(配置与依赖)

> 做什么:桥配置就位、组合/策略/股票池数据齐备。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| A1 | 桥地址/token | ✅ | 📖 | `config.iquant_bridge`,缺省 127.0.0.1:8790;token 从 `IQUANT_BRIDGE_TOKEN` 或 `.bridge_token` | ✅ 无问题 |
| A2 | `ACCOUNT` 硬编码 | ✅ | 📖 | 桥跑在 iQuant 客户端内,账号随客户端登录确定,写死合理,不改 | ✅ 保留写死 |
| A3 | `DRY_RUN` 切换方式 | ✅ | 🧠 | 实盘桥 / 模拟桥**分两个桥文件**部署,各自 `DRY_RUN` 不同。Core 端按 `mode` 连对应桥(→ 连带定 B3) | ✅ 两个桥文件 |
| A4 | `ALLOWED_STOCKS` 白名单 | ✅ | 🧠 | **取消白名单**,桥尽可能简化,信任 Core 端控制 | ✅ 取消 |
| A5 | 股票池成分股 | ✅ | 📖 | `StockPoolStock` 表,`_resolve_stock_codes` 读取(→ design §2.1)。有股票池数据即可 | ✅ 有条件即可 |
| A6 | 组合/策略/公式配置 | ✅ | 📖 | `PortfolioStrategy` + `Strategy` + `FormulaSignal` 表。有配置即可 | ✅ 有条件即可 |

---

## B. 前端发起实盘会话

> 做什么:前端选组合 → 建会话 → 点启动。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| B1 | `POST /sessions` 建会话 | ✅ | 📖 | `api/live.py:72`,建 LiveSession + 关联组合(→ design §1.1) | — |
| B2 | `POST /sessions/{id}/start` | ✅ | 📖 | `api/live.py:173`(→ design §1.2) | — |
| B3 | `mode` 字段语义 + 模式匹配 | ✅ | 🧠 | Core 按 `mode` 连对应桥(实盘桥/模拟桥,见 A3)。**启动会话时先匹配模式**:读 `session.mode` → 连对应桥地址 → 再组装引擎。不再依赖单一 `DRY_RUN`。**配置需两套桥地址**(mode→bridge_url 映射,扩 `config.iquant_bridge` 段) | ✅ 启动先匹配模式→连对应桥 |
| B4 | 前端实盘启动页 | ✅已实现 | ✅ | **工作台**(`LiveSessions.vue` 扩展):① SSE 实时事件日志(B4a)——消费 `/sessions/{id}/stream` 五类事件(signal/order/trade/position/risk)渲染为可读日志,运行中 session 自动连接、停止/卸载关闭、200 条封顶、ping 心跳不进日志;② 持仓/委托/成交三表聚合工作台 + 历史加载(B4b)——运行中读引擎内存态虚拟持仓、停止后从 live_trades 聚合;历史查询端点 `GET /sessions/{id}/orders?status=`(委托,可选状态过滤)、`/trades`(成交,time 倒序)、`/positions`(虚拟持仓,与 recover 同口径)。SSE position 事件按 code upsert,order/trade 事件顶部插入封顶 | ✅ 已实现(2026-08-10,411127b B4a + d0b66d4 B4b 后端 + 后续前端):纯函数 `liveEvents.ts`/`liveWorkbench.ts` 可单测(vitest 12+4 用例);事件日志 + 工作台均 TDD |
| B5 | SSE 成交/信号推送 | ✅已实现 | 📖 | `/stream` 当前只发 ping(→ design §1.5)。暂时不确定,后续再定,不阻塞主链路 | ✅ 已实现(2026-08-10,本提交):`LiveEngine._emit`/`stream_events` 广播五类事件(signal/order/trade/position/risk,均带 `portfolio_id`)+ `/stream` 端点运行中转发、空闲 30s ping、未运行 ping-only。前端已消费(2026-08-10,411127b):事件日志 + 工作台 |
| B6 | 多 session 并发限制 | ✅已实现 | 🧠 | **限制同一时刻全局只跑 1 个实盘 session**(2026-08-07 定)。简化隔离,避免多引擎争抢桥单线程 + 持仓归属混乱(Q1 未解)。**代码缺口**:`live.py:191` 现状 `if session_id in _ENGINES` 只防**同一 session 重复 start**(返回 running),**不防全局多个不同 session 并跑** → 待改成 `_ENGINES` 非空即拒绝任何新 start(返回错误提示已有 session 在跑) | ✅ 已实现(2026-08-10,3c826e8):`live.py:191` 同 session 重复 start 幂等返回 running;`_ENGINES` 非空即拒新 start(409 业务错误,提示已有 session 在跑) |

---

## C. Core 初始化引擎(_build_engine)

> 做什么:组装 Portfolio + 公式映射 + dispatcher + poller。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| C1 | 组装 Portfolio | ✅ | 📖 | `assemble_portfolio`(回测/实盘共用,→ design §2.1) | — |
| C2 | 公式映射 `{sid: formula_name}` | ✅ | 📖 | `_build_engine` 批量查 Formula(→ design §2.1) | — |
| C3 | **周期对齐规则** + 三段式实盘模式 + formula_count | ✅已实现 | 🔬+📖+🧠 | **三类周期 × 三种实盘模式**(2026-08-07 定):周期按"取数+求值+触发"分三类,不再统一走 BarPoller。**(A) 分钟级 `1m/5m/15m/30m/1h`——1m 单轮询 + 时间边界分发模式(→ C5)**:不用每周期各建 BarPoller。`_loop` 每 30s 拉一次 **1m bar**(相对变化判完成,保留 BarPoller 现有逻辑) → 新完成 1m bar 触发 1m 策略 on_bar;并按 1m bar stime 判定更长周期边界(`minute%5==0`→5m、`%15==0`→15m、`%30==0`→30m、`minute==0`→1h),边界到才拉该周期 bar + `compute_injected(period=该周期)` 注入求值 → 触发该周期策略 on_bar。**只用 1m 线**驱动全周期(1m stime 含所有更长周期边界信息),省 N 倍 HTTP。`_fill_signal_cache` 注入求值逻辑不变(已实现,live_engine.py:216),signal_cache key `(sid,code,bar.bar_time)` 与 on_bar 同 key → 命中。求值节拍=回测(每策略按自身 period 完成 bar——5m 策略只在 5m 边界到时算,不被 1m 节拍每分钟算)。**(B) `1d`——14:30 快照模式**:不走 BarPoller(相对变化判完成会"次日才完成",今日 1d 永远 latest 不触发,信号晚一天)。1d 已验是**当日盘中实时快照**(Close/量盘中刷新,verify_1d_snapshot.py),故 **每日 14:30 取一次**桥 /quote?period=1d 最新(forming)1d bar → `compute_injected(period="1d")` 算一次公式 → 填 signal_cache → 触发该批 1d 策略 on_bar 下单。1d 一日一算,盘中 14:30 出今日信号(回测只看完成日线,实盘看 14:30 forming 快照,此差异实时独有,接受)。**(C) `1w/1mon`——启动时通达信算模式**:桥端 xtdata **拉不到**(真机已验,xtdata 远程分支 `'NoneType' object is not iterable`,3 股皆空,→ Q4),但 **TQ+通达信本身支持 1w/1mon**(真机验过 8 周期注入与自取等价)。故 1w/1mon **不走桥**,session 启动时直接 `TQFormula.compute(period="1w"/"1mon")`(正常自取链路,formula.py:6,让 TQ 从通达信拉数据算,绕开桥端)算一次公式 → 存 signal_cache → **14:30 与 1d 统一触发下单**(1w/1mon 信号变化慢,不需单独盘中触发点,与 1d 合并到 14:30 日线级时点最简)。**周期边界**:`VALID_PERIODS`(strategies.py:23,策略创建唯一校验点)需从当前 6 个扩到 **8 个**`{1m,5m,15m,30m,1h,1d,1w,1mon}`——因 1w/1mon 改走通达信放行。前端 `PERIODS`(Portfolios.vue:33)同步扩。`60m/2h/4h/8h/1q/1y` 仍不放行(TQ periodstr error)。**count=200** 对分钟级 5 周期均够(→ Q4 决策4:按公式配 `Formula.formula_count`);1d/1w/1mon 单次快照算,count 取默认即可。**代码缺口**:当前 live.py:159 单一 `BarPoller(period="1m")` 硬编码但无边界分发,只驱动 1m;5m/15m/30m/1h 无边界拉取链路;1d/1w/1mon 无 14:30/启动链路 → 待改(→ C6) | ✅ 已实现(C6,c2e1482):1m 单轮询+边界分发 5m/15m/30m/1h / 1d 14:30 快照 / 1w+1mon 启动通达信算+14:30 统一下单;周期扩到 8(1w/1mon 走通达信放行);formula_count 字段已实现(#27,3b74cbf) |
| C4 | 股票池跨组合并集 + 注入去重 | ✅已实现 | 🧠+✅ | **现状已确认**:每个组合 × 每个子策略(各带 `ctx.period`) × 每只股票各拉一次 `query_quote`,跨组合/同组合多策略同周期同公式存在重复拉取+重复算 TQ。**隔离无风险**(`_check_risks` 只遍历本 ctx 持仓 + `code in bar.stocks` 守卫;`_signal_to_order` 只取 `ctx.positions`;`signal_cache` key 带 strategy_id)。**去重方案(2026-08-07 定)**:三维去重 `(股票, 周期, 公式)`,两层临时缓存(单 bar 生命周期,不跨 bar)。**拉取去重** `df_cache` key=`(code, period)` → 同 key 只 `query_quote` 一次。**计算去重** `raw_cache` key=`(code, period, formula_name)` → 同 key 只 `compute_injected` 一次(TQ 计算最贵,省这层收益最大)。缓存建在 `_on_bar` 级(跨组合共享),传给各 portfolio 的 `_fill_signal_cache`。**count 不进 key 的前提**:count 是 `Formula.formula_count` 公式级字段(→ 任务#27),同公式 count 恒定,故同 `(code, period, formula_name)` 的 count 必然相同,无需进 key。**不含 formula_arg**(当前公式无参数,留待将来;若加参数,去重 key 补 arg)。signal_cache key 仍带 strategy_id,每策略各有 cache 条目(值相同),隔离不变。**依赖**:#27 formula_count 字段(保证 count 一致,虽不进 key);实现见 #28 | ✅ 已实现(2026-08-10,3b74cbf):df_cache[(code,period)] 拉取去重(count 更大升级重拉)+ raw_cache[(code,period,formula)] 计算去重;缓存建在 _on_bar/_dispatch_period_bar/_maybe_daily_bars 级跨组合共享;注入 count 来自 Formula.formula_count(#27);边界/日终预拉用周期最大 count 够最长公式。5 个 C4 单测全过 |
| C5 | **30s 单轮询 + 1m 时间边界分发** | ✅已实现 | 🧠 | **(2026-08-07 定)**:实盘**只用 1m 线**作为轮询基线,**30 秒拉一次** 1m bar,按 1m bar 时间戳判定更长周期边界,边界到才拉该周期 bar。**机制**:(1) `_loop` 每 30s 调一次 `BarPoller(period="1m")` 拉所有股票 1m bar,用**相对变化判完成**(保留 BarPoller 现有逻辑,不依赖绝对时钟,只读 bar stime) → 新完成的 1m bar 触发 1m 策略 on_bar。(2) 对每根新完成的 1m bar,看其 stime 判定是否触达更长周期边界:`stime.minute % 5 == 0` → 5m 边界到;`% 15 == 0` → 15m;`% 30 == 0` → 30m;`stime.minute == 0` → 1h。(3) 边界到才拉该周期 bar(`query_quote(period="5m"/"15m"/"30m"/"1h", count=N)`,桥端这些周期走 xtdata 本地白名单,稳) → `compute_injected(period=该周期)` 注入算公式 → 填 signal_cache → 触发该周期策略 on_bar。**一拉一算**(边界到拉一次,非相对变化判完成那套——边界本身由 1m stime 确定已完成)。**不漏不重**:1m 边界每 60s 一次,30s 拉 → 最多延迟 30s 发现,不漏;5m 边界 300s 一次,30s 拉 → 延迟 ≤30s,不漏。**为何只用 1m 不直接拉各周期**:1m 是最细粒度,其 bar 时间戳天然含所有更长周期的边界信息(5m/15m/30m/1h 边界都是 1m 边界的子集),只拉 1m + 边界分发 = 一次 1m 拉取驱动全周期,比每周期各建 BarPoller 各拉省 N 倍 HTTP。**轮询间隔 30s**(替换原 poll_interval=15s):1m bar 60s 一根,30s 拉既能及时捕获完成又不浪费;5m+ 周期靠边界判定,30s 检查足够。**代码缺口**:当前 live.py:159 单一 BarPoller(period="1m") 硬编码但无边界分发,只驱动 1m;5m/15m/30m/1h 无边界拉取链路 → 待改(→ C6) | ✅ 已实现(C6,c2e1482):30s 拉 1m + stime 边界分发 5m/15m/30m/1h(到边界才拉);只用 1m 线,边界判定只读 1m stime 不引入本机时钟 |
| C6 | **三段式实盘周期链路实现**(周期对齐) | ✅已实现 | ✅+📖 | **现状缺口**:live.py:159 单一 `BarPoller(period="1m")` 硬编码,只驱动 1m 无边界分发;5m/15m/30m/1h 无边界拉取链路;1d 无 14:30 快照链路;1w/1mon 无启动通达信算链路(→ C3 三段式 + C5 边界分发)。**待改三段**:(A) 分钟级 `1m/5m/15m/30m/1h`——**单一 BarPoller(period="1m", count=10)**(只拉 1m),`_loop` 每 **30s** 调 `poller.poll()`(替换 poll_interval=15s)。poll() 内对每根新完成 1m bar:① 触发 1m 策略 on_bar;② 判定 stime 边界(`minute%5==0`→5m、`%15`→15m、`%30`→30m、`minute==0`→1h),边界到则对该批股票拉对应周期 bar(`query_quote(period=该周期, count=注入用)`)+ `compute_injected(period=该周期)` + 填 signal_cache + 构造 BarEvent(bar_time=该周期 bar stime)驱动该周期策略 on_bar。**边界判定只读 1m bar stime,不引入本机时钟**。可在一个新的分发器里实现(如 `PeriodDispatcher` 持 1m BarPoller + 边界逻辑 + 各周期策略路由),LiveEngine 调它。(B) `1d`——`_loop` 内每轮检查本机时间 ≥ 14:30 且当日未算过(`_last_daily_date` 标记),取桥 /quote?period=1d 最新 1d bar → `compute_injected(period="1d")` → 填 signal_cache → 构造 BarEvent 驱动 1d 策略 on_bar。(C) `1w/1mon`——`engine.start()` 时(或 recover 后)对 1w/1mon 策略调 `TQFormula.compute(period=ctx.period)`(正常自取,绕开桥)算一次 → 存 signal_cache;14:30 时点与 1d 合并,用同一 BarEvent 触发 1w/1mon 策略 on_bar 下单。**实现要点**:14:30 用本机 Asia/Shanghai 时钟(实盘固有时点,非 bar 完成判定,可用绝对时钟);30s 轮询够覆盖 14:30 触发(误差 ≤30s)。**周期边界同步改**(C3 定:`VALID_PERIODS` 扩到 8 个):`strategies.py:23` `VALID_PERIODS` 加 `1w,1mon`;前端 `Portfolios.vue:33` `PERIODS` 同步加;`web/src/api/index.ts:84,141,159` period 注释补 `1w,1mon`。**依赖**:C3+C5 已定规则,此项纯实现,可休市 TDD(1m 边界分发 + 14:30 时点 + 启动算注入,均可 mock 桥/TQ 测) | ✅ 已实现(2026-08-10 TDD,c2e1482):C6(A) 1m 单轮询+stime 边界分发 5m/15m/30m/1h(latest_completed_bar 避免 forming bar);C6(B) 14:30 桥 /quote?period=1d 快照驱动 1d;C6(C) 1w/1mon 启动 TQFormula.compute 通达信注入+14:30 统一驱动(日切重注);VALID_PERIODS 扩 8 + 前端同步;20+ C6/E8 单测全过 |

---

## D. 持仓恢复(recover)

> 做什么:Core 重启后从 `live_trades` 重放重建虚拟持仓/现金。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| D1 | 重放 live_trades 重建持仓 | ✅ | 📖 | `live_engine.py:367`(→ design §3.1) | — |
| D2 | 虚拟现金以成本计 | ✅ | 📖 | AGENTS §93,与在线时 `Account.apply_trade` 一致(→ design §3.2) | — |
| D3 | **对账(虚拟 vs 桥 /positions)** | ✅已实现 | 🔬+✅ | recover 后查桥 `/positions` 按 code 聚合比对。**2026-08-10 真机已验字段**:POSITION 对象 `m_strInstrumentID=600000`+`m_strExchangeID=SH`(拼接 `600000.SH` 对账可行)、`m_nVolume`(总持仓)、`m_nCanUseVolume`(T+1 可用)、`m_nYesterdayVolume`/`m_nCoveredVolume`/`m_nOnRoadVolume`(昨仓/今仓/在途)。**桥 query_positions 已改**:instrument+exchange+volume+available(=m_nCanUseVolume)+yesterday/on_road/market_value。不一致如何处理(告警/以真实为准修正)仍待定 → 归到切片5 对账实现 | 🧠 已定+✅ 已实现(仅告警不修正):recover 末尾 `_reconcile_positions` 按 code 聚合虚拟净持仓 vs 桥 volume,差异记 `_reconcile_mismatches`+告警日志,不自动改账(在途单/延迟期防误改);桥离线跳过不崩。自动校准留待真机跑顺再放开 |
| D4 | 熔断计数读回 | ✅已实现 | ✅ | `LiveSessionPortfolio.circuit_breaker_count` 字段存在但引擎未读写。重启后累计次数丢失,recover 时读回(→ design §8.3) | ✅ 已实现(recover 读回):`recover()` 读 `circuit_breaker_count` → `risk_manager.consecutive_drawdown_triggers`;达 3 次 → `manual_recovery=True`+`circuit_breaker_active=True`(停新开仓等待人工);<3 次单日熔断当天已恢复不补挂(单一计数模型)。预置 `_breaker_count_written` 避免首 bar 重复写 |
| D5 | recover 时机 | ✅ | 📖 | start 时 `engine.recover(db)`,在 `engine.start()` 之前 | — |

---

## E. 循环取 bar(主循环 _loop)

> 做什么:心跳 → 拉行情 → 触发 on_bar → (回填) → (风控日终) → sleep。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| E1 | 心跳检测桥在线 | ✅ | 📖 | `dispatcher.heartbeat()` = GET /ping(→ design §4.2) | — |
| E2 | BarPoller 相对变化判完成 | ✅ | 📖 | 不依赖绝对时钟(→ design §4.3) | — |
| E3 | 多股票按 code 独立判定 | ✅ | 📖 | 同时间戳合并为 BarEvent | — |
| E4 | 首次 poll 建基线不触发 | ✅ | 📖 | 不回放历史 | — |
| E5 | **风控日终 `risk_manager.update` 接线** | ✅已实现 | ✅+🧠 | 实盘 `_loop`/`_handle_bar` **从未调用** `risk_manager.update` → 熔断(§88)+日内亏损暂停**完全哑火**。回测每 bar 都调(`backtest_engine.py:84`)。**(2026-08-07 定接线方式)**:实盘分两步调——**每 bar 调 `update_peak`**(实时盯回撤+熔断次日恢复),**日终 14:30 调 `update_daily`**(日内盈亏+日内暂停次日恢复)。`update` 拆两方法,原 `update` 保留(=peak+daily 合集,回测每 bar 调,语义不变,向后兼容)。`Portfolio.on_bar` 已咨询 `is_trading_halted()`(`portfolio.py:70`)剥 BUY,无需改。`total_value` 复用回测 `_total_value` 逻辑(现金+持仓按 close 市值)。14:30 时点与 C6 1d 快照/1w/1mon 统一下单同一时点,复用本机 Asia/Shanghai 时钟。**2026-08-10 已实现(TDD)**:`risk_manager.py` 拆 `update_peak`(跨日刷新 prev_close+peak+熔断)/`update_daily`(daily_loss),`update`=合集向后兼容;`live_engine.py` `_handle_bar` 每 bar 调 `update_peak`(新增 `_total_value` 辅助)+ `_loop` 加 `_maybe_daily_close`(14:30 一次,`_last_daily_date` 幂等);181 单测+99 集成全过,新增 5 个分钟级单测(跨日刷新/14:30一次/分钟不误触/分钟熔断/update兼容) | ✅ 已实现并接线(2026-08-10 TDD,测试全过) |
| E6 | 熔断/日内亏损分钟级基准 | ✅ | 🧠 | 分钟级 bar 下「日终」语义**(2026-08-07 定)**:(1) `current_date` = `bar.bar_time.date()`(与回测 `t.date()` 一致);(2) `prev_close`(日内盈亏基准)= **昨日最后一根 bar 的 total_value**(=昨日收盘),`update_peak` 内**跨日检测**(`current_date != _last_bar_date`)时刷新 `prev_close_value = _last_bar_total_value`,记录 `_last_bar_date`/`_last_bar_total_value`;(3) 日内盈亏 `daily_pnl = total_value(14:30) - prev_close(昨日收盘)`;(4) 日切重置:跨日时 `update_peak` 自动刷新 `prev_close`;`daily_pause`/`circuit_breaker` 次日恢复由 `current_date > trigger_date` 判定(已有逻辑)。**实盘"日终"= 14:30**(与 C6 1d 快照统一时点,本机 Asia/Shanghai 时钟触发),接受 14:30 总市值作当日收盘基准(14:30-15:00 波动不影响:次日开盘 `update_peak` 的 peak 更新捕获回撤;日内已暂停则不开新仓)。**为何不每 bar 调 daily_loss**:`update` 的 `prev_close_value = total_value`(line 115)每次覆盖,分钟级每 bar 调会让 `daily_pnl` 退化成"分钟间盈亏"被分钟抖动误触发 daily_loss_limit → 故 daily_loss 只日终算一次,prev_close 只跨日刷新。**向后兼容**:回测每 bar 调原 `update`(=peak+daily),每 bar 跨日 → 每 bar 刷新 prev_close = 上一 bar total_value,`daily_pnl = 今日-昨日`,语义不变 | ✅ `current_date=bar.bar_time.date()`;`prev_close`=昨日最后一根 bar total_value(`update_peak` 跨日刷新);日终=14:30;`daily_pnl=14:30总市值-昨日收盘`;回测兼容 |
| E7 | 离线暂停 vs 停循环 | ✅ | 📖 | 离线标 `_bridge_online=False`,暂停下单但循环继续重试心跳 | — |
| E8 | 桥离线期间错过的 bar | ✅ | 🧠 | **(2026-08-07 定:不补)** 桥恢复后,错过的已完成 bar **丢弃**,只把 `_last_completed` 水位推进到恢复时拉到的最新完成 bar,不触发 on_bar。**为何不补**:实盘信号有时效性,离线期间信号基于那时的 bar,恢复时价格已变,补下单 = 过时信号 + 现价成交,价格错位;且离线 N 分钟补 N 根 bar 会集中触发 N 批 on_bar → N 批订单短时砸桥(桥单线程),叠加多策略更甚,与 E5 风控也不友好。**实现**:BarPoller 加"恢复后首次 poll 重建基线"模式——`_loop` 心跳从离线转在线时(`_bridge_online` False→True),标 `poller._initialized = False`(或加 `_resync` 标志),下次 `poll()` 走 line 157-159 首次基线分支(只记录 `_last_completed`,不触发),之后恢复正常触发。水位推进到恢复时最新完成 bar,中间错过的 bar 自然丢弃。**与 E7 衔接**:E7 离线时 `_loop` `continue` 不调 `poll()`(水位冻结);恢复时 E8 重置基线跳过补触发。**代码缺口**:BarPoller 无重置基线入口(只 `_initialized` 内部管),`_loop` 无离线→在线转场检测 → 待实现(可休市 TDD) | ✅ 已实现(2026-08-10 TDD,c2e1482):`BarPoller.reset_baseline()`(置 `_initialized=False` + 清 `_last_completed`) + `_loop` 心跳离线→在线转场(`not was_online and online`)时调;恢复后首次 poll 只推水位不触发,中间 bar 丢弃;reset_baseline 单测全过 |

---

## F. 公式触发下单(单 bar 处理)

> 做什么:公式注入算信号 → 取信号+风控+优先级 → 下单 → 落库。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| F1 | 公式注入 `_fill_signal_cache` | ✅ | 📖 | 0010,拉历史→内存注入→填 cache(→ design §5.3) | — |
| F2 | 信号优先级(风控>公式) | ✅ | 📖 | CLOSE>REDUCE>ADD>OPEN(→ design §5.4) | — |
| F3 | 主从联动约束 | ✅ | 📖 | 从策略 OPEN 只能买主策略持有的股 | — |
| F4 | BUY 资金审批 | ✅ | 📖 | `account.approve_order`(→ design §5.5) | — |
| F5 | **SELL 量上限(T+1)** | ✅已实现 | 🔬+✅ | **2026-08-10 真机已验**:POSITION 对象 `m_nCanUseVolume` **精确反映 T+1 可用**——持仓 600(昨仓 200+今买 400),`m_nCanUseVolume=200`、`m_nCoveredVolume=400`、`m_nOnRoadVolume=400`,今日买入 400 不可卖 ✅。**桥 query_positions 的 available 已改取 `m_nCanUseVolume`**(原取 ACCOUNT 的 `m_dAvailable` 是资金非股数,且 POSITION 对象上无此字段→null)。实现:`LiveT1Checker` 改 `min(本策略持有量, 桥 available)` | ✅ 已实现(2026-08-10 TDD,3c826e8):`LiveT1Checker` 持 `_available_map`(LiveEngine 每 bar 刷一次 /positions,强引用去重),`min(持有量, m_nCanUseVolume)`;桥无该仓/未取到→全量放行(券商端 T+1 兜底,G6 处理拒单);`_handle_bar` 先 `cap_quantity` 再落 submitted,DB 量=实发量 |
| F6 | **同 bar 多策略超卖** | ✅已实现 | 🧠已定 | A 卖 600 + B 卖 400,券商 available 只 800。需「bar 内可用量递减记账」?(→ open-questions Q2) | 🧠 已定+✅ 已实现:bar 内可用量递减记账。`_refresh_available_map` 每 bar 重设快照,`LiveT1Checker.consume_available` 在 SELL 发单成功(`_handle_bar` ③桥受理后)扣减——A 卖 600 后 B 只见 200,不超卖;拒单/失败不扣,扣过量钳到 0,券商端仍兜底 |
| F7 | 成交时机 | ✅ | 📖 | 当根 bar 立即成交(非下一 bar open) | — |
| F8 | 成交价近似 | ✅已修正 | — | 用 `bar.close`,prType=14 实际是盘口一档价。切片5 回填修正(→ design §5.6) | ✅ 切片5 已修正:成交价/量/佣金取 /deals 真实回报,成交均价=金额/量(`_backfill_order`,live_engine.py:741),不再用 bar.close 近似 |
| F9 | 佣金/印花税 | ⚠️已知 | — | 首期 0,真实成本从 /deals 回报取 | ⚠️ 佣金已实现:取 /deals commission(`_backfill_order`);印花税仍 0(DEAL 印花税字段待真机验证) |
| F10 | 落库 `status=accepted` | ✅已实现 | ✅ | 应拆 `submitted`,切片5(→ design §5.7) | ✅ 切片5 已拆(G1,eb4bc40):先写 `LiveOrder(status=submitted)`+commit 再发 passorder(`_persist_order_submitted`);submitted 阶段不 apply_trade、不写 LiveTrade,回填确认 filled 才 apply |

---

## G. 下单与成交同步(切片5 核心区)

> 做什么:订单状态机 + 轮询 /deals 回填 + 修正虚拟持仓。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| G1 | 订单状态机 | ✅已实现 | ✅ | submitted→partial→filled/rejected(→ design §7.1)。**提交时序要求(2026-08-07 补,从 I4 崩溃分析定)**:`_persist_trade` 改成**先写 `LiveOrder(status=submitted)` + commit,再(异步)发 passorder**——确保崩在 passorder 已发、未确认窗口时 DB 至少有 submitted 记录,供 I4 查未完结挂回。现状 `_persist_trade` 是 place_order 后才写 LiveOrder+LiveTrade 一起 commit(live_engine.py:333-364),崩在 commit 前 → DB 无记录但券商可能已成交 → 背离。submitted 阶段**不 apply_trade、不写 LiveTrade**(回填确认成交才写) | ✅ 已实现(切片5,eb4bc40):`_persist_order_submitted` 先写 submitted+commit 再发 passorder(I4 命门窗口闭合);submitted 不 apply,`_poll_deals` 回填确认 filled 才 `_apply_filled_trade`;partial 只写 LiveTrade 不 apply |
| G2 | 主循环轮询 /deals | ✅已实现 | ✅ | 已定:主循环轮询(已选)。每轮查未完结 LiveOrder(→ design §7.2) | ✅ 已实现(切片5,eb4bc40):`_loop` 每轮 `_poll_deals` 查未完结单→`_try_match_order_ref` 定位→按 `m_strOrderRef` 过滤 /deals→`_backfill_order` 回填;桥离线本轮跳过下轮重试 |
| G3 | **订单匹配键** | ✅**定案** | 🔬✅ | **2026-08-10 真机定案:匹配键 = `m_strOrderRef`(委托引用号)**。DEAL 与 ORDER 对象**共享同一 `m_strOrderRef`**,3 笔真实成交(BRIDGE×2 + GUI×1)全部对上:`...3499794`↔`...3499794`、`...502163`↔`...502163`、`...502165`↔`...502165`;`m_strOrderSysID`(合同号)同样一致。**passorder 返回 0 无法预知 OrderRef**,故匹配流程:Core 下单→轮询 `/orders` 用组合键(股票+方向+数量+下单后时间窗口;全局限 1 session 串行→取最新未匹配 ORDER 即自己的单)定位自己的委托→取 `m_strOrderRef` 回写 LiveOrder→轮询 `/deals` 按 OrderRef 关联成交→回填 | ✅ 匹配键定案=`m_strOrderRef`(ORDER↔DEAL 共享,真机 3 笔全对上);实现见 G2 |
| G4 | `/deals` 字段够不够 | ✅**定案** | 🔬✅ | **2026-08-10 真机摸全**:DEAL 对象字段齐全——`m_strOrderRef`(匹配键)、`m_strOrderSysID`、`m_strTradeID`(成交编号)、`m_strTradeTime`/`m_strTradeDate`(成交时间)、`m_nDirection`(48买/49卖)、`m_dTradeAmount`、`m_dCommission`、`m_strSource`(BRIDGE/GUI)、`m_strOrderStrategyType`(函数下单/常规下单)。**桥 query_deals 已改**:原取 `m_nOrderID`(对象上不存在→null,是 order_id 一直为 null 的根因),现返回 order_ref/order_sysid/trade_id/instrument/exchange/direction/price/volume/amount/commission/trade_time/trade_date/source/order_type。**ORDER 对象同理补全**(query_orders 已改,status 用 `m_nOrderStatus` 54撤/56成) | ✅ 字段已全摸 + 桥 query_deals/query_orders 已改;根因=m_nOrderID 字段不存在 |
| G5 | 回填轮询频率 | ✅已实现 | 🧠已定 | 跟 poll_interval(15s)同?还是单独更短?成交回报秒级,15s 可能太慢 | 🧠 已定+✅ 已实现:独立更短轮询 5s。`_deals_loop` 独立 asyncio task(`deals_poll_interval=5.0` 默认)处理 `/deals` 回填,主循环仍 30s 拉 bar——成交秒级落库,持仓/资金近实时 |
| G6 | **拒单/部分成交修正** | ✅已实现 | ✅ | 当前按全量成交记账,拒单需反向退回(加回现金/持仓/还原 _lots)。`apply_trade` 只有正向,需补反向或重放修正(→ design §7.4) | ✅ 已实现(切片5,eb4bc40):`_backfill_order` 据真实成交回填(总成交量/金额/佣金,成交均价=金额/量),filled 才 `_apply_filled_trade` 落持仓;拒单置 `status=rejected` 不 apply;partial 只写/更新 LiveTrade 不 apply,等最终 filled 或撤单 |
| G7 | 桥状态并入 session API | ✅已实现 | ✅ | 0009 §11.5 要求并入 `/sessions/{id}` 详情(→ design §1.4) | ✅ 已实现(切片5,eb4bc40):session 详情并入 `bridge_online`(实时 /ping)+ `pending_orders` 在途计数 + `last_backfill_time`;另有独立 `/bridge-status` 端点 |

---

## H. 休市/停盘

> 做什么:用户停会话 / 收盘后状态留存。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| H1 | `POST /sessions/{id}/stop` | ✅ | 📖 | 取消循环任务,status=stopped(→ design §1.3) | — |
| H2 | 休市时桥是否在跑 | ✅ | 🧠 | **(2026-08-07 定:不通知)** 桥无 `/shutdown` 端点(只有 ping/order/positions/account/orders/deals/quote),是 iQuant 客户端内策略,Core 无法让它停。Core stop session 后桥继续监听但无请求 = 空转无害;下次 start session 桥还在,省重连。不改桥 | ✅ 不通知,桥空转无害 |
| H3 | 持仓快照留存 | ✅ | 🧠 | **(2026-08-07 定:不存快照,只靠 live_trades 重放)** 实盘无快照表(只有回测 `BacktestDailySnapshot`);`recover`(live_engine.py:367)重放 `live_trades` 重建持仓+现金,逻辑完整,成本 O(交易笔数),单用户毫秒级可忽略。**为何不存**:(1)快照不增加对账能力——D3 拿重放结果即可对账,快照只是"某一刻的",无额外价值;(2)快照不解决非交易状态丢失——熔断计数/peak/prev_close 那是 D4/H4 的活,不是持仓;(3)快照+live_trades 两套源,写快照后又成交没更新 → 脏数据风险;(4)只省可忽略的重放成本,却引入双源维护负担。**单一事实源 = live_trades**,不引入快照表 | ✅ 不存快照,只靠 live_trades 重放;非交易状态走 D4/H4 |
| H4 | 熔断计数持久化 | ✅已实现 | ✅ | 触发熔断时写 `circuit_breaker_count`,目前不写(→ design §8.3) | ✅ 已实现:`_handle_bar` 每 bar update_peak 后调 `_persist_breaker_count`——max_drawdown 触发(计数+1)才落库(未变跳过,避免每 bar 写);达 3 次 status 转 `circuit_broken` |
| H5 | 次日开盘自动恢复 | ✅ | 🧠 | **(2026-08-07 定:手动)** 停盘后次日不自动 start,用户手动点启动。与 B6 限 1 个 session 一致(避免自动恢复撞上用户已开的新 session);`recover` 重放 live_trades 已能重建持仓,手动 start 即接上。不引入定时任务,运维可控 | ✅ 手动启动 |

---

## I. 异常与重启

> 做什么:桥离线、Core 崩溃、部分成交等异常处理。

| # | 细节 | 状态 | 方式 | 说明(→ 参照) | 确认结论 |
|---|---|---|---|---|---|
| I1 | 桥离线暂停下单 | ✅ | 📖 | `_bridge_online=False`,不抛异常继续循环 | — |
| I2 | 下单时桥离线 | ✅ | 📖 | `_on_bar` 捕获 `BridgeUnavailableError` | — |
| I3 | Core 崩溃重启 → recover | ✅ | 📖 | 从 live_trades 重放(但不对账,见 D3) | — |
| I4 | 部分成交跨重启 | ✅已实现 | 🧠 | **(2026-08-07 定:查未完结挂回等回填)**。两种崩溃场景分析:**(A)Core 崩,桥在跑**:Core 内存态丢,桥端订单在券商服务器不受影响;`recover` 重放 `live_trades` 重建持仓。命门窗口 = `passorder` 已发券商、`_persist_trade` 未 commit 就崩 → DB 无记录但券商有真实成交 → 背离。**(B)桥崩(iQuant 重开策略),Core 在跑**:桥内存态全清(`_placed`/`_quote_cache` 纯内存无持久化),Core 心跳发现离线暂停下单(E7 已实现);桥重开新 init 起来,真实订单不丢(券商侧数据,`get_trade_detail_data` 仍能查);行情缺口 E8 已定不补。命门窗口同 A = Core 发 /order、桥已 passorder 发券商、响应返回前崩 → Core 未确认 → 背离。**共同命门**:passorder 已发券商、Core 未落库/未 apply_trade → DB 无记录 vs 券商有成交 → 虚拟持仓背离。**闭合方案(依赖 G1)**:(1)`_persist_trade` 改先写 `LiveOrder(status=submitted)` + commit,再发 passorder → 崩在此窗口后 DB 至少有 submitted 记录;(2)`recover` 重放 live_trades(已成交)+ **查未完结 `status in (submitted,partial)` 挂回引擎待回填队列**;(3)G2 主循环 /deals 轮询回填挂回的单 → G6 据真实成交补记/修正。**I4 只做"查未完结 + 挂回队列"**,不做对账(D3)、不做修正(G6)、不做匹配(G3)。**真机卡点**:挂回的单要和券商侧成交对上,依赖 G3 订单匹配键稳定性(passorder 返回 0,`/deals` m_nOrderID 对不上 MD5)——G3 不稳则 I4 挂回也白搭。**前提**:G1 订单状态机就位(submitted 阶段不 apply_trade、不写 LiveTrade,回填确认才写) | ✅ 规则定 + 已实现(切片5,eb4bc40):`recover` 查未完结(submitted/partial)挂回引擎待回填队列,主循环 `_poll_deals` 按 `m_strOrderRef` 匹配 /deals 补记(G2/G6);前提 G1(先写 submitted)+ G3 匹配键真机定案均已就位 |
| I5 | 桥端 `_placed` 缓存跨重启 | ✅ | 🧠 | **(2026-08-07 定:无风险,关闭)** 读码确认:Core 每根 bar 生成新 OrderEvent,`_persist_trade` 写库后订单完成,**不会重发同 order_id**。`_placed`(桥内存 order_id→result)是防同一 bar 内桥回调重入(实盘无此场景)。桥重启后 `_placed` 清空无影响,因 Core 不重发。无需处理 | ✅ 无风险,关闭 I5 |

---

## 推进路线(按确定方式归组)

### 🔬 真机验证(等开盘,卡点项)

> **2026-08-10 已全验清空**:G3(匹配键=`m_strOrderRef`,真机 3 笔全对上)、G4(DEAL/ORDER 字段全摸 + 桥已改)、F5(`m_nCanUseVolume` 精确 T+1)、D3(/positions 字段拼接后缀可行)均已定案,见对应行。
> **重大真机发现**:桥下单在「**模拟交易**」模式下 passorder 只出策略信号不发委托(迅投硬规则);切「**实盘交易**」模式 + 仿真盘账号后,**桥 HTTP→passorder→真实成交链路跑通**(限价/对手盘都成交,多笔连续验证)。桥策略必须以实盘模式运行。

### ✅ 已完成的 TDD 实现

| # | 细节 | 提交 |
|---|---|---|
| C6 | 三段式实盘周期链路(1m 单轮询+边界分发 5m/15m/30m/1h + 1d 14:30 快照 + 1w/1mon 启动通达信算) | c2e1482 |
| E8 | 桥离线恢复不补 bar(`BarPoller.reset_baseline` + `_loop` 转场检测) | c2e1482 |
| G1 | 订单状态机(先写 submitted+commit 再 passorder) | eb4bc40 |
| G2 | 主循环轮询 /deals(按 `m_strOrderRef` 关联成交回填) | eb4bc40 |
| G6 | 拒单/部分成交持仓修正(回填确认 filled 才 apply) | eb4bc40 |
| G7 | 桥状态并入 session API(`bridge_online`/`pending_orders`/`last_backfill_time`) | eb4bc40 |
| F10 | 落库 status 拆 submitted | eb4bc40 |
| #27 | `Formula.formula_count` 字段+迁移+前端公式页(count 按公式配) | 3b74cbf |
| #28 | C4 三维去重(拉取 `(code,period)` + 计算 `(code,period,formula)`) | 3b74cbf |
| B5 | SSE 事件流(`_emit` 五类事件 signal/order/trade/position/risk + `/stream` 端点转发,前端消费留待 B4 实盘启动页) | 本提交 |

### 🧠 设计决策(需人定,不阻塞读码)

（当前无待定决策。F6 bar 内可用量递减记账、G5 独立 5s 回填轮询已定并实现。）

### ⚠️ 桥端待改(需改 live/,Py3.6 兼容)

| # | 细节 | 依赖 |
|---|---|---|
| ✅已改 | `ACCOUNT` 硬编码常量 | ✅ 2026-08-12:iQuant 无读取登录账号 API(`ContextInfo` 仅 `set_account`),账号作常量写进桥文件顶部,换账号改这一行;双桥 `iquant_bridge.py`(仿真 8790)/`iquant_bridge_live.py`(实盘 8791)各自一处;纯 ASCII,`py_compile` 过 |
| ✅已改 | `query_deals`/`query_orders`/`query_positions` 字段已补全(`m_strOrderRef`/`m_nCanUseVolume` 等,见 G4/D3/F5 行) | ✅ 2026-08-10 |
| ✅已改 | `_do_place` 支持 `pr_type`(0 限价/14 对手价)——已恢复硬编码 14(对手价) | ✅ 2026-08-10 |
| ✅已写 | **桥部署 README + 「实盘交易」模式要求**——`live/bridge/README.md` 部署/账号/TOKEN/白名单配置说明,注明必须以实盘交易模式运行(模拟模式 passorder 不发委托,真机验证) | ✅ 2026-08-10 |

---

## 优先级建议

1. **✅ 已完成:E5/E6 熔断接线**(2026-08-10 TDD,测试全过)——实盘熔断/日内亏损已接线。
2. **✅ 已完成:C6 三段式实盘周期链路 + E8**(c2e1482)——1m 边界分发 5m/15m/30m/1h + 1d 14:30 快照 + 1w/1mon 通达信注入;离线恢复不补 bar。
3. **✅ 已完成:切片5 订单同步 G1/G2/G6 + G7 + F10 + I4**(eb4bc40)——订单状态机、/deals 回填(按 `m_strOrderRef`)、桥状态并入 session、submitted 拆分、recover 挂回未完结单。
4. **✅ 已完成:F5 + B6**(3c826e8)——`LiveT1Checker` 接桥 `m_nCanUseVolume`(每 bar 刷一次 /positions,SELL 封顶);全局限 1 个实盘 session(`_ENGINES` 非空即拒)。
5. **✅ 已完成:#27 formula_count 字段 + #28 C4 三维去重**(3b74cbf)——公式级注入根数(字段+迁移+前端公式页);df_cache 拉取去重 + raw_cache 计算去重(省重复 TQ 计算)。
6. **✅ 已完成:D4/H4 熔断计数读回/持久化**——max_drawdown 触发计数落库(`circuit_breaker_count`,未变不写),recover 读回;达 3 次转手动恢复(停新开仓等待人工)。
7. **✅ 已完成:F6 同 bar 超卖 + G5 独立回填轮询**——SELL 发单成功后 `consume_available` bar 内可用量递减记账(多策略同 bar 卖不超券商 available);`/deals` 回填拆独立 5s 轮询(`_deals_loop`),主循环仍 30s 拉 bar。
8. **✅ 已完成:D3 对账(仅告警不修正)**——recover 末尾按 code 聚合虚拟持仓 vs 桥 /positions,差异记 `_reconcile_mismatches`+告警日志,不自动改账;桥离线跳过不崩。
9. **✅ 已完成:桥部署 README + A2 账号改配置** — `live/bridge/README.md` 部署/账号/TOKEN/白名单说明 + 实盘交易模式硬要求;`ACCOUNT` 改 env/文件配置(同 `load_secret` 模式)。
10. **🧠 决策项** — 当前无待定决策(F6/G5/D3/A2 已定);随时可过。

---

## 真机日志核对(2026-08-12,sec 实例)

> 实例 `sec`(LiveSession id=2,mode=simulation→8790 仿真桥)启动后桥日志核对。
> 目的:验证预热缓存+增量拼接优化(commit 5704778)在真机按预期生效,并记录池配置对账方法。

### 配置侧(sec 绑定)

- 绑定 2 个组合:**组合1「测试」**(股票池 `etf` id=5,17 只 ETF)+ **组合2「第二」**(股票池 `第二股票` id=2,9 只 ETF)。
- **两组合不同池**(`etf` 17 只 ⊃ `第二股票` 9 只),非共享池。组合2 的 9 只是组合1 的子集。
- 公式统一 `MACROSSPRO`(formula_id=1,formula_count=30)。
- 周期分布:组合1 = {1d, 5m, 1m};组合2 = {1d, 5m, 30m}。
  - 1m 策略仅组合1 → 预热只覆盖 etf 池 17 只
  - 5m 策略两组合都有 → 预热覆盖并集 17 只(9⊂17)
  - 30m 策略仅组合2 → 预热只覆盖第二股票池 9 只
  - 1d 在预热跳过名单(走日终 14:30 `_maybe_daily_bars`),不预热

### 日志对账结论

| 日志阶段(桥端 `xtdata ok ... count=N got=N`) | 对应引擎路径 | 是否符合预期 |
|---|---|---|
| 启动一次性 5m/1m/30m × count=30 拉取 | `_preheat()` 按 `_code_period_count` 只拉有策略的 (code,period) | ✅ count=30=formula_count(非兜底 200);只数=该周期有策略的股票数 |
| 跳过 1d/1w/1mon 预热 | `_preheat` 内 `if period in ("1d","1w","1mon"): continue` | ✅ 日志无 1d 预热 |
| 每 ~30s 一拨 1m count=10 × 全池 | `BarPoller.poll()` 完成检测(self._count=10,只用 stime 判完成) | ✅ 固定 count=10,不走预热 |
| 每个分钟边界后多一拨 1m count=10 | `_on_bar→_fill_signal_cache→_get_bars_with_increment` **增量拉**(INCREMENT_COUNT=10) | ✅ 关键证据:count=10 而非 count=30,说明命中预热缓存走增量,未每分钟重拉 30 根 |
| 5m 边界(14:05)一拨 5m count=10 | `_dispatch_period_bar("5m")` 走增量 | ✅ count=10 增量,非 count=30 全量 |

### 优化效果量化

对比优化前(每边界全量拉 count=30):
- 1m 公式注入:18×30 → 18×10 根/分钟(**-67%**)
- 5m 边界:18×30 → 18×10 根/5 分钟(**-67%**)
- 30m 边界:9×30 → 9×10 根/30 分钟(**-67%**)
- 请求数不变(仍一 code 一请求),**每请求传输 bar 数降到 1/3**,正是 commit 5704778「降桥 /quote 拉取量」目标。

### 发现的配置侧问题(非引擎 bug,已闭环)

1. **159058.SZ 全程 `xtdata empty`**:该券在两个池中桥端均无数据(疑似退市/本地从未下载)。引擎按启动时池快照拉取,因预热 `if not bars: continue` 未缓存,之后每周期走"缓存未命中→全量补"路径,持续空拉浪费。**用户已从池中删除该券**,下次启动不再拉取,自愈。
2. **日志时间(14:00:24)早于 sec 当前 started_at(14:10:43)**:核对确认该日志是**删除 159058 之前**那次启动留下,引擎按当时池快照(含 159058)拉取,行为正确;当前 DB 池已不含 159058。

### 核对方法(可复用)

1. `LiveSession` 按 name 查 → 取 id/mode/started_at;`LiveSessionPortfolio` 取绑定 portfolio_strategy_id 列表。
2. 每个 `PortfolioStrategy` 取 `stock_pool_id` → `StockPoolStock` 取成分股(注意:不同组合可能绑不同池,不能假设全共享)。
3. `Strategy` 取 portfolio_id 下各策略 period + formula_id → `Formula` 取 formula_count。
4. 据此推算 `_code_period_count` 的预期 key 数与 count,与桥日志的预热阶段(code×period×count)逐项对账。
5. 日志 `count=N` 预热阶段应 = formula_count(或跨公式取 max);运行期增量阶段应 = INCREMENT_COUNT(10);全量补(缓存未命中,如 empty 券)会回到 count=全量。

---

## 真机日志核对 2(2026-08-12 14:18–14:35,sec 实例第二轮)

> 第一轮(14:00–14:05)验证了预热优化。第二轮更长日志(14:18–14:35)跨过 **14:30 日终**,首次出现真实下单(4 笔,全被桥拒),挖出 3 个引擎问题 + 1 个桥配置问题。四个问题已全部修复(2026-08-12):① 15m 空拉、② 同根 bar 二次驱动、③ 组合2 跨池下单、④ MAX_VOLUME 拒单 + 错误回显,见下方各发现 ✅。

### 14:30 日终时段完整拉取时序(桥日志逐行)

```
14:30:00-01  1m ×17 count=10     BarPoller.poll(30s 节拍)
14:30:02-03  5m ×17 count=10     _on_bar 边界分发(14:30 是 5m 边界)
14:30:05-06  15m ×17 count=200   _dispatch_period_bar("15m")  ← 实例无 15m 策略,白拉
14:30:06-07  30m ×17(9 只 count=10 + 8 只 count=30)   _dispatch_period_bar("30m")
14:30:08-09  1d ×17 count=30     _maybe_daily_bars(14:30 日终,只拉 1d/1w/1mon)
14:30:09     4 × POST /order     两组合 1d 策略同根 1d bar 触发 OPEN
14:30:40     1m ×17 count=10     下一轮 poll
14:30:48-49  15m ×17 count=10    再次 _dispatch_period_bar("15m") ← 同根 bar 二次驱动
```

### 四个发现(按严重度)

1. **实例无 15m 策略,14:30 却拉 15m**:`periods_on_boundary` 是纯算术(`minute%15==0`→15m),不查实例有无该周期策略。`_dispatch_period_bar` 对全 stock_codes 拉,15m 不在 `_code_period_count`/`_period_count` → 兜底 count=200,白拉 17 只后 period 过滤全跳过。
   **✅ 已修复**:`live_engine.py` `_dispatch_period_bar` 入口加 `if period not in self._strategy_periods: return`;`__init__` 加 `_strategy_periods`(所有策略周期集合,含无公式策略,防误伤无公式策略的风控单)。15m 从此不拉。带回归测试 `test_dispatch_period_bar_skips_period_no_strategy_uses`。

2. **同一根 14:30 的 1m bar 被两轮 poll 重复驱动**(14:30:05 与 14:30:48 都分发 15m/5m/30m):`BarPoller.poll` 完成检测按 **per-code** `last` 判"新完成"(`bar_poller.py:233` `new_times = [t for t in completed if t > last]`),快股票第一轮把 14:30 bar 标完成触发,慢股票第二轮才完成又触发同一 stime。**无跨 code 全局 stime 去重** → 5m/15m/30m 边界在该 bar 重复分发(全局重拉 + 周期策略二次求值)、`update_peak`/风控重复跑。订单靠 order_id 幂等 + cap_quantity 挡了重下,但重算是真浪费。
   **✅ 已修复(2026-08-12)**:`live_engine.py` `_on_bar` 边界分发加 `_dispatched_boundaries` 集合——同 bar_time 只分发一次(分发成功后才记账,桥异常中断留口可重试)。**在 `_on_bar` 层而非 `BarPoller.poll` 层做去重**:后者会把慢股票的 1m 求值也吞掉(慢股票数据到位后永远不再触发);前者只挡"重复周期分发",1m 策略仍按当轮 `bar.stocks` 逐股求值(每轮 bar 只含当轮新完成股票,快股票只在第一轮、慢股票在其完成那轮各求值一次)。带回归测试 `test_on_bar_dispatches_boundary_once_for_same_bar_time`。
   顺带:主循环拉 bar 节拍 30s→60s(`poll_interval` 默认值,降一半桥 /quote 拉取量)。14:30 边界分发由 14:30:00 那轮 poll 触发,60s 节拍不改变其时序;二次触发若发生,从 14:30:48 挪到下一轮 poll(~14:31),仍被 `_dispatched_boundaries` 挡住。

3. **组合2 跨池下单**:order 3/4 是 strat=4(组合2,1d)在 `159066.SZ` 触发 OPEN,但 159066 **不在组合2 池**(`第二股票` 9 只=159048–159057),只在组合1 etf 池。根因:引擎层**无按组合 stock_pool_id 过滤**——`_build_engine` 把各组合池并成全局 `stock_codes`,`_fill_signal_cache`/`on_bar` 按 bar.stocks 全量注入/驱动,池成员资格只用来拼拉取范围、不约束"哪组合能交易哪只股"。
   **✅ 已修复(2026-08-12)**:`StrategyContext` 加 `stock_pool`(组合池成分 set),`assemble_portfolio`(回测/实盘共用)从 `stock_pool_stocks` 按 `ps.stock_pool_id` 解析注入;`get_signal` 池外股票不产公式信号(风控信号不受限——持仓必须可卖),`_fill_signal_cache` 池外跳过不拉公式。回测 bar 本就只覆盖池内股票(`_pool_stocks` 定 klines 范围),过滤对其为 no-op,仅实盘多组合共享全局 bar 时才生效。带回归测试 `test_get_signal_pool_filter_excludes_out_of_pool`/`test_on_bar_pool_filter_blocks_cross_pool_order`。

4. **4 笔下单全被桥拒,err 统一写成 `approval failed or bridge rejected`**:订单量 22600/21500/33900/32300 **全超桥 `MAX_VOLUME=10000`**(`iquant_bridge.py:67`),`check_whitelist`(`:97`) `volume > MAX_VOLUME` 返回 `ok:false`。dispatcher 见 ok=false 返 None,`_handle_bar` 落 rejected。**桥真实拒因(超量)被笼统文案吞了**——日志侧表现:`[AUDIT] POST /order` 有、`[BRIDGE] order` 无(超量拒单不进 `_do_place`)。
   **✅ 已修复(2026-08-12)**:两桥 `MAX_VOLUME` 10000→**100000**(`iquant_bridge.py`/`iquant_bridge_live.py`,低价 ETF 单笔 ~3.4 万股,3x 余量);dispatcher 加 `BridgeOrderRejected`,`ok=false` 且有 `error` 时抛出回显桥侧真实原因(`ok=false` 无 error / 非 JSON 路径维持返回 None,#24 语义);`_handle_bar` 捕它标 rejected + `error_message=桥 error 文案`,`trade is None` 兜底文案收紧为 `approval failed`。带回归测试 `test_place_order_bridge_rejection_raises_with_error`/`test_handle_bar_bridge_business_reject_surfaces_error`。

### 配置核对(与第一轮一致,无变化)

- 组合1「测试」etf 17 只(159048–159067 去掉 159054、159058——159058 用户已删)→ {1d,5m,1m}
- 组合2「第二」第二股票 9 只(159048–159057)→ {1d,5m,30m}
- 公式全 MACROSSPRO formula_count=30
- sec 启动 14:18:04 停止 14:35:40;跨过 14:30 → 1d 策略首次触发(第一轮 10:54–13:48 未到 14:30,零下单)
