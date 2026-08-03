<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
} from '../api'

// ===== 列表 =====
const records = ref<any[]>([])
const portfolios = ref<any[]>([])

// ===== 发起弹窗 =====
const showForm = ref(false)
const submitting = ref(false)
const form = ref({ portfolio_strategy_id: 0, name: '', start_date: '', end_date: '' })

// ===== 详情视图 =====
const currentRecord = ref<any | null>(null)
const detail = ref<any | null>(null)

const STATUS_LABEL: Record<string, string> = {
  completed: '已完成', running: '运行中', failed: '失败', pending: '待运行',
}

function errMsg(e: any): string {
  const d = e?.response?.data
  if (d?.message) return d.message
  if (typeof d?.detail === 'string') return d.detail
  return e?.message || '请求失败'
}

function poolName(id: number) {
  return portfolios.value.find(p => p.id === id)?.name || `#${id}`
}

async function load() {
  const [recs, ps] = await Promise.all([
    getBacktestRecords(),
    getPortfolios().catch(() => []),
  ])
  records.value = recs as any[]
  portfolios.value = ps as any[]
}

function openForm() {
  form.value = {
    portfolio_strategy_id: portfolios.value[0]?.id || 0,
    name: '', start_date: '', end_date: '',
  }
  showForm.value = true
}

async function submit() {
  if (!form.value.portfolio_strategy_id) { alert('请选择组合'); return }
  if (!form.value.name.trim()) { alert('请填写回测名称'); return }
  if (!form.value.start_date || !form.value.end_date) { alert('请选择起止日期'); return }
  // 前端拦截日期区间错误：start 必须 ≤ end，避免发到后端才报错
  if (form.value.start_date > form.value.end_date) {
    alert(`开始日期不能晚于结束日期：${form.value.start_date} ~ ${form.value.end_date}`)
    return
  }
  submitting.value = true
  try {
    const res = await runBacktest(form.value)
    showForm.value = false
    await load()
    if (res?.record_id) await openDetail(res.record_id)
  } catch (e) {
    alert(`发起失败：${errMsg(e)}`)
  } finally {
    submitting.value = false
  }
}

async function openDetail(id: number) {
  detail.value = await getBacktestDetail(id)
  currentRecord.value = detail.value?.record || null
}

function backToList() {
  currentRecord.value = null
  detail.value = null
}

// ===== 评估指标格式 =====
function pct(v: any): string {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(2)}%`
}
function num(v: any, digits = 4): string {
  if (v === null || v === undefined) return '—'
  return Number(v).toFixed(digits)
}

const metrics = computed(() => {
  const e = detail.value?.evaluations
  if (!e) return []
  return [
    { label: '总收益', value: pct(e.total_return) },
    { label: '年化收益', value: pct(e.annual_return) },
    { label: '最大回撤', value: pct(e.max_drawdown) },
    { label: '波动率', value: pct(e.volatility) },
    { label: '夏普比率', value: num(e.sharpe_ratio, 2) },
    { label: '索提诺比率', value: num(e.sortino_ratio, 2) },
    { label: '卡尔玛比率', value: num(e.calmar_ratio, 2) },
    { label: '胜率', value: pct(e.win_rate) },
    { label: '盈亏比', value: num(e.profit_factor, 2) },
    { label: '交易次数', value: String(e.total_trades ?? '—') },
    { label: '基准收益', value: pct(e.benchmark_return) },
    { label: '平均持仓天数', value: num(e.avg_holding_days, 1) },
  ]
})

// ===== 净值曲线 SVG（单序列折线，2px，recessive 轴，crosshair tooltip）=====
const chart = computed(() => {
  const snaps = detail.value?.snapshots || []
  if (snaps.length < 2) return null
  const W = 760, H = 240, PAD_L = 56, PAD_R = 16, PAD_T = 16, PAD_B = 28
  const values = snaps.map((s: any) => Number(s.total_value))
  const dates = snaps.map((s: any) => s.snap_date)
  const minV = Math.min(...values), maxV = Math.max(...values)
  const range = maxV - minV || 1
  const x = (i: number) => PAD_L + (i / (values.length - 1)) * (W - PAD_L - PAD_R)
  const y = (v: number) => PAD_T + (1 - (v - minV) / range) * (H - PAD_T - PAD_B)
  const points = values.map((v: number, i: number) => `${x(i)},${y(v)}`).join(' ')
  const yTicks = [0, 0.33, 0.66, 1].map(f => {
    const v = minV + range * f
    return { v, y: y(v), label: Math.round(v).toLocaleString() }
  })
  const xTicks = [0, Math.floor((dates.length - 1) / 2), dates.length - 1].map(i => ({
    x: x(i), label: dates[i],
  }))
  return { W, H, PAD_L, PAD_R, PAD_T, PAD_B, points, x, y, values, dates, yTicks, xTicks, minV, maxV }
})

const hoverIdx = ref<number | null>(null)
function onChartMove(e: MouseEvent) {
  if (!chart.value) return
  const rect = (e.currentTarget as SVGElement).querySelector('rect.chart-hit')!.getBoundingClientRect()
  const rx = (e.clientX - rect.left) / rect.width * chart.value.W
  const { PAD_L, PAD_R, values } = chart.value
  const plotW = chart.value.W - PAD_L - PAD_R
  const ratio = Math.max(0, Math.min(1, (rx - PAD_L) / plotW))
  hoverIdx.value = Math.round(ratio * (values.length - 1))
}
function onChartLeave() { hoverIdx.value = null }

onMounted(load)
</script>

<template>
  <!-- ============ 列表视图 ============ -->
  <div v-if="!currentRecord">
    <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
      <button @click="openForm" class="btn btn-primary">+ 发起回测</button>
    </div>

    <div class="card table-wrap">
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>组合</th><th>起止</th><th>状态</th><th>进度</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in records" :key="r.id">
            <td style="color:#888">#{{ r.id }}</td>
            <td>{{ r.name }}</td>
            <td>{{ poolName(r.portfolio_strategy_id) }}</td>
            <td style="color:#888;font-size:12px">{{ r.start_date }} ~ {{ r.end_date }}</td>
            <td><span class="badge" :class="r.status === 'completed' ? 'badge-green' : r.status === 'running' ? 'badge-blue' : 'badge-gray'">{{ STATUS_LABEL[r.status] || r.status }}</span></td>
            <td><span v-if="r.progress != null">{{ r.progress }}%</span><span v-else style="color:#888">-</span></td>
            <td>
              <button @click="openDetail(r.id)" class="btn btn-sm btn-primary" :disabled="r.status !== 'completed'">查看</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="records.length === 0" class="empty-state"><p>暂无回测记录，点右上[+发起回测]</p></div>
    </div>

    <!-- 发起回测 Modal -->
    <div v-if="showForm" class="modal-overlay modal-lg" @click.self="showForm = false">
      <div class="modal-content">
        <h3>发起回测</h3>

        <label>组合</label>
        <select v-model="form.portfolio_strategy_id">
          <option :value="0" disabled>请选择组合</option>
          <option v-for="p in portfolios" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>

        <label>回测名称</label>
        <input v-model="form.name" placeholder="例如：稳健组合-7月回测" />

        <div class="signal-row">
          <div style="flex:1">
            <label>开始日期</label>
            <input v-model="form.start_date" type="date" />
          </div>
          <div style="flex:1">
            <label>结束日期</label>
            <input v-model="form.end_date" type="date" />
          </div>
        </div>

        <div class="modal-actions">
          <button @click="submit" class="btn btn-primary" :disabled="submitting">{{ submitting ? '运行中...' : '确定' }}</button>
          <button @click="showForm = false" class="btn" :disabled="submitting">取消</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ 详情视图 ============ -->
  <div v-else>
    <div style="margin-bottom:16px;display:flex;align-items:center;gap:12px">
      <button @click="backToList" class="btn btn-sm">← 返回</button>
      <h3 style="font-size:16px;font-weight:600;color:var(--text-heading);margin:0">
        {{ currentRecord.name }}
      </h3>
      <span style="color:#888;font-size:12px">{{ currentRecord.start_date }} ~ {{ currentRecord.end_date }}</span>
    </div>

    <!-- 评估指标卡 -->
    <div class="metric-grid">
      <div v-for="m in metrics" :key="m.label" class="metric-card">
        <span class="metric-label">{{ m.label }}</span>
        <span class="metric-value">{{ m.value }}</span>
      </div>
    </div>

    <!-- 净值曲线 -->
    <div v-if="chart" class="card chart-wrap">
      <div class="chart-title">净值曲线</div>
      <svg class="net-value-chart" :viewBox="`0 0 ${chart.W} ${chart.H}`" @mousemove="onChartMove" @mouseleave="onChartLeave">
        <g v-for="t in chart.yTicks" :key="t.label">
          <line :x1="chart.PAD_L" :x2="chart.W - chart.PAD_R" :y1="t.y" :y2="t.y" stroke="#e5e7eb" stroke-width="1" />
          <text :x="chart.PAD_L - 8" :y="t.y + 4" text-anchor="end" font-size="11" fill="#888">{{ t.label }}</text>
        </g>
        <g v-for="(t, i) in chart.xTicks" :key="i">
          <text :x="t.x" :y="chart.H - 8" text-anchor="middle" font-size="11" fill="#888">{{ t.label }}</text>
        </g>
        <polyline :points="chart.points" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
        <g v-if="hoverIdx !== null">
          <line :x1="chart.x(hoverIdx)" :x2="chart.x(hoverIdx)" :y1="chart.PAD_T" :y2="chart.H - chart.PAD_B" stroke="#bbb" stroke-width="1" stroke-dasharray="3,3" />
          <circle :cx="chart.x(hoverIdx)" :cy="chart.y(chart.values[hoverIdx])" r="4" fill="#3b82f6" stroke="#fff" stroke-width="2" />
        </g>
        <rect class="chart-hit" :x="chart.PAD_L" :y="chart.PAD_T" :width="chart.W - chart.PAD_L - chart.PAD_R" :height="chart.H - chart.PAD_T - chart.PAD_B" fill="transparent" />
      </svg>
      <div v-if="hoverIdx !== null" class="chart-tooltip">
        <span class="tt-date">{{ chart.dates[hoverIdx] }}</span>
        <span class="tt-value">¥{{ Number(chart.values[hoverIdx]).toLocaleString() }}</span>
      </div>
    </div>
    <div v-else class="card empty-state"><p>快照不足，无法绘制曲线</p></div>

    <!-- 交易明细 -->
    <div class="card table-wrap" style="margin-top:16px">
      <div class="chart-title">交易明细</div>
      <table>
        <thead><tr><th>时间</th><th>股票</th><th>信号</th><th>买卖</th><th>价格</th><th>数量</th><th>金额</th><th>佣金</th><th>印花税</th></tr></thead>
        <tbody>
          <tr v-for="t in (detail?.trades || [])" :key="t.id">
            <td style="font-size:12px;color:#888">{{ t.bar_time?.replace('T', ' ') }}</td>
            <td>{{ t.stock_code }}</td>
            <td>{{ t.signal_name }}</td>
            <td><span class="badge" :class="t.trade_type === 'BUY' ? 'badge-blue' : 'badge-gray'">{{ t.trade_type === 'BUY' ? '买入' : '卖出' }}</span></td>
            <td>{{ Number(t.price).toFixed(3) }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ Number(t.amount).toLocaleString() }}</td>
            <td>{{ Number(t.commission).toFixed(2) }}</td>
            <td>{{ Number(t.stamp_duty).toFixed(2) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="(detail?.trades || []).length === 0" class="empty-state"><p>无交易记录</p></div>
    </div>
  </div>
</template>
