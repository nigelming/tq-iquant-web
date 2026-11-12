# 0017 hermes 日频实盘信号 — 14:20 全池计算 + 局域网拉取接口

> 状态：**已实施**（2026-11-12，单元/集成测试全绿，真机验收待首个实盘日）
> 日期：2026-11-12
> 接续：2026-10-26 live-signal-alert（alert 模式 P0，本计划是其消费侧延伸）
> 用户定调：纯信号不下单（hermes 侧终端手动执行）；GET 拉取无鉴权；关掉 send_warn；触发时间 14:20

## 1. 背景与目标

监管收紧后 iQuant/QMT 客户端内通道停用，实盘执行改为人工在其他终端操作。需要系统每个交易日 **14:20**（原 14:30 提前 10 分钟，留出人工操作余量）对池内股票跑一遍公式信号、落库，并让另一台电脑上的 hermes 程序拉取当日信号 JSON，供用户人工下单。

目标：
1. 复用 alert 预警模式（只算信号不下单），1d 默认触发时点 14:20
2. 新增 `GET /api/hermes/signals`（无鉴权，局域网）跨 session 按日拉取
3. send_warn 通达信预警改配置开关，默认关

## 2. 现状基线

alert 链路已完整（见 `docs/superpowers/specs/2026-10-26-live-signal-alert-design.md`）：`SignalScheduler` 1d 调度（默认 14:30，`catch_up_minutes=30`）、`SignalMonitor` 命中/边沿去重 → 落 `live_signals` → `send_warn` → SSE、`AlertDataDispatcher` 只读行情 + place_order 抛错。缺的只有三件事。

## 3. 方案设计

### 3.1 触发时间 14:20
`main/core/services/signal_schedule.py` `_DEFAULT_AT["1d"]="14:20"`（调度默认值与 API 校验共用），`parse_signal_at` 非法回退同步 14:20。每策略可配 `signal_at` 覆盖。**不动** `daily_closer._DAILY_CLOSE_TIME=(14,30)`（auto 模式日线收盘机制）。

### 3.2 send_warn 开关
- 配置：`live.alert_send_warn_enabled`，默认 **False**（`core/config.py` `_defaults()`；config.yaml 设 true 恢复）
- `SignalMonitor` 构造参数 `send_warn_enabled`（组件默认 True 保留历史行为，`build_engine` 注入配置值）；关闭时 `_persist_and_alert` 跳过 send_warn：`status` 停留 `new`、`alert_status` 空、SSE 照发、**返回 True** 保水位
- preflight：关闭时跳过 `_preflight_tq_alert` 探测（公式试算 `_smoke_formula_outputs` 保留）

### 3.3 hermes API（`core/api/hermes.py` + `core/services/hermes_service.py`）
- `GET /api/hermes/health` → `{ok, date}`
- `GET /api/hermes/signals?date=YYYY-MM-DD&portfolio_id=&strategy_id=&signal_type=&status=&limit=&offset=`
  - date 缺省 = 今天（Asia/Shanghai）；按 `live_signals.bar_time` 当日范围过滤
  - 跨 session JOIN `live_sessions`，返回 `serialize_signal` 全字段 + `session_id/session_name/session_mode/session_status`
  - envelope `ok()/err()`；date 非法 400；空日 total=0 非 404
- 无鉴权：用户确认决策，后端 `manage.ps1` 改绑 `0.0.0.0:8001`

## 4. 验收清单（A 项）

- [x] 14:20 默认时点：`test_signal_schedule.py` + `test_signal_scheduler.py` 14 passed
- [x] send_warn 开关三向：monitor 关闭行为 / preflight 跳过 / config 默认值（38 passed）
- [x] hermes API 七用例：health/聚合/隔离/默认今天/过滤/空日/非法 date（7 passed）
- [ ] 真机：另一台电脑 curl health + 14:20 前后 signals；通达信无弹窗；前端信号 tab 正常

## 5. 风险与边界

- alert session 须 14:20 前手动启动；14:50 后启动当日不补算（catch_up 超窗跳过并告警）
- 无鉴权 = 内网任何机器可读（用户接受）；防火墙首次放行一次
- 关闭后 `alert_status` 为空：前端信号 tab 该列显示空；`iquant-reconcile` 对账（订单层）不受影响
- 多 session 同日信号不去重（`uq_live_signal_dedup` 含 session 维度天然分开）

## 6. 实施顺序

TDD 切片 0-5 见 `docs/superpowers/plans/2026-11-12-hermes-daily-signals.md`（checkbox 驱动）。
