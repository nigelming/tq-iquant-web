# hermes 日频实盘信号（0017）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** alert 预警模式复用为「日频信号」：每个交易日 14:20 全池计算信号落库，另一台电脑的 hermes 经无鉴权 GET 接口拉取当日信号 JSON，用户在 hermes 侧终端手动执行买卖（不下单、不回传）。send_warn 通达信预警加配置开关、默认关。

**Architecture:** 全部复用 alert 链路（`SignalScheduler` 1d 调度 + `SignalMonitor` 落库/SSE + `AlertDataDispatcher` 只读行情），新增三件事：① 默认时点 14:20；② `live.alert_send_warn_enabled` 开关（config → build_engine → SignalMonitor / preflight）；③ `main/core/api/hermes.py` + `hermes_service.py` 跨 session 按日查询接口，manage.ps1 改绑 0.0.0.0。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / SQLite / pytest；无新依赖、无迁移。

## Global Constraints

- `alert` 模式绝不下单：`AlertDataDispatcher.place_order()` 保持抛错；hermes 接口只读。
- 触发时间只改 **信号调度默认值**（`signal_schedule._DEFAULT_AT`）；`daily_closer._DAILY_CLOSE_TIME=(14,30)` 是 auto 模式日线收盘机制，不动。
- `send_warn_enabled=False` 时 `_persist_and_alert` 必须返回 `True`（返回 False 会恢复水位导致同电平信号每轮重复候选）。
- 组件默认值保留旧行为（`SignalMonitor.send_warn_enabled=True` 默认），全局关闭发生在 config 默认值 + build_engine 注入层。
- 无鉴权为用户确认决策（单用户局域网工具）；后端绑 `0.0.0.0:8001`。
- 所有时间按 Asia/Shanghai（`now_shanghai`）；接口 date 缺省 = 今天上海时区。
- 后端测试：`cd main && PYTHONIOENCODING=utf-8 uv run pytest`。

---

### Task 1: 默认触发时间 14:20

- [ ] Test: `main/core/tests/unit/test_signal_schedule.py` — `parse_signal_at(None, "1d") == time(14, 20)`（已完成，红→绿）
- [ ] Impl: `main/core/services/signal_schedule.py` — `_DEFAULT_AT["1d"]="14:20"`；`parse_signal_at` 非法回退 `time(14, 20)`（已完成）
- [ ] Verify: `uv run pytest core/tests/unit/test_signal_schedule.py core/tests/unit/test_signal_scheduler.py`（14 passed）

### Task 2: send_warn 开关（默认关）

- [x] Test: `test_signal_monitor.py::test_monitor_send_warn_disabled_keeps_new_status`（关闭时：status=new、alert_status 空、不调 send_warn、SSE 照发、水位保留）
- [x] Test: `test_live_alert_service.py::test_preflight_alert_skips_tq_alert_probe_when_send_warn_disabled`（关闭时跳过 `_preflight_tq_alert`，公式试算保留）
- [x] Test: `test_live_alert_service.py::test_send_warn_enabled_reads_config` + `test_config_defaults.py::test_live_alert_send_warn_default_disabled`
- [x] Impl: `core/config.py` `_defaults()` live 节加 `"alert_send_warn_enabled": False`；`packaging/config.template.yaml` 同步
- [x] Impl: `core/engine/live/signal_monitor.py` — 构造参数 `send_warn_enabled=True`；`_persist_and_alert` 关闭分支跳过 send_warn，emit 抽 `_emit_signal`
- [x] Impl: `core/services/live_service.py` — `_send_warn_enabled()` 助手；preflight alert 分支按开关跳过探测；`build_engine` 传 `send_warn_enabled=_send_warn_enabled()`
- [x] Impl: `core/engine/live_engine.py` — 构造参数 `send_warn_enabled`，自动构造 SignalMonitor 时注入
- [x] Verify: signal_monitor/alert_service/config_defaults/live_engine_alert/schedule 共 38 passed

### Task 3: hermes API

- [x] Test: `core/tests/integration/test_hermes_api.py`（health / 跨 session 按日聚合 / date 隔离 / 默认今天 / portfolio_id+strategy_id+signal_type 过滤 / 空日 total=0 非 404 / date 非法 400）
- [x] Impl: `core/services/hermes_service.py` — `daily_signals()`：JOIN live_sessions、bar_time 当日范围、serialize_signal + session_id/session_name/session_mode/session_status
- [x] Impl: `core/api/hermes.py` — `GET /api/hermes/health`、`GET /api/hermes/signals?date=&portfolio_id=&strategy_id=&signal_type=&status=&limit=&offset=`，envelope ok/err
- [x] Impl: `core/main.py` 注册 hermes_router（import + include_router 两处）
- [x] Verify: 7 passed

### Task 4: 外部可访问

- [x] Impl: `manage.ps1` start 分支 `--host 127.0.0.1` → `--host 0.0.0.0`（端口 8001 不变）
- [ ] 人工：Windows 防火墙首次对外监听放行一次（uvicorn / Python）

### Task 5: 文档

- [x] `docs/plans/0017-hermes-daily-signals.md`（简版设计：背景/方案/验收/风险）
- [ ] CLAUDE.md 通信拓扑补一行 hermes 接口（可选，下次实盘日一并做）

## 验收（真机，首个实盘日）

- [ ] `manage.ps1 start` 后另一台电脑 `curl http://<ip>:8001/api/hermes/health` 返回 ok
- [ ] 实盘页建 alert session（绑定组合、1d 频率、signal_at 缺省 14:20）→ start（不再要求 send_warn 探测通过）
- [ ] 14:20 后 `curl http://<ip>:8001/api/hermes/signals` 返回当日信号 JSON；14:20 前为空集
- [ ] 通达信无预警弹窗（send_warn 默认关）；前端信号 tab 信号正常显示、alert_status 列为空
