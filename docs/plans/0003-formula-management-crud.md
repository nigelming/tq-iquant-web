# 公式管理 CRUD — 前端首个完整管理功能

## Context

回测后端已闭环（`POST /api/backtest` 能跑真实回测）。但**公式是回测和组合策略的依赖前置**——策略(`Strategy.formula_id`)依赖公式，公式信号(`FormulaSignal`)决定开仓/加仓/减仓/平仓。当前公式管理**完全缺失**：

- 前端无公式视图（views/ 里无 Formula 相关），侧边栏/路由无公式入口
- 后端 `GET /api/formulas` 是桩 `{"code":0,"data":[]}`，无创建/编辑/删除接口
- 公式只能手动入 DB，无法在界面管理

业务规则（来自 [docs/system-plan-draft.md:195](../system-plan-draft.md#L195)）：**公式运行后输出多个信号值（数量不定），每个信号映射到四种操作类型（OPEN/ADD/REDUCE/CLOSE）之一，并定义触发值为 1 或 -1**。信号执行顺序：公式内 CLOSE > REDUCE > ADD > OPEN（[:270](../system-plan-draft.md#L270)）；CLOSE 一次性全平，REDUCE 用减仓比例（[:258-259](../system-plan-draft.md#L258)）。

目标：实现公式管理完整 CRUD（列表 / 新建 / 编辑 / 删除），每个公式含名称+内容+多个信号配置（signal_name / signal_type∈{OPEN,ADD,REDUCE,CLOSE} / trigger_value∈{1,-1}）。全程按 TDD。这是回测/组合策略前端的前置依赖——公式管起来，后续组合策略才能在界面选公式。

## 总体前端布局（沿用，不改框架）

基于 `web/src/App.vue` + `web/src/style.css` 现状：左右两栏 `.app-layout`（flex 横向满屏高），灰底白卡片风格。

- **左栏 `.sidebar`**（固定 200px，白底右边框）：`.sidebar-header` h1「创懿量化」+ `.sidebar-nav` `.nav-item`（router-link，当前页高亮蓝）。**本次新增「📐 公式管理」导航项** → `/formulas`
- **右栏 `.main-container`**：`.topbar`（显路由标题）+ `.content`（padding 20px，放 `<router-view/>`）
- **页面组织规范**：列表 = `.card.table-wrap` 包 `<table>` + `.empty-state`；弹窗 = `.modal-overlay` + `.modal-content`；按钮 `.btn`/`.btn-primary`/`.btn-sm`/`.btn-danger`；配色 灰底+白卡+蓝 accent+6px 圆角

公式管理页 = `.content` 里「顶部操作栏 + 公式列表卡片 + 新建/编辑 Modal」。**侧边栏/顶栏/配色/卡片样式全部沿用**，仅在 `App.vue` 加 1 个导航项 + `router/index.ts` 加 1 条路由 + `style.css` 可能加 `.modal-lg`（信号多时弹窗略宽）。

## 页面布局（已与用户确认方向：完整 CRUD）

```
┌─ 操作栏 ───────────────────────────────[ + 新建公式 ]─┐
└───────────────────────────────────────────────────────┘
┌─ 公式列表 ────────────────────────────────────────────┐
│ #1  MACROSSPRO  REF(CLOSE,1)...  4 个信号  [编辑][删除] │
│ #2  OPEN_FORMULA  ...           1 个信号  [编辑][删除] │
│             （暂无公式 ← empty-state）                 │
└───────────────────────────────────────────────────────┘

点[+新建公式] / [编辑] → Modal：
┌─ 新建公式 ─────────────────────────────┐
│ 名称    [MACROSSPRO          ]          │
│ 公式内容 [textarea 多行，通达信公式文本] │
│                                         │
│ 信号配置：                               │
│  信号名称[开仓  ] 类型[OPEN ▾] 触发值[ 1]│
│  信号名称[加仓  ] 类型[ADD  ▾] 触发值[ 1]│
│  信号名称[减仓  ] 类型[REDUCE▾] 触发值[ 1]│
│  信号名称[平仓  ] 类型[CLOSE▾] 触发值[ 1]│
│              [ + 添加信号 ]              │
│                                         │
│                       [确认] [取消]      │
└─────────────────────────────────────────┘
```

- 信号类型 select 固定 4 选项：OPEN(开仓)/ADD(加仓)/REDUCE(减仓)/CLOSE(平仓)
- 触发值 select 2 选项：1 / -1（业务规则 trigger_value∈{1,-1}）
- 信号行可动态增删（公式输出信号数量不定）
- 删除二次确认（`.btn-danger` + confirm 或二次点击）

## 实现范围

### 第 1 步：后端公式 CRUD 接口（TDD，pytest）

复用 `main/core/api/stock_pools.py` 模式：内联 dict 序列化 + `{"code":0,"data":...}` 信封；404 返回 `{"code":404,"message":"..."}`（HTTP 200）。模型无 `relationship()`，公式→信号用显式二次查询。文件：`main/core/api/formulas.py`。

接口清单：

| 接口 | 方法 | 数据 |
|---|---|---|
| `/api/formulas` | GET | 公式列表，每条附 `signals:[{id,signal_name,signal_type,trigger_value}]` |
| `/api/formulas/{id}` | GET | 单公式详情 + signals |
| `/api/formulas` | POST | 创建公式 + 其下信号（事务：先建 Formula 再循环建 FormulaSignal） |
| `/api/formulas/{id}` | PUT | 编辑公式 + 信号（信号全量替换：删旧建新，或 diff；先实现全量替换简单可靠） |
| `/api/formulas/{id}` | DELETE | 删公式（FormulaSignal 随 `ondelete=CASCADE` 自动删） |

POST/PUT 请求体（Pydantic）：
```python
class SignalItem(BaseModel):
    signal_name: str
    signal_type: str  # OPEN|ADD|REDUCE|CLOSE
    trigger_value: int  # 1 或 -1
class FormulaCreate(BaseModel):
    name: str
    content: str
    signals: list[SignalItem]
```
校验：signal_type 必须在 {OPEN,ADD,REDUCE,CLOSE}，trigger_value 必须 ∈{1,-1}（不在则 `{"code":400,"message":"..."}`）。

**TDD 顺序**（每接口 RED → GREEN → 全量回归）：
1. `test_list_formulas` — seed Formula+FormulaSignal 后断言返回含 signals 子列表
2. 实现 `list_formulas`
3. `test_get_formula` / `test_get_formula_not_found`
4. 实现 `get_formula`
5. `test_create_formula` — POST 后断言 DB 有 Formula + N 条 FormulaSignal
6. 实现 `create_formula`
7. `test_update_formula` / `test_delete_formula`
8. 实现 `update_formula` / `delete_formula`

测试文件：新建 `main/core/tests/integration/test_formula_api.py`，复用 `test_backtest_api.py` 的 `client` fixture（StaticPool + 函数键 `get_db` 覆盖 + yield `(c, Session)`）。seed 只需 `Formula + FormulaSignal`（无 FK 依赖其他表）。

### 第 2 步：前端 API 客户端扩展（`web/src/api/index.ts`）

`getFormulas()` 已存在。新增：
```ts
export async function getFormulaDetail(id: number) { ... }       // GET /formulas/{id}
export async function createFormula(req: { name; content; signals: SignalItem[] }) { ... }  // POST /formulas
export async function updateFormula(id: number, req: {...}) { ... }  // PUT /formulas/{id}
export async function deleteFormula(id: number) { ... }          // DELETE /formulas/{id}
```
沿用既有 `api` 实例 + `ApiResponse<T>` 解包模式。给 `web/package.json` 加 `"test": "vitest"`。

### 第 3 步：前端新增公式管理视图（TDD，vitest）

- 新建 `web/src/views/Formulas.vue`：操作栏 + 列表卡片 + 新建/编辑 Modal（信号行动态增删）
- `web/src/router/index.ts`：加 `{ path: '/formulas', name: 'formulas', component: () => import('../views/Formulas.vue') }`
- `web/src/App.vue`：侧边栏加 `<router-link to="/formulas" class="nav-item" active-class="active">📐 公式管理</router-link>` + titles 加 `'formulas': '公式管理'`
- `web/src/style.css`：按需加 `.modal-lg`（信号多行时弹窗宽度）

**前端 TDD**（组件挂载测试，从零建立 vitest 模式）：
- 测试文件 `web/src/__tests__/Formulas.test.ts`
- `vi.mock('../api', ...)` stub `getFormulas`/`createFormula`/`updateFormula`/`deleteFormula`，`mount(Formulas)` + `flushPromises()`
- RED 用例：挂载渲染 mock 公式行 / 点新建弹 Modal / 添加信号行 / 提交调 `createFormula` 且参数含 signals / 点删除调 `deleteFormula`
- GREEN：实现 `Formulas.vue` 至测试通过
- 若 happy-dom 限制，回退测 API 客户端函数（`vi.mock('axios')` 断言调用参数）保底

### 第 4 步：端到端联调

`./manage.ps1 start` → 浏览器 `/formulas` → 新建公式（填 MACROSSPRO + 4 信号）→ 列表出现 → 编辑改信号 → 删除。验证 DB 落库正确。

## 关键文件

**后端**：
- `main/core/api/formulas.py` — 5 个 CRUD 路由实现
- 新建 `main/core/tests/integration/test_formula_api.py` — TDD 测试

**前端**：
- 新建 `web/src/views/Formulas.vue` — 公式管理视图
- `web/src/api/index.ts` — 加 4 个 CRUD 函数
- `web/src/router/index.ts` — 加路由
- `web/src/App.vue` — 加侧边栏导航项 + title
- `web/src/style.css` — 按需加 `.modal-lg`
- `web/package.json` — 加 `test` 脚本
- 新建 `web/src/__tests__/Formulas.test.ts` — 前端 TDD 测试

**不改**：模型（Formula/FormulaSignal 已完备）、引擎、回测接口、其他视图。

## 验证

1. 后端：`uv run pytest`（在 `main/`）— 现有 60 + 新增约 8 = 全绿
2. 前端：`npm test`（在 `web/`）— 新增组件测试通过
3. 端到端：`./manage.ps1 start` → `/formulas` → 新建/编辑/删除公式全流程通

## 不做（明确排除）

- 组合策略前端（Portfolios.vue 重写）— 依赖公式管理先就绪，属后续切片
- 回测前端闭环（Backtest.vue 重写）— 依赖组合策略，再后续
- 公式语法校验/测试运行（调通达信验证公式）— 仅存文本，不联调通达信
