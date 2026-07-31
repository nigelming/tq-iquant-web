<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas, type StrategyItem, type PortfolioRequest,
} from '../api'

const portfolios = ref<any[]>([])
const stockPools = ref<any[]>([])
const formulas = ref<any[]>([])
const showForm = ref(false)
const editingId = ref<number | null>(null)

const PERIODS = ['1m', '5m', '30m', '60m', '1d', '1w']
const ROLES = [
  { value: 'independent', label: '对立' },
  { value: 'master', label: '主策略' },
  { value: 'slave', label: '从策略' },
]
const TRADING_SESSIONS = [
  { value: 'full', label: '全天' },
  { value: 'am', label: '仅上午' },
  { value: 'pm', label: '仅下午' },
]

const emptyStrategy = (): StrategyItem => ({
  name: '', formula_id: 0, period: '1d', role: 'independent',
  master_strategy_id: null, capital_ratio: 0.6, max_positions: 5,
  stop_loss_ratio: 0.05, take_profit_ratio: 0.15, trailing_stop_ratio: 0.03,
})

const emptyForm = (): PortfolioRequest => ({
  name: '', stock_pool_id: 0, benchmark_index: '000300.SH',
  initial_capital: 500000, max_drawdown: 0.2, daily_loss_limit: 0.05,
  max_holdings: 10, trading_session: 'full', status: 'active',
  strategies: [emptyStrategy()],
})
const form = ref<PortfolioRequest>(emptyForm())

async function load() {
  const [ps, pools, fs] = await Promise.all([
    getPortfolios(),
    getStockPools().catch(() => []),
    getFormulas().catch(() => []),
  ])
  portfolios.value = ps as any[]
  stockPools.value = pools as any[]
  formulas.value = fs as any[]
}

function poolName(id: number) {
  return stockPools.value.find(p => p.id === id)?.name || `#${id}`
}

function openCreate() {
  editingId.value = null
  form.value = emptyForm()
  showForm.value = true
}

async function openEdit(p: any) {
  editingId.value = p.id
  const detail = await getPortfolioDetail(p.id)
  form.value = {
    name: detail.name, stock_pool_id: detail.stock_pool_id,
    benchmark_index: detail.benchmark_index || '000300.SH',
    initial_capital: detail.initial_capital, max_drawdown: detail.max_drawdown,
    daily_loss_limit: detail.daily_loss_limit, max_holdings: detail.max_holdings,
    trading_session: detail.trading_session, status: detail.status,
    strategies: detail.strategies.length
      ? detail.strategies.map((s: any) => ({
          name: s.name, formula_id: s.formula_id, period: s.period, role: s.role,
          master_strategy_id: s.master_strategy_id, capital_ratio: s.capital_ratio,
          max_positions: s.max_positions, stop_loss_ratio: s.stop_loss_ratio,
          take_profit_ratio: s.take_profit_ratio, trailing_stop_ratio: s.trailing_stop_ratio,
        }))
      : [emptyStrategy()],
  }
  showForm.value = true
}

function addStrategy() {
  form.value.strategies.push(emptyStrategy())
}

function removeStrategy(idx: number) {
  form.value.strategies.splice(idx, 1)
}

function masterOptions() {
  // 返回本批 role=master 的子策略索引列表，供 slave 选择主策略
  return form.value.strategies
    .map((s, i) => ({ idx: i, name: s.name }))
    .filter(o => form.value.strategies[o.idx].role === 'master')
}

async function submit() {
  if (editingId.value === null) {
    await createPortfolio(form.value)
  } else {
    await updatePortfolio(editingId.value, form.value)
  }
  showForm.value = false
  load()
}

async function remove(id: number) {
  if (!confirm('确认删除该组合策略？子策略将一并删除。')) return
  await deletePortfolio(id)
  load()
}

onMounted(load)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="openCreate" class="btn btn-primary">+ 新建组合</button>
  </div>

  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>股票池</th><th>子策略</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="p in portfolios" :key="p.id">
          <td style="color:#888">#{{ p.id }}</td>
          <td>{{ p.name }}</td>
          <td>{{ poolName(p.stock_pool_id) }}</td>
          <td><span class="badge badge-blue">{{ p.strategies.length }} 个子策略</span></td>
          <td><span class="badge" :class="p.status === 'active' ? 'badge-green' : 'badge-gray'">{{ p.status === 'active' ? '运行中' : '已归档' }}</span></td>
          <td>
            <button @click="openEdit(p)" class="btn btn-sm btn-primary">编辑</button>
            <button @click="remove(p.id)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="portfolios.length === 0" class="empty-state"><p>暂无组合策略</p></div>
  </div>

  <div v-if="showForm" class="modal-overlay modal-lg" @click.self="showForm = false">
    <div class="modal-content">
      <h3>{{ editingId === null ? '新建组合' : '编辑组合' }}</h3>

      <label>名称</label>
      <input v-model="form.name" placeholder="例如：稳健组合（组合策略名称）" />

      <label>股票池</label>
      <select v-model="form.stock_pool_id">
        <option :value="0" disabled>请选择股票池</option>
        <option v-for="p in stockPools" :key="p.id" :value="p.id">{{ p.name }}（{{ p.code }}）</option>
      </select>

      <label>基准指数</label>
      <input v-model="form.benchmark_index" placeholder="000300.SH" />

      <label>初始资金（元）</label>
      <input v-model.number="form.initial_capital" type="number" />

      <label>风控参数</label>
      <div class="signal-row">
        <input v-model.number="form.max_drawdown" type="number" step="0.01" placeholder="最大回撤" />
        <input v-model.number="form.daily_loss_limit" type="number" step="0.01" placeholder="日亏损限" />
        <input v-model.number="form.max_holdings" type="number" placeholder="最大持仓数" />
      </div>

      <label>交易时段</label>
      <select v-model="form.trading_session">
        <option v-for="t in TRADING_SESSIONS" :key="t.value" :value="t.value">{{ t.label }}（{{ t.value }}）</option>
      </select>

      <label>子策略配置</label>
      <div v-for="(s, idx) in form.strategies" :key="idx" class="signal-row">
        <input v-model="s.name" placeholder="子策略名称" />
        <select v-model="s.formula_id">
          <option :value="0" disabled>选公式</option>
          <option v-for="f in formulas" :key="f.id" :value="f.id">{{ f.name }}</option>
        </select>
        <select v-model="s.period">
          <option v-for="p in PERIODS" :key="p" :value="p">{{ p }}</option>
        </select>
        <select v-model="s.role">
          <option v-for="r in ROLES" :key="r.value" :value="r.value">{{ r.label }}</option>
        </select>
        <select v-if="s.role === 'slave'" v-model="s.master_strategy_id">
          <option :value="null" disabled>选主策略</option>
          <option v-for="o in masterOptions()" :key="o.idx" :value="o.idx">{{ o.name }}（第{{ o.idx + 1 }}行）</option>
        </select>
        <button @click="removeStrategy(idx)" class="btn btn-sm btn-danger">×</button>
      </div>
      <button @click="addStrategy" class="btn btn-sm signal-add">+ 添加子策略</button>

      <div class="modal-actions">
        <button @click="submit" class="btn btn-primary">确认</button>
        <button @click="showForm = false" class="btn">取消</button>
      </div>
    </div>
  </div>
</template>
