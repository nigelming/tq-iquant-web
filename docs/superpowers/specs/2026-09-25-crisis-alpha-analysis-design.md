# 危机Alpha 专题分析迁移 — 设计

日期：2026-09-25
分支：`feat/backtest-return-heatmap`（待定，见「分支收尾」）
来源：`E:\宏观模型回测引擎\宏观模型回测引擎增强版.py`（Streamlit 单文件应用，5286 行）
范围：后端 `main/core/engine/` + `main/core/services/` + `main/core/models/` + `main/core/api/` + 新增 Alembic 迁移；前端 `web/src/views/Backtest.vue` + `web/src/api/index.ts`

## 背景与动机

源项目（宏观模型回测引擎）内置「🌪️ 危机Alpha专题分析」：以组合/策略日频净值与宽基指数为输入，评估策略在 A 股系统性下跌阶段的对冲能力，输出 12 个分析模块 + 9 维度加权「综合危机保护评分」。当前项目回测管理栏目的风险收益指标仅有 18 个通用指标（夏普/索提诺/卡玛/VaR/CVaR 等），缺少「下跌市对冲能力」这一维度的评估。本次将该专题分析完整迁移进回测详情页，并在整体表现指标区上提两个核心结论。

## 需求（已与用户逐项确认）

1. **分析对象**：仅组合净值（`backtest_daily_snapshots` 的 `target_type='portfolio'`），不对子策略出报告。
2. **计算时机**：回测结束时计算并落库（不在查看详情时实时算）。
3. **基准来源**：主基准 = 组合已有的 `benchmark_index`（默认 `000300.SH`），不新增选择控件；「多宽基一致性」用内置 6 宽基固定集合。
4. **样本不足/缺数**：照搬源项目内建降级逻辑（无深跌周/无回撤事件 → 该维度中性 50 分；相关不足 52 周 → 扩展窗口补全；共同交易日 <30 → 整块不算）。
5. **UI 落点**：回测详情页「整体表现指标」之后新增独立 `🌪️ 危机Alpha 专题分析` section，12 个子模块按源项目顺序全部展开。
6. **历史回测**：详情页提供「重新计算」按钮（实时算完写库），供本次上线前已完成的回测补算。
7. **核心结论上提**：「整体表现指标」区追加「危机Alpha占比」与「综合危机保护评分」2 张卡，点击滚动到专题 section。

## 明确不迁移

- 源 L5238-5273「📊 附加：全部策略危机保护评分横向对比」（用户指定排除）。
- 源项目的基准缓存 xlsx（`E:\...\基准指数\*.xlsx`）与 akshare 腾讯/新浪回退源：当前项目只有 TQ 数据通道，且无 akshare 依赖。TQ 取不到就跳过。
- 源项目的「强制刷新基准数据」复选框：改为「重新计算」按钮（连同策略净值一起重算）。
- 源项目的策略下拉（多策略切换）、独立主基准下拉、多宽基多选框：分别由决策 1/3 排除或替代。

## 方案选择

- **方案 A（已选）**：分析器 1:1 移植到 `core/engine/crisis_alpha.py`（pandas），回测结束后独立事务落库，Vue 端 ECharts 复刻 12 模块。算法与源完全一致，口径可核对。
- 方案 B（用 polars 重写分析器）未选——源实现重度依赖 pandas 惯用法（`resample('W-FRI')`、`rolling().corr()`、`expanding().corr()`、`groupby().get_group()`、`quantile`、`np.isin`），polars 无 1:1 等价 API，重写会引入难以核对的口径偏差与回归风险。
- 方案 C（实时计算不落库）未选——用户已选落库；且需 TQ 在线、每次查看重复拉 7 段指数。
- 方案 D（只落库摘要、图表数据前端自算）未选——会把统计口径分散到前端，违背「引擎层集中计算」的现有分层。

## 实现设计

### 1. 后端分析器 `main/core/engine/crisis_alpha.py`（新增）

从源 L2815-3277 逐行移植，**算法、阈值、口径一字不改**：

| 源位置 | 目标符号 | 说明 |
|---|---|---|
| L127-136 | 模块级常量 | `CRISIS_THRESHOLD=0.0`、`EXTREME_TOP_N=10`、`DEEP_DD_THRESHOLD=-0.05`、`ROLLING_CORR_WINDOW=52`、`DEEP_WEEK_TESTS=(-0.03,-0.05)`、`MONTH_BIG_MOVE=0.05`、`WEEKS_PER_YEAR=52` |
| L117-131 | `CRISIS_BENCHMARKS` | 6 宽基内置表（见下） |
| L2815-2821 | `CRISIS_CATS` / `CRISIS_COLORS` | 四档名与配色（红/橙/浅绿/深绿） |
| L2823-2838 | `_classify_crisis` | 逐周四档分类（ratio<0.5 / 0.5~1 / >1，1e-12 容差照搬） |
| L2841-2858 | `_rating` / `alpha_rating` | 卓越≥85/优秀≥70/一般；占比 0.8/0.6/0.4 分档 |
| L2861-3277 | `class CrisisAlphaAnalyzer` | 全部方法原样移植 |

内置宽基（照搬源 `Config.BENCHMARK_CONFIG` + `MULTI_BENCHMARK_KEYS`）：

```python
CRISIS_BENCHMARKS = (
    {"key": "csi_all",  "name": "中证全指", "code": "000985.CSI"},
    {"key": "hs300",    "name": "沪深300",  "code": "000300.SH"},
    {"key": "csi500",   "name": "中证500",  "code": "000905.SH"},
    {"key": "csi1000",  "name": "中证1000", "code": "000852.SH"},
    {"key": "cyb",      "name": "创业板指", "code": "399006.SZ"},
    {"key": "kc50",     "name": "科创50",   "code": "000688.SH"},
)
```

`CrisisAlphaAnalyzer.__init__` 的两处适配：
- 源入参 `strategy_nav` 来自 `res['Net_Value']`；本项目改为回测结果内存中的组合日净值序列（`result["snapshots"][*]["total_value"]`，键为 `snap_date`），构造 `pd.Series`（索引 `pd.to_datetime(snap_date)`）。
- 源入参 `benchmark_price` 为主基准指数收盘 `pd.Series`（键为交易日）。
- 源 L2874 的 `len(common) < 30` 校验保留，抛出后由 service 转为 `status='insufficient'`。

新增编排函数：

```python
def build_crisis_alpha_payload(strategy_nav, main_code, main_name,
                                benchmark_closes: dict[str, pd.Series],
                                strategy_name: str) -> dict
```

职责（照搬源 UI 层 L4723-4760 的构造顺序）：
1. 用 `main_code` 建主分析器 `an`，`s = an.summary(multi_alphas)`。
2. 遍历 `benchmark_closes`（6 宽基，主基准自身已在其中则跳过重复项），逐个建分析器取 `quadrant_analysis()`，产出 `multi_bench` 列表：`[{name, code, alpha_ratio, n_down, up_rate, is_main}]`；构造时按主基准的周频索引裁剪（照搬源 L4723-4728）。
3. `multi_alphas = [m["alpha_ratio"] for m in multi_bench]`，传给 `an.summary(multi_alphas)`（影响 9 维评分的第 5 维「多宽基一致性」）。
4. 追加 UI 层才需要的字段：`nav`（`an._nav()` 的两条归一化序列）、`shade_spans`（`an.drawdown_shade_spans()`）、`alpha_rating`（`alpha_rating(s['quadrant']['alpha_ratio'])`）。

JSON 净化（**硬性要求**）：分析器输出含 `numpy` 标量、`pd.Timestamp`、`Decimal`、`NaN`/`Inf`。FastAPI 默认 `JSONResponse` 用 `allow_nan=True` 会吐出裸 `NaN`，**JS `JSON.parse` 直接抛错**。故落库前统一过 `sanitize_for_json()`：`NaN/Inf → None`、`numpy.floating/integer → float/int`、`pd.Timestamp → "YYYY-MM-DD"`（周频日期为周末，取 ISO 日期即可）、`Decimal → float`、`pd.NaT → None`、dict/list 递归。

### 2. 指数取数（`backtest_service.py`）

现有 `build_benchmark_data`（`:304-339`）只取单个 `ps.benchmark_index` 且**无 try/except**（TQ 异常会冒泡）。新增：

```python
def build_index_close_series(codes: list[str], start: date, end: date) -> dict[str, dict[date, Decimal]]
```

- 一次 `TQData().get_history_raw(stocks=codes, periods=["1d"], start, end, dividend_type="front", count=-1)` 批量取全部 code（主基准 + 6 宽基去重），按 code 拆成 `{code: {date: close}}`；单个 code 缺列/空 → 该 code 不出现在结果里（视作跳过）。
- 整个调用包在 `try/except Exception` 内 → 返回已取到的部分（源项目「多源择优」的简化版：只有 TQ 一个源）。
- `build_benchmark_data` 改为调用此函数取单 code（保持既有行为与测试不变）。
- 拉数区间 = 回测 `start_date`~`end_date`（与快照区间一致；不额外扩展，源项目 `_nav()` 允许基准区间更长，但本项目基准本就按回测区间拉取）。

### 3. 计算时机与事务（`backtest_service.py:1101-1125`）

现状：`_persist_result`（`:1101`）→ 标记 `completed`（`:1106-1108`）→ `db.commit()`（`:1109`）。

危机Alpha 插在 **`:1109` 之后**（而非之前），原因：
- 指数拉取是长耗时同步 I/O，放主事务内会长时间持有写锁；
- 任何异常都不会连带回滚快照/评估/状态，避免「回测跑完却因指数缺数而变 failed」。

```python
db.commit()                      # :1109 回测主结果落库完成
try:
    compute_and_store_crisis_alpha(db, record_id=rec.id, ps=ps,
                                    snapshots=result["snapshots"],
                                    start=req.start_date, end=req.end_date)
except Exception as exc:
    _store_crisis_alpha_failure(db, record_id=rec.id, error=str(exc))
```

`compute_and_store_crisis_alpha` 内部：先拉指数（不持事务）→ 构造分析器 → `status='completed'`（或 `'insufficient'`）→ upsert 单行 → 独立 `db.commit()`。`run_backtest` 的返回值不变（不新增字段，避免影响既有前端/测试）。

`recompute_crisis_alpha(db, record_id) -> dict | None`：读记录的 `start_date/end_date` + `params_snapshot` 中的组合策略基准代码（回测冻结参数），从 `backtest_daily_snapshots` 取 `target_type='portfolio'` 快照重建净值序列，再走同一 `compute_and_store_crisis_alpha`（upsert 覆盖）。返回 `None` 表示记录不存在。

### 4. 存储

新表 `backtest_crisis_alphas`（模型 `main/core/models/backtest_crisis_alpha.py`，导出到 `core/models/__init__.py`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK autoincrement | |
| `backtest_record_id` | Integer FK `backtest_records.id` ondelete CASCADE, unique, index | 1:1 |
| `benchmark_code` | String(20) | 主基准代码快照 |
| `benchmark_name` | String(20) | 主基准显示名（代码本身或 `沪深300` 等内置名） |
| `status` | String(12) | `completed` / `insufficient` / `failed` |
| `error` | Text nullable | 失败原因 |
| `skipped_benchmarks_json` | Text | `["中证全指", ...]` 取数失败的宽基 |
| `payload_json` | Text | `build_crisis_alpha_payload` 净化后的 JSON |
| `computed_at` | DateTime | |

风格与现有迁移一致（参考 `main/alembic/versions/e5f6a7b8c9d0_add_decision_event_tables.py`）：typed `revision/down_revision/branch_labels/depends_on`，`op.create_table` + 显式 FK/index，`downgrade()` 反序 drop。`down_revision = 'e5f6a7b8c9d0'`（当前 head）。JSON 存 `Text` 而非 JSON 列——与 `backtest_daily_snapshots.positions_json` 现状一致，SQLite 下无差异，避免引入新列类型。

体积估算：5 年回测约 260 周 → `weekly_detail` 260 行 + `rolling_corr` 260 行 + 其余约 100 行，单条 payload 约 60-120KB。SQLite 本地单用户可接受。

### 5. API

- 详情：`get_record_detail`（`:888-973`）返回值加 `crisis_alpha` 键 = `{status, error, skipped_benchmarks, computed_at, benchmark_code, benchmark_name, data}`；无记录行时为 `null`。
- 新增 `POST /api/backtest/records/{record_id}/crisis-alpha/recompute`（`core/api/backtest.py`，紧邻 DELETE `:70-76`）：
  - 同步 `def` 路由（FastAPI 自动进线程池，不阻塞事件循环，符合 AGENTS.md 并发模型）；不加 `_BACKTEST_LOCK`（不跑回测、不写回测表；TQ 侧 `data.py` 自有全局 TDX 锁串行化）。
  - 记录不存在 → `err(404, "回测记录不存在")`；成功 → `ok({"status": ..., "computed_at": ...})`；内部异常 → 落 `status='failed'` 并 `ok(...)` 返回（不抛 500，便于前端展示失败原因）。
  - 需在 `core/api/backtest.py:33-49` 的 re-export 列表追加 `recompute_crisis_alpha`（保持该文件的测试兼容约定）。
- 前端 `web/src/api/index.ts`：`BacktestRecordDetail`（`:235-246`）加 `crisis_alpha` 字段 + 12 模块 TS 接口；新增 `recomputeCrisisAlpha(recordId)` 方法（`:472-497` 区块内）。

### 6. 前端 UI（`web/src/views/Backtest.vue`）

**6.1 整体表现指标 6 → 8 卡**（`:228-240` `overallStats`、`:741-748` 模板、`:986-991` 样式）
- 追加 `{label:'危机Alpha占比', value: pct(...) + ' · ' + rating}`、`{label:'综合危机保护评分', value: num(total_score,1) + ' · ' + rating}`；`detail.crisis_alpha` 为空/`status!=='completed'` 时 `value: '—'`。
- 两张卡带 `anchor: 'crisis-alpha'`，模板 `@click="s.anchor && scrollToSection(s.anchor)"`（`scrollIntoView({behavior:'smooth'})`）。
- `.stats-grid` 桌面列数 `repeat(6,1fr)` → `repeat(4,1fr)`（8 卡 = 2 行 × 4）；`:1188`/`:1192` 的 3 列/2 列响应式断点不变；`:985` 注释同步改为 8 卡。

**6.2 新增 section**（插在 `:748` 之后、`:750`「策略对比分析」之前）
- `ref="crisisAlphaSection"` 锚点 + 条件渲染：有数据 → 全量模块；`null` → 「本次回测未包含危机Alpha分析」+「重新计算」按钮；`status='failed'` → 失败原因 + 重算按钮；`status='insufficient'` → 样本不足说明。
- 头部 4 张 KPI 卡（照搬源 L4768-4801）：危机Alpha占比（含评级）、综合危机保护评分（含评级）、下跌周逆势上涨比例、累计收益（副标年化 + MDD）。
- 状态条：共同交易日口径说明（照搬源 L4848 语义）、已跳过基准列表、`computed_at`、重新计算按钮（计算中 loading，成功后重载详情）。
- 12 个子模块（顺序、标题照搬源 L4804-5235）：

| # | 标题 | 呈现 | 数据来源 |
|---|---|---|---|
| 一 | 核心结论 | 段落 | `quadrant`/`performance`/`deep_dd_events`/`score` |
| 二 | 产品与基准走势 | 双净值折线 + 基准深度回撤阴影（ECharts `markArea`）+ 绩效表 | `nav` / `shade_spans` / `performance` |
| 三 | 危机Alpha四档分类 | 环形图 + 分类统计表 + 「深跌周条件检验（更严苛）」子表 | `quadrant` / `conditions` |
| 四 | 极端下跌周 TOP10 | 分组柱状图（基准 vs 策略）+ 正收益/跑赢周数说明 | `extreme_top` |
| 五 | 全周期四象限分布 | 散点（x=基准周收益, y=策略周收益, 四档配色 + 象限分割线） | `weekly_detail` |
| 六 | 深度回撤事件独立性 | 事件表 + 对冲比例说明 | `deep_dd_events` |
| 七 | 多宽基一致性 | 柱状图（主基准虚线参考）+ 明细表 | `multi_bench` |
| 八 | 月度视角 | 四状态分组柱 + 汇总表 + 「大跌月明细（股票月跌 >5%）」子表 | `monthly` |
| 九 | 动态相关性与年度趋势 | 滚动/扩展相关折线（不足窗口段虚线）+ 年度危机Alpha柱状图/表（非完整年加 `*`） | `rolling_corr` / `expanding_corr` / `annual` |
| 十 | 代价检验（上涨周表现） | 5 行表 | `cost` |
| 十一 | 综合危机保护评分 | 9 维条形图 + 维度/类型/权重/得分/评级/贡献表 | `score` |
| 十二 | 全部下跌周明细（N 周） | 表格（`table-container` 内滚动） | `weekly_detail` 中基准收益<0 的子集，前端按日期倒序 |

- 配色：四档沿用源 `CRISIS_COLORS`（逆势上涨 `#d6403a`、跌得少 `#f0a429`、跟着跌 `#9dc88d`、跌更多 `#1f6e4f`），与本项目红涨绿跌语义一致（红=好/涨，绿=差/跌），不改动既有风险色。
- 9 个新 ECharts 实例并入现有生命周期（本文件**无 `watch()`**，勿引入）：
  - `echarts.init(el)` 前先 `dispose()` 旧实例（照 `:395-396` / `:473-474` 的写法）；
  - 新增 `initCrisisAlphaCharts()`，在 `initCharts()`（`:384-391`）末尾调用；section 未渲染（无数据/失败/样本不足）时该函数内部直接 return；
  - 9 个实例在 `onUnmounted`（`:646-653`）统一 `dispose()`；
  - 「重新计算」成功后：重取详情 → 赋 `detail.value` → `await nextTick()` → `initCharts()`（照 `openDetail()` `:127-140` 的顺序，不重复实现加载逻辑）。
- 空数据（如无回撤事件、无下跌月）显示 `.empty-state` 文案而非空图。
- 长表（十二、年度）沿用 `.table-container` 样式，不新增 CSS 体系。

### 7. 降级与边界（照搬源逻辑）

| 场景 | 行为 |
|---|---|
| 共同交易日 < 30 | `status='insufficient'`，不落 payload，section 显示样本不足 |
| 主基准取数失败/空 | `status='failed'` + error；回测本身不受影响 |
| 6 宽基部分取数失败 | 跳过并记入 `skipped_benchmarks`；`multi_bench` 只含成功项；全失败时第 5 维按 `composite_score` 的 `else` 分支退化（用主基准 alpha_ratio，源 L3220-3221） |
| 无深跌周（>3%/>5%） | 第 2 维给中性 50（源 L3211） |
| 无深度回撤事件 | 第 4 维给中性 50（源 L3215） |
| 周数 < 52 | 第 9 维照常用全期 corr；相关曲线用 `expanding(min_periods=8)` 段补全（源 L3067-3069 + UI L5110 附近） |
| 上涨周/下跌周/Q1 样本 < 5 | 对应统计给 0（源 L3084-3093） |
| 无下跌月 | `hedge_ratio=0`，大跌月表空 |
| 回测区间无基准配置 | 主基准回落 `000300.SH`（与 `build_benchmark_data:313` 一致） |

## 测试策略（TDD，先写测试）

**后端单测（新增）** `main/core/tests/unit/test_crisis_alpha.py`
- `_classify_crisis`：涨/跌、ratio 恰 0.5、恰 1.0、基准近零（inf）四类边界。
- `quadrant_analysis`：构造已知周收益序列，断言 `alpha_ratio`/`up_rate`/`odds`/`counts`/`avg_ret`。
- `condition_tests`：阈值 -3%/-5% 的 n/b_avg/s_avg/up_rate；无样本时全 0。
- `extreme_drops_top_n`：TOP10 选取 + 按基准升序（最惨在前）+ `超额`/`分类` 列。
- `deep_drawdown_events`：峰值→低点口径、-5% 阈值过滤、无事件返回空；`drawdown_shade_spans` 连续区间合并。
- `cost_analysis`：`drag`/`sync_up`/`elasticity`/`tail_corr`（<5 样本给 0）/`q1_corr`。
- `monthly_analysis`：四档状态阈值（±5%）与 `hedge_ratio`；`annual_stats` 完整年判定（≥40 周）。
- `rolling_correlation`/`expanding_correlation` 长度与 dropna。
- `composite_score`：9 维权重之和=1.0、贡献=得分×权重、`total_score` 加权求和；无深跌周/无事件时中性 50；`multi_alphas` 影响第 5 维。
- `build_crisis_alpha_payload`：键集完整、`multi_bench` 含 `is_main`、`alpha_rating` 分档。
- `sanitize_for_json`：`NaN`/`Inf`/`NaT`/`numpy`/`Timestamp`/`Decimal` 全部转 JSON 安全值（**`NaN` 必须变 `None`**，用 `json.dumps(..., allow_nan=False)` 断言不抛错）。

**后端单测（扩展）** `main/core/tests/unit/test_backtest_service.py`
- `build_index_close_series`：批量多 code、`_FakeTQ` 捕获 `stocks/periods/start/end`、缺列 code 被剔除、异常返回部分结果。
- `compute_and_store_crisis_alpha`：completed / insufficient / failed 三态落库；异常被吞且回测记录仍 `completed`（复用 `test_run_backtest_happy_path...:115` 模式，monkeypatch 使计算抛错）。
- `recompute_crisis_alpha`：upsert 覆盖写、记录不存在返回 `None`、从 DB 快照重建净值。
- `get_record_detail` 返回 `crisis_alpha`（有/无两种）。
- 保留并维持既有 `test_build_benchmark_data_uses_custom_benchmark_index`（当前未提交，本次一并纳入）。

**后端集成测试** `main/core/tests/integration/test_backtest_api.py`
- `POST /api/backtest` → `GET /records/{id}` 含 `crisis_alpha.status='completed'`。
- `POST /records/{id}/crisis-alpha/recompute` 成功/404。
- 注意：现有集成 fixture（`:199-212`）未 patch `build_benchmark_data`，新增 crisis 路径需一并 patch，避免测试触碰真实 TQ。

**前端测试（扩展）** `web/src/__tests__/Backtest.test.ts`
- `overallStats` 8 卡（含 `—` 降级）；两张危机卡点击滚动。
- section 12 个子模块标题渲染；有数据时 KPI/表格/图表容器出现。
- 三种空态：`null`（未计算 + 重算按钮）、`failed`（错误文案）、`insufficient`（样本不足）。
- 重算按钮触发 `recomputeCrisisAlpha` 并在成功后重载详情。
- 四档配色常量与红涨绿跌不冲突（断言 `CRISIS_COLORS` 值）。

**验证命令**
```
cd main && uv run pytest core/tests/ -q
cd web && npx vitest run
cd web && npx vue-tsc --noEmit
cd web && npm run build
```

## 风险与对策

| 风险 | 对策 |
|---|---|
| TQ 不识别 `000985.CSI`（源项目正因此需要 akshare 回退） | 逐 code 独立判定，取不到即跳过并展示「已跳过」；主基准失败才置 failed |
| pandas 目前是 pytdx 的传递依赖（`backtest_service.py:20` 已 import，lock 3.0.5） | 在 `main/pyproject.toml` 显式声明 `pandas>=3.0.5`（lock 已有，无需新下载），不依赖传递关系 |
| `NaN` 破坏前端 `JSON.parse` | `sanitize_for_json` 强制净化，落库前用 `json.dumps(allow_nan=False)` 自检 |
| 危机Alpha 失败污染回测状态 | 计算在主 commit 之后、独立事务、异常吞掉只落 failed |
| payload 体积（60-120KB/记录） | 单用户 SQLite 可接受；若后续记录数过多，可只保留最近 N 条或裁剪 `weekly_detail`（本期不做） |
| 12 模块前端代码量大（单文件已 1196 行） | 本期不拆组件（遵循现有 Backtest.vue 单文件惯例）；若 review 认为过大，后续再抽 `CrisisAlphaSection.vue` |

## 分支与提交策略（已确认）

- **新分支** `feat/crisis-alpha-analysis`，从当前 HEAD（`387797b`，含上轮加固）切出；原分支 `feat/backtest-return-heatmap` 保持纯净、可单独合并。
- 切分支前先把上轮遗留的未提交改动（`main/core/tests/unit/test_backtest_service.py::test_build_benchmark_data_uses_custom_benchmark_index`）作为本特性第一个独立提交落库（`test: 补充自定义基准取数单测`），不与其他改动混提。
- 本特性其余提交按实施计划的任务边界切分。
