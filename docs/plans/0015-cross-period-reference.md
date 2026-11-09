# 0015 通达信内置引擎批次 9：跨周期/跨公式引用 专项评估

> 状态：**批次 9 全部在线闭环**（9a：TQVERIFY9 12 条线 × 8473 根 PASSED；9b：TQVERIFY9B 7 条线 × 8473 根 PASSED——R06 的 11 根 TDX 内部 MA 浮点平局差已声明 known，其余零差异；单元 1180 passed；全套 TQVERIFY1-9B 在线回归 18 passed）。
> 日期：2026-10-19（9a 实施+闭环：2026-10-20；9b 实施+闭环：2026-10-21）
> 前置：批次 8 已闭环（函数库 113→移除 2 个后 111 个有效，全量回归 1385 passed，TQVERIFY8/8B 在线全绿）
> 上游：[tdx-builtin-batch8-9-plan.md](../../.trae/documents/tdx-builtin-batch8-9-plan.md) 批次 9 节（方向已批准，本文档为实施级评估）

## 0.2 9b 在线实证结论（TQVERIFY9B，2026-10-21）

- **E5/E6 实证通过**（用户通达信实测）：`TESTA.A1`（跨公式）与 `TESTA.A1#WEEK`（跨公式+跨周期组合）均编译执行通过——QMT 通道内嵌通达信接受 `NAME.LINE[#PERIOD]` 全部语法形态
- **7 条线实证语义**（TESTA 内容=A1:CLOSE;）：R01≡C、R02≡REF(C,5)、R03≡MA(C,5)、R04≡C#WEEK（零差异，与 L01 同语义）、R05≡HHV(C#WEEK,8)（零差异，与 L11 同语义）、R07≡0（TESTA.A1-C 恒等）——resolver 求值路径与 FIELD#PERIOD 对齐器口径完全一致
- **R06 已知差**（CROSS(TESTA.A1,MA(TESTA.A1,5))，11/8473 根）：探针实证 11 根前一根 C==MA5 全为数学平局（prevC−MA5=0，当根间距充分）——TDX 内部 MA 浮点求和尾差把平局判为「已上穿」不触发 CROSS，内置整数分定点平局精确相等触发。批次 1 F15 已实证不可复现（四种求和口径均无法全复现），声明 known
- 全量回归：单元 1180 passed；在线对比 TQVERIFY1-9B 全部 PASSED（18 passed）

## 0.3 9b 实施记录（2026-10-21）

| 层 | 落地内容 |
|---|---|
| evaluator.py | `_TOKEN_RE` op 组加 `.`（num 分支优先匹配小数）；`_atom` 解析 `NAME.LINE` / `NAME.LINE#PERIOD`（→`xref`/`xref_cross` 节点，名字统一大写）；`_Evaluator(xref_resolver=)` + `_xref_series`（resolver 缺失/未知后缀/缺上级数据/更细周期/线缺失显式报错，带后缀经 `_align_cross` 折回）；`evaluate_outputs(xref_resolver=)` 透传；`xref_names_of()` 收集被引公式名（解析失败容错）；`_static_check` 对 xref 节点不校验（运行时经 resolver）；`_walk_cross` 补收集 `xref_cross` 周期键（`TESTA.A1#WEEK` 取数需周线）；`__init__.py` 导出 `xref_names_of` |
| backtest_service.py | `_make_xref_resolver(code, base_period, ...)`：单 code resolver 闭包（被引公式仅查 builtin 名表、memo `{(formula_id, period, code)}` build_signal_cache 级共享、被引公式自身跨周期/跨公式引用递归解析）；`_xref_static_check`：DFS 三色环检测 + 被引公式存在性（缺失/非 builtin → diagnostics.errors 零信号）；`_builtin_formula_entries(xref_formulas=, xref_memo=)` 逐 code 注入 resolver |
| 测试 | 单测 TestBatch9XRef 10 例（恒等/表达式/线缺失/无 resolver/组合对齐/no_leak/未知后缀/缺上级数据/名字收集/静态检查放行）+ 接线 4 例（base 周期/被引缺失/自引用环/组合语法端到端）+ cross_periods_of 组合语法 2 断言；TQVERIFY9B.txt（7 条线）+ harness 手工 TESTA resolver（不走 db）+ test_tqverify9b_matches_builtin + R04/R05/R06 known |

**默认行为不变**：公式无 `NAME.LINE` 引用时 `_xref_formulas` 名表与静态检查零影响（engine=tq 路径完全不经过）；`xref_resolver=None`（旧调用方/单测直接调 evaluate_outputs）遇 xref 节点显式报错。

## 0.1 9a 在线实证结论（TQVERIFY9，2026-10-20）

- **E1 实证通过**：QMT 通道内嵌通达信接受 `FIELD#PERIOD` 后缀语法（TQVERIFY9 导入成功，12 条线全部编译执行）
- **E7 实证**：TQ 周线时间戳为「该周最后交易日」标（现代=周五；1991-1992 早期周六/周日也交易日，标周六/周日）。对齐器改为**日历归算**（`_period_window_start`：周线归算周一 00:00、月线归算当月 1 日、日线当日 00:00、分钟不变），免疫标法差异
- **E8 实证**：价格字段（C/O/H/L）跨周期引用=所属上级 bar **整根终值**（含周内未来引用，与用户定调一致）；**VOL 例外**：`V#WEEK`/`V#MONTH` 为**上级窗口内逐日累计**（不含未来，周一=当日量、周五=周总量），`_cross_volume_series` 特殊实现
- **known 豁免**（279 根，全部 ≤bar524，1991-1993）：① 早期周六/周日交易日历（TDX 周线周日换根、行时间戳周六/周日标，与自然周一分桶及 TQ 周线行分周不一致）；② 1993-05 除权周（bar531 除权日同源），TDX 内部跨周期对除权周 OHLC 预调整而 TQ 周线 df 为原始价；③ V#WEEK 累计起点同步偏移。现代段（1997+）零差异
- 全量回归：单元 1166 passed；在线对比 TQVERIFY1-9 全部 PASSED

## 0. 9a 实施记录（2026-10-20）

| 层 | 落地内容 |
|---|---|
| evaluator.py | `_CROSS_PERIOD_MAP`（MIN1..MIN60/DAY/WEEK/MONTH→周期键，60m/1h 双写法）、`_PERIOD_MINUTES` 粒度表、`_align_cross` 对齐器（区间 [t_i, t_{i+1}) 分桶、末根 +∞、no_leak 取上一根）、`_Evaluator._cross_series`（数据缺失/更细周期显式报错）、`evaluate_outputs(period_frames=, no_leak=)`、`cross_periods_of()` 取数收集、`_static_check` cross 改周期后缀白名单校验 |
| backtest_service.py | `build_klines` 扫描 builtin 公式附加拉取缺失上级周期（无引用零改动）、`_builtin_formula_entries(cross_periods=, no_leak=)` 组装 period_frames 注入、`build_signal_cache` memo + no_leak_cross 透传 |
| models + 迁移 | `Formula.no_leak_cross`（Boolean，server_default 0）+ 迁移 c0d1e2f3a4b5（已 upgrade） |
| API/前端 | `FormulaCreate.no_leak_cross`（默认 False）、serialize/create/update、Formulas.vue 勾选（仅 engine=builtin 时显示，默认不勾）、api/index.ts 类型 |
| 测试 | 单测 TestBatch9CrossPeriod 14 例 + TestCrossPeriodSyntax 4 例；TQVERIFY9.txt（12 条线）+ harness 拉周/月线注入 period_frames + test_tqverify9_matches_builtin |

**待用户操作**：在通达信公式管理器导入 `main/core/tests/tdx_compare/TQVERIFY9.txt`（公式名 TQVERIFY9，技术指标-其他类型）。若编辑器报「未知字符串/语法错误」→ E1 实证失败（QMT 不支持 `#` 语法），9a 需重新评估语法形态；导入成功 → 跑 `uv run pytest core/tests/integration/test_tdx_formula_compare.py -v -s`（cwd=main，需通达信在线）完成 12 条线逐 bar 对比（重点 E7 周线时间戳惯例、E8 所属取值语义，预期按实际口径修对齐器）。

9b（跨公式引用 `NAME.LINE`）待 9a 在线闭环后启动（前置 E5/E6 实证）。

## 1. 背景与目标

用户真实公式（周线 MACD 金叉分类、缠论威科夫 30 分钟主图）的核心阻塞点是跨周期/跨公式引用。批次 9 目标：

- **跨周期引用**：`C#WEEK` / `HIGH#DAY` 等 `FIELD#PERIOD` 后缀语法，在 builtin 引擎内对上级周期序列求值并对齐回当前周期
- **跨公式引用**：`公式名.线名`（及组合 `公式名.线名#周期`）引用其他 engine=builtin 公式的输出线

硬约束（不变）：**仅对接回测 engine=builtin 链路**，实盘始终走 TQ 调通达信，不受影响。默认关闭、不启用时行为与现状完全一致。

## 2. 现状基线（代码探查结论）

| 层 | 现状 | 位置 |
|---|---|---|
| 词法 | `#` 已 token 化（`(?P<hash>#)`）；`.` 不在 op 表（数字分支 `\d+(?:\.\d+)?` 会吞小数点，`NAME.LINE` 场景需单加 op） | [evaluator.py](../../main/core/engine/tdx_formula/evaluator.py) `_TOKEN_RE` L149-156 |
| 解析 | `FIELD#PERIOD` 已产出 `("cross", FIELD, PERIOD)` 节点（词法层早已就绪，只差求值） | `_atom` L275-281 |
| 求值 | cross 节点显式报错「跨周期引用 … 暂不支持」 | L464-466 |
| 静态预检 | cross 节点报致命错误 | `_static_check` L2058-2069 |
| 求值入口 | 已有关键字参数 `period=`（PERIOD 周期编号）、`index_frames=`（批次 8 INDEX* 族），扩展点成熟 | `evaluate_outputs` L1991-2016 |
| 数据层 | `build_klines` 仅拉取**策略周期集合**（`_strategy_periods` 去重）；klines 结构 `{code: {period: df}}` 天然多周期 | [backtest_service.py](../../main/core/services/backtest_service.py) L288-324 |
| 引擎 | `(datetime, period)` 复合键索引，W4 已根治混合周期午夜覆盖；signal cache 键 `(strategy_id, code, bar_time)` 不含 period | [backtest_engine.py](../../main/core/engine/backtest_engine.py) `_build_price_index` L393 |
| 周期合法值 | 策略层 `{"1m","5m","15m","30m","1h","1d","1w","1mon"}`；引擎 `_PERIOD_ORDER` 用 `"60m"`（与 `"1h"` 并存，映射表需兼容两写法） | [strategies.py](../../main/core/api/strategies.py) L44 |
| 公式模型 | `Formula.engine`（tq/builtin）、`formula_count` 已有；无跨周期相关字段 | [formula.py](../../main/core/models/formula.py) |

**核心数据缺口**：5m 策略引用 `C#WEEK` 时，周线数据根本不在 klines 里（build_klines 只拉策略周期）——跨周期的第一改动点是**取数扩展**，其次才是对齐。

## 3. TDX 实证清单（需用户在通达信公式编辑器实测）

> 批次 6/7 教训：词典≠运行时函数表。`#` 是**操作符**不是函数，理论上不受函数表约束，但 QMT 通道内嵌版编译器是否接受该语法**未经实证**，是本批次的 P0 前置。

**P0（9a 动工前必须实证）**：

| # | 实证点 | 测试公式 | 预期分支 |
|---|---|---|---|
| E1 | `FIELD#PERIOD` 后缀语法是否被编译器接受 | `T:C#WEEK;` | 接受 → 9a 继续；报「未知字符串/语法错误」→ 9a 重新评估语法形态（如带引号 `"C#WEEK"` 或函数式） |
| E2 | 大小写 | `t:c#week;` | 与 E1 一致则大小写不敏感 |
| E3 | 分钟锚点名在 `#` 后是否可用 | `T:C#MIN30;` `T:C#MIN5;` | 函数 MIN1..MIN60 不在运行时表（批次 8B 实证），但后缀锚点可能独立支持 |
| E4 | 引用自身周期/更细周期 | 日线公式 `T:C#DAY;` | 观察行为（预期恒等取本周期值） |

**P1（9b 动工前）**：

| # | 实证点 | 测试公式 |
|---|---|---|
| E5 | 跨公式引用 `公式名.线名` 是否支持（TDX 传统写法，需先建被引用公式） | 建 `TESTA`（`A1:CLOSE;`），再建 `T:TESTA.A1;` |
| E6 | 组合语法 `公式名.线名#周期` | `T:TESTA.A1#WEEK;` |

**P2（可由在线对比替代实证）**：

| # | 实证点 | 说明 |
|---|---|---|
| E7 | TQ 周线/月线 bar 时间戳惯例（周一 00:00 还是周五？） | 影响对齐器区间构造；对齐器按「相邻时间戳分桶」实现可免疫，TQVERIFY9 逐 bar 对比直接暴露 |
| E8 | 上级 bar「所属」取值语义 | 用户已定调默认 TDX 语义（所属 bar 整根终值，周内含未来引用）；TQVERIFY9 全历史逐 bar 对比即实证 |

## 4. 架构方案

### 4.1 跨周期引用（9a）

**① 取数扩展（backtest_service.build_klines）**

- 扫描本次回测所有 builtin 公式的 AST，收集 `#PERIOD` 引用的周期集合 → 与策略周期集合求差 → 差集按上级周期**附加拉取**（同一次 `get_history_raw` 调用，periods 参数多传几项）
- 上级周期预热窗口独立计算：1 根周 bar 覆盖 5 交易日、1 根月 bar 覆盖约 22 交易日；无泄露模式还要多取 1 根
- **公式不含 `#` 引用时零改动**（默认行为不变）

**② 对齐器 `_align_cross(base_df, higher_df, no_leak: bool)`**

- 由上级 df 时间戳序列构造区间 `[t_i, t_{i+1})`，base bar datetime 落入哪根区间即「所属」该上级 bar；末根区间延至 +∞
- 输出长度恒等于 base_df 高度（与 `_builtin_formula_entries` 的 bar_times 对齐消费兼容）
- 默认（no_leak=False，TDX 语义）：取所属上级 bar 的**整根终值**（含未来引用）
- no_leak=True：所属 bar 未完成（base bar 时间 < 上级 bar 终点时间）→ 取上一根已完成 bar
- **仅支持引用更粗周期**；引用更细周期（如日线策略引 `C#MIN5`）显式报错——1:N 无法对齐，v1 边界
- 上级数据缺失/不足 → 输出 null（无信号常态，与 TQ 口径一致）

**③ 求值上下文扩展（向后兼容）**

```python
evaluate_outputs(src, df, *, period=None, index_frames=None,
                 period_frames: dict[str, pl.DataFrame] | None = None,
                 no_leak: bool = False) -> dict[str, pl.Series]
```

- `period_frames`: `{周期键: 上级 df}`（含当前周期自身的键时对齐器恒等映射，覆盖 E4 场景）
- 周期后缀映射：`MIN1/MIN5/MIN15/MIN30/MIN60→1m/5m/15m/30m/60m`（兼容 `1h`）、`DAY→1d`、`WEEK→1w`、`MONTH→1mon`；未知后缀报错
- cross 节点求值：`("cross", FIELD, PERIOD)` → 查 period_frames → 对齐器 → 取 FIELD 列

**④ 无泄露开关 `no_leak_cross`（公式级，默认 false）**

- `Formula` 表加列 `no_leak_cross`（Boolean，server_default false）+ Alembic 迁移
- API/前端公式表单勾选项，默认不勾选（符合项目「新增默认不启用」约束）
- 透传链：Formula → build_signal_cache → `_builtin_formula_entries` → evaluate_outputs

**⑤ 预检放宽（dictionary.py / syntax_check）**

- cross 节点从「致命错误」改为静态校验：周期名白名单 + 拒绝更细周期引用；数据可得性（周期是否在 klines）留求值层报
- 未来函数关键字预检对 `#` 引用豁免（默认 TDX 语义本身含未来引用，避免误报）

### 4.2 跨公式引用（9b）

- **词法**：op 分支加 `.`（数字分支优先匹配 `1.5` 小数，`NAME.LINE` 的 `.` 独立成 op，无冲突；`NAME.5` 类非法语法解析报错）
- **解析**：`NAME.LINE` → `("xref", FORM, LINE)`；`NAME.LINE#PERIOD` → `("xref_cross", FORM, LINE, PERIOD)`
- **resolver**：`build_signal_cache` 构建闭包 `xref_resolver(formula_name)`：
  - 按名查 Formula（仅 engine=builtin，否则报「仅支持引用内置引擎公式」）
  - memo 缓存按 `(formula_id, period, code)`（复用组内求值，避免逐 bar 重复求值）
  - `xref`：在同 code 同周期 df 上求值被引公式 → 取该线序列（长度天然一致，无需对齐）
  - `xref_cross`：在目标周期 df 上求值 → 经对齐器转回 base 周期
- **环检测**：resolver 内维护求值栈（A→B→A 报「循环引用」）；实现时优先做静态 AST 扫描（构建期一次性检出，报错更早）
- **错误口径**：公式缺失 / 线名缺失 / 非 builtin / 循环引用 → `FormulaEvalError` → `diagnostics.errors`（该组策略零信号，沿用现有口径）

## 5. 引擎影响面

- **引擎零改动**：对齐后序列长度=base df 高度，信号照常写入 `(strat.id, code, base_bar_time)` 键；W4 `(datetime, period)` 路由不受影响（跨周期信号已折算到 base bar_time）
- **实盘零改动**：bridge/TQ 路径不触碰
- **TQ 路径回测零改动**：`engine="tq"` 公式不进 builtin 分支，跨周期语法由通达信自行处理（若 QMT 支持）

## 6. 分期实施建议

**9a 跨周期引用**（前置 E1-E4 实证通过）：

1. 单测先行：对齐器（含末根区间/无泄露/更细周期拒绝/null 传播）、周期映射表、取数扩展
2. 实现：evaluator cross 求值 + build_klines 扩展拉取 + Formula.no_leak_cross 迁移 + 预检放宽
3. TQVERIFY9 验证公式（用户导入实测）→ 在线逐 bar 对比（重点 E7/E8 语义实证，预期按实际口径修对齐器）→ 修复

**9b 跨公式引用**（前置 E5/E6 实证通过）：

4. 单测先行：`.` token / xref / xref_cross / 环检测 / memo
5. 实现：词法+解析+resolver
6. TQVERIFY9B 验证公式（引用公式）→ 在线对比 → 修复

**收尾**：全量回归（`uv run pytest core/tests/ -q`，cwd=main）+ 含 `C#WEEK` 真实策略手验回测 + 更新项目记忆实证结论。

每步验证：`uv run pytest core/tests/unit/test_tdx_formula_eval.py -q`（cwd=main）；在线对比 `uv run pytest core/tests/integration/test_tdx_formula_compare.py -q -s`（需通达信在线、用户预导入公式）。

## 7. 风险与边界

| 风险 | 应对 |
|---|---|
| E1 实证不过（QMT 不支持 `#` 语法） | 9a 重新评估语法形态（引号式/函数式）或降级为仅跨公式引用；**评估阶段即卡口，避免白做** |
| 周线 bar 时间戳惯例未实证（E7） | 对齐器按相邻时间戳分桶实现，不硬编码「周一 00:00」假设 |
| 回测初期上级序列不足 | 输出 null → 无信号（与 TQ 预热语义一致），文档标注 |
| `"1h"` 与 `"60m"` 双写法 | 映射表两写法均收 |
| 未来函数预检误报 | cross/xref 节点豁免未来函数关键字检查（无泄露开关才是防泄露闸门） |
| 跨公式引用放大求值开销 | memo 按 (formula_id, period, code)；被引公式同样享受组内共享 |

## 8. 决策点（请用户确认）

1. E1-E6 实证顺序：建议先做 E1（一条公式 5 分钟），P0 全过再动工 9a
2. `no_leak_cross` 落点确认为 Formula 表加列（vs 公式内容内注释标记）——推荐加列，与 engine 字段同模式
3. 分期节奏：9a/9b 分两个 TQVERIFY 轮次（推荐，隔离实证风险），还是合并一轮
