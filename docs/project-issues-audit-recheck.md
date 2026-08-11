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

> **2026-08-11 更新**：P0 8/8 已处理（见下方"提交后状态"）；P1 #13/#14/#42/#15(剩余) 已修（见 P1 提交后状态小节）。当前实际仍 open：P1 6 项、P2 19 项、P3 9 项。

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

## P3 — 低（9/9 仍存在）

| # | 状态 | 位置 | 证据 |
|---|------|------|------|
| 37 | 🔴 | `test_live_engine.py` | `test_loop_offline_to_online_resets_baseline` 存在，为同步函数内嵌 `asyncio.run()`，无 `pytest.mark.asyncio` 标记 |
| 38 | 🔴 | `conftest.py:24` | `app.dependency_overrides["get_db"]` 用字符串 key（应改为函数对象 `get_db`） |
| 39 | 🔴 | 项目根 | 无 `README.md`（根目录仅 AGENTS.md/CLAUDE.md/config.yaml/docker-compose.yml/docs/main/shared/web） |
| 40 | 🔴 | `web/src/stores/` | 空目录；`package.json` 含 `pinia ^4.0.2`；`web/src` grep `defineStore` 零命中 —— 已装未用 |
| 41 | 🔴 | `web/src/views/` | 无 Dashboard/Home 视图；路由 `/` 仅 `redirect: '/stock-pools'` |
| 42 | 🔴 | `api/index.ts:193` | `deleteStrategy` 返回 `res.data`（完整 ApiResponse）而非 `res.data.data`，与同文件其他函数不一致 |
| 43 | 🔴 | `iquant_bridge.py:109` | `headers.get("x-auth-token") == TOKEN` 用 `==`（grep `compare_digest`/`hmac` 零命中），有时序侧信道风险 |
| 44 | 🔴 | `AGENTS.md` | 模块复用段仍写"97% 代码复用"，未修订为"核心逻辑共用 + 引擎特有逻辑实盘独有" |
| 45 | 🔴 | `main/core/models/` | 16 个模型文件 grep `relationship(` 零命中，全部仅 `Column(ForeignKey(...))` |

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
