# 前端接线：6 项「针对模型不足」后端改动（2026-10-13）

## 背景与目标

深夜（2026-10-13 00:04–02:29）后端已落地 6 项针对模型不足的改动（全部**未提交**、全部 **API-first、前端零接入**）。目标：把全部改动接入前端，使用户能实际使用这些能力。

已通过**后端改动审计**（本 spec 同目录 `../plans/` 对应实施计划基于同一契约）：
- 1058 后端测试全绿；
- oos_gate / walk-forward / param-perturb / 执行真实感 / 未来函数检测 / acceptance 语义逐项核实无误；
- `web/dist` 与 `tq-iquant.exe` 均为构建于 10-12 的过期产物，需在实现后重建/重打包才整体生效。

## 变更范围（全部接线，用户已确认）

| # | 功能 | 前端落点 |
|---|------|---------|
| B6 | 评估口径 absolute/relative | 回测发起表单 select + 详情接受验收块口径切换 |
| C7 | IS/OOS 采纳门槛 | 回测发起表单开关+比例 + 记录列表门状态徽章 + 详情门状态 |
| C8 | walk-forward 滚动验证 | **新页面 `/backtest/analysis` tab1**（发起/列表/窗口详情） |
| P2b | 参数微扰敏感性 | **新页面 `/backtest/analysis` tab2**（发起/列表/baseline+params 详情） |
| #1/#2/#3 | 执行真实感开关 | Portfolios.vue 组合「执行真实感」组 + 策略「策略增强」组 |
| P2a | 公式未来函数检测 | Formulas.vue 新建/编辑表单 checkbox |

## 后端契约（前端必须精确匹配）

### GET /api/backtest/records（列表项，`_serialize_record`）
新增字段：`evaluation_profile`("absolute"|"relative"，历史记录回落 absolute)、`is_oos_gate_enabled`(bool)、`oos_gate_min_ratio`(float|None)、`gate_status`("passed"|"rejected"|None)、`gate_reasons`(string[])。列表已过滤 WF/PP 子回测。

### POST /api/backtest（BacktestRequest）
`dividend_mode`("front"默认)，`evaluation_profile`("absolute"默认)，`is_oos_gate_enabled`(false 默认)，`oos_gate_min_ratio`(0.5 默认，非法回退 0.5)。返回值新增：`warnings`(string[])、`gate`({status, reasons: string[], min_ratio} | null)。400 校验：组合 404；日期顺序/未来；portfolio 校验沿用。

### GET /api/backtest/records/{id}
新增 query `?profile=absolute|relative`（仅影响 acceptancе 块，不改存储）。响应新增 `acceptance`：
`{profile, pass: True|False|None, checks: [{metric, op, threshold, value, pass, label}]}`。
absolute 阈值：annual_return≥0.08、max_drawdown≤0.20、calmar_ratio≥1.0；relative：information_ratio≥0.5、excess_annual_return≥0.0、down_capture_ratio≤1.0。

### POST /api/backtest/walk-forward（WalkForwardRequest 内含默认值）
`portfolio_strategy_id, name, start_date, end_date, train_years=3, test_years=1, oos_gate_min_ratio=0.5, dividend_mode="front", evaluation_profile="absolute"`。
校验：日期顺序/未来；train_years≥1；test_years≥1；ratio 0<(r)≤2；组合 404；并发占用 409。
返回：`{run_id, status, windows_passed, overall_pass, error}`。
GET `/walk-forward` 与 `/walk-forward/{run_id}`（404 语义）序列化：
`{id, portfolio_strategy_id, name, start_date, end_date ISO, train_years, test_years, oos_gate_min_ratio, status, error_message, summary, created_at, completed_at}`。
`summary`：`{windows: [{index, train:{start,end}, test:{start,end}, record_id, is_cagr, oos_cagr, oos_return, oos_benchmark_return, gate:{status,reasons}, error?}], windows_passed: "k/n", overall_pass: bool}`。

### POST /api/backtest/param-perturb（ParamPerturbRequest 内含默认值）
`portfolio_strategy_id, name, start_date, end_date, perturb_pct=0.10, dividend_mode="front", evaluation_profile="absolute"`。
校验：日期顺序/未来；perturb_pct 0<(p)≤0.5；组合 404；409 并发。
返回：`{run_id, status, rounds_run, most_sensitive, error}`。
GET 序列化：`{id, portfolio_strategy_id, name, start_date, end_date, perturb_pct, status, error_message, summary, created_at, completed_at}`。
`summary`：`{baseline: {record_id, status, annual_return, ..., error}, rounds: [...], params: [{param, low:{…}|None, high:{…}|None, delta_cagr}], most_sensitive}`。
⚠️ `delta_cagr` 实为「年化收益差值（high−low）」，前端文案按此标注。
扰动参数（10 个）：capital_ratio, max_positions(int), single_open_ratio, stop_loss_ratio, take_profit_ratio, trailing_stop_ratio, add_position_threshold, max_add_count(int), add_position_ratio, reduce_position_ratio。

### PORTFOLIO 相关（strategies API）
组合字段：`price_limit_check_enabled`(bool，新建默认 True，编辑不动)、`volume_limit_enabled`(bool 默认 False)、`volume_limit_ratio`(Numeric(4,2) 默认 0.10，校验 0<(r)≤1)。
策略字段：`rotation_enabled`/`rotation_momentum_bars`(默认20)/`rotation_hysteresis`(默认0.05)、`regime_filter_enabled`、`trend_trailing_stop_ratio`(float|None)、`trend_max_positions`(int|None)、`exposure_cap_enabled`(bool 默认 False)。
序列化均含以上字段（见 strategy_service.serialize_strategy/serialize_portfolio）。

### FORMULA 相关（formulas API）
`FormulaCreate/FormulaUpdate.strict_future_check`(bool 默认 False)。开启且内容命中未来函数关键字（ZIG/ZIGA/FLATZIG/PEAK/PEAKBARS/TROUGH/TROUGHBARS/BACKSET/REFX/REFXV/XMA，词边界+左括号匹配）→ HTTP 400，message 含命中关键字。

## 前端架构

- 新增 view `BacktestAnalysis.vue`（/backtest/analysis 路由），两 tab：滚动前推 / 参数敏感度。入口为回测管理页顶部两个按钮。发起用 modal（沿用 Backtest.vue 表单样式），列表+详情直接页内渲染。
- 组合/策略/公式改动全部落在现有表单，**不新增 schema 字段类型之外的东西**。
- 新增字段类型 `switch`（Portfolios schema builder 当前无 boolean 类型）。
- api 层 `web/src/api/index.ts` 增类型 + 函数；**不写 mock 服务**，vitest 断言调用 URL/参数序列化。
- 不做离线 HTML 报告扩展；实盘详情不加执行真实感。

## 错误与空态

- 404「组合不存在」/「回测记录不存在」→ 沿用现有 message 展示。
- 409 并发 → 发起模态保留并提示。
- WF 区间不足 → status=failed + error_message、windows "0/0"；空 windows 列表文案「无完整窗口」。
- PP 轮次失败 → summaries 内 error 字段逐轮展示；`rounds_run "k/n"`。
- gate_status=None：未启用或数据不足；reasons 展示「未评估」原因。
- 所有百分比展示统一走 `utils/format.ts` 既有 helper；新增 helper 放同一文件（若有）。

## 验收（每项可独立交叉验证）

1. 回测发起表单出现口径 select + 门开关/比例；提交请求 JSON 含 3 新字段；门开启时响应 banner 显示 gate 结果。
2. 记录列表有「门状态」列；历史记录显示「未启用」。
3. 详情有「采纳验收」折叠块，含 3 check 行 + pass 徽章 + 绝对/相对切换（重拉 `?profile=`）。
4. `/backtest/analysis` 两 tab 各可发起、列列表、看详情；WF 窗口表带 gate 徽章与 record_id 跳转；PP 有 baseline 卡片与 params 表 + most_sensitive。
5. Portfolios 组合表单有「执行真实感」组（涨跌停开关、成交量开关+比例）；子策略有「策略增强」组 7 字段；新建组合默认涨跌停开（服务端行为，前端提交不带该字段即可）。
6. Formulas 表单有「严格未来函数检查」checkbox；开启且含 ZIG 提交 → 顶部 errorMsg 展示 400 message。
7. `npm run build` + `vue-tsc -b` 全绿；vitest 全绿。
8. 部署侧（另行确认）：重启后端 uvicorn（dev）或重打包 exe + 重建 web/dist。

## 边界（明确不做，YAGNI）

- 不接受用户额外要求时不新增评估指标的「自定义阈值」UI；保留 8%/20%/1.0 等固定阈值展示。
- 不做 WF/PP 的 WebSocket/轮询进度——两者后端均同步内联，请求体阻塞完成后才返回（与主回测一致）。
- 不引入新的前端依赖/路由库（沿用 vue-router 现有配置）。