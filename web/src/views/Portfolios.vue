<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas,
  getStrategies, createStrategy, updateStrategy, deleteStrategy,
  type PortfolioRequest, type StrategyRequest, type StrategyDetail,
} from '../api'

// ===== 数据 =====
const portfolios = ref<any[]>([])
const stockPools = ref<any[]>([])
const formulas = ref<any[]>([])

// ===== 第一层：组合列表 =====
const showPortfolioForm = ref(false)
const editingPortfolioId = ref<number | null>(null)
const portfolioForm = ref<PortfolioRequest>(emptyPortfolioForm())

// ===== 第二层：子策略列表 =====
const currentPortfolio = ref<any | null>(null)  // null=在第一层
const strategies = ref<StrategyDetail[]>([])
const showStrategyForm = ref(false)
const editingStrategyId = ref<number | null>(null)
const strategyForm = ref<StrategyRequest>(emptyStrategyForm())

// ===== 枚举 =====
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

function emptyStrategyForm(): StrategyRequest {
  return {
    name: '', formula_id: 0, period: '1d', role: 'independent',
    master_strategy_id: null, capital_ratio: 0.6, max_positions: 5,
    single_open_ratio: 0.1,
    stop_loss_ratio: 0.05, take_profit_ratio: 0.15, trailing_stop_ratio: 0.03,
    add_position_threshold: 0.05, max_add_count: 2,
    add_position_ratio: 0.1, reduce_position_ratio: 0.3,
  }
}

function emptyPortfolioForm(): PortfolioRequest {
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

// ===== 加载 =====
async function loadPortfolios() {
  const [ps, pools, fs] = await Promise.all([
    getPortfolios(),
    getStockPools().catch(() => []),
    getFormulas().catch(() => []),
  ])
  portfolios.value = ps as any[]
  stockPools.value = pools as any[]
  formulas.value = fs as any[]
}

async function loadStrategies(pid: number) {
  strategies.value = await getStrategies(pid)
}

function poolName(id: number) {
  return stockPools.value.find(p => p.id === id)?.name || `#${id}`
}

function formulaName(id: number) {
  return formulas.value.find(f => f.id === id)?.name || `#${id}`
}

// 同组合下 role=master 的子策略（供 slave 选主策略）
const masterOptions = computed(() =>
  strategies.value.filter(s => s.role === 'master')
)

// ===== 第一层：组合 CRUD =====
function openCreatePortfolio() {
  editingPortfolioId.value = null
  portfolioForm.value = emptyPortfolioForm()
  showPortfolioForm.value = true
}

async function openEditPortfolio(p: any) {
  editingPortfolioId.value = p.id
  const detail = await getPortfolioDetail(p.id)
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
  showPortfolioForm.value = true
}

async function submitPortfolio() {
  if (editingPortfolioId.value === null) {
    await createPortfolio(portfolioForm.value)
  } else {
    await updatePortfolio(editingPortfolioId.value, portfolioForm.value)
  }
  showPortfolioForm.value = false
  loadPortfolios()
}

async function removePortfolio(id: number) {
  if (!confirm('确认删除该组合策略？子策略将一并删除。')) return
  await deletePortfolio(id)
  loadPortfolios()
}

// ===== 切换到第二层 =====
async function openStrategies(p: any) {
  currentPortfolio.value = p
  await loadStrategies(p.id)
}

function backToPortfolios() {
  currentPortfolio.value = null
  strategies.value = []
}

// ===== 第二层：子策略 CRUD =====
function openCreateStrategy() {
  editingStrategyId.value = null
  strategyForm.value = emptyStrategyForm()
  showStrategyForm.value = true
}

function openEditStrategy(s: StrategyDetail) {
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
  showStrategyForm.value = true
}

async function submitStrategy() {
  const pid = currentPortfolio.value.id
  if (editingStrategyId.value === null) {
    await createStrategy(pid, strategyForm.value)
  } else {
    await updateStrategy(pid, editingStrategyId.value, strategyForm.value)
  }
  showStrategyForm.value = false
  loadStrategies(pid)
}

async function removeStrategy(s: StrategyDetail) {
  if (!confirm(`确认删除子策略「${s.name}」？`)) return
  await deleteStrategy(currentPortfolio.value.id, s.id)
  loadStrategies(currentPortfolio.value.id)
}

onMounted(loadPortfolios)
</script>

<template>
  <!-- ============ 第一层：组合列表 ============ -->
  <div v-if="!currentPortfolio">
    <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
      <button @click="openCreatePortfolio" class="btn btn-primary">+ 新建组合</button>
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
              <button @click="openEditPortfolio(p)" class="btn btn-sm btn-primary">编辑</button>
              <button @click="openStrategies(p)" class="btn btn-sm" style="margin-left:6px">子策略</button>
              <button @click="removePortfolio(p.id)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="portfolios.length === 0" class="empty-state"><p>暂无组合策略</p></div>
    </div>

    <!-- 组合编辑 Modal（两层设计：不含子策略配置） -->
    <div v-if="showPortfolioForm" class="modal-overlay modal-lg" @click.self="showPortfolioForm = false">
      <div class="modal-content">
        <h3>{{ editingPortfolioId === null ? '新建组合' : '编辑组合' }}</h3>

        <label>名称</label>
        <input v-model="portfolioForm.name" placeholder="例如：稳健组合（组合策略名称）" />

        <label>股票池</label>
        <select v-model="portfolioForm.stock_pool_id">
          <option :value="0" disabled>请选择股票池</option>
          <option v-for="p in stockPools" :key="p.id" :value="p.id">{{ p.name }}（{{ p.code }}）</option>
        </select>

        <label>基准指数</label>
        <input v-model="portfolioForm.benchmark_index" placeholder="000300.SH" />

        <label>初始资金（元）</label>
        <input v-model.number="portfolioForm.initial_capital" type="number" />

        <label>风控参数</label>
        <div class="signal-row">
          <input v-model.number="portfolioForm.max_drawdown" type="number" step="0.01" placeholder="最大回撤" />
          <input v-model.number="portfolioForm.daily_loss_limit" type="number" step="0.01" placeholder="日亏损限" />
          <input v-model.number="portfolioForm.max_holdings" type="number" placeholder="最大持仓数" />
        </div>

        <label>交易成本</label>
        <div class="signal-row">
          <input v-model.number="portfolioForm.min_commission" type="number" step="0.01" placeholder="最低佣金" />
          <input v-model.number="portfolioForm.buy_commission_rate" type="number" step="0.0001" placeholder="买佣金" />
          <input v-model.number="portfolioForm.sell_commission_rate" type="number" step="0.0001" placeholder="卖佣金" />
        </div>
        <div class="signal-row">
          <input v-model.number="portfolioForm.stamp_duty_rate" type="number" step="0.0001" placeholder="印花税" />
          <input v-model.number="portfolioForm.slippage" type="number" step="0.0001" placeholder="滑点" />
        </div>

        <label>交易时段</label>
        <select v-model="portfolioForm.trading_session">
          <option v-for="t in TRADING_SESSIONS" :key="t.value" :value="t.value">{{ t.label }}（{{ t.value }}）</option>
        </select>

        <div class="modal-actions">
          <button @click="submitPortfolio" class="btn btn-primary">确认</button>
          <button @click="showPortfolioForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ 第二层：子策略列表 ============ -->
  <div v-else>
    <div style="margin-bottom:16px;display:flex;align-items:center;justify-content:space-between">
      <div style="display:flex;align-items:center;gap:12px">
        <button @click="backToPortfolios" class="btn btn-sm">← 返回</button>
        <h3 style="font-size:16px;font-weight:600;color:var(--text-heading);margin:0">
          {{ currentPortfolio.name }} — 子策略
        </h3>
      </div>
      <button @click="openCreateStrategy" class="btn btn-primary">+ 新建子策略</button>
    </div>

    <div class="card table-wrap">
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>公式</th><th>周期</th><th>角色</th><th>主策略</th><th>资金占比</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="s in strategies" :key="s.id">
            <td style="color:#888">#{{ s.id }}</td>
            <td>{{ s.name }}</td>
            <td>{{ formulaName(s.formula_id) }}</td>
            <td>{{ s.period }}</td>
            <td><span class="badge badge-blue">{{ ROLES.find(r => r.value === s.role)?.label || s.role }}</span></td>
            <td>{{ s.role === 'slave' ? (strategies.find(m => m.id === s.master_strategy_id)?.name || `#${s.master_strategy_id}`) : '—' }}</td>
            <td>{{ (s.capital_ratio * 100).toFixed(0) }}%</td>
            <td>
              <button @click="openEditStrategy(s)" class="btn btn-sm btn-primary">编辑</button>
              <button @click="removeStrategy(s)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="strategies.length === 0" class="empty-state"><p>暂无子策略，点右上[+新建子策略]添加</p></div>
    </div>

    <!-- 单个子策略编辑 Modal -->
    <div v-if="showStrategyForm" class="modal-overlay modal-lg" @click.self="showStrategyForm = false">
      <div class="modal-content">
        <h3>{{ editingStrategyId === null ? '新建子策略' : '编辑子策略' }}</h3>

        <label>名称</label>
        <input v-model="strategyForm.name" placeholder="子策略名称" />

        <div class="signal-row">
          <select v-model="strategyForm.formula_id">
            <option :value="0" disabled>选公式</option>
            <option v-for="f in formulas" :key="f.id" :value="f.id">{{ f.name }}</option>
          </select>
          <select v-model="strategyForm.period">
            <option v-for="p in PERIODS" :key="p" :value="p">{{ p }}</option>
          </select>
          <select v-model="strategyForm.role">
            <option v-for="r in ROLES" :key="r.value" :value="r.value">{{ r.label }}</option>
          </select>
        </div>

        <div v-if="strategyForm.role === 'slave'">
          <label>主策略</label>
          <select v-model="strategyForm.master_strategy_id">
            <option :value="null" disabled>选主策略</option>
            <option v-for="m in masterOptions" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>

        <label>资金</label>
        <div class="signal-row">
          <input v-model.number="strategyForm.capital_ratio" type="number" step="0.01" placeholder="资金占比" />
          <input v-model.number="strategyForm.max_positions" type="number" placeholder="最大持仓数" />
          <input v-model.number="strategyForm.single_open_ratio" type="number" step="0.01" placeholder="单仓占比" />
        </div>
        <label>风控</label>
        <div class="signal-row">
          <input v-model.number="strategyForm.stop_loss_ratio" type="number" step="0.01" placeholder="止损" />
          <input v-model.number="strategyForm.take_profit_ratio" type="number" step="0.01" placeholder="止盈" />
          <input v-model.number="strategyForm.trailing_stop_ratio" type="number" step="0.01" placeholder="移动止损" />
        </div>
        <label>加仓</label>
        <div class="signal-row">
          <input v-model.number="strategyForm.add_position_threshold" type="number" step="0.01" placeholder="加仓阈值" />
          <input v-model.number="strategyForm.max_add_count" type="number" placeholder="加仓次数" />
          <input v-model.number="strategyForm.add_position_ratio" type="number" step="0.01" placeholder="加仓比例" />
          <input v-model.number="strategyForm.reduce_position_ratio" type="number" step="0.01" placeholder="减仓比例" />
        </div>

        <div class="modal-actions">
          <button @click="submitStrategy" class="btn btn-primary">确认</button>
          <button @click="showStrategyForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>
