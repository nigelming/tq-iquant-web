# 0009 实盘交易桥 — iQuant 客户端内 HTTP 桥（形态二）

> 状态：**可行性已验证**（桥 v6 真机跑通，HTTP 24-39ms 秒回 + `passorder` 返回 0 受理成功），待实施
> 日期：2026-08-04
> 接续：0008 实盘引擎模拟撮合已打通（模拟下单）→ 本计划把 0008「次期 NatsDispatcher + iQuant 网关」重构为「客户端内 HTTP 桥」，打通**真实下单/查单/查持仓/查资金 + 1m/5m 行情拉取**
> 验证：`live/bridge/iquant_bridge.py` v6（HTTP 秒回 + passorder 受理成功，模拟模式）；环境约束见 [[iquant-bridge-env-constraints]]

## 1. 背景与目标

**为什么换方案**（对比 miniQMT / TDX，均为真机验证结论）：
- **miniQMT（形态三）不可行**：个人账户开通 miniQMT 后**只有行情数据权限、无交易权限**（交易权限仅机构可申请），且当前环境缺 `xtminiqmt.exe`。见 [[iquant-miniqmt-auth-requirement]]。
- **TDX 无真分钟 bar**：0008 验证风险点 9 定论 TDX 侧 1m 实盘不可行（`subscribe_quote` 废代码、`get_market_data` 盘中只有昨天、snapshot 漏 tick）。
- **iQuant 客户端内策略（形态二）可行**：策略在已登录客户端内运行，**天然认证**；`passorder`/`get_history_data`/`get_trade_detail_data` 均为官方策略 API，已验证 `passorder` 真实受理。

**目标**：以「iQuant 客户端内 Python 策略做 HTTP 桥」为个人账户的真实交易 + 1m/5m 行情通道，Core 通过 HTTP 调用桥，打通实盘全链路。

**不在本计划范围**：
- 多周期合成之外的复杂聚合（仅 1m→5m 起步）
- iQuant 订阅推送回调（`subscribe_quote` 依赖框架回调，init 阻塞下不可靠，**本方案一律用拉取**）
- 前端实盘监控大改（保留 0008 的 `LiveSessions.vue`，桥状态并入）

## 2. 已验证结论（真机实测，2026-08-04）

### 2.1 iQuant 嵌入 Python 环境三大限制
1. **`threading.Thread` 线程不执行**：`start()` 不报错但线程函数从不执行（`thread alive` 从未打印，连接全 CLOSE_WAIT）。
2. **`handlebar` 仅启动瞬间高频**：模拟模式启动时历史 K 线回放（57ms 内 5000+ tick），回放完成后停止调用。
3. **`run_time` 回调未被调用**：定义后日志无输出。

**→ 唯一可行机制：`init` 自身进入阻塞主循环（init 不返回），全程在策略主线程处理请求。** 实测 HTTP 请求 24-39ms 秒回。副作用：handlebar/回放逻辑不执行（对桥无影响）。

### 2.2 `passorder` 真实签名（从 C++ 签名报错解析）
```python
# 10 参变体（已验证可用，返回 0 = 委托受理成功）
passorder(opType:int, 0, accountID:str, orderCode:str, prType:int,
          price:float, volume:float, strategyName:str, quickTrade:int, ContextInfo)
```
- `prType`：**0=限价(指定价)、5=最新价**。⚠️ 是 int 不是字符串（早期误传 str 报类型错误）。
- `price/volume` 传 float；尾参必须传 `ContextInfo`（init 里 `_CTX = ContextInfo` 全局捕获）。
- 存在多只重载（8/9/10/11 参），当前用 10 参变体已验证。

### 2.3 桥验证结果（v6）
- `GET /ping`、`POST /order` 毫秒级响应；
- `DRY_RUN=False` 时 `passorder_result=0`（受理成功），模拟模式下单流程通。

## 3. 架构

```
┌─ Core (main/, Python 3.13 FastAPI) ─────────────────────────┐
│  HttpBridgeDispatcher (实现 OrderDispatcher 接口)            │
│    place_order/query_order/query_positions/query_account/    │
│    heartbeat → httpx 调 127.0.0.1:8790                       │
│  行情通道 BarPoller：定时拉 /quote → bar 完成检测 → 1m→5m 聚合 │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP + JSON（统一响应格式）
                           ▼
┌─ iQuant 客户端内桥策略 (Python 3.6, live/bridge/) ───────────┐
│  init 阻塞主循环 = 单线程事件循环                              │
│   ├─ HTTP 层：非阻塞 accept + 分帧解析/响应                   │
│   ├─ 路由层：/ping /order /positions /account /orders        │
│   │         /deals /quote                                     │
│   ├─ 业务层：passorder / get_trade_detail_data / get_history_data│
│   ├─ 安全层：鉴权(token/HMAC) + 白名单 + 限额                 │
│   ├─ 审计层：请求日志                                         │
│   └─ 行情缓存：每秒缓存最新 bar（Core 高频拉不压桥）           │
└──────────────────────────────────────────────────────────────┘
```

## 4. 桥实现模式

### 4.1 运行模型：init 阻塞主循环 = 单线程事件循环

一个循环同时做四件事，不用线程、不依赖 handlebar：

```python
def init(ContextInfo):
    _CTX = ContextInfo                  # passorder 尾参
    token = load_secret()               # 从环境/文件读密钥，不写代码
    s = bind/listen/setblocking(False)  # 127.0.0.1:8790
    clients = {}                        # conn -> 收包缓冲
    last_tick = 0
    while True:                         # ← init 不返回
        _accept_new(s, clients)         # ① 收新连接（非阻塞）
        _serve_requests(clients)        # ② 鉴权/路由/调用/响应/审计
        if time.time() - last_tick > 1.0:
            _tick_jobs()                # ③ 每秒：行情缓存/心跳/订单状态
            last_tick = time.time()
        time.sleep(0.01)                # ④ 让出 CPU
```

### 4.2 通信协议
- HTTP/1.1 + JSON，只绑 `127.0.0.1`。
- 鉴权头：`X-Auth-Token`（简单版）或 `X-Timestamp + X-Signature`（HMAC-SHA256(secret, ts+method+path+body)）。
- 统一响应：`{"ok": true, "data": {...}, "ts": ...}` / `{"ok": false, "error": {"code", "msg"}}`。

### 4.3 端点清单（拉取模式，全同步）

| 方法/路径 | 参数 | 桥内调用 | 说明 |
|---|---|---|---|
| `GET /ping` | — | — | 探活 + 桥状态 |
| `POST /order` | `{order_id, code, op, volume, price, pr_type}` | `passorder` | **幂等**（见 4.5.1） |
| `GET /positions` | `{account}` | `get_trade_detail_data(acc,'STOCK','POSITION')` | 当前持仓 |
| `GET /account` | `{account}` | `get_trade_detail_data(acc,'STOCK','ACCOUNT')` | 资金/可用/市值 |
| `GET /orders?order_id=` | `{account, order_id}` | `get_value_by_order_id` | 委托状态 |
| `GET /deals?order_id=` | `{account, order_id}` | `get_trade_detail_data(acc,'STOCK','DEAL')` | 成交回报 |
| `GET /quote?code=&period=&count=` | — | `ContextInfo.get_history_data(count, period, ...)` | 拉 1m/5m/1d bar（读缓存） |

### 4.4 请求处理管线（每帧）
```
收齐请求帧 → 鉴权 → 白名单校验 → 路由 → 参数/限额校验
→ 调 iQuant API → 组装响应 → 写审计日志 → 返回
```
失败逐层短路：鉴权不过 401、白名单/限额不过 403、参数非法 400、API 异常 500。

### 4.5 关键机制

#### 4.5.1 下单幂等（防重复下单）
```python
def place_order(params):
    oid = params["order_id"]            # Core 生成的唯一单号
    if oid in _placed: return _placed[oid]     # 已受理 → 返回原结果
    if oid in _placing: return {"ok": False, "error": "duplicate in-flight"}
    _placing.add(oid)
    try:
        r = passorder(op_type, 0, acc, code, pr_type, float(price),
                      float(volume), "iquant_bridge", 2, _CTX)
        _placed[oid] = {"ok": True, "passorder_result": r}
    finally:
        _placing.discard(oid)
    return _placed[oid]
```
`order_id` 同时透传 `passorder` 的 `userOrderId` 参数到券商端，双保险。

#### 4.5.2 订单/成交状态
- `passorder` 返回 0 只是受理，成交需查 `get_trade_detail_data(acc,'STOCK','DEAL')`。
- Core 下单后按 `order_id` 轮询 `GET /deals`，维护订单状态机：submitted → partial → filled / rejected。

#### 4.5.3 行情（拉取非订阅）
- Core 定时（如 15s）调 `GET /quote`，桥读内存缓存返回 OHLCV。
- 桥 `_tick_jobs` 每秒调 `get_history_data` 刷新缓存，避免每次穿 iQuant API。

#### 4.5.4 白名单 + 限额（止损线）
```python
ALLOWED_STOCKS = {...}   # Core 声明的授权股票
MAX_VOLUME = 10000       # 单笔上限
RATE_LIMIT = 5           # 每 10 秒最多 N 笔
```
超限拒绝——即使鉴权被绕过，攻击也只能在授权股票 + 限额内下单。

#### 4.5.5 审计日志
每请求一行：`[AUDIT] 2026-08-04 16:20:33 POST /order {order_id} 600000.SH buy 100 → ok(0)`。

#### 4.5.6 密钥管理
token/HMAC secret 从环境变量或权限收紧的本地文件读（`load_secret()`），不写死在策略代码、不进 git。

## 5. 1m/5m 行情处理（拉取 + Core 侧加工）— ✅ 已验证（2026-08-05 盘中）

### 5.1 数据源：`get_market_data_ex`（已验证，`get_history_data` 已废弃）
```
桥 /quote 端点内部：
  download_history_data(code, period, start, end)        # 补历史
  ContextInfo.get_market_data_ex([], [code], period='1m'/'5m',
                                  count=N, dividend_type='none')
  → {code: DataFrame}，字段含 amount/close/high/low/open/preClose/
    stime/time/volume 等
```
- **1m 实时**：盘中返回今天数据，每 10s 更新 ✅
- **5m 原生周期**：可直接拉，对齐整除点（9:35/9:40/...）✅ → **方案 A 确认，1m 聚合（方案 B）不需要**

### 5.2 bar 完成检测（已确认语义）
- DataFrame 的 `stime` 是 yyyymmddHHMMSS 的 **bar 结束时间**（如 10:08:00 = 10:07–10:08 那根结束）；`time` 是毫秒时间戳。
- **已完成判断**：`bar_stime <= now` → 已完成，可触发信号。
- **最新一根 `bar_stime > now` 是进行中**（OHLC 还在变），策略只用已完成 bar 防信号闪烁/未来函数。
- Core 维护 `last_bar_time`，拉到 `stime <= now` 且 `> last_bar_time` 的 bar → 触发该 bar 信号（复用 Portfolio.on_bar）。

### 5.3 数据流
```
Core 每 15s 调 GET /quote?period=1m&count=10
  → 桥读缓存（每秒刷新）返回最新 10 根 1m bar
  → Core 过滤 stime<=now 的已完成 bar，与 last_bar_time 比对 → 触发信号
5m 同理直接拉（原生周期），无需聚合。
```

## 6. 安全设计

| 层级 | 措施 |
|---|---|
| 传输 | 只绑 `127.0.0.1`，不暴露外网 |
| 鉴权 | `X-Auth-Token` 或 HMAC 签名（含时间戳防重放）；密钥外部化 |
| 幂等 | `order_id` 去重（见 4.5.1） |
| 止损 | 白名单股票 + 单笔限额 + 限频（见 4.5.4） |
| 审计 | 全量请求日志（见 4.5.5） |

**威胁模型**：Token 单独挡不住"能读到 token 的本机恶意进程"——应用层鉴权防的是误操作/误连；真正的止损线是白名单+限额+审计。若需防"已攻破机器"，属操作系统隔离范畴（独立用户/虚拟机），非本计划范围。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 客户端生命周期=桥生命周期 | Core 心跳检测 + 断线暂停下单、pending 订单标离线 |
| init 阻塞可能被客户端判定卡死 | 长期运行测试；事件循环非阻塞 + sleep 让出 CPU |
| 单点阻塞（慢请求堵下单） | 非阻塞 accept + 分帧缓冲（支持并发连接） |
| 重复下单 | `order_id` 幂等 + `userOrderId` 透传 |
| 成交状态不回传 | Core 轮询 `/deals` 状态机 |
| 行情实时性（模拟 vs 真实） | 开盘后实测（§12 待验证点） |
| 券商程序化风控 | 下单限频、交易时段过滤 |
| iQuant 客户端版本更新改 API | 桥代码集中、文档记录签名，适配更新 |
| Core Bug 变真实交易 | 模拟撮合充分验证 → 小额真实灰度 |

## 8. 合规边界（不篡改 iQuant）

- **零篡改**：不改 `D:\iQuant\` 任何安装文件、不 hook/注入进程、不绕过认证、不碰 xtquant RPC。
- 使用 iQuant **官方策略机制**（策略编辑器 + 官方 API `passorder`/`get_history_data`/`get_trade_detail_data`）。
- `init` 阻塞是"非预期但合法"的策略代码写法。
- **待确认**：①外部程序 HTTP 驱动策略下单是否在 iQuant 条款/券商政策内；②程序化交易是否需报备。低频自用一般合规，建议向客户经理确认。

## 9. 与 0008 的关系

- `OrderDispatcher` 接口已就绪（[execution_engine.py](main/core/engine/execution_engine.py)）。新增 `HttpBridgeDispatcher` 实现该接口，替换 `SimulatedDispatcher`（模拟撮合）→ `HttpBridgeDispatcher`（真实下单），`handle_bar` 引擎核心**不改**。
- `live/iguant_gateway/`（NATS mock 网关）**废弃**，NATS 方案不再需要。
- 行情驱动从「TDX `subscribe_hq` + `get_market_snapshot`（1d）」扩展为「iQuant 桥 `get_history_data`（1m/5m/1d）」——0008 §3 数据流的 TDX 路径保留为备选。

## 10. 关键文件

**桥（iQuant 客户端内）**
- `live/bridge/iquant_bridge.py` — v6 验证版 → 升级为正式版（§4 全部机制）
- `live/bridge/README.md` — 部署/密钥/账号配置说明

**后端（Core）**
- 新建 `main/core/engine/http_bridge_dispatcher.py` — 实现 `OrderDispatcher`
- 新建 `main/core/engine/bar_poller.py` — 定时拉 `/quote` + bar 完成检测（`stime<=now`）+ 多股票合并 `BarEvent`
- 改 `main/core/engine/live_engine.py` — 注入 `HttpBridgeDispatcher`（替换/并行 SimulatedDispatcher）
- 改 `main/core/api/live.py` — 桥状态/心跳端点、订单状态回传
- 删 `live/iguant_gateway/`（NATS mock）

**后端测试**
- `main/core/tests/unit/test_http_bridge_dispatcher.py`
- `main/core/tests/unit/test_bar_poller.py`（Mock 桥）

## 11. 实现范围（全程 TDD，5 切片）

每切片：先写失败测试 → 实现 → 绿 → 回归。

### 切片 1：桥正式版（iQuant 策略，Mock 单测）
桥升级：端点全量 + 鉴权 + 幂等 + 白名单/限额 + 审计 + 行情缓存 + 事件循环（非阻塞 accept）。
单测（Mock iQuant 内置函数）：
- `test_ping`、`test_order_dry_run`
- `test_order_idempotent`（同 order_id 重复请求返回原结果，不重复 passorder）
- `test_auth_reject`（无 token/错 token → 401）
- `test_whitelist_reject`（白名单外股票 → 403）
- `test_rate_limit`（超频 → 拒绝）
- `test_quote_cache`（行情缓存命中）

### 切片 2：Core `HttpBridgeDispatcher`
实现 `OrderDispatcher`：`place_order`→`POST /order`、`query_order`→`GET /orders`、`query_positions`→`GET /positions`、`query_account`→`GET /account`、`heartbeat`→`GET /ping`。
单测（httpx Mock）：
- `test_place_order_maps_to_http`
- `test_query_order_maps_to_http`
- `test_bridge_offline_raises`（桥不可用 → 异常/状态标记）
- `test_idempotent_order_id_passthrough`

### 切片 3：Core 行情通道 `BarPoller`
- 定时拉 `/quote` → bar 完成检测（`stime <= now`）→ 只触发已完成 bar → 驱动 `Portfolio.on_bar`
- 5m 原生周期直接拉（§5.1 验证定案，**不做 1m→5m 聚合**）；支持多股票合并为一根 `BarEvent`
桥 `/quote` 返回（`_df_to_bars` = `df.reset_index().to_dict("records")`）：
- 字段含 `stime`（`yyyymmddHHMMSS` 字符串，bar **结束**时间）、`open/high/low/close/volume/amount` 等
- `stime > now` 的最新一根 = 进行中（OHLC 在变），不触发；`stime <= now` 且 `> last_bar_time` = 新已完成 bar，触发
单测（Mock 桥 `/quote` 响应，httpx MockTransport）：
- `test_new_bar_triggered`（已完成 bar 的 `stime` > last → 触发回调，`BarEvent` 含 OHLCV）
- `test_in_progress_bar_ignored`（最新 bar `stime > now` 进行中 → 不触发）
- `test_only_beyond_last_bar_triggered`（多根已完成 bar 只触发 > last_bar_time 的）
- `test_multiple_stocks_merged_into_one_bar_event`（多股票同 bar 时间合并为单 `BarEvent.stocks`）
- `test_bridge_offline_raises`（桥不可用 → `BridgeUnavailableError`，不吞异常）

### 切片 4：集成
`LiveEngine` 注入 `HttpBridgeDispatcher` + `BarPoller`，模拟模式端到端：拉 bar → 信号 → 真实下单（桥 DRY_RUN 或模拟模式）→ 查单/成交回报 → 落 live_trades。
集成测：
- `test_live_engine_with_http_bridge_full_flow`
- `test_bridge_disconnect_pauses_trading`

### 切片 5：运维 + 回归
- 心跳/断线处理、桥状态并入 `/api/live/sessions` 响应
- 全量回归（既有 174 测试 + 0008 实盘测试无回归）

## 12. 待验证点 — ✅ 全部已验证（2026-08-05 盘中，`live/scripts/verify_iquant_realtime.py`）

| # | 待验证点 | 结论 |
|---|---|---|
| 1 | 盘中 1m 实时性 | ✅ `get_market_data_ex('1m')` 返回今天数据、每 10s 更新。注意：`get_history_data` 已废弃返回空，**必须用 `get_market_data_ex`** |
| 2 | 最后一根是否进行中 | ✅ `stime` 是 bar 结束时间，最新一根 `stime > now` 为进行中；已完成判断 `stime <= now` |
| 3 | 5m 周期完整性 | ✅ 5m 是原生基础周期，直接拉、对齐整除点（9:35/9:40/...）→ 方案 A 定案 |
| 4 | subscribe_quote 订阅回调 | ✅ **不可用**：注册成功但 init 阻塞下 60s 无回调 → 「拉取模式」定案 |

## 13. 验证

- 后端：`uv run pytest` 全绿（新 test_http_bridge_dispatcher/test_bar_poller + 既有测试无回归）
- E2E（用户本地，需 iQuant 客户端运行桥）：
  1. 启动桥策略 → Core 起实盘 session
  2. Core 心跳检测桥在线 → 行情通道拉 1m → 信号 → 真实下单（先模拟模式）→ 查单/成交回报 → live_trades 落库
  3. 停掉 iQuant 客户端 → Core 检测桥离线 → 暂停下单
  4. 开盘日验证 1m 行情实时性（待验证点）
