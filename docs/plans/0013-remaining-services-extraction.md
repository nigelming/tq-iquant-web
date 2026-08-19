# 0013 其余 4 个 Service 层提取（P1 #9 续块）

> 状态：**待实施**
> 日期：2026-08-18
> 对应审计项：[project-issues-audit.md](../project-issues-audit.md) P1 #9「Service 层完全缺失」
> 接续：[0012-backtest-service-layer.md](0012-backtest-service-layer.md) 已跑通 backtest_service 模式。本计划把**其余 4 个非实盘 service** 一次性做完：stock_pool / formula / strategy / system。**live_service 不在本计划范围**（实盘在跑，风险高，单独窗口 + 真机回归）。

## 1. 背景与目标

### 1.1 上块成果与可复用结论

0012 已把 `backtest.py` 874 行降到 113 行，业务逻辑下沉到 `backtest_service.py`，跑通「路由薄 / service 厚」模式。§4 记录的关键经验：

- **re-export 兼容只对「读符号」测试成立**；patch 模块函数的测试须把 setattr 目标改到 service 模块；patch 类方法（TQData/TQFormula）则 re-export 即够。
- **边界判据**：404/400/409 HTTP 语义 + 并发锁留路由；纯校验函数（`_validate_*`）留路由；资源存在性校验（`db.get`/`db.query` 判 None）留路由；其余业务逻辑迁 service。
- **service 返回纯 dict/list**，路由用 `ok()`/`err()` 包装（service 不依赖 `core.api.response`）。

### 1.2 本块范围：4 个 service 一次做完

| 路由文件 | 行数 | 迁出 service | 备注 |
|---|---|---|---|
| `stock_pools.py` | 162 | `stock_pool_service.py` | 含通达信对接（`TQData`/`TDXConnectionError`） |
| `formulas.py` | 137 | `formula_service.py` | 纯 DB CRUD + 信号全量替换 |
| `strategies.py` | 392 | `strategy_service.py` | 最重：组合 CRUD + 子策略 CRUD + 两步 commit + 主从校验 |
| `system.py` | 18 | `system_service.py` | 最轻：只是 `load_config`/`save_config` 包装 |
| `live.py` | 484 | **不做** | 实盘在跑，单独立项 + 真机回归 |

### 1.3 为何 4 个一次做（而非逐个立项）

0012 单独做 backtest 是为「跑通模式 + 零行为变更 + 风险隔离」。模式已验证后，这 4 个**结构同构**（都是 CRUD 路由，无并发锁、无主链路编排、无 monkeypatch 模块函数的测试），且**远比 backtest 简单**（无 _run_backtest_locked 那种主链路）。逐个立项 = 4 份计划书 + 4 轮验收，组织成本高于实际收益。一次性做完，统一验收。

### 1.4 不在本计划范围

- **`live_service.py`** —— live.py 484 行，实盘在跑，B6 单 session 守卫 / SSE / 熔断 / TQ 回调线程，风险最高。单独窗口 + 真机回归，不在本计划。
- **删 backtest.py re-export** —— 0012 §8 说待 6 个 service 全部就位后再统一评估。本计划做完仍有 live 未做，re-export 暂留。
- **改测试 import 路径** —— 凡测试 patch 模块函数的（backtest 已处理），本计划的 4 个测试**不 patch 路由/service 模块函数**（见 §3.2），故零改动。
- **任何行为变更** —— 纯重构，HTTP 响应、DB 写入、校验逻辑一字不变。

## 2. 现状基线

### 2.1 测试引用约束（关键，决定改动量）

全仓 grep `import core.api.(stock_pools|formulas|strategies|system)`：

| 测试文件 | import 语句 | 实际用到的符号 | patch 模块函数？ |
|---|---|---|---|
| `test_stock_pool_api.py` | `import core.api.stock_pools as sp_api` | **零引用**（grep `sp_api` 仅 import 行命中，死导入） | 否——patch 的是 `core.tq.data.get_tq`（另一模块） |
| `test_formula_api.py` | 无路由 import | — | 否——纯 TestClient + DB seed |
| `test_portfolio_api.py` | 无路由 import | — | 否——纯 TestClient + DB seed |
| `test_backtest_data.py:557` | `from core.api.strategies import VALID_PERIODS` | `VALID_PERIODS`（对齐检查，只读） | 否——只读常量 |
| `test_main.py` | 无 | — | 否——只打 `/health` |

**结论：4 个测试都不 patch 路由/service 的模块函数**。stock_pools 测试 patch 的是 `core.tq.data.get_tq`（通达信 SDK 入口），与路由/service 层无关——迁到 service 后，service 内部调 `TQData()` 走的也是 `core.tq.data.get_tq`，patch 仍生效。

> 对比 0012：backtest 测试 `monkeypatch.setattr(bt_api, "build_klines", ...)` patch 的是**路由模块的模块函数**，迁 service 后 patch 失效，须改 setattr 目标。本计划的 4 个**无此问题**——这是本块比 backtest 简单的根本原因。

### 2.2 唯一外部依赖：`VALID_PERIODS`

`test_backtest_data.py:557` `from core.api.strategies import VALID_PERIODS` 做白名单对齐检查（`_MINUTE_PERIODS ⊆ VALID_PERIODS`）。

**处理**：`VALID_PERIODS` / `VALID_ROLES` / `VALID_TRADING_SESSIONS` / `VALID_STATUSES` 是**校验常量**，与 `_validate_portfolio`/`_validate_strategy` 同属 HTTP 入口校验语义，**留路由**（strategies.py）。`test_backtest_data.py` 零改动。

### 2.3 跨模块生产引用

grep `from core.api.(stock_pools|formulas|strategies|system) import`：仅 `core/main.py` import `router`（7 个路由注册）。**无任何生产代码 import 这 4 个路由的业务函数**。故迁 service 后除 main.py（只认 router，不动）外无其他调用者受影响。

### 2.4 测试基线

- 后端：432 passed（0012 阶段 4 后：424 原有 + 8 backtest_service 单测）
- `test_stock_pool_api.py`：14 测试（patch `core.tq.data.get_tq`）
- `test_formula_api.py`：16 测试（纯 TestClient）
- `test_portfolio_api.py`：~30 测试（纯 TestClient，含主从校验/两步 commit）
- `test_main.py`：1 测试（/health）

## 3. 设计：4 个 service 边界

### 3.1 通用边界判据（沿用 0012 §3.3）

| 留路由 | 迁 service |
|---|---|
| pydantic 请求模型（`*Create`/`*Item`/`SyncReq`） | 序列化函数（`_serialize_*`） |
| `VALID_*` 校验常量集合 | DB 查询/写入逻辑 |
| `_validate_*` 纯校验函数（返回错误消息或 None） | upsert / 全量替换子表 / 两步 commit |
| `db.query(...).first()` 判 None → 404 | `_apply_*_fields`（字段赋值） |
| `IntegrityError` → 409（HTTP 语义，依赖 ondelete=RESTRICT） | 通达信对接（`TQData`/`get_pool_stocks`） |
| `ok()`/`err()` 响应包装 | |

**注意 IntegrityError → 409 的归属**：`try: db.delete(); db.commit() except IntegrityError: db.rollback(); return err(409, ...)` 是 HTTP 语义（把 DB 约束冲突翻译成可读 HTTP 错误），**留路由**。service 只暴露 `delete_xxx(db, id) -> bool`（False=不存在），路由判 False→404；路由自己 try delete 捕 IntegrityError→409。或更干净：service 暴露 `delete_xxx(db, id)` 抛 `IntegrityError`，路由 catch 转 409。**采用后者**——service 不吞异常，只做「删存在/删不存在」两件事，409 翻译留路由（与 0012 backtest 的 404 翻译同构）。

### 3.2 各 service 签名

**`stock_pool_service.py`**：
```python
from core.tq.data import TQData
from core.tq.utils import TDXConnectionError

def serialize_pool(db, p) -> dict: ...                    # 原 _serialize_pool
def list_local_pools(db) -> list[dict]: ...
def list_tdx_pools(db) -> list[dict]: ...                 # 抛 TDXConnectionError（路由 catch→500）
def list_tdx_stocks(code) -> list[dict]: ...              # 板块不存在抛 LookupError（路由→404），TDX 不可达抛 TDXConnectionError（→500）
def sync_pool(db, code) -> dict: ...                       # 板块不存在抛 LookupError（→404），TDX 不可达抛 TDXConnectionError（→500）
def delete_pool(db, pool_id) -> bool: ...                  # False=不存在；被引用抛 IntegrityError（路由→409）
```

**`formula_service.py`**：
```python
def serialize_formula(db, f) -> dict: ...                 # 原 _serialize_formula
def list_formulas(db) -> list[dict]: ...
def get_formula(db, formula_id) -> dict | None: ...       # None=不存在
def create_formula(db, req) -> dict: ...                  # req 是 pydantic 模型（路由传入）
def update_formula(db, formula_id, req) -> dict | None: ...  # None=不存在
def delete_formula(db, formula_id) -> bool: ...           # False=不存在；被引用抛 IntegrityError（→409）
```

**`strategy_service.py`**（最重）：
```python
def serialize_strategy(s) -> dict: ...                    # 原 _serialize_strategy
def serialize_portfolio(db, p) -> dict: ...               # 原 _serialize_portfolio
def list_portfolios(db) -> list[dict]: ...
def get_portfolio(db, pid) -> dict | None: ...
def create_portfolio(db, req) -> dict: ...
def update_portfolio(db, pid, req) -> dict | None: ...
def delete_portfolio(db, pid) -> bool: ...                # 被回测/实盘引用抛 IntegrityError（→409）
def list_strategies(db, pid) -> list[dict] | None: ...    # None=组合不存在
def create_strategy(db, pid, req) -> dict | None: ...     # None=组合不存在
def update_strategy(db, pid, sid, req) -> dict | None: ...  # None=子策略不存在
def delete_strategy(db, pid, sid) -> bool: ...            # False=不存在；master 被 slave 引用→由路由 check 返回 400；被交易记录引用抛 IntegrityError（→400）
# 私有：_apply_portfolio_fields / _create_strategies_two_step / _apply_strategy_fields
```

> **`_validate_portfolio`/`_validate_strategy` 留路由**（纯校验，返回错误消息，HTTP 400 语义）。但它们内部查 `StockPool`/`Formula` 存在性——这是「校验依赖 DB 查询」的灰色地带。0012 把 `db.get(PortfolioStrategy)` 资源校验留路由。此处同理：`_validate_*` 留路由，内部 `db.query(StockPool/Formula)` 也留路由（校验的一部分）。service 不承担校验，只接已校验合法的 `req`。

**`system_service.py`**（最轻）：
```python
from core.config import load_config, save_config  # re-export？无需——service 直接用

def get_config() -> dict: ...        # load_config 包装
def update_config(data: dict) -> None: ...  # save_config 包装
```

### 3.3 路由层提取后形态（4 个路由统一）

每个路由文件只剩：
1. `router = APIRouter(...)` + pydantic 请求模型
2. `VALID_*` 常量（strategies 有，其余无）
3. `_validate_*` 纯校验函数（formulas/strategies 有）
4. 路由函数：`db.query().first()` 判 None→404 / `_validate_*`→400 / try delete 捕 IntegrityError→409 / 调 service / `ok()`/`err()` 包装
5. **无 re-export 块**（本计划 4 个测试不 patch 路由/service 模块函数，§2.1 已证）

## 4. 兼容策略：无需 re-export

与 0012 不同，本计划**不建 re-export 块**。依据 §2.1：

- 4 个测试都不 patch 路由/service 模块函数（stock_pools patch 的是 `core.tq.data.get_tq`，formula/portfolio 不 patch 任何东西）。
- 唯一外部符号引用 `VALID_PERIODS` 留路由，零改动。
- `sp_api` 死导入（test_stock_pool_api.py:19）只要 `core.api.stock_pools` 模块存在就成立，与内容无关。

**测试零改动全绿**是本计划的验收基线（与 0012 backtest 不同——backtest 改了 patch 目标）。

## 5. 实施阶段

4 个 service 同构且独立，**一次性迁完再统一验收**（不逐个 stage commit，避免 4×3 个小提交）。按依赖顺序：system（最轻，无依赖）→ formula（纯 DB）→ stock_pool（含 TQ）→ strategy（最重，依赖 formula/stock_pool 概念但无代码依赖）。

### 阶段 1 — 建 4 个空 service + 通 import 链

- 新建 `main/core/services/stock_pool_service.py` / `formula_service.py` / `strategy_service.py` / `system_service.py`（各空文件 + 模块 docstring）
- `main/core/services/__init__.py` 保持空
- 跑 `uv run pytest -q` 确认 432 绿（此时 service 空，路由未动）
- **验收**：import 链通，零业务迁移

### 阶段 2 — 迁业务逻辑（4 个并行，但串行提交）

逐个迁：把 §2.1 表中各路由的「序列化 + DB 查询/写入 + 通达信对接 + 字段赋值 + 两步 commit」平移到 service，路由改调 service。

- **system.py**：`get_config`/`update_config` 调 `system_service`。路由 ~18→~15 行
- **formulas.py**：迁 `_serialize_formula` + 5 个路由的 DB 逻辑。`_validate_signals` + `VALID_SIGNAL_TYPES`/`VALID_TRIGGER_VALUES` 留路由。路由 137→~60 行
- **stock_pools.py**：迁 `_serialize_pool` + 5 个路由的 DB/TQ 逻辑。路由 162→~70 行
- **strategies.py**：迁 `_serialize_strategy`/`_serialize_portfolio`/`_apply_*`/`_create_strategies_two_step` + 8 个路由的 DB 逻辑。`VALID_*` + `_validate_portfolio`/`_validate_strategy` 留路由。路由 392→~150 行

每迁完一个跑对应集成测试文件确认绿，再迁下一个。

### 阶段 3 — 统一验收

- 跑 `uv run pytest -q` 确认 **432 全绿**（零新增、零失败、零改动测试）
- `npm run build`（主检出，vue-tsc 校验前端类型——API 契约零变更，预期通过；0012 已验证主检出 build 可跑）
- git diff 复核：4 个路由文件纯减行 + 4 个 service 文件新增，函数体字节级平移
- **验收**：432 全绿 + 前端 build 通过 + 4 个测试文件零改动

## 6. 风险与回退

| 风险 | 缓解 |
|---|---|
| service 内重新 import TQData 到局部 → stock_pools 测试 patch `core.tq.data.get_tq` 失效 | service 顶部 `from core.tq.data import TQData`，不在函数内 import；`TQData()` 构造时内部调 `get_tq()`，patch 全局 `get_tq` 对所有引用生效（与 0012 类方法 patch 同理） |
| `VALID_PERIODS` 误迁 service → test_backtest_data ImportError | §2.2 已定：VALID_* 留路由；阶段 2 迁 strategies 时显式跳过 |
| IntegrityError 归属错（service 吞了 409 语义） | §3.1 已定：service 不吞异常，`delete_*` 抛 IntegrityError，路由 catch 转 409/400 |
| 两步 commit 平移漏 flush → master_strategy_id 写不进 | 字节级平移 `_create_strategies_two_step`，保留 `db.flush()` 拿 id 再 UPDATE |
| 循环 import（service ↔ 路由） | service 不 import `core.api.*`；单向依赖 `路由 → service` |

**回退**：阶段 2 每迁完一个独立提交，任一集成测试不绿即 `git revert` 该提交。4 个 service 互不依赖，可独立回退。

## 7. 验收清单（全部通过 = 计划完成）

- [ ] `main/core/services/{stock_pool,formula,strategy,system}_service.py` 4 个文件存在，各承接对应路由全部业务逻辑
- [ ] 4 个路由文件仅剩路由壳（system ~15 / formulas ~60 / stock_pools ~70 / strategies ~150 行）
- [ ] `uv run pytest -q` **432 全绿**（零新增、零失败）
- [ ] `npm run build` 通过（主检出）
- [ ] 4 个测试文件（test_stock_pool_api / test_formula_api / test_portfolio_api / test_backtest_data）**零改动**通过
- [ ] `VALID_PERIODS` 仍在 `core.api.strategies`（test_backtest_data:557 零改动）
- [ ] 4 个路由 HTTP 行为不变：200/400/404/409 + 响应字段 + IntegrityError 翻译
- [ ] [project-issues-audit-recheck.md](../project-issues-audit-recheck.md) P1 #9 标注更新为「5/6 service 已落地（stock_pool/formula/strategy/system/backtest），仅 live_service 待立项」

## 8. 后续（不在本计划）

- **0014 live_service**：live.py 484 行，实盘在跑。B6 单 session 守卫 + SSE 五类事件 + E5/E6 熔断 + TQ 回调线程 + /deals 回填。风险最高，单独窗口 + 真机回归。做完后 P1 #9 全部关闭。
- live_service 就位后：评估删除 backtest.py re-export（0012 §8 遗留）、统一测试 import 到 `core.services.*`。
