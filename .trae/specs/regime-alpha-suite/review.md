# 独立评审报告 — 回测管理增强·regime-alpha-suite

评审人：独立评审员（未参与实现）
评审日期：2026-09-27
质量门实测（评审当场复跑）：`uv run pytest core/tests/` **780 passed**；`npx vitest run` **182 passed（11 文件）**。

## 评审范围

- 规格与任务：`.trae/specs/regime-alpha-suite/spec.md`、`tasks.md`（Task 1–20 完成声明）
- 后端引擎：`main/core/engine/regime_alpha.py`（934 行）、`robustness.py`、`industry_attribution.py`、`regime.py`、`evaluator.py`（新增指标段）、`crisis_alpha.py`（镜像对照）
- 服务/API：`main/core/services/backtest_service.py`（`compute_and_store_market_regime`、`recompute_market_regime`、`recompute_evaluation` 策略层扩展、`_json_safe`、`_build_industry_attribution`）、`main/core/services/html_report.py`、`main/core/api/backtest.py`（3 个新端点）
- 前端：`web/src/views/Backtest.vue`（市场状态 9 子区块、needs_rerun 门禁、稳健性紧凑双栏、导出按钮）、`web/src/api/index.ts`
- 测试：`test_regime_alpha.py`（16）、`test_robustness.py`（8）、`test_industry_attribution.py`（5）、`test_html_report.py`（6）、`test_backtest_service.py`（market-regime/evaluation 相关）、`web/src/__tests__/Backtest.test.ts`（市场状态专项 describe 11 例）

## 发现的问题（按严重级）

### Blocker
无。

### Major
无。

### Minor
1. **隐含暴露 OLS beta 存在 ddof 系统性偏差**（`regime_alpha.py` `_exposure_block` L926-931）：`sx.cov(bx)` 用 pandas 默认 `ddof=1`，而分母 `bx.var(ddof=0)`，二者自由度不一致 → beta = (n/(n-1))·真实 OLS beta。满窗 52 周时高估约 2%；窗口预热期 n=4 时高估 33%。对比：`evaluator._compute_relative_metrics` 与 `robustness._relative_metrics` 均分子分母同 ddof（比值精确）。该值仅用于七/七-B 热力图与最新 Beta 表（且前端已注明"v1 不约束非负权重"），不进核心指标，故定 minor。修法：统一 ddof（如 `bx.var()` 默认 ddof=1）。
2. **FR-13「20/60 周双窗口」未完整实现**：`STYLE_FAST_WINDOW=20` 仅用作最小样本闸门（`len(common) < 20` 返回空矩阵），未按 spec 输出 20 周窗口的第二组排名/标签，实际只有 60 周（早期扩张窗）单窗口结果。功能可用但与 spec 条款有偏差。
3. **HTML 报告三处标签口径不严谨**（`html_report.py`）：
   - `_regime_states` 表头「日胜率」，实际 `win_rate` 是周收益胜率（L436 `sum(1 for r in s_rets if r>0)` 基于周收益）；
   - `_neutral` KPI「震荡区间基准幅度」取的值是 `range_ratio`（震荡周数占比），名不符实；
   - `_neutral` KPI「成本/总收益」实为 `cost_gross_ratio`（成本/平仓毛利）。
4. **Task 19 完成证据声称渲染「4.附录原始 JSON」，代码中不存在该附录**（`build_html_report` 仅 概览+净值+危机+市场状态 四部分）。spec FR-21 未要求附录，故仅为任务证据与代码不符，非功能缺陷。
5. **相对指标存在两套平行实现**（`evaluator._compute_relative_metrics` vs `robustness._relative_metrics`），口径靠注释承诺一致，长期有漂移风险（当前逐行比对一致）。

### Nit
1. 分态年化换手/月均笔数的「自然年数」用该态首末交易日跨度（`_state_turnover_cost` L367、`_neutral` L632-637），对跨段分散的态会低估年化强度；模块 docstring 已明确固化口径，属已知近似。
2. `worst_weekly_return` 为「周末值/周首值-1」周桶口径，不含上周末收盘到本周首日的跳空；已注释。
3. 行业归因一次性拉取全部 128 个行业指数日线（分批 40/批），NFR-6 的 P95≤5s 在本机未能复测，存在超预算风险；但老记录无 positions_json 时在 TQ 调用前即短路 needs_rerun，不会白拉取。
4. HTML 导出端点未校验记录状态（running 记录也可导出半成品报告），单用户本地工具可接受。
5. `_echarts_source()` 运行时依赖 `web/node_modules/echarts/dist/echarts.min.js` 存在；生产若裁剪 node_modules 会 500，建议（非必须）回落到 vendored 副本。

## Rubric A 评分（口径金融合理性）— 平均 4.4

| 项 | 分 | 证据 |
|---|---|---|
| A1 指标定义正确 | 4 | beta/Jensen α(CAGR 口径, rf=0.02)/年化超额/TE/IR/上下行捕获/超额回撤在 evaluator 与 robustness 逐行核对均标准且互相一致；周收益 W-FRI、状态累计连乘、最大回撤峰谷口径正确并有构造序列精确断言（test_evaluator 5 例、test_robustness 8 例、TR-7.1 状态总表精确值）。扣分点：`_exposure_block` beta 的 ddof 混用（Minor-1）；机遇「上涨捕获率」为均值之比而非经典捕获率（已注释口径）。 |
| A2 危机/机遇/中性镜像对称 | 4 | 大涨周阈值 +3%/+5% 与危机 `DEEP_WEEK_TESTS=(-0.03,-0.05)` 精确镜像；四档分类 0.5 比例双侧均有（危机 跌得少 ratio<0.5，机遇 同步涨 s≥0.5b、踏空/逆势跌分界 -0.5b）。扣分点：危机负侧按 ratio 1.0 再分「跟着跌/跌更多」，机遇负侧按 0.5b 分「踏空/逆势跌」，并非逐档严格镜像，docstring「镜像对称」表述略强于实现。 |
| A3 样本不足/缺失安全 | 5 | 全链路门禁完整：相对指标 <20 对齐日全 None；市场态 <30 共同交易日 → insufficient 不落 payload；无某态样本 → 周数 0 + None 安全空值（TR-7.3）；positions_json 缺失 → needs_rerun（TR-16.2，不产出伪装结果）；不可得指数进 skipped 且前端注明（TR-11.2/TR-12.2）；rolling 窗口样本不足整窗 None + 直方图全 0。 |
| A4 NaN/Inf 边界 | 5 | `_json_safe` 在落库边界递归清洗（仅 float NaN/±Inf→None，不吞其他类型），`allow_nan=False` 严格 JSON 兜底；robustness 内部 `_r4` 归一 + 专测 `test_payload_is_json_safe_without_nan`；零方差 beta/corr 有守卫（var<1e-15→None）；HTML 侧 `</`→`<\/` 防 script 逃逸。未发现过度吞错。 |
| A5 A 股规则 | 4 | FIFO 配对正确（卖超忽略、按平仓日归属磨损、span 按交易日计，WHIPSAW_DAYS=5 个交易日口径）；T+1 由引擎层强制（本套件为分析层，不重跑不回填）；换手率统一「双边成交额/日均净值/自然年」并在 evaluator/regime_alpha 两处一致（TR-7.2 独立聚合交叉断言 rel 1e-2）。扣分点：双边口径与常见单边换手习惯不同，虽已注释但易误读。 |

## Rubric B 评分（整体体验）— 平均 4.4

| 项 | 分 | 证据 |
|---|---|---|
| B1 信息层级与紧凑度 | 4 | 单一折叠 section 内 9 个编号子区块（总表/机遇/反弹/中性/风格/分化/暴露/四象限/行业/稳健性），默认折叠 + nextTick 初始化、chart dispose 重建，与危机 Alpha 完全同构（复用 ca-banner/ca-kpi-grid/ca-status 样式）；稳健性 rb-grid 半年矩阵与滚动分布两栏并排；KPI 4 卡收敛。扣分点：单 section 9 子块信息密度高，展开后页面很长（有 scrollToSection 锚点缓解）。 |
| B2 空态/失败态/门禁可理解性 | 5 | null（未计算+重算按钮）/failed（红字原因+重算）/insufficient（灰字原因、无重算按钮，与危机一致）/needs_rerun（🏭「该记录回测时未保存持仓明细，重新回测后可用」，不 init 图表）四态齐全，skipped 指数逐块注明，四象限老记录无键走空态不崩。vitest 对每个分支均有用例。 |
| B3 导出报告完整性与自包含 | 4 | 单文件内联 ECharts（lru_cache 读 node_modules）+ 全部 option/数据 JSON 内联；白名单外链测试通过（`test_tr19_1_no_external_links_outside_whitelist`）；数字同源（`test_tr19_2` 抽查总收益/年化/回撤/危机占比与详情一致）；未计算模块显示未计算态不报错；Content-Disposition attachment 中文名 URL 编码。扣分点：Minor-3 三处标签口径、Minor-4 附录缺失。 |
| B4 测试覆盖充分性 | 5 | 后端：regime_alpha 16 例（含 TR-7.2 换手/成本/月均笔数独立聚合交叉断言、TR-7.3 缺态结构）、robustness 8 例（含 TR-18.1 mock BacktestEngine.run 零调用、JSON 安全）、industry 5 例（正反命中率/未映射/needs_rerun）、html_report 6 例、service/API 集成（重算→落库→详情内嵌、upsert 单行、failed 不阻主链路、404）。前端：市场状态专项 11 例覆盖折叠/三态/9 块渲染/重算成败/行业门禁双分支/稳健性空态。评审当场复跑 780+182 全绿。 |
| B5 代码可维护性 | 4 | 常量全部集中命名并注口径（regime.py 5 常量、regime_alpha 10+ 常量、模块 docstring 固化「勿随手感改」）；引擎纯计算层与 TQ I/O 层分离（industry_attribution 无 TQ/DB 依赖可单测）；`_EVALUATION_FIELDS` 表驱动消除三处漂移；`_exposure_block` 抽共用给宽基/四象限复用。扣分点：Minor-5 相对指标双实现漂移风险；regime_alpha.py 单类 934 行承载 9 个 payload 块，继续加块建议拆分。 |

## 结论

**Pass**。两项 rubric 均 ≥4（A=4.4，B=4.4），无 blocker/major。质量门复跑通过（pytest 780 / vitest 182）。

返工建议（不阻塞验收，建议后续迭代处理，按优先级）：
1. 修 `_exposure_block` beta 的 ddof 混用（一行改动，消除 2%~33% 系统性高估）。
2. 修正 HTML 报告三处标签（日胜率→周胜率、震荡区间基准幅度→震荡周占比、成本/总收益→成本/毛利）。
3. 补齐 FR-13 双窗口（20 周）输出，或将 spec 条款与实现对齐（改为单窗口 + 最小样本闸门的文字修订）。
4. 将相对指标计算下沉为 evaluator/robustness 共用函数，消除双实现漂移风险。
