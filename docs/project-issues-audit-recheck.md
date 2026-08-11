# 项目问题清单 — 对照复核

> 复核日期：2026-08-10（初版）
> 修订：2026-08-11 — 审计后 4 个提交改变了 P0 局面，本节追加"提交后状态"
> 复核方法：4 个 explore agent 并行逐条核实当前代码状态（STILL_OPEN / FIXED / INACCURATE）
> 对照原文：[project-issues-audit.md](project-issues-audit.md)

## 总览

| 级别 | 总数 | STILL_OPEN | FIXED | INACCURATE |
|------|------|------------|-------|------------|
| P0 | 8 | 8 | 0 | 0 |
| P1 | 9 | 8 | 0 | 1 |
| P2 | 19 | 19 | 0 | 0 |
| P3 | 9 | 9 | 0 | 0 |
| **合计** | **45** | **44** | **0** | **1** |

**结论**：审计后无任何条目被修复；仅 #15 描述过宽（实为 5 视图中 3 个已补 try/catch，2 个仍缺）。其余 44 条与审计描述一致，全部仍存在。

> **2026-08-11 更新**：P0 8/8 已处理（见下方"提交后状态"）；P1 #13/#14/#42/#15(剩余) 已修（见 P1 提交后状态小节）；#10/#17/#34/#44 已修 + #43 N/A（见"死代码+文档清理"小节）；#23/#24/#29 已修（见"实盘正确性/风险"小节）；#11/#12/#16 已修（见"API 一致性"小节）；#36/#35/#38/#31 已修或部分修（见"P2/P3 低成本清理"小节）；#25/#30/#32 已修（见"引擎/桥去重+封装"小节）；#26 已修（见"前端 any 类型清理"小节）；#27/#28 已修（见"前端收尾"小节）；#33 已修（见"硬编码清理"小节）；#39/#40 已修（见"P3 低成本项"小节）；#18(剩余 1)/#19/#20/#21/#22 已修（见"数据完整性迁移组"小节）；#37/#41/#44/#45 已处理 + #42 已修（见"P3 收尾组"小节）。当前实际仍 open：**P1 1 项（仅 #9 service 层，经评估暂不做）**、**P2 0 项（19/19 清零）**、**P3 0 项（9/9 全部处理）**。

---

## 提交后状态（2026-08-11 核实）

复核文档写下后，仓库有 4 个提交改变 P0 局面。逐条复核当前代码：

| # | 复核期初版状态 | 提交后现状 | 证据 |
|---|------|------|------|
| 1 | 🔴 仍存在 | ✅ **已修** | `f1054fe`：`DRY_RUN = False` 注释改 "real order mode. Flip to True for dry run"，值与注释一致 |
| 2 | 🔴 仍存在 | ✅ **不再适用** | `a0f0515`：token 鉴权整体移除（桥绑 loopback 单用户）。`check_auth`/`TOKEN` 已删，fail-open 前提消失 |
| 3 | 🔴 仍存在 | ✅ **已修** | `6947b36`：`_loop`/`_deals_loop` tick 走单 worker `ThreadPoolExecutor`，事件循环不再被同步 `httpx.Client` 阻塞 |
| 4 | 🔴 仍存在 | ✅ **已修** | `f1054fe`：`_poll_deals` 错误处理已补连续失败计数与 re-raise 路径 |
| 5 | 🔴 仍存在 | ✅ **已修** | `f1054fe`：`live.py:463` stream 端点改 `raise HTTPException(404)`，EventSource onerror 可见；其余端点保留 body-code（非 SSE，前端按 code 判断） |
| 6 | 🔴 仍存在 | ✅ **已修** | `.gitignore:35` 已加 `live/bridge/.bridge_account` |
| 7 | 🔴 仍存在 | 🔴 **仍存在** | `config.yaml` 仅 4 行，无 `iquant_bridge` 段；`api/live.py:37` 靠 `br.get("base_url","http://127.0.0.1:8790")` 兜底。token 移除后安全理由失效，现为纯配置完整性问题（base_url 可配置） |
| 8 | 🔴 仍存在 | ✅ **不再适用** | `a7c5bac`：放弃 PostgreSQL，纯单用户 SQLite。审计的 PG 切换路径前提已废弃 |

**P0 当前结论**：8 项中 7 项已处理（6 修复 + 1 因架构调整不再适用），**仅 #7 仍存在**（config.yaml 缺 `iquant_bridge` 段，纯配置完整性，非安全问题）。

> 附带发现：`web/src/views/SystemConfig.vue:87` 仍残留 "生产期切 PostgreSQL...TQ_DB_PASSWORD" 过时文案（PG 已放弃），应一并清理。

### 2026-08-11 #7 修复（commit b03653a）

- `config.yaml` + `main/core/config.py` `_defaults()` 补 `iquant_bridge.base_url` 段（默认 `http://127.0.0.1:8790`），`_deep_merge` 保证与用户配置正确合并；`api/live.py:_bridge_config()` 现读显式值而非兜底。
- `web/src/views/SystemConfig.vue` 表单补「桥地址」字段——关键：`PUT /configs` 全量覆盖写入，前端表单不含该段会写丢；同时清理数据库段 PostgreSQL 过时文案（PG 已放弃）。
- `live/bridge/README.md` 清理 token 鉴权过时内容（`IQUANT_BRIDGE_TOKEN`/`.bridge_token`/`load_secret`，token 已于 `a0f0515` 移除）。
- `SystemConfig.test.ts` mockConfig + 断言覆盖桥字段渲染与提交。
- 验证：后端 370 passed、前端 77 passed、`npm run build` 通过（vue-tsc 零错误）。

**P0 最终结论：8/8 全部处理完毕**（#1/#3/#4/#5/#6/#7 已修，#2/#8 因架构调整不再适用）。

---

## P0 — 严重（8/8 仍存在）

| # | 状态 | 位置 | 证据 |
|---|------|------|------|
| 1 | 🔴 | `live/bridge/iquant_bridge.py:42` | `DRY_RUN = False` 注释却写"safe default: only print, no real order. Flip to False when ready"，值与注释矛盾 |
| 2 | 🔴 | `live/bridge/iquant_bridge.py:106-109` | `if not TOKEN: return True` — TOKEN 为 None 即放行（fail-open） |
| 3 | 🔴 | `main/core/engine/live_engine.py:245+` | `_loop` 声明 async 但调同步 `dispatcher.heartbeat()` + `bar_poller.poll()`，dispatcher 用 `httpx.Client`（同步阻塞） |
| 4 | 🔴 | `main/core/engine/live_engine.py` `_poll_deals` | `except Exception: db.rollback(); logger.exception(...)` 不 re-raise，全文件无连续失败计数器 |
| 5 | 🔴 | `main/core/api/live.py:460-461` | session 不存在返回 `{"code":404,"message":"资源不存在"}` + HTTP 200，非 `raise HTTPException(404)` |
| 6 | 🔴 | `.gitignore:35` | 只忽略 `.bridge_token`，未忽略 `.bridge_account`（桥 `iquant_bridge.py:88` 读取该文件） |
| 7 | 🔴 | `config.yaml` / `config.py` | 无 `iquant_bridge` 段；`api/live.py:37` 仅靠 `br.get("base_url","http://127.0.0.1:8790")` 兜底，配置缺失静默回落且默认无 token |
| 8 | 🔴 | `main/core/db.py:17` / `config.py` | 仅 `sqlite:///` URL，无 `TQ_DB_PASSWORD`/`postgresql`/`database.url` 任何引用，无 PostgreSQL 切换路径 |

---

## P1 — 高（8 仍存在 / 1 描述不准）

| # | 状态 | 位置 | 证据 |
|---|------|------|------|
| 9 | 🔴 | `main/core/services/__init__.py` | 空文件，无任何 service；业务逻辑仍在路由层（backtest.py 880 行、live.py 484 行、strategies.py 391 行，含 `_build_engine` 等业务函数） |
| 10 | 🔴 | `engine/signal_engine.py` + `event_bus.py` | 两文件存在 + `engine/__init__.py:2,8` 导出，但全仓生产代码**零引用**（仅定义/导出/测试） |
| 11 | 🔴 | 全部 API 路由 | 成功响应普遍缺 `message`（system.py:11/status.py:15/formulas.py:69/live.py:66 均 `{"code":0,"data":...}`），无统一 `ok()`/`err()`（grep 零命中） |
| 12 | 🔴 | `backtest.py` vs `live.py` | backtest 用 `raise HTTPException(404/409,...)`（761/768 行），live 用 `{"code":非零}` + HTTP 200（102/212 行）—— 两套错误模式并存 |
| 13 | 🔴 | `main/core/main.py` | 全文 36 行无 `@app.exception_handler`（grep 全仓零命中），未捕获异常产生 FastAPI 默认 `{"detail":...}` + 500 |
| 14 | 🔴 | `web/src/api/index.ts` | `axios.create({baseURL:'/api'})` 后无 `interceptors`（grep 零命中），不检查 `code!==0` |
| 15 | 🟡 | `Formulas.vue` / `StockPools.vue` | **描述过宽**：审计称 5 视图全缺 try/catch，实际 Backtest/LiveSessions/Portfolios 已补；仅 **Formulas.vue（全裸）+ StockPools.vue（仅 `.catch(()=>[])`）** 仍缺。两文件仍 open，但"前端无 try/catch"表述不准 |
| 16 | 🔴 | API 路由 | 代表端点确认缺失：`GET /stock-pools/{id}`、`POST /formulas/{id}/test-run`、`PUT /live/sessions/{id}` 均无；`DELETE /stock-pools/{id}` 已实现（额外端点） |
| 17 | 🔴 | `AGENTS.md:78` + 全仓 grep | 仍写"TQ 回调线程 → `asyncio.run_coroutine_threadsafe`"，但生产代码 grep `run_coroutine_threadsafe` **零命中**；实际为同步轮询 |

### 2026-08-11 P1 #13/#14/#42/#15(剩余) 修复

四项一致性缺口联动处理（全局异常处理器 + 前端拦截器 + 视图 try/catch + 死代码清理）：

| # | 提交后现状 | 证据 |
|---|------|------|
| 13 | ✅ **已修** | `main/core/main.py` 加 `@app.exception_handler(Exception)` → 未捕获异常返回 `{code:500,message:"服务器内部错误",data:null}` + HTTP 500（非 FastAPI 默认空 body）。**HTTPException 保留 pass-through**（不注册其 handler，3 处 `raise HTTPException` 行为不变：backtest 404/409、live SSE 404 仍真实 HTTP 状态码 + `detail`）。测试：`test_backtest_api` 两 500 测试加 body 断言、`test_live_engine_api` 加 SSE 404 测试 |
| 14 | ✅ **已修** | `web/src/api/index.ts` `api` 实例后加 `interceptors.response.use`：`code!==0`（HTTP 200 + 业务错误）→ reject 带 `response.data`（让 `errMsg` 读 message）；HTTP 404/409/500 由 axios 自动 reject，不动。`api.test.ts` 4 用例覆盖（code:0 resolve / code≠0 reject / 无 code 透传 / HTTP 500 reject） |
| 42 | ✅ **已修** | `api/index.ts` `deleteStrategy` 返回 `res.data.data`（原 `res.data`），与同文件其他函数一致 |
| 15(剩余) | ✅ **已修** | Formulas.vue（4 处 try/catch + errorMsg 条 + errMsg helper）、StockPools.vue（3 处 + 删 L29-33 手动 code 检查死代码 + errMsg）、Backtest.vue（load/openDetail 2 处 + errorMsg 条）、Portfolios.vue（loadPortfolios/toggleExpand/openEditPortfolio/removePortfolio/2 处刷新 getStrategies 6 处 + 删 L324-327 手动 code 检查死代码）全部补 try/catch。拦截器 reject 后无 unhandled rejection。各视图补失败路径测试 |

验证：后端 371 passed、前端 87 passed、`npm run build` 通过（vue-tsc 零错误）。

**P1 当前结论**：9 项中 4 项已处理（#13/#14/#42/#15），**#9/#10/#11/#12/#16/#17 仍存在**（6 项）。

### 2026-08-11 死代码 + 文档清理（#10/#17/#34/#44 + #43 关闭）

清理零引用死代码 + 修订 AGENTS.md 多处与现状相反的过时描述（AGENTS.md 在 CLAUDE.md 中被标为"权威业务规则文档"，过时描述会持续误导）：

| # | 提交后现状 | 证据 |
|---|------|------|
| 10 | ✅ **已修** | 删 `main/core/engine/signal_engine.py`（`SignalEngine` 零生产引用）、`event_bus.py`（`EventBus` 仅测试引用）、`test_event_bus.py`；`engine/__init__.py` 删两行导出。**关键**：`EventBus.process_signals` 的信号优先级逻辑是重复旧实现，`portfolio.py:12-35` 有实际使用的等价实现（`_signal_priority` 被 `Portfolio._process_strategy` 调用），删 EventBus 不丢业务逻辑 |
| 17 | ✅ **已修** | AGENTS.md:75-76 并发模型重写：回测同步内联 + 全局锁 `_BACKTEST_LOCK`（并发 409，非 `ProcessPoolExecutor`）；实盘 `BarPoller` 同步轮询 + 单 worker `ThreadPoolExecutor` 转入（非 `run_coroutine_threadsafe`——生产代码 grep 零命中） |
| 34 | ✅ **已修** | AGENTS.md:5 "Greenfield...无实际代码" → "已实现（脚手架+主链路已通）"，指向 CLAUDE.md 实现状态 |
| 44 | ✅ **已修** | AGENTS.md:96 "约 97% 代码复用" → "核心逻辑共用 + 实盘独有对账/熔断/SSE/周期边界处理" |
| 43 | ✅ **不再适用** | token 鉴权已于 `a0f0515` 整体移除，`iquant_bridge.py` 文件头写 "no auth token is required"，`check_auth`/`TOKEN`/`x-auth-token`/`compare_digest`/`hmac` 全零命中。时序侧信道前提消失，关闭 |

附带清理（同在 AGENTS.md，一并修订）：L18 "WebSocket"→"SSE"（与 L60 自相矛盾）、L112 `live/iguant_gateway/`（已删除）→ `live/bridge/` iQuant HTTP 桥。

验证：后端测试 369 passed（删 2 个 EventBus 测试，371→369）、grep `signal_engine|event_bus` 在 `main/core/` 零残留。

**P1 最终结论**：9 项中 6 项已处理（#10/#13/#14/#15/#17 + #42），**#9/#11/#12/#16 仍存在**（4 项）。**P3**：9 项中 #43 关闭，剩 8 项。

### 2026-08-11 实盘正确性/风险（#23/#29/#24）

实盘引擎三处静默失效/哑火缺陷，逐一 TDD 修复（先红后绿）：

| # | 提交后现状 | 证据 |
|---|------|------|
| 29 | ✅ **已修** | `strategy_context.py` `__init__` 显式声明 `self.strategy_risk: Optional[StrategyRiskManager] = None`（原靠 `portfolio_builder.py:73` 动态设置，未 assemble 时 `getattr(ctx,"strategy_risk",None)` 静默 None → 风控全失效）。`portfolio.py:_check_risks` 在 `risk_manager is None` 时告警（非 raise，避免中断同组合其他策略的 on_bar），让失效可见。测试：`test_strategy_risk_defaults_none` + `test_check_risks_warns_when_strategy_risk_none`（断言 warning + 不产止损单） |
| 24 | ✅ **已修** | `http_bridge_dispatcher.py` 三处 JSON 解析 `except Exception: data={}`/`return []` 补 `logger.warning`（place_order/_get_json/query_quote）。桥返回非 JSON（HTML 错误页/坏网关）原被当业务拒绝/空结果静默吞，现告警可见。**网络异常仍抛 `BridgeUnavailableError`**（不重复告警）。测试：3 个 `*_non_json_body_warns_*` 断言 warning + 返回值 |
| 23 | ✅ **已修** | `live_engine.py` 加 `now_shanghai()` 辅助（`datetime.now(tz=_CST).replace(tzinfo=None)`，naive 与引擎其余 datetime 一致），替换 6 处裸 `datetime.now()` 时间点判定。核心：`(14,30)` 日终判定改按上海时间——Core 部署 UTC 服务器时本机 14:30 ≠ 上海 14:30，日终哑火会让 E5/E6 熔断、1d 快照、1w/1mon 注入全失效。`_maybe_daily_close`/`_maybe_daily_bars` 加 `now=None` 可注入参数便于测试。测试：`test_now_shanghai_returns_shanghai_wall_clock`（UTC 06:30→上海 14:30）+ `test_maybe_daily_close_uses_shanghai_time`（14:29 不触发/14:30 触发） |

验证：后端测试 376 passed（+5 新测试：#29×2、#24×3、#23×2，其中 #23 的 `test_now_shanghai` 与 `daily_close` 合并计）。`now_shanghai` 返回 naive，与引擎内所有 datetime 比较一致（aware 与 naive 比较会抛 TypeError）。

**P2 当前结论**：19 项中 #23/#24 已修（仍 open 17 项）；**P2 #29 实为 P2 表内 #29**（见 P2 表，原列于 P2），现亦已修——P2 open 降至 16 项。

### 2026-08-11 API 一致性（#11/#12/#16）

P1 收尾三联项——统一响应 envelope + 错误模式收敛 + 设计端点偏差标注。纯重构 + 文档，无 schema 迁移、无业务逻辑变更：

| # | 提交后现状 | 证据 |
|---|------|------|
| 11 | ✅ **已修** | 新增 `main/core/api/response.py`：`ok(data=None,message="ok")` / `err(code,message,data=None)` 强制三键齐全。7 个路由文件（formulas/strategies/live/backtest/stock_pools/status/system）手工 `return {"code":0,"data":...}` / `{"code":4xx,"message":...}` 全替换为 `ok()`/`err()`。grep `{"code": 0` 在 `main/core/api/` 仅剩 `response.py` 内部。3 个空列表测试断言 `== {"code":0,"data":[]}` 更新为含 `message:"ok"`（test_formula/test_portfolio/test_stock_pool_api）。**前端无破坏**：拦截器按 `code!==0` reject，`undefined`→`null` 微变各调用方均当 falsy 处理 |
| 12 | ✅ **已修** | `backtest.py:761` `raise HTTPException(404,"portfolio strategy not found")` 收敛为 `return err(404,"组合策略不存在")`（body-code 模式 A）。**两处有意保留**：`backtest.py:769` `HTTPException(409,...)` 并发锁——CLAUDE.md 约定"并发启动返回 HTTP 409"+ 测试断言 `status_code==409`，加注释标明刻意例外；`live.py:461` SSE 404——EventSource 需真实 HTTP 错误码触发 onerror（#5 已定）。grep `HTTPException` 在 `main/core/api/` 仅剩这 2 处 |
| 16 | ✅ **已标文档（不补端点）** | 用户决策：仅标设计文档，暂不补 12 个未实现端点。`docs/system-plan-draft.md` §5.6.3/5.6.4/5.6.7/5.6.8 各加"实现状态"blockquote：标注哪些已并入现有端点（回测 trades/snapshots/results #7-9 已并入 `GET /records/{id}` 内嵌详情；公式信号 #2-5 已并入公式 CRUD 全量保存）、哪些未实现（股票池详情/公式试运行/单组合启停/编辑会话）、路径偏差（`{id}/sync`→`/sync`、`{id}/stocks`→`/tdx/{code}/stocks`）、额外端点（`DELETE /stock-pools/{id}`、`DELETE /records/{id}`、`positions`、`bridge-status`）。后续若需单组合启停/公式试运行，单独立项 |

验证：后端测试 376 passed（`test_loop_offline_to_online_still_resets_baseline_after_thread_offload` 偶发 flaky，隔离运行通过，与响应格式无关）；grep `{"code": 0` 仅 `response.py`；grep `HTTPException` 仅 backtest 409 + live SSE 404 两处有意保留。

**P1 最终结论**：9 项中 8 项已处理（#10/#11/#12/#13/#14/#15/#16/#17 + #42），**仅 #9 service 层仍 open**（架构重构，路由直接操作 ORM，单独立项）。**P1 open 4→1**。

### 2026-08-11 P2/P3 低成本清理（#36/#35/#38/#31）

#9 service 层经评估为单个体量最大的组织债（非故障债），用户决策暂不做（系统当前功能正确）。先扫一批零风险低成本项：

| # | 提交后现状 | 证据 |
|---|------|------|
| 36 | ✅ **已修** | `backtest.py:855` `datetime.utcnow()` → `datetime.now(timezone.utc).replace(tzinfo=None)`（naive UTC，与原 `utcnow()` 语义一致，消除 Python 3.13 弃用警告）。顶部 import 补 `timezone` |
| 35 | ✅ **已修** | `status.py` 删 `iguant_gateway` 硬编码死块（NATS 网关已于架构调整废弃，`live/iguant_gateway/` 删除）。前端/测试 grep `iguant_gateway` 零引用 |
| 38 | ✅ **已修** | `conftest.py:24` `app.dependency_overrides["get_db"]` 字符串 key → 函数对象 `get_db`（顶部补 `from core.db import get_db`）。注：`test_client` fixture 实际零引用（各集成测试自带 `client` fixture），`db_session` fixture 仍被 `test_backtest_data.py` 使用 |
| 31 | ✅ **部分修** | `live_engine.py:84` 函数内 `import math` 提到模块顶部（stdlib 轻量无理由 lazy）；`:895` `import pandas as pd` **刻意保留 lazy** + 加注释——pandas 较重且仅公式注入路径调用，模块导入期无条件加载不划算。审计 #31 的精神是消除"无必要的"函数内 import，pandas 此处为有意的延迟加载 |

附带勘察更正：审计 #18"8 个 FK 无 ondelete"现**多数已修**——grep `ondelete` 显示 `portfolio_strategy.stock_pool_id`/`strategy.portfolio_id+formula_id`/各 `*_record_id`/`live_session_id` 等 FK 均已带 `ondelete=CASCADE/RESTRICT`；**仅 `strategy.master_strategy_id`（自引用）仍无 ondelete**（#18 剩 1 处）。#19 `stock_pool.code` 仍无 `unique=True`（#19 仍 open，需迁移）。

验证：后端测试 376 passed（utcnow 弃用警告同步消除）。

**P2/P3 当前结论**：#36/#35 已修（P2 16→15，P3 #38 8→7）；#31 部分修（math 提顶，pandas 刻意保留）。**#9 service 层暂不做**（评估为最大组织债，单独立项窗口处理）。

### 2026-08-11 引擎/桥去重 + 封装（#25/#30/#32）

继续扫 P2/P3 低风险项，聚焦引擎层重复 + 跨类私有访问 + 桥内存无界：

| # | 提交后现状 | 证据 |
|---|------|------|
| 25 | ✅ **已修** | `_total_value` + `_find_strategy` 在 `backtest_engine.py` 与 `live_engine.py` **字节级重复**。下沉到 `Portfolio` 作公共方法 `total_value(bar)` / `find_strategy(sid)`（与已有 `_find_strategy_by_id`/`snapshot` 同域）。两引擎 6 处调用点改 `portfolio.total_value(bar)` / `portfolio.find_strategy(sid)`，删 4 处重复定义。连带清理两引擎各自因去重而悬空的 `StrategyContext` import |
| 30 | ✅ **已修** | `live_engine.py` 5 处 `self._bar_poller._stock_codes` + 1 处 `self._dispatcher._order_id(order)` 跨类访问私有。`BarPoller` 加只读 `stock_codes` property（与已有 `last_completed_stime` property 同模式）；`HttpBridgeDispatcher._order_id` staticmethod 改公共 `order_id`（纯函数无状态，本就该 public）。6 处调用点改公共访问，grep `\._bar_poller\._\|\._dispatcher\._order_id` 零残留 |
| 32 | ✅ **已修** | `iquant_bridge.py` `_placed = {}` 幂等缓存无 TTL/上限，长期运行内存无界增长。改 `OrderedDict` + `PLACED_MAX=5000` 上限，插入后超限 `popitem(last=False)` 淘汰最旧（idempotency 只对近期重试有意义，月级 order_id 不会被重发）。`OrderedDict` 是 dict 子类，`oid in`/`[oid]`/赋值语义不变，向后兼容。桥纯 ASCII/GBK/Py3.6 兼容（`collections.OrderedDict` 标准库） |

验证：后端测试 376 passed；`iquant_bridge.py` GBK 解析通过；grep 跨类私有访问零残留。

**P2/P3 当前结论**：#25/#30/#32 已修（P2 15→12）。剩余 P2 多为数据完整性项（#18 剩 1 处/#19 unique/#20 索引/#21 server_default，需 Alembic 迁移，单用户有数据有风险）+ 前端 #26 any 类型（见下小节，已修）+ #33 硬编码 + #22 init_db 走 Alembic。

### 2026-08-11 前端 any 类型清理（#26）

跨 6 视图 + API 客户端清除业务对象 any，纯类型改动（vue-tsc 可验证，不碰运行时）：

| # | 提交后现状 | 证据 |
|---|------|------|
| 26 | ✅ **已修** | `api/index.ts` 14 个 `any` 泛型全替换为 13 个类型化响应接口（StockPoolItem/TdxPoolItem/TdxPoolStockItem/FormulaSignalItem/FormulaItem/PortfolioItem/BacktestRecordItem/BacktestSnapshotItem/BacktestTradeItem/BacktestEvaluationItem/BacktestStrategyEvaluationItem/BacktestStrategySnapshotItem/BacktestDetailItem，字段对齐各路由 serializer，`StrategyDetail` 补 `portfolio_id`）。6 视图清业务对象 any：Backtest.vue（4 refs 类型化 + pct/num/valueClass/buildMetrics 参数 + 删 `as any[]`）、Portfolios.vue（3 refs 类型化 + toPercent/fromPercent 参数 + toggleExpand/openEditPortfolio 参数 + 2 处 `const out: any` → `Record<string,unknown>`）、Formulas.vue（formulas ref + openEdit 参数 + signals map 走推断）、LiveSessions.vue（本地 LiveSessionItem 接口 + portfolios ref<PortfolioItem> + TAB_LABEL 键类型化删 `key as any`）、SystemConfig.vue（SystemConfig 接口 + config ref 类型化 + catch `e: any` → instanceof 收敛）、StockPools.vue（删本地重复 TdxPool 接口改用共享 TdxPoolItem + stocksList ref<TdxPoolStockItem>）。**合理 any 保留**：`errMsg(e: any)`（外部 axios 错误结构）、echarts `series: any[]`/`params: any`（第三方回调签名）、测试文件 `(fn as any).mock`（vitest mock 惯用法） |

验证：`npx vue-tsc --noEmit` 零错误；`npx vitest run` 87 passed（10 文件）。

**P2 当前结论**：#26 已修（P2 12→11）。剩余 P2 以数据完整性项为主（#18 剩 1 处/#19 unique/#20 索引/#21 server_default，需 Alembic 迁移，单用户有数据有风险）+ #33 硬编码 + #22 init_db 走 Alembic。

### 2026-08-11 前端收尾（#27/#28）

#26 之后继续清前端一致性缺口——原生 axios 直连收敛到统一 API 客户端 + 5 视图补加载态：

| # | 提交后现状 | 证据 |
|---|------|------|
| 27 | ✅ **已修** | `api/index.ts` 追加实盘会话 CRUD/启停 + 系统配置封装（`getLiveSessions`/`createLiveSession`/`startLiveSession`/`stopLiveSession`/`deleteLiveSession`/`getSystemConfigs`/`updateSystemConfigs`，类型 `LiveSessionItem`/`LiveSessionCreate`/`SystemConfig` 对齐 live.py list_sessions serializer + core/config.py `_defaults()`）。`LiveSessions.vue` 删原生 `import axios`，会话查询/新建/启停改走客户端（load 补 try/catch 防 unhandled rejection）；`SystemConfig.vue` 删原生 axios，GET/PUT 改走客户端。**收益**：两视图统一走响应拦截器（code≠0 → reject），错误路径与其余视图一致。测试 mock 同步：LiveSessions.test.ts/SystemConfig.test.ts 从 `vi.mock('axios')` 改 mock `../api` 函数（`getLiveSessions` 等直接 resolved 数组，替代 `{data:{data}}` 包装） |
| 28 | ✅ **已修** | 5 视图补 `loading` ref + 模板 gate（`v-if="loading"` 加载中条 / `v-else` 数据表）：Backtest.vue/Formulas.vue/LiveSessions.vue/Portfolios.vue/StockPools.vue。load 函数 `finally { loading = false }`（重载/刷新复用 load 时不闪加载态）。SystemConfig.vue 原本已有 loading，不动 |

验证：`npx vue-tsc --noEmit` 零错误；`npx vitest run` 87 passed（10 文件，含两测试文件 mock 重写后全绿）。

**P2 当前结论**：#27/#28 已修（P2 11→9）。剩余 P2 以数据完整性项为主（#18 剩 1 处/#19 unique/#20 索引/#21 server_default，需 Alembic 迁移，单用户有数据有风险）+ #33 硬编码 + #22 init_db 走 Alembic。

### 2026-08-11 硬编码清理（#33）

审计点名的三处魔法数字，逐一提为命名常量（纯重构，无行为变更）：

| # | 提交后现状 | 证据 |
|---|------|------|
| 33 | ✅ **已修** | `evaluator.py` `rf = 0.02`（Sharpe/Sortino 无风险利率）→ 模块级常量 `RISK_FREE_RATE = 0.02` 带注释。`live_engine.py` 两处 `if (now.hour, now.minute) < (14, 30)`（`_maybe_daily_close`/`_maybe_daily_bars`，日终判定）→ 模块级常量 `_DAILY_CLOSE_TIME = (14, 30)`，与 `_CST` 同区并注释说明 E5/E6 + C6(B/C) 共用。`poll_interval=30.0` 经核实为构造参数默认值（可配置），非魔法数字，不在处理范围 |

验证：后端测试 376 passed；grep `(14, 30)`/`rf = 0.02` 在 `main/core`（非测试）仅剩常量定义处。

**P2 当前结论**：#33 已修（P2 9→8）。剩余 P2 全部为数据完整性/迁移项（#18 剩 1 处/#19 unique/#20 索引/#21 server_default/#22 init_db 走 Alembic）——单用户有数据，迁移有风险，建议单独立项评估。

### 2026-08-11 P3 低成本项（#39/#40）

继续清 P3 文档/依赖类零风险项：

| # | 提交后现状 | 证据 |
|---|------|------|
| 40 | ✅ **已修** | `pinia ^4.0.2` 已装未用（`src/stores/` 空目录、`defineStore` 零命中、`main.ts` 仅 `use(router)` 未注册 pinia）→ `npm uninstall pinia` 移除依赖，`package.json`/`package-lock.json` 同步，空 `src/stores/` 目录删除。前端无需状态管理库（视图本地 ref 足够） |
| 39 | ✅ **已修** | 根目录新增 `README.md`（架构 4 模块 2 环境 + 快速开始命令 + 关键约定 + 文档索引），项目简介缺 README 缺口闭合 |

附带修复（build 验证暴露）：Backtest.vue 三处 `vue-tsc --noEmit` 未查但 `vue-tsc -b`（build 模式）报出的类型问题——`buildMetrics` 参数补 `| undefined`（`detail.value?.evaluations` 链式访问含 undefined，`if (!m)` 已兜底）、`benchmarkCurve` 的 `base` 加 `?? 0`（`find` 可能无结果）。说明 `--noEmit` 与 build 模式检查强度不同，后者更严。

验证：`npm run build` 通过（vue-tsc -b + vite）；`npx vitest run` 87 passed；后端 376 passed。

**P3 当前结论**：#39/#40 已修（P3 7→5）。剩余 #37（flaky 测试，需单独 TDD 处理）/ #41（Dashboard 首页，功能新增成本高）/ #45（relationship，架构级）。

---

## P2 — 中（19/19 仍存在）

| # | 状态 | 位置 | 证据 |
|---|------|------|------|
| 18 | 🔴 | 8 个 FK | `backtest_trade.py:13`(`formula_signal_id`)、`live_trade.py:12-14`、`backtest_trade.py:12`、`live_order.py:12-13`、`strategy.py:16`(`master_strategy_id`) 均无 `ondelete=` |
| 19 | 🔴 | `stock_pool.py:11` | `code = Column(String(50), nullable=False)` — 无 UniqueConstraint、无 Index、无 `unique=True` |
| 20 | 🔴 | 多个 FK 列 | `stock_pools.code`/`strategies.portfolio_id`/`live_orders.stock_code` 等无 Index（注：`live_orders.portfolio_strategy_id` 有索引，但审计点名的 `stock_code` 仍缺） |
| 21 | 🔴 | 多模型 | `strategy.py` 10 列、`portfolio_strategy.py` 9 列、`live_order.filled_quantity`、`backtest_record.progress` 等仅 `default=` 无 `server_default`（仅 `formula.formula_count` 两者都有） |
| 22 | 🔴 | `db.py:36` | `init_db()` 用 `Base.metadata.create_all()` 绕过 Alembic |
| 23 | 🔴 | `live_engine.py` | 第 201/216/306/352/1049/1123/1153 行用 `datetime.now()` 无时区；`(14,30)` 日终判定依赖裸服务器时间（bar_poller.py 有 `_CST` 但 live_engine 未用） |
| 24 | 🔴 | `http_bridge_dispatcher.py` | `place_order:87` `except: data={}`、`_get_json:121` `except: return []`、`query_quote:158` `except: return []` — 全无 logger，错误静默吞没 |
| 25 | 🔴 | 两引擎 | `_total_value`（backtest_engine.py:178 / live_engine.py:646）+ `_find_strategy`（:170 / :658）完全重复 |
| 26 | 🔴 | `web/src/` | 52 处 `: any`/`<any>` 分布 9 文件，`api/index.ts` 占 13 处 |
| 27 | 🔴 | `api/index.ts` | 缺 session CRUD/start/stop + 系统配置封装；`LiveSessions.vue`/`SystemConfig.vue` 直接 `import axios` 用原生 axios（绕过 API 客户端） |
| 28 | 🔴 | 6 视图 | 仅 `SystemConfig.vue:15` 有 `loading` ref；其余 5 视图（Backtest/Formulas/LiveSessions/Portfolios/StockPools）无加载态 |
| 29 | 🔴 | `strategy_context.py` | `__init__` 无 `self.strategy_risk`；全文件 grep `strategy_risk` 零命中 —— 靠 `portfolio_builder.py:73` 动态设置 + `getattr(...,None)` 访问，未 assemble 时风控静默失效 |
| 30 | 🔴 | `live_engine.py` | `self._bar_poller._stock_codes` 4 处（361/623/874/905）+ `self._dispatcher._order_id` 1 处（554）访问私有属性 |
| 31 | 🔴 | `live_engine.py` | `import math` 在函数体内（:70，嵌套于 if 块）；`import pandas as pd` 在函数体内（:797） |
| 32 | 🔴 | `iquant_bridge.py:55` | `_placed = {}` 存订单结果，无 TTL/清理/大小限制，长期运行内存无界增长 |
| 33 | 🟠部分 | 硬编码 | `evaluator.py:46` `rf=0.02`、`live_engine.py:307/353` `(14,30)` 确实硬编码；**但 `poll_interval=30.0` 是构造参数默认值（可配置），非魔法数字** —— 此子项描述偏严，但整体仍 open |
| 34 | 🔴 | `AGENTS.md:5` | 仍写"Greenfield — 仅存在设计文档…无实际代码"，与现状（14 模型+17 引擎文件+7 路由+6 视图）完全不符 |
| 35 | 🔴 | `status.py:22-26` | 硬编码 `iguant_gateway: {online:False,...}`（NATS 网关已废弃） |
| 36 | 🔴 | `backtest.py:858` | `rec.completed_at = datetime.utcnow()` — Python 3.13 已弃用 |

---

## P3 — 低（9/9 已处理）

| # | 状态 | 位置 | 证据 |
|---|------|------|------|
| 37 | ✅ **已修** | `test_live_engine.py` | `test_loop_offline_to_online_resets_baseline`/`..._still_resets_baseline_after_thread_offload` 2 个离线→在线转场测试的固定 `sleep` 改有界轮询（沿用 `:1831-1839` 先例），消除跨线程调度抖动下的偶发失败 |
| 38 | ✅ **已修** | `conftest.py:25` | `app.dependency_overrides[get_db]` 用函数对象 key（非字符串），见"P2/P3 低成本清理"小节 |
| 39 | ✅ **已修** | 项目根 | `README.md` 已新增，见"P3 低成本项"小节 |
| 40 | ✅ **已修** | `web/` | `pinia` 依赖已移除、空 `src/stores/` 删除，见"P3 低成本项"小节 |
| 41 | ✅ **已修** | `web/src/views/Dashboard.vue` | 新增最小 Dashboard 首页（统计卡 + 最近回测 + 配置），路由 `/` 重定向 `/dashboard`，见"P3 收尾组"小节 |
| 42 | ✅ **已修** | `api/index.ts:363-366` | `deleteStrategy` 已返回 `res.data.data`，与同文件其他 5 个 delete 一致，见"P1 一致性"小节 |
| 43 | ✅ **不再适用** | `iquant_bridge.py` | token 鉴权整体移除（`a0f0515`），时序侧信道前提消失，见"死代码+文档清理"小节 |
| 44 | ✅ **已修** | `AGENTS.md` | "97% 代码复用"已修订（`f6c6ff3`）；本组再修迁移数 6→7 与测试路径 `tests/`→`core/tests/`，见"P3 收尾组"小节 |
| 45 | ✅ **非缺陷** | `main/core/models/` | 模型零 `relationship()` 是架构偏好非缺陷：FK + 外键约束 + 显式查询已满足完整性，加 relationship 仅省 ORM 级联读写。维持现状（用户确认），见"P3 收尾组"小节 |

---

## 修复建议优先级（沿用审计原文，按复核结果）

| 优先级 | 编号 | 主题 | 修复成本 |
|--------|------|------|----------|
| **立即（安全）** | #1, #2, #6 | 桥 DRY_RUN/鉴权/gitignore | 低（改值/改逻辑/加一行） |
| **立即（实盘正确性）** | #5, #7 | SSE 404 状态码 + config.yaml iquant_bridge 段 | 低 |
| **近期（引擎）** | #3, #4, #23 | 异步 I/O 包装 / 错误 re-raise / 时区统一 | 中 |
| **近期（一致性）** | #11, #12, #13, #14 | 统一 ok()/err() + 全局异常处理器 + 前端拦截器 | 中 |
| **近期（前端健壮）** | #15(剩余2文件), #27, #28 | 补 try/catch + API 客户端 + loading 态 | 中 |
| **迭代（架构）** | #9, #10, #17, #34 | service 层 / 删死代码 / 改 AGENTS.md 并发模型 + 项目状态 | 高 |
| **迭代（数据完整性）** | #18, #19, #20, #21, #22 | FK ondelete + 索引 + server_default + init_db 走 Alembic | 中（需迁移） |
| **长期（质量）** | #24-#26, #29-#33, #35, #36 | JSON 错误日志 / 去重 / any 类型 / 封装 / import 位置 / _placed TTL / 硬编码 / status.py / utcnow | 中-高 |
| **长期（风格）** | #37-#45 | flaky / conftest key / README / Pinia / 仪表盘 / deleteStrategy / token 比较 / 97% / relationship | 低-中 |
| **生产前（PG）** | #8 | PostgreSQL 切换路径 | 中（db.py + config.py + TQ_DB_PASSWORD） |

---

## 最该先动的 5 条

1. **#6** `.bridge_account` 加进 .gitignore —— 一行，防账号文件误提交
2. **#1** DRY_RUN 值与注释统一 —— 一行，消除误导
3. **#7** config.yaml 补 `iquant_bridge` 段 —— 消除默认无 token 鉴权
4. **#2** check_auth 改 fail-closed —— 防"未配 token = 无鉴权"
5. **#5** SSE 404 用 HTTPException —— 让前端 onerror 可见

这 5 条都是低成本、高安全收益，适合先批量处理。

---

## 2026-08-11 数据完整性迁移组（#18 剩余/#19/#20/#21/#22）

一条 Alembic 迁移链 `a5b351e0d6d7 → d4a5b6c7d8e9` + 9 个模型同步 + init_db 改造，P2 清零。

| # | 状态 | 处理 |
|---|------|------|
| 18 | ✅ **已修**（剩余 1 处） | `strategies.master_strategy_id` 自引用 FK 补 `ondelete="RESTRICT"`（业务规则禁止 slave 无 master 孤儿行；delete_strategy app 层预检 + DB 层兜底）。迁移 drop/create FK + 模型 `strategy.py:16` |
| 19 | ✅ **已修** | `stock_pools.code` 补 `UNIQUE`（`uq_stock_pools_code`）；dev.db 无重复 code，迁移安全。测试 `test_create_strategy_slave_master_wrong_portfolio` 的 `_seed_pool` 改幂等（同 code 复用，模拟"同一板块被多组合引用"）以符合新约束 |
| 20 | ✅ **已修** | FK/查询列补索引 13 处：strategies×3、portfolio_strategies.stock_pool_id、live_orders×2（strategy_id/stock_code）、live_trades×2、backtest_records.portfolio_strategy_id、backtest_trades×2、formula_signals.formula_id。`backtest_trades`/live 系列无 ondelete FK 有意保留（改会破坏 `test_portfolio_api.py:675` 的 IntegrityError 预期） |
| 21 | ✅ **已修** | 26 列补 `server_default` 并**一并收紧 NOT NULL**（用户确认）：strategy 10、portfolio_strategy 12、live_order.filled_quantity、backtest_record.progress、live_session_portfolio 2。`formula.formula_count` 已有 server_default 不动 |
| 22 | ✅ **已修** | `init_db()`（`core/db.py`）改 `alembic command.upgrade(head)`，替代 `Base.metadata.create_all`，schema 变更全部走迁移链。**刻意不加载 alembic.ini**——env.py 的 fileConfig() 默认 disable_existing_loggers 会禁用 core.* 业务 logger，污染运行期日志与测试 caplog（曾导致 4 个顺序敏感测试全量失败）；改为无参 `Config()` + 绝对路径 `script_location` + 手动 `sys.path` 插入 |

**迁移**：`main/alembic/versions/d4a5b6c7d8e9_add_data_integrity_constraints.py`（down_revision=`a5b351e0d6d7`，全 `op.batch_alter_table`）。`alembic check` 无差异；`uv run pytest -q` **376 全绿**；dev.db 迁移前已备份 `main/data/dev.db.bak`；server_default/unique 已实测（插入不带默认列 → 默认值落库；重复 code → IntegrityError）。

**P2 当前结论**：#18(剩余 1)/#19/#20/#21/#22 全部已修，**P2 19/19 清零**。剩余 open 仅 P1 #9（service 层，暂不做，见"P3 收尾组"）。

---

## 2026-08-11 P3 收尾组（#37/#41/#42/#44/#45）

P3 最后 5 项收尾。其中 #42 代码已修（仅文档表同步）、#44 部分已修（补 3 处小不一致）、#45 评估为非缺陷（用户确认维持现状）；#37 为真实 flaky 缺陷、#41 为最小 Dashboard 功能新增：

| # | 提交后现状 | 证据 |
|---|------|------|
| 37 | ✅ **已修** | `test_live_engine.py` `test_loop_offline_to_online_resets_baseline`（`:1347-1396`）/`test_loop_offline_to_online_still_resets_baseline_after_thread_offload`（`:2478-2524`）的固定 `sleep(0.08/0.1)` 改有界轮询（`while len(reset_calls) < 1 and ... < deadline: await asyncio.sleep(0.005)`），沿用 `:1831-1839` 先例。断言不变（`bridge_online is True`、`len(reset_calls) == 1`）。文件重复跑 5 次无失败 |
| 41 | ✅ **已修** | 新增 `web/src/views/Dashboard.vue`（metric-grid 统计卡：股票池/公式/组合/回测/实盘/系统状态 + 最近回测表 + 配置卡；`Promise.allSettled` 逐项兜底，任一失败不阻塞其余）；`web/src/api/index.ts` 加 `getSystemStatus()`（消费 `GET /api/status`）；路由 `/` → `/dashboard` + 新路由；`App.vue` 菜单/titles 加"🏠 首页"；新增 `web/src/__tests__/Dashboard.test.ts`（挂载渲染 7 getter 全调 + 单源失败不阻塞其余）。`npm run build` 通过、vitest 89 passed |
| 42 | ✅ **已修** | `api/index.ts:363-366` `deleteStrategy` 已返回 `res.data.data`（提交 22227e5），本组仅同步 recheck.md 表 |
| 44 | ✅ **已修** | `f6c6ff3` 已修"97% 复用"；本组补：`AGENTS.md:5` 迁移数 6→7（head `d4a5b6c7d8e9`）、`AGENTS.md:37-39` 测试路径 `tests/`→`core/tests/`（实际 `main/core/tests/`）、`CLAUDE.md:9` 删失效"Greenfield 已过时"交叉引用 |
| 45 | ✅ **非缺陷** | 14 个模型零 `relationship()` 是架构偏好非缺陷：FK + 外键约束 + `PRAGMA foreign_keys=ON` + 显式查询已满足完整性，relationship 仅省 ORM 级联读写。**维持现状**（用户确认），记录结论，后续若做关系层重构另行立项 |

验证：后端 `uv run pytest -q` **376 全绿**（含 2 个转场测试，文件级重复 5 次稳定）；前端 `npm run build`（vue-tsc -b）零错误 + `npx vitest run` **89 passed**（含新增 Dashboard.test.ts 2 例）。

**P3 当前结论**：#37/#41/#42/#44/#45 全部处理，**P3 9/9 清零**。

**整体收尾状态**：45 项审计中，**P0 8/8、P2 19/19、P3 9/9 全部处理**；P1 仅 #9 service 层经评估暂不做（最大组织债，单独立项窗口处理）。当前仍 open 唯一项 = **P1 #9**。
