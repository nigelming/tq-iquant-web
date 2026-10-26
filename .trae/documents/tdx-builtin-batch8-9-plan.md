# 通达信内置引擎 批次8（FINDHIGH 族补充）+ 批次9（跨周期/跨公式引用）立项计划

## Context

批次 6/7 已在线实证闭环（函数库 111 个）。用户确认立项两项：
1. **批次 8**：FINDHIGH 族补充——从词典 396 条中剩余 278 条未实现里选型四组约 22 个函数
2. **批次 9**：跨周期（`C#WEEK`）/跨公式（`公式名.线名`）引用——用户真实公式（周线MACD金叉分类、缠论威科夫30分钟主图）的核心阻塞点

用户已确认决策：
- 跨周期语义：**默认 TDX 语义**（引用"所属"上级 bar 整根值，周内存在未来引用，与通达信全历史显示逐 bar 零差异）+ **公式级可选无泄露开关**（仅引用已完成上级 bar，默认关闭）
- 跨公式：**含组合语法** `NAME.LINE#PERIOD`（仅允许引用 engine=builtin 公式，带环检测）
- 选型四组全做，**最终词表以通达信公式编辑器运行时实测为准**（词典≠运行时函数表，批次 6 DAYOFWEEK/TOTALBARCOUNT 教训）

硬约束：仅对接回测 engine=builtin 链路，实盘始终走 TQ 不受影响；TDD 流程：单测→实现→TQVERIFYn 导入实测→在线对比→修复→全量回归。

## 批次 8：函数补充选型（22 个）

| 组 | 函数 | 说明 |
|---|---|---|
| 核心组 | XMA | 偏移移动平均；词典注明用未来 N/2 日数据——预检从拒绝改**警告**（leak 标签），实现按 TDX 全历史递推 |
| 核心组 | HHVLLV(X,T,N1,N2) | 区间极值（T=0 高/T=1 低，前 N1 到前 N2 时段） |
| 核心组 | MEDIAN(X,M,N) | 倒数第 M 个起 N 周期中位数 |
| 核心组 | UPDOWN(X) | SIGN(X−REF(X,1)) → -1/0/1 |
| 核心组 | MAX6(A..F) | 6 参数最大值 |
| 核心组 | NEWSAR(N,S) | 新抛物转向（起始统计天数+加速因子） |
| 核心组 | DRAWNULL | 恒 null 占位序列（配合 IF 控制不画） |
| 信号过滤组 | TFILTER(A,B,N) | 多空配对去抖（状态机循环） |
| 信号过滤组 | TTFILTER(开,平,卖开,买平,N) | 四信号配对（状态机循环） |
| 时间/周期组 | WEEK / SEASON / WEEKOFYEAR | 裸标识符时间分量（批次 5 家族，依赖 datetime 列） |
| 时间/周期组 | PERIOD / MIN1..MIN60 | 当前周期编号 + 周期常量；**给求值器加周期上下文**（批次 9 复用） |
| 大盘引用族 | INDEXC/O/H/L/V/A | 对应大盘指数 OHLCV；需向回测注入指数 K 线（沪→上证指数、深→深证成指，代码映射实现时核对 TQ 指数取数路径）；**仅当公式实际用到才拉取**，不影响现有行为 |

TQVERIFY8：全部新函数出线（约 24 条），用户导入通达信实测运行时存在性（预期迭代剔除"未知字符串"项）→ 在线对比 → 修复。

## 批次 9：跨周期/跨公式引用

### 语法与求值
- [lexer.py](file:///d:/project/tq-iquant-web/main/core/engine/tdx_formula/lexer.py)：新增 `.` op token（注意与数字小数点区分）
- [evaluator.py](file:///d:/project/tq-iquant-web/main/core/engine/tdx_formula/evaluator.py) `_atom`（L243-262）：
  - `NAME.LINE` → `("xref", FORM, LINE)`；`NAME.LINE#PERIOD` → `("xref_cross", FORM, LINE, PERIOD)`；`FIELD#PERIOD` 已有 `("cross", ...)` 节点
- 求值上下文扩展（向后兼容，现有调用不动）：
  `evaluate_outputs(src, df, *, period=None, period_frames=None, index_frames=None, xref_resolver=None)`
- **对齐器** `_align_cross(base_df, higher_df)`：由上级 df 时间戳序列构造区间 [prev_end+1天, end]，base bar 落入哪根上级 bar 即"所属"；无泄露变体 = 所属 bar 未完成（base 时间 < 上级 bar 终点）时取上一根。**仅支持引用更粗周期**，引用更细周期显式报错（v1 边界）
- 周期后缀映射：MIN1/MIN5/MIN15/MIN30/MIN60→1m/5m/15m/30m/60m，DAY→1d，WEEK→1w，MONTH→1mon；未知后缀报错。注意 backtest_service `_MINUTE_PERIODS`（L1443）含 `"1h"` 与 `_PERIOD_ORDER`（backtest_engine L31）`"60m"` 并存，映射表需兼容两写法（实现时以 `_convert_market_data_multi` 实际键为准）
- 无泄露开关：公式级字段（如 `no_leak_cross`，默认 false=TDX 语义），从策略/公式配置透传到 evaluate_outputs

### 跨公式解析
- [backtest_service.py](file:///d:/project/tq-iquant-web/main/core/services/backtest_service.py) `_builtin_formula_entries`（L1484）扩展：
  - 构建 `xref_resolver(formula_name, period_suffix|None)` 闭包：按名查 Formula（仅 engine=builtin，否则报错）→ 在同 code、目标周期 df 上求值（复用 memo cache 按 (formula_id, period, code)）→ `NAME.LINE#PERIOD` 经对齐器转回 base 周期序列
  - 求值栈环检测（A→B→A 报"循环引用"）；公式缺失/线名缺失报明确错误 → `diagnostics.errors`（该组策略零信号，现有口径）
- INDEX* 数据注入：`index_frames` 由 backtest_service 在 build_signal_cache 内按需（公式含 INDEX* 才拉取）经 TQ 取指数 K 线

### 预检
- [dictionary.py](file:///d:/project/tq-iquant-web/main/core/engine/tdx_formula/dictionary.py) `precheck`：XMA 由拒绝改警告（新增 leak 规则）；`#`/`.` 引用不做 db 校验，求值层报错（precheck 保持纯函数）

## 关键文件

| 文件 | 改动 |
|---|---|
| main/core/engine/tdx_formula/evaluator.py | 批次8 函数实现 + EvalContext/对齐器 + xref/cross 节点求值 |
| main/core/engine/tdx_formula/lexer.py | `.` token |
| main/core/engine/tdx_formula/data/tdx_funcs.json | XMA 能力标签调整（如需） |
| main/core/engine/tdx_formula/dictionary.py | precheck leak 警告规则 |
| main/core/services/backtest_service.py | resolver 构建 + 指数注入 + 无泄露开关透传 |
| main/core/tests/unit/test_tdx_formula_eval.py | 批次8/9 语义单测（先行） |
| main/core/tests/integration/test_tdx_formula_compare.py | TQVERIFY8/9 接入；harness 为批次9 拉取周线数据 + 引用公式（TQVERIFY9B） |
| main/core/tests/tdx_compare/TQVERIFY8.txt / TQVERIFY9.txt / TQVERIFY9B.txt | 新建验证公式 |

## 实施顺序

1. **批次 8**：单测（22 函数语义假设）→ 实现 → TQVERIFY8 导入实测（用户）→ 在线对比 → 修复 → 全量回归
2. **批次 9**：单测（对齐器/xref/环检测/无泄露）→ 实现 → TQVERIFY9 导入实测（用户）→ 在线对比（重点：周内对齐语义逐 bar 实证，预期需按 TDX 实际口径修对齐器）→ 修复 → 全量回归
3. 每批收尾：更新项目记忆实证结论、清理临时脚本

## 验证

- 每步：`uv run pytest core/tests/unit/test_tdx_formula_eval.py -q`（cwd=main）
- 在线对比：`uv run pytest core/tests/integration/test_tdx_formula_compare.py -q -s`（需通达信在线、用户预导入公式）
- 全量：`uv run pytest core/tests/ -q`
- 批次9 附加手验：用含 `C#WEEK` 的真实策略跑一次回测，确认信号产出与 diagnostics 无报错
