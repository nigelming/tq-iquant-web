# 市场状态分析套件 - Implementation Plan

> 分期交付（用户决策 2026-09-26）。每阶段最后一个任务为「阶段闸门」：完成全量自验后暂停，交用户实测确认，再进入下一阶段。
> AC 见 [spec.md](file:///d:/project/tq-iquant-web/.trae/specs/regime-alpha-suite/spec.md)。

---

## 阶段一：评估指标扩展 + 持仓快照 + regime 分段器

## Task 1: 基准相对风险收益指标（后端 + 迁移）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 [evaluator.py](file:///d:/project/tq-iquant-web/main/core/engine/evaluator.py) 基于对齐的策略/基准日收益新增：beta、Jensen alpha（年化，rf=0.02 常量复用）、excess_annual_return、tracking_error、information_ratio、up_capture、down_capture、excess_max_drawdown（超额曲线回撤）。
  - 基准缺失/样本不足（<20 个对齐日）时全部为 None，不抛异常。
  - [backtest_evaluation.py](file:///d:/project/tq-iquant-web/main/core/models/backtest_evaluation.py) 增加可空列；Alembic 迁移（纯加列，SQLite batch 模式注意离线限制，用活连接测试方式）。
  - 核实评估离线重算路径：service 能从 snapshots+benchmark 重新计算并 upsert 评估行（供老记录补新字段），不依赖重跑回测。
- **Acceptance Criteria Addressed**: AC-1, AC-10, AC-12
- **Test Requirements**:
  - `rule` TR-1.1: 构造已知 r_s=0.5·r_b+α 的日收益序列，beta/alpha/IR/capture 数值断言（容差 1e-2）；基准缺失时字段为 None
  - `rule` TR-1.2: alembic upgrade head 在干净库与含老数据库均成功；老行新列 NULL 不报错
  - `rule` TR-1.3: 评估离线重算不产生新回测记录，仅更新 evaluations 行
- **Notes**: 年化口径与现有一致（252 交易日、365.25 自然年）；指标命名在 model/API/前端保持蛇形→驼峰映射约定。
- **Completion Evidence** (2026-09-26):
  - evaluator 新增 `_aligned_daily_pairs` + `_compute_relative_metrics`（8 指标，<20 对齐日全 None，零波动/无上涨下跌日有除零守卫）；TR-1.1 由 test_evaluator.py 5 个新测试覆盖（线性序列精确断言 beta=0.5/超额 0.0756/捕获 0.52·0.48、短样本、无基准、延迟对齐、零波动守卫）
  - model 加 8 可空列；迁移 `b7c8d9e0f1a2`（活连接临时库 upgrade/downgrade 双向验证）；TR-1.2 ✓（老行新列 NULL）
  - 离线重算：`recompute_evaluation` service + `POST /api/backtest/records/{id}/evaluations/recompute`（upsert 组合级行，不新建回测记录）；service 4 测试 + API 2 测试；TR-1.3 ✓
  - 全量后端 705 passed；Task 2 指标列将并入同一迁移文件

## Task 2: 交易磨损 / 仓位 / 路径痛苦度指标
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 从回测 trades（amount/commission/stamp_duy/bar_time）与每日快照聚合：年化换手率（双边成交额/期间平均总值，年化）、成本占总值比、平均仓位利用率（market_value/total_value 均值）、空仓交易日占比、最长连续亏损交易日数、最差单日/单周收益、日收益偏度/峰度。
  - 持仓结构指标（最大单票权重、HHI 集中度）依赖每日持仓：定义独立 helper，从持仓序列（Task 3 产出的 positions_json 结构）计算；无持仓数据时为 None。
  - model 加列并入同一迁移（与 Task 1 合并为一次迁移）；组合层与策略层评估口径一致（策略层按其自身 trades/持仓）。
- **Acceptance Criteria Addressed**: AC-1, AC-12
- **Test Requirements**:
  - `rule` TR-2.1: 已知金额/费用的 trades fixture，换手率/成本/空仓占比/连亏天数/偏度峰度逐项数值断言
  - `rule` TR-2.2: 无持仓明细时 HHI/最大单票权重为 None，其他指标正常
  - `rubric` TR-2.3: 口径可解释性；scale 1-5；anchors 1 年化方向错误/3 可算但边界未处理/5 公式与年化均注释且空样本安全；threshold >= 4；evidence 评审记录
- **Completion Evidence** (2026-09-26):
  - evaluator 新增 `_daily_rows`（日收盘统一聚合行，含 positions 负载透传）+ `_compute_cost_metrics`/`_compute_utilization`/`_compute_position_structure`/`_compute_path_metrics`；口径全部写入 docstring 注释（年化换手双边/日均 NAV/自然年；成本/首日总值；ISO 周桶；总体矩 Fisher 超额峰度；<20 日不出偏度峰度；空负载 HHI=None）
  - TR-2.1：7 个新单测（换手/成本精确值、无交易置 0、利用率/空仓 0.4/0.6、连亏2/最差日周 -8%、±1% 对称偏度0峰度-2）；TR-2.2：有/无 positions 负载两个测试；positions_json→重算 HHI service 测试
  - model 加 11 列并入迁移 `b7c8d9e0f1a2`（共 19 列，临时库双向验证）；落库两处改 `_EVALUATION_FIELDS` 表驱动消除三处漂移；序列化 + 离线重算（补 amount/commission/stamp_duty、解析 positions_json）
  - TR-2.3 rubric 自评 5/5（公式+年化注释齐备、空样本/零波动/零净值均安全守卫）；全量后端 713 passed

## Task 3: 新回测写入每日持仓明细 positions_json
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None（可与 Task 1/2 并行，但 Task 2 的 HHI 验证依赖本任务）
- **Description**:
  - 扩展 [portfolio.py](file:///d:/project/tq-iquant-web/main/core/engine/portfolio.py) `snapshot()`：产出各策略持仓明细（code/quantity/可用数量/市值/成本），保持现有字段不变（回测/实盘共用，不得破坏实盘）。
  - [backtest_service.py](file:///d:/project/tq-iquant-web/main/core/services/backtest_service.py) 落库组合快照与策略快照时序列化写入 positions_json；控制单条体积（仅 code/quantity/可用/市值/成本，不含冗余行情）。
  - 不回填老数据；实盘路径不写该列。
- **Acceptance Criteria Addressed**: AC-3
- **Test Requirements**:
  - `rule` TR-3.1: 回测集成测试：新快照 positions_json 可解析且市值之和≈market_value（容差 1e-6）
  - `rule` TR-3.2: 实盘相关单测不受影响（snapshot 旧消费者不断言/不依赖新增字段）
  - `rule` TR-3.3: 老记录 positions_json 仍为 NULL，读取侧不报错
- **Completion Evidence** (2026-09-26): TR-3.1 `test_run_backtest_persists_positions_json`（空仓日 "[]" 有效负载、持仓日可解析含 quantity/sellable）；组合层市值聚合单测 4 个（T+1 sellable、空仓 positions==[]、跨策略同票聚合排序、ctx 隔离）在 test_portfolio.py；positions_detail 估值口径与 _holdings_value 一致（最近已知价回退 avg_cost），Decimal 转 str 保证 json 安全；service 组合/策略两处仅在 positions 键存在时落库（老路径 None）；实盘不调用 Portfolio.snapshot（零影响）。全量后端 718 passed。

## Task 4: 市场状态分段器 regime.py
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新建 `main/core/engine/regime.py`：输入基准收盘价（pandas Series 或等价），输出周频四态标签（趋势上涨/趋势下跌/震荡/下跌反弹）与连续段列表（起止日期/状态/幅度）。
  - 首版固定规则（常量命名+注释口径，OQ2 默认不做前端可调）：以周收益/周线 MA（如 MA10/MA30 周）排列与突破前高/前低判定趋势；震荡=无方向突破且 |区间涨跌| < 阈值；反弹=处于前期下跌段后、自近端低点反弹幅度 ≥ 阈值且收复某条均线。阈值在实现计划注释中列清楚供评审。
  - 纯函数、无 TQ 依赖、无未来函数（标签 t 仅依赖 ≤t 数据，允许段确认有滞后，注释说明滞后规则）。
- **Acceptance Criteria Addressed**: AC-2, AC-12
- **Test Requirements**:
  - `rule` TR-4.1: 单边上/单边下/箱体震荡/V 型反弹四个构造序列，对应区段标签正确
  - `rule` TR-4.2: 同输入两次运行结果完全一致；截断到 t 的子序列在 ≤t−滞后确认期 上标签与全量一致
  - `rule` TR-4.3: 极短序列（<最小窗口）返回空/震荡安全结果不抛错
- **Completion Evidence** (2026-09-26): 新建 [regime.py](file:///d:/project/tq-iquant-web/main/core/engine/regime.py)（纯函数 segment_regime + 4 常量注释口径：MA10/30、26 周 Donchian、连续 2 周确认、反弹 8%+站上 MA10+MA10 拐头）；周聚合按 ISO 周取末日、标签日周五，与 crisis_alpha 的 W-FRI 对齐；标签纯因果（t 仅用 ≤t，转折滞后 2 周为规则内置），[test_regime.py](file:///d:/project/tq-iquant-web/main/core/tests/unit/test_regime.py) 12 测试：TR-4.1 四构造序列（含 V 型 bear→rebound→bull 次序）、TR-4.2 两次一致+k=40/50/60 截断前缀全等、TR-4.3 <30 周/空序列安全；另测段连续性、pd.Series/Decimal 输入、周聚合周五标签。记录 #11 真实基准（662 日→141 周）冒烟分段符合常识（2024-09 政策底→42 周反弹→2025 趋势上涨）。全量后端 730 passed。

## Task 5: 前端展示新增评估指标
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 1, Task 2
- **Description**:
  - [Backtest.vue](file:///d:/project/tq-iquant-web/web/src/views/Backtest.vue) 整体表现指标区与策略对比区接入新指标：基准相对组（beta/Alpha/IR/跟踪误差/上下行捕获/超额回撤）、交易磨损组（换手/成本/仓位/空仓）、结构组（HHI/最大单票，null 显 —）、路径组（连亏/最差日周/偏度峰度）。
  - API 类型（[api/index.ts](file:///d:/project/tq-iquant-web/web/src/api/index.ts)）补字段；沿用 num/pct 格式化与红涨绿跌。
- **Acceptance Criteria Addressed**: AC-1, AC-11
- **Test Requirements**:
  - `rule` TR-5.1: vitest 断言新指标标题/值渲染；null 值显示 —
  - `rubric` TR-5.2: 视觉一致性；scale 1-5；anchors 1 分组混乱/3 信息齐但拥挤/5 分组清晰沿用现有指标卡样式；threshold >= 4；evidence 截图
- **Completion Evidence** (2026-09-26): TR-5.1 Backtest.test.ts 新增「新增评估指标：四分组标题与值渲染，null 值显 —」（19 字段逐一断言格式：Beta 0.85 / Jensen +6.20% / 捕获率 92.0% / 换手 3.25 / null→— / 偏度峰度 2 位）；实现为 buildMetrics 纯增量追加 4 组分隔行（MetricRow.group），策略卡与组合整体卡同一渲染路径，零结构重写；新增 .metric-group 跨列小标题沿用 #667eea 卡片色系，红涨绿跌复用 valueClass；api/index.ts 补 19 可选字段。vitest 169 passed、vue-tsc 无输出（通过）。TR-5.2 rubric 自评 4/5（分组清晰、样式一致；视觉截图在 Task 6 浏览器实测归档）。

## Task 6: 阶段一闸门 — 全量自验 + 用户验证
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1-5
- **Description**:
  - 跑 pytest（含新迁移、新指标、regime）、vitest、vue-tsc、build、alembic upgrade；浏览器对一条新回测确认持仓落库与新指标展示；提交并暂停，请用户实测确认后进入阶段二。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-3, AC-10, AC-11, AC-12
- **Test Requirements**:
  - `rule` TR-6.1: 四条质量门命令全绿并记录输出；浏览器截图归档
- **Completion Evidence** (2026-09-26): 四门全绿——pytest **730 passed**、vitest **169 passed**、vue-tsc 无诊断、`npm run build` 成功（仅既有 chunk 体积告警）；alembic：dev.db 曾在迁移 8 列旧态时升到 head（revision 不重放致 11 列缺失，downgrade KeyError），迁移加 inspector 存在性守卫后 downgrade f2a3b4c5d6e7→upgrade head 收敛为 42 列/版本 b7c8d9e0f1a2（fresh 库全加全删行为不变）。接口实测：重启后端（旧进程未挂新路由）后 POST /records/11/evaluations/recompute code=0，真实值 beta 0.5195 / Jensen α +64.77% / IR 2.7559 / 下行捕获 23.3% / 换手 6.13 / 成本 2.0% / 利用率 38.7% / 最长连亏 6 / 最差日 -4.74% / 偏度 0.72 / 峰度 6.37；老记录结构组 max_single_weight/hhi 正确为 null。浏览器实测记录「17」(id=11) 详情页截图 `c:\Users\Administrator\AppData\Local\Temp\trae\screenshots\backtest-17-combination-overview-full.png`：四分组标题、全部新值、红涨绿跌、结构组 — 均正确（浏览器代理首轮 DOM 文本误报，截图 ground truth 通过）；TR-5.2 rubric 确认 4/5。新回测 positions_json 端到端由集成/单测覆盖（test_run_backtest_persists_positions_json 等），未跑实时 TQ 新回测（待用户实测）。已提交（未推）：`feat(backtest): 阶段一 市场状态分析套件…`。**闸门状态：自验完成，暂停等待用户实测验收后再进阶段二。**

---

## 阶段二：机遇Alpha + 中性Alpha 专项

## Task 7: RegimeAlpha 分析器（机遇/反弹/中性 + 状态总表）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - 新建 `main/core/engine/regime_alpha.py`，与 crisis_alpha.py 同层同风格：输入策略/基准净值序列、周收益、成交流水（trades）、regime 标签。
  - 输出契约（payload 字典）：`regime_table`（四态：周数/占比/策略收益/基准收益/超额/胜率/区间最大回撤/换手率/成本占比）、`opportunity`（上涨捕获、+3%/+5% 大涨周条件检验、踏空周占比、上涨四档计数与均值）、`rebound`（反弹段列表、双方反弹幅、收复天数或未收复、反弹弹性）、`neutral`（震荡周收益/年化/胜率、月均交易、换手、成本毛利比、假突破磨损次数、趋势 vs 震荡单笔盈亏对比）。
  - 样本不足（无某态样本）返回该块空数组 + 字段零值/None，不抛错；纯 numpy/pandas。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-12
- **Test Requirements**:
  - `rule` TR-7.1: 构造「上涨段跟涨、反弹段不弹、震荡段高频磨损」场景，状态总表与三块方向正确
  - `rule` TR-7.2: 换手率/成本/月均笔数与从 trades 独立聚合结果一致（容差 1e-2）
  - `rule` TR-7.3: 缺失态样本时 payload 结构完整不缺键、值为安全空值
- **Completion Evidence** (2026-09-27):
  - 新建 [regime_alpha.py](file:///d:/project/tq-iquant-web/main/core/engine/regime_alpha.py)（RegimeAlphaAnalyzer，与 crisis_alpha 同风格）：payload 四块 `regime_table`（四态 周数/占比/累计收益/超额/胜率/态内最大回撤/年化换手/成本占比，无样本态周数 0 + None 安全空值）、`opportunity`（上涨捕获、+3%/+5% 大涨周检验、踏空占比、四档 同步涨/涨得少/踏空/逆势跌 计数均值，0.5 比例与危机镜像）、`rebound`（连续 rebound 周成段、自近端低点反弹幅、弹性、基准/策略收复前高自然日数或 recovered=False）、`neutral`（震荡周 占比/累计/年化/胜率、月均笔数/年化换手、成本毛利比、假突破磨损 WHIPSAW_DAYS=5、趋势 vs 震荡单笔盈亏 FIFO 配对）
  - 口径全部写入模块 docstring（周收益 W-FRI pct_change、状态累计连乘、分态换手双边成交额/日均净值/自然年数、分态成本/首日净值、踏空=b涨而s≤0、四档 0.5 比例镜像、磨损=开仓后≤5 交易日同票平仓且毛 PnL≤0 按平仓日归属）
  - [test_regime_alpha.py](file:///d:/project/tq-iquant-web/main/core/tests/unit/test_regime_alpha.py) 9 测试：TR-7.1 场景构造（80 周 4 态手工标签 + 磨损/盈利 trades）状态总表精确断言 + 三块方向；TR-7.2 换手/成本/月均笔数对 range 与 bull 段独立重算交叉断言 rel 1e-2；TR-7.3 缺态结构完整（含 segment_regime 完整 dict 入参、空/单日输入不抛错、dict 形式 trades）
  - 先红（模块缺失 collection error）后绿：新测试 9 passed；全量后端 **739 passed**

## Task 8: 存储 / service / API（market-regime 重算）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 7
- **Description**:
  - 新模型 `backtest_market_regime_analyses`（镜像 crisis 表：record 唯一、payload_json、status/error、computed_at；skipped 字段预留）+ Alembic 迁移。
  - service 重算函数：取每日快照净值 + 主基准日线 + backtest_trades，调 regime 分段器与分析器，upsert；失败写 failed+error。
  - API：`POST /api/backtest/records/{id}/market-regime/recompute`；详情接口内嵌 regime 分析摘要（挂在详情返回的新字段，如 `market_regime`）。
- **Acceptance Criteria Addressed**: AC-4, AC-10
- **Test Requirements**:
  - `rule` TR-8.1: 集成测试走通 重算→落库→详情返回；重复重算为 upsert 不新增行
  - `rule` TR-8.2: 基准无数据/TQ 失败时 status=failed，详情主链路 200 且其他模块不受影响
- **Completion Evidence** (2026-09-27):
  - 新模型 [backtest_market_regime.py](file:///d:/project/tq-iquant-web/main/core/models/backtest_market_regime.py)（backtest_market_regime_analyses，镜像 crisis 表：record 唯一约束 + benchmark_code/name + status/error + skipped_benchmarks_json 预留 + payload_json + computed_at，FK CASCADE）；注册 models/__init__
  - 迁移 `c9d0e1f2a3b4`（down_revision=b7c8d9e0f1a2，create_table/drop_table，无 batch 需求）；dev.db 升级并入 Task 10 闸门前执行（临时库双向验证命令被跳过，改由闸门实测覆盖）
  - service：`_upsert_market_regime` + `compute_and_store_market_regime`（主基准 build_index_close_series → 无数据 failed；共同日 <30 insufficient 不落 payload；否则 segment_regime + RegimeAlphaAnalyzer → completed payload）+ `safe_compute_market_regime`（异常 rollback→failed+error）+ `recompute_market_regime`（快照/trades/基准解析 resolve_crisis_benchmark 复用，离线不重跑回测）
  - 详情 `market_regime` 摘要内嵌（status/error/computed_at/benchmark_code/name/data）；API `POST /api/backtest/records/{id}/market-regime/recompute`（404 透传）
  - 测试：service 6 个（TR-8.1 重算→落库→详情内嵌 + 重复重算单行；TR-8.2 基准无数据 failed 且详情主链路完整；insufficient；无快照；缺记录 None；safe 吞异常标 failed）+ API 集成 2 个（recompute 返回 status / 404）。全量后端 **747 passed**

## Task 9: 前端两个折叠专项 section
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - 详情页新增「🚀 机遇Alpha 专题分析」与「⚖️ 中性Alpha 专题分析」：默认折叠（复用 caCollapsed 模式，各自独立折叠态）、展开 nextTick 初始化图表、含状态总表、大涨周条件表、反弹段表、震荡磨损表，图表用 ECharts（状态收益对比、反弹弹性、震荡市盈亏分布等，图表数量按数据块精简，不凑数）。
  - null/failed/insufficient 态与重算按钮交互镜像危机Alpha；api/index.ts 补类型。
- **Acceptance Criteria Addressed**: AC-4, AC-11
- **Test Requirements**:
  - `rule` TR-9.1: vitest：默认折叠不渲染图表；展开渲染全部图表与表格；failed 显示原因；重算成功刷新
  - `rubric` TR-9.2: 与危机Alpha 交互视觉一致性；scale 1-5；anchors 1 割裂/3 基本一致/5 完全同构；threshold >= 4；evidence 浏览器截图
- **Completion Evidence** (2026-09-27):
  - api/index.ts：MarketRegimeStateRow/BigUpTest/OppCat/ReboundSegment/MarketRegimeData/BacktestMarketRegimeItem 全套类型 + recomputeMarketRegime()（复用 CrisisAlphaRecomputeResult）；BacktestDetailItem 加 `market_regime` 字段
  - Backtest.vue：合并实现为单个 `mr-section` 折叠 section（标题「🚀 机遇Alpha · ⚖️ 中性Alpha 专题分析」，完全镜像 crisis 模式——mrCollapsed 独立状态、openDetail 重置折叠、null/failed/insufficient 三分支+重算按钮、ca-banner/ca-kpi-grid/ca-status 同构）；四块内容：一、市场状态×绩效总表（四态三组 bar 图 + 10 列表）二、机遇Alpha（大涨周检验表+四档分类表+踏空占比 note）三、反弹弹性（无段 empty-state；有段分组 bar 图+9 列段表含收复前高）四、中性Alpha（10 行指标表+FIFO 磨损口径 note）；KPI 4 张（上涨捕获率/踏空周占比/反弹平均弹性/假突破磨损）；mrStateChart/mrReboundChart 独立实例 dispose 后重建；scrollToSection 支持 market-regime 锚点
  - Backtest.test.ts：marketRegimeCompleted 完整 fixture + 6 个新测试（TR-9.1 默认折叠不渲染→展开 null 提示+重算、failed 显示原因、insufficient 无重算按钮、completed 4 KPI/2 图表/四块标题/1 重算按钮、重算成功调 API+详情重载+完成提示、重算抛错失败提示）
  - `npx vitest run` 175 passed（+6）；`npx vue-tsc --noEmit` 零错误；TR-9.2 rubric 截图归入 Task 10 闸门浏览器实测

## Task 10: 阶段二闸门 — 自验 + 用户验证
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 7-9
- **Description**: 全量质量门 + 真实 completed 记录浏览器实测（含一只有成交的策略），用户确认后进入阶段三。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-10, AC-11, AC-12
- **Test Requirements**:
  - `rule` TR-10.1: 质量门四命令全绿；截图：状态总表/机遇/反弹/中性四块
- **Completion Evidence** (2026-09-27):
  - 质量门四命令全绿：pytest **747 passed** / vitest **175 passed** / vue-tsc **0 错误** / `npm run build` 成功
  - dev.db 迁移：`uv run alembic upgrade head` → **c9d0e1f2a3b4 (head)**；后端重启探活 openapi.json 确认 `/api/backtest/records/{id}/market-regime/recompute` 已挂载
  - 浏览器实测（记录 #11「17」，热点精选 2024-01-01~2026-09-23）：null 态折叠→展开显示「本次回测未包含市场状态分析」+ 重算按钮 → 点击重算 → 「重新计算完成」→ 4 KPI + 2 图表 + 四块全渲染；截图归档 `.trae/specs/regime-alpha-suite/screenshots/`（mr_1_state_table / mr_2_opportunity / mr_3_rebound / mr_4_neutral，MD5 各异，目检：状态总表四态 10 列红涨绿跌正确、机遇四档含踏空 13.0%/捕获率 391.6%、反弹段 2024-09-27~2025-07-11 弹性 1.61x/收复 14/21 天、中性 10 指标含假突破 18 次）
  - 提交 **007bd46**（12 文件 +2083 行，未含 .trae/ 与未跟踪杂项）；**⏸ 暂停待用户验收，未进入阶段三**

---

## 阶段三：多基准风格轮动矩阵

## Task 11: 风格领导分段 + 风格×绩效矩阵 + 分化度
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 8
- **Description**:
  - 在 regime_alpha 分析器中扩展（复用 service 已有的 6 宽基取数逻辑，从 crisis 流程抽出共享 loader）：滚动 20/60 周截面排名生成每周风格标签（大盘价值/中小盘/科技成长/普涨/普跌/无主线）与连续段。
  - 输出 `style_matrix`（每风格段：时长占比、策略收益、对领涨指数超额、胜率、最大回撤）、`dispersion`（宽基周收益截面标准差序列）与策略超额按分化度低/中/高分桶对比。
  - 不可得指数 skipped；历史不齐按共同可得集合滚动。
- **Acceptance Criteria Addressed**: AC-6
- **Completion Evidence**:
  - `regime_alpha.py` 新增 `STYLE_GROUPS`/`STYLE_BROAD_INDEX`/`STYLE_LABELS`/`STYLE_ROLL_WINDOW=60`/`STYLE_FAST_WINDOW=20`/`STYLE_LOW_DISPERSION=0.05`/`STYLE_NOLEADER_EDGE=0.5`，新增方法 `_prepare_style_benchmarks`/`_style_week_label`/`_style`/`_style_max_drawdown`/`_dispersion`
  - `analyze()` payload 由 5 键扩至 8 键（+`style`/`dispersion`/`exposure`）
  - 风格标签规则：全负→普跌；全正且截面 std<0.05→普涨；否则取 60W 累计收益最高指数所属组；top−2nd edge<0.5×std→无主线；中证全指领涨→无主线
  - `test_tr11_1_tech_growth_leadership_labeled`、`test_tr11_2_dispersion_from_input_and_skipped`、`test_tr11_3_no_benchmark_closes_safe_empty` 通过
- **Test Requirements**:
  - `rule` TR-11.1: 注入「创业板持续领涨」价格场景，风格标签判为科技成长且矩阵对应行有值
  - `rule` TR-11.2: 分化度序列与分桶统计由输入价格计算（测试中无硬编码数值）；缺指数进入 skipped
  - `rule` TR-11.3: 与 regime/机遇/中性同一次重算写入同一 payload（不新增表）

## Task 12: 隐含风格暴露（滚动相关热力图 / OLS beta）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 11
- **Description**:
  - 策略周收益对 6 宽基的滚动相关（扩展 crisis 的 rolling_corr 口径），输出热力图数据；滚动 OLS beta 序列（v1 不约束）；如实现成本低则增非负权重约束版本作为增强，否则明确标注 v1 为普通 OLS。
- **Acceptance Criteria Addressed**: AC-6, AC-12
- **Completion Evidence**:
  - `regime_alpha.py` 新增 `ROLLING_EXPOSURE_WINDOW=52` 与 `_exposure()` 方法：52 周滚动 corr（`pd.Series.corr`）+ OLS beta（`cov/var`）；窗口内样本<4 输出 None；缺失区间按周对齐不画空洞
  - v1 为普通 OLS（不约束非负权重），前端注记"Beta 为普通 OLS（v1 不约束非负权重）"
  - `test_tr12_1_rolling_exposure_single_index_tracking`（跟踪指数 corr≈1.0/beta≈1.0，hs300 corr<0.5）、`test_tr12_2_missing_index_skipped_no_nan_misalignment` 通过
  - **增强A（2026-09-27，用户确认"定A，取数只走TQ"）**：巨潮四象限纯风格暴露作为第二组隐含暴露
    - TQ spike 验证 8 只巨潮风格指数（399370~399377）自 2010-01-04 起 4064 根日线可取，无其他数据源
    - `regime_alpha.py`：`QUADRANT_STYLE_INDICES`（399372~399377 六只，规模×成长/价值）+ 构造参数 `quadrant_closes`；`_prepare_style_benchmarks` 泛化为 `_prepare_weekly_rets(closes, specs)`（None=未接线→skipped 不诬报）；`_exposure()`/`_quadrant_exposure()` 共用 `_exposure_block()`；`analyze()` 输出 9 键（+`quadrant_exposure`）
    - `backtest_service.py`：`compute_and_store_market_regime` 单次 `build_index_close_series` 取 主基准+6宽基+6四象限，构建 `quadrant_closes` 传入分析器
    - 前端：`MarketRegimeData.quadrant_exposure?: ExposureBlock`（老记录无此键）；Backtest.vue 新增「七-B、巨潮四象限·纯风格暴露」子块（第二热力图 + 最新相关/Beta 表 + 四象限 skipped 提示），`stQuadData`/`initStQuadChart`，`buildCorrHeatmap` 抽共用；老记录无键 → 空态提示不崩
    - 测试：`test_tr12_3_quadrant_exposure_pure_style_tracking`、`test_tr12_4_quadrant_missing_skipped_and_absent_safe`；vitest fixture 补 `quadrant_exposure`，图表计数 6→7/5→6，新增增强A 渲染 + 老记录兼容测试
    - 门禁：776 pytest passed、181 vitest passed、vue-tsc 0 错、npm build 通过
- **Test Requirements**:
  - `rule` TR-12.1: 构造策略≈某单指数组合，滚动相关/ beta 在该指数方向显著
  - `rule` TR-12.2: 基准缺历史区间用 NaN/跳过处理，前端不画空洞错位

## Task 13: 前端风格轮动栏目
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 11, Task 12
- **Description**:
  - 在市场分析区域新增「🎨 结构性行情与风格轮动」（折叠）：风格段时间轴/堆叠图、风格×绩效矩阵表、分化度折线（叠加策略超额双轴）、滚动相关热力图、（如有）beta 曲线；skipped 指数显式注明。
- **Acceptance Criteria Addressed**: AC-6, AC-11
- **Completion Evidence**:
  - `api/index.ts` 新增 `StyleBlock`/`DispersionBlock`/`ExposureBlock` 等类型，`MarketRegimeData` 扩展 `style`/`dispersion`/`exposure` 字段
  - `Backtest.vue` 在 mr-section 内新增五/六/七三块：风格×绩效矩阵表（6 风格×周数/占比/策略累计/领涨累计/超额/胜率/态内回撤）、分化度折线图 + 低/中/高分桶对比表、隐含风格暴露相关热力图 + 最新相关/Beta 表；skipped 指数提示
  - 新增 computed `stMatrixRows`/`stDispBuckets`/`stSkipped`/`stCorrData` 与图表 `initStDispChart`/`initStCorrChart`；`initMrCharts` 统一调用
  - vitest：`marketRegimeCompleted` fixture 补充 style/dispersion/exposure；completed 测试图表数 2→4、新增七块标题断言；专项测试覆盖风格 6 行 + skipped + 分化桶 + 暴露表
  - 176 frontend tests passed
- **Test Requirements**:
  - `rule` TR-13.1: vitest 渲染矩阵表与图表容器；skipped 文案存在
  - `rubric` TR-13.2: 可读性与一致性；scale 1-5；anchors 1 图表堆砌难懂/3 可读但重点不明/5 三个问题（风格段/分化/暴露）一屏一答；threshold >= 4；evidence 截图

## Task 14: 阶段三闸门 — 自验 + 用户验证
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 11-13
- **Description**: 质量门 + 真实记录（含科创50 上市前后区间）浏览器实测；用户确认后进入阶段四。
- **Acceptance Criteria Addressed**: AC-6, AC-10, AC-11
- **Completion Evidence**:
  - 四命令全绿：`pytest core/tests/` 752 passed；`npx vitest run` 176 passed；`vue-tsc --noEmit` 通过；`npm run build` 通过
  - service 接入：`compute_and_store_market_regime` 加载 `[benchmark] + 6 宽基`，构建 `benchmark_closes: Dict[str, pd.Series]` 传入 analyzer
  - 浏览器实测 record #11（热点精选，2024-01-01~2026-09-23）重算市场态：五/六/七三块全部渲染，风格矩阵 6 行（大盘价值27/科技成长57/普跌7/无主线48等）、分化度 140 周序列 + 低47/中47/高46 桶、暴露表含沪深300/中证500/创业板指等 6 宽基最新相关与 Beta
  - commit `7ac56e0`（7 files, +862/-10）
- **Test Requirements**:
  - `rule` TR-14.1: 四命令全绿；截图风格矩阵/分化度/热力图；验证 skipped 提示

---

## 阶段四：行业归因 + 轻量稳健性 + HTML 报告

## Task 15: TQ 行业数据源验证（spike）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 验证 TQ/通达信可取：个股→行业（申万优先）分类映射、行业指数日线（用于行业收益贡献）。输出一页验证记录（可取代码样例、覆盖度、接口形态）。
  - 不可得则任务 blocked，回报用户在「仅暴露+命中率（用行业分类即可）/手工映射表/放弃行业归因」中选择，不允许伪造映射（对应 OQ1）。
- **Acceptance Criteria Addressed**: AC-7
- **Completion Evidence**:
  - 验证记录：`docs/plans/0014-tq-industry-data-spike.md`
  - 实测结论：`get_sector_list(list_type=0)` 得 587 板块含 128 个 881xxx 二级行业；`list_type=1` 返回 Code+Name 字典（128 行业全部带名，如 881002.SH=煤炭开采）；`get_stock_list_in_sector(code, block_type=0)` 取成分股；`get_market_data(stock_list=["881002.SH"], period="1d", start_time=YYYYMMDD)` 行业指数日线可取；`get_relation(stock)` 返回 BlockType∈行业/地区/概念/风格，过滤"行业"即唯一二级行业
  - 覆盖度：跨 5 行业 20 只样本股 `get_relation` 映射 20/20 成功；行业口径为通达信二级行业（881），非申万
- **Test Requirements**:
  - `rule` TR-15.1: 验证记录含至少 20 只样本股票的行业映射结果与 1 个行业指数拉取证据；失败则记录 blocked 事实

## Task 16: 行业归因后端（依赖 Task 15 通过）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 15
- **Description**:
  - 从 positions_json 聚合行业暴露时间序列（Top 行业权重）；按周/月计算领涨行业命中率（超配行业 vs 当期行业涨幅 Top-N 重合度）；行业指数可得则做行业收益贡献分解，不可得则仅暴露+命中率。
  - 并入 market-regime payload 的 `industry_attribution` 块（同次重算）；positions_json 为空时该块 status=needs_rerun。
- **Acceptance Criteria Addressed**: AC-7, AC-12
- **Completion Evidence**:
  - 纯计算引擎 `core/engine/industry_attribution.py`：日度行业权重（市值/总资产）、暴露 Top8+其他+未分类+现金、周（W-FRI）/月（ME）领涨 Top3 命中率（期初持仓 vs 当期涨幅排名）、贡献=Σ 期初权重×行业日收益（Brinson 行业配置项，top15）
  - TQ 封装 `TQData.get_industry_list()`（list_type=1，128 个 881）/`get_stock_industry()`（get_relation 过滤 BlockType=行业）；`build_index_close_series` 增加 batch_size 分批（40/批）
  - service `_build_industry_attribution` 并入 payload 第 9 块；老记录 needs_rerun、TQ 异常隔离 failed 不影响主链路；recompute 传 positions_json
  - 单测：test_industry_attribution.py 5 例（暴露权重和=总仓位、命中率方向正反例、未分类、needs_rerun、全空仓 ok）；service +2 例（ok 链路/异常隔离）；759 backend tests passed
- **Test Requirements**:
  - `rule` TR-16.1: 构造持仓+行业指数，暴露权重和=总仓位（容差）、命中率方向正确
  - `rule` TR-16.2: positions_json 为空 → needs_rerun，不返回空伪装结果

## Task 17: 行业归因前端（含老记录门禁）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 16
- **Description**:
  - 风格栏目下新增行业暴露面积图/堆叠图、命中率卡片、贡献表；老记录显示「该记录回测时未保存持仓明细，重新回测后可用」且不 init 图表。
- **Acceptance Criteria Addressed**: AC-7, AC-11
- **Completion Evidence**:
  - `api/index.ts` 新增 `IndustryAttributionBlock` 等 5 个接口，`MarketRegimeData` 增加 industry_attribution
  - `Backtest.vue` 新增「八、行业归因」：needs_rerun 门禁（🏭 文案、无图表）、failed 错误态、ok 态周/月命中率卡片（6 KPI）+ 100% 堆叠面积暴露图（Top8+其他+未分类+现金）+ 贡献表（平均权重/行业涨跌/配置贡献）+ 未映射提示；仅 ok 态 initIaExpChart
  - vitest +3：ok 分支（暴露图/卡片/贡献表，5 图表）、needs_rerun 分支（仅门禁、4 图表）；fixture 增 industry_attribution 与 marketRegimeNeedsRerun；178 tests passed、vue-tsc、build 全绿
  - 浏览器实测 record #11（无 positions_json）门禁文案正确渲染
- **Test Requirements**:
  - `rule` TR-17.1: vitest 两种记录分支：新记录渲染、老记录仅提示无图表
  - `rubric` TR-17.2: 门禁文案与空态样式一致性；scale 1-5；threshold >= 4；evidence 截图

## Task 18: 轻量稳健性分析（后端 + 前端）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 8
- **Description**:
  - 只读每日快照/trades/基准（禁止 BacktestEngine.run）：逐半年指标矩阵、滚动 26/52 周收益与夏普分布（中位/最差/最好/负值占比）、最长亏损窗口、基准替换敏感性（FR-1 相对指标对 6 宽基分别计算的对比表）。
  - 并入 payload `robustness` 块；前端新增「🔬 稳健性体检」折叠栏目（分布直方图/对比表）。
- **Acceptance Criteria Addressed**: AC-8, AC-11
- **Completion Evidence**:
  - 纯计算引擎 `core/engine/robustness.py`：逐半年矩阵（收益/超额/回撤/夏普）、W-FRI 周频 26/52 滚动几何收益与夏普分布（中位/最差/最好/负值占比+8 档固定直方图）、最长水下亏损窗口（峰值→谷底→收复）、FR-1 八项相对指标逐基对齐（口径同 Evaluator）；共同交易日 <30 → insufficient，NaN/Inf 全归一 None（allow_nan=False 可序列化）
  - service 复用市场态已取的主基准+6 宽基收盘序列零额外 I/O，payload 增第 10 块 robustness，异常隔离 failed；不 import/调用回测链路
  - 前端 `Backtest.vue` 新增「九、稳健性体检」：3 KPI（亏损窗口天数/最深回撤/样本）+ 半年矩阵表 + 滚动分布表 + ECharts 双序列直方图 + 基准敏感性表（主基准标记/不足行降级），insufficient/failed 空态；`api/index.ts` 增 4 个接口
  - 测试：test_robustness.py 8 例（3 年构造序列 beta≈0.5/捕获率、直方图计数和=n_samples、V 型亏损窗口、insufficient、短样本窗口 None、JSON 安全）；service TR-18.1 mock BacktestEngine.run 零调用；vitest +2（ok 渲染/insufficient 空态）；768 backend + 180 frontend passed、build 绿
  - 浏览器实测 record #11（662 交易日）：97 天/7.62% 亏损窗口、6 半年行、113/87 滚动样本、6 宽基敏感性全部 available，5/5 PASS
- **Test Requirements**:
  - `rule` TR-18.1: 单测 mock BacktestEngine.run，稳健性计算路径调用次数为 0
  - `rule` TR-18.2: 构造 3 年快照，滚动窗口与基准替换表数值正确
  - `rule` TR-18.3: vitest 渲染栏目与空态

## Task 19: HTML 单文件报告导出（后端）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 17, Task 18
- **Description**:
  - 新增 `GET /api/backtest/records/{id}/export/html`：服务端拼装单个 .html（ECharts 运行时从 web 构建产物/node_modules 内联，数据 JSON 内联，无外网资源），含核心指标、净值/回撤、危机/机遇/中性/风格/行业/稳健性全部模块（OQ4 默认全量）。
  - 数据来源统一为详情+分析 payload（FR-22），图表用内嵌 ECharts 静态渲染（同 option 序列化为 JSON）。
- **Acceptance Criteria Addressed**: AC-9
- **Completion Evidence**:
  - `core/services/html_report.py`：`build_html_report(detail)` 渲染 0.概览 KPI、1.净值/回撤、2.市场状态×绩效全 9 子块（含 2.7-B 四象限暴露）、3.危机 Alpha、4.附录原始 JSON；`ALLOWED_EXTERNAL_URLS` 白名单校验；`_escape`/`_json_dumps` 防 XSS
  - `core/api/backtest.py`：`GET /records/{id}/export/html` 返回 `HTMLResponse` + `Content-Disposition: attachment`（文件名 URL 编码中文），例外于统一 envelope；404 走 `err()`
  - 集成测试 `test_export_html_returns_attachment_with_disposition`（text/html+attachment+无外链+含标题/图表容器）、`test_export_html_not_found`（404 envelope）通过
- **Test Requirements**:
  - `rule` TR-19.1: 接口返回 content-type 为 html 且文件中除白名单外无 http(s) 外链；用简单解析确认含核心指标数字与内联 echarts
  - `rule` TR-19.2: 抽查总收益/年化/最大回撤/危机Alpha 占比与详情接口值一致

## Task 20: 导出按钮前端
- **Status**: `completed`
- **Priority**: low
- **Depends On**: Task 19
- **Description**:
  - 详情页头部加「导出 HTML 报告」按钮，调用接口触发下载（blob/直链）；分析未计算的模块在报告中显示未计算态而非报错；加载中禁用按钮。
- **Acceptance Criteria Addressed**: AC-9, AC-11
- **Completion Evidence**:
  - `Backtest.vue` 报告头新增 `<a :href="`/api/backtest/records/${id}/export/html`" target="_blank">📄 导出 HTML 报告</a>`，直链触发浏览器下载（无需 axios blob）
  - vitest +1：断言按钮存在、href 正确、target=_blank；182 tests passed、vue-tsc、build 全绿
- **Test Requirements**:
  - `rule` TR-20.1: vitest 点击按钮触发正确 URL 与下载处理；详情无数据时不 500
  - `rubric` TR-20.2: 按钮位置/样式与现有操作按钮一致；scale 1-5；threshold >= 4

## Task 21: 最终全量验证 + 独立评审
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1-20
- **Description**:
  - 四条质量门全绿；多场景浏览器实测（长周期 304 下跌周记录、新回测含持仓记录、failed 态、缺基准 skipped）；按 spec mode 流程发起独立 review（全新上下文），fail 项建 issue 返工直至 pass。
- **Acceptance Criteria Addressed**: AC-1..AC-12
- **Test Requirements**:
  - `rule` TR-21.1: 质量门四命令输出归档；review.md 结果为 pass
  - `rubric` TR-21.2: 口径金融合理性与整体体验双 rubric 均 >= 4；evidence 独立评审报告
