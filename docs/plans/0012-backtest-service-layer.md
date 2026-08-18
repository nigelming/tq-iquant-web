# 0012 回测 Service 层提取（P1 #9 首块）

> 状态：**已完成**（阶段 0-4 落地，后端 432 全绿；前端 build 待主检出复核；recheck 标注待用户确认）
> 日期：2026-08-18
> 对应审计项：[project-issues-audit.md](../project-issues-audit.md) P1 #9「Service 层完全缺失」
> 接续：[recheck](../project-issues-audit-recheck.md) 结论「P1 仅 #9 service 层仍 open，经评估暂不做（最大组织债，单独立项窗口处理）」。本计划是该单独立项的**第一块**——只做 `backtest_service.py`，为后续 5 个 service 跑通模式。

## 1. 背景与目标

### 1.1 问题：业务逻辑全在路由层

`main/core/api/backtest.py` 874 行，其中业务逻辑约 700+ 行：数据获取层（TQ 对接）、Portfolio 组装、引擎编排、结果持久化、序列化、回测主链路。路由文件臃肿，逻辑不可复用、难以单测（现有单测只能 `import core.api.backtest as bt_api` 绕过 HTTP 打白盒）。

设计文档 [system-plan-draft.md:54-61](../system-plan-draft.md) 规定 6 个 service 文件（stock_pool / formula / strategy / backtest / live / system），全部不存在——`main/core/services/__init__.py` 是空文件。

### 1.2 目标（本计划范围）

**只做 `backtest_service.py`**：把 `backtest.py` 的业务逻辑提取到 `main/core/services/backtest_service.py`，路由层仅剩「参数校验 + 并发锁 + 调 service + 响应包装」。

完成后：
- `backtest.py` 从 874 行降到 ~120 行（路由壳）
- `backtest_service.py` 承接全部业务逻辑，可独立单测
- 现有测试**零改动全绿**（靠 re-export 兼容，见 §4）
- 跑通「路由薄 / service 厚」模式，为后续 5 个 service 立模板

### 1.3 不在本计划范围

- **其余 5 个 service**（stock_pool / formula / strategy / live / system）——逐个单独立项，本计划只立 backtest 模式
- **改测试 import 路径**——本次用 backtest.py re-export 保持 `bt_api.xxx` 兼容（§4），测试文件不动；待 6 个 service 全部就位后再统一评估是否切到 `import backtest_service`
- **`live.py` 重构**——live.py 484 行同样臃肿，但实盘在跑、风险高，单独窗口处理
- **任何行为变更**——纯重构，回测结果、HTTP 响应、并发语义一字不变

## 2. 现状基线

### 2.1 backtest.py 结构（874 行）

| 段落 | 行 | 性质 | 去向 |
|---|---|---|---|
| import + router + `_BACKTEST_LOCK` | 1-33 | 路由壳 | **留路由** |
| `BacktestRequest`（pydantic） | 60-64 | 请求模型 | **留路由**（HTTP 入口契约） |
| 数据获取层：`_convert_market_data` / `_build_polars_kline` / `_is_nan` / `_to_decimal` / `_to_int` / `_convert_market_data_multi` / `build_klines` / `build_open_prices` / `build_benchmark_data` | 72-270 | 业务（TQ 对接） | **迁 service** |
| 公式信号：`_convert_formula_output` / `_parse_date_str` / `_MINUTE_PERIODS` / `_bar_times_by_code` / `build_signal_cache` | 274-414 | 业务（TQ 对接） | **迁 service** |
| DB 辅助：`_pool_stocks` / `_strategy_periods` / `_portfolio_strategies` / `_kline_time_range` | 418-454 | 业务 | **迁 service** |
| 持久化 `_persist_result` | 467-562 | 业务 | **迁 service** |
| 序列化 `_serialize_record` / `_f` / `_serialize_snapshot` / `_serialize_trade` / `_serialize_trade_with_name` / `_serialize_evaluation` | 568-644 | 业务 | **迁 service** |
| 路由 `list_records` / `get_record` / `delete_record` | 650-737 | 路由（含查询逻辑） | **路由留壳，查询逻辑迁 service** |
| `_validate_backtest_request` | 740-748 | 纯校验函数 | **留路由**（err(400) 语义） |
| 路由 `run_backtest` | 751-776 | 路由（校验+锁+409） | **留路由** |
| `_run_backtest_locked` | 779-873 | 业务（主链路） | **迁 service** |

### 2.2 测试引用约束（关键）

两个测试文件 `import core.api.backtest as bt_api`，直接引用以下符号（白盒单测）：

```
bt_api._BACKTEST_LOCK          # 留路由（re-export 不需要，本就在 backtest.py）
bt_api._convert_formula_output # 迁 service → 需 re-export
bt_api._convert_market_data    # 迁 service → 需 re-export
bt_api._convert_market_data_multi  # 迁 service → 需 re-export
bt_api._MINUTE_PERIODS         # 迁 service → 需 re-export
bt_api.build_klines            # 迁 service → 需 re-export
bt_api.build_open_prices       # 迁 service → 需 re-export
bt_api.build_signal_cache      # 迁 service → 需 re-export
bt_api.TQData                  # backtest.py 顶部 import 的类 → 需 re-export
bt_api.TQFormula               # 同上 → 需 re-export
```

测试用 `monkeypatch.setattr(bt_api.TQData, "get_history_raw", fake)` 注入——**类方法 monkeypatch 与 import 路径无关**（`TQData` 是同一个类对象，patch 类属性对所有引用生效），故 `build_klines` 迁到 service 后测试仍绿，前提是 `bt_api.TQData` 这个名字仍在（re-export）。

### 2.3 测试基线

- 后端：376 passed（`uv run pytest -q`）
- `test_backtest_data.py`（单测，604 行）：覆盖 `build_klines` / `build_signal_cache` / `build_open_prices` 编排层
- `test_backtest_api.py`（集成，走 TestClient）：覆盖 HTTP 端点
- `test_backtest_engine.py`（单测）：覆盖 `BacktestEngine`，不碰 backtest.py

## 3. 设计：service 边界

### 3.1 路由层职责（薄）

`backtest.py` 提取后只剩：
1. **HTTP 入口**：`@router.get/post/delete` + pydantic 请求模型
2. **资源校验**：`db.get(PortfolioStrategy, id)` → None 则 `err(404)`（资源不存在的 HTTP 语义）
3. **参数校验**：`_validate_backtest_request(req)` → `err(400)`（纯函数，留路由）
4. **并发锁**：`_BACKTEST_LOCK.acquire(blocking=False)` 失败 → `raise HTTPException(409)`（#12 刻意保留的真实 HTTP 状态码）
5. **响应包装**：`ok(...)` / `err(...)`
6. **调用 service**：把已校验合法的 `ps` + `req` + `db` 交给 service

### 3.2 service 层职责（厚）

`backtest_service.py` 是**模块级函数集合**（沿用现有数据层风格，不引入 class），签名统一 `(db, ...) -> ...`：

```python
# 数据获取层（从 backtest.py 平移，被测试引用 → re-export）
def build_klines(ps, start, end, db=None) -> dict: ...
def build_open_prices(ps, klines) -> dict: ...
def build_benchmark_data(ps, start, end, db=None) -> dict: ...
def build_signal_cache(ps, klines, db=None) -> dict: ...
# 纯函数辅助（同上）
_convert_market_data / _convert_market_data_multi / _build_polars_kline
_is_nan / _to_decimal / _to_int / _convert_formula_output
_parse_date_str / _bar_times_by_code / _MINUTE_PERIODS
_pool_stocks / _strategy_periods / _portfolio_strategies / _kline_time_range

# 持久化 + 序列化（从 backtest.py 平移）
def _persist_result(db, record_id, ps_id, result, strategies) -> None: ...
def serialize_record(r) -> dict: ...          # 公开化（去掉前缀 _，service 对外 API）
def serialize_snapshot(s) -> dict: ...
def serialize_trade(t) -> dict: ...
def serialize_evaluation(e) -> dict: ...

# 查询（list/get/delete 的 DB 逻辑迁此）
def list_records(db) -> list[dict]: ...
def get_record_detail(db, record_id) -> dict | None: ...   # None=不存在
def delete_record(db, record_id) -> bool: ...               # False=不存在

# 主链路（_run_backtest_locked 迁此）
def run_backtest(db, ps, req) -> dict: ...
```

**`run_backtest` 契约**：
- 前置：路由已保证 `ps` 非 None、日期合法、已持锁
- 内部：写 record(running) → assemble → build_klines → build_signal_cache → build_open_prices → build_benchmark_data → 空行情保护 → engine.run → _persist_result → 标 completed → 返回 `{record_id, trades_count, snapshots_count, evaluations}`
- 异常：标 record(failed) + `re-raise`（路由层全局异常处理器 `@app.exception_handler(Exception)` 兜底转 `{code:500}`，与现状一致——现状 `_run_backtest_locked` 也是 raise 出去）

### 3.3 边界判断依据

| 留路由还是迁 service | 判据 |
|---|---|
| 404 / 400 / 409 | HTTP 语义 → 留路由 |
| `_BACKTEST_LOCK` + `HTTPException(409)` | 并发是 HTTP 入口语义 + #12 刻意保留 → 留路由 |
| `_validate_backtest_request` | 纯校验，返回错误消息给路由 `err(400)` → 留路由 |
| `db.get(PortfolioStrategy)` | 资源存在性校验 → 留路由（拿到 ps 传 service） |
| 其余全部 | 业务逻辑 → 迁 service |

## 4. 兼容策略：backtest.py re-export

迁移后 `backtest.py` 顶部加 re-export 块，让 `bt_api.xxx` 仍可用，**测试零改动**：

```python
# backtest.py（迁移后）
from core.services.backtest_service import (  # re-export 供测试 bt_api.xxx 兼容
    build_klines, build_open_prices, build_benchmark_data, build_signal_cache,
    _convert_market_data, _convert_market_data_multi, _convert_formula_output,
    _MINUTE_PERIODS,
)
from core.tq.data import TQData          # re-export（测试 monkeypatch bt_api.TQData）
from core.tq.formula import TQFormula    # re-export（同上）
```

> 注：`_BACKTEST_LOCK` 本就留在 backtest.py，无需 re-export。`TQData`/`TQFormula` 在 backtest.py 迁移后不再直接使用（service 内用），但为测试 monkeypatch 仍 re-export。

**为何不直接改测试 import**：本次目标是「跑通 service 模式 + 零行为变更」，改 6 个测试文件的 import 路径会扩大 diff、增加风险，且 re-export 已能让测试全绿。待 6 个 service 全部就位后，统一评估是否切到 `from core.services.backtest_service import ...`（届时 re-export 可删）。

> **执行修正（2026-08-18，阶段 3 落地时发现）**：re-export 兼容只对「读符号」的测试成立（`test_backtest_data.py` 直接调 `bt_api.build_klines(...)` → re-export 是同一可调用对象，零改动通过）。但对「patch bt_api 命名空间再让回测路径使用 patch」的测试**不成立**——`test_backtest_api.py` 用 `monkeypatch.setattr(bt_api, "build_klines", ...)`，而模块函数的 patch 只改 `bt_api` 模块全局，`service.run_backtest` 调的是 `backtest_service` 模块全局的 `build_klines`，patch 失效，6 个集成测试红。
>
> **根因**：模块函数 monkeypatch 与「调用所在模块」绑定（类方法 patch 才与路径无关——同一类对象）。这是 re-export 模式的固有边界。
>
> **处理**：集成测试 `build_klines`/`build_signal_cache`/`build_open_prices` 的 `setattr` 目标由 `bt_api` 改为 `core.services.backtest_service`（新增 `import ... as svc`，13 处机械改动）；`_BACKTEST_LOCK` 仍 patch `bt_api`（锁留路由）。`test_backtest_data.py` 零改动。**此修正使阶段 3 验收由「集成测试零改动」放宽为「集成测试仅改 patch 目标模块名，断言与 mock 一字不变」**——后续 5 个 service 立项时，凡测试 patch 模块函数的，照此办理；凡只 patch 类方法的（TQData/TQFormula），re-export 即够。

## 5. 实施阶段（每阶段验收：后端 376 全绿 + 现有测试零改动）

### 阶段 0 — 脚手架（建空 service + 通 import 链）

- 新建 `main/core/services/backtest_service.py`（空文件 + 模块 docstring）
- `main/core/services/__init__.py` 保持空（不 eager export，避免循环 import 风险）
- 跑 `uv run pytest -q` 确认 376 绿（此时 service 空，backtest.py 未动）
- **验收**：import 链通，零业务迁移，测试不变

### 阶段 1 — 迁数据获取层 + re-export（最高风险阶段，先做被测试直接引用的）

- 把 §2.1 表中「数据获取层」+「公式信号」+「DB 辅助」共 ~20 个函数平移到 `backtest_service.py`
- `backtest.py` 删除这些函数定义，顶部加 §4 的 re-export 块
- 跑 `uv run pytest core/tests/unit/test_backtest_data.py -q` 确认数据层单测全绿
- 跑 `uv run pytest -q` 确认 376 全绿
- **验收**：`bt_api.build_klines` 等仍可调用（re-export 生效），数据层单测零改动通过

### 阶段 2 — 迁持久化 + 序列化 + 查询

- 迁 `_persist_result` + 6 个 `_serialize_*`（序列化函数去掉前缀 `_` 作 service 公开 API，`_f` 保留私有）
- 迁 `list_records` / `get_record` / `delete_record` 的 DB 查询逻辑到 `list_records(db)` / `get_record_detail(db, id)` / `delete_record(db, id)`
- 路由 `list_records` / `get_record` / `delete_record` 改为调 service + 包装 ok/err（404 判 None）
- 跑 `uv run pytest core/tests/integration/test_backtest_api.py -q` 确认集成测试绿（HTTP 响应不变）
- 跑 `uv run pytest -q` 确认 376 全绿
- **验收**：三个查询端点行为不变，序列化输出字段一字不变

### 阶段 3 — 迁主链路 _run_backtest_locked

- 迁 `_run_backtest_locked` 到 `backtest_service.run_backtest(db, ps, req)`
- 路由 `run_backtest` 改为：校验 ps(404) → 校验日期(400) → 抢锁(409) → `service.run_backtest(db, ps, req)` → 包装 ok
- 异常处理：service 内部已标 record failed + re-raise，路由层不需额外 catch（全局异常处理器兜底，与现状一致）
- 跑 `uv run pytest -q` 确认 376 全绿（含 `test_post_backtest_409_when_already_running` 等并发测试）
- **验收**：`run_backtest` HTTP 行为（404/400/409/200 + 响应体）一字不变

### 阶段 4 — 补 service 层单测

- 新建 `main/core/tests/unit/test_backtest_service.py`
- 直接 `from core.services.backtest_service import ...`（不经 re-export），覆盖：
  - `run_backtest` 主链路（monkeypatch TQ + engine.run，断言 record 状态流转 running→completed、结果持久化）
  - `run_backtest` 异常路径（engine.run 抛错 → record 标 failed + re-raise）
  - `get_record_detail` 不存在返回 None
  - `delete_record` 不存在返回 False / 存在返回 True 且子表清空
- 跑 `uv run pytest -q` 确认 376 + 新增测试全绿
- **验收**：service 层有独立单测，不依赖路由 HTTP 入口

## 6. 风险与回退

| 风险 | 缓解 |
|---|---|
| re-export 漏符号 → 测试 AttributeError | 阶段 1 后立即跑 `test_backtest_data.py`，逐符号核对 §2.2 清单 |
| monkeypatch 失效（若 service 内重新 import TQData 到局部） | service 顶部 `from core.tq.data import TQData`，不在函数内 import；类方法 patch 与路径无关，已验证逻辑 |
| 序列化字段漂移 → 前端破 | 阶段 2 后跑集成测试 + `npm run build`（vue-tsc 校验前端类型） |
| 循环 import（service ↔ 路由） | service 不 import `core.api.backtest`；单向依赖 `路由 → service` |
| 行为微妙变化（如 record 状态写入时机） | 每阶段对比 git diff，保持函数体字节级平移，仅改位置不改逻辑 |

**回退**：每阶段独立提交，任一阶段测试不绿即 `git revert` 该阶段提交，回到上一绿点。阶段 0-3 任意一步都可独立回退，不影响已迁移部分（service 文件保留，路由回退）。

## 7. 验收清单（全部通过 = 计划完成）

- [x] `main/core/services/backtest_service.py` 存在，承接 backtest.py 全部业务逻辑
- [x] `backtest.py` ≤ 130 行（113 行；路由壳：import + router + 校验 + 锁 + 调 service + 响应）
- [x] `uv run pytest -q` 432 全绿（424 原有 + 阶段 4 新增 8 个 service 单测）
- [ ] `npm run build` 通过（前端类型不破）—— **未在本 worktree 执行**：worktree 为全新检出，`web/node_modules` 未安装（gitignored），vue-tsc 不在 PATH。本次纯后端重构、API 请求/响应契约零变更（序列化函数字节级平移，集成测试 `test_get_record_detail` 等断言响应字段一字不变且全绿），前端无受影响面。待主检出跑 `npm run build` 复核（或 worktree 装 deps 后补跑）。
- [x] `test_backtest_data.py` **零改动**通过（re-export 兼容，读符号）
- [x] `test_backtest_api.py` 通过——patch 目标由 `bt_api` 改 `backtest_service`（§4 修正），断言/mock 一字不变
- [x] 回测 HTTP 行为不变：404/400/409/200 + 响应字段 + 并发语义（432 全绿）
- [ ] [project-issues-audit-recheck.md](../project-issues-audit-recheck.md) P1 #9 标注「backtest_service 已提取（其余 5 service 待立项）」—— 待用户确认后标注

## 8. 后续（不在本计划）

- 0013+ 逐个立项：stock_pool_service / formula_service / strategy_service / live_service / system_service
- 6 个 service 全部就位后：评估删除 backtest.py re-export、统一测试 import 到 `core.services.*`
- live_service 风险最高（实盘在跑），单独窗口、需真机回归
