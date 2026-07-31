# 股票池 v2 — 直读通达信用户板块 + 同步落库（修 SDK 格式 bug）

## Context

v1（[0004-stock-pool-management.md](0004-stock-pool-management.md)）已完成但线上炸了：`GET /api/stock-pools/tdx` 报 `TypeError: unhashable type: 'dict'`。

**根因**：v1 对通达信 SDK 返回格式做了**未经验证的假设**。`main/core/tq/data.py` 的 `_get_pools` 把 SDK 返回的 dict 当字符串塞进 `{"name": s}`；`stock_pools.py:40` 对 dict 求哈希炸。集成测试 monkeypatch 替换的是 `TQData.get_stock_pools`（高层包装），喂 `[{"name": str}]`，**从没覆盖 `_get_pools` 对 SDK 原始返回的解析** → 测试全绿却线上炸。教训：假设 SDK 格式必须实证，测试必须打 SDK 层。

**SDK 真实格式（已用探查脚本 100% 实证，非假设）**：
- 用户自定义板块：`tq.get_user_sector()`（无参数）→ `list[dict]`，字段 `Code`/`Name`（大写首字母）。例 `[{'Code':'TQCS','Name':'tq自选'}, {'Code':'DEGP','Name':'第二股票'}, {'Code':'ETF','Name':'etf'}]`
- 成分股：`tq.get_stock_list_in_sector(block_code, block_type=1, list_type=1)` → `list[dict]`，字段 `Code`/`Name`。例 `[{'Code':'600000.SH','Name':'浦发银行'}]`。**必须传板块 `Code`（如 `TQCS`），传 `Name` 返回空**。`block_type=1, list_type=1` 是参考项目 `D:\project\tdx\tdx-tq-pyqt` 的固定写法
- `get_sector_list(list_type=1)` 返回 587 个系统板块（轮动趋势等），**不是用户要的，弃用**

**用户新需求**：「股票池的管理，直接获取通达信，分为获取列表和每个股票池的股票详情，只存在同步的问题」+「现在只需要读出通达信用户定义的股票池」：
- 股票池 = 通达信用户自定义板块（`get_user_sector()`）
- **列表**：直读通达信（实时）
- **详情（成分股）**：直读通达信（实时）
- **同步**：落库（唯一写本地 DB 的操作）
- 不做"新建空池/手动建池"

**架构约束（核实过）**：
- 回测引擎 [backtest.py:276](main/core/api/backtest.py#L276) 用 `StockPoolStock` 表（本地成分股快照）取回测股票，**不直读通达信** → 同步落库是必需的（回测要历史数据且不能依赖通达信在线）
- 组合策略 `PortfolioStrategy.stock_pool_id → stock_pools.id (ondelete=RESTRICT)` 引用本地已同步池
- `StockPool` 模型只有 `name`，没存板块 `Code` → 同步成分股必须用 Code，只存 name 若板块改名就找不到 → **需加 `code` 列**
- `StockPoolStock` 已有 `stock_name` 列（可空），成分股 `list_type=1` 返回 `Name` 可填上

## 关键文件

- 改 [main/core/tq/data.py](main/core/tq/data.py) — `_get_pools` 用 `get_user_sector()`，`_get_stocks` 传板块 Code + 取 `Code`/`Name`
- 改 [main/core/models/stock_pool.py](main/core/models/stock_pool.py) — 加 `code` 列
- 新建 Alembic 迁移 — `stock_pools` 加 `code` 列
- 改 [main/core/api/stock_pools.py](main/core/api/stock_pools.py) — 接口改为直读通达信模型
- 重写 [main/core/tests/integration/test_stock_pool_api.py](main/core/tests/integration/test_stock_pool_api.py) — monkeypatch 打 SDK 层
- 改 [web/src/views/StockPools.vue](web/src/views/StockPools.vue) — 主列表=通达信板块，去掉新建 Modal
- 改 [web/src/api/index.ts](web/src/api/index.ts) — 函数签名调整
- 改 [web/src/__tests__/StockPools.test.ts](web/src/__tests__/StockPools.test.ts) — 适配新交互

## 实现范围（全程 TDD）

### 第 1 步：TQ 数据层修复（TDD，先复现 bug）

改 [data.py](main/core/tq/data.py) `_get_pools`/`_get_stocks`，归一化输出。

**归一化输出格式**（小写键，对外稳定；与参考项目 `tdx-tq-pyqt` 一致）：
- `_get_pools()` → `[{"code": 板块Code, "name": 板块Name}]`
- `_get_stocks(code)` → `[{"stock_code": 股票Code, "stock_name": 股票Name}]`（股票 Code 已带 `.SH`/`.SZ` 后缀）

**`get_stock_pools()` / `get_pool_stocks()` 签名调整**：`get_pool_stocks` 入参从 `pool_name` 改为 `pool_code`（语义对齐 SDK）。保留 `get_tdx_lock()` 线程锁，不改 `get_tq()` 单例。

**冗余方法清理**：`get_sectors`/`get_stocks_in_sector`（data.py:47-53）标注混乱（`List[dict]`/`List[str]` 但实际透传 SDK dict），且无人调用——删除，避免再有人误用。

**TDD 顺序**：
1. 新建 `main/core/tests/unit/test_tq_data.py`（单元测试，不打通达信）
2. RED：`test_get_pools_parses_user_sector` — monkeypatch `core.tq.data.get_tq` 返回 fake tq，其 `get_user_sector()` 返回 `[{'Code':'TQCS','Name':'tq自选'}]` → 断言 `TQData().get_stock_pools()` 返回 `[{'code':'TQCS','name':'tq自选'}]`。当前代码用 `get_sector_list` 且把 dict 当 str → fail（TypeError 或断言不符）
3. GREEN：改 `_get_pools` 用 `get_user_sector()` + `{"code": s["Code"], "name": s["Name"]}`
4. RED：`test_get_stocks_parses_sector_stocks` — fake tq `get_stock_list_in_sector('TQCS', block_type=1, list_type=1)` 返回 `[{'Code':'600000.SH','Name':'浦发银行'}]` → 断言 `TQData().get_pool_stocks('TQCS')` 返回 `[{'stock_code':'600000.SH','stock_name':'浦发银行'}]`，并断言调用参数 `block_type=1, list_type=1`
5. GREEN：改 `_get_stocks` 传 code + block_type=1, list_type=1 + 取 `Code`/`Name`
6. RED：`test_get_stocks_empty` — fake 返回 `[]` → 返回 `[]`（不报错）
7. 全量回归

### 第 2 步：StockPool 加 code 列 + 迁移

改 [stock_pool.py](main/core/models/stock_pool.py)：
```python
code = Column(String(50), nullable=False)  # 通达信板块 Code，同步时落库
```
不加 `unique=True`（同一板块 Code 可能被删了再同步，历史 id 不同；唯一性靠业务逻辑保证：同步时先按 code 查，有则更新无则新建）。

**迁移**：`uv run alembic revision --autogenerate -m "add code to stock_pools"`。`nullable=False`。**dev.db 可清空**（用户已确认现有 stock_pools 数据无需保留）→ 迁移直接加 NOT NULL code 列，联调前清空 stock_pools + stock_pool_stocks 表（或重建 dev.db），无需回填。若生产环境有数据另写回填逻辑，当前单用户开发库不涉及。

### 第 3 步：后端接口改为直读通达信模型（TDD）

**接口最终态**：

| 接口 | 方法 | 数据 | 状态 |
|---|---|---|---|
| `/api/stock-pools/tdx` | GET | 全部用户板块 ∪ 本地已同步残留 `[{code,name,synced,exists_in_tdx,stock_count}]`。`synced` 标记本地是否已同步；`exists_in_tdx` 标记通达信是否还有此板块（false=本地残留，通达信已删）；已同步的带 `stock_count` | **改**（直读通达信 + 合并本地残留） |
| `/api/stock-pools/tdx/{code}/stocks` | GET | 通达信成分股 `[{stock_code,stock_name}]`（实时）；板块在通达信已删 → `{"code":404,"message":"板块不存在"}` | **新增** |
| `/api/stock-pools/sync` | POST | `{code}` → upsert 本地 StockPool（by code）+ 全量替换成分股 + `synced_at` → 返回 `{id,code,name,synced_at,stock_count}`。**已同步的也可再次同步**（手动保持两边一致） | **改**（按 code upsert，支持重同步） |
| `/api/stock-pools` | GET | 本地已同步池 `[{id,code,name,synced_at,stock_count}]`（供组合策略引用） | **改**（加 code 字段） |
| `/api/stock-pools/{id}` | DELETE | 删本地记录（成分股随 CASCADE 删） | **保留** |
| `/api/stock-pools/{id}/stocks` | GET | — | **删**（详情走 tdx 实时；回测引擎用 ORM 直查不需此接口） |
| `/api/stock-pools` (POST 新建空池) | POST | — | **删**（不做手动建池） |
| `/api/stock-pools/{id}` (GET 详情) | GET | — | **删**（前端列表已含全部信息） |

**关键设计**：
- **sync upsert**：`POST /sync {code}` → 先 `tq.get_user_sector()` 找到该 code 的 name（避免前端传 name 不一致），`db.query(StockPool).filter_by(code=code).first()` 有则更新 name + 全量替换成分股，无则新建。成分股全量替换：删旧 `db.query(StockPoolStock).filter_by(pool_id).delete()` + 循环 `db.add(StockPoolStock(pool_id, stock_code, stock_name))`。`synced_at = func.now()`
- **列表 synced 标记 + 本地残留合并**：`tdx_pools = TQData().get_stock_pools()`（通达信全部用户板块，可能抛连接异常→见下），`local_pools = db.query(StockPool).all()`。合并算法：
  - 对每个通达信板块 `t`：`synced = t.code in {p.code for p in local_pools}`，`exists_in_tdx=True`，`stock_count` = 本地对应池的成分股数（未同步则 0）
  - 对每个本地池 `p` 其 `code` 不在通达信板块里：作为残留项加入，`synced=True`，`exists_in_tdx=False`，`stock_count` = 本地成分股数，`name` 取本地存的（通达信已删无法取）
  - 返回 `[{code, name, synced, exists_in_tdx, stock_count}]`。前端据此：已同步行显"已同步 N 只"+[查看][同步][删除]；未同步行显[查看][同步]；`exists_in_tdx=False` 的残留行显"通达信已删除"+[删除]（无同步/查看，因通达信已无此板块）
  - **已同步可再次同步**：synced 行的[同步]按钮调 `POST /sync {code}` 重新全量替换成分股，保持两边一致
- **详情实时**：`GET /tdx/{code}/stocks` → `TQData().get_pool_stocks(code)`，若板块在通达信已删（返回空或板块不存在）→ `{"code": 404, "message": "板块不存在"}`（区别于"板块存在但无成分股"的空列表 `{"code":0,"data":[]}`：用 `get_user_sector()` 确认 code 是否存在，不存在才 404）
- **删除**：`DELETE /{id}` 删本地 StockPool，`StockPoolStock` 随 `ondelete=CASCADE` 删。组合策略 `ondelete=RESTRICT` 会阻止删除被引用的池 → 捕获 `IntegrityError` 返回 `{"code": 409, "message": "该股票池被组合策略引用，无法删除"}`（HTTP 200）

**SDK 调用失败处理**：通达信未启动时 `get_tq()` 抛 `TDXConnectionError`（或 `ImportError`）。接口层捕获 → 返回 `{"code": 500, "message": "通达信未启动或连接失败"}`（HTTP 200），不抛 500 异常。前端按 `code != 0` 显示"通达信未连接"。

**Pydantic**：
```python
class SyncReq(BaseModel):
    code: str
```

**TDD 顺序**（每接口 RED → GREEN → 回归，**monkeypatch 打 `core.tq.data.get_tq` 返回 fake tq**，不 patch `TQData` 高层）：
1. `test_tdx_list_returns_user_sectors_with_synced_flag` — fake tq `get_user_sector` 返回 3 板块，DB 已同步 1 个 → 列表返 3 条，对应那条 `synced=True` + `stock_count` 正确，其余 `synced=False, stock_count=0`
2. `test_tdx_list_includes_local_orphans` — fake tq 返回 `[TQCS]`，DB 有 `TQCS` + `DEGP`（DEGP 通达信已删）→ 列表返 2 条，DEGP `exists_in_tdx=False, synced=True`，TQCS `exists_in_tdx=True`
3. `test_tdx_list_tdx_unreachable` — fake `get_tq` 抛 `TDXConnectionError` → `{"code":500, "message":"..."}`
4. `test_tdx_stocks_returns_realtime` — fake tq `get_stock_list_in_sector` 返回成分股 → `GET /tdx/TQCS/stocks` 返回 `[{stock_code,stock_name}]`
5. `test_tdx_stocks_sector_not_in_tdx` — fake tq `get_user_sector` 不含 `NOPE` → `GET /tdx/NOPE/stocks` → `{"code":404,"message":"板块不存在"}`
6. `test_sync_creates_new_pool` — fake tq `get_user_sector` 含 `TQCS` + `get_stock_list_in_sector` 返回 2 股票 → `POST /sync {code:'TQCS'}` → DB 新建 StockPool(code=TQCS) + 2 成分股 + synced_at 非 None，返回 id
7. `test_sync_updates_existing_pool` — 先 seed 本地 StockPool(code=TQCS, 旧成分股 3 只) → sync → name 更新、成分股全量替换为新的 2 只、synced_at 刷新、id 不变（重同步场景）
8. `test_sync_unknown_code` — fake tq `get_user_sector` 不含 `NOPE` → `{"code":404, "message":"板块不存在"}`
9. `test_list_local_pools` — seed 本地池 + 成分股 → `GET /stock-pools` 返回 `[{id,code,name,synced_at,stock_count}]`
10. `test_delete_pool_cascades_stocks` — seed → DELETE → 池 + 成分股都没了
11. `test_delete_pool_referenced_by_strategy` — seed 池 + PortfolioStrategy 引用 → DELETE → `{"code":409}`
12. `test_delete_pool_not_found` — DELETE 9999 → `{"code":404}`
13. 全量回归（含 backtest 测试，确认改 data.py 不破坏回测数据加载）

**fake tq 结构**（测试共用）：
```python
class FakeTq:
    def __init__(self, sectors, stocks_map):
        self._sectors = sectors  # [{"Code","Name"}]
        self._stocks = stocks_map  # {code: [{"Code","Name"}]}
    def get_user_sector(self):
        return self._sectors
    def get_stock_list_in_sector(self, block_code, block_type=0, list_type=0):
        return self._stocks.get(block_code, [])
def _patch_tq(monkeypatch, fake):
    monkeypatch.setattr(sp_api, "get_tq", lambda: fake)  # 或 core.tq.data.get_tq
```

### 第 4 步：前端重写（TDD，vitest）

**StockPools.vue 新布局**：
```
┌─ 操作栏 ──────────────[ 刷新 ]─────────┐  ← 刷新按钮（重拉通达信列表）
└────────────────────────────────────────┘
┌─ 通达信用户板块 ───────────────────────┐
│ tq自选 (TQCS)    已同步 50只  [查看][同步][删除]│  ← synced + exists_in_tdx：可查看/重同步/删
│ 第二股票 (DEGP)   未同步       [查看][同步]    │  ← 未同步：可查看/同步
│ 旧板块 (OLDBLK)   通达信已删除 12只 [删除]    │  ← 本地残留 exists_in_tdx=False：只能删
└────────────────────────────────────────┘
点[查看] → 成分股 Modal（实时调 tdx stocks 接口，仅 exists_in_tdx 行有）
点[同步] → POST /sync {code} → 刷新（未同步首次同步 / 已同步重同步都走此）
点[删除] → confirm → DELETE /{id} → 刷新（仅 synced 行有；残留行也有）
```

**api/index.ts 函数调整**：
```ts
export async function getTdxPools()              // GET /stock-pools/tdx → [{code,name,synced}]
export async function getTdxPoolStocks(code: string)  // GET /stock-pools/tdx/{code}/stocks → [{stock_code,stock_name}]
export async function syncStockPool(req: { code: string })  // POST /stock-pools/sync
export async function getStockPools()            // GET /stock-pools → 本地已同步池 [{id,code,name,synced_at,stock_count}]
export async function deleteStockPool(id: number)  // DELETE /stock-pools/{id}
// 删除 createStockPool、getPoolStocks（旧）
```

**前端 TDD**（[StockPools.test.ts](web/src/__tests__/StockPools.test.ts) 重写）：
- `vi.mock('../api', ...)` stub 5 个新函数
- 用例：挂载渲染通达信板块行（显 synced/未synced + 股票数）/ 点查看弹成分股 Modal / 点同步调 syncStockPool({code}) / 点删除调 deleteStockPool(id) / 通达信不可达时显示提示
- 删除/同步用 `vi.stubGlobal('confirm', () => true)`

### 第 5 步：端到端联调

确保通达信已启动 → `uv run uvicorn core.main:app --reload`（在 `main/`）→ `/stock-pools` 页面 → 查看成分股 → 同步 → 删除 → 验证 DB（StockPool 含 code + StockPoolStock + synced_at）→ 跑回测确认数据加载正常。**注意**：manage.ps1 start 被沙箱挡，需用户本地起；sync 依赖通达信进程在跑。

## 验证

1. 后端：`uv run pytest`（在 `main/`）— 新增约 10 测试 + 现有全绿（含 backtest 回归）
2. 前端：`npx vitest run`（在 `web/`）— 重写约 5 测试全绿 + `npm run build` 通过
3. 迁移：`uv run alembic upgrade head` 成功，`stock_pools` 表有 `code` 列
4. 端到端：通达信在线 → 列表显用户板块 → 查看成分股 → 同步落库 → 删除 → 回测仍正常

## 不做

- 手动新建股票池 / 手动增删单只股票（股票池以通达信为准）
- 取系统板块（`get_sector_list`，587 个行业/概念板块）—— 只要用户自定义板块
- 股票池改名/编辑（通达信那边改，同步即可）
- 组合策略前端（后续切片）
