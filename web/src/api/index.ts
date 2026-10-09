import axios from 'axios'

export const api = axios.create({ baseURL: '/api' })

// #14：响应拦截器 — 业务错误（HTTP 200 但 body.code !== 0）转 reject。
// 后端统一响应 {code,message,data}，code=0 成功；非 0 是业务错误，但 HTTP 仍 200，
// axios 当成功 resolve → 调用方误处理（静默 bug）。此处统一拦下，reject 一个带
// response.data 的错误（让 errMsg 能读 message）。HTTP 错误（404/409/500）由
// axios 自动 reject，不动。
api.interceptors.response.use((response) => {
  const body = response.data as ApiResponse<unknown>
  if (body && typeof body.code === 'number' && body.code !== 0) {
    const err: Error & { response?: unknown } = new Error(body.message || '请求失败')
    err.response = response
    return Promise.reject(err)
  }
  return response
})

export interface ApiResponse<T> {
  code: number
  message?: string
  data: T
}

// ---------------------------------------------------------------------------
// 响应类型（对齐后端 serializer，审计 #26 any 清理）
// ---------------------------------------------------------------------------

// 本地已同步股票池（GET /stock-pools、POST /stock-pools/sync）— 对齐 stock_pools._serialize_pool
export interface StockPoolItem {
  id: number
  code: string
  name: string
  synced_at: string | null
  stock_count: number
}

// 通达信用户板块 + 本地残留合并（GET /stock-pools/tdx）
export interface TdxPoolItem {
  code: string
  name: string
  synced: boolean
  exists_in_tdx: boolean
  stock_count: number
}

// 通达信板块实时成分股（GET /stock-pools/tdx/{code}/stocks）— 对齐 tq.data.get_pool_stocks
export interface TdxPoolStockItem {
  stock_code: string
  stock_name: string
}

// 公式信号（响应侧，含 id）— 对齐 formulas._serialize_formula signals 子项
export interface FormulaSignalItem {
  id: number
  signal_name: string
  signal_type: string  // OPEN|ADD|REDUCE|CLOSE
  trigger_value: number  // -2|-1|0|1|2
}

// 公式（GET/POST/PUT /formulas）— 对齐 formulas._serialize_formula
export interface FormulaItem {
  id: number
  name: string
  content: string
  formula_count: number
  created_at: string | null
  updated_at: string | null
  signals: FormulaSignalItem[]
}

// 组合策略（GET/POST/PUT /portfolios）— 对齐 strategies._serialize_portfolio
export interface PortfolioItem {
  id: number
  name: string
  stock_pool_id: number
  benchmark_index: string | null
  initial_capital: number | null
  max_drawdown: number | null
  daily_loss_limit: number | null
  max_holdings: number | null
  min_commission: number | null
  buy_commission_rate: number | null
  sell_commission_rate: number | null
  stamp_duty_rate: number | null
  slippage: number | null
  trading_session: string  // full|am|pm
  status: string  // active|archived
  created_at: string | null
  updated_at: string | null
  strategies: StrategyDetail[]
}

// 回测记录（列表项）— 对齐 backtest._serialize_record
export interface BacktestRecordItem {
  id: number
  portfolio_strategy_id: number
  name: string
  start_date: string | null
  end_date: string | null
  status: string  // running|completed|failed
  error_message: string | null
  warnings: string[]  // 信号预检非致命警告（零信号/输出线缺失/公式报错）
  created_at: string | null
  completed_at: string | null
}

// 组合日快照 — 对齐 backtest._serialize_snapshot
export interface BacktestSnapshotItem {
  snap_date: string | null
  total_value: number | null
  cash: number | null
  market_value: number | null
  daily_return: number | null
  cumulative_return: number | null
  benchmark_value: number | null
}

// 回测交易 — 对齐 backtest._serialize_trade_with_name（详情内嵌，含 strategy_name）
export interface BacktestTradeItem {
  id: number
  strategy_id: number
  strategy_name: string
  signal_name: string | null
  signal_type: string | null
  stock_code: string
  trade_type: string  // BUY|SELL
  price: number | null
  quantity: number
  amount: number | null
  commission: number | null
  stamp_duty: number | null
  bar_time: string | null
}

// 回测评估 — 对齐 backtest._serialize_evaluation
export interface BacktestEvaluationItem {
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  volatility: number | null
  sharpe_ratio: number | null
  sortino_ratio: number | null
  calmar_ratio: number | null
  win_rate: number | null
  profit_factor: number | null
  total_trades: number | null
  benchmark_return: number | null
  avg_holding_days: number | null
  var_95: number | null
  cvar_95: number | null
  avg_recovery_days: number | null
  max_recovery_days: number | null
  ulcer_index: number | null
  return_stability: number | null
  // 基准相对 8 指标（regime-alpha suite）
  beta: number | null
  jensen_alpha: number | null
  excess_annual_return: number | null
  tracking_error: number | null
  information_ratio: number | null
  up_capture_ratio: number | null
  down_capture_ratio: number | null
  excess_max_drawdown: number | null
  // 磨损/仓位/结构 6 指标
  annual_turnover: number | null
  total_cost_ratio: number | null
  avg_position_utilization: number | null
  empty_days_ratio: number | null
  max_single_weight: number | null
  hhi: number | null
  // 路径 6 指标
  max_consecutive_loss_days: number | null
  max_consecutive_profit_days: number | null
  worst_daily_return: number | null
  worst_weekly_return: number | null
  return_skewness: number | null
  return_kurtosis: number | null
}

// 策略评估（详情内嵌，含 strategy_id/name）— 对齐 backtest get_record strategy_evaluations
export interface BacktestStrategyEvaluationItem extends BacktestEvaluationItem {
  strategy_id: number
  strategy_name: string
}

// 策略净值曲线（详情内嵌）— 对齐 backtest get_record strategy_snapshots
export interface BacktestStrategySnapshotItem {
  strategy_id: number
  strategy_name: string
  curve: { snap_date: string | null; total_value: number | null }[]
}

// 决策闸门事件（回测/实盘同口径）— 对齐 decision_event 序列化
// 覆盖信号→成交全链路拦截点：止损/止盈/熔断/资金不足/超仓/收盘/去重/桥拒单…
export interface DecisionEventItem {
  id?: number
  gate: string          // 闸门代码（stop_loss/max_positions_full/insufficient_funds/after_close_block…）
  layer: string         // strategy_risk|portfolio_risk|signal_gate|capital_gate|t1|live_gate
  action: string        // trigger|halt|recover|strip|block|reject|shrink|clamp
  portfolio_id: number | null
  strategy_id: number | null
  stock_code: string | null
  bar_time: string | null
  param_name: string | null   // 关联调参名（stop_loss_ratio/max_positions/max_drawdown…）
  param_value: number | null  // 阈值
  actual_value: number | null // 实际值（亏损%/回撤%/持仓数…）
  requested_qty: number | null
  final_qty: number | null
  message: string | null
  created_at?: string | null
}

// 决策闸门聚合统计（summarize_decisions，按 gate+param_name 分组）
export interface DecisionSummaryItem {
  gate: string
  layer: string
  action: string
  param_name: string | null
  param_value: number | null
  count: number
  first_bar_time: string | null
  last_bar_time: string | null
  stock_count: number
  requested_qty_sum: number | null
  final_qty_sum: number | null
}

// 决策事件查询响应（实盘 /decisions；回测详情内嵌同构字段）
export interface DecisionData {
  summary: DecisionSummaryItem[]
  events: DecisionEventItem[]
}

// 月度收益热力图矩阵（年度×12月）
export interface BacktestMonthlyMatrix {
  years: number[]
  months: string[]           // ["1月", "2月", ..., "12月"]
  matrix: (number | null)[][] // matrix[yearIdx][monthIdx] = 月收益% 或 null
  text: string[][]           // 同 shape，格式 "+1.23%"
}

// 年度统计（日收益分箱 + 月收益 + 年累计）
export interface BacktestYearlyStats {
  year: number
  daily_returns: number[]    // 该年日收益（小数）
  monthly_returns: number[]  // 该年月收益（小数）
  annual_return: number | null  // 年度累计收益（小数，复利；无收益年份为 null）
}

// 收益分布桶（月度热力图 + 年度箱线图数据源）
export interface BacktestReturnsBucket {
  monthly_matrix: BacktestMonthlyMatrix
  yearly: BacktestYearlyStats[]
}

export interface CrisisAlphaQuadrant {
  n_crisis: number
  n_down: number
  n_total: number
  hit_rate: number
  up_rate: number
  alpha_ratio: number
  follow_ratio: number
  odds: number | null
  counts: Record<string, number>
  avg_ret: Record<string, number>
  down_avg_s: number
  down_avg_b: number
  win_avg: number | null
  loss_avg: number
}

export interface CrisisAlphaCondition {
  threshold: number
  n: number
  b_avg: number
  s_avg: number
  up_rate: number
}

export interface CrisisAlphaWeekRow {
  日期: string
  基准?: number
  策略?: number
  基准收益?: number
  策略收益?: number
  超额?: number
  分类: string
}

export interface CrisisAlphaEvent {
  start: string
  end: string
  benchmark_ret: number
  strategy_ret: number
  excess: number
  hedged: boolean
  verdict: string
  max_dd: number
  days: number
}

export interface CrisisAlphaPoint {
  date: string
  value: number | null
}

export interface CrisisAlphaMonthState {
  n: number
  s_avg: number
  b_avg: number
  win_rate: number
}

export interface CrisisAlphaAnnual {
  年份: number
  下跌周: number
  危机Alpha: number
  策略收益: number
  基准收益: number
  完整: boolean
}

export interface CrisisAlphaCost {
  drag: number
  sync_up: number
  elasticity: number
  tail_corr: number | null
  q1_corr: number | null
  n_up: number
}

export interface CrisisAlphaPerfSide {
  cum: number
  ann: number
  vol: number | null
  mdd: number
}

export interface CrisisAlphaPerformance {
  s: CrisisAlphaPerfSide
  b: CrisisAlphaPerfSide
  corr: number | null
  start: string
  end: string
}

export interface CrisisAlphaDimension {
  维度: string
  类型: string
  权重: number
  得分: number
  评级: string
  贡献: number
}

export interface CrisisAlphaScore {
  total_score: number
  rating: string
  dimensions: CrisisAlphaDimension[]
}

export interface CrisisAlphaMultiBench {
  key: string
  name: string
  code: string
  alpha_ratio: number
  n_down: number
  up_rate: number
  is_main: boolean
}

export interface CrisisAlphaNav {
  dates: string[]
  strategy: (number | null)[]
  benchmark: (number | null)[]
  shade_spans: string[][]
}

export interface CrisisAlphaData {
  strategy_name: string
  benchmark_code: string
  benchmark_name: string
  alpha_rating: string
  n_weeks: number
  quadrant: CrisisAlphaQuadrant
  conditions: CrisisAlphaCondition[]
  extreme_top: CrisisAlphaWeekRow[]
  deep_dd_events: CrisisAlphaEvent[]
  rolling_corr: CrisisAlphaPoint[]
  expanding_corr: CrisisAlphaPoint[]
  monthly: {
    states: Record<string, CrisisAlphaMonthState>
    hedge_ratio: number
    n_down_month: number
    big_down: Array<{ 日期: string; 基准: number; 策略: number }>
  }
  annual: CrisisAlphaAnnual[]
  cost: CrisisAlphaCost
  performance: CrisisAlphaPerformance
  score: CrisisAlphaScore
  weekly_detail: CrisisAlphaWeekRow[]
  down_weeks: CrisisAlphaWeekRow[]
  nav: CrisisAlphaNav
  multi_bench: CrisisAlphaMultiBench[]
}

export interface BacktestCrisisAlphaItem {
  status: 'completed' | 'insufficient' | 'failed'
  error: string | null
  skipped_benchmarks: string[]
  computed_at: string | null
  benchmark_code: string
  benchmark_name: string
  data: CrisisAlphaData | null
}

// 回测详情（GET /backtest/records/{id}）— 对齐 backtest.get_record
export interface BacktestDetailItem {
  record: BacktestRecordItem
  snapshots: BacktestSnapshotItem[]
  trades: BacktestTradeItem[]
  evaluations: BacktestEvaluationItem | null
  strategy_evaluations: BacktestStrategyEvaluationItem[]
  strategy_snapshots: BacktestStrategySnapshotItem[]
  decision_summary: DecisionSummaryItem[]  // 风控与拦截统计（调参可观测性）
  decisions: DecisionEventItem[]           // 逐闸门事件（下钻）
  returns_buckets: BacktestReturnsBucket | null  // 收益分布（无快照时 null）
  crisis_alpha: BacktestCrisisAlphaItem | null
  market_regime: BacktestMarketRegimeItem | null
}

export async function getStockPools() {
  const res = await api.get<ApiResponse<StockPoolItem[]>>('/stock-pools')
  return res.data.data
}

// 通达信用户板块 + 本地残留合并：[{code, name, synced, exists_in_tdx, stock_count}]
export async function getTdxPools() {
  const res = await api.get<ApiResponse<TdxPoolItem[]>>('/stock-pools/tdx')
  return res.data.data
}

// 通达信板块实时成分股：[{stock_code, stock_name}]
export async function getTdxPoolStocks(code: string) {
  const res = await api.get<ApiResponse<TdxPoolStockItem[]>>(`/stock-pools/tdx/${code}/stocks`)
  return res.data.data
}

// 按 code 同步（upsert 本地池 + 全量替换成分股）；已同步的可重同步
export async function syncStockPool(req: { code: string }) {
  const res = await api.post<ApiResponse<StockPoolItem>>('/stock-pools/sync', req)
  return res.data.data
}

export async function deleteStockPool(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/stock-pools/${id}`)
  return res.data.data
}

export async function getFormulas() {
  const res = await api.get<ApiResponse<FormulaItem[]>>('/formulas')
  return res.data.data
}

export interface SignalItem {
  signal_name: string
  signal_type: string  // OPEN|ADD|REDUCE|CLOSE
  trigger_value: number  // 1 或 -1
}

export interface FormulaRequest {
  name: string
  content: string
  signals: SignalItem[]
  formula_count: number  // 注入历史根数（公式级，默认 200）
}

export async function getFormulaDetail(id: number) {
  const res = await api.get<ApiResponse<FormulaItem>>(`/formulas/${id}`)
  return res.data.data
}

export async function createFormula(req: FormulaRequest) {
  const res = await api.post<ApiResponse<FormulaItem>>('/formulas', req)
  return res.data.data
}

export async function updateFormula(id: number, req: FormulaRequest) {
  const res = await api.put<ApiResponse<FormulaItem>>(`/formulas/${id}`, req)
  return res.data.data
}

export async function deleteFormula(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/formulas/${id}`)
  return res.data.data
}

// 通达信公式导入三步（对齐 formulas.tdx_list / tdx_info / import）
export interface TdxFormulaListItem {
  acCode: string
  acName: string
  isSys: number | null
  imported: boolean
  formula_id: number | null
}

export interface TdxFormulaInfo {
  acCode: string
  acName: string
  isSys: number
  ParaNum: number
  Para?: { ParaName: string; Min: string; Max: string; Default: string }[]
  LineNum: number
  Line?: { LineName: string }[]
}

export async function getTdxFormulas(userOnly = true) {
  const res = await api.get<ApiResponse<TdxFormulaListItem[]>>('/formulas/tdx-list', {
    params: { user_only: userOnly },
  })
  return res.data.data
}

export async function getTdxInfo(acCode: string, formulaType = 0) {
  const res = await api.get<ApiResponse<TdxFormulaInfo>>('/formulas/tdx-info', {
    params: { ac_code: acCode, formula_type: formulaType },
  })
  return res.data.data
}

export async function importTdxFormula(acCode: string) {
  const res = await api.post<ApiResponse<FormulaItem>>('/formulas/import', { ac_code: acCode })
  return res.data.data
}

export async function getPortfolios() {
  const res = await api.get<ApiResponse<PortfolioItem[]>>('/portfolios')
  return res.data.data
}

export interface StrategyItem {
  name: string
  formula_id: number
  period: string  // 1m|5m|15m|30m|1h|1d|1w|1mon
  role: string  // independent|master|slave
  master_strategy_id: number | null  // 0=本批第N个；null=无
  stock_pool_id?: number | null  // null=继承组合级
  slave_exit_mode?: string  // independent|follow_master（仅 slave 有意义）
  capital_ratio: number
  max_positions: number
  single_open_ratio: number
  stop_loss_ratio: number
  take_profit_ratio: number
  trailing_stop_ratio: number
  add_position_threshold: number
  max_add_count: number
  add_position_ratio: number
  reduce_position_ratio: number
}

export interface PortfolioRequest {
  name: string
  stock_pool_id: number
  benchmark_index?: string
  initial_capital: number
  max_drawdown: number
  daily_loss_limit: number
  max_holdings: number
  min_commission: number
  buy_commission_rate: number
  sell_commission_rate: number
  stamp_duty_rate: number
  slippage: number
  trading_session: string  // full|am|pm
  status: string  // active|archived
  strategies: StrategyItem[]
}

export async function getPortfolioDetail(id: number) {
  const res = await api.get<ApiResponse<PortfolioItem>>(`/portfolios/${id}`)
  return res.data.data
}

export async function createPortfolio(req: PortfolioRequest) {
  const res = await api.post<ApiResponse<PortfolioItem>>('/portfolios', req)
  return res.data.data
}

export async function updatePortfolio(id: number, req: PortfolioRequest) {
  const res = await api.put<ApiResponse<PortfolioItem>>(`/portfolios/${id}`, req)
  return res.data.data
}

export async function deletePortfolio(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/portfolios/${id}`)
  return res.data.data
}

export interface StrategyDetail {
  id: number
  portfolio_id: number
  name: string
  formula_id: number
  period: string  // 1m|5m|15m|30m|1h|1d|1w|1mon
  role: string  // independent|master|slave
  master_strategy_id: number | null  // 已存在的同组合 master id
  stock_pool_id?: number | null  // null=继承组合级
  slave_exit_mode?: string  // independent|follow_master（仅 slave 有意义）
  capital_ratio: number
  max_positions: number
  single_open_ratio: number
  stop_loss_ratio: number
  take_profit_ratio: number
  trailing_stop_ratio: number
  add_position_threshold: number
  max_add_count: number
  add_position_ratio: number
  reduce_position_ratio: number
}

export interface StrategyRequest {
  name: string
  formula_id: number
  period: string  // 1m|5m|15m|30m|1h|1d|1w|1mon
  role: string  // independent|master|slave
  master_strategy_id: number | null  // 已存在的同组合 master id
  stock_pool_id?: number | null  // null=继承组合级
  slave_exit_mode?: string  // independent|follow_master（仅 slave 有意义）
  capital_ratio: number
  max_positions: number
  single_open_ratio: number
  stop_loss_ratio: number
  take_profit_ratio: number
  trailing_stop_ratio: number
  add_position_threshold: number
  max_add_count: number
  add_position_ratio: number
  reduce_position_ratio: number
}

// 独立子策略 CRUD（两层设计：组合下单独管理子策略）
export async function getStrategies(pid: number) {
  const res = await api.get<ApiResponse<StrategyDetail[]>>(`/portfolios/${pid}/strategies`)
  return res.data.data
}

export async function createStrategy(pid: number, req: StrategyRequest) {
  const res = await api.post<ApiResponse<StrategyDetail>>(`/portfolios/${pid}/strategies`, req)
  return res.data.data
}

export async function updateStrategy(pid: number, sid: number, req: StrategyRequest) {
  const res = await api.put<ApiResponse<StrategyDetail>>(`/portfolios/${pid}/strategies/${sid}`, req)
  return res.data.data
}

export async function deleteStrategy(pid: number, sid: number) {
  const res = await api.delete<ApiResponse<null>>(`/portfolios/${pid}/strategies/${sid}`)
  return res.data.data
}

export async function getBacktestRecords() {
  const res = await api.get<ApiResponse<BacktestRecordItem[]>>('/backtest/records')
  return res.data.data
}

export interface BacktestRequest {
  portfolio_strategy_id: number
  name: string
  start_date: string  // YYYY-MM-DD
  end_date: string    // YYYY-MM-DD
}

export async function runBacktest(req: BacktestRequest) {
  const res = await api.post<ApiResponse<{ record_id: number }>>('/backtest', req)
  return res.data.data
}

export async function getBacktestDetail(id: number) {
  const res = await api.get<ApiResponse<BacktestDetailItem>>(`/backtest/records/${id}`)
  return res.data.data
}

export async function deleteBacktestRecord(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/backtest/records/${id}`)
  return res.data.data
}

export interface CrisisAlphaBenchmarkResolution {
  from_snapshot: boolean
  mismatch: boolean
  benchmark_code: string
  benchmark_name: string
  live_benchmark_code: string
  live_benchmark_name: string
  stored_benchmark_code: string | null
  stored_benchmark_name: string | null
}

export interface CrisisAlphaRecomputeResult {
  status: string
  error: string | null
  computed_at: string | null
  benchmark_resolution?: CrisisAlphaBenchmarkResolution
}

export async function recomputeCrisisAlpha(id: number) {
  const res = await api.post<ApiResponse<CrisisAlphaRecomputeResult>>(
    `/backtest/records/${id}/crisis-alpha/recompute`)
  return res.data.data
}

// ---- 市场状态×绩效分析（机遇/中性Alpha，regime-alpha 阶段二）----

export interface MarketRegimeStateRow {
  label: string
  label_cn: string
  weeks: number
  ratio: number | null
  strategy_return: number | null
  benchmark_return: number | null
  excess_return: number | null
  win_rate: number | null
  max_drawdown: number | null
  turnover: number | null
  cost_ratio: number | null
}

export interface MarketRegimeBigUpTest {
  threshold: number
  n: number
  avg_strategy: number | null
  avg_benchmark: number | null
  win_rate: number | null
}

export interface MarketRegimeOppCat {
  count: number
  avg_strategy: number | null
}

export interface MarketRegimeReboundSegment {
  start_date: string
  end_date: string
  trough_date: string
  weeks: number
  benchmark_rebound_pct: number | null
  strategy_rebound_pct: number | null
  elasticity: number | null
  benchmark_recover_days: number | null
  benchmark_recovered: boolean
  strategy_recover_days: number | null
  strategy_recovered: boolean
}

export interface MarketRegimeData {
  meta: {
    strategy_name: string
    benchmark_name: string
    n_weeks_labeled: number
    n_weeks_total: number
    state_week_counts: Record<string, number>
  }
  regime_table: Record<string, MarketRegimeStateRow>
  opportunity: {
    n_bull_weeks: number
    up_capture: number | null
    miss_ratio: number
    big_up_tests: MarketRegimeBigUpTest[]
    cats: Record<string, MarketRegimeOppCat>
  }
  rebound: {
    n_segments: number
    avg_elasticity: number | null
    segments: MarketRegimeReboundSegment[]
  }
  neutral: {
    n_range_weeks: number
    range_ratio: number | null
    cumulative_return: number | null
    annualized_return: number | null
    win_rate: number | null
    n_range_days: number
    monthly_trades: number | null
    annual_turnover: number | null
    cost_gross_ratio: number | null
    whipsaw_count: number
    whipsaw_days: number
    avg_trade_pnl: {
      trend: number | null
      trend_count: number
      range: number | null
      range_count: number
    }
  }
  style: StyleBlock
  dispersion: DispersionBlock
  exposure: ExposureBlock
  // 增强A：巨潮四象限纯风格暴露（规模×成长/价值，399372~399377）；老记录无此键
  quadrant_exposure?: ExposureBlock
  industry_attribution: IndustryAttributionBlock
  robustness: RobustnessBlock
}

// ---- 轻量稳健性（FR-20）----

export interface RobustHalfYearRow {
  period: string
  start_date: string
  end_date: string
  strategy_return: number
  benchmark_return: number
  excess_return: number
  max_drawdown: number
  sharpe: number | null
}

export interface RobustDist {
  median: number | null
  min: number | null
  max: number | null
  negative_ratio: number | null
}

export interface RobustWindow {
  window: number
  n_samples: number
  returns: RobustDist
  sharpe: RobustDist
  histogram: { label: string; count: number }[]
}

export interface RobustSensitivityRow {
  code: string
  name: string
  is_primary: boolean
  available: boolean
  n_days: number
  strategy_total_return: number | null
  benchmark_total_return: number | null
  beta: number | null
  jensen_alpha: number | null
  excess_annual_return: number | null
  tracking_error: number | null
  information_ratio: number | null
  up_capture_ratio: number | null
  down_capture_ratio: number | null
  excess_max_drawdown: number | null
}

export interface RobustnessBlock {
  status: 'ok' | 'insufficient' | 'failed'
  error?: string
  n_days?: number
  start_date?: string
  end_date?: string
  half_year_matrix?: RobustHalfYearRow[]
  rolling?: {
    weekly_points: number
    windows: RobustWindow[]
  }
  longest_loss_window?: {
    start_date: string | null
    trough_date: string | null
    end_date: string | null
    days: number
    drawdown: number | null
  }
  benchmark_sensitivity?: RobustSensitivityRow[]
}

// ---- 风格轮动 / 分化度 / 隐含风格暴露（FR-13 ~ FR-16）----

export interface StyleWeekRow {
  date: string
  label: string
  leader_code: string | null
  leader_return: number
  // FR-13 双窗口（Minor-2 修复）：20W 快窗口标签，老记录 payload 无此键
  fast_label?: string
  fast_leader_code?: string | null
}

export interface StyleMatrixRow {
  label: string
  weeks: number
  ratio: number
  strategy_return: number | null
  leader_return: number | null
  excess_return: number | null
  win_rate: number | null
  max_drawdown: number | null
}

export interface StyleBlock {
  available_codes: string[]
  skipped: string[]
  weeks: StyleWeekRow[]
  matrix: Record<string, StyleMatrixRow>
  // FR-13 双窗口：快窗口周数与 20W/60W 标签一致周占比（老记录无此键）
  fast_window?: number
  label_agree_ratio?: number | null
}

export interface DispersionPoint {
  date: string
  dispersion: number
}

export interface DispersionBucket {
  count: number
  mean_excess: number | null
  win_rate: number | null
  threshold: number | null
}

export interface DispersionBlock {
  available_codes: string[]
  skipped: string[]
  series: DispersionPoint[]
  buckets: {
    low: DispersionBucket
    mid: DispersionBucket
    high: DispersionBucket
  }
}

export interface ExposureBlock {
  available_codes: string[]
  available_names: string[]
  skipped: string[]
  dates: string[]
  corr: Record<string, (number | null)[]>
  beta: Record<string, (number | null)[]>
  window: number
}

// ---- 行业归因（FR-17，通达信二级行业 881）----

export interface IndustryExposureSeries {
  code: string
  name: string
  avg_weight: number
  weights: number[]
}

export interface IndustryExposureBlock {
  dates: string[]
  series: IndustryExposureSeries[]
  cash: number[]
}

export interface IndustryHitRatePeriod {
  top_n: number
  periods: number
  periods_holding: number
  hits: number
  hit_rate: number | null
  details: {
    period: string
    held: string[]
    leaders: string[]
    hit: boolean
  }[]
}

export interface IndustryContributionRow {
  code: string
  name: string
  avg_weight: number
  index_return: number | null
  contribution: number
}

export interface IndustryAttributionBlock {
  status: 'ok' | 'needs_rerun' | 'failed'
  taxonomy: string
  reason?: string
  error?: string
  n_days?: number
  unmapped_codes?: string[]
  exposure?: IndustryExposureBlock
  hit_rate?: {
    weekly: IndustryHitRatePeriod
    monthly: IndustryHitRatePeriod
  }
  contribution?: {
    available: boolean
    rows: IndustryContributionRow[]
    total_contribution: number | null
  }
}

export interface BacktestMarketRegimeItem {
  status: 'completed' | 'insufficient' | 'failed'
  error: string | null
  computed_at: string | null
  benchmark_code: string
  benchmark_name: string
  data: MarketRegimeData | null
}

export async function recomputeMarketRegime(id: number) {
  const res = await api.post<ApiResponse<CrisisAlphaRecomputeResult>>(
    `/backtest/records/${id}/market-regime/recompute`)
  return res.data.data
}

// ---- B4b: 实盘历史查询（订单/成交/持仓）----

export interface LiveOrderItem {
  id: number
  stock_code: string
  trade_type: string  // BUY|SELL
  order_type: string
  price: number | null
  quantity: number
  filled_quantity: number
  filled_price: number | null
  status: string  // submitted|filled|partial|rejected|canceled
  error_message: string | null
  signal_name: string | null
  signal_type: string | null
  bar_time: string | null
  created_at: string | null
}

export interface LiveTradeItem {
  id: number
  stock_code: string
  trade_type: string
  price: number
  quantity: number
  amount: number
  commission: number
  stamp_duty: number
  trade_time: string | null
}

export interface LivePositionItem {
  stock_code: string
  quantity: number
  avg_cost: number
  market_value: number
  // 归属（组合策略/子策略）；旧数据/旧接口可能缺省
  portfolio_id?: number | null
  strategy_id?: number | null
}

export async function getLiveOrders(sessionId: number, status?: string) {
  const params = status ? { status } : {}
  const res = await api.get<ApiResponse<LiveOrderItem[]>>(`/live/sessions/${sessionId}/orders`, { params })
  return res.data.data
}

export async function getLiveTrades(sessionId: number) {
  const res = await api.get<ApiResponse<LiveTradeItem[]>>(`/live/sessions/${sessionId}/trades`)
  return res.data.data
}

export async function getLivePositions(sessionId: number) {
  const res = await api.get<ApiResponse<LivePositionItem[]>>(`/live/sessions/${sessionId}/positions`)
  return res.data.data
}

// 决策闸门事件（调参可观测性）：聚合统计 + 逐事件，对齐 live.session_decisions
export async function getLiveDecisions(sessionId: number) {
  const res = await api.get<ApiResponse<DecisionData>>(`/live/sessions/${sessionId}/decisions`)
  return res.data.data
}

// ---- #27: 实盘会话 CRUD/启停（LiveSessions.vue 曾原生 axios 直连）----
// 列表对齐 live.py list_sessions serializer；启停/create 返回 {id, status}

export interface LiveSessionItem {
  id: number
  name: string
  mode: string  // simulation|live
  status: string  // stopped|running
  started_at: string | null
  stopped_at: string | null
  portfolio_ids: number[]
  portfolios?: { portfolio_id: number; status: string }[]  // 组合级状态(active|circuit_broken)
}

export interface LiveSessionCreate {
  name: string
  mode: string
  portfolio_ids: number[]
}

export async function getLiveSessions() {
  const res = await api.get<ApiResponse<LiveSessionItem[]>>('/live/sessions')
  return res.data.data
}

export async function createLiveSession(req: LiveSessionCreate) {
  const res = await api.post<ApiResponse<{ id: number; status: string }>>('/live/sessions', req)
  return res.data.data
}

export async function startLiveSession(id: number) {
  const res = await api.post<ApiResponse<{ id: number; status: string }>>(`/live/sessions/${id}/start`)
  return res.data.data
}

export async function stopLiveSession(id: number) {
  const res = await api.post<ApiResponse<{ id: number; status: string }>>(`/live/sessions/${id}/stop`)
  return res.data.data
}

export async function deleteLiveSession(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/live/sessions/${id}`)
  return res.data.data
}

export async function recoverLiveBreaker(sessionId: number, portfolioId: number) {
  const res = await api.post<ApiResponse<{ portfolio_id: number; status: string; circuit_breaker_count: number }>>(
    `/live/sessions/${sessionId}/portfolios/${portfolioId}/recover`,
  )
  return res.data.data
}

// ---- #27: 系统配置（SystemConfig.vue 曾原生 axios 直连）----
// 对齐 core/config.py _defaults()；GET 全量返回、PUT 全量覆盖写

export interface SystemConfig {
  tdx_path: string
  iquant_path: string
  max_concurrent_backtest: number
  database: { sqlite_path: string }
  iquant_bridge: {
    simulation: { base_url: string }
    live: { base_url: string }
  }
}

export interface SystemStatus {
  core: { online: boolean; version: string; uptime: string }
}

export async function getSystemStatus() {
  const res = await api.get<ApiResponse<SystemStatus>>('/status')
  return res.data.data
}

export async function getSystemConfigs() {
  const res = await api.get<ApiResponse<SystemConfig>>('/system/configs')
  return res.data.data
}

export async function updateSystemConfigs(cfg: SystemConfig) {
  const res = await api.put<ApiResponse<null>>('/system/configs', cfg)
  return res.data.data
}
