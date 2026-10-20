# 市场状态分析套件（机遇Alpha / 中性Alpha / 风格轮动 / 归因 / 稳健性 / 报告导出） - PRD

## Overview
- **Summary**: 在现有「绝对收益指标 + 危机Alpha 防守专项」之上，补齐对称的进攻与震荡能力分析、多基准结构性行情分析、持仓归因、轻量稳健性与单文件报告导出，形成「基准相对指标 + 牛/熊/震荡/反弹四状态 + 风格结构」的完整绩效分析体系。
- **Purpose**: 让单用户本地量化平台能回答三类目前回答不了的问题——①基准上涨/反弹时策略赚不赚（机遇）；②基准震荡时策略能否不磨损地赚钱（中性）；③结构性行情（科创热、300 强、中小盘行情）中策略表现与风格暴露。
- **Target Users**: 平台唯一用户（策略作者本人），用于策略评估、调参与复盘。

## Goals
- G1: 主评估器补齐基准相对指标、交易磨损、仓位结构、路径痛苦度指标。
- G2: 建立规则固定、可复算的基准市场状态分段器（牛/熊/震荡/反弹），作为后续专项的共用基础设施。
- G3: 上线「机遇Alpha 专项」与「中性Alpha 专项」，分析口径、存储、API、前端形态与危机Alpha 对称。
- G4: 多宽基风格轮动矩阵 + 市场分化度 + 隐含风格暴露，回答结构性行情能力。
- G5: 基于每日持仓快照的行业/风格归因（仅新回测可用）。
- G6: 不重跑回测的轻量稳健性分析（子区间、滚动窗口、基准替换敏感性）。
- G7: 一键导出离线自包含 HTML 分析报告。
- G8: 分阶段交付，每阶段独立上线、独立验证后再进入下一阶段。

## Non-Goals
- 不做参数网格批量回测（用户已确认轻量版先行；参数网格留待后续独立功能）。
- 不回溯补写老回测记录的每日持仓明细（positions_json 老数据保持 NULL，相关模块显示「需重新回测」）。
- 不做全策略横向评分对比（危机Alpha 源文件末尾的横向对比此前已明确排除，本套件同样不做）。
- 不引入新的重型前端框架/UI 库；图表继续用 ECharts。
- 不做多用户、鉴权、在线分享。

## Background & Context
- 现有实现证据：
  - 18 项指标在 [evaluator.py](file:///d:/project/tq-iquant-web/main/core/engine/evaluator.py#L96-L121)，基准侧仅有单点 `benchmark_return`，无 beta/IR/捕获率。
  - 危机Alpha 分析器 [crisis_alpha.py](file:///d:/project/tq-iquant-web/main/core/engine/crisis_alpha.py) 已有：周频四档分类、深跌条件检验、极端周、深度回撤事件、多宽基（6 个，仅比下跌周）、月度、滚动相关、年度、代价检验（上涨周拖后腿/同步率/弹性/下尾相关）、八维评分；其 `cost_analysis` 是机遇/中性分析的部分雏形。
  - 存储模式：`backtest_crisis_alphas` 每记录一行（unique record_id）+ `payload_json` + status/error/skipped + computed_at（[model](file:///d:/project/tq-iquant-web/main/core/models/backtest_crisis_alpha.py)），重算路由 `POST /api/backtest/records/{id}/crisis-alpha/recompute`。
  - 宽基行情：TQ（tqcenter）`get_history_raw` 获取指数日线；6 宽基常量 `CRISIS_BENCHMARKS`（中证全指/沪深300/中证500/中证1000/创业板指/科创50），不可得指数走 skipped 机制（科创50 历史短）。
  - `backtest_daily_snapshots.positions_json` 列已存在但 `Portfolio.snapshot()` 不产出持仓明细、service 不写入（[snapshot()](file:///d:/project/tq-iquant-web/main/core/engine/portfolio.py#L487-L498)、[model](file:///d:/project/tq-iquant-web/main/core/models/backtest_daily_snapshot.py#L21)）。
  - 回测成交落 `backtest_trades`（stock_code/price/quantity/amount/commission/stamp_duty/bar_time），换手与成本统计数据现成。
- 用户决策（2026-09-26 澄清）：
  1. 分阶段交付，每阶段独立验证；2. 持仓明细仅对新回测生效；3. 稳健性先做轻量版（不重跑）；4. 报告导出选 HTML 单文件。

## Functional Requirements

### 阶段一：指标扩展 + 持仓快照 + 状态分段器
- **FR-1**: 回测评估新增基准相对指标（需基准日线对齐）：beta、Jensen alpha（年化）、年化超额、跟踪误差、信息比率、上行捕获率、下行捕获率、超额曲线最大回撤。基准数据不足时返回 None 而非报错。
- **FR-2**: 新增交易磨损与持仓指标（组合层）：年化换手率、总交易成本（佣金+印花税）占组合总值比、平均仓位利用率（market_value/total_value 均值）、空仓交易日占比、最大单票权重、持仓集中度 HHI（后两者依赖持仓明细，仅新回测有值）。
- **FR-3**: 新增路径/分布指标：最长连续亏损交易日数、最差单日/单周收益、日收益偏度与峰度。
- **FR-4**: 新回测落库时写入每日 `positions_json`（按 target_type/target_id 维度，内容为当日各股票 code/quantity/可卖数量/市值/成本）；老数据不回填。
- **FR-5**: 新增独立的市场状态分段器 `regime.py`：输入基准收盘价序列，输出每周（必要时每日）四态标签（趋势上涨/趋势下跌/震荡/下跌反弹）与连续段列表；判定规则与阈值以常量固定、写入口径文档注释，同输入必须同输出（确定性），不使用未来函数。
- **FR-6**: 新增指标通过 Alembic 迁移在 `backtest_evaluations` 增加可空列；老记录重算危机Alpha 不强制重跑回测，新指标对老记录在「重新计算评估」或重跑回测后可见（评估指标可由现有快照+trades+基准离线重算，不依赖重跑；持仓类指标除外）。

### 阶段二：机遇Alpha + 中性Alpha 专项
- **FR-7**: 新建 `regime_alpha.py` 分析器（或等价单一分析器），与危机Alpha 同层，输入策略/基准净值、周收益、成交流水、regime 标签，输出 `opportunity`（趋势上涨）、`rebound`（下跌反弹）、`neutral`（震荡）三个数据块与一张「市场状态×绩效」总表（每态：周数占比、策略收益、基准收益、超额、胜率、区间最大回撤、换手率、成本占比）。
- **FR-8**: 机遇块至少含：上涨捕获率、大涨周（阈值镜像危机：+3%/+5%）策略平均收益与收涨比例、踏空周（基准涨而策略跌或空仓未参与）占比、上涨跟涨四档（同步涨/涨得少/踏空/逆势跌）计数与均值。
- **FR-9**: 反弹块至少含：反弹段识别（自近期下跌段低点起的恢复段）、每段基准/策略反弹幅度、策略收复前高天数（未收复标未收复）、反弹弹性（策略反弹幅/基准反弹幅）、反弹段列表。
- **FR-10**: 中性块至少含：震荡周数与占比、震荡市策略累计收益/年化/胜率、震荡市月均交易次数与年化换手率、交易成本/震荡市毛利比、假突破磨损次数（开仓后 N 日内止损/小亏平仓次数）、震荡市与趋势市平均单笔盈亏对比。
- **FR-11**: 存储/API 沿用危机模式：新表 `backtest_market_regime_analyses`（每记录一行、payload_json、status/error/skipped_benchmarks_json、computed_at、唯一约束 record_id）；路由 `POST /api/backtest/records/{id}/market-regime/recompute`；详情接口内嵌其摘要数据；分析失败写 status=failed 与 error，不影响回测详情主链路。
- **FR-12**: 前端在回测详情新增「🚀 机遇Alpha 专题分析」「⚖️ 中性Alpha 专题分析」（可合并为一个大 section 内两个锚点，或两个折叠 section，实现时与危机Alpha 视觉/交互保持一致：默认折叠、展开初始化图表）。

### 阶段三：多基准风格轮动矩阵
- **FR-13**: 复用 6 宽基行情，按滚动窗口（20 周与 60 周双窗口）对宽基截面收益排名，输出每周风格领导标签：大盘价值（沪深300 领先）、中小盘（500/1000 领先）、科技成长（创业板/科创领先）、普涨、普跌、无主线；输出连续风格段。
- **FR-14**: 输出「风格状态×绩效」矩阵（每类风格段：时长占比、策略收益、相对该领涨指数的超额、胜率、最大回撤），直接回答「科创火热/300 强势时策略表现」。
- **FR-15**: 输出宽基截面分化度时间序列（每期 6 指数收益的截面标准差），并将策略超额收益按分化度分桶（低/中/高）对比，验证超额是否在结构性行情（高分化）中更显著。
- **FR-16**: 输出策略对 6 宽基的滚动相关热力图（复用危机已有 rolling_corr 口径扩展）；滚动 OLS beta（不约束为 v1，非负约束回归若实现成本低则作为增强）。
- **FR-17**: 风格分析并入阶段二的 payload（同次重算、同一张表），不可得指数沿用 skipped 机制并在前端注明。

### 阶段四：行业归因 + 轻量稳健性 + HTML 报告
- **FR-18**: 行业归因仅消费新回测的 positions_json：股票→行业映射通过 TQ/通达信可得的行业分类（实施前必须先验证数据源；不可得时该项阻塞并回报用户，不允许伪造映射）。输出行业暴露时间序列（Top 行业权重）、每期领涨行业命中率（组合超配行业与当期涨幅 Top-N 行业的重合度）、行业贡献分解（有数据则做，无行业指数则只做暴露与命中率）。
- **FR-19**: 老记录（positions_json 为空）行业归因模块显示「该记录回测时未保存持仓明细，重新回测后可用」，不显示空图表、不报错。
- **FR-20**: 轻量稳健性（全部基于已有每日快照，禁止触发回测重跑）：逐年/逐半年指标矩阵、滚动 26 周与 52 周收益/夏普分布（中位、最差、最好、负值占比）、最长亏损窗口、基准替换敏感性（相对指标分别对 6 宽基计算并列对比表）。
- **FR-21**: 一键导出 HTML 单文件：后端生成离线可打开的单个 .html（内嵌 ECharts 运行时与全部数据，无外网依赖），包含本回测的核心指标、净值/回撤、危机/机遇/中性/风格各专项图表与表格；前端详情页提供「导出 HTML 报告」按钮下载。
- **FR-22**: 导出报告中的所有数字必须来自同一详情/分析接口数据，禁止手写或猜测数值。

## Non-Functional Requirements
- **NFR-1（TDD）**: 后端 pytest + 前端 vitest 先行；分析器使用构造的小样本序列单测，不连 TQ。
- **NFR-2（兼容性）**: 新分析器纯标准库/numpy/pandas/polars，兼容 main 的 Python 3.13；不得改动 live 桥与 shared 的 3.6/3.7 兼容面。
- **NFR-3（确定性）**: 所有分段与统计同输入同输出；阈值为命名常量并注释口径；时区按 Asia/Shanghai。
- **NFR-4（隔离性）**: 专项分析失败（缺基准/样本不足/TQ 失败）只影响本模块 status，不阻断回测详情与其他模块。
- **NFR-5（迁移安全）**: 所有 schema 变更走 Alembic，只加可空列/新表，不删改既有列；迁移可正向 upgrade。
- **NFR-6（性能）**: 一次「市场状态分析」重算（含 6 宽基取数与全部统计）在普通记录（2~5 年日线）下 P95 ≤ 5 秒；前端展开渲染不卡死（沿用折叠+nextTick init）。
- **NFR-7（视觉一致）**: 新 section/图表/分页/配色沿用现有约定（A 股红涨绿跌、四档色、折叠样式）。

## Constraints
- **Technical**: FastAPI + SQLAlchemy + SQLite + Alembic；Vue 3 + Pinia + ECharts；行情仅能来自 TQ 直连通达信；回测强制 T+1；统一返回 `{code,message,data}`。
- **Business**: 单用户无鉴权；回测仅支持单组合策略；行业归因对老记录不可用（用户已确认）。
- **Dependencies**: 阶段二依赖阶段一的 regime 分段器；阶段三依赖阶段二的表/API 与既有宽基取数；阶段四持仓归因依赖 FR-4 的真实数据积累与 TQ 行业分类可用性验证。

## Assumptions
- TQ 可取全部 6 个宽基指数日线（现状已验），行业分类/行业指数可取性待阶段四开工前验证（列为 Open Question）。
- 回测详情接口已包含 benchmark_value 对齐序列，可离线重算 FR-1 指标（实施时核实；若缺则评估重算从原始基准数据补齐）。
- 专项分析沿用「手动点重算」触发，不做回测完成自动全量分析（与危机Alpha 现状一致；如自动计算成本可接受，实现阶段可确认）。
- ECharts 运行时可从 web 构建产物/node_modules 读取并内联进导出 HTML。

## Acceptance Criteria

### AC-1: 评估指标扩展正确落库
- **Type**: `rule`
- **Given**: 一组已知策略/基准日收益的构造快照与成交数据
- **When**: Evaluator.evaluate 执行并持久化到 backtest_evaluations
- **Then**: beta/Jensen alpha/年化超额/跟踪误差/IR/上行捕获/下行捕获/超额回撤/换手率/成本占比/仓位利用率/空仓占比/连亏天数/最差日/最差周/偏度/峰度均有输出或在数据不足时为 None；Alembic 迁移后新列存在且可空
- **Pass Condition**: 单测对每个指标给出独立数值断言（容差内）且迁移 upgrade 成功、老行不报错
- **Evidence**: `uv run pytest core/tests/` 相关用例 + alembic upgrade 验证

### AC-2: regime 分段器确定性且无未来函数
- **Type**: `rule`
- **Given**: 三段构造基准序列（单边上涨、单边下跌、区间震荡 + 一个 V 型反弹）
- **When**: 分段器运行两次、并以截断到 t 的子序列运行
- **Then**: 两次输出完全一致；截断序列在 t 之前的标签与全量序列完全一致；四态均能在对应构造段被识别
- **Pass Condition**: 确定性断言与逐点截断断言通过；阈值常量有口径注释
- **Evidence**: `core/tests/unit/` 分段器单测

### AC-3: 新回测写入每日持仓明细
- **Type**: `rule`
- **Given**: 发起一次新回测
- **When**: 回测完成落库
- **Then**: 每个快照行 positions_json 非空且可解析出 code/quantity/市值等字段；存量老快照 positions_json 仍为 NULL
- **Pass Condition**: 集成测试断言新快照 JSON 结构与老数据 NULL 共存
- **Evidence**: 集成测试 + 数据库检查

### AC-4: 机遇/中性专项端到端可用
- **Type**: `rule`
- **Given**: 一条 completed 且含成交记录的回测
- **When**: 调用 market-regime 重算并打开详情展开两个专项
- **Then**: 状态总表四态齐全（无样本的态周数为 0 而非缺块），机遇/反弹/中性三块字段完整，前端渲染图表与表格；失败场景 status=failed 且页面显示原因不崩
- **Pass Condition**: API payload 字段契约测试 + 前端渲染测试 + failed 态测试通过
- **Evidence**: pytest + vitest 用例与浏览器实测截图

### AC-5: 中性磨损指标与成交流水一致
- **Type**: `rule`
- **Given**: 回测 backtest_trades 已知金额与费用
- **When**: 中性块计算换手率/成本/月均交易次数
- **Then**: 数值等于按定义从 trades 直接聚合的结果（容差内）
- **Pass Condition**: 以独立聚合实现做交叉断言
- **Evidence**: 后端单测

### AC-6: 风格轮动矩阵数据真实且覆盖可得宽基
- **Type**: `rule`
- **Given**: TQ 可取的宽基集合（模拟层用注入价格）
- **When**: 风格分析执行
- **Then**: 风格标签、风格×绩效矩阵、分化度序列、滚动相关全部来自输入价格计算（测试中无任何硬编码结果）；不可得指数进入 skipped 并在 payload 与 UI 标注
- **Pass Condition**: 构造价格场景断言风格标签方向；skipped 用例存在
- **Evidence**: 单测 + 前端测试

### AC-7: 行业归因的数据门禁正确
- **Type**: `rule`
- **Given**: 一条新回测（有 positions_json）与一条老回测（NULL）
- **When**: 打开分析
- **Then**: 新记录展示行业暴露/命中率（行业数据源验证通过为前提）；老记录显示「重新回测后可用」且无行业图表实例
- **Pass Condition**: 两种记录的前端分支均有测试；数据源不可达时任务 blocked 并回报，不产出伪映射
- **Evidence**: vitest 用例 + 数据源验证记录

### AC-8: 轻量稳健性不触发回测重跑
- **Type**: `rule`
- **Given**: 任意 completed 记录
- **When**: 计算稳健性模块
- **Then**: 过程中不调用回测执行链路（无 BacktestEngine.run、无新回测记录产生），仅读取快照/trades/基准
- **Pass Condition**: 代码审查 + 测试中 mock 回测入口断言零调用
- **Evidence**: 单测 mock 断言

### AC-9: HTML 报告离线自包含且数据一致
- **Type**: `rule`
- **Given**: 一条 completed 记录
- **When**: 调用导出接口下载 HTML
- **Then**: 单文件在断网环境用浏览器可打开，核心表格与图表渲染；报告关键数字与详情页一致（抽查总收益、年化、最大回撤、危机Alpha 占比）
- **Pass Condition**: 文件无外部 URL 依赖（grep http/https 资源引用为 0，白名单除外）；数字抽查一致
- **Evidence**: 接口测试 + 断网打开截图

### AC-10: 全链路质量门
- **Type**: `rule`
- **Given**: 全部阶段实现完成
- **When**: 运行全量测试、类型检查、构建与迁移
- **Then**: `uv run pytest core/tests/` 全绿、`npx vitest` 全绿、`npx vue-tsc --noEmit` 退出 0、`npm run build` 成功、Alembic upgrade 成功
- **Pass Condition**: 四条命令全部通过
- **Evidence**: 命令输出

### AC-11: 前端与现有危机Alpha 的体验一致性
- **Type**: `rubric`
- **Dimension**: 交互与视觉一致性
- **Scale**: 1-5
- **Anchors**: 1 = 新栏目布局/配色/折叠行为与现有页面明显割裂；3 = 基本可用但样式或交互有可见不一致；5 = 折叠/图表初始化/分页/配色/空态与危机Alpha 完全同构，用户无学习成本
- **Pass Threshold**: >= 4
- **Evidence**: 浏览器实测截图 + 评审

### AC-12: 指标口径的金融合理性
- **Type**: `rubric`
- **Dimension**: 统计口径正确性与可解释性
- **Scale**: 1-5
- **Anchors**: 1 = 存在未来函数/年化错误/方向相反等硬伤；3 = 口径可算但存在歧义或未注释；5 = 全部指标口径正确、边界（样本不足/缺基准）处理清晰、常量与公式均有注释、结论文案不夸大
- **Pass Threshold**: >= 4
- **Evidence**: 独立评审对照构造样本与公式逐项检查

## Open Questions
- [ ] OQ1: TQ/通达信是否可取申万行业分类与行业指数日线？阶段四开工前验证；不可得则与用户确认降级方案（仅风格暴露/手工映射）。
- [ ] OQ2: 市场状态阈值（趋势/震荡/反弹判定窗口与幅度）首版用固定常量（计划在 tasks 中给出口径），是否需要在前端暴露参数调节？默认不暴露。
- [ ] OQ3: 回测完成后是否自动计算市场状态分析？默认沿用危机Alpha 的手动重算模式。
- [ ] OQ4: 导出报告的栏目范围默认与详情页一致（含各专项），是否需要精简版？默认全量。
