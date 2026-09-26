<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord, recomputeCrisisAlpha,
} from '../api'
import type {
  BacktestRecordItem, BacktestDetailItem, PortfolioItem,
  BacktestEvaluationItem, DecisionSummaryItem, DecisionEventItem,
  CrisisAlphaData, CrisisAlphaBenchmarkResolution,
} from '../api'
import { gateLabel, LAYER_LABEL, ACTION_LABEL } from '../utils/liveEvents'

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

// 收益分布图（heatmap + box×2 + bar）
const monthlyHeatEl = ref<HTMLDivElement | null>(null)
const dailyBoxEl = ref<HTMLDivElement | null>(null)
const monthlyBoxEl = ref<HTMLDivElement | null>(null)
const annualBarEl = ref<HTMLDivElement | null>(null)
let monthlyHeatChart: echarts.ECharts | null = null
let dailyBoxChart: echarts.ECharts | null = null
let monthlyBoxChart: echarts.ECharts | null = null
let annualBarChart: echarts.ECharts | null = null

const crisisAlphaSectionEl = ref<HTMLDivElement | null>(null)
const recomputing = ref(false)
const recomputeMsg = ref('')
const recomputeWarn = ref(false)
const recomputeMsgClass = computed(() => (recomputeWarn.value ? 'ca-msg ca-warn' : 'ca-msg'))

const CRISIS_COLORS: Record<string, string> = {
  逆势上涨: '#d6403a', 跌得少: '#f0a429', 跟着跌: '#9dc88d', 跌更多: '#1f6e4f',
}
const CRISIS_SCORE_COLORS: Record<string, string> = {
  卓越: '#d6403a', 优秀: '#f0a429', 一般: '#b0a890',
}
const CRISIS_CATS = ['逆势上涨', '跌得少', '跟着跌', '跌更多']
const MONTH_STATES = ['大涨', '小涨', '小跌', '大跌']

const caCollapsed = ref(true)
const crisisAlpha = computed(() => detail.value?.crisis_alpha ?? null)
const caStatus = computed(() => crisisAlpha.value?.status ?? null)
const caSkipped = computed(() => crisisAlpha.value?.skipped_benchmarks || [])
const ca = computed<CrisisAlphaData | null>(() => {
  const item = crisisAlpha.value
  if (!item || item.status !== 'completed' || !item.data) return null
  return item.data
})

const caKpis = computed(() => {
  const d = ca.value
  if (!d) return []
  const q = d.quadrant
  const s = d.performance.s
  return [
    {
      label: '危机Alpha占比',
      value: pct1(q.alpha_ratio),
      sub: `逆势上涨+跌得少 / 下跌周 · ${d.alpha_rating}`,
      color: CRISIS_COLORS.逆势上涨,
    },
    {
      label: '综合危机保护评分',
      value: num(d.score.total_score, 1),
      sub: d.score.rating,
      color: CRISIS_SCORE_COLORS[d.score.rating] || '#6b7280',
    },
    {
      label: '下跌周逆势上涨比例',
      value: pct1(q.up_rate),
      sub: `${q.counts.逆势上涨 ?? 0} / ${q.n_down} 周`,
      color: CRISIS_COLORS.逆势上涨,
    },
    {
      label: '策略累计收益',
      value: pct1(s.cum),
      sub: `年化 ${pct1(s.ann)} ｜ MDD ${pct1(s.mdd)}`,
      color: '#1f3a68',
    },
  ]
})

const caPerfRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const s = d.performance.s
  const b = d.performance.b
  const corr = d.performance.corr
  return [
    { 指标: '累计收益', 策略: pct1(s.cum), 基准: pct1(b.cum) },
    { 指标: '年化收益', 策略: pct1(s.ann), 基准: pct1(b.ann) },
    { 指标: '年化波动', 策略: pct1(s.vol), 基准: pct1(b.vol) },
    { 指标: '最大回撤', 策略: pct1(s.mdd), 基准: pct1(b.mdd) },
    {
      指标: '相关系数',
      策略: '—',
      基准: corr !== null && corr < 0.3
        ? `${num(corr, 3)}（低/负相关，对冲属性显著）`
        : num(corr, 3),
    },
  ]
})

const caCatRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const q = d.quadrant
  const n = Math.max(q.n_down, 1)
  const rows: Array<{
    分类: string
    周数: number
    占比: string
    平均周收益: string
    avg: number | null
    color: string
  }> = CRISIS_CATS.map((c) => ({
    分类: c,
    周数: q.counts[c] ?? 0,
    占比: pct1((q.counts[c] ?? 0) / n),
    平均周收益: pctSigned(q.avg_ret[c] ?? 0),
    avg: q.avg_ret[c] ?? 0,
    color: CRISIS_COLORS[c],
  }))
  rows.push({
    分类: '危机Alpha合计',
    周数: (q.counts.逆势上涨 ?? 0) + (q.counts.跌得少 ?? 0),
    占比: pct1(q.alpha_ratio),
    平均周收益: '—',
    avg: null,
    color: '#111827',
  })
  return rows
})

const caCondRows = computed((): Array<{
  条件: string
  周数: number
  股票平均: string
  策略平均: string
  s_avg: number | null
  收涨比例: string
}> => {
  const d = ca.value
  if (!d) return []
  return d.conditions.map((t) => ({
    条件: `${d.benchmark_name}周跌 >${(Math.abs(t.threshold) * 100).toFixed(0)}%`,
    周数: t.n,
    股票平均: pctSigned(t.b_avg),
    策略平均: pctSigned(t.s_avg),
    s_avg: t.s_avg,
    收涨比例: pct1(t.up_rate),
  }))
})

const caPie = computed(() => {
  const d = ca.value
  if (!d) return null
  const q = d.quadrant
  const values = CRISIS_CATS.map((c) => q.counts[c] ?? 0)
  if (values.reduce((a, b) => a + b, 0) === 0) return null
  const n = Math.max(q.n_down, 1)
  return {
    labels: CRISIS_CATS.map((c, i) => `${c}  ${(values[i] / n * 100).toFixed(1)}%`),
    shortLabels: CRISIS_CATS.map((c) => `${c} ${values[CRISIS_CATS.indexOf(c)]}周`),
    values,
    colors: CRISIS_CATS.map((c) => CRISIS_COLORS[c]),
    alpha: (q.alpha_ratio * 100).toFixed(1),
  }
})

const caTop = computed(() => {
  const rows = ca.value?.extreme_top ?? []
  if (!rows.length) return null
  return {
    labels: rows.map((r) => String(r.日期).slice(2)),
    b: rows.map((r) => +((r.基准 ?? 0) * 100).toFixed(4)),
    s: rows.map((r) => +((r.策略 ?? 0) * 100).toFixed(4)),
  }
})
const caTopStat = computed(() => {
  const rows = ca.value?.extreme_top ?? []
  return {
    n: rows.length,
    up: rows.filter((r) => (r.策略 ?? 0) > 0).length,
    beat: rows.filter((r) => (r.策略 ?? 0) > (r.基准 ?? 0)).length,
  }
})

const caScatter = computed(() => {
  const d = ca.value
  if (!d) return null
  return CRISIS_CATS.map((cat) => ({
    name: cat,
    color: CRISIS_COLORS[cat],
    data: d.weekly_detail
      .filter((r) => r.分类 === cat)
      .map((r) => [
        +(((r.基准收益 ?? 0)) * 100).toFixed(4),
        +(((r.策略收益 ?? 0)) * 100).toFixed(4),
      ]),
  })).filter((s) => s.data.length > 0)
})

const caEventRows = computed((): Array<{
  回撤起点: string
  回撤低点: string
  基准: string
  策略: string
  超额: string
  strategy_ret: number | null
  excess: number | null
  结果: string
  hedged: boolean
}> => (ca.value?.deep_dd_events ?? []).map((e) => ({
  回撤起点: e.start,
  回撤低点: e.end,
  基准: pctSigned(e.benchmark_ret),
  策略: pctSigned(e.strategy_ret),
  超额: pctSigned(e.excess),
  strategy_ret: e.strategy_ret,
  excess: e.excess,
  结果: `${e.hedged ? '✓ ' : '✗ '}${e.verdict}`,
  hedged: e.hedged,
})))
const caEventStat = computed(() => {
  const ev = ca.value?.deep_dd_events ?? []
  const hedged = ev.filter((e) => e.hedged).length
  return { n: ev.length, hedged, pct: ev.length ? Math.round(hedged / ev.length * 100) : 0 }
})

const caMultiRows = computed(() => (ca.value?.multi_bench ?? []).map((m) => ({
  基准: `${m.name}${m.is_main ? '（主基准）' : ''}`,
  下跌周数: m.n_down,
  危机Alpha: pct1(m.alpha_ratio),
  逆势上涨比例: pct1(m.up_rate),
})))
const caMultiSeries = computed(() => {
  const rows = ca.value?.multi_bench ?? []
  if (rows.length < 2) return null
  return {
    labels: rows.map((m) => `${m.name}${m.is_main ? '（主基准）' : ''}`),
    values: rows.map((m) => +(m.alpha_ratio * 100).toFixed(4)),
    colors: rows.map((m) => (m.is_main ? '#d6403a' : '#f0a429')),
    main: +(((ca.value?.quadrant.alpha_ratio ?? 0)) * 100).toFixed(4),
  }
})

const caMonthRows = computed(() => {
  const d = ca.value
  if (!d) return []
  return MONTH_STATES.map((s) => {
    const st = d.monthly.states[s]
    return {
      市场状态: s,
      月数: st?.n ?? 0,
      股票月均: pctSigned(st?.b_avg ?? 0, 1),
      策略月均: pctSigned(st?.s_avg ?? 0),
      正收益比例: pct1(st?.win_rate ?? 0),
    }
  })
})
const caBigDownRows = computed(() => (ca.value?.monthly.big_down ?? []).map((r) => ({
  月份: String(r.日期).slice(0, 7),
  基准: pctSigned(r.基准, 1),
  策略: pctSigned(r.策略),
})))
const caMonthSeries = computed(() => {
  const d = ca.value
  if (!d) return null
  return {
    labels: MONTH_STATES.map((s) => `${s}（${d.monthly.states[s]?.n ?? 0}月）`),
    b: MONTH_STATES.map((s) => +(((d.monthly.states[s]?.b_avg ?? 0)) * 100).toFixed(4)),
    s: MONTH_STATES.map((s) => +(((d.monthly.states[s]?.s_avg ?? 0)) * 100).toFixed(4)),
  }
})

const caCorrRows = computed(() => {
  const d = ca.value
  if (!d) return null
  const rc = d.rolling_corr
  if (!rc.length) return null
  const first = rc[0].date
  const early = d.expanding_corr.filter((p) => p.date < first)
  const dates: string[] = []
  const seen = new Set<string>()
  for (const p of [...early, ...rc]) {
    if (!seen.has(p.date)) { seen.add(p.date); dates.push(p.date) }
  }
  const pos = new Map(dates.map((v, i) => [v, i]))
  const earlyData: (number | null)[] = new Array(dates.length).fill(null)
  for (const p of early) earlyData[pos.get(p.date)!] = p.value
  const rcData: (number | null)[] = new Array(dates.length).fill(null)
  for (const p of rc) rcData[pos.get(p.date)!] = p.value
  if (early.length) earlyData[pos.get(first)!] = rc[0].value
  return { dates, earlyData, rcData }
})
const caAnnualRows = computed(() => (ca.value?.annual ?? []).map((r) => ({
  年度: `${r.年份}${r.完整 ? '' : '*'}`,
  下跌周: r.下跌周,
  危机Alpha: pct1(r.危机Alpha),
  策略收益: pctSigned(r.策略收益, 1),
  基准收益: pctSigned(r.基准收益, 1),
  完整: r.完整,
})))
const caAnnualSeries = computed(() => {
  const rows = ca.value?.annual ?? []
  if (!rows.length) return null
  return {
    labels: rows.map((r) => `${r.年份}${r.完整 ? '' : '*'}`),
    values: rows.map((r) => +(r.危机Alpha * 100).toFixed(4)),
    colors: rows.map((r) => (r.危机Alpha * 100 >= 85 ? '#d6403a' : '#f0a429')),
    main: +(((ca.value?.quadrant.alpha_ratio ?? 0)) * 100).toFixed(4),
  }
})

const caCostRows = computed(() => {
  const d = ca.value
  if (!d) return []
  const c = d.cost
  const n = d.strategy_name
  const signed = (v: number | null, digits: number) =>
    v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`
  return [
    { 指标: '上涨周拖后腿比例', 数值: pct1(c.drag), 解读: `股票上涨时${n}为负的周次占比，属低β配置型策略的正常代价` },
    { 指标: '上涨周同步上涨比例', 数值: pct1(c.sync_up), 解读: `股票上涨时${n}跟随上涨的周次占比` },
    { 指标: '收益弹性比', 数值: num(c.elasticity, 2), 解读: `${n}上涨周均收益 / 股票上涨周均收益，值低说明不依赖股市β` },
    { 指标: '下尾相关（下跌周内）', 数值: signed(c.tail_corr, 2), 解读: '下跌情境下相关性为负，越跌越对冲' },
    { 指标: 'Q1分位相关（最差25%周）', 数值: signed(c.q1_corr, 3), 解读: '极端下跌时相关性为负，尾部对冲有效' },
  ]
})

const caDimRows = computed(() => (ca.value?.score.dimensions ?? []).map((x) => ({
  维度: x.维度,
  类型: x.类型,
  权重: `${(x.权重 * 100).toFixed(0)}%`,
  得分: num(x.得分, 1),
  评级: x.评级,
  贡献: num(x.贡献, 1),
})))
const caDimSeries = computed(() => {
  const dims = ca.value?.score.dimensions ?? []
  if (!dims.length) return null
  const sorted = [...dims].sort((a, b) => b.得分 - a.得分)
  return {
    names: sorted.map((x) => x.维度),
    types: sorted.map((x) => x.类型),
    values: sorted.map((x) => Math.max(x.得分, 3)),
    raw: sorted.map((x) => x.得分),
    colors: sorted.map((x) => CRISIS_SCORE_COLORS[x.评级] || '#b0a890'),
  }
})

const caDownRows = computed(() => (ca.value?.down_weeks ?? []).map((r) => ({
  日期: r.日期,
  基准: pctSigned(r.基准收益 ?? 0),
  策略: pctSigned(r.策略收益 ?? 0),
  超额: pctSigned(r.超额 ?? 0),
  分类: r.分类,
  color: CRISIS_COLORS[r.分类] || '#111827',
})))

function caBenchmarkWarnText(br: CrisisAlphaBenchmarkResolution): string {
  const used = br.benchmark_name
  const live = br.live_benchmark_name
  const stored = br.stored_benchmark_code && br.stored_benchmark_code !== br.benchmark_code
    ? `（记录原为「${br.stored_benchmark_name || br.stored_benchmark_code}」）`
    : ''
  return br.from_snapshot
    ? `⚠️ 组合策略基准现为「${live}」，本次按回测快照基准「${used}」重算${stored}`
    : `⚠️ 该记录无参数快照，组合策略基准现为「${live}」，本次按记录原基准「${used}」重算${stored}`
}

async function recomputeCa() {
  const id = currentRecord.value?.id
  if (!id) return
  recomputing.value = true
  recomputeMsg.value = ''
  recomputeWarn.value = false
  try {
    const res = await recomputeCrisisAlpha(id)
    const base = res.status === 'completed'
      ? '重新计算完成'
      : `重新计算未完成：${res.error || res.status}`
    const br = res.benchmark_resolution
    recomputeWarn.value = !!br?.mismatch
    recomputeMsg.value = recomputeWarn.value && br
      ? `${base} ｜ ${caBenchmarkWarnText(br)}`
      : base
    detail.value = await getBacktestDetail(id)
    await nextTick()
    initCharts()
  } catch (e) {
    recomputeWarn.value = false
    recomputeMsg.value = `重新计算失败：${errMsg(e)}`
  } finally {
    recomputing.value = false
  }
}

function initCrisisAlphaCharts() {
  if (!ca.value) return
  initCaNavChart()
  initCaPieChart()
  initCaTopChart()
  initCaScatterChart()
  initCaMultiChart()
  initCaMonthChart()
  initCaCorrChart()
  initCaAnnualChart()
  initCaDimChart()
}

function initCaNavChart() {
  if (!caNavEl.value || !ca.value) return
  const d = ca.value
  const nav = d.nav
  caNavChart?.dispose()
  caNavChart = echarts.init(caNavEl.value)
  const mark = nav.shade_spans.length
    ? [{
        silent: true,
        itemStyle: { color: 'rgba(214,64,58,0.08)' },
        data: nav.shade_spans.map(([s, e]) => [{ xAxis: s }, { xAxis: e }]),
      }]
    : undefined
  caNavChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: [d.strategy_name, d.benchmark_name], bottom: 0 },
    grid: { left: 56, right: 24, bottom: 34, top: 40, containLabel: true },
    xAxis: { type: 'category', data: nav.dates, boundaryGap: false },
    yAxis: {
      type: 'value', name: '累计净值(起点=1.0)', scale: true,
      axisLabel: { formatter: (v: number) => v.toFixed(2) },
      splitLine: { lineStyle: { type: 'dashed' } },
    },
    series: [
      {
        name: d.strategy_name, type: 'line', data: nav.strategy, smooth: true, symbol: 'none',
        lineStyle: { color: '#d6403a', width: 2.2 }, itemStyle: { color: '#d6403a' },
        markArea: mark,
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#9ca3af', type: 'dashed' },
          data: [{ yAxis: 1 }],
        },
      },
      {
        name: d.benchmark_name, type: 'line', data: nav.benchmark, smooth: true, symbol: 'none',
        lineStyle: { color: '#4a7ec4', width: 1.8 }, itemStyle: { color: '#4a7ec4' },
      },
    ],
  })
}

function initCaPieChart() {
  if (!caPieEl.value || !caPie.value) return
  const p = caPie.value
  caPieChart?.dispose()
  caPieChart = echarts.init(caPieEl.value)
  caPieChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c}周' },
    legend: { bottom: 0, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['62%', '84%'],
      center: ['50%', '46%'],
      sort: false,
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: true, position: 'inner', color: '#fff', fontSize: 12,
        formatter: (x: any) => String(x.name).split('  ')[0] },
      labelLine: { show: false },
      data: p.labels.map((lb, i) => ({
        name: lb, value: p.values[i], itemStyle: { color: p.colors[i] },
      })),
    }],
    graphic: [
      { type: 'text', left: 'center', top: '38%',
        style: { text: `${p.alpha}%`, fontSize: 30, fontWeight: 800, fill: '#d6403a', textAlign: 'center' } },
      { type: 'text', left: 'center', top: '56%',
        style: { text: '危机Alpha', fontSize: 13, fill: '#1f3a68', textAlign: 'center' } },
    ],
  })
}

function initCaTopChart() {
  if (!caTopEl.value || !caTop.value) return
  const t = caTop.value
  caTopChart?.dispose()
  caTopChart = echarts.init(caTopEl.value)
  caTopChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['基准周收益', '策略周收益'], top: 0 },
    grid: { left: 52, right: 24, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'category', data: t.labels },
    yAxis: { type: 'value', name: '周收益(%)' },
    series: [
      { name: '基准周收益', type: 'bar', data: t.b, barMaxWidth: 20, itemStyle: { color: '#2e8b57' },
        label: { show: true, position: 'bottom', fontSize: 10, formatter: (x: any) => Number(x.value).toFixed(1) },
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#6b7280', width: 1 },
          data: [{ yAxis: 0 }],
        } },
      { name: '策略周收益', type: 'bar', data: t.s, barMaxWidth: 20, itemStyle: { color: '#d6403a' },
        label: { show: true, position: 'top', fontSize: 10, formatter: (x: any) => Number(x.value).toFixed(2) } },
    ],
  })
}

function initCaScatterChart() {
  if (!caScatterEl.value || !caScatter.value) return
  const sc = caScatter.value
  if (!sc.length) return
  caScatterChart?.dispose()
  caScatterChart = echarts.init(caScatterEl.value)
  const shortName = ca.value && ca.value.strategy_name.length > 7
    ? `${ca.value.strategy_name.slice(0, 7)}…`
    : (ca.value?.strategy_name || '')
  caScatterChart.setOption({
    tooltip: {
      trigger: 'item',
      formatter: (x: any) => `${x.seriesName}<br/>基准周收益 ${x.value[0]}%<br/>策略周收益 ${x.value[1]}%`,
    },
    legend: { orient: 'vertical', right: 12, top: 40, textStyle: { fontSize: 12 } },
    grid: { left: 58, right: 110, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'value', name: '基准周收益(%)', scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
    yAxis: { type: 'value', name: '策略周收益(%)', scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
    series: sc.map((s, i) => ({
      name: s.name, type: 'scatter', data: s.data, symbolSize: 8,
      itemStyle: { color: s.color, opacity: 0.78 },
      markLine: i === 0 ? {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#4b5563', width: 1.2 },
        data: [{ xAxis: 0 }, { yAxis: 0 }],
      } : undefined,
    })),
    graphic: [
      { type: 'text', left: 60, top: 46, style: { text: `危机Alpha\n（股跌+${shortName}涨）`, fill: '#d6403a', fontSize: 12 } },
      { type: 'text', right: 150, top: 46, style: { text: '同涨', fill: '#b8860b', fontSize: 12 } },
      { type: 'text', left: 60, bottom: 50, style: { text: '同跌', fill: '#2b6cb0', fontSize: 12 } },
      { type: 'text', right: 130, bottom: 50, style: { text: `股涨+${shortName}跌`, fill: '#1f6e4f', fontSize: 12 } },
    ],
  })
}

const caNavEl = ref<HTMLDivElement | null>(null)
const caPieEl = ref<HTMLDivElement | null>(null)
const caTopEl = ref<HTMLDivElement | null>(null)
const caScatterEl = ref<HTMLDivElement | null>(null)
const caMultiEl = ref<HTMLDivElement | null>(null)
const caMonthEl = ref<HTMLDivElement | null>(null)
const caCorrEl = ref<HTMLDivElement | null>(null)
const caAnnualEl = ref<HTMLDivElement | null>(null)
const caDimEl = ref<HTMLDivElement | null>(null)
let caNavChart: echarts.ECharts | null = null
let caPieChart: echarts.ECharts | null = null
let caTopChart: echarts.ECharts | null = null
let caScatterChart: echarts.ECharts | null = null
let caMultiChart: echarts.ECharts | null = null
let caMonthChart: echarts.ECharts | null = null
let caCorrChart: echarts.ECharts | null = null
let caAnnualChart: echarts.ECharts | null = null
let caDimChart: echarts.ECharts | null = null

function initCaMultiChart() {
  if (!caMultiEl.value || !caMultiSeries.value) return
  const m = caMultiSeries.value
  caMultiChart?.dispose()
  caMultiChart = echarts.init(caMultiEl.value)
  caMultiChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 52, right: 24, bottom: 60, top: 30, containLabel: true },
    xAxis: { type: 'category', data: m.labels, axisLabel: { rotate: 20, fontSize: 11 } },
    yAxis: { type: 'value', name: '危机Alpha(%)', max: 112 },
    series: [{
      type: 'bar', barMaxWidth: 40,
      data: m.values.map((v, i) => ({ value: v, itemStyle: { color: m.colors[i] } })),
      label: { show: true, position: 'top', formatter: (x: any) => `${Number(x.value).toFixed(1)}%` },
      markLine: {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#1f3a68', type: 'dashed', width: 1.4 },
        data: [{ yAxis: m.main }],
      },
    }],
  })
}

function initCaMonthChart() {
  if (!caMonthEl.value || !caMonthSeries.value) return
  const m = caMonthSeries.value
  caMonthChart?.dispose()
  caMonthChart = echarts.init(caMonthEl.value)
  caMonthChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['基准月均', '策略月均'], top: 0 },
    grid: { left: 52, right: 24, bottom: 40, top: 40, containLabel: true },
    xAxis: { type: 'category', data: m.labels },
    yAxis: { type: 'value', name: '月均收益(%)' },
    series: [
      { name: '基准月均', type: 'bar', data: m.b, barMaxWidth: 26, itemStyle: { color: '#9db3d2' },
        label: { show: true, position: 'top', fontSize: 11, formatter: (x: any) => Number(x.value).toFixed(1) },
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#6b7280', width: 1 },
          data: [{ yAxis: 0 }],
        } },
      { name: '策略月均', type: 'bar', data: m.s, barMaxWidth: 26, itemStyle: { color: '#d6403a' },
        label: { show: true, position: 'top', fontSize: 11, formatter: (x: any) => Number(x.value).toFixed(2) } },
    ],
  })
}

function initCaCorrChart() {
  if (!caCorrEl.value || !caCorrRows.value) return
  const c = caCorrRows.value
  caCorrChart?.dispose()
  caCorrChart = echarts.init(caCorrEl.value)
  caCorrChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['扩展窗口相关（不足52周）', '滚动52周相关'], top: 0 },
    grid: { left: 52, right: 24, bottom: 36, top: 40, containLabel: true },
    xAxis: { type: 'category', data: c.dates, boundaryGap: false },
    yAxis: { type: 'value', name: '相关系数', min: -1.05, max: 1.05 },
    series: [
      { name: '扩展窗口相关（不足52周）', type: 'line', data: c.earlyData, symbol: 'none', connectNulls: true,
        lineStyle: { color: '#9ca3af', width: 1.4, type: 'dashed' } },
      { name: '滚动52周相关', type: 'line', data: c.rcData, symbol: 'none', connectNulls: true,
        lineStyle: { color: '#1f3a68', width: 2 }, areaStyle: { color: 'rgba(31,58,104,0.10)' },
        markLine: {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#6b7280', type: 'dashed' },
          data: [{ yAxis: 0 }],
        } },
    ],
  })
}

function initCaAnnualChart() {
  if (!caAnnualEl.value || !caAnnualSeries.value) return
  const a = caAnnualSeries.value
  caAnnualChart?.dispose()
  caAnnualChart = echarts.init(caAnnualEl.value)
  caAnnualChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 52, right: 24, bottom: 30, top: 30, containLabel: true },
    xAxis: { type: 'category', data: a.labels },
    yAxis: { type: 'value', name: '危机Alpha(%)', max: 112 },
    series: [{
      type: 'bar', barMaxWidth: 40,
      data: a.values.map((v, i) => ({ value: v, itemStyle: { color: a.colors[i] } })),
      label: { show: true, position: 'top', formatter: (x: any) => Number(x.value).toFixed(0) },
      markLine: {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#1f3a68', type: 'dashed' },
        data: [{ yAxis: a.main }],
      },
    }],
  })
}

function initCaDimChart() {
  if (!caDimEl.value || !caDimSeries.value) return
  const m = caDimSeries.value
  caDimChart?.dispose()
  caDimChart = echarts.init(caDimEl.value)
  caDimChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps: any) => `${ps[0].name}<br/>得分 ${m.raw[ps[0].dataIndex].toFixed(1)}` },
    grid: { left: 104, right: 40, bottom: 30, top: 20, containLabel: true },
    xAxis: { type: 'value', max: 105, splitNumber: 5 },
    yAxis: { type: 'category', data: m.names, inverse: true, axisTick: { show: false } },
    series: [
      {
        type: 'bar', barMaxWidth: 22,
        data: m.values.map((v, i) => ({ value: v, itemStyle: { color: m.colors[i] } })),
        label: { show: true, position: 'right', formatter: (x: any) => m.raw[x.dataIndex].toFixed(0) },
      },
      {
        type: 'bar', barMaxWidth: 22, barGap: '-100%', silent: true,
        tooltip: { show: false },
        data: m.values.map((v, i) => ({
          value: v,
          itemStyle: { color: 'transparent' },
          label: {
            show: true,
            position: m.raw[i] >= 10 ? 'insideLeft' : 'right',
            color: m.raw[i] >= 10 ? 'rgba(255,255,255,0.85)' : '#6b7280',
            fontSize: 12,
            formatter: m.types[i],
          },
        })),
      },
    ],
  })
}

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
  const today = new Date()
  const fmt = (d: Date) => {
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${d.getFullYear()}-${m}-${day}`
  }
  form.value = {
    portfolio_strategy_id: portfolios.value[0]?.id || 0,
    name: '',
    start_date: '2021-01-01',
    end_date: fmt(today),
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
  selectedKey.value = null
  caCollapsed.value = true
  // 等 DOM 渲染后初始化图表
  await nextTick()
  initCharts()
}

async function expandCaSection() {
  if (!caCollapsed.value) return
  caCollapsed.value = false
  await nextTick()
  initCrisisAlphaCharts()
}

async function toggleCaSection() {
  caCollapsed.value = !caCollapsed.value
  if (!caCollapsed.value) {
    await nextTick()
    initCrisisAlphaCharts()
  }
}

function scrollToSection(anchor: string) {
  if (anchor === 'crisis-alpha') {
    void expandCaSection().then(() => {
      crisisAlphaSectionEl.value?.scrollIntoView({ behavior: 'smooth' })
    })
  }
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
// A 股红涨绿跌：正收益→红、负收益→绿
function valueClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return Number(v) >= 0 ? 'text-red' : 'text-green'
}

function pct1(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(1)}%`
}

function pctSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(digits)}%`
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

// 整体表现 8 渐变卡
const overallStats = computed<Array<{ label: string; value: string; anchor?: string }>>(() => {
  const e = detail.value?.evaluations
  if (!e) return []
  const cd = ca.value
  return [
    { label: '总收益', value: pct(e.total_return) },
    { label: '年化收益', value: pct(e.annual_return) },
    { label: '最大回撤', value: pct(e.max_drawdown) },
    { label: '年化波动率', value: pct(e.volatility) },
    { label: '夏普比率', value: num(e.sharpe_ratio, 2) },
    { label: '卡玛比率', value: num(e.calmar_ratio, 2) },
    {
      label: '危机Alpha占比',
      value: cd ? `${pct1(cd.quadrant.alpha_ratio)} · ${cd.alpha_rating}` : '—',
      anchor: 'crisis-alpha',
    },
    {
      label: '综合危机保护评分',
      value: cd ? `${num(cd.score.total_score, 1)} · ${cd.score.rating}` : '—',
      anchor: 'crisis-alpha',
    },
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
    // 用百分比表示回撤（负值），round 到 4 位小数
    return peak > 0 ? Number((-(peak - v) / peak * 100).toFixed(4)) : 0
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

// 超额收益 = 组合累计收益率% − 基准累计收益率%（逐点）；基准 null 处超额也为 null
const excessCurve = computed<Array<number | null>>(() => {
  const p = portfolioReturnPct.value
  const b = benchmarkCurve.value
  return p.map((pv, i) => {
    const bv = b[i]
    if (bv === null || bv === undefined) return null
    return Number((pv - bv).toFixed(4))
  })
})

// ===== 收益分布（月度热力图 / 年度箱线图 / 年收益柱状图）=====
const returnsBuckets = computed(() => detail.value?.returns_buckets ?? null)
const hasReturnsBuckets = computed(() => {
  const rb = returnsBuckets.value
  return rb && rb.monthly_matrix.years.length > 0 && rb.yearly.length > 0
})

// ECharts boxplot 五数摘要计算（min / Q1 / median / Q3 / max）
function boxplotData(arr: number[]): [number, number, number, number, number] | null {
  if (arr.length < 5) return null
  const sorted = [...arr].sort((a, b) => a - b)
  const q = (p: number) => {
    const idx = (sorted.length - 1) * p
    const lo = Math.floor(idx)
    const hi = Math.ceil(idx)
    return lo === hi ? sorted[lo] : sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo)
  }
  return [sorted[0], q(0.25), q(0.5), q(0.75), sorted[sorted.length - 1]]
}

// ===== 交易明细分页 =====
const totalPages = computed(() => Math.max(1, Math.ceil((detail.value?.trades || []).length / pageSize)))
const paginatedTrades = computed(() => {
  const trades = detail.value?.trades || []
  const start = (currentPage.value - 1) * pageSize
  return trades.slice(start, start + pageSize)
})

// ===== 风控与拦截统计（决策闸门，调参可观测性）=====
// 闸门阈值/实际值：比率类（0<|v|<1）转百分比，数量类（max_positions 等）原样显示。
function fmtGateVal(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  if (n !== 0 && Math.abs(n) < 1) return `${(n * 100).toFixed(2)}%`
  return String(Math.round(n * 10000) / 10000)
}
// 请求/实发量对：相等表示未拦截；final=0 整单被拦；否则缩量。
function qtyPair(req: number | null | undefined, fin: number | null | undefined): string {
  if (req === null || req === undefined) return '—'
  if (fin === null || fin === undefined) return `${req}（全拦）`
  if (Number(fin) === Number(req)) return `${req}`
  if (Number(fin) === 0) return `${req}（全拦）`
  return `${req} → ${fin}`
}
function fmtBarTime(t: string | null | undefined): string {
  return t ? t.replace('T', ' ').slice(0, 16) : '—'
}
function layerLabel(l: string): string { return LAYER_LABEL[l] || l || '—' }
function actionLabel(a: string): string { return ACTION_LABEL[a] || a || '—' }

// 聚合表按触发次数降序，高频闸门（最该调的参数）排最前。
const decisionSummary = computed<DecisionSummaryItem[]>(() =>
  [...(detail.value?.decision_summary || [])].sort((a, b) => b.count - a.count))

// 下钻：点选某行（gate + param_name 唯一）展开该闸门逐事件明细。
const selectedKey = ref<string | null>(null)
function gateKey(s: DecisionSummaryItem): string { return `${s.gate}::${s.param_name || ''}` }
function toggleGate(s: DecisionSummaryItem) {
  const k = gateKey(s)
  selectedKey.value = selectedKey.value === k ? null : k
}
const selectedSummary = computed<DecisionSummaryItem | null>(() =>
  decisionSummary.value.find((s) => gateKey(s) === selectedKey.value) || null)
const selectedEvents = computed<DecisionEventItem[]>(() => {
  const sel = selectedSummary.value
  if (!sel) return []
  return (detail.value?.decisions || []).filter(
    (e) => e.gate === sel.gate && (e.param_name || '') === (sel.param_name || ''))
})

// ===== echarts 初始化 =====
function initCharts() {
  initEquityChart()
  initDrawdownChart()
  initMonthlyHeatChart()
  initDailyBoxChart()
  initMonthlyBoxChart()
  initAnnualBarChart()
  initCrisisAlphaCharts()
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
  // 超额收益曲线（金色，区别组合紫/策略自动色/基准灰虚线）；无基准数据则不画
  if (hasBenchmark.value) {
    series.push({
      name: '超额收益（相对基准）', type: 'line', data: excessCurve.value, smooth: true,
      symbol: 'none',
      lineStyle: { width: 1.5, color: '#f59e0b' },
      itemStyle: { color: '#f59e0b' },
    })
  }

  const legendData = ['组合', ...strategyCurves.value.map((s) => s.name)]
  if (hasBenchmark.value) {
    legendData.push('基准')
    legendData.push('超额收益（相对基准）')
  }

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
    tooltip: { trigger: 'axis', formatter: (p: any) => `${p[0].axisValue}<br />回撤: ${Number(p[0].value).toFixed(4)}%` },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '10%', containLabel: true },
    xAxis: { type: 'category', data: equityDates.value, boundaryGap: false },
    yAxis: { type: 'value', name: '回撤(%)', max: 0 },
    series: [{
      name: '回撤', type: 'line', data: drawdownCurve.value, smooth: true,
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(16, 185, 129, 0.3)' },
          { offset: 1, color: 'rgba(16, 185, 129, 0.05)' },
        ]),
      },
      itemStyle: { color: '#10b981' }, // A股红涨绿跌：回撤=跌幅→绿
    }],
  })
}

function initMonthlyHeatChart() {
  if (!monthlyHeatEl.value || !hasReturnsBuckets.value) return
  monthlyHeatChart?.dispose()
  monthlyHeatChart = echarts.init(monthlyHeatEl.value)
  const { years, months, matrix, text } = returnsBuckets.value!.monthly_matrix
  // 展开为 [rowIdx, colIdx, value] 三元组，跳过 null
  const data: [number, number, number][] = []
  matrix.forEach((row, ri) => {
    row.forEach((val, ci) => {
      if (val !== null) data.push([ci, ri, val])
    })
  })
  const absMax = Math.max(...data.map((d) => Math.abs(d[2])), 5)
  monthlyHeatChart.setOption({
    tooltip: {
      formatter: (params: any) => {
        const [ci, ri] = params.data
        return `${years[ri]}年${months[ci]}<br/>收益率: ${text[ri][ci]}`
      },
    },
    grid: { left: 60, right: 80, top: 30, bottom: 30 },
    xAxis: {
      type: 'category', data: months,
      axisLabel: { fontSize: 11 },
      splitArea: { show: true },
    },
    yAxis: {
      type: 'category', data: years.map(String),
      inverse: true,
      axisLabel: { fontSize: 11 },
    },
    visualMap: {
      min: -absMax, max: absMax,
      calculable: true,
      orient: 'vertical', right: 0, top: 'center',
      // A 股红涨绿跌：负收益（min）绿 → 零（中）黄 → 正收益（max）红
      inRange: { color: ['#10b981', '#fef3c7', '#ef4444'] },
      text: ['+', '-'],
      textStyle: { fontSize: 11 },
    },
    series: [{
      type: 'heatmap', data,
      label: {
        show: true,
        formatter: (params: any) => {
          const [ci, ri] = params.data
          return text[ri]?.[ci] || ''
        },
        fontSize: 10,
      },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.3)' } },
    }],
  })
}

function initDailyBoxChart() {
  if (!dailyBoxEl.value || !hasReturnsBuckets.value) return
  dailyBoxChart?.dispose()
  dailyBoxChart = echarts.init(dailyBoxEl.value)
  const { yearly } = returnsBuckets.value!
  const categories = yearly.map((y) => String(y.year))
  const boxData = yearly
    .map((y) => y.daily_returns.map((r) => Number((r * 100).toFixed(4))))
    .map(boxplotData)
    .filter(Boolean)
  if (boxData.length === 0) return
  dailyBoxChart.setOption({
    tooltip: {
      trigger: 'item',
      formatter: (p: any) => {
        const [min, q1, med, q3, max] = p.value
        return `${categories[p.dataIndex]}年<br/>最小: ${min.toFixed(4)}%<br/>Q1: ${q1.toFixed(4)}%<br/>中位: ${med.toFixed(4)}%<br/>Q3: ${q3.toFixed(4)}%<br/>最大: ${max.toFixed(4)}%`
      },
    },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value', name: '日收益率(%)' },
    series: [{
      type: 'boxplot', data: boxData,
      itemStyle: { color: '#dbeafe', borderColor: '#3b82f6' },
    }],
  })
}

function initMonthlyBoxChart() {
  if (!monthlyBoxEl.value || !hasReturnsBuckets.value) return
  monthlyBoxChart?.dispose()
  monthlyBoxChart = echarts.init(monthlyBoxEl.value)
  const { yearly } = returnsBuckets.value!
  const categories = yearly.map((y) => String(y.year))
  const boxData = yearly
    .map((y) => y.monthly_returns.map((r) => Number((r * 100).toFixed(4))))
    .map(boxplotData)
    .filter(Boolean)
  if (boxData.length === 0) return
  monthlyBoxChart.setOption({
    tooltip: {
      trigger: 'item',
      formatter: (p: any) => {
        const [min, q1, med, q3, max] = p.value
        return `${categories[p.dataIndex]}年<br/>最小: ${min.toFixed(4)}%<br/>Q1: ${q1.toFixed(4)}%<br/>中位: ${med.toFixed(4)}%<br/>Q3: ${q3.toFixed(4)}%<br/>最大: ${max.toFixed(4)}%`
      },
    },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value', name: '月收益率(%)' },
    series: [{
      type: 'boxplot', data: boxData,
      itemStyle: { color: '#d1fae5', borderColor: '#10b981' },
    }],
  })
}

function initAnnualBarChart() {
  if (!annualBarEl.value || !hasReturnsBuckets.value) return
  annualBarChart?.dispose()
  annualBarChart = echarts.init(annualBarEl.value)
  const { yearly } = returnsBuckets.value!
  const data = yearly.map((y) => {
    const ar = y.annual_return ?? 0
    return {
      value: +(ar * 100).toFixed(2),
      name: String(y.year),
      // A 股红涨绿跌：正收益红、负收益绿
      itemStyle: { color: ar >= 0 ? '#ef4444' : '#10b981' },
    }
  })
  annualBarChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: yearly.map((y) => String(y.year)) },
    yAxis: { type: 'value', name: '年收益率(%)' },
    series: [{
      type: 'bar', data,
      label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11 },
      barMaxWidth: 40,
    }],
  })
}

function handleResize() {
  equityChart?.resize()
  drawdownChart?.resize()
  monthlyHeatChart?.resize()
  dailyBoxChart?.resize()
  monthlyBoxChart?.resize()
  annualBarChart?.resize()
  caNavChart?.resize()
  caPieChart?.resize()
  caTopChart?.resize()
  caScatterChart?.resize()
  caMultiChart?.resize()
  caMonthChart?.resize()
  caCorrChart?.resize()
  caAnnualChart?.resize()
  caDimChart?.resize()
}

onMounted(() => {
  load()
  window.addEventListener('resize', handleResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  equityChart?.dispose()
  drawdownChart?.dispose()
  monthlyHeatChart?.dispose()
  dailyBoxChart?.dispose()
  monthlyBoxChart?.dispose()
  annualBarChart?.dispose()
  caNavChart?.dispose()
  caPieChart?.dispose()
  caTopChart?.dispose()
  caScatterChart?.dispose()
  caMultiChart?.dispose()
  caMonthChart?.dispose()
  caCorrChart?.dispose()
  caAnnualChart?.dispose()
  caDimChart?.dispose()
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
      <div v-for="s in overallStats" :key="s.label" class="stat-card"
           :class="{ clickable: !!s.anchor }"
           @click="s.anchor && scrollToSection(s.anchor)">
        <div class="value">{{ s.value }}</div>
        <div class="label">{{ s.label }}</div>
      </div>
    </div>

    <div ref="crisisAlphaSectionEl" class="ca-section">
      <div class="section-title ca-header" @click="toggleCaSection">
        <span>🌪️ 危机Alpha 专题分析</span>
        <span class="ca-caret" :class="{ open: !caCollapsed }">▶</span>
      </div>

      <template v-if="!caCollapsed">
      <div v-if="caStatus === null" class="card" style="padding:12px">
        <p style="color:#6b7280;margin:0 0 8px">本次回测未包含危机Alpha分析。</p>
        <button class="btn btn-sm btn-primary" :disabled="recomputing" @click="recomputeCa">
          {{ recomputing ? '计算中…' : '重新计算' }}
        </button>
        <span v-if="recomputeMsg" :class="recomputeMsgClass">{{ recomputeMsg }}</span>
      </div>

      <div v-else-if="caStatus === 'failed'" class="card" style="padding:12px">
        <p style="color:#c0392b;margin:0 0 8px">危机Alpha分析失败：{{ crisisAlpha?.error || '未知原因' }}</p>
        <button class="btn btn-sm btn-primary" :disabled="recomputing" @click="recomputeCa">
          {{ recomputing ? '计算中…' : '重新计算' }}
        </button>
        <span v-if="recomputeMsg" :class="recomputeMsgClass">{{ recomputeMsg }}</span>
      </div>

      <div v-else-if="caStatus === 'insufficient'" class="card" style="padding:12px">
        <p style="color:#6b7280;margin:0">样本不足：{{ crisisAlpha?.error || '共同交易日不足 30 天' }}</p>
      </div>

      <template v-else>
        <div class="ca-banner">
          <div class="t">{{ ca?.strategy_name }} · 危机Alpha专项分析</div>
          <div class="d">
            {{ ca?.strategy_name }} 在 A 股宽基系统性下跌阶段的对冲能力评估
          </div>
          <div class="d">
            数据区间 {{ ca?.performance.start }} ~ {{ ca?.performance.end }} ｜ 主基准：{{ ca?.benchmark_name }}
            ｜ 周频 {{ ca?.n_weeks }} 个收益周 ｜ 下跌 {{ ca?.quadrant.n_down }} 周
          </div>
        </div>

        <div class="ca-kpi-grid">
          <div v-for="k in caKpis" :key="k.label" class="ca-kpi">
            <div class="v" :style="{ color: k.color }">{{ k.value }}</div>
            <div class="l">{{ k.label }}</div>
            <div class="s">{{ k.sub }}</div>
          </div>
        </div>

        <div class="ca-status">
          <span>统计口径：策略与主基准共同交易日内的周频收益</span>
          <span v-if="caSkipped.length">已跳过基准：{{ caSkipped.join('、') }}</span>
          <span v-if="crisisAlpha?.computed_at">
            计算于 {{ crisisAlpha?.computed_at?.replace('T', ' ').slice(0, 19) }}
          </span>
          <button class="btn btn-sm" :disabled="recomputing" @click="recomputeCa">
            {{ recomputing ? '计算中…' : '重新计算' }}
          </button>
          <span v-if="recomputeMsg" :class="recomputeMsgClass">{{ recomputeMsg }}</span>
        </div>

        <div class="section-title" style="font-size:14px">一、核心结论</div>
        <p class="ca-note">
          在 {{ ca?.quadrant.n_down }} 个{{ ca?.benchmark_name }}下跌周中，{{ ca?.strategy_name }}
          有 <strong>{{ pct1(ca?.quadrant.alpha_ratio) }}</strong> 的周次实现危机对冲（其中
          <strong>{{ pct1(ca?.quadrant.up_rate) }}</strong> 逆势上涨、
          <strong>{{ pct1((ca?.quadrant.counts.跌得少 ?? 0) / Math.max(ca?.quadrant.n_down ?? 1, 1)) }}</strong>
          跌幅不足股票一半），而真正“跟着跌”或“跌更多”的周次合计仅
          <strong>{{ pct1(ca?.quadrant.follow_ratio) }}</strong>。下跌周平均收益
          <strong :class="valueClass(ca?.quadrant.down_avg_s)">{{ pctSigned(ca?.quadrant.down_avg_s) }}</strong>
          （同期{{ ca?.benchmark_name }}平均 {{ pctSigned(ca?.quadrant.down_avg_b) }}），
          与基准整体相关仅 <strong>{{ num(ca?.performance.corr, 3) }}</strong>，
          且在最深的 {{ caEventStat.n }} 次基准回撤中有 {{ caEventStat.hedged }} 次逆势录得正收益。
        </p>
        <div class="ca-tags">
          <span class="ca-tag" style="background:#d6403a">
            危机Alpha {{ pct1(ca?.quadrant.alpha_ratio) }} · {{ ca?.alpha_rating }}
          </span>
          <span class="ca-tag" style="background:#f0a429">
            综合评分 {{ num(ca?.score.total_score, 1) }} · {{ ca?.score.rating }}
          </span>
        </div>

        <div class="section-title" style="font-size:14px">二、产品与基准走势</div>
        <div class="chart-box">
          <div ref="caNavEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">
          淡红色阴影为 {{ ca?.benchmark_name }} 深度回撤区间（自高点回撤超过 5%）；
          危机Alpha各项统计均在策略与基准的共同交易日内计算。
        </p>
        <div class="table-container">
          <table>
            <thead><tr><th>指标</th><th>{{ ca?.strategy_name }}</th><th>{{ ca?.benchmark_name }}</th></tr></thead>
            <tbody>
              <tr v-for="r in caPerfRows" :key="r.指标">
                <td>{{ r.指标 }}</td>
                <td :class="valueClass(0)">{{ r.策略 }}</td>
                <td>{{ r.基准 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="section-title" style="font-size:14px">三、危机Alpha四档分类</div>
        <div class="chart-box">
          <div ref="caPieEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table>
            <thead><tr><th>分类</th><th>周数</th><th>占比</th><th>平均周收益</th></tr></thead>
            <tbody>
              <tr v-for="r in caCatRows" :key="r.分类">
                <td :style="{ color: r.color, fontWeight: 700 }">{{ r.分类 }}</td>
                <td>{{ r.周数 }}</td>
                <td>{{ r.占比 }}</td>
                <td :class="valueClass(r.avg)">{{ r.平均周收益 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="ca-sub">深跌周条件检验（更严苛）</div>
        <div class="table-container">
          <table class="ca-cond-table">
            <thead><tr><th>条件</th><th>周数</th><th>股票平均</th><th>策略平均</th><th>收涨比例</th></tr></thead>
            <tbody>
              <tr v-for="r in caCondRows" :key="r.条件">
                <td>{{ r.条件 }}</td>
                <td>{{ r.周数 }}</td>
                <td class="text-green">{{ r.股票平均 }}</td>
                <td :class="valueClass(r.s_avg)">{{ r.策略平均 }}</td>
                <td>{{ r.收涨比例 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="section-title" style="font-size:14px">四、极端下跌周 TOP{{ caTopStat.n }}</div>
        <div class="chart-box">
          <div ref="caTopEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">
          {{ ca?.benchmark_name }}跌幅最大的 {{ caTopStat.n }} 周中，{{ ca?.strategy_name }}
          有 {{ caTopStat.up }} 周逆势录得正收益，{{ caTopStat.beat }} 周跑赢基准，且整体跌幅远小于股票。
        </p>

        <div class="section-title" style="font-size:14px">五、全周期四象限分布</div>
        <div class="chart-box">
          <div ref="caScatterEl" class="ca-chart"></div>
        </div>
        <p class="ca-note">左上象限（股跌+策略涨）即“危机Alpha”区，是{{ ca?.strategy_name }}点位最密集的区域。</p>

        <div class="section-title" style="font-size:14px">六、深度回撤事件独立性</div>
        <div v-if="caEventRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内未触发基准深度回撤事件。</p></div>
        </div>
        <template v-else>
          <div class="table-container">
            <table class="ca-event-table">
              <thead><tr><th>回撤起点</th><th>回撤低点</th><th>基准</th><th>策略</th><th>超额</th><th>结果</th></tr></thead>
              <tbody>
                <tr v-for="r in caEventRows" :key="r.回撤起点">
                  <td>{{ r.回撤起点 }}</td>
                  <td>{{ r.回撤低点 }}</td>
                  <td class="text-green">{{ r.基准 }}</td>
                  <td :class="valueClass(r.strategy_ret)">{{ r.策略 }}</td>
                  <td :class="valueClass(r.excess)">{{ r.超额 }}</td>
                  <td :style="{ color: r.hedged ? '#d6403a' : '#6b7280', fontWeight: 700 }">{{ r.结果 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="ca-note">
            基准深度回撤（&gt;5%）事件共 {{ caEventStat.n }} 次，{{ ca?.strategy_name }}
            有 {{ caEventStat.hedged }} 次逆势录得正收益，回撤独立性 {{ caEventStat.pct }}%。
          </p>
        </template>

        <div class="section-title" style="font-size:14px">七、多宽基一致性</div>
        <div v-if="!caMultiSeries" class="table-container">
          <div class="empty-state"><p>可用宽基不足 2 个，跳过多宽基一致性检验。</p></div>
        </div>
        <template v-else>
          <div class="chart-box">
            <div ref="caMultiEl" class="ca-chart"></div>
          </div>
          <div class="table-container">
            <table class="ca-multi-table">
              <thead><tr><th>基准</th><th>下跌周数</th><th>危机Alpha</th><th>逆势上涨比例</th></tr></thead>
              <tbody>
                <tr v-for="r in caMultiRows" :key="r.基准">
                  <td>{{ r.基准 }}</td>
                  <td>{{ r.下跌周数 }}</td>
                  <td>{{ r.危机Alpha }}</td>
                  <td>{{ r.逆势上涨比例 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="ca-note">各宽基均在同一时间窗口内统计；深色虚线为主基准的危机Alpha占比。</p>
        </template>

        <div class="section-title" style="font-size:14px">八、月度视角</div>
        <div class="chart-box">
          <div ref="caMonthEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table>
            <thead><tr><th>市场状态</th><th>月数</th><th>股票月均</th><th>策略月均</th><th>正收益比例</th></tr></thead>
            <tbody>
              <tr v-for="r in caMonthRows" :key="r.市场状态">
                <td>{{ r.市场状态 }}</td>
                <td>{{ r.月数 }}</td>
                <td class="text-green">{{ r.股票月均 }}</td>
                <td :class="valueClass(Number(r.策略月均))">{{ r.策略月均 }}</td>
                <td>{{ r.正收益比例 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="ca-sub">大跌月明细（股票月跌 &gt;5%）</div>
        <div v-if="caBigDownRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内无大跌月。</p></div>
        </div>
        <div v-else class="table-container">
          <table class="ca-bigdown-table">
            <thead><tr><th>月份</th><th>基准</th><th>策略</th></tr></thead>
            <tbody>
              <tr v-for="r in caBigDownRows" :key="r.月份">
                <td>{{ r.月份 }}</td>
                <td class="text-green">{{ r.基准 }}</td>
                <td :class="valueClass(Number(r.策略))">{{ r.策略 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="ca-note">下跌月份共 {{ pct1(ca?.monthly.hedge_ratio) }} 实现危机对冲（月度口径）。</p>

        <div class="section-title" style="font-size:14px">九、动态相关性与年度趋势</div>
        <div v-if="!caCorrRows" class="table-container">
          <div class="empty-state"><p>周收益样本不足 52 周，无法计算滚动相关性。</p></div>
        </div>
        <template v-else>
          <div class="chart-box">
            <div ref="caCorrEl" class="ca-chart"></div>
          </div>
          <p class="ca-note">
            实线为 52 周滚动相关系数；数据开头不足 52 周的部分以灰色虚线（扩展窗口相关）补全。0 轴以下表示两类资产反向、具对冲性。
          </p>
        </template>
        <template v-if="caAnnualRows.length">
          <div class="chart-box">
            <div ref="caAnnualEl" class="ca-chart"></div>
          </div>
          <div class="table-container">
            <table>
              <thead><tr><th>年度</th><th>下跌周</th><th>危机Alpha</th><th>策略收益</th><th>基准收益</th></tr></thead>
              <tbody>
                <tr v-for="r in caAnnualRows" :key="r.年度">
                  <td>{{ r.年度 }}</td>
                  <td>{{ r.下跌周 }}</td>
                  <td>{{ r.危机Alpha }}</td>
                  <td :class="valueClass(Number(r.策略收益))">{{ r.策略收益 }}</td>
                  <td :class="valueClass(Number(r.基准收益))">{{ r.基准收益 }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="caAnnualRows.some(r => !r.完整)" class="ca-note">
            注：标 * 的年度为数据区间首尾的非完整年度（有效收益周不足 40 周），指标仅供参考。
          </p>
        </template>

        <div class="section-title" style="font-size:14px">十、代价检验（上涨周表现）</div>
        <div class="table-container">
          <table class="ca-cost-table">
            <thead><tr><th>指标</th><th>数值</th><th>解读</th></tr></thead>
            <tbody>
              <tr v-for="r in caCostRows" :key="r.指标">
                <td>{{ r.指标 }}</td>
                <td>{{ r.数值 }}</td>
                <td style="color:#6b7280">{{ r.解读 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="section-title" style="font-size:14px">十一、综合危机保护评分</div>
        <div class="chart-box">
          <div ref="caDimEl" class="ca-chart"></div>
        </div>
        <div class="table-container">
          <table class="ca-dim-table">
            <thead><tr><th>维度</th><th>类型</th><th>权重</th><th>得分</th><th>评级</th><th>贡献</th></tr></thead>
            <tbody>
              <tr v-for="r in caDimRows" :key="r.维度">
                <td>{{ r.维度 }}</td>
                <td>{{ r.类型 }}</td>
                <td>{{ r.权重 }}</td>
                <td>{{ r.得分 }}</td>
                <td :style="{ color: CRISIS_SCORE_COLORS[r.评级] || '#d6403a', fontWeight: 700 }">{{ r.评级 }}</td>
                <td>{{ r.贡献 }}</td>
              </tr>
              <tr>
                <td style="font-weight:700">综合评分</td>
                <td>—</td>
                <td>100%</td>
                <td>—</td>
                <td :style="{ color: CRISIS_SCORE_COLORS[ca?.score.rating || ''] || '#d6403a', fontWeight: 700 }">
                  {{ ca?.score.rating }}
                </td>
                <td>{{ num(ca?.score.total_score, 1) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="section-title" style="font-size:14px">十二、全部下跌周明细（{{ ca?.quadrant.n_down }} 周）</div>
        <div v-if="caDownRows.length === 0" class="table-container">
          <div class="empty-state"><p>分析区间内无基准下跌周。</p></div>
        </div>
        <div v-else class="table-container">
          <table class="ca-down-table">
            <thead><tr><th>日期</th><th>基准</th><th>策略</th><th>超额</th><th>分类</th></tr></thead>
            <tbody>
              <tr v-for="r in caDownRows" :key="r.日期">
                <td>{{ r.日期 }}</td>
                <td class="text-green">{{ r.基准 }}</td>
                <td :class="valueClass(Number(r.策略))">{{ r.策略 }}</td>
                <td :class="valueClass(Number(r.超额))">{{ r.超额 }}</td>
                <td :style="{ color: r.color, fontWeight: 700 }">{{ r.分类 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      </template>
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

    <!-- 收益分布（月度热力图 / 年度箱线图 / 年收益柱状图） -->
    <template v-if="hasReturnsBuckets">
      <div class="section-title">收益分布</div>

      <div class="section-title" style="font-size:14px">月度收益热力图</div>
      <div class="chart-box">
        <div ref="monthlyHeatEl" class="chart-container" style="min-height:240px"></div>
      </div>

      <div class="section-title" style="font-size:14px">年度日收益箱线图</div>
      <div class="chart-box">
        <div ref="dailyBoxEl" class="chart-container" style="min-height:260px"></div>
      </div>

      <div class="section-title" style="font-size:14px">各年月收益箱线图</div>
      <div class="chart-box">
        <div ref="monthlyBoxEl" class="chart-container" style="min-height:260px"></div>
      </div>

      <div class="section-title" style="font-size:14px">各年年收益柱状图</div>
      <div class="chart-box">
        <div ref="annualBarEl" class="chart-container" style="min-height:260px"></div>
      </div>
    </template>

    <!-- 风控与拦截统计（调参可观测性：信号→成交链路上每个闸门触发/拦截了多少次） -->
    <div class="section-title">
      风控与拦截统计
      <span class="title-hint">点行展开该闸门逐笔明细 · 按触发次数降序</span>
    </div>
    <div v-if="decisionSummary.length === 0" class="table-container">
      <div class="empty-state"><p>本次回测无闸门拦截/触发记录</p></div>
    </div>
    <template v-else>
      <div class="table-container">
        <table class="decision-table">
          <thead>
            <tr>
              <th>闸门</th><th>层</th><th>动作</th><th>参数</th><th>阈值</th>
              <th>触发次数</th><th>首次</th><th>末次</th><th>涉及股票</th><th>请求/实发(股)</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in decisionSummary" :key="gateKey(s)"
                class="decision-row" :class="{ active: gateKey(s) === selectedKey }"
                @click="toggleGate(s)">
              <td>
                {{ gateLabel(s.gate) }}
                <span class="gate-code">{{ s.gate }}</span>
              </td>
              <td>{{ layerLabel(s.layer) }}</td>
              <td>{{ actionLabel(s.action) }}</td>
              <td class="param-name">{{ s.param_name || '—' }}</td>
              <td>{{ fmtGateVal(s.param_value) }}</td>
              <td class="num strong">{{ s.count }}</td>
              <td class="time">{{ fmtBarTime(s.first_bar_time) }}</td>
              <td class="time">{{ fmtBarTime(s.last_bar_time) }}</td>
              <td class="num">{{ s.stock_count }}</td>
              <td class="num">{{ qtyPair(s.requested_qty_sum, s.final_qty_sum) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 下钻：选中闸门的逐笔事件 -->
      <div v-if="selectedSummary" class="table-container drill-box">
        <div class="drill-head">
          「{{ gateLabel(selectedSummary.gate) }}」明细 · 共 {{ selectedEvents.length }} 条
          <span class="drill-close" @click="selectedKey = null">收起 ✕</span>
        </div>
        <table class="trade-table">
          <thead>
            <tr>
              <th>时间</th><th>策略</th><th>代码</th><th>阈值</th><th>实际</th>
              <th>请求/实发</th><th>原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in selectedEvents" :key="e.id">
              <td class="time">{{ fmtBarTime(e.bar_time) }}</td>
              <td>{{ e.strategy_id ? `#${e.strategy_id}` : '组合' }}</td>
              <td>{{ e.stock_code || '—' }}</td>
              <td>{{ fmtGateVal(e.param_value) }}</td>
              <td>{{ fmtGateVal(e.actual_value) }}</td>
              <td class="num">{{ qtyPair(e.requested_qty, e.final_qty) }}</td>
              <td class="msg">{{ e.message || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

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
.summary-card.positive { border-left-color: #ef4444; } /* 正收益 → 红（A股红涨绿跌） */
.summary-card.danger { border-left-color: #ef4444; }   /* 最大回撤 → 风险警示红，保持不变 */

/* 整体表现 8 渐变卡 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
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
.stat-card.clickable { cursor: pointer; }

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

/* 风控与拦截统计 */
.title-hint {
  font-size: 12px;
  font-weight: 400;
  color: #9ca3af;
  margin-left: 6px;
}
.decision-table {
  width: 100%;
  border-collapse: collapse;
}
.decision-table th {
  padding: 10px 12px;
  text-align: left;
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: #fff;
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
}
.decision-table td {
  padding: 9px 12px;
  border-bottom: 1px solid #e9ecef;
  font-size: 12px;
  white-space: nowrap;
}
.decision-row { cursor: pointer; }
.decision-row:hover { background: #eef2ff; }
.decision-row.active { background: #e0e7ff; }
.decision-row.active td { font-weight: 600; }
.gate-code {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 4px;
  background: #f0f1f3;
  color: #999;
  font-size: 11px;
  font-weight: 400;
}
.param-name { color: #667eea; font-family: monospace; }
.decision-table .num, .trade-table .num { text-align: right; }
.strong { font-weight: 700; color: #ef4444; }
.decision-table .time, .trade-table .time { color: #888; font-size: 11px; }
.trade-table .msg { white-space: normal; color: #555; min-width: 180px; }
.drill-box { margin-top: 12px; border: 2px solid #667eea; }
.drill-head {
  font-size: 13px;
  font-weight: 600;
  color: #4338ca;
  margin-bottom: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.drill-close { cursor: pointer; color: #9ca3af; font-weight: 400; font-size: 12px; }
.drill-close:hover { color: #ef4444; }

.ca-section { scroll-margin-top: 16px; }
.ca-header { cursor: pointer; user-select: none; justify-content: space-between; }
.ca-header:hover { color: #4f46e5; }
.ca-caret {
  font-size: 11px;
  color: #6b7280;
  transition: transform .2s ease;
  margin-left: 8px;
}
.ca-caret.open { transform: rotate(90deg); }
.ca-banner {
  background: #1f3a68;
  border-radius: 12px;
  padding: 18px 24px;
  color: #fff;
  margin-bottom: 6px;
}
.ca-banner .t { font-size: 20px; font-weight: 800; }
.ca-banner .d { font-size: 12px; opacity: .85; margin-top: 6px; }
.ca-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin: 14px 0 10px;
}
.ca-kpi {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 16px 10px;
  text-align: center;
}
.ca-kpi .v { font-size: 26px; font-weight: 800; }
.ca-kpi .l { font-size: 13px; color: #6b7280; margin-top: 6px; }
.ca-kpi .s { font-size: 11px; color: #9ca3af; margin-top: 4px; }
.ca-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 10px;
}
.ca-msg { font-size: 12px; color: #1f3a68; }
.ca-warn { color: #c0392b; font-weight: 700; }
.ca-sub { font-size: 14px; font-weight: 700; color: #333; margin: 14px 0 8px; }
.ca-note { font-size: 12px; color: #6b7280; margin: 6px 0 10px; }
.ca-tags { margin: 10px 0; }
.ca-tag {
  display: inline-block;
  border-radius: 14px;
  padding: 5px 14px;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  margin-right: 8px;
}
.ca-chart { width: 100%; min-height: 400px; }

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
  .ca-kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .summary-section { grid-template-columns: 1fr; }
  .risk-metrics { grid-template-columns: 1fr; }
  .ca-kpi-grid { grid-template-columns: 1fr; }
}
</style>
