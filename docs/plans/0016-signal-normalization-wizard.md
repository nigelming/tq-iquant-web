# 0016 信号标准化向导：源码级改写为回测平台信号标识 专项评估

> 状态：**已实施（2026-10-21，全量回归通过：后端 1463 passed / 前端 271 passed）**
> 日期：2026-10-21
> 前置：「通达信指标源码整理工具.html」能力吸收进度——源码体检（lint 四类检测）已落地 syntax_check（单元 1428 passed 全绿）；strip_draw_attrs / parse_defs 基础件现成；本批次吸收**信号标准化**（TdxSigNorm）
> 用户定调：**改造公式为回测平台接收的信号标识**；存新公式不动原件
> 实施落点：生成算法 [normalizer.py](../../main/core/engine/tdx_formula/normalizer.py)（纯函数 normalize_formula）；API `POST /api/formulas/normalize-preview`（不落库预览，保存走现有 POST /api/formulas）；前端公式管理页每行「信号标准化」按钮 → 三步向导（选线→配规则→预览保存，默认名 `原名-STD`，engine/formula_count/no_leak_cross 继承源公式，生成线预填开仓信号+触发值 1）；E6 入口闸门=点击时先跑语法预检，语法错误或无 `:` 输出线 alert 拦截；E1/E2/E3/E4/E5 按建议默认值实施；单测 31 例（test_tdx_formula_normalizer.py）+ 集成 4 例 + 前端 5 例。待用户验收 A2（生成公式导入通达信可编译抽查）/ A3（端到端信号触发）

## 1. 背景与目标

回测平台对公式信号的接收口径（现有硬规则）：

| 规则 | 说明 |
|---|---|
| 信号来源 | 仅 `:` 定义的输出线（`:=` 中间变量、STICKLINE 等绘图指令均不可配置为信号） |
| 匹配逻辑 | 严格相等：输出线在 bar 上的值 == trigger_value 才触发，其余静默无信号 |
| 取值范围 | trigger_value 仅允许 ±1/±2，禁止 0（防海量假信号） |

用户从通达信复制的公式，输出线多为**连续值**（如 `DIF:EMA(C,12)-EMA(C,26);`、`VAR1:CLOSE;`），直接配置信号会永远匹配不上。信号标准化向导的目标：**选源公式 + 输出线 + 规则 → 自动生成新公式**——原输出线降级为中间变量，尾部追加离散标识值（±1/±2/0）的标准化输出线，新公式直接可被策略信号配置使用。

硬约束：纯新增功能；生成的公式存为**新公式记录**，源公式零改动；不触碰回测/实盘执行链路。

## 2. 现状基线（代码探查结论）

| 能力 | 现状 | 位置 |
|---|---|---|
| 语句切分 | `parse_defs` 按顶层 `;` 切分（引号/注释内分号不切），中文线名、跨行语句、末尾无分号均已支持 | [parser.py](../../main/core/engine/tdx_formula/parser.py) `parse_defs` |
| 属性剥离 | `strip_draw_attrs` 剥输出线尾部 COLORxxx/LINETHICK/NODRAW 等（括号平衡保护） | 同上 `strip_draw_attrs` |
| 输出线枚举 | `syntax_check` 返回 `outputs`（`:` 线名列表）；`Definition(op, expr, attrs, line)` 结构完整 | [evaluator.py](../../main/core/engine/tdx_formula/evaluator.py) `syntax_check` |
| 语法预检 | 向导生成的公式可直接走现有 `POST /api/formulas/precheck` 验证 | [formulas API] `precheck` |
| 函数可用性 | 规则生成表达式仅用 CROSS/IF/比较运算符——全部已实现且在线实证（批次 1-9） | evaluator.py `_SUPPORTED_FUNCS` |
| 公式 CRUD | 存新公式走现有 `POST /api/formulas`（engine/name/description 可继承源公式） | [formulas API] |

**结论：无数据层/schema 改动，无新 TDX 语法，本批次不需要通达信 P0 实证。**

## 3. 方案设计

### 3.1 生成算法（纯函数，后端 `core/engine/tdx_formula/normalizer.py`）

```
normalize_formula(src: str, plans: list[NormalizePlan]) -> NormalizeResult
  NormalizePlan = {line: str, rule: str, params: dict}
  rule ∈ {"cross_zero", "above_threshold", "map_values"}
```

步骤：
1. `parse_defs(src)` 取全部定义（顺序保留）
2. 校验：plans 中每个 line 必须是源公式的 `:` 输出线，否则报错
3. 全部原 `:` 线**降级为 `:=`**（含被选中与未选中的——被选中线降级后作为新输出线的输入；strip_draw_attrs 已在 parse_defs 内完成，attrs 丢弃即自然完成「输出清理」）
4. 按 plans 顺序在**源码尾部**追加标准化输出线（表达式引用原线名，TDX 大小写不敏感）
5. 注释（`{...}`）原样保留在原位置；返回新源码 + 生成的输出线清单（供前端展示「可用信号」对照）

### 3.2 三类规则的默认生成语义（待 E1/E3 确认）

| 规则 | 用户输入 | 生成表达式（示例：原线 MACD） | 输出值域 |
|---|---|---|---|
| 上穿0 | 方向：双向/仅上穿/仅下穿 | `XG:CROSS(MACD,0);` + `XD:CROSS(0,MACD);`（双向） | XG/XD 各 1.0/0.0 |
| 大于阈值 | 阈值 + 方向（大于/小于） | `XG:MACD>0.2;` | 1.0/0.0 |
| 值域映射 | 映射对列表（原值→±1/±2） | `XG:IF(MACD=1,1,IF(MACD=-1,-1,0));` | 配置的离散值 |

上穿0/大于阈值也可选「单线 ±1」形态（`XG:CROSS(MACD,0)-CROSS(0,MACD);` → 1/-1/0），见 E1。

### 3.3 API 与前端

| 层 | 内容 |
|---|---|
| API | `POST /api/formulas/normalize-preview`：`{content, plans}` → `{new_content, output_lines, warnings}`（接受源码而非 id，支持草稿预览）；保存走现有 `POST /api/formulas`（前端携带生成内容+继承的元数据） |
| 前端 | 公式管理页每行加「信号标准化」按钮 → 向导对话框三步：①勾选输出线（调 precheck 取 outputs）②逐线选规则+参数 ③预览新源码（含生成线清单）→ 确认保存为新公式（默认名 `原名-STD`，engine/描述继承，可改） |

## 4. 待决问题（E 项，需用户定调后实施）

| # | 问题 | 建议 |
|---|---|---|
| E1 | 「上穿0」输出形态：XG+XD 两条独立线（各 1/0，策略里分开绑定信号）还是单线 ±1（上穿=1 下穿=-1） | 建议**双线默认**（与通达信 XG/XD 惯例一致、语义直白），单线作为可选形态 |
| E2 | 多线标准化 / 源公式已有 XG/XD 时的命名冲突 | 建议：首个生成线 XG，冲突时依次 XG2/XG3…；值域映射只生成一条线时优先 XG |
| E3 | 「值域映射」的交互形式与边界：映射对数量上限、原值非离散（浮点误差）怎么办 | 建议映射对 ≤4 个、原值输入精确相等比较（与平台 trigger 匹配口径一致），页面提示浮点值需与公式输出完全一致 |
| E4 | 「大于阈值」是否需要「介于区间」变体（low<线<high → 1） | 首版不做，用值域映射+两条件公式可覆盖；有真实需求再扩展 |
| E5 | 降级后绘图属性（COLORRED 等）直接丢弃是否可接受（原公式不受影响） | 建议丢弃（新公式是信号用途，属性无意义） |
| E6 | 无 `:` 输出线 / 语法错误的源公式，向导入口处理 | 建议按钮置灰 + tooltip 提示先跑语法预检 |
| E7 | 生成的公式是否要求在通达信公式编辑器可编译（双引擎可用） | 建议要求（生成表达式仅用基础函数，天然合法；作为验收项 A2，用户抽查导入编译） |

## 5. 验收清单

- A1 单测：三类规则各 1 正例（生成源码快照）+ 降级/属性丢弃/注释保留/命名冲突/非法 line 报错/多线顺序；TDD 先行
- A2 用户抽查：生成的公式导入通达信公式编辑器可编译（E7）
- A3 端到端：生成的新公式在回测策略中配置信号（线名+trigger_value）正常触发；源公式记录内容零变化
- A4 全量回归：`uv run pytest core/tests/ -q` 全绿；前端 `npx vitest` 全绿
- A5 默认行为不变：不调用向导时公式管理/回测链路零影响

## 6. 实施顺序（TDD）

1. normalizer.py 纯函数 + 单测（规则/降级/冲突/边界）
2. API normalize-preview + 集成测试
3. 前端向导对话框（三步）+ vitest
4. 全量回归 + 用户验收（A2/A3）
