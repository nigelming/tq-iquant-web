<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas,
  getStrategies, createStrategy, updateStrategy, deleteStrategy,
  type PortfolioRequest, type StrategyRequest, type StrategyDetail,
  type PortfolioItem, type StockPoolItem, type FormulaItem,
} from '../api'
import PageHeader from '../components/PageHeader.vue'

// ===== 数据 =====
const portfolios = ref<PortfolioItem[]>([])
const stockPools = ref<StockPoolItem[]>([])
const formulas = ref<FormulaItem[]>([])
const loading = ref(true)

// 展开的组合 id 集合 + 各组合子策略缓存
const expanded = ref<Set<number>>(new Set())
const strategyCache = ref<Record<number, StrategyDetail[]>>({})

// ===== 组合表单 =====
const showPortfolioForm = ref(false)
const editingPortfolioId = ref<number | null>(null)
const portfolioForm = ref<Record<string, any>>(emptyPortfolioForm())
const showGuide = ref(false)
const showStrategyGuide = ref(false)

// ===== 子策略表单 =====
const showStrategyForm = ref(false)
const strategyPortfolioId = ref<number | null>(null)  // 当前编辑的子策略所属组合
const editingStrategyId = ref<number | null>(null)
const strategyForm = ref<Record<string, any>>(emptyStrategyForm())

// ===== 枚举 =====
// 周期取 TQ 公式支持 ∩ iQuant 桥 xtdata 交集（open-questions Q4）+ C6 三段式：
// 1m/5m/15m/30m/1h 走桥 BarPoller + 边界分发；1d 走 14:30 快照；1w/1mon 走通达信注入。
// 60m 两端都不认。
const PERIODS = ['1m', '5m', '15m', '30m', '1h', '1d', '1w', '1mon']
const ROLES = [
  { value: 'independent', label: '独立' },
  { value: 'master', label: '主策略' },
  { value: 'slave', label: '从策略' },
]
const TRADING_SESSIONS = [
  { value: 'full', label: '全天' },
  { value: 'am', label: '仅上午' },
  { value: 'pm', label: '仅下午' },
]
const SLAVE_EXIT_MODES = [
  { value: 'independent', label: '独立卖出' },
  { value: 'follow_master', label: '跟随主策略' },
]
const POSITION_SIZINGS = [
  { value: 'dynamic', label: '动态净资产（按当前总净值）' },
  { value: 'fixed', label: '固定名义（按初始资金）' },
]

// 百分比字段集合（后台存小数，前台输入/显示百分比，提交时 ÷100 转回小数）
const PORTFOLIO_RATIO_FIELDS = new Set([
  'max_drawdown', 'daily_loss_limit',
  'buy_commission_rate', 'sell_commission_rate', 'stamp_duty_rate', 'slippage',
  'volume_limit_ratio', 'cash_enhance_position_ratio_threshold',
  'slippage_impact_coef',
])
const STRATEGY_RATIO_FIELDS = new Set([
  'capital_ratio', 'single_open_ratio',
  'stop_loss_ratio', 'take_profit_ratio', 'trailing_stop_ratio',
  'add_position_threshold', 'add_position_ratio', 'reduce_position_ratio',
  'rotation_hysteresis', 'trend_trailing_stop_ratio',
])

// 表单字段元数据：key/名称/一行几个（≤2 保证不溢出）。分组用于弹窗分区。
interface FieldMeta {
  key: string
  label: string
  span?: number
  type: string
  placeholder?: string
  options?: string
  hint?: string
  showIf?: string | { key: string; equals: unknown }
}
const PORTFOLIO_GROUPS: { title: string; fields: FieldMeta[] }[] = [
  { title: '基本信息', fields: [
    { key: 'name', label: '名称', span: 2, type: 'text', placeholder: '例如：稳健组合' },
    { key: 'stock_pool_id', label: '股票池', span: 1, type: 'select', options: 'pools' },
    { key: 'benchmark_index', label: '基准指数', span: 1, type: 'text', placeholder: '000300.SH' },
    { key: 'initial_capital', label: '初始资金（元）', span: 2, type: 'number' },
  ]},
  { title: '风控参数', fields: [
    { key: 'max_drawdown', label: '最大回撤', span: 1, type: 'percent', placeholder: '20' },
    { key: 'daily_loss_limit', label: '日亏损限', span: 1, type: 'percent', placeholder: '5' },
    { key: 'max_holdings', label: '最大持仓数', span: 2, type: 'number', placeholder: '10' },
  ]},
  { title: '交易成本', fields: [
    { key: 'min_commission', label: '最低佣金（元）', span: 1, type: 'number', placeholder: '5' },
    { key: 'buy_commission_rate', label: '买佣金率', span: 1, type: 'percent', placeholder: '0.025' },
    { key: 'sell_commission_rate', label: '卖佣金率', span: 1, type: 'percent', placeholder: '0.025' },
    { key: 'stamp_duty_rate', label: '印花税率', span: 1, type: 'percent', placeholder: '0.05' },
    { key: 'slippage', label: '滑点', span: 2, type: 'percent', placeholder: '0' },
  ]},
  { title: '交易时段', fields: [
    { key: 'trading_session', label: '交易时段', span: 2, type: 'select', options: 'sessions' },
  ]},
  { title: '执行真实感', fields: [
    { key: 'price_limit_check_enabled', label: '涨跌停约束', span: 1, type: 'switch', hint: '涨停买不进/跌停卖不出，延后重试' },
    { key: 'volume_limit_enabled', label: '成交量约束', span: 1, type: 'switch', hint: '按 bar 成交量占比封顶' },
    { key: 'volume_limit_ratio', label: '成交量占比上限', span: 2, type: 'percent', placeholder: '10', showIf: { key: 'volume_limit_enabled', equals: true } },
    { key: 'adaptive_slippage_enabled', label: '自适应滑点', span: 1, type: 'switch', hint: '滑点随订单量占 bar 成交量比例上浮（市场冲击成本）' },
    { key: 'slippage_impact_coef', label: '滑点冲击系数', span: 1, type: 'percent', placeholder: '10', showIf: { key: 'adaptive_slippage_enabled', equals: true }, hint: '订单吞掉整根 bar 成交量时冲击 10%，封顶' },
  ]},
  { title: '仓位与现金管理', fields: [
    { key: 'position_sizing', label: '仓位模型', span: 2, type: 'select', options: 'positionSizings', hint: '动态=按组合当前总净值分配资金；固定=按初始资金（旧行为）' },
    { key: 'equity_cap', label: '容量上限（元）', span: 2, type: 'number', placeholder: '不设上限', showIf: { key: 'position_sizing', equals: 'dynamic' }, hint: '动态仓位的资金封顶：净值超上限后仓位不再放大（模拟超容量撤回/封盘），None=不设上限' },
    { key: 'cash_enhance_enabled', label: '现金管理增强（预留）', span: 2, type: 'switch', hint: '命中阈值仅记录决策事件，暂不自动下单' },
    { key: 'cash_enhance_equity_threshold', label: '净资产阈值（元）', span: 1, type: 'number', showIf: { key: 'cash_enhance_enabled', equals: true } },
    { key: 'cash_enhance_position_ratio_threshold', label: '仓位占比阈值', span: 1, type: 'percent', placeholder: '30', showIf: { key: 'cash_enhance_enabled', equals: true } },
  ]},
]

const STRATEGY_GROUPS: { title: string; fields: FieldMeta[] }[] = [
  { title: '基本信息', fields: [
    { key: 'name', label: '名称', span: 2, type: 'text', placeholder: '子策略名称' },
    { key: 'formula_id', label: '公式', span: 1, type: 'select', options: 'formulas' },
    { key: 'period', label: '周期', span: 1, type: 'select', options: 'periods' },
    { key: 'role', label: '角色', span: 1, type: 'select', options: 'roles' },
    { key: 'master_strategy_id', label: '主策略', span: 1, type: 'select', options: 'masters', showIf: 'slave' },
    { key: 'stock_pool_id', label: '股票池', span: 1, type: 'select', options: 'strategyPools' },
    { key: 'slave_exit_mode', label: '卖出模式', span: 1, type: 'select', options: 'slaveExitModes', showIf: 'slave' },
  ]},
  { title: '资金参数', fields: [
    { key: 'capital_ratio', label: '资金占比', span: 1, type: 'percent', placeholder: '60' },
    { key: 'max_positions', label: '最大持仓数', span: 1, type: 'number', placeholder: '5' },
    { key: 'single_open_ratio', label: '单仓占比', span: 2, type: 'percent', placeholder: '10' },
  ]},
  { title: '风控参数', fields: [
    { key: 'stop_loss_ratio', label: '止损', span: 1, type: 'percent', placeholder: '5' },
    { key: 'take_profit_ratio', label: '止盈', span: 1, type: 'percent', placeholder: '15' },
    { key: 'trailing_stop_ratio', label: '移动止损', span: 2, type: 'percent', placeholder: '3' },
  ]},
  { title: '加仓参数', fields: [
    { key: 'add_position_threshold', label: '加仓阈值', span: 1, type: 'percent', placeholder: '5（-1=任何价）' },
    { key: 'max_add_count', label: '加仓次数', span: 1, type: 'number', placeholder: '2' },
    { key: 'add_position_ratio', label: '加仓比例', span: 1, type: 'percent', placeholder: '10' },
    { key: 'reduce_position_ratio', label: '减仓比例', span: 1, type: 'percent', placeholder: '30' },
  ]},
  { title: '策略增强（默认关闭）', fields: [
    { key: 'rotation_enabled', label: 'OPEN 择优轮动', span: 1, type: 'switch', hint: '满槽时强信号轮出最弱持仓' },
    { key: 'regime_filter_enabled', label: '市场状态闸', span: 1, type: 'switch', hint: '基准周线 bull 态用趋势参数' },
    { key: 'rotation_momentum_bars', label: '轮动动量bars', span: 1, type: 'number', placeholder: '20', showIf: { key: 'rotation_enabled', equals: true } },
    { key: 'rotation_hysteresis', label: '轮动滞后', span: 1, type: 'percent', placeholder: '5', showIf: { key: 'rotation_enabled', equals: true } },
    { key: 'trend_trailing_stop_ratio', label: '趋势移动止损', span: 1, type: 'percent', placeholder: '3', showIf: { key: 'regime_filter_enabled', equals: true } },
    { key: 'trend_max_positions', label: '趋势最大持仓', span: 1, type: 'number', placeholder: '5', showIf: { key: 'regime_filter_enabled', equals: true } },
    { key: 'exposure_cap_enabled', label: '策略级敞口上限', span: 2, type: 'switch', hint: '总持仓市值 ≤ capital_ratio × 初始资金' },
  ]},
]

function emptyStrategyForm(): Record<string, any> {
  return {
    name: '', formula_id: 0, period: '1d', role: 'independent',
    master_strategy_id: null, stock_pool_id: null, slave_exit_mode: 'independent',
    capital_ratio: 0.6, max_positions: 5,
    single_open_ratio: 0.1,
    stop_loss_ratio: 0.05, take_profit_ratio: 0.15, trailing_stop_ratio: 0.03,
    add_position_threshold: -1, max_add_count: 2,  // -1=任何价都加（页面默认即跳过回撤检查）
    add_position_ratio: 0.1, reduce_position_ratio: 0.3,
    rotation_enabled: false, rotation_momentum_bars: 20, rotation_hysteresis: 0.05,
    regime_filter_enabled: false, trend_trailing_stop_ratio: null, trend_max_positions: null,
    exposure_cap_enabled: false,
  }
}

function emptyPortfolioForm(): Record<string, any> {
  return {
    name: '', stock_pool_id: 0, benchmark_index: '000300.SH',
    initial_capital: 500000, max_drawdown: 0.2, daily_loss_limit: 0.05,
    max_holdings: 10,
    min_commission: 5, buy_commission_rate: 0.00025, sell_commission_rate: 0.00025,
    stamp_duty_rate: 0.0005, slippage: 0,
    trading_session: 'full', status: 'active',
    price_limit_check_enabled: true, volume_limit_enabled: false, volume_limit_ratio: 0.1,
    adaptive_slippage_enabled: false, slippage_impact_coef: 0.1,
    position_sizing: 'dynamic', cash_enhance_enabled: false,
    cash_enhance_equity_threshold: null, cash_enhance_position_ratio_threshold: null,
    equity_cap: null,
    strategies: [],
  }
}

// ===== 百分比 ↔ 小数转换 =====
// 仅对 ratio 字段做 ×100 / ÷100；其他字段原值透传。文本字段保持字符串。
function toPercent(v: string | number | null | undefined, isRatio: boolean) {
  if (!isRatio || v === null || v === undefined || v === '') return v
  const n = Number(v)
  // -1 是 add_position_threshold 的"任何价都加"特殊标记，不做 ×100
  if (n === -1) return v
  return n * 100
}
function fromPercent(v: string | number | null | undefined, fld: { type: string; key: string }) {
  // select 字段（trading_session/period/role/stock_pool_id 等）原值透传，
  // 不做 Number() —— 字符串值如 'full' 会被转成 NaN 致后端 422。
  if (fld.type !== 'number' && fld.type !== 'percent') return v
  if (v === null || v === undefined || v === '') return v
  const num = Number(v)
  // add_position_threshold 的 -1 是"任何价都加"的特殊标记（非百分比），不除以 100
  if (fld.key === 'add_position_threshold' && num === -1) return num
  return STRATEGY_RATIO_FIELDS.has(fld.key) || PORTFOLIO_RATIO_FIELDS.has(fld.key) ? num / 100 : num
}

// ===== 加载 =====
async function loadPortfolios() {
  try {
    const [ps, pools, fs] = await Promise.all([
      getPortfolios(),
      getStockPools().catch(() => []),
      getFormulas().catch(() => []),
    ])
    portfolios.value = ps
    stockPools.value = pools
    formulas.value = fs
  } catch (e) {
    alert(`加载失败：${errMsg(e)}`)
    portfolios.value = []
  } finally {
    loading.value = false
  }
}

async function toggleExpand(p: PortfolioItem) {
  if (expanded.value.has(p.id)) {
    expanded.value.delete(p.id)
  } else {
    expanded.value.add(p.id)
    if (!strategyCache.value[p.id]) {
      try {
        strategyCache.value[p.id] = await getStrategies(p.id)
      } catch (e) {
        alert(`加载子策略失败：${errMsg(e)}`)
      }
    }
  }
}

function poolName(id: number) {
  return stockPools.value.find(p => p.id === id)?.name || `#${id}`
}

function formulaName(id: number) {
  return formulas.value.find(f => f.id === id)?.name || `#${id}`
}

// 同组合下 role=master 的子策略（供 slave 选主策略）；编辑时排除自身，避免自引用
function masterOptions(pid: number, excludeId: number | null = null) {
  return (strategyCache.value[pid] || []).filter(s => s.role === 'master' && s.id !== excludeId)
}

// 提交组合表单：比例字段从百分比转回小数
function buildPortfolioPayload(): PortfolioRequest {
  const f = portfolioForm.value
  const out: Record<string, unknown> = { strategies: [] }
  for (const g of PORTFOLIO_GROUPS) for (const fld of g.fields) {
    if (!fieldVisible(fld, f)) continue
    out[fld.key] = fromPercent(f[fld.key], fld)
  }
  out.status = f.status
  // equity_cap 容量上限：0/空/未填一律归一为 null（不设上限）——后端校验须 >0，
  // 用户留空（默认"不设上限"）或填 0 都不应触发 400 拒绝。
  if (out.equity_cap === '' || out.equity_cap === null || out.equity_cap === 0) {
    out.equity_cap = null
  }
  return out as unknown as PortfolioRequest
}

// 提交子策略表单：比例字段从百分比转回小数
function buildStrategyPayload(): StrategyRequest {
  const f = strategyForm.value
  const out: Record<string, unknown> = {}
  for (const g of STRATEGY_GROUPS) for (const fld of g.fields) {
    if (!fieldVisible(fld, f)) continue
    out[fld.key] = fromPercent(f[fld.key], fld)
  }
  // showIf 跳过 master_strategy_id 时，独立/主策略置 null
  if (f.role !== 'slave') {
    out.master_strategy_id = null
    out.slave_exit_mode = 'independent'
  }
  return out as unknown as StrategyRequest
}

// ===== 组合 CRUD =====
function openCreatePortfolio() {
  editingPortfolioId.value = null
  portfolioForm.value = emptyPortfolioForm()
  // 把比例默认值转成百分比显示
  for (const k of PORTFOLIO_RATIO_FIELDS) {
    portfolioForm.value[k] = toPercent(portfolioForm.value[k], true)
  }
  showPortfolioForm.value = true
}

async function openEditPortfolio(p: PortfolioItem) {
  editingPortfolioId.value = p.id
  let detail: PortfolioItem
  try {
    detail = await getPortfolioDetail(p.id)
  } catch (e) {
    alert(`加载组合详情失败：${errMsg(e)}`)
    return
  }
  portfolioForm.value = {
    name: detail.name, stock_pool_id: detail.stock_pool_id,
    benchmark_index: detail.benchmark_index || '000300.SH',
    initial_capital: detail.initial_capital, max_drawdown: detail.max_drawdown,
    daily_loss_limit: detail.daily_loss_limit, max_holdings: detail.max_holdings,
    min_commission: detail.min_commission ?? 5,
    buy_commission_rate: detail.buy_commission_rate ?? 0.00025,
    sell_commission_rate: detail.sell_commission_rate ?? 0.00025,
    stamp_duty_rate: detail.stamp_duty_rate ?? 0.0005,
    slippage: detail.slippage ?? 0,
    trading_session: detail.trading_session, status: detail.status,
    price_limit_check_enabled: detail.price_limit_check_enabled,
    volume_limit_enabled: detail.volume_limit_enabled,
    volume_limit_ratio: detail.volume_limit_ratio,
    adaptive_slippage_enabled: detail.adaptive_slippage_enabled ?? false,
    slippage_impact_coef: detail.slippage_impact_coef ?? 0.1,
    position_sizing: detail.position_sizing || 'dynamic',
    cash_enhance_enabled: detail.cash_enhance_enabled,
    cash_enhance_equity_threshold: detail.cash_enhance_equity_threshold,
    cash_enhance_position_ratio_threshold: detail.cash_enhance_position_ratio_threshold,
    equity_cap: detail.equity_cap ?? null,
    strategies: [],
  }
  // 比例字段转百分比显示
  for (const k of PORTFOLIO_RATIO_FIELDS) {
    portfolioForm.value[k] = toPercent(portfolioForm.value[k], true)
  }
  showPortfolioForm.value = true
}

// 从 axios 错误里提取后端错误消息（统一响应 {code,message} 或 Pydantic 422 detail）
function errMsg(e: any): string {
  const d = e?.response?.data
  if (d?.message) return d.message
  if (Array.isArray(d?.detail)) return d.detail.map((x: { loc?: unknown[]; msg?: string }) => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ')
  if (typeof d?.detail === 'string') return d.detail
  return e?.message || '请求失败'
}

async function submitPortfolio() {
  const payload = buildPortfolioPayload()
  try {
    if (editingPortfolioId.value === null) {
      await createPortfolio(payload)
    } else {
      await updatePortfolio(editingPortfolioId.value, payload)
    }
  } catch (e) {
    alert(`保存失败：${errMsg(e)}`)
    return  // 弹窗保持打开，供用户修正
  }
  showPortfolioForm.value = false
  loadPortfolios()
}

async function removePortfolio(id: number) {
  if (!confirm('确认删除该组合策略？子策略将一并删除。')) return
  try {
    await deletePortfolio(id)
  } catch (e) {
    alert(`删除失败：${errMsg(e)}`)
    return
  }
  expanded.value.delete(id)
  delete strategyCache.value[id]
  loadPortfolios()
}

// ===== 子策略 CRUD =====
function openCreateStrategy(pid: number) {
  strategyPortfolioId.value = pid
  editingStrategyId.value = null
  strategyForm.value = emptyStrategyForm()
  for (const k of STRATEGY_RATIO_FIELDS) {
    strategyForm.value[k] = toPercent(strategyForm.value[k], true)
  }
  showStrategyForm.value = true
}

function openEditStrategy(pid: number, s: StrategyDetail) {
  strategyPortfolioId.value = pid
  editingStrategyId.value = s.id
  strategyForm.value = {
    name: s.name, formula_id: s.formula_id, period: s.period, role: s.role,
    master_strategy_id: s.master_strategy_id,
    stock_pool_id: s.stock_pool_id ?? null,
    slave_exit_mode: s.slave_exit_mode ?? 'independent',
    capital_ratio: s.capital_ratio,
    max_positions: s.max_positions, single_open_ratio: s.single_open_ratio,
    stop_loss_ratio: s.stop_loss_ratio, take_profit_ratio: s.take_profit_ratio,
    trailing_stop_ratio: s.trailing_stop_ratio,
    add_position_threshold: s.add_position_threshold, max_add_count: s.max_add_count,
    add_position_ratio: s.add_position_ratio, reduce_position_ratio: s.reduce_position_ratio,
    rotation_enabled: s.rotation_enabled, rotation_momentum_bars: s.rotation_momentum_bars,
    rotation_hysteresis: s.rotation_hysteresis, regime_filter_enabled: s.regime_filter_enabled,
    trend_trailing_stop_ratio: s.trend_trailing_stop_ratio, trend_max_positions: s.trend_max_positions,
    exposure_cap_enabled: s.exposure_cap_enabled,
  }
  for (const k of STRATEGY_RATIO_FIELDS) {
    strategyForm.value[k] = toPercent(strategyForm.value[k], true)
  }
  showStrategyForm.value = true
}

async function submitStrategy() {
  const pid = strategyPortfolioId.value!
  const payload = buildStrategyPayload()
  try {
    if (editingStrategyId.value === null) {
      await createStrategy(pid, payload)
    } else {
      await updateStrategy(pid, editingStrategyId.value, payload)
    }
  } catch (e) {
    alert(`保存失败：${errMsg(e)}`)
    return  // 弹窗保持打开，供用户修正
  }
  showStrategyForm.value = false
  try {
    strategyCache.value[pid] = await getStrategies(pid)
  } catch (e) {
    // 刷新失败不阻塞主操作（保存已成功），清缓存让下次展开重新拉
    delete strategyCache.value[pid]
  }
  loadPortfolios()  // 刷新列表徽章（子策略计数来自组合列表数据）
}

async function removeStrategy(pid: number, s: StrategyDetail) {
  if (!confirm(`确认删除子策略「${s.name}」？`)) return
  try {
    await deleteStrategy(pid, s.id)
  } catch (e) {
    // 业务错误（如被回测交易引用）经拦截器 reject 或 HTTP 错误 → 统一 alert
    alert(`删除失败：${errMsg(e)}`)
    return
  }
  try {
    strategyCache.value[pid] = await getStrategies(pid)
  } catch (e) {
    delete strategyCache.value[pid]
  }
  loadPortfolios()  // 刷新列表徽章
}

// 表单字段是否显示（showIf 条件：字符串=按 role 匹配，对象=按布尔/等值匹配）
function fieldVisible(fld: { showIf?: string | { key: string; equals: unknown } }, form: Record<string, any>): boolean {
  if (!fld.showIf) return true
  if (typeof fld.showIf === 'string') return form.role === fld.showIf
  return form[fld.showIf.key] === fld.showIf.equals
}

// 下拉选项源
function optionsOf(src: string | undefined, pid?: number) {
  if (!src) return []
  if (src === 'pools') return stockPools.value.map(p => ({ value: p.id, label: `${p.name}（${p.code}）` }))
  if (src === 'strategyPools') return [
    { value: null, label: '继承组合级' },
    ...stockPools.value.map(p => ({ value: p.id, label: `${p.name}（${p.code}）` })),
  ]
  if (src === 'sessions') return TRADING_SESSIONS.map(t => ({ value: t.value, label: `${t.label}（${t.value}）` }))
  if (src === 'formulas') return formulas.value.map(f => ({ value: f.id, label: f.name }))
  if (src === 'periods') return PERIODS.map(p => ({ value: p, label: p }))
  if (src === 'roles') return ROLES.map(r => ({ value: r.value, label: r.label }))
  if (src === 'slaveExitModes') return SLAVE_EXIT_MODES.map(m => ({ value: m.value, label: m.label }))
  if (src === 'positionSizings') return POSITION_SIZINGS.map(s => ({ value: s.value, label: s.label }))
  if (src === 'masters') return (pid ? masterOptions(pid, editingStrategyId.value) : []).map(m => ({ value: m.id, label: m.name }))
  return []
}

onMounted(loadPortfolios)
</script>

<template>
  <div>
    <PageHeader title="组合策略">
      <button @click="openCreatePortfolio" class="btn btn-primary">+ 新建组合</button>
    </PageHeader>

    <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
    <div v-else class="card table-wrap">
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>股票池</th><th>子策略</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <template v-for="p in portfolios" :key="p.id">
            <tr>
              <td style="color:var(--ink-soft)">#{{ p.id }}</td>
              <td>
                <button class="btn btn-sm toggle-expand" @click="toggleExpand(p)">
                  {{ expanded.has(p.id) ? '▼' : '▶' }}
                </button>
                {{ p.name }}
              </td>
              <td>{{ poolName(p.stock_pool_id) }}</td>
              <td><span class="badge badge-blue">{{ p.strategies.length }} 个子策略</span></td>
              <td><span class="badge" :class="p.status === 'active' ? 'badge-green' : 'badge-gray'">{{ p.status === 'active' ? '运行中' : '已归档' }}</span></td>
              <td>
                <button @click="openEditPortfolio(p)" class="btn btn-sm btn-primary">编辑</button>
                <button @click="removePortfolio(p.id)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
              </td>
            </tr>
            <!-- 树状子策略子行 -->
            <tr v-for="s in (expanded.has(p.id) ? (strategyCache[p.id] || []) : [])" :key="`${p.id}-${s.id}`" class="strategy-sub-row">
              <td></td>
              <td colspan="5">
                <div class="sub-strategy-row">
                  <span class="sub-strategy-name">{{ ROLES.find(r => r.value === s.role)?.label || s.role }} · {{ s.name }}</span>
                  <span class="sub-strategy-meta">{{ formulaName(s.formula_id) }} · {{ s.period }} · 资金 {{ (s.capital_ratio * 100).toFixed(0) }}%</span>
                  <span class="sub-strategy-actions">
                    <button @click="openEditStrategy(p.id, s)" class="btn btn-sm btn-primary">编辑</button>
                    <button @click="removeStrategy(p.id, s)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
                  </span>
                </div>
              </td>
            </tr>
            <!-- 展开区域的[+新建子策略] -->
            <tr v-if="expanded.has(p.id)" class="strategy-add-row">
              <td></td>
              <td colspan="5">
                <button @click="openCreateStrategy(p.id)" class="btn btn-sm signal-add" style="margin:0">+ 新建子策略</button>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div v-if="portfolios.length === 0" class="empty-state"><p>暂无组合策略</p></div>
    </div>

    <!-- ============ 组合编辑 Modal ============ -->
    <div v-if="showPortfolioForm" class="modal-overlay modal-lg" @click.self="showPortfolioForm = false">
      <div class="modal-content">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <h3 style="margin:0">{{ editingPortfolioId === null ? '新建组合' : '编辑组合' }}</h3>
          <button class="btn btn-sm" @click="showGuide = true" data-field="guide-toggle">📖 配置指南</button>
        </div>

        <div class="modal-body">
          <div v-for="g in PORTFOLIO_GROUPS" :key="g.title">
            <label class="group-label">{{ g.title }}</label>
            <div class="field-grid">
              <div
                v-for="fld in g.fields" :key="fld.key"
                v-show="fieldVisible(fld, portfolioForm)"
                class="field-row" :class="fld.span === 2 ? 'field-span-2' : ''"
              >
                <span class="field-label">{{ fld.label }}</span>
                <label v-if="fld.type === 'switch'" class="switch-row">
                  <input type="checkbox" :data-field="fld.key" v-model="portfolioForm[fld.key]" />
                  <span v-if="fld.hint" class="field-hint">{{ fld.hint }}</span>
                </label>
                <input
                  v-else-if="fld.type === 'text' || fld.type === 'number' || fld.type === 'percent'"
                  :type="fld.type === 'percent' ? 'number' : fld.type"
                  :data-field="fld.key"
                  v-model="portfolioForm[fld.key]"
                  :placeholder="fld.placeholder"
                />
                <select v-else :data-field="fld.key" v-model="portfolioForm[fld.key]">
                  <option v-for="o in optionsOf(fld.options)" :key="o.value ?? 'null'" :value="o.value">{{ o.label }}</option>
                </select>
                <span v-if="fld.type === 'percent'" class="field-suffix">%</span>
              </div>
            </div>
          </div>
        </div>

        <div class="modal-actions">
          <button @click="submitPortfolio" class="btn btn-primary">确认</button>
          <button @click="showPortfolioForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>

    <!-- ============ 配置指南 Modal（动态仓位完整设置说明）============ -->
    <div v-if="showGuide" class="modal-overlay modal-lg" @click.self="showGuide = false">
      <div class="modal-content">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <h3 style="margin:0">配置指南</h3>
          <button class="btn btn-sm" @click="showGuide = false">关闭</button>
        </div>
        <div class="modal-body" style="margin-top:12px">
          <div class="form-group">
            <label class="group-label">一、动态仓位模式（按步骤设置）</label>
            <div class="guide-block">
              <p class="guide-step"><b>① 仓位模型 → 「动态净资产」</b>：资金随净值滚动放大，盈利复投。</p>
              <p class="guide-step"><b>② 容量上限 equity_cap（关键）</b>：防止复利后单仓超限。
                单仓规模 = 单仓比例 × 资金占比 × 容量上限。若期望单仓 ≤ 100 万、单仓比例 15%、资金占比 0.9，
                则 <code>容量上限 = 100万 ÷ (0.15×0.9) ≈ 740万</code>。净值超上限后仓位封顶、超额现金闲置。</p>
              <p class="guide-step"><b>③ 成交量约束 → 开启，占比 5%~10%</b>：单笔不超当日成交量的该比例，
                防止大单"吃满流动性"、回测价失真。</p>
              <p class="guide-step"><b>④ 自适应滑点 → 开启，冲击系数 10%</b>：滑点随订单量占成交量比例上浮，
                模拟真实市场冲击成本。</p>
              <p class="guide-step"><b>⑤ 发起回测 → 高级选项勾选「费用预留」</b>：买入审批含佣金，防现金为负。
                也可临时开关成交量/容量/自适应滑点做 A/B 对比（不改组合配置）。</p>
              <p class="guide-note">顺序建议：先设容量上限 + 成交量约束跑基准，再叠加自适应滑点/费用预留，
                逐层观察收益衰减是否合理。</p>
            </div>
          </div>

          <div class="form-group">
            <label class="group-label">二、各设置说明（按需查阅）</label>
            <div class="guide-block">
              <table>
                <thead><tr><th>设置</th><th>位置</th><th>默认</th><th>说明</th></tr></thead>
                <tbody>
                  <tr><td>仓位模型</td><td>仓位与现金管理</td><td>动态</td>
                      <td>动态=按当前净值分配资金（复利）；固定=按初始资金（保守）。</td></tr>
                  <tr><td>容量上限</td><td>仓位与现金管理</td><td>不设上限</td>
                      <td>动态仓位的资金封顶；净值超上限后仓位不再放大（模拟超容量撤回/封盘）。</td></tr>
                  <tr><td>涨跌停约束</td><td>执行真实感</td><td>开</td>
                      <td>涨停买不进/跌停卖不出，延后重试。</td></tr>
                  <tr><td>成交量约束</td><td>执行真实感</td><td>关</td>
                      <td>单笔成交 ≤ 当日成交量 × 占比，超限缩量或延后。</td></tr>
                  <tr><td>自适应滑点</td><td>执行真实感</td><td>关</td>
                      <td>有效滑点 = 基础滑点 + 冲击系数×订单量/(当日量×100)，封顶 10%。</td></tr>
                  <tr><td>费用预留</td><td>发起回测→高级选项</td><td>关</td>
                      <td>买入审批含佣金，避免现金为负（run 级，单次回测生效）。</td></tr>
                  <tr><td>IS/OOS 门槛</td><td>发起回测→高级选项</td><td>关</td>
                      <td>回测完成时评估 OOS CAGR ≥ 比例×IS CAGR，防样本内过拟合。</td></tr>
                  <tr><td>评估口径</td><td>发起回测→高级选项</td><td>绝对</td>
                      <td>绝对=年化/回撤/卡玛；相对=信息比率/超额/下行捕获。</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="form-group">
            <label class="group-label">三、验证与风控提示</label>
            <div class="guide-block">
              <p class="guide-note">· 回测完成后详情页自动给出「🧪 验证结论」：容量健康、危机防御、样本外引用——若提示
                "含不可实现仓位"，请设容量上限或开启成交量约束后重跑。</p>
              <p class="guide-note">· 最大回撤受 3-5% 止损 + 移动止损 + 熔断控制，但止损按"收盘触发、次日开盘成交"，
                跳空低开时实际损失可能超过止损线。</p>
              <p class="guide-note">· 动态仓位下建议先用「回归验证」（walk-forward）做样本外检验，再决定是否实盘。</p>
            </div>
          </div>
        </div>
        <div class="modal-actions">
          <button @click="showGuide = false" class="btn btn-primary">知道了</button>
        </div>
      </div>
    </div>

    <!-- ============ 子策略编辑 Modal ============ -->
    <div v-if="showStrategyForm" class="modal-overlay modal-lg" @click.self="showStrategyForm = false">
      <div class="modal-content">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <h3 style="margin:0">{{ editingStrategyId === null ? '新建子策略' : '编辑子策略' }}</h3>
          <button class="btn btn-sm" @click="showStrategyGuide = true" data-field="strategy-guide-toggle">📖 配置指南</button>
        </div>

        <div class="modal-body">
          <div v-for="g in STRATEGY_GROUPS" :key="g.title">
            <label class="group-label">{{ g.title }}</label>
            <div class="field-grid">
              <div
                v-for="fld in g.fields" :key="fld.key"
                v-show="fieldVisible(fld, strategyForm)"
                class="field-row" :class="fld.span === 2 ? 'field-span-2' : ''"
              >
                <span class="field-label">{{ fld.label }}</span>
                <label v-if="fld.type === 'switch'" class="switch-row">
                  <input type="checkbox" :data-field="fld.key" v-model="strategyForm[fld.key]" />
                  <span v-if="fld.hint" class="field-hint">{{ fld.hint }}</span>
                </label>
                <input
                  v-else-if="fld.type === 'text' || fld.type === 'number' || fld.type === 'percent'"
                  :type="fld.type === 'percent' ? 'number' : fld.type"
                  :data-field="fld.key"
                  v-model="strategyForm[fld.key]"
                  :placeholder="fld.placeholder"
                />
                <select v-else :data-field="fld.key" v-model="strategyForm[fld.key]">
                  <option v-for="o in optionsOf(fld.options, strategyPortfolioId!)" :key="o.value ?? 'null'" :value="o.value">{{ o.label }}</option>
                </select>
                <span v-if="fld.type === 'percent'" class="field-suffix">%</span>
              </div>
            </div>
          </div>
        </div>

        <div class="modal-actions">
          <button @click="submitStrategy" class="btn btn-primary">确认</button>
          <button @click="showStrategyForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>

    <!-- ============ 子策略配置指南 Modal ============ -->
    <div v-if="showStrategyGuide" class="modal-overlay modal-lg" @click.self="showStrategyGuide = false">
      <div class="modal-content">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <h3 style="margin:0">子策略配置指南</h3>
          <button class="btn btn-sm" @click="showStrategyGuide = false">关闭</button>
        </div>
        <div class="modal-body" style="margin-top:12px">
          <div class="form-group">
            <label class="group-label">一、仓位与下单参数</label>
            <div class="guide-block">
              <p class="guide-step"><b>资金占比 capital_ratio</b>：本策略可动用的组合资金比例（持仓上限，非预分），
                多策略之和可超 100%。</p>
              <p class="guide-step"><b>最大持仓 max_positions + 单仓比例 single_open_ratio</b>：决定每次开仓量 =
                单仓比例 × 资金占比 × 仓位基座。如 6 仓 × 15% × 0.9 = 满仓 81%。</p>
              <p class="guide-step"><b>止损/止盈/移动止损</b>：止损按成本、止盈按成本、移动止损按持仓期间最高价
                （收盘触发、次日开盘成交，跳空低开实际损失可能超线）。</p>
              <p class="guide-step"><b>加仓 add_position_threshold / max_add_count / add_position_ratio</b>：
                threshold=-1=任何价格都加；max_add_count=加仓次数上限（0=禁加仓）。</p>
            </div>
          </div>
          <div class="form-group">
            <label class="group-label">二、可选增强（默认关，开启才生效）</label>
            <div class="guide-block">
              <p class="guide-step"><b>OPEN 择优轮动（B4）</b>：满槽时新信号动量 ≥ 最弱持仓 + 滞后阈值 → 轮出最弱换入。
                轮动动量窗口=看多少根 bar 的涨幅。</p>
              <p class="guide-step"><b>市场状态闸（B5）</b>：基准周线 bull 态时用 trend_* 覆盖移动止损/最大持仓，
                其余状态回落原参数。</p>
              <p class="guide-step"><b>敞口上限</b>：仅回测——BUY 审批卡策略总持仓市值 ≤ 资金占比 × 组合初始资金。</p>
            </div>
          </div>
          <div class="form-group">
            <label class="group-label">三、主从策略</label>
            <div class="guide-block">
              <p class="guide-step"><b>角色 master / slave</b>：slave 只能买入 master 当前持有的同一只股票；
                master 清仓后 slave 不可新开仓但存量可自行卖出。</p>
              <p class="guide-step"><b>卖出模式 slave_exit_mode</b>：follow_master=master 清仓后 slave 同步全平；
                independent=按自身信号。</p>
            </div>
          </div>
        </div>
        <div class="modal-actions">
          <button @click="showStrategyGuide = false" class="btn btn-primary">知道了</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
</style>
