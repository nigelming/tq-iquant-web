<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas,
  getStrategies, createStrategy, updateStrategy, deleteStrategy,
  type PortfolioRequest, type StrategyRequest, type StrategyDetail,
  type PortfolioItem, type StockPoolItem, type FormulaItem,
} from '../api'

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

// 百分比字段集合（后台存小数，前台输入/显示百分比，提交时 ÷100 转回小数）
const PORTFOLIO_RATIO_FIELDS = new Set([
  'max_drawdown', 'daily_loss_limit',
  'buy_commission_rate', 'sell_commission_rate', 'stamp_duty_rate', 'slippage',
])
const STRATEGY_RATIO_FIELDS = new Set([
  'capital_ratio', 'single_open_ratio',
  'stop_loss_ratio', 'take_profit_ratio', 'trailing_stop_ratio',
  'add_position_threshold', 'add_position_ratio', 'reduce_position_ratio',
])

// 表单字段元数据：key/名称/一行几个（≤2 保证不溢出）。分组用于弹窗分区。
const PORTFOLIO_GROUPS = [
  { title: '基本信息', fields: [
    { key: 'name', label: '名称', span: 2, type: 'text', placeholder: '例如：稳健组合' },
    { key: 'stock_pool_id', label: '股票池', span: 2, type: 'select', options: 'pools' },
    { key: 'benchmark_index', label: '基准指数', span: 2, type: 'text', placeholder: '000300.SH' },
    { key: 'initial_capital', label: '初始资金（元）', span: 2, type: 'number' },
  ]},
  { title: '风控参数', fields: [
    { key: 'max_drawdown', label: '最大回撤', span: 2, type: 'percent', placeholder: '20' },
    { key: 'daily_loss_limit', label: '日亏损限', span: 2, type: 'percent', placeholder: '5' },
    { key: 'max_holdings', label: '最大持仓数', span: 2, type: 'number', placeholder: '10' },
  ]},
  { title: '交易成本', fields: [
    { key: 'min_commission', label: '最低佣金（元）', span: 2, type: 'number', placeholder: '5' },
    { key: 'buy_commission_rate', label: '买佣金率', span: 2, type: 'percent', placeholder: '0.025' },
    { key: 'sell_commission_rate', label: '卖佣金率', span: 2, type: 'percent', placeholder: '0.025' },
    { key: 'stamp_duty_rate', label: '印花税率', span: 2, type: 'percent', placeholder: '0.05' },
    { key: 'slippage', label: '滑点', span: 2, type: 'percent', placeholder: '0' },
  ]},
  { title: '交易时段', fields: [
    { key: 'trading_session', label: '交易时段', span: 2, type: 'select', options: 'sessions' },
  ]},
]

const STRATEGY_GROUPS = [
  { title: '基本信息', fields: [
    { key: 'name', label: '名称', span: 2, type: 'text', placeholder: '子策略名称' },
    { key: 'formula_id', label: '公式', span: 2, type: 'select', options: 'formulas' },
    { key: 'period', label: '周期', span: 2, type: 'select', options: 'periods' },
    { key: 'role', label: '角色', span: 2, type: 'select', options: 'roles' },
    { key: 'master_strategy_id', label: '主策略', span: 2, type: 'select', options: 'masters', showIf: 'slave' },
  ]},
  { title: '资金参数', fields: [
    { key: 'capital_ratio', label: '资金占比', span: 2, type: 'percent', placeholder: '60' },
    { key: 'max_positions', label: '最大持仓数', span: 2, type: 'number', placeholder: '5' },
    { key: 'single_open_ratio', label: '单仓占比', span: 2, type: 'percent', placeholder: '10' },
  ]},
  { title: '风控参数', fields: [
    { key: 'stop_loss_ratio', label: '止损', span: 2, type: 'percent', placeholder: '5' },
    { key: 'take_profit_ratio', label: '止盈', span: 2, type: 'percent', placeholder: '15' },
    { key: 'trailing_stop_ratio', label: '移动止损', span: 2, type: 'percent', placeholder: '3' },
  ]},
  { title: '加仓参数', fields: [
    { key: 'add_position_threshold', label: '加仓阈值', span: 2, type: 'percent', placeholder: '5（-1=任何价）' },
    { key: 'max_add_count', label: '加仓次数', span: 2, type: 'number', placeholder: '2' },
    { key: 'add_position_ratio', label: '加仓比例', span: 2, type: 'percent', placeholder: '10' },
    { key: 'reduce_position_ratio', label: '减仓比例', span: 2, type: 'percent', placeholder: '30' },
  ]},
]

function emptyStrategyForm(): Record<string, any> {
  return {
    name: '', formula_id: 0, period: '1d', role: 'independent',
    master_strategy_id: null, capital_ratio: 0.6, max_positions: 5,
    single_open_ratio: 0.1,
    stop_loss_ratio: 0.05, take_profit_ratio: 0.15, trailing_stop_ratio: 0.03,
    add_position_threshold: -1, max_add_count: 2,  // -1=任何价都加（页面默认即跳过回撤检查）
    add_position_ratio: 0.1, reduce_position_ratio: 0.3,
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

// 同组合下 role=master 的子策略（供 slave 选主策略）
function masterOptions(pid: number) {
  return (strategyCache.value[pid] || []).filter(s => s.role === 'master')
}

// 提交组合表单：比例字段从百分比转回小数
function buildPortfolioPayload(): PortfolioRequest {
  const f = portfolioForm.value
  const out: Record<string, unknown> = { strategies: [] }
  for (const g of PORTFOLIO_GROUPS) for (const fld of g.fields) {
    out[fld.key] = fromPercent(f[fld.key], fld)
  }
  out.status = f.status
  return out as unknown as PortfolioRequest
}

// 提交子策略表单：比例字段从百分比转回小数
function buildStrategyPayload(): StrategyRequest {
  const f = strategyForm.value
  const out: Record<string, unknown> = {}
  for (const g of STRATEGY_GROUPS) for (const fld of g.fields) {
    if (fld.showIf && f.role !== fld.showIf) continue
    out[fld.key] = fromPercent(f[fld.key], fld)
  }
  // showIf 跳过 master_strategy_id 时，独立/主策略置 null
  if (f.role !== 'slave') out.master_strategy_id = null
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
    master_strategy_id: s.master_strategy_id, capital_ratio: s.capital_ratio,
    max_positions: s.max_positions, single_open_ratio: s.single_open_ratio,
    stop_loss_ratio: s.stop_loss_ratio, take_profit_ratio: s.take_profit_ratio,
    trailing_stop_ratio: s.trailing_stop_ratio,
    add_position_threshold: s.add_position_threshold, max_add_count: s.max_add_count,
    add_position_ratio: s.add_position_ratio, reduce_position_ratio: s.reduce_position_ratio,
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
}

// 表单字段是否显示（showIf 条件）
function fieldVisible(fld: { showIf?: string }, form: Record<string, any>): boolean {
  if (!fld.showIf) return true
  return form.role === fld.showIf
}

// 下拉选项源
function optionsOf(src: string | undefined, pid?: number) {
  if (!src) return []
  if (src === 'pools') return stockPools.value.map(p => ({ value: p.id, label: `${p.name}（${p.code}）` }))
  if (src === 'sessions') return TRADING_SESSIONS.map(t => ({ value: t.value, label: `${t.label}（${t.value}）` }))
  if (src === 'formulas') return formulas.value.map(f => ({ value: f.id, label: f.name }))
  if (src === 'periods') return PERIODS.map(p => ({ value: p, label: p }))
  if (src === 'roles') return ROLES.map(r => ({ value: r.value, label: r.label }))
  if (src === 'masters') return (pid ? masterOptions(pid) : []).map(m => ({ value: m.id, label: m.name }))
  return []
}

onMounted(loadPortfolios)
</script>

<template>
  <div>
    <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
      <button @click="openCreatePortfolio" class="btn btn-primary">+ 新建组合</button>
    </div>

    <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
    <div v-else class="card table-wrap">
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>股票池</th><th>子策略</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <template v-for="p in portfolios" :key="p.id">
            <tr>
              <td style="color:#888">#{{ p.id }}</td>
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
        <h3>{{ editingPortfolioId === null ? '新建组合' : '编辑组合' }}</h3>

        <div v-for="g in PORTFOLIO_GROUPS" :key="g.title">
          <label class="group-label">{{ g.title }}</label>
          <div v-for="fld in g.fields" :key="fld.key" class="field-row">
            <span class="field-label">{{ fld.label }}</span>
            <input
              v-if="fld.type === 'text' || fld.type === 'number' || fld.type === 'percent'"
              :type="fld.type === 'percent' ? 'number' : fld.type"
              :data-field="fld.key"
              v-model="portfolioForm[fld.key]"
              :placeholder="fld.placeholder"
            />
            <select v-else :data-field="fld.key" v-model="portfolioForm[fld.key]">
              <option v-for="o in optionsOf(fld.options)" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <span v-if="fld.type === 'percent'" class="field-suffix">%</span>
          </div>
        </div>

        <div class="modal-actions">
          <button @click="submitPortfolio" class="btn btn-primary">确认</button>
          <button @click="showPortfolioForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>

    <!-- ============ 子策略编辑 Modal ============ -->
    <div v-if="showStrategyForm" class="modal-overlay modal-lg" @click.self="showStrategyForm = false">
      <div class="modal-content">
        <h3>{{ editingStrategyId === null ? '新建子策略' : '编辑子策略' }}</h3>

        <div v-for="g in STRATEGY_GROUPS" :key="g.title">
          <label class="group-label">{{ g.title }}</label>
          <div v-for="fld in g.fields" :key="fld.key" v-show="fieldVisible(fld, strategyForm)" class="field-row">
            <span class="field-label">{{ fld.label }}</span>
            <input
              v-if="fld.type === 'text' || fld.type === 'number' || fld.type === 'percent'"
              :type="fld.type === 'percent' ? 'number' : fld.type"
              :data-field="fld.key"
              v-model="strategyForm[fld.key]"
              :placeholder="fld.placeholder"
            />
            <select v-else :data-field="fld.key" v-model="strategyForm[fld.key]">
              <option v-for="o in optionsOf(fld.options, strategyPortfolioId!)" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <span v-if="fld.type === 'percent'" class="field-suffix">%</span>
          </div>
        </div>

        <div class="modal-actions">
          <button @click="submitStrategy" class="btn btn-primary">确认</button>
          <button @click="showStrategyForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>
