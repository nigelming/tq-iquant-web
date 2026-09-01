# Live 引擎内部分解：把 2389 行的 LiveEngine 拆成编排器 + 5 个协作者

## Context

`core/engine/live_engine.py` 已膨胀到 2389 行，揉了 5 类本可独立的职责：SSE 广播、行情/信号缓存、
委托状态机+成交回填、日终收盘、熔断编排。本次目标是**引擎内部分解**——与上一阶段（已合并的
`live_service` 抽离，commit a77d132）不同，这次动的是 `LiveEngine` 本身。

**边界（已与现有目录结构对齐）**：
- `core/engine/` = 领域引擎层（有状态、可被回测/实盘/测试直接构造、同层协作者互相 import）。
  `LiveEngine` 和拆出的协作者**都留在这里**，建子包 `core/engine/live/`。
- `core/services/` = 应用编排层（查库/事务/组装引擎给 API 用，import engine，engine 不 import service）。
  `live_service.py` 不动。
- 范式参照：`BacktestEngine` 留在 `engine/`、`backtest_service` 在 `services/`。`LiveEngine` 同理。

**铁律**：
1. **纯搬移，不改行为**。每一步独立一个 commit，每步结束全量后端测试必须绿（基线 523 passed）。
2. **公共方法签名冻结**：`start/stop/recover/stream_events/recover_breaker/bridge_online/
   pending_orders_count/last_backfill_time/dispatcher` 这些被 `live_service`、`live.py`、
   集成测试引用的方法始终留在 `LiveEngine`，内部转调协作者。
3. **协作者不反向 import LiveEngine**。需要引擎状态时，注入窄接口（回调 / 协议对象 / 不可变快照），
   杜绝循环依赖、让协作者可单独构造单测。
4. **共享可变状态集中**：`positions`、`_pending_orders`、`account` 等被多个协作者改写的状态，
   归一个持有者（`EngineContext`）或经窄接口暴露，不能让 5 个协作者各抓一个引擎引用随意改。
5. **不动实盘桥**：`live/bridge/` 一行不碰；后端 `manage.ps1` 无 `--reload`，验证后需 restart 才生效，
   **绝不在盘中动**。
6. **`shared/` Python 3.7 兼容约束对本次无影响**（拆出的协作者在 main/，Python 3.13）。

### 已知测试耦合（搬移时必须处理，不能假设零改动）

`core/tests/unit/test_live_engine.py`（5000+ 行）深度绑定模块路径：
- `monkeypatch.setattr("core.engine.live_engine.datetime", _FakeDateTime)` 多处——任何被搬走的代码
  若直接调 `datetime.now()`，搬后 patch 不再生效。对策：时间统一走 `now_shanghai()`，且让被搬代码
  注入一个 `clock` 可调用对象（默认 `now_shanghai`），测试 monkeypatch 该 clock；**不**全局改测试。
- `from core.engine.live_engine import periods_on_boundary, now_shanghai, _CST`——这三个符号必须
  在 `live_engine.py` 继续可导入（re-export）。
- 几十处 `caplog.at_level(logging.INFO, logger="core.engine.live_engine")`——代码搬走后 logger 名变成
  `core.engine.live.xxx`，对应断言的 caplog logger 名要同步改（这是搬移的正当测试维护，逐处改）。

## 目标结构

```
core/engine/
├── live_engine.py            # 编排器，最终 ~400-500 行（公共方法签名不变，import 路径不变）
├── risk_manager.py           # 不动（StrategyRiskManager / PortfolioRiskManager，规则唯一权威）
├── portfolio.py              # 不动（on_bar/_check_risks/剥 BUY/max_positions 等共用规则）
├── execution_engine.py       # 不动（资金审批、T1Checker）
├── account.py position.py event.py bar_poller.py ...  不动
└── live/                     # 新建子包：LiveEngine 的内部协作者
    ├── __init__.py
    ├── timing.py             # 无状态时间/周期工具（now_shanghai/periods_on_boundary 等，live_engine re-export）
    ├── context.py            # EngineContext：集中持有共享可变状态 + clock + db 工厂 + dispatcher
    ├── event_bus.py          # EventBus：SSE 多播 + ping
    ├── market_data.py        # MarketDataService：bar 缓存 / 信号求值 / 周期 bar 分发
    ├── order_machine.py      # OrderStateMachine + OrderGate：委托状态机/成交回填/在途门/T+1
    ├── breaker.py            # BreakerService：熔断编排 + 副作用（计数持久化/risk 事件/手动恢复）
    └── daily_closer.py       # DailyCloser：日终/收盘三件套的"时点判断"，逻辑转调 breaker/order_machine
```

`core/engine/__init__.py` 继续 `from .live_engine import LiveEngine`；外部 import 路径
（`core.engine.live_engine.LiveEngine`、`core.engine.LiveEngine`）全部不变。

---

## 步骤 0：抽出 `live/timing.py`（极低风险，去噪）

**文件**：新建 `core/engine/live/timing.py`（归 live 协作者子包内部，不放 engine 顶层）。

**搬入的模块级函数/常量**（现 live_engine.py:79-148）：
- `_parse_insert_utc`、`now_shanghai`、`periods_on_boundary`、`_to_int`、`_CST`。

**做法**：
- 原样移动，函数体不改。
- 在 `live_engine.py` 顶部 `from .live.timing import (_parse_insert_utc, now_shanghai,
  periods_on_boundary, _to_int, _CST)`，保持这三个被测试直接 import 的符号在
  `core.engine.live_engine` 命名空间可见（re-export）。
- 给这些函数加一个可注入 clock 的口子（仅 `now_shanghai` 的调用方需要时）；本步先纯搬，
  clock 注入留到步骤 2/4 真正搬时间相关逻辑时再加，避免一次改太多。

**验证**：`uv run pytest -q` 全绿（测试 import 与 datetime patch 仍命中 live_engine 命名空间）。

---

## 步骤 1：抽出 `EventBus`（极低风险）

**文件**：`core/engine/live/event_bus.py`。

**搬入**（现 live_engine.py:285-341）：`_emit`、`_emit_to_subscribers`、`stream_events`，以及
`__init__` 里订阅者队列集合的初始化。

**接口**：
```python
class EventBus:
    def __init__(self, clock=now_shanghai): ...
    def emit(self, event_type: str, payload: dict) -> None
    def emit_raw(self, ev: dict) -> None          # _emit_to_subscribers
    async def stream(self) -> AsyncIterator[dict] # 含 30s ping
    def close(self) -> None
```

**做法**：
- `LiveEngine.__init__` 建 `self.bus = EventBus(clock=...)`。
- 引擎内所有 `self._emit(t, p)` 替换为 `self.bus.emit(t, p)`（grep 全覆盖：信号/订单/成交/持仓/
  risk 五类事件的发射点散在 on_bar、breaker、order_machine，本步先只搬广播机制，发射点随各自
  协作者在后续步骤迁移；未迁移前 `self._emit` 保留为 `self.bus.emit` 的薄委托）。
- 公共方法 `LiveEngine.stream_events()` 保留，`return self.bus.stream()`。
- ping 用注入的 clock（默认 now_shanghai），测试若 patch 时间则 patch bus.clock。

**验证**：SSE 集成测试（`/stream` 五类事件 + ping 心跳）全绿；`uv run pytest -q`。

---

## 步骤 2：抽出 `EngineContext` + `MarketDataService`（中风险）

### 2a. `context.py`：集中共享状态

**文件**：`core/engine/live/context.py`。

**目的**：后续协作者都需要读/写引擎的运行态，但不能各自抓整个 LiveEngine。先定义一个窄容器，
作为协作者之间唯一的共享状态通道。

**持有（从 `LiveEngine.__init__` 迁移归它所有，引擎持引用转发）**：
- `positions: Dict[int, Dict[str, Position]]`（portfolio_id → code → position）
- `portfolios: Dict[int, Portfolio]`
- `session_id`、`code_period_count`、`dispatcher`、`db_session_factory`
- `clock: Callable[[], datetime]`（默认 now_shanghai，供测试 patch）
- 可选：`pending_orders` 也放这里（步骤 3 用），但本步先只放行情相关状态，pending 在步骤 3 迁入。

**注意**：这是数据容器，不含业务逻辑；协作者通过它读状态、经它暴露的窄方法改状态。
迁移时 `LiveEngine` 上的同名属性可保留为 property 委托（`self.ctx.positions`），减少一次性改动面。

### 2b. `market_data.py`：行情/信号缓存与周期分发

**文件**：`core/engine/live/market_data.py`。

**搬入**（现 live_engine.py:343-428, 1307-1639，约 350 行）：
- 缓存：`_preheat`、`_make_cache_entry`、`_get_bars_with_increment`、`_bar_stime`、
  `_sort_and_cap`、`_max_stime`
- 取数/求值/分发：`_dispatch_period_bar`、`_refresh_available_map`、`_fill_signal_cache`、
  `_fetch_cached_bars`、`_reuse_provided_with_cache`、`_bars_to_formula_df`、`_extract_latest_signal`
- 启动补齐：`_inject_startup_periods`、`_startup_periods_missing`

**接口要点**：
```python
class MarketDataService:
    def __init__(self, ctx: EngineContext, formula: TQFormula): ...
    def preheat(self) -> None
    def get_bars(self, code, period, count) -> list
    def fill_signal_cache(self, ...) -> None
    def dispatch_period_bar(self, period, boundary_time, on_bar) -> None
```
- `signal_cache`、bar cache 字典归本服务所有。
- `_dispatch_period_bar` 会驱动 on_bar 管线并最终产生订单——**用 `on_bar` 回调注入**（引擎传入
  `self._on_bar` 或一个把订单送进订单管线的闭包），服务本身不 import LiveEngine、不直接下单。
- 时间读取走 `ctx.clock`，便于测试。

**做法**：`LiveEngine` 持有 `self.market_data`，把上述私有方法改为薄委托或直接改调用点。
`_preheat` 由 `start()` 调；`_dispatch_period_bar` 由 `_loop/_tick_main` 调；
`_fill_signal_cache` 由 `_handle_bar` 调。逐处替换，保持调用时序不变。

**验证**：行情/信号缓存/周期边界（C6 三段式周期）相关单测全绿；全量 `pytest -q`。

---

## 步骤 3：抽出 `OrderStateMachine` + `OrderGate`（最高风险，测试护航重点）

**文件**：`core/engine/live/order_machine.py`。

这是最大最复杂的一块（现 live_engine.py:1640-2230，约 590 行），也是最近反复修 bug 的区域
（修复 A+ order_ref/remark 兜底、id40-41 陈旧 submitted、53/55/56/57 状态码、partial 不 apply、
F7 在途门、I4 命门窗口）。**必须靠现有这些用例逐组护航，搬一组绿一组。**

### 搬入

- 持久化/匹配：`_persist_order_submitted`、`_try_match_order_ref`、`_match_by_remark`、
  `_match_legacy_fuzzy`
- 轮询/同步：`_poll_deals`、`_sync_terminal_order_status`、`_expire_stale_orders`、
  `_sync_pending_orders`、`_backfill_order`、`_parse_trade_time`、`_apply_filled_trade`
- 执行门控（现 `_handle_bar` 内联，:1040-1208 一带）：在途单门 F7（同向 pending 压制）、
  T+1 available 消费（`_t1_checker.consume_available`）、bridge 不可用时 pending 回滚——
  抽成内部协作者 `OrderGate`（可同文件或单独 `order_gate.py`），归订单管线所有。

### 接口要点

```python
class OrderStateMachine:
    def __init__(self, ctx: EngineContext, bus: EventBus, db_factory, t1_checker): ...
    def persist_submitted(self, order) -> LiveOrder
    def poll_deals(self) -> None          # 供 _deals_loop / _tick_deals 调
    def sync_pending(self) -> None
    def expire_stale(self, pending=None) -> None
    def submit(self, order) -> None       # _handle_bar 里"审批→落 submitted→调桥→异常回滚"整段

class OrderGate:
    def allow(self, portfolio_id, strategy_id, code, trade_type, bar_time) -> bool  # F7 在途门
    def consume_t1(self, code, qty) -> int   # available 钳量
```

### 关键耦合：`_apply_filled_trade` 改持仓/账户/缓存

它直接改 `positions`/`account`/`signal_cache`。**不要让它抓整个引擎**——经 `EngineContext`
暴露的窄方法改（如 `ctx.apply_fill_to_position(code, qty, price, trade_type)`、`ctx.account`），
或传入一个 `PositionMutator` 协议。这样成交回填可在不拉起 LiveEngine 的情况下单测。

`pending_orders` 字典归 `OrderStateMachine` 所有（或归 ctx，但由它独占读写）。

### 做法

- `LiveEngine` 持有 `self.orders = OrderStateMachine(...)`。
- `_deals_loop/_tick_deals` 转调 `self.orders.poll_deals()`。
- `_handle_bar` 里"生成订单后"的整段落单逻辑改为 `self.orders.submit(order)`；门控在 submit 内部。
- 公共属性 `pending_orders_count`、`last_backfill_time`、`dispatcher` 保留在 LiveEngine，
  前两个从 `self.orders` 读数转发。
- **逐子块搬、逐子块跑测试**：先搬纯函数（`_parse_trade_time`/匹配函数），再搬持久化，
  最后搬 poll/backfill/apply。每搬一组跑 `pytest core/tests/unit/test_live_engine.py -q`。

**验证**：成交回填、order_ref/remark 匹配、超时过期、部分成交、53/55/56/57 状态码、F7 在途门、
T+1 钳量——所有相关用例全绿；全量 `pytest -q` + 集成 27 绿。

---

## 步骤 4：抽出 `BreakerService`（风控在 live 层唯一的家，中风险）

**文件**：`core/engine/live/breaker.py`。

风控已分两层：**规则/状态**在 `risk_manager.py::PortfolioRiskManager`（回测实盘共用，不动）；
**编排 + 副作用**散在 live_engine，本步收敛到 `BreakerService`。

### 搬入（散落的熔断编排代码）

- 每 bar 推进 max_drawdown：`_on_bar` 里 `rm.update_peak(...)` + 状态翻转检测 + 日志（:988-1000）
- 日终推进 daily_loss：`_maybe_daily_close` 里 `rm.update_daily(...)` + 次日恢复 + risk 事件
  （:625-647）
- 计数持久化 + 3 次转手动：`_persist_breaker_count`（:1209-1264）
- 手动恢复：`recover_breaker`（:1265-1306，公共方法，LiveEngine 保留签名转调）
- 重启读回累计次数：`recover` 里 D4/H4 那段（:2296-2323）
- 三处 `self._emit("risk", ...)` 发射点统一进本服务（经 EventBus）

### 接口

```python
class BreakerService:
    def __init__(self, ctx: EngineContext, bus: EventBus, db_factory): ...
    def on_bar_update(self, portfolio) -> None      # 每 bar：update_peak + 翻转检测 + 持久化 + 发事件
    def on_daily_update(self, portfolio) -> None    # 14:30：update_daily + 次日恢复 + 发事件
    def persist_count(self, portfolio) -> None      # H4 计数落库 + 3 次转 circuit_broken
    def recover(self, portfolio_id) -> bool         # 手动恢复：清零 + 解除 + 落库 + 发事件
    def restore_counts(self) -> None                # recover() 重启读回
```

### 边界（重要）

- `BreakerService` **单向依赖** `risk_manager`（调 update_peak/update_daily、读标志位），不反向。
- `portfolio.py::on_bar` 里"熔断期剥 BUY 留 SELL"（:80-89）**不搬**——那是回测实盘共用规则，留原位。
- `_signal_to_order` 里的 max_positions/资金量也不搬，留 portfolio。
- 执行层的在途门/T+1 不属熔断，已在步骤 3。
- 触发时点不归本服务：on_bar 节拍由引擎调 `on_bar_update`，14:30 由 DailyCloser 调
  `on_daily_update`——本服务只管"推进+副作用"，不管"什么时候"。

**做法**：构造独立单测（内存 Portfolio + PortfolioRiskManager + fake bus/db），断言
"update_peak 触发→计数+1→发 risk 事件→第 3 次转 circuit_broken"、"手动恢复清零"、
"重启读回累计"，不必拉起 LiveEngine。再把引擎内散落点替换为转调。

**验证**：E5/E6/H4/§8.3 熔断相关单测 + 手动恢复 API 集成测试全绿；全量 `pytest -q`。

---

## 步骤 5：抽出 `DailyCloser`（中风险，收尾）

**文件**：`core/engine/live/daily_closer.py`。

**搬入**（现 live_engine.py:596-914，约 370 行）：`_maybe_daily_close`、`_maybe_daily_bars`、
`_maybe_close_sweep`。

**边界**：本服务只负责"**时点判断 + 编排**"——到 14:30 调 `breaker.on_daily_update(...)`、
收盘扫单调 `orders.expire_stale()/sync_pending()`、日 bar 调 `market_data`。具体熔断逻辑、
订单状态逻辑已分别在步骤 4/3，本步不复制业务规则，只转调协作者。

**接口**：
```python
class DailyCloser:
    def __init__(self, ctx, breaker, orders, market_data, bus): ...
    def tick(self, now=None) -> None   # _tick_main 在固定时点调；内部判断是否到点
```

**做法**：`LiveEngine._tick_main` 里对这三个 `_maybe_*` 的调用改为 `self.closer.tick(now)`。
时间走 `ctx.clock`。

**验证**：日终/收盘（daily_loss 推进、15:05 收盘扫单、日 bar 落库）相关单测全绿；全量 `pytest -q`。

---

## 完成后的 LiveEngine（~400-500 行）

只保留：
- `__init__`：构造 ctx + 5 个协作者（bus/market_data/orders/breaker/closer），装配依赖注入
- `start/stop`：生命周期，建两个 asyncio loop（60s 主 / 5s deals），启停协作者
- `_loop/_tick_main`/`_deals_loop/_tick_deals`：节拍调度，按 tick 转发给 market_data/orders/closer
- `_on_bar/_handle_bar`：bar 驱动主链（取信号→风控→经 orders.submit 落单），风控/缓存/落单分别委托
- `recover`：入口，编排 breaker.restore_counts + 持仓重放
- 公共属性/方法：`stream_events`、`recover_breaker`、`bridge_online`、`pending_orders_count`、
  `last_backfill_time`、`dispatcher`——全部薄转发到对应协作者
- 顶部 re-export `periods_on_boundary/now_shanghai/_CST`（测试兼容）

---

## 验证（每步都做，最后总验）

1. 每步：`cd main && PYTHONIOENCODING=utf-8 uv run pytest -q` 全绿（基线 523）。
2. 每步：`uv run python -c "import core.engine; from core.engine import LiveEngine; print('ok')"`
   确认公共 import 路径不变。
3. 每步重点跑相关子集：
   - 步骤 1：`pytest core/tests/integration/test_live_engine_api.py -q`（SSE）
   - 步骤 3：`pytest core/tests/unit/test_live_engine.py -q`（成交回填/状态机全量）
   - 步骤 4：`pytest -k "breaker or circuit or recover or daily" -q`
4. 静态：`uv run ruff check core/engine/`（若 ruff 可用）。
5. 总验：后端 523 + 前端 `cd web && npx vitest run`（前端不应受影响，HTTP 契约不变）。
6. 手验（需你协调，**非盘中**）：`./manage.ps1 restart` 后，跑一个 simulation session，
   验证启动预热→bar 驱动→下单→成交回填→SSE 五类事件→14:30/15:05 时点→停止/重启读回计数全链路。

## 风险与回滚

- 主要风险：搬移时漏改 logger 名 / datetime patch 失效 / 共享状态改写顺序错位（尤其
  `_apply_filled_trade` 和 I4 命门窗口的"先落 submitted 再调桥"顺序）。
- 缓解：每步独立 commit、纯搬移不改行为、协作者不反向依赖引擎、clock 注入解决时间 patch。
- 回滚：任一步出问题 `git revert` 单个 commit（各步文件边界清晰）。
- **不动 `core/engine/live_engine.py` 的公共 import 路径与方法签名**，故 `live_service.py`、
  `live.py`、前端、桥策略全程无需改动。

## 建议起步

先做**步骤 0 + 步骤 1**（live.timing 工具 + EventBus，约 130 行，极低风险），跑顺整套
"建子包 + 协作者注入 + 测试 logger/clock 兼容"的机制后，再推进步骤 2。步骤 3 是真正的硬骨头，
留到机制验证顺手后集中精力做。
