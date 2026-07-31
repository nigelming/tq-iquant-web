# 股票池前端完善 — 同步通达信 + 查看股票清单 + 删除

## Context

公式管理 CRUD 已完成（前端首个完整管理功能）。用户要求"将股票池、公式管理前端功能先完善"——公式管理已就绪，现完善**股票池**。股票池是组合策略的前置依赖（组合策略 `stock_pool_id` 指向股票池），与公式管理并列为两大基础配置。

**当前股票池现状**（前端只读骨架 + 后端半实现）：

- 前端 [StockPools.vue](web/src/views/StockPools.vue) 只读列表（id/name/synced_at），无新建/同步/查看股票/删除
- 后端 [stock_pools.py](main/core/api/stock_pools.py) 只有 `GET ""`（list）+ `GET "/{id}"`（详情只返 id/name）+ `GET "/tdx"`（桩 `[]`），**缺 sync / stocks 清单 / delete / 列表 stock_count**
- 后端 TQ 模块 [tq/data.py](main/core/tq/data.py) **已具备**通达信读取能力：`get_stock_pools()` → `[{"name": 板块名}]`，`get_pool_stocks(name)` → `[{"stock_code": "000001.SZ"}]`（真实可跑，非桩）
- 模型 [StockPool](main/core/models/stock_pool.py)（id/name/synced_at/created_at）+ [StockPoolStock](main/core/models/stock_pool_stock.py)（id/pool_id→CASCADE/stock_code/stock_name，UniqueConstraint(pool_id,stock_code)）已完备，**不改模型**

**业务规则**（来自 [docs/system-plan-draft.md:998-1015](docs/system-plan-draft.md#L998)）：
- `GET /api/stock-pools` 列表每条含 `stock_count`
- `GET /api/stock-pools/tdx` 取通达信可用板块（未同步的）
- `POST /api/stock-pools/{id}/sync` 全量替换股票清单（从通达信拉取覆盖）
- `GET /api/stock-pools/{id}/stocks` 股票清单
- 股票代码统一带后缀（`000001.SZ`，[shared/tq_iquant_shared/stock_utils.py](shared/tq_iquant_shared/stock_utils.py) 校验 `^\d{6}\.(SZ|SH)$`）
- 股票池以通达信同步为准（用户已选"完整:同步+查看+删除"，**不做手动增删单只股票**）

## 实现范围（全程 TDD）

### 第 1 步：后端股票池接口补全（TDD，pytest）

扩展 [main/core/api/stock_pools.py](main/core/api/stock_pools.py)，复用 [formulas.py](main/core/api/formulas.py) 模式：内联 dict 序列化 + `{"code":0,"data":...}` 信封；404 → `{"code":404,"message":"股票池不存在"}`（HTTP 200）。模型无 relationship，股票清单用显式二次查询。

**接口清单**（最终态）：

| 接口 | 方法 | 数据 | 状态 |
|---|---|---|---|
| `/api/stock-pools` | GET | 列表，每条含 `stock_count` | **改**（加 stock_count） |
| `/api/stock-pools/tdx` | GET | 通达信可用板块 `[{"name":...}]`，排除已同步的 | **改**（接 TQ，非桩） |
| `/api/stock-pools` | POST | 新建空股票池 `{name}` → 返回 id | **新增** |
| `/api/stock-pools/{id}` | GET | 详情（id/name/synced_at/stock_count） | **改**（补全字段） |
| `/api/stock-pools/{id}/stocks` | GET | 股票清单 `[{id,stock_code,stock_name}]` | **新增** |
| `/api/stock-pools/{id}/sync` | POST | 从通达信全量替换股票清单，更新 synced_at | **新增** |
| `/api/stock-pools/{id}` | DELETE | 删股票池（StockPoolStock 随 CASCADE 删） | **新增** |

**关键设计**：
- **sync 全量替换**：先 `db.query(StockPoolStock).filter(pool_id=id).delete()`，再循环 `db.add(StockPoolStock(...))`，最后 `pool.synced_at = func.now()`。调 `TQData().get_pool_stocks(pool.name)` 取通达信股票清单。
- **tdx 列表排除已同步**：`get_stock_pools()` 返回全部板块，减去 DB 中已有的 `StockPool.name`，返回 `[{"name": n}]`。让前端"选未同步板块新建+同步"。
- **新建流程**：POST `{name}` 建空 StockPool（0 股票，synced_at=None）→ 前端再调 sync 拉取股票。或者前端先调 tdx 列表选板块，POST 建池，再 sync。两种都支持。
- **Decimal/时间序列化**：synced_at 是 DateTime，FastAPI 默认 ISO 字符串序列化（无需 jsonable_encoder，沿用 formulas.py 内联 dict 方式，datetime 直接放 dict 由 FastAPI 序列化）。
- **stock_count**：list 接口对每个 pool 用 `db.query(StockPoolStock).filter_by(pool_id=p.id).count()`。

**Pydantic 请求体**：
```python
class StockPoolCreate(BaseModel):
    name: str
```

**TDD 顺序**（每接口 RED → GREEN → 全量回归）：
1. `test_list_pools_with_stock_count` — seed pool+3 stocks → list 含 stock_count=3
2. 改 `list_pools` 加 stock_count
3. `test_get_pool_detail` / `test_get_pool_not_found`
4. 改 `get_pool` 补全字段
5. `test_create_pool` — POST {name} → DB 有 StockPool，0 股票
6. 实现 `create_pool`
7. `test_get_pool_stocks` — seed 后 GET /{id}/stocks 返回清单
8. 实现 `get_pool_stocks`（路由名避开与 TQData.get_pool_stocks 混淆，用 `list_pool_stocks`）
9. `test_sync_pool_replaces_stocks` — monkeypatch TQData.get_pool_stocks 返回 2 股票 → POST sync → 旧股票清空、新 2 条落库、synced_at 非 None
10. `test_sync_pool_not_found`
11. 实现 `sync_pool`
12. `test_delete_pool_removes_stocks` — DELETE → pool + stocks 都没了（CASCADE，fixture 需 PRAGMA foreign_keys=ON，同 test_formula_api.py）
13. `test_delete_pool_not_found`
14. 实现 `delete_pool`
15. `test_tdx_pools_excludes_synced` — monkeypatch TQData.get_stock_pools 返回 [A,B,C]，DB 已有 A → tdx 列表返 [B,C]
16. 改 `list_tdx_pools` 接 TQ + 排除已同步

**测试文件**：新建 [main/core/tests/integration/test_stock_pool_api.py](main/core/tests/integration/test_stock_pool_api.py)，复用 test_formula_api.py 的 `client` fixture（StaticPool + PRAGMA foreign_keys=ON + 函数键 get_db 覆盖 + yield (c, Session)）。TQ 调用用 monkeypatch（不依赖通达信进程）。

### 第 2 步：前端 API 客户端扩展（[web/src/api/index.ts](web/src/api/index.ts)）

`getStockPools()` 已存在。新增：
```ts
export async function getTdxPools() { ... }                       // GET /stock-pools/tdx
export async function createStockPool(req: { name: string }) { ... }  // POST /stock-pools
export async function getPoolStocks(id: number) { ... }           // GET /stock-pools/{id}/stocks
export async function syncStockPool(id: number) { ... }           // POST /stock-pools/{id}/sync
export async function deleteStockPool(id: number) { ... }         // DELETE /stock-pools/{id}
```

### 第 3 步：前端 StockPools.vue 重写（TDD，vitest）

重写 [web/src/views/StockPools.vue](web/src/views/StockPools.vue) 为完整管理视图。**侧边栏/路由/title 已存在**（`/stock-pools` 已注册，titles 已含"股票池"），无需改 App.vue/router。

**页面布局**：
```
┌─ 操作栏 ─────────────────────[ + 新建股票池 ]─────────┐
└──────────────────────────────────────────────────────┘
┌─ 股票池列表 ─────────────────────────────────────────┐
│ #1  涨停池  50 只股票  2026-07-28  [查看][同步][删除] │
│ #2  龙头股  12 只股票  -          [查看][同步][删除] │
│             （暂无股票池 ← empty-state）              │
└──────────────────────────────────────────────────────┘

点[+新建股票池] → Modal：
┌─ 新建股票池 ─────────────────────────┐
│ 从通达信板块选择：                    │
│  ( ) 涨停板  ( ) 龙头股  ( ) 自选股  │  ← tdx 列表单选
│  或手动输入名称 [           ]         │
│                       [确认] [取消]   │
└──────────────────────────────────────┘
新建后自动调 sync 拉取股票清单。

点[查看] → 股票清单 Modal（或展开行）：
┌─ 涨停池 股票清单 (50) ───────────────┐
│ 000001.SZ  平安银行                   │
│ 000002.SZ  万科A                      │
│ ...                                   │
│                       [关闭]          │
└──────────────────────────────────────┘

点[同步] → 二次确认 → POST sync → 刷新列表
点[删除] → 二次确认 → DELETE → 刷新列表
```

- 新建 Modal：调 `getTdxPools()` 拉未同步板块单选；也允许手输名称。确认调 `createStockPool({name})` → 再 `syncStockPool(id)`。
- 查看股票清单：调 `getPoolStocks(id)`，弹 Modal 展示表格。
- 同步/删除：confirm 二次确认（同 Formulas.vue 删除模式）。
- 沿用现有 CSS 类（.card/.table-wrap/.btn/.modal-overlay/.modal-content/.empty-state/.badge），可能加 `.modal-lg`（股票清单 Modal 略宽，已存在）。

**前端 TDD**（[web/src/__tests__/StockPools.test.ts](web/src/__tests__/StockPools.test.ts)）：
- `vi.mock('../api', ...)` stub `getStockPools`/`getTdxPools`/`createStockPool`/`getPoolStocks`/`syncStockPool`/`deleteStockPool`
- RED 用例：挂载渲染 mock 池行（显股票数）/ 点新建弹 Modal 显 tdx 板块 / 提交新建调 createStockPool+syncStockPool / 点查看弹股票清单 / 点删除调 deleteStockPool
- GREEN：实现 StockPools.vue 至测试通过
- 删除/同步用例需 `vi.stubGlobal('confirm', () => true)`（happy-dom confirm 返回 undefined，同 Formulas.test.ts）

### 第 4 步：端到端联调

`./manage.ps1 start`（或后台启 uvicorn）→ `/stock-pools` → 新建（选通达信板块）→ 同步 → 查看股票清单 → 删除。验证 DB 落库（StockPool + StockPoolStock + synced_at）。**注意**：sync 依赖通达信进程在跑（`tdx_backtest_path` 配置），若未启动则 sync 返回空清单——联调时需确保通达信已启动（同 P1 回测联调前提）。

## 关键文件

**后端**：
- 改 [main/core/api/stock_pools.py](main/core/api/stock_pools.py) — 补全 7 个接口
- 新建 [main/core/tests/integration/test_stock_pool_api.py](main/core/tests/integration/test_stock_pool_api.py) — TDD 测试
- 复用 [main/core/tq/data.py](main/core/tq/data.py) — `TQData.get_stock_pools` / `get_pool_stocks`（已实现，不改）

**前端**：
- 改 [web/src/views/StockPools.vue](web/src/views/StockPools.vue) — 重写为完整管理视图
- 改 [web/src/api/index.ts](web/src/api/index.ts) — 加 5 个函数
- 新建 [web/src/__tests__/StockPools.test.ts](web/src/__tests__/StockPools.test.ts) — 前端 TDD 测试
- 不改 App.vue/router（stock-pools 路由+导航已存在）

**不改**：模型（StockPool/StockPoolStock 已完备）、TQ 模块、其他视图。

## 验证

1. 后端：`uv run pytest`（在 `main/`）— 现有 72 + 新增约 12 = 全绿
2. 前端：`npm test`（在 `web/`）— 现有 6 + 新增约 5 = 全绿
3. 构建：`npm run build` 通过
4. 端到端：启通达信 → `./manage.ps1 start` → `/stock-pools` → 新建/同步/查看/删除全流程通

## 不做（明确排除）

- 手动增删单只股票（股票池以通达信同步为准，用户已确认范围）
- 组合策略前端（Portfolios.vue）— 依赖股票池+公式先就绪，属后续切片
- 回测前端闭环 — 依赖组合策略，再后续
- 股票名 stock_name 的实时补全（通达信 get_pool_stocks 只返 stock_code，stock_name 暂留空，后续可接行情补全）
