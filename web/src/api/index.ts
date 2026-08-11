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
  trigger_value: number  // 1 | -1
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
  progress: number | null
  error_message: string | null
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

// 回测详情（GET /backtest/records/{id}）— 对齐 backtest.get_record
export interface BacktestDetailItem {
  record: BacktestRecordItem
  snapshots: BacktestSnapshotItem[]
  trades: BacktestTradeItem[]
  evaluations: BacktestEvaluationItem | null
  strategy_evaluations: BacktestStrategyEvaluationItem[]
  strategy_snapshots: BacktestStrategySnapshotItem[]
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
