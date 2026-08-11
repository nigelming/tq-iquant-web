<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord,
} from '../api'
import type {
  BacktestRecordItem, BacktestDetailItem, PortfolioItem,
  BacktestEvaluationItem,
} from '../api'

// ===== 列表 =====
const records = ref<BacktestRecordItem[]>([])
const portfolios = ref<PortfolioItem[]>([])
const loading = ref(true)

// ===== 发起弹窗 =====
const showForm = ref(false)
const submitting = ref(false)
const form = ref({ portfolio_strategy_id: 0, name: '', start_date: '', end_date: '' })

// ===== 详情视图 =====
const currentRecord = ref<BacktestRecordItem | null>(null)
const detail = ref<BacktestDetailItem | null>(null)
const errorMsg = ref('')

// echarts 实例（净值 + 回撤）
const equityChartEl = ref<HTMLDivElement | null>(null)
const drawdownChartEl = ref<HTMLDivElement | null>(null)
let equityChart: echarts.ECharts | null = null
let drawdownChart: echarts.ECharts | null = null

// 交易明细分页
const currentPage = ref(1)
const pageSize = 20

const STATUS_LABEL: Record<string, string> = {
  completed: '已完成', running: '运行中', failed: '失败', pending: '待运行',
}

// 信号类型 → 中文（公式 OPEN/ADD/REDUCE/CLOSE + 风控 STOP_LOSS/TAKE_PROFIT/TRAILING_STOP）
const SIGNAL_TYPE_LABEL: Record<string, string> = {
  OPEN: '开仓', ADD: '加仓', REDUCE: '减仓', CLOSE: '清仓',
  STOP_LOSS: '止损', TAKE_PROFIT: '止盈', TRAILING_STOP: '移动止损',
}
function signalTypeLabel(t: string): string {
  return SIGNAL_TYPE_LABEL[t] || t || ''
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
  errorMsg.value = ''
  try {
    const [recs, ps] = await Promise.all([
      getBacktestRecords(),
      getPortfolios().catch(() => []),
    ])
    records.value = recs
    portfolios.value = ps
  } catch (e) {
    errorMsg.value = '加载失败：' + errMsg(e)
    records.value = []
  } finally {
    loading.value = false
  }
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
  try {
    detail.value = await getBacktestDetail(id)
  } catch (e) {
    alert(`加载详情失败：${errMsg(e)}`)
    return
  }
  currentRecord.value = detail.value?.record || null
  currentPage.value = 1
  // 等 DOM 渲染后初始化图表
  await nextTick()
  initCharts()
}

async function onDelete(id: number) {
  if (!confirm('确定删除该回测记录？删除后不可恢复。')) return
  try {
    await deleteBacktestRecord(id)
    await load()
  } catch (e) {
    alert(`删除失败：${errMsg(e)}`)
  }
}

function backToList() {
  currentRecord.value = null
  detail.value = null
  // 销毁图表实例，避免内存泄漏
  equityChart?.dispose(); equityChart = null
  drawdownChart?.dispose(); drawdownChart = null
}

// ===== 格式化 =====
function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(2)}%`
}
function num(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined) return '—'
  return Number(v).toFixed(digits)
}
// 按值正负着色
function valueClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return Number(v) >= 0 ? 'text-green' : 'text-red'
}

// ===== 指标构建（18 项，参考 quant-cy buildMetricsList）=====
function buildMetrics(m: BacktestEvaluationItem | null | undefined) {
  if (!m) return []
  const retVolRatio = m.annual_return != null && m.volatility
    ? m.annual_return / m.volatility : null
  return [
    { label: '总收益', value: pct(m.total_return), cls: valueClass(m.total_return) },
    { label: '年化收益', value: pct(m.annual_return), cls: valueClass(m.annual_return) },
    { label: '最大回撤', value: pct(m.max_drawdown), cls: 'text-red' },
    { label: '年化波动率', value: pct(m.volatility), cls: 'text-orange' },
    { label: '夏普比率', value: num(m.sharpe_ratio, 2), cls: 'text-blue' },
    { label: '卡玛比率', value: num(m.calmar_ratio, 2), cls: 'text-blue' },
    { label: '索提诺比率', value: num(m.sortino_ratio, 2), cls: 'text-blue' },
    { label: 'VaR (95%)', value: pct(m.var_95), cls: 'text-red' },
    { label: 'CVaR (95%)', value: pct(m.cvar_95), cls: 'text-red' },
    { label: '收益/波动比', value: num(retVolRatio, 2), cls: 'text-blue' },
    { label: '平均修复天数', value: num(m.avg_recovery_days, 0), cls: 'text-orange' },
    { label: '最大修复天数', value: num(m.max_recovery_days, 0), cls: 'text-orange' },
    { label: '胜率', value: pct(m.win_rate), cls: 'text-blue' },
    { label: '盈亏比', value: num(m.profit_factor, 2), cls: 'text-blue' },
    { label: '交易次数', value: String(m.total_trades ?? '—'), cls: '' },
    { label: '平均持仓天数', value: num(m.avg_holding_days, 1), cls: 'text-blue' },
    { label: 'Ulcer指数', value: num(m.ulcer_index, 2), cls: 'text-orange' },
    { label: '收益稳定性', value: num(m.return_stability, 2), cls: 'text-blue' },
  ]
}

// 组合整体 18 项指标
const portfolioMetrics = computed(() => buildMetrics(detail.value?.evaluations))

// 策略对比卡片
const strategyCards = computed(() => {
  const evals = detail.value?.strategy_evaluations || []
  return evals.map((s) => ({
    strategy_id: s.strategy_id,
    strategy_name: s.strategy_name,
    metrics: buildMetrics(s),
  }))
})

// 关键指标摘要（4 卡）
const summaryCards = computed(() => {
  const e = detail.value?.evaluations
  if (!e) return []
  return [
    { label: '总收益', value: pct(e.total_return), tone: 'positive' },
    { label: '年化收益', value: pct(e.annual_return), tone: 'positive' },
    { label: '最大回撤', value: pct(e.max_drawdown), tone: 'danger' },
    { label: '夏普比率', value: num(e.sharpe_ratio, 2), tone: 'neutral' },
  ]
})

// 整体表现 6 渐变卡
const overallStats = computed(() => {
  const e = detail.value?.evaluations
  if (!e) return []
  return [
    { label: '总收益', value: pct(e.total_return) },
    { label: '年化收益', value: pct(e.annual_return) },
    { label: '最大回撤', value: pct(e.max_drawdown) },
    { label: '年化波动率', value: pct(e.volatility) },
    { label: '夏普比率', value: num(e.sharpe_ratio, 2) },
    { label: '卡玛比率', value: num(e.calmar_ratio, 2) },
  ]
})

// 副标题：交易天数
const subtitle = computed(() => {
  const snaps = detail.value?.snapshots || []
  if (snaps.length < 2) return ''
  const days = snaps.length - 1
  const years = (days / 252).toFixed(2)
  return `回测周期: ${days} 个交易日 (${years}年)`
})

// ===== 净值 / 回撤曲线数据 =====
const equityDates = computed(() => (detail.value?.snapshots || []).map((s) => s.snap_date))
const portfolioCurve = computed(() => (detail.value?.snapshots || []).map((s) => Number(s.total_value)))

// 基准指数曲线（归一化为累计收益率%，与组合/策略同轴）。
// benchmark_value 缺失（旧记录/未配置）→ hasBenchmark=false，前端隐藏基准线。
const benchmarkRaw = computed(() => (detail.value?.snapshots || []).map((s) => s.benchmark_value))
const hasBenchmark = computed(() => benchmarkRaw.value.some((v) => v !== null && v !== undefined))
const benchmarkCurve = computed(() => {
  const vals = benchmarkRaw.value
  const base = vals.find((v) => v !== null && v !== undefined) ?? 0
  return vals.map((v) => {
    if (v === null || v === undefined) return null
    return base > 0 ? ((v - base) / base) * 100 : 0
  })
})

// 回撤由组合净值序列前端算（peak − current）
const drawdownCurve = computed(() => {
  const vals = portfolioCurve.value
  let peak = vals[0] ?? 0
  return vals.map((v) => {
    if (v > peak) peak = v
    // 用百分比表示回撤（负值）
    return peak > 0 ? -((peak - v) / peak) * 100 : 0
  })
})

// 策略净值曲线（归一化到百分比收益率，便于与组合同轴比较）
const strategyCurves = computed(() => {
  const sSnaps = detail.value?.strategy_snapshots || []
  return sSnaps.map((s) => {
    const curve = s.curve || []
    // 用首日为基准算累计收益率%
    const base = Number(curve[0]?.total_value) || 0
    return {
      name: s.strategy_name,
      data: curve.map((p) => base > 0 ? ((Number(p.total_value) - base) / base) * 100 : 0),
      dates: curve.map((p) => p.snap_date),
    }
  })
})

// 组合净值也归一化为累计收益率%，与策略曲线同轴
const portfolioReturnPct = computed(() => {
  const vals = portfolioCurve.value
  const base = vals[0] ?? 0
  return vals.map((v) => base > 0 ? ((v - base) / base) * 100 : 0)
})

// ===== 交易明细分页 =====
const totalPages = computed(() => Math.max(1, Math.ceil((detail.value?.trades || []).length / pageSize)))
const paginatedTrades = computed(() => {
  const trades = detail.value?.trades || []
  const start = (currentPage.value - 1) * pageSize
  return trades.slice(start, start + pageSize)
})

// ===== echarts 初始化 =====
function initCharts() {
  initEquityChart()
  initDrawdownChart()
}

function initEquityChart() {
  if (!equityChartEl.value || portfolioCurve.value.length < 2) return
  equityChart?.dispose()
  equityChart = echarts.init(equityChartEl.value)

  const series: any[] = [{
    name: '组合',
    type: 'line',
    data: portfolioReturnPct.value,
    smooth: true,
    areaStyle: {
      color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: 'rgba(102, 126, 234, 0.3)' },
        { offset: 1, color: 'rgba(102, 126, 234, 0.05)' },
      ]),
    },
    itemStyle: { color: '#667eea' },
    markLine: {
      silent: true,
      data: [{ yAxis: 0, lineStyle: { color: '#999', type: 'dashed' } }],
      label: { show: false },
    },
  }]
  // 策略曲线
  for (const sc of strategyCurves.value) {
    series.push({
      name: sc.name, type: 'line', data: sc.data, smooth: true,
      lineStyle: { width: 1.5 },
    })
  }
  // 基准曲线（灰色虚线，无面积填充）；无基准数据则不画
  if (hasBenchmark.value) {
    series.push({
      name: '基准', type: 'line', data: benchmarkCurve.value, smooth: true,
      symbol: 'none',
      lineStyle: { width: 1.5, type: 'dashed', color: '#9ca3af' },
      itemStyle: { color: '#9ca3af' },
    })
  }

  const legendData = ['组合', ...strategyCurves.value.map((s) => s.name)]
  if (hasBenchmark.value) legendData.push('基准')

  equityChart.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        let r = params[0].axisValue + '<br/>'
        params.forEach((p: any) => {
          // 基准在某些日期可能为 null（无数据日）→ 显示 '—'
          const val = p.value == null ? '—' : `${p.value.toFixed(2)}%`
          r += `${p.marker}${p.seriesName}: ${val}<br/>`
        })
        return r
      },
    },
    legend: { data: legendData, bottom: 0 },
    grid: { left: '3%', right: '4%', bottom: '15%', top: '10%', containLabel: true },
    xAxis: { type: 'category', data: equityDates.value, boundaryGap: false },
    yAxis: { type: 'value', name: '收益率(%)', scale: true,
      axisLabel: { formatter: '{value}%' },
      splitLine: { lineStyle: { type: 'dashed' } } },
    series,
  })
}

function initDrawdownChart() {
  if (!drawdownChartEl.value || drawdownCurve.value.length < 2) return
  drawdownChart?.dispose()
  drawdownChart = echarts.init(drawdownChartEl.value)
  drawdownChart.setOption({
    tooltip: { trigger: 'axis', formatter: '{b}<br />回撤: {c}%' },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '10%', containLabel: true },
    xAxis: { type: 'category', data: equityDates.value, boundaryGap: false },
    yAxis: { type: 'value', name: '回撤(%)', max: 0 },
    series: [{
      name: '回撤', type: 'line', data: drawdownCurve.value, smooth: true,
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(239, 68, 68, 0.3)' },
          { offset: 1, color: 'rgba(239, 68, 68, 0.05)' },
        ]),
      },
      itemStyle: { color: '#ef4444' },
    }],
  })
}

function handleResize() {
  equityChart?.resize()
  drawdownChart?.resize()
}

onMounted(() => {
  load()
  window.addEventListener('resize', handleResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  equityChart?.dispose()
  drawdownChart?.dispose()
})
</script>

<template>
  <!-- ============ 列表视图 ============ -->
  <div v-if="!currentRecord">
    <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
      <button @click="openForm" class="btn btn-primary">+ 发起回测</button>
    </div>

    <div v-if="errorMsg" class="card" style="padding:12px;color:#c0392b;margin-bottom:12px">
      {{ errorMsg }}
    </div>

    <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
    <div v-else class="card table-wrap">
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
              <button @click="onDelete(r.id)" class="btn btn-sm">删除</button>
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

  <!-- ============ 详情报告视图 ============ -->
  <div v-else class="report-page">
    <!-- 报告头 -->
    <div class="report-header">
      <button @click="backToList" class="back-link btn btn-sm">← 返回</button>
      <h1>{{ currentRecord.name }}</h1>
      <p class="subtitle">{{ currentRecord.start_date }} ~ {{ currentRecord.end_date }} · {{ subtitle }}</p>
    </div>

    <!-- 关键指标摘要 -->
    <div class="section-title">关键指标摘要</div>
    <div class="summary-section">
      <div v-for="c in summaryCards" :key="c.label" class="summary-card" :class="c.tone">
        <div class="title">{{ c.label }}</div>
        <div class="value">{{ c.value }}</div>
      </div>
    </div>

    <!-- 整体表现指标 -->
    <div class="section-title">整体表现指标</div>
    <div class="stats-grid">
      <div v-for="s in overallStats" :key="s.label" class="stat-card">
        <div class="value">{{ s.value }}</div>
        <div class="label">{{ s.label }}</div>
      </div>
    </div>

    <!-- 策略对比分析 -->
    <template v-if="strategyCards.length > 0">
      <div class="section-title">策略对比分析</div>
      <div class="strategy-comparison">
        <div v-for="sc in strategyCards" :key="sc.strategy_id" class="strategy-card">
          <h3>{{ sc.strategy_name }}</h3>
          <div class="risk-metrics">
            <div v-for="m in sc.metrics" :key="m.label" class="metric-row">
              <span class="metric-label">{{ m.label }}</span>
              <span :class="m.cls">{{ m.value }}</span>
            </div>
          </div>
        </div>
        <!-- 组合整体卡 -->
        <div class="strategy-card portfolio-card">
          <h3>组合整体</h3>
          <div class="risk-metrics">
            <div v-for="m in portfolioMetrics" :key="m.label" class="metric-row">
              <span class="metric-label">{{ m.label }}</span>
              <span :class="m.cls">{{ m.value }}</span>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 净值曲线 -->
    <div class="section-title">净值曲线</div>
    <div class="chart-box">
      <div ref="equityChartEl" class="chart-container"></div>
    </div>

    <!-- 回撤曲线 -->
    <div class="section-title">回撤曲线</div>
    <div class="chart-box">
      <div ref="drawdownChartEl" class="chart-container"></div>
    </div>

    <!-- 交易明细 -->
    <div class="section-title">交易明细</div>
    <div class="table-container">
      <table class="trade-table">
        <thead>
          <tr>
            <th>时间</th><th>策略</th><th>信号来源</th><th>买卖</th><th>代码</th>
            <th>数量</th><th>价格</th><th>金额</th><th>佣金</th><th>印花税</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in paginatedTrades" :key="t.id">
            <td style="font-size:12px;color:#888">{{ t.bar_time?.replace('T', ' ') }}</td>
            <td><span class="strategy-badge">{{ t.strategy_name || `#${t.strategy_id}` }}</span></td>
            <td>
              <span style="font-size:12px">{{ t.signal_name || '—' }}</span>
              <span v-if="t.signal_type" class="signal-badge">{{ signalTypeLabel(t.signal_type) }}</span>
            </td>
            <td :class="t.trade_type === 'BUY' ? 'text-green' : 'text-red'">{{ t.trade_type === 'BUY' ? '买入' : '卖出' }}</td>
            <td>{{ t.stock_code }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ Number(t.price).toFixed(3) }}</td>
            <td>{{ Number(t.amount).toLocaleString() }}</td>
            <td>{{ Number(t.commission).toFixed(2) }}</td>
            <td>{{ Number(t.stamp_duty).toFixed(2) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="(detail?.trades || []).length === 0" class="empty-state"><p>无交易记录</p></div>
      <div v-if="(detail?.trades || []).length > pageSize" class="pagination">
        <button :disabled="currentPage === 1" @click="currentPage--" class="page-btn">上一页</button>
        <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页</span>
        <button :disabled="currentPage === totalPages" @click="currentPage++" class="page-btn">下一页</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.report-page {
  max-width: 1400px;
  margin: 0 auto;
}
.report-header {
  text-align: center;
  margin-bottom: 24px;
  position: relative;
}
.back-link {
  position: absolute;
  left: 0;
  top: 0;
  text-decoration: none;
}
.report-header h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: var(--text-heading, #1a1a2e);
}
.subtitle {
  margin: 6px 0 0;
  font-size: 13px;
  color: #888;
}

/* 区块标题 */
.section-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-heading, #1a1a2e);
  margin: 24px 0 14px;
  padding-bottom: 8px;
  border-bottom: 2px solid #667eea;
  display: flex;
  align-items: center;
  gap: 10px;
}
.section-title::before {
  content: '';
  width: 4px;
  height: 18px;
  background: linear-gradient(180deg, #667eea, #764ba2);
  border-radius: 2px;
}

/* 关键指标摘要 */
.summary-section {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 8px;
}
.summary-card {
  background: #fff;
  border-radius: 10px;
  padding: 16px;
  border-left: 4px solid #667eea;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.summary-card .title { font-size: 12px; color: #6b7280; margin-bottom: 6px; }
.summary-card .value { font-size: 22px; font-weight: 700; color: #333; }
.summary-card.positive { border-left-color: #10b981; }
.summary-card.danger { border-left-color: #ef4444; }

/* 整体表现 6 渐变卡 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
  margin-bottom: 8px;
}
.stat-card {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 10px;
  padding: 18px 12px;
  color: #fff;
  text-align: center;
  transition: transform 0.25s, box-shadow 0.25s;
}
.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(102, 126, 234, 0.3);
}
.stat-card .value { font-size: 22px; font-weight: 700; }
.stat-card .label { font-size: 12px; opacity: 0.9; margin-top: 6px; }

/* 策略对比 */
.strategy-comparison {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
  margin-bottom: 8px;
}
.strategy-card {
  background: #f8f9fa;
  border-radius: 10px;
  padding: 16px;
  border: 1px solid #e9ecef;
}
.strategy-card.portfolio-card {
  border: 2px solid #667eea;
  background: linear-gradient(135deg, #f8f9fa 0%, #e8f4f8 100%);
}
.strategy-card h3 {
  margin: 0 0 12px;
  font-size: 14px;
  color: #333;
}
.risk-metrics {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
}
.metric-row {
  display: flex;
  justify-content: space-between;
  padding: 5px 10px;
  background: #fff;
  border-radius: 5px;
  font-size: 12px;
}
.metric-label { color: #666; }
.metric-row span:last-child { font-weight: 600; }

.text-green { color: #10b981; font-weight: 600; }
.text-red { color: #ef4444; font-weight: 600; }
.text-orange { color: #f59e0b; font-weight: 600; }
.text-blue { color: #667eea; font-weight: 600; }

/* 图表 */
.chart-box {
  background: #f8f9fa;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 8px;
}
.chart-container {
  width: 100%;
  height: 380px;
}

/* 交易明细表 */
.table-container {
  background: #f8f9fa;
  border-radius: 10px;
  padding: 16px;
  overflow-x: auto;
}
.trade-table {
  width: 100%;
  border-collapse: collapse;
}
.trade-table th {
  padding: 10px 12px;
  text-align: left;
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: #fff;
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
}
.trade-table td {
  padding: 9px 12px;
  border-bottom: 1px solid #e9ecef;
  font-size: 12px;
}
.trade-table tr:hover { background: #e9ecef; }
.strategy-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  background: #e0e7ff;
  color: #4338ca;
}
.signal-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  background: #f0f1f3;
  color: #666;
}

/* 分页 */
.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 14px;
  margin-top: 14px;
}
.page-btn {
  padding: 6px 16px;
  background: #667eea;
  color: #fff;
  border: none;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
}
.page-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.page-info { font-size: 13px; color: #6b7280; }

/* 响应式 */
@media (max-width: 1200px) {
  .stats-grid { grid-template-columns: repeat(3, 1fr); }
  .summary-section { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .summary-section { grid-template-columns: 1fr; }
  .risk-metrics { grid-template-columns: 1fr; }
}
</style>
