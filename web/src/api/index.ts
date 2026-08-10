import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

export interface ApiResponse<T> {
  code: number
  message?: string
  data: T
}

export async function getStockPools() {
  const res = await api.get<ApiResponse<any[]>>('/stock-pools')
  return res.data.data
}

// 通达信用户板块 + 本地残留合并：[{code, name, synced, exists_in_tdx, stock_count}]
export async function getTdxPools() {
  const res = await api.get<ApiResponse<any[]>>('/stock-pools/tdx')
  return res.data.data
}

// 通达信板块实时成分股：[{stock_code, stock_name}]
export async function getTdxPoolStocks(code: string) {
  const res = await api.get<ApiResponse<any[]>>(`/stock-pools/tdx/${code}/stocks`)
  return res.data.data
}

// 按 code 同步（upsert 本地池 + 全量替换成分股）；已同步的可重同步
export async function syncStockPool(req: { code: string }) {
  const res = await api.post<ApiResponse<any>>('/stock-pools/sync', req)
  return res.data.data
}

export async function deleteStockPool(id: number) {
  const res = await api.delete<ApiResponse<any>>(`/stock-pools/${id}`)
  return res.data.data
}

export async function getFormulas() {
  const res = await api.get<ApiResponse<any[]>>('/formulas')
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
  const res = await api.get<ApiResponse<any>>(`/formulas/${id}`)
  return res.data.data
}

export async function createFormula(req: FormulaRequest) {
  const res = await api.post<ApiResponse<any>>('/formulas', req)
  return res.data.data
}

export async function updateFormula(id: number, req: FormulaRequest) {
  const res = await api.put<ApiResponse<any>>(`/formulas/${id}`, req)
  return res.data.data
}

export async function deleteFormula(id: number) {
  const res = await api.delete<ApiResponse<any>>(`/formulas/${id}`)
  return res.data.data
}

export async function getPortfolios() {
  const res = await api.get<ApiResponse<any[]>>('/portfolios')
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
  const res = await api.get<ApiResponse<any>>(`/portfolios/${id}`)
  return res.data.data
}

export async function createPortfolio(req: PortfolioRequest) {
  const res = await api.post<ApiResponse<any>>('/portfolios', req)
  return res.data.data
}

export async function updatePortfolio(id: number, req: PortfolioRequest) {
  const res = await api.put<ApiResponse<any>>(`/portfolios/${id}`, req)
  return res.data.data
}

export async function deletePortfolio(id: number) {
  const res = await api.delete<ApiResponse<any>>(`/portfolios/${id}`)
  return res.data.data
}

export interface StrategyDetail {
  id: number
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
  const res = await api.delete<ApiResponse<any>>(`/portfolios/${pid}/strategies/${sid}`)
  return res.data
}

export async function getBacktestRecords() {
  const res = await api.get<ApiResponse<any[]>>('/backtest/records')
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
  const res = await api.get<ApiResponse<any>>(`/backtest/records/${id}`)
  return res.data.data
}

export async function deleteBacktestRecord(id: number) {
  const res = await api.delete<ApiResponse<any>>(`/backtest/records/${id}`)
  return res.data.data
}
