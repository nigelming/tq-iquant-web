<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { formatEvent, nextEventId, EVENT_TYPE_COLOR, tradeTypeLabel, ORDER_STATUS_LABEL, gateLabel, LAYER_LABEL, ACTION_LABEL, type LiveEvent } from '../utils/liveEvents'
import {
  orderHistoryToRows, upsertOrderRow,
  tradeEventToRow, tradeHistoryToRows,
  upsertPositionRows, positionHistoryToRows, prependCapped,
  decisionEventToRow, decisionHistoryToRows,
  filterRowsByRange, summarizeDecisionRows, beijingNowHms, TIME_RANGE_OPTIONS,
  positionRowKey,
  type OrderRow, type TradeRow, type PositionRow, type DecisionRow, type TimeRange,
} from '../utils/liveWorkbench'
import {
  getLiveOrders, getLiveTrades, getLivePositions, getLiveDecisions, getLiveSignals, getPortfolios,
  getLiveSessions, getLiveSession, createLiveSession, startLiveSession, stopLiveSession, deleteLiveSession,
  recoverLiveBreaker, getLiveChannel, testLiveChannel, sendUserBlock, getFormulas,
  type PortfolioItem, type LiveSessionItem,
  type LiveChannelInfo, type LiveEngineKey, type LiveExecutionMode, type LiveSignalItem,
  type FormulaItem,
} from '../api'
import PageHeader from '../components/PageHeader.vue'
import { deriveBlockCode } from '../utils/blockCode'

const sessions = ref<LiveSessionItem[]>([])
const loading = ref(true)
// 当前生效的下单通道（全局配置 ths_ui.enabled 决定，切换需重启运行中 session）
const channel = ref<LiveChannelInfo | null>(null)
const channelTesting = ref(false)
const channelTestMsg = ref<{ ok: boolean; text: string } | null>(null)
const portfolios = ref<PortfolioItem[]>([])  // 全量组合策略,供新建实盘多选 + 会话列表解析名称
const formulas = ref<FormulaItem[]>([])  // 公式列表（formula_id → name 映射，用于组合策略标注公式名）
const showCreate = ref(false)
// M2 实盘配置指南弹窗
const showGuide = ref(false)
// 默认与后端 create_session 对齐：新会话默认 alert（半自动预警，仅 TQ 行情+send_warn，不下单）
// 组合策略多选时标注公式名称——外部信号注入需按公式名建立一一对应关系
function portfolioFormulas(pid: number): string {
  const p = portfolios.value.find(x => x.id === pid)
  if (!p || !(p as any).strategies) return ''
  const names = ((p as any).strategies as { formula_id?: number }[])
    .map((s) => {
      const f = formulas.value.find(f => f.id === s.formula_id)
      return f ? f.name : null
    })
    .filter(Boolean)
  return [...new Set(names)].join(' + ')
}

const form = ref({
  name: '', mode: 'simulation',
  engine: 'tdx' as LiveEngineKey,
  execution_mode: 'alert' as LiveExecutionMode,
  portfolio_ids: [] as number[],
  // 实盘资金基准（元）：null=用组合初始资金；number input v-model 空输入为 ''，提交前归一化
  live_capital: null as number | string | null,
  // 外部信号注入：通达信条件预警输出的信号文件路径（空=走 TQ 公式计算）
  signal_file_path: '' as string,
})

/** live_capital 归一化：空串/未填 → null（用组合初始资金），否则转数值透传。 */
function normalizedLiveCapital(): number | null {
  const v = form.value.live_capital
  if (v === null || v === undefined || v === '') return null
  return Number(v)
}
// 引擎中文名（列表与弹窗共用；后端 engines 清单里有 label 时优先）
const ENGINE_LABEL: Record<string, string> = {
  goldminer: '掘金量化', iquant: 'QMT 通道', tdx: '通达信', ths: '同花顺',
}
function engineLabel(key?: string): string {
  if (!key) return ENGINE_LABEL.iquant
  return channel.value?.engines?.find((e) => e.key === key)?.label || ENGINE_LABEL[key] || key
}

// 执行模式中文名（alert=预警/auto=自动）；老数据缺省视作 auto
const EXECUTION_MODE_LABEL: Record<string, string> = { alert: '预警', auto: '自动' }
function executionModeLabel(m?: string): string {
  return EXECUTION_MODE_LABEL[m || 'auto'] || m || '自动'
}
function isAlert(s: LiveSessionItem): boolean {
  return (s.execution_mode || 'auto') === 'alert'
}

// 通道条前缀：有运行中会话=运行中通道（跟随该会话引擎）；否则=默认引擎（default_engine）
const runningEngine = ref<LiveEngineKey | null>(null)
const channelLabelPrefix = computed(() => (runningEngine.value ? '运行中通道' : '默认引擎'))
// 引擎清单去掉默认引擎（首徽章已展示，避免重复渲染两个相同徽章）
const otherEngines = computed(
  () => (channel.value?.engines || []).filter((e) => e.key !== channel.value?.engine),
)
const channelBadgeTitle =
  '默认引擎：新建实盘（自动模式）默认使用；可在 系统设置 → 通达信 TQ 交易通道 → 默认交易引擎 修改'
// 新建实盘默认引擎：跟随后端 default_engine（SystemConfig 可配，当前默认 tdx）
function defaultEngineKey(): LiveEngineKey {
  return (channel.value?.engine as LiveEngineKey) || 'tdx'
}

// M1 多会话并行：B6 全局限 1 已移除（后端改 THS 通道互斥），启动按钮恒可点，
// 多会话冲突由后端 409 兜底（同花顺通道键鼠独占）。

// 组合级状态列表：优先用后端 portfolios 字段，缺省时兜底 portfolio_ids(无状态视为 active)
function portfolioStatuses(s: LiveSessionItem): { portfolio_id: number; status: string }[] {
  if (s.portfolios && s.portfolios.length > 0) return s.portfolios
  return (s.portfolio_ids || []).map((id) => ({ portfolio_id: id, status: 'active' }))
}

function portfolioName(id: number): string {
  return portfolios.value.find((p) => p.id === id)?.name || `#${id}`
}

/** 子策略名：从已加载的 portfolios 嵌套 strategies 里解析；查不到兜底 #id。 */
function strategyName(pid: number | null, sid: number | null): string {
  if (sid === null || sid === undefined) return '—'
  const p = portfolios.value.find((x) => x.id === pid)
  const s = p?.strategies.find((y) => y.id === sid)
  return s?.name || `#${sid}`
}

async function recoverBreaker(s: LiveSessionItem, pid: number) {
  if (!confirm(`确定手动恢复组合「${portfolioName(pid)}」的熔断？将清零熔断计数并恢复开仓。`)) return
  try {
    await recoverLiveBreaker(s.id, pid)
    await load()
  } catch (e: any) {
    alert(e?.message || '恢复失败')
  }
}

// ---- B4a: 实时事件日志面板（SSE，多会话并行订阅）----
type EventRow = LiveEvent & { sessionId?: number; sessionLabel?: string }
const events = ref<EventRow[]>([])
const connState = ref<'closed' | 'connecting' | 'open' | 'error'>('closed')
const connLabel: Record<string, string> = {
  closed: '未连接', connecting: '连接中', open: '已连接', error: '连接失败',
}
// 多会话并行：每个运行中会话一条 SSE（事件按来源流标会话前缀）
const esMap = new Map<number, EventSource>()
// 工作台聚焦的会话（持仓/委托/成交跟随它；事件日志不受限）
let activeSessionId: number | null = null
const EVENT_TYPES = ['signal', 'order', 'trade', 'position', 'risk', 'decision'] as const
const LOG_CAP = 200

// ---- B4b: 工作台（持仓/委托/成交/决策拦截/预警信号）----
const positions = ref<PositionRow[]>([])
const orders = ref<OrderRow[]>([])
const trades = ref<TradeRow[]>([])
const decisions = ref<DecisionRow[]>([])
// 时间区间筛选（默认今日；聚合统计随筛选区间重算，不再显示全时段累计）
const orderRange = ref<TimeRange>('today')
const decRange = ref<TimeRange>('today')
const filteredOrders = computed(() => filterRowsByRange(orders.value, orderRange.value))
const filteredDecisions = computed(() => filterRowsByRange(decisions.value, decRange.value))
// 成交转化与决策聚合使用同一筛选区间（decRange），不跟随委托表的 orderRange
const decRangeOrders = computed(() => filterRowsByRange(orders.value, decRange.value))
const decisionSummaryView = computed(
  () => summarizeDecisionRows(filteredDecisions.value, decRangeOrders.value))
const signals = ref<LiveSignalItem[]>([])
const signalsTotal = ref(0)
// 账户全貌（真实账户快照）：多会话并行 → 每个运行中会话一张卡（不同账号分列）
const accountSnapshots = ref<Record<number, any>>({})
// 交易日历来源/退化状态（详情接口 trading_calendar；仅聚焦会话拉取，不额外轮询）
const calendarStatus = ref<Record<number, any>>({})

/** 单年日历摘要：正常=来源/交易日数/覆盖区间；退化=来源+标记。 */
function calendarYearText(year: string, y: any): string {
  if (!y) return year
  if (y.degraded) return `${year}：${y.source || '退化'}（退化）`
  const days = y.trading_days ?? '—'
  const cov = Array.isArray(y.coverage) && y.coverage.length >= 2
    ? `${y.coverage[0]} ~ ${y.coverage[1]}` : '—'
  return `${year}：${y.source || '—'}，${days} 个交易日，覆盖 ${cov}`
}

/** 多年度按年份升序拼接，最多展示最近 2 年（避免撑破会话卡）。 */
function calendarInfoText(cal: any): string {
  if (!cal || !cal.years) return ''
  const years = Object.keys(cal.years).sort().slice(-2)
  return '日历 ' + years.map((y) => calendarYearText(y, cal.years[y])).join('；')
}

/** 退化徽标 title：逐年列出退化来源。 */
function calendarDegradedTitle(cal: any): string {
  if (!cal || !cal.years) return '交易日历已退化'
  const parts = Object.keys(cal.years).sort()
    .filter((y) => cal.years[y]?.degraded)
    .map((y) => calendarYearText(y, cal.years[y]))
  return parts.length > 0 ? parts.join('；') : '交易日历已退化'
}
type WbTab = 'positions' | 'orders' | 'trades' | 'decisions' | 'signals'
const wbTab = ref<WbTab>('positions')
const TAB_LABEL: Record<WbTab, string> = {
  positions: '持仓', orders: '委托', trades: '成交', decisions: '决策/拦截', signals: '预警信号',
}

// 信号类型 / 预警推送状态中文映射
const SIGNAL_TYPE_LABEL: Record<string, string> = {
  OPEN: '开仓', ADD: '加仓', REDUCE: '减仓', CLOSE: '清仓',
}
function signalTypeLabel(t: string): string {
  return SIGNAL_TYPE_LABEL[t] || t
}
const ALERT_STATUS_LABEL: Record<string, string> = {
  sent: '已发送', failed: '发送失败', skipped: '已跳过',
}
function alertStatusLabel(s: string | null): string {
  if (!s) return '—'
  return ALERT_STATUS_LABEL[s] || s
}
function alertStatusClass(s: string | null): string {
  if (s === 'sent') return 'badge-green'
  if (s === 'failed') return 'badge-red'
  return 'badge-gray'
}
function fmtTime(t: string | null): string {
  return t ? t.replace('T', ' ').slice(0, 19) : '—'
}
// 成本价展示统一 3 位小数（后端计算/存储仍全精度）
function fmtCost(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(3)
}
// 金额（市值）展示统一 2 位小数（分为精度；后端计算/存储仍全精度）
function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(2)
}

// ---- 1.3.1 预警信号股同步到通达信自选板块（当前信号列表股票代码去重）----
async function syncSignalsBlock() {
  const codes = Array.from(new Set(signals.value.map((s) => s.stock_code).filter(Boolean)))
  if (codes.length === 0) {
    alert('暂无信号股票')
    return
  }
  const name = window.prompt('通达信自选板块名', '创懿预警')
  if (!name) return
  try {
    const r = await sendUserBlock({ block_code: deriveBlockCode(name), block_name: name, stock_codes: codes, mode: 'replace' })
    alert(`已写入 ${r.count} 只到「${r.block_code}」`)
  } catch (e: any) {
    alert(e?.response?.data?.message || e?.message || '同步失败')
  }
}

// 决策闸门阈值/实际：比率（0<|v|<1）转百分比，数量原样。
function fmtGateVal(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  if (n !== 0 && Math.abs(n) < 1) return `${(n * 100).toFixed(2)}%`
  return String(Math.round(n * 10000) / 10000)
}
// 成交转化率：0~1 → 百分比；未统计（旧数据）→「—」
function fmtRate(r: number | null | undefined): string {
  if (r === null || r === undefined || !Number.isFinite(Number(r))) return '—'
  return `${(Number(r) * 100).toFixed(1)}%`
}
function qtyPair(req: number | null | undefined, fin: number | null | undefined): string {
  if (req === null || req === undefined) return '—'
  if (fin === null || fin === undefined) return `${req}（全拦）`
  if (Number(fin) === Number(req)) return `${req}`
  if (Number(fin) === 0) return `${req}（全拦）`
  return `${req} → ${fin}`
}
const layerLabel = (l: string) => LAYER_LABEL[l] || l || '—'
const actionLabel = (a: string) => ACTION_LABEL[a] || a || '—'

const nowTime = () => beijingNowHms()

function pushLog(type: string, data: Record<string, unknown>, time: string, sessionLabel = '', sessionId?: number) {
  const text = formatEvent(type, data)
  if (!text) return // ping 心跳不进日志
  events.value.push({
    id: nextEventId(),
    type: type as LiveEvent['type'],
    time,
    text: sessionLabel ? `[${sessionLabel}] ${text}` : text,
    sessionLabel,
    sessionId,
  })
  if (events.value.length > LOG_CAP) events.value = events.value.slice(-LOG_CAP)
}

// 多会话事件过滤：全部 / 按会话（运行中会话 >1 时显示下拉）
const eventFilter = ref<number | 'all'>('all')
const runningSessions = computed(() => sessions.value.filter((s) => s.status === 'running'))
const filteredEvents = computed(() => {
  if (eventFilter.value === 'all') return events.value
  return events.value.filter((e) => e.sessionId === eventFilter.value)
})

// 预警信号节流重拉：SSE signal 事件到达后 2s 窗口内合并为一次列表重拉。
// 不直接把 SSE payload 映射成行——payload 是精简形状（signal_id 而非 id），
// 缺 period/formula_name/trigger_value 等列，重拉才完整。
let signalsRefreshTimer: ReturnType<typeof setTimeout> | null = null
function scheduleSignalsRefresh() {
  if (signalsRefreshTimer !== null || activeSessionId === null) return
  signalsRefreshTimer = setTimeout(async () => {
    signalsRefreshTimer = null
    if (activeSessionId === null) return
    // await 期间用户可能已切到别的会话：响应回来必须丢弃，否则旧会话
    // 信号会写进新会话的预警表（与 loadHistory 的 activeSessionId 校验同理）。
    const id = activeSessionId
    try {
      const sig = await getLiveSignals(id, { limit: 200 })
      if (activeSessionId !== id) return
      signals.value = sig.items
      signalsTotal.value = sig.total
    } catch { /* 会话已删除等 → 忽略，与 loadHistory 兜底一致 */ }
  }, 2000)
}

// loadHistory 与 SSE 竞态计数（audit C1）：五请求在途期间到达的 workbench 事件数。
// 只计 position/order/trade——三类历史 resolve 时会整体覆盖 SSE 已插的行；
// decision/signals 不计（signals 走节流重拉，decision 逐事件流可接受毫秒级窗口）。
let workbenchEventsSinceLoad = 0

function applyToWorkbench(type: string, data: Record<string, unknown>, time: string) {
  if (type === 'position') {
    workbenchEventsSinceLoad += 1
    positions.value = upsertPositionRows(positions.value, data)
  }
  else if (type === 'order') {
    workbenchEventsSinceLoad += 1
    orders.value = upsertOrderRow(orders.value, data, time)
  }
  else if (type === 'trade') {
    workbenchEventsSinceLoad += 1
    trades.value = prependCapped(trades.value, tradeEventToRow(data, time))
  }
  else if (type === 'decision') decisions.value = prependCapped(decisions.value, decisionEventToRow(data, time), 500)
  else if (type === 'signal') scheduleSignalsRefresh()
}

async function loadHistory(id: number) {
  workbenchEventsSinceLoad = 0
  try {
    const [pos, ord, trd, dec, sig] = await Promise.all([
      getLivePositions(id), getLiveOrders(id), getLiveTrades(id),
      getLiveDecisions(id).catch(() => null),
      getLiveSignals(id, { limit: 200 }).catch(() => null),
    ])
    // M1 多会话监控切换：五请求在途期间用户已切走（activeSessionId 变更）→
    // 丢弃过期结果，防旧会话历史覆盖新会话工作台。
    if (activeSessionId !== id) return
    positions.value = positionHistoryToRows(pos)
    orders.value = orderHistoryToRows(ord)
    trades.value = tradeHistoryToRows(trd)
    // 决策历史：逐事件（旧库/迁移未应用时端点 404 → 兜底空）；聚合统计前端按筛选区重算
    decisions.value = dec ? decisionHistoryToRows(dec.events) : []
    // 预警信号：alert 模式主产物；auto 模式下也可能有信号落库
    signals.value = sig?.items ?? []
    signalsTotal.value = sig?.total ?? 0
    // 账户全貌快照 + 交易日历来源/退化状态（同一详情请求，采集于 deals 节拍）——失败静默（辅助卡）
    try {
      getLiveSession(id).then(d => {
        if (activeSessionId !== id) return
        accountSnapshots.value = { ...accountSnapshots.value, [id]: d?.account_snapshot ?? null }
        calendarStatus.value = { ...calendarStatus.value, [id]: d?.trading_calendar ?? null }
      }).catch(() => {})
    } catch { /* api 缺失等 → 辅助卡静默 */ }
    // 竞态补拉（audit C1）：五请求在途期间 SSE 已推过 position/order/trade 时，
    // 上面整体覆盖会丢这些事件 → 计数 >0 则补拉一次三表历史兜底（补拉前清零；
    // 补拉期间再进的事件仍会漏，残余窗口毫秒级可接受，不无限递归）。
    if (workbenchEventsSinceLoad > 0) {
      workbenchEventsSinceLoad = 0
      const [pos2, ord2, trd2] = await Promise.all([
        getLivePositions(id), getLiveOrders(id), getLiveTrades(id),
      ])
      // 补拉同样先验会话未切换（同上）
      if (activeSessionId !== id) return
      positions.value = positionHistoryToRows(pos2)
      orders.value = orderHistoryToRows(ord2)
      trades.value = tradeHistoryToRows(trd2)
    }
  } catch {
    // 会话已删除等 → 保持现状
  }
}

function startEventStreams() {
  closeEventStreams()
  const running = sessions.value.filter((s) => s.status === 'running')
  if (running.length === 0) {
    connState.value = 'closed'
    return
  }
  for (const s of running) {
    const id = s.id
    const label = `#${s.id} ${s.name}`
    const source = new EventSource(`/api/live/sessions/${id}/stream`)
    // D-5（audit 前端 I4）：心跳流升级——收到 reconnect 关当前流重接（按流隔离，
    // 旧流迟到的 reconnect 不影响其它会话）。
    source.addEventListener('reconnect', () => {
      const cur = esMap.get(id)
      if (cur === source) {
        cur.close()
        esMap.delete(id)
        openOneStream(s)
        // 心跳流→真流切换间隙可能漏事件：聚焦会话重拉工作台历史兜底
        if (activeSessionId === id) loadHistory(id)
      }
    })
    EVENT_TYPES.forEach((t) => {
      source.addEventListener(t, (e: MessageEvent) => {
        const data = JSON.parse(e.data)
        const time = nowTime()
        pushLog(t, data, time, label, id)
        // 工作台（持仓/委托/成交…）只跟随聚焦会话
        if (id === activeSessionId) applyToWorkbench(t, data, time)
      })
    })
    source.onopen = () => { connState.value = 'open' }
    source.onerror = () => {
      // EventSource 规范：readyState=2(CLOSED) 才是终态不再重连（如后端 404），
      // 0/1 都仍在自动重连中（显示「连接中」而非 closed/未连接）
      connState.value = source.readyState === 2 ? 'error' : 'connecting'
    }
    esMap.set(id, source)
  }
}

function openOneStream(s: { id: number; name: string; status: string }) {
  const id = s.id
  const label = `#${s.id} ${s.name}`
  const source = new EventSource(`/api/live/sessions/${id}/stream`)
  source.addEventListener('reconnect', () => {
    const cur = esMap.get(id)
    if (cur === source) {
      cur.close()
      esMap.delete(id)
      openOneStream(s)
    }
  })
  EVENT_TYPES.forEach((t) => {
    source.addEventListener(t, (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      const time = nowTime()
      pushLog(t, data, time, label, id)
      if (id === activeSessionId) applyToWorkbench(t, data, time)
    })
  })
  source.onopen = () => { connState.value = 'open' }
  source.onerror = () => {
    connState.value = source.readyState === 2 ? 'error' : 'connecting'
  }
  esMap.set(id, source)
}

function closeEventStreams() {
  for (const s of esMap.values()) s.close()
  esMap.clear()
  connState.value = 'closed'
}

/** 切换工作台聚焦会话：清空工作台、拉该会话历史；事件流不动（多会话并行常开）。 */
const watchedSessionId = ref<number | null>(null)

function watchSession(id: number) {
  watchedSessionId.value = id
  activeSessionId = id
  const target = sessions.value.find((s) => s.id === id)
  if (target) {
    runningEngine.value = (target.engine as LiveEngineKey) || null
    loadChannel(target.engine)
  }
  // 清空工作台（事件日志保留——多会话合并流）
  positions.value = []
  orders.value = []
  trades.value = []
  decisions.value = []
  signals.value = []
  signalsTotal.value = 0
  loadHistory(id)
}

async function load() {
  try {
    sessions.value = await getLiveSessions()
  } catch {
    sessions.value = []
  } finally {
    loading.value = false
  }
  portfolios.value = await getPortfolios().catch(() => [])
  formulas.value = await getFormulas().catch(() => [])
  // 多会话并行监控：为所有运行中会话开 SSE（事件日志合并带会话前缀）；
  // 工作台聚焦「监控中」会话（优先续接，无则回退第一个 running）。
  const running = sessions.value.filter((s) => s.status === 'running')
  const target = running.find((s) => s.id === watchedSessionId.value) || running[0] || null
  watchedSessionId.value = target ? target.id : null
  runningEngine.value = (target?.engine as LiveEngineKey) || null
  startEventStreams()
  if (target) {
    activeSessionId = target.id
    loadHistory(target.id)
  } else {
    // 无运行会话 → 断开事件流、工作台聚焦清空
    closeEventStreams()
    activeSessionId = null
  }
  // 通道条跟随监控中 session 的引擎；无运行会话时跟随默认引擎
  loadChannel(target?.engine)
}

function openCreate() {
  form.value = { name: '', mode: 'simulation', engine: defaultEngineKey(), execution_mode: 'alert', portfolio_ids: [], live_capital: null, signal_file_path: '' }
  showCreate.value = true
}

async function create() {
  if (form.value.portfolio_ids.length === 0) {
    alert('请至少选择一个组合策略')
    return
  }
  try {
    await createLiveSession({ ...form.value, live_capital: normalizedLiveCapital() })
    showCreate.value = false
    form.value = { name: '', mode: 'simulation', engine: defaultEngineKey(), execution_mode: 'alert', portfolio_ids: [], live_capital: null, signal_file_path: '' }
    load()
  } catch (e: any) {
    // 后端参数校验失败（名称空/资金≤0 等）返回 code!==0 → 拦截器 reject，
    // 必须提示并保留弹窗，否则点「确认」看起来完全无反应。
    alert(e?.message || '创建失败')
  }
}

async function startSession(id: number) {
  try {
    await startLiveSession(id)
    load() // load 内自动连接新运行 session 的流
  } catch (e: any) {
    // 桥未启动等业务错误：后端返回 code!==0，拦截器 reject 带 message。
    alert(e?.message || '启动失败')
  }
}

async function stopSession(id: number) {
  // 先停后端成功再关流（audit M3）：stop 后端会经流推 canceled 终态事件，
  // 先关流会错过清扫；API 失败则保持流开着（会话实际仍在运行）。
  try {
    await stopLiveSession(id)
    load() // 多会话并行：load 会重开所有 running 流、关停已停会话的流
  } catch (e: any) {
    alert(e?.message || '停止失败')
  }
}

async function deleteSession(s: LiveSessionItem) {
  if (!confirm(`确定删除实盘 session「${s.name}」？该 session 的历史委托/成交将一并清除，且不可恢复。`)) return
  try {
    await deleteLiveSession(s.id)
    load()
  } catch (e: any) {
    alert(e?.message || '删除失败')
  }
}

async function loadChannel(engine?: LiveEngineKey) {
  try {
    channel.value = await getLiveChannel(engine)
  } catch {
    channel.value = null
  }
}

async function testChannel() {
  if (!channel.value) return
  channelTesting.value = true
  channelTestMsg.value = null
  try {
    const r = await testLiveChannel(channel.value.active)
    const label = channel.value.label
    channelTestMsg.value = r.ok
      ? { ok: true, text: `${label} 已连接${r.positions !== undefined ? `，读到 ${r.positions} 条持仓` : r.base_url ? `（${r.base_url}）` : ''}` }
      : { ok: false, text: r.error || '连接失败' }
  } catch (e: any) {
    channelTestMsg.value = { ok: false, text: e?.message || '连接失败' }
  } finally {
    channelTesting.value = false
  }
}

onMounted(() => { load() })
onUnmounted(closeEventStreams)
</script>

<template>
  <PageHeader title="实盘交易">
    <button class="btn btn-sm" @click="showGuide = true" data-field="live-guide-toggle">📖 配置指南</button>
    <button @click="openCreate" class="btn btn-primary">+ 新建实盘</button>
  </PageHeader>

  <div class="card channel-bar">
    <span class="channel-label">{{ channelLabelPrefix }}</span>
    <span class="badge" :class="channel?.online ? 'badge-green' : 'badge-gray'"
      :title="channelBadgeTitle">
      {{ channel?.label || '未知' }}
    </span>
    <span v-for="e in otherEngines" :key="e.key" class="badge"
      :class="e.configured ? 'badge-gray' : 'badge-red'"
      :title="`${e.detail}（自动模式可在新建实盘时另选）`">{{ e.label }}{{ e.configured ? '' : '（未配置）' }}</span>
    <!-- 引擎未运行 → 不做在线判定（此前无引擎时探活只看客户端是否开着，会把
         「引擎未运行」误显示为绿色在线；重启后尤其误导）。 -->
    <span
      v-if="channel?.engine_running === false"
      class="badge badge-gray"
      title="引擎未运行：重启后需重新启动会话，启动后才会实时检测通道连通性"
    >未检测（会话未启动）</span>
    <span
      v-else-if="channel?.online !== null && channel?.online !== undefined"
      class="badge" :class="channel.online ? 'badge-green' : 'badge-red'"
    >{{ channel.online ? '在线' : '离线' }}</span>
    <button class="btn btn-sm" :disabled="channelTesting" @click="testChannel">
      {{ channelTesting ? '测试中…' : '测试连接' }}
    </button>
    <span
      v-if="channelTestMsg"
      :style="{ color: channelTestMsg.ok ? 'var(--green)' : 'var(--red)', fontSize: '13px' }"
    >{{ channelTestMsg.text }}</span>
    <span class="channel-hint">自动模式可在「新建实盘」时另选引擎；默认引擎在 系统设置 → 通达信 TQ 交易通道 修改</span>
    <span class="channel-hint">支持多会话并行（不同引擎/不同账户）；同花顺通道同时仅一个会话；同组合多会话=复制交易（各账户独立记账）</span>
    <router-link to="/system" style="margin-left:auto;font-size:13px">通道配置 →</router-link>
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <div v-else class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>组合策略</th><th>执行模式</th><th>引擎</th><th>模式</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="s in sessions" :key="s.id">
          <td style="color:var(--ink-soft)">#{{ s.id }}</td>
          <td>{{ s.name }}</td>
          <td style="color:var(--ink-soft)">
            <span v-for="p in portfolioStatuses(s)" :key="p.portfolio_id" class="portfolio-chip">
              {{ portfolioName(p.portfolio_id) }}
              <span v-if="p.status === 'circuit_broken'" class="badge badge-red" style="margin-left:4px">熔断</span>
              <button v-if="p.status === 'circuit_broken'" @click="recoverBreaker(s, p.portfolio_id)" class="btn btn-sm" style="margin-left:4px">恢复</button>
            </span>
            <span v-if="portfolioStatuses(s).length === 0">-</span>
          </td>
          <td>
            <span class="badge" :class="isAlert(s) ? 'badge-blue' : 'badge-purple'"
              :title="isAlert(s) ? '半自动预警：仅 TQ 行情 + send_warn 推送预警，不下单' : '全自动：按交易引擎下单'"
            >{{ executionModeLabel(s.execution_mode) }}</span>
          </td>
          <td>
            <span class="badge badge-gray" :title="isAlert(s) ? '预警模式：引擎决定信号计算数据源（TQ 内存注入），不下单' : '全自动：按交易引擎下单'">{{ engineLabel(s.engine) }}</span>
          </td>
          <td>{{ s.mode === 'simulation' ? '仿真' : '实盘' }}</td>
          <td>
            <span class="badge" :class="s.status === 'running' ? 'badge-green' : 'badge-gray'">{{ s.status === 'running' ? '运行中' : '已停止' }}</span>
            <!-- 状态陈旧保护：后端重启后引擎未恢复时，DB 可能仍留 running；以引擎
                 注册表（engine_running）为准提示，避免误以为在自动交易。 -->
            <span v-if="s.status === 'running' && s.engine_running === false"
              class="badge badge-red" style="margin-left:4px"
              title="状态陈旧：后端重启后引擎未恢复，实际未运行；请重新启动会话">引擎未启动</span>
            <!-- 「工作台聚焦」≠「是否被监控」：所有 running 会话都并行订阅 SSE、并行监控；
                 该徽标仅表示工作台当前聚焦展示哪一个会话。 -->
            <span v-if="s.status === 'running' && s.id === watchedSessionId"
              class="badge badge-blue" style="margin-left:4px"
              title="工作台聚焦：仅表示工作台当前聚焦展示该会话；所有运行中会话都在并行监控与订阅">工作台聚焦</span>
            <!-- 监控正常：引擎在册（engine_running=true）才算真的在被监控；后端未返回该字段时不显示，避免误报。 -->
            <span v-if="s.status === 'running' && s.engine_running === true"
              class="badge badge-green" style="margin-left:4px"
              title="监控正常：引擎在册，轮询与风控在线">监控正常</span>
          </td>
          <td>
            <button v-if="s.status === 'stopped'" @click="startSession(s.id)" class="btn btn-sm btn-primary">启动</button>
            <button v-if="s.status === 'running'" @click="stopSession(s.id)" class="btn btn-sm btn-danger">停止</button>
            <button v-if="s.status === 'running' && s.id !== watchedSessionId" @click="watchSession(s.id)" class="btn btn-sm">监控</button>
            <button v-if="s.status === 'stopped'" @click="deleteSession(s)" class="btn btn-sm">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="sessions.length === 0" class="empty-state"><p>暂无实盘 session</p></div>
  </div>

  <!-- B4b: 工作台（持仓/委托/成交） -->
  <div class="card" style="margin-top:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h3 style="margin:0">
        实盘工作台
        <span v-if="watchedSessionId" style="font-size:13px;color:var(--ink-soft);font-weight:normal;margin-left:8px">
          聚焦：#{{ watchedSessionId }} {{ sessions.find((s) => s.id === watchedSessionId)?.name }}
        </span>
      </h3>
      <div style="display:flex;align-items:center;margin-left:auto">
        <select
          v-if="runningSessions.length > 1"
          class="input" style="width:auto;margin-right:8px;padding:4px 8px"
          :value="watchedSessionId ?? ''"
          @change="watchSession(Number(($event.target as HTMLSelectElement).value))"
        >
          <option v-for="s in runningSessions" :key="s.id" :value="s.id">#{{ s.id }} {{ s.name }}（{{ s.engine }}）</option>
        </select>
        <button
          v-for="(label, key) in TAB_LABEL" :key="key"
          class="btn btn-sm" :class="wbTab === key ? 'btn-primary' : ''"
          @click="wbTab = key"
        >{{ label }}</button>
        <button
          v-if="wbTab === 'positions'"
          class="btn btn-sm" style="margin-left:8px"
          :disabled="loading || activeSessionId === null"
          @click="loadHistory(activeSessionId!)"
          title="重新拉取全量持仓（原有持仓服务端缓存 60s）"
        >↻ 刷新持仓</button>
      </div>
    </div>

    <!-- 账户全貌（多会话并行：每个运行中会话一张卡，不同账号分列） -->
    <div v-if="wbTab === 'positions' && runningSessions.length > 0" class="card" style="margin-bottom:12px;padding:10px 14px">
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div
          v-for="s in runningSessions" :key="s.id"
          style="min-width:280px;flex:1;border:1px solid var(--line,#ddd);border-radius:8px;padding:8px 12px"
          :style="s.id === watchedSessionId ? 'border-color:var(--accent,#3b82f6)' : ''"
        >
          <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
            <strong style="margin:0">#{{ s.id }} {{ s.name }}<span style="color:var(--ink-soft);font-weight:normal;margin-left:6px">{{ s.engine }}</span></strong>
            <span
              v-if="s.status === 'running' && calendarStatus[s.id]?.degraded === true"
              class="badge badge-red" data-test="calendar-degraded"
              :title="calendarDegradedTitle(calendarStatus[s.id])"
            >交易日历退化</span>
          </div>
          <p
            v-if="s.status === 'running' && calendarStatus[s.id] && calendarStatus[s.id].degraded !== true"
            data-test="calendar-info"
            style="margin:4px 0 0;color:var(--ink-soft);font-size:12px"
            :title="calendarInfoText(calendarStatus[s.id])"
          >{{ calendarInfoText(calendarStatus[s.id]) }}</p>
          <template v-if="accountSnapshots[s.id]">
            <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:4px">
              <span>总资产 <b>{{ accountSnapshots[s.id].total_asset ?? '—' }}</b></span>
              <span>可用资金 <b>{{ accountSnapshots[s.id].available_cash ?? '—' }}</b></span>
              <span>持仓市值 <b>{{ accountSnapshots[s.id].market_value ?? '—' }}</b></span>
            </div>
            <p style="margin:4px 0 0;color:var(--ink-soft);font-size:12px">
              采集于 {{ accountSnapshots[s.id].captured_at }}；BUY 审批额度 = min(虚拟现金, 真实可用 × 0.98)
            </p>
          </template>
          <p v-else style="margin:4px 0 0;color:var(--ink-soft);font-size:12px">
            账户快照采集中…（THS 通道 60s/次，读表需下单窗口打开）
          </p>
        </div>
      </div>
      <p style="margin:8px 0 0;color:var(--ink-soft);font-size:12px">
        外部持仓（柜台原有）已自动导入对应会话的虚拟账本纳入风控；不同引擎=不同账户，持仓/资金按会话隔离。
      </p>
    </div>

    <!-- 持仓（序号 + 组合/子策略归属，同票多子策略分多行；来源分级：策略/原有持仓） -->
    <div v-if="wbTab === 'positions'" class="table-wrap">
      <table>
        <thead><tr><th>序号</th><th>组合策略</th><th>子策略</th><th>代码</th><th>数量</th><th>成本价</th><th>市值</th><th>来源</th></tr></thead>
        <tbody>
          <tr v-for="(p, i) in positions" :key="positionRowKey(p)">
            <td style="color:var(--ink-soft)">{{ i + 1 }}</td>
            <td>{{ p.portfolio_id !== null && p.portfolio_id !== undefined ? portfolioName(p.portfolio_id) : '—' }}</td>
            <td>{{ strategyName(p.portfolio_id, p.strategy_id) }}</td>
            <td>{{ p.stock_code }}</td><td>{{ p.quantity }}</td><td>{{ fmtCost(p.avg_cost) }}</td><td>{{ fmtMoney(p.market_value) }}</td>
            <td>
              <span class="badge" :class="p.source === 'external' ? 'badge-gray' : 'badge-blue'">
                {{ p.source === 'external' ? '原有持仓' : '策略持仓' }}
              </span>
              <span v-if="p.attribution === 'fallback'" class="badge badge-orange" style="margin-left:4px"
                    title="不在该组合股票池内，按兜底规则挂到首个组合；按该组合风控参数管理，触及止损/止盈会自动卖出">
                非池内·兜底归属
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="positions.length === 0" class="empty-state">
        <p>暂无持仓</p>
        <p>账户原有持仓（非策略买入）以灰色标注为原有持仓，已自动导入虚拟账本纳入风控（真实成本计算止损/止盈）</p>
      </div>
    </div>

    <!-- 委托 -->
    <div v-if="wbTab === 'orders'" class="table-wrap">
      <div style="display:flex;align-items:center;gap:8px;margin:0 0 8px">
        <span style="font-size:12px;color:var(--ink-soft)">时间范围</span>
        <select v-model="orderRange" style="font-size:12px;padding:2px 6px" aria-label="委托时间范围">
          <option v-for="opt in TIME_RANGE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </select>
        <span style="font-size:12px;color:var(--ink-faint)">按北京时间自然日 · 共 {{ filteredOrders.length }} 条</span>
      </div>
      <table>
        <thead><tr><th>时间（北京时间）</th><th>方向</th><th>代码</th><th>状态</th><th>数量</th><th>价格</th><th>已成交</th><th>错误</th></tr></thead>
        <tbody>
          <tr v-for="o in filteredOrders" :key="o.key">
            <td style="color:var(--ink-soft)">{{ o.time || '—' }}</td>
            <td>{{ tradeTypeLabel(o.trade_type) }}</td>
            <td>{{ o.stock_code }}</td>
            <td><span class="badge" :class="o.status === 'filled' ? 'badge-green' : o.status === 'rejected' ? 'badge-red' : 'badge-gray'">{{ ORDER_STATUS_LABEL[o.status] || o.status }}</span></td>
            <td>{{ o.quantity }}</td>
            <td>{{ o.price ?? '-' }}</td>
            <td>{{ o.filled_quantity ?? '-' }}</td>
            <td style="color:var(--red);max-width:280px;white-space:normal">{{ o.error_message ?? '-' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="filteredOrders.length === 0" class="empty-state">
        <p>{{ orderRange === 'today' ? '今日暂无委托' : '该时间范围暂无委托' }}</p>
      </div>
    </div>

    <!-- 成交 -->
    <div v-if="wbTab === 'trades'" class="table-wrap">
      <table>
        <thead><tr><th>时间（北京时间）</th><th>方向</th><th>代码</th><th>价格</th><th>数量</th><th>金额</th></tr></thead>
        <tbody>
          <tr v-for="t in trades" :key="t.key">
            <td style="color:var(--ink-soft)">{{ t.time }}</td>
            <td>{{ tradeTypeLabel(t.trade_type) }}</td>
            <td>{{ t.stock_code }}</td>
            <td>{{ t.price }}</td><td>{{ t.quantity }}</td><td>{{ t.amount }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="trades.length === 0" class="empty-state"><p>暂无成交</p></div>
    </div>

    <!-- 决策/拦截（调参可观测性：闸门触发聚合 + 逐事件流） -->
    <div v-if="wbTab === 'decisions'" class="table-wrap">
      <div style="display:flex;align-items:center;gap:8px;margin:0 0 8px">
        <span style="font-size:12px;color:var(--ink-soft)">时间范围</span>
        <select v-model="decRange" style="font-size:12px;padding:2px 6px" aria-label="决策时间范围">
          <option v-for="opt in TIME_RANGE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </select>
        <span style="font-size:12px;color:var(--ink-faint)">按北京时间自然日 · 聚合与统计随筛选重算 · 共 {{ filteredDecisions.length }} 条事件</span>
      </div>
      <!-- 聚合统计（按当前筛选区间重算，不再是全时段累计） -->
      <table v-if="decisionSummaryView.length > 0" style="margin-bottom:14px">
        <thead><tr>
          <th>闸门</th><th>层</th><th>动作</th><th>参数</th><th>阈值</th>
          <th>次数</th><th>成交笔数</th><th>转化率</th><th>首次</th><th>末次</th><th>股票数</th><th>请求/实发</th>
        </tr></thead>
        <tbody>
          <tr v-for="(s, i) in decisionSummaryView" :key="s.gate + (s.param_name || '') + i">
            <td>{{ gateLabel(s.gate) }} <span class="gate-code">{{ s.gate }}</span></td>
            <td>{{ layerLabel(s.layer) }}</td>
            <td>{{ actionLabel(s.action) }}</td>
            <td style="color:var(--accent);font-family:monospace">{{ s.param_name || '—' }}</td>
            <td>{{ fmtGateVal(s.param_value) }}</td>
            <td style="text-align:right;font-weight:700;color:var(--red)">{{ s.count }}</td>
            <td style="text-align:right">{{ s.filled_count == null ? '—' : s.filled_count }}</td>
            <td style="text-align:right">{{ fmtRate(s.conversion_rate) }}</td>
            <td style="color:var(--ink-soft);font-size:11px">{{ s.first_bar_time ? s.first_bar_time.replace('T',' ').slice(11,19) : '—' }}</td>
            <td style="color:var(--ink-soft);font-size:11px">{{ s.last_bar_time ? s.last_bar_time.replace('T',' ').slice(11,19) : '—' }}</td>
            <td style="text-align:right">{{ s.stock_count }}</td>
            <td style="text-align:right">{{ qtyPair(s.requested_qty_sum, s.final_qty_sum) }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 逐事件流 -->
      <table>
        <thead><tr><th>时间（北京时间）</th><th>闸门</th><th>层</th><th>代码</th><th>策略</th><th>阈值</th><th>实际</th><th>请求/实发</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="d in filteredDecisions" :key="d.key">
            <td style="color:var(--ink-soft)">{{ d.time || '—' }}</td>
            <td><span class="badge badge-purple">{{ gateLabel(d.gate) }}</span></td>
            <td style="color:var(--ink-soft);font-size:11px">{{ layerLabel(d.layer) }}</td>
            <td>{{ d.stock_code || '—' }}</td>
            <td>{{ d.strategy_id ? `#${d.strategy_id}` : '组合' }}</td>
            <td>{{ fmtGateVal(d.param_value) }}</td>
            <td>{{ fmtGateVal(d.actual_value) }}</td>
            <td style="text-align:right">{{ qtyPair(d.requested_qty, d.final_qty) }}</td>
            <td style="color:var(--ink-soft);white-space:normal;min-width:160px">{{ d.message || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="filteredDecisions.length === 0" class="empty-state">
        <p v-if="decRange === 'today'">今日暂无决策事件。止损/止盈/熔断/资金不足/超仓/收盘拦单等事件会实时显示在这里。</p>
        <p v-else>该时间范围暂无决策事件。止损/止盈/熔断/资金不足/超仓/收盘拦单等事件会实时显示在这里。</p>
      </div>
    </div>

    <!-- 预警信号：alert 模式主产物（半自动）；auto 模式信号同样落库并默认经 send_warn 推送（审计旁路） -->
    <div v-if="wbTab === 'signals'" class="table-wrap">
      <div style="display:flex;justify-content:flex-end;align-items:center;margin:0 0 6px">
        <button @click="syncSignalsBlock" class="btn btn-sm"
                title="将当前信号列表的股票代码去重后写入通达信自选板块；覆盖写入板块">同步信号股到自选</button>
      </div>
      <p v-if="signalsTotal > signals.length" style="font-size:12px;color:var(--ink-faint);margin:0 0 6px">
        共 {{ signalsTotal }} 条，仅显示最新 {{ signals.length }} 条
      </p>
      <table>
        <thead><tr>
          <th>bar 时间</th><th>组合/策略</th><th>代码</th><th>公式</th><th>信号</th>
          <th>类型</th><th>触发值</th><th>价格</th>
          <th>预警推送</th><th>推送时间</th><th>错误</th>
        </tr></thead>
        <tbody>
          <tr v-for="s in signals" :key="s.id">
            <td style="color:var(--ink-soft)">{{ fmtTime(s.bar_time) }}</td>
            <td style="color:var(--ink-soft);font-size:12px">
              {{ portfolioName(s.portfolio_id) }} / {{ strategyName(s.portfolio_id, s.strategy_id) }}
            </td>
            <td>{{ s.stock_code }}</td>
            <td style="color:var(--ink-soft)">{{ s.formula_name }}</td>
            <td>{{ s.signal_name }}</td>
            <td><span class="badge badge-blue">{{ signalTypeLabel(s.signal_type) }}</span></td>
            <td>{{ s.trigger_value }}</td>
            <td>{{ s.price ?? '—' }}</td>
            <td>
              <span class="badge" :class="alertStatusClass(s.alert_status)"
                :title="s.tdx_run_id ? `tdx_run_id: ${s.tdx_run_id}` : ''"
              >{{ alertStatusLabel(s.alert_status) }}</span>
            </td>
            <td style="color:var(--ink-soft)">{{ fmtTime(s.alerted_at) }}</td>
            <td style="color:var(--red);font-size:11px;white-space:normal;min-width:120px">{{ s.alert_error || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="signals.length === 0" class="empty-state">
        <p>暂无预警信号。alert（半自动）模式信号触发后落库并经通达信 send_warn 推送；auto（全自动）模式默认同样推送（审计旁路），信号均落库供本页查看。</p>
      </div>
    </div>
  </div>

  <!-- B4a: 实时事件日志（多会话合并流，带会话前缀 + 过滤） -->
  <div class="card" style="margin-top:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h3 style="margin:0">实时事件日志</h3>
      <div style="display:flex;align-items:center;gap:8px">
        <select
          v-if="runningSessions.length > 1"
          class="input" style="width:auto;padding:4px 8px"
          v-model="eventFilter"
        >
          <option value="all">全部会话</option>
          <option v-for="s in runningSessions" :key="s.id" :value="s.id">#{{ s.id }} {{ s.name }}</option>
        </select>
        <span class="badge" :class="connState === 'open' ? 'badge-green' : 'badge-gray'">{{ connLabel[connState] }}</span>
      </div>
    </div>
    <div class="event-log" data-test="event-log">
      <div v-if="filteredEvents.length === 0" class="empty-state"><p>运行中 session 的信号 / 委托 / 成交 / 持仓 / 风控事件将实时显示在这里（多会话并行时事件带 [#id 名称] 前缀）</p></div>
      <div v-for="ev in filteredEvents" :key="ev.id" class="event-row">
        <span class="event-time">{{ ev.time }}</span>
        <span class="badge" :class="`badge-${EVENT_TYPE_COLOR[ev.type] || 'gray'}`">{{ ev.type }}</span>
        <span class="event-text">{{ ev.text }}</span>
      </div>
    </div>
  </div>

  <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
    <div class="modal-content">
      <h3>新建实盘</h3>
      <label>名称</label><input v-model="form.name" placeholder="例如：模拟盘A" />
      <label>模式</label><select v-model="form.mode"><option value="simulation">仿真</option><option value="live">实盘</option></select>
      <label>执行模式</label>
      <select v-model="form.execution_mode">
        <option value="alert">预警（半自动，仅 TQ 行情 + send_warn 推送，不下单）</option>
        <option value="auto">自动（按交易引擎下单）</option>
      </select>
      <label>交易引擎（决定信号计算的数据源）</label>
      <select v-model="form.engine">
        <option v-for="e in channel?.engines || []" :key="e.key" :value="e.key" :disabled="!e.configured">
          {{ e.label }}{{ e.configured ? '' : '（未配置）' }}
        </option>
      </select>
      <p style="font-size:12px;color:var(--ink-faint);margin:2px 0 12px">
        仿真=仿真账号(虚拟资金)，实盘=真实账号(真实资金)。
        <span v-if="form.execution_mode === 'alert'">
          预警模式：按所选引擎的数据源计算信号并通过 send_warn 推送预警，不下单。通达信 TQ 引擎使用 TQ 内存注入计算信号。
        </span>
        <span v-else>
          自动模式：下单由所选引擎执行——掘金量化桥跑在掘金终端内（桥策略运行中）；通达信 TQ 直连通达信客户端；同花顺走独立交易客户端 UI 自动化。引擎未就绪时启动会直接报错，不会静默换通道。
        </span>
      </p>
      <label>实盘资金基准（元）</label>
      <input v-model="form.live_capital" type="number" min="0" placeholder="留空 = 用组合初始资金" />
      <p style="font-size:12px;color:var(--ink-faint);margin:2px 0 12px">
        填写后会话内所有组合以该资金起账，资金占比/单仓/敞口等比率配置等比缩放；多组合各自独立记账。组合资金与实盘差异过大时，高价股可能受一手(100股)约束。
      </p>
      <label>外部信号文件路径（通达信条件预警输出，可选）</label>
      <input v-model="form.signal_file_path" placeholder="留空 = 走 TQ 公式计算信号；填写完整文件路径" />
      <p style="font-size:12px;color:var(--ink-faint);margin:2px 0 12px">
        填写后（如 D:\project\分时突击.txt）引擎定时读取该文件的新增信号行并注入交易链路——绕过 TQ 公式引擎的周期限制（如 1m 级别）。文件由通达信「条件预警」功能动态输出（GBK 追加写入）。所有级别公式均可走此链路。
      </p>
      <label>组合策略（可多选）</label>
      <div class="portfolio-check-list">
        <label v-for="p in portfolios" :key="p.id" class="portfolio-check">
          <input v-model="form.portfolio_ids" type="checkbox" :value="p.id" />
          {{ p.name }}<span style="color:var(--ink-faint)">（#{{ p.id }}）</span>
          <span v-if="portfolioFormulas(p.id)" class="formula-tag">{{ portfolioFormulas(p.id) }}</span>
        </label>
      </div>
      <div v-if="portfolios.length === 0" class="empty-state" style="padding:8px">
        <p>暂无组合策略，请先在"组合管理"页创建</p>
      </div>
      <div class="modal-actions">
        <button @click="create" class="btn btn-primary">确认</button>
        <button @click="showCreate = false" class="btn">取消</button>
      </div>
    </div>
  </div>

  <!-- ============ 实盘配置指南 Modal（M2）============ -->
  <div v-if="showGuide" class="modal-overlay modal-lg" @click.self="showGuide = false">
    <div class="modal-content">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <h3 style="margin:0">实盘配置指南</h3>
        <button class="btn btn-sm" @click="showGuide = false">关闭</button>
      </div>
      <div class="modal-body" style="margin-top:12px">
        <div class="form-group">
          <label class="group-label">一、交易通道选择（三引擎对比）</label>
          <div class="guide-block">
            <p class="guide-step"><b>通达信 TQ</b>：SDK 直调，无 UI 依赖，撤单/价格校验/大额拆单全支持——<b>无人值守首选</b>。</p>
            <p class="guide-step"><b>同花顺</b>：UI 自动化（键鼠模拟），file_builtin 文件交接；下单键入已真机校准
              （2026-10-29 实证：代码/价格/数量三字段全落框、真实委托成交）——撤单已自动化（F3 双击/OCR 导航）；
              运行期间勿触碰键鼠（UI 独占前台）。</p>
            <p class="guide-step"><b>掘金量化</b>：HTTP 桥（桥策略跑在掘金终端内，gm API 下单）；确认弹窗自动处置、持仓/资金实时读回。</p>
            <p class="guide-note">· 前置条件与通道配置入口：系统设置 → 对应通道段（通达信 TQ 交易通道 / 同花顺交易通道 / 掘金量化桥）。</p>
          </div>
        </div>

        <div class="form-group">
          <label class="group-label">二、会话与组合</label>
          <div class="guide-block">
            <p class="guide-step"><b>一个会话 = 一个引擎 + N 个组合</b>（新建实盘多选）；组合内多策略并行。</p>
            <p class="guide-step"><b>多会话并行</b>：支持（不同引擎/不同账户）；<b>同花顺通道同时仅一个会话</b>（键鼠独占）。</p>
            <p class="guide-step"><b>复制交易</b>：同一组合挂多个会话 = 各账户独立记账、同一信号两账户各执行一次（执行偏差自担）；
              资金基准 live_capital 按各账户规模设置。</p>
            <p class="guide-note" style="color:var(--red)"><b>禁止</b>：同一资金账号挂两个会话（资金竞争）。</p>
          </div>
        </div>

        <div class="form-group">
          <label class="group-label">三、实盘执行配置（策略表单「实盘执行」分组）</label>
          <div class="guide-block">
            <p class="guide-step"><b>撤单后追价重下 requote</b>：超时未成交 → 自动撤单 → 追 1 tick 重下
              （最多 requote_max_attempts 次）；默认关。</p>
            <p class="guide-step"><b>大额拆单（仅 TDX）</b>：单笔 ≥ split_order_threshold → 拆 N 子单，成交驱动续发；默认关。</p>
            <p class="guide-step"><b>风控执行时点 risk_timing</b>：next_day（默认，与回测一致——日线信号次日执行）/
              immediate（当日确认当日执行）。</p>
            <p class="guide-note">· 上述默认关闭 = 与回测行为一致；开启后建议先仿真观察。</p>
          </div>
        </div>

        <div class="form-group">
          <label class="group-label">四、原有持仓</label>
          <div class="guide-block">
            <p class="guide-step">账户存量持仓自动显示为「<b>原有持仓</b>」（灰色），仅供监视不参与策略交易。</p>
            <p class="guide-note">· 需会话运行中才显示；与策略持仓（蓝色）分列来源；对账差异含原有持仓属正常。</p>
          </div>
        </div>

        <div class="form-group">
          <label class="group-label">五、执行模式</label>
          <div class="guide-block">
            <p class="guide-step"><b>预警（alert）</b>：信号/风控全套落文件+推送，不下单——半自动。</p>
            <p class="guide-step"><b>自动（auto）</b>：全套自动下单——按所选引擎执行。</p>
            <p class="guide-note">· THS 自动模式已真机验证（下单→成交→撤单全链路）；首次实盘建议小单先行；运行期间勿触碰键鼠。</p>
          </div>
        </div>

        <div class="form-group">
          <label class="group-label">六、风险提示</label>
          <div class="guide-block">
            <p class="guide-note">· 同一资金账号禁止挂两个会话。</p>
            <p class="guide-note">· 同花顺真实账户操作请先小单验证。</p>
            <p class="guide-note">· 佣金/印花税按柜台真实回报记账；下单前费用为估算。</p>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <button @click="showGuide = false" class="btn btn-primary">知道了</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.channel-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  margin-bottom: 12px;
}
.channel-label { font-size: 13px; color: var(--ink-soft); }
.channel-hint { font-size: 12px; color: var(--ink-faint); }
.formula-tag { font-size: 11px; color: var(--accent); background: var(--accent-light); padding: 1px 6px; border-radius: 3px; margin-left: 4px; }
.event-log {
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 13px;
  background: var(--hover-bg);
}
.event-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--line);
}
.event-row:last-child { border-bottom: none; }
.event-time { color: var(--ink-faint); font-variant-numeric: tabular-nums; flex-shrink: 0; }
.event-text { word-break: break-all; }
.portfolio-check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 4px 0 12px;
}
.portfolio-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  background: var(--hover-bg);
}
.portfolio-chip {
  display: inline-block;
  margin-right: 6px;
}
.gate-code {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--hover-bg);
  color: var(--ink-faint);
  font-size: 11px;
  font-weight: 400;
}
</style>
