<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { formatEvent, nextEventId, EVENT_TYPE_COLOR, TRADE_TYPE_LABEL, ORDER_STATUS_LABEL, type LiveEvent } from '../utils/liveEvents'
import {
  orderEventToRow, orderHistoryToRows,
  tradeEventToRow, tradeHistoryToRows,
  upsertPositionRows, positionHistoryToRows, prependCapped,
  type OrderRow, type TradeRow, type PositionRow,
} from '../utils/liveWorkbench'
import {
  getLiveOrders, getLiveTrades, getLivePositions, getPortfolios,
  getLiveSessions, createLiveSession, startLiveSession, stopLiveSession, deleteLiveSession,
  recoverLiveBreaker,
  type PortfolioItem, type LiveSessionItem,
} from '../api'

const sessions = ref<LiveSessionItem[]>([])
const loading = ref(true)
const portfolios = ref<PortfolioItem[]>([])  // 全量组合策略,供新建实盘多选 + 会话列表解析名称
const showCreate = ref(false)
const form = ref({ name: '', mode: 'simulation', portfolio_ids: [] as number[] })

// 组合级状态列表：优先用后端 portfolios 字段，缺省时兜底 portfolio_ids(无状态视为 active)
function portfolioStatuses(s: LiveSessionItem): { portfolio_id: number; status: string }[] {
  if (s.portfolios && s.portfolios.length > 0) return s.portfolios
  return (s.portfolio_ids || []).map((id) => ({ portfolio_id: id, status: 'active' }))
}

function portfolioName(id: number): string {
  return portfolios.value.find((p) => p.id === id)?.name || `#${id}`
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

// ---- B4a: 实时事件日志面板（SSE）----
const events = ref<LiveEvent[]>([])
const connState = ref<'closed' | 'connecting' | 'open'>('closed')
const connLabel: Record<string, string> = { closed: '未连接', connecting: '连接中', open: '已连接' }
let es: EventSource | null = null
const EVENT_TYPES = ['signal', 'order', 'trade', 'position', 'risk'] as const
const LOG_CAP = 200

// ---- B4b: 工作台（持仓/委托/成交）----
const positions = ref<PositionRow[]>([])
const orders = ref<OrderRow[]>([])
const trades = ref<TradeRow[]>([])
const wbTab = ref<'positions' | 'orders' | 'trades'>('positions')
const TAB_LABEL: Record<'positions' | 'orders' | 'trades', string> = { positions: '持仓', orders: '委托', trades: '成交' }

const nowTime = () => new Date().toLocaleTimeString('zh-CN', { hour12: false })

function pushLog(type: string, data: Record<string, unknown>, time: string) {
  const text = formatEvent(type, data)
  if (!text) return // ping 心跳不进日志
  events.value.push({
    id: nextEventId(),
    type: type as LiveEvent['type'],
    time,
    text,
  })
  if (events.value.length > LOG_CAP) events.value = events.value.slice(-LOG_CAP)
}

function applyToWorkbench(type: string, data: Record<string, unknown>, time: string) {
  if (type === 'position') positions.value = upsertPositionRows(positions.value, data)
  else if (type === 'order') orders.value = prependCapped(orders.value, orderEventToRow(data, time))
  else if (type === 'trade') trades.value = prependCapped(trades.value, tradeEventToRow(data, time))
}

async function loadHistory(id: number) {
  try {
    const [pos, ord, trd] = await Promise.all([
      getLivePositions(id), getLiveOrders(id), getLiveTrades(id),
    ])
    positions.value = positionHistoryToRows(pos)
    orders.value = orderHistoryToRows(ord)
    trades.value = tradeHistoryToRows(trd)
  } catch {
    // 会话已删除等 → 保持现状
  }
}

function startEventStream(id: number) {
  closeEventStream()
  connState.value = 'connecting'
  const source = new EventSource(`/api/live/sessions/${id}/stream`)
  EVENT_TYPES.forEach((t) => {
    source.addEventListener(t, (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      const time = nowTime()
      pushLog(t, data, time)
      applyToWorkbench(t, data, time)
    })
  })
  source.onopen = () => { connState.value = 'open' }
  source.onerror = () => { connState.value = 'closed' } // EventSource 自动重连,不主动 close
  es = source
  loadHistory(id)
}

function closeEventStream() {
  if (es) { es.close(); es = null }
  connState.value = 'closed'
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
  // B6 全局限 1 个运行 session,自动接它的流
  const running = sessions.value.find((s) => s.status === 'running')
  if (running) startEventStream(running.id)
}

function openCreate() {
  form.value = { name: '', mode: 'simulation', portfolio_ids: [] }
  showCreate.value = true
}

async function create() {
  if (form.value.portfolio_ids.length === 0) {
    alert('请至少选择一个组合策略')
    return
  }
  await createLiveSession(form.value)
  showCreate.value = false
  form.value = { name: '', mode: 'simulation', portfolio_ids: [] }
  load()
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
  closeEventStream()
  await stopLiveSession(id)
  load()
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

onMounted(load)
onUnmounted(closeEventStream)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="openCreate" class="btn btn-primary">+ 新建实盘</button>
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <div v-else class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>组合策略</th><th>模式</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="s in sessions" :key="s.id">
          <td style="color:#888">#{{ s.id }}</td>
          <td>{{ s.name }}</td>
          <td style="color:#666">
            <span v-for="p in portfolioStatuses(s)" :key="p.portfolio_id" class="portfolio-chip">
              {{ portfolioName(p.portfolio_id) }}
              <span v-if="p.status === 'circuit_broken'" class="badge badge-red" style="margin-left:4px">熔断</span>
              <button v-if="p.status === 'circuit_broken'" @click="recoverBreaker(s, p.portfolio_id)" class="btn btn-sm" style="margin-left:4px">恢复</button>
            </span>
            <span v-if="portfolioStatuses(s).length === 0">-</span>
          </td>
          <td>{{ s.mode === 'simulation' ? '仿真' : '实盘' }}</td>
          <td><span class="badge" :class="s.status === 'running' ? 'badge-green' : 'badge-gray'">{{ s.status === 'running' ? '运行中' : '已停止' }}</span></td>
          <td>
            <button v-if="s.status === 'stopped'" @click="startSession(s.id)" class="btn btn-sm btn-primary">启动</button>
            <button v-if="s.status === 'running'" @click="stopSession(s.id)" class="btn btn-sm btn-danger">停止</button>
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
      <h3 style="margin:0">实盘工作台</h3>
      <div>
        <button
          v-for="(label, key) in TAB_LABEL" :key="key"
          class="btn btn-sm" :class="wbTab === key ? 'btn-primary' : ''"
          @click="wbTab = key"
        >{{ label }}</button>
      </div>
    </div>

    <!-- 持仓 -->
    <div v-if="wbTab === 'positions'" class="table-wrap">
      <table>
        <thead><tr><th>代码</th><th>数量</th><th>成本价</th><th>市值</th></tr></thead>
        <tbody>
          <tr v-for="p in positions" :key="p.stock_code">
            <td>{{ p.stock_code }}</td><td>{{ p.quantity }}</td><td>{{ p.avg_cost }}</td><td>{{ p.market_value }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="positions.length === 0" class="empty-state"><p>暂无持仓</p></div>
    </div>

    <!-- 委托 -->
    <div v-if="wbTab === 'orders'" class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>方向</th><th>代码</th><th>状态</th><th>数量</th><th>价格</th><th>已成交</th></tr></thead>
        <tbody>
          <tr v-for="o in orders" :key="o.key">
            <td style="color:#888">{{ o.time }}</td>
            <td>{{ TRADE_TYPE_LABEL[o.trade_type] || o.trade_type }}</td>
            <td>{{ o.stock_code }}</td>
            <td><span class="badge" :class="o.status === 'filled' ? 'badge-green' : o.status === 'rejected' ? 'badge-red' : 'badge-gray'">{{ ORDER_STATUS_LABEL[o.status] || o.status }}</span></td>
            <td>{{ o.quantity }}</td>
            <td>{{ o.price ?? '-' }}</td>
            <td>{{ o.filled_quantity ?? '-' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="orders.length === 0" class="empty-state"><p>暂无委托</p></div>
    </div>

    <!-- 成交 -->
    <div v-if="wbTab === 'trades'" class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>方向</th><th>代码</th><th>价格</th><th>数量</th><th>金额</th></tr></thead>
        <tbody>
          <tr v-for="t in trades" :key="t.key">
            <td style="color:#888">{{ t.time }}</td>
            <td>{{ TRADE_TYPE_LABEL[t.trade_type] || t.trade_type }}</td>
            <td>{{ t.stock_code }}</td>
            <td>{{ t.price }}</td><td>{{ t.quantity }}</td><td>{{ t.amount }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="trades.length === 0" class="empty-state"><p>暂无成交</p></div>
    </div>
  </div>

  <!-- B4a: 实时事件日志 -->
  <div class="card" style="margin-top:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h3 style="margin:0">实时事件日志</h3>
      <span class="badge" :class="connState === 'open' ? 'badge-green' : 'badge-gray'">{{ connLabel[connState] }}</span>
    </div>
    <div class="event-log" data-test="event-log">
      <div v-if="events.length === 0" class="empty-state"><p>运行中 session 的信号 / 委托 / 成交 / 持仓 / 风控事件将实时显示在这里</p></div>
      <div v-for="ev in events" :key="ev.id" class="event-row">
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
      <p style="font-size:12px;color:#999;margin:2px 0 12px">仿真=仿真账号(虚拟资金)，实盘=真实账号(真实资金)。模拟/实盘下单由 iQuant 客户端启动按钮控制，此处仅选账号环境。</p>
      <label>组合策略（可多选）</label>
      <div class="portfolio-check-list">
        <label v-for="p in portfolios" :key="p.id" class="portfolio-check">
          <input v-model="form.portfolio_ids" type="checkbox" :value="p.id" />
          {{ p.name }}<span style="color:#999">（#{{ p.id }}）</span>
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
</template>

<style scoped>
.event-log {
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid #eee;
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 13px;
  background: #fafafa;
}
.event-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed #eee;
}
.event-row:last-child { border-bottom: none; }
.event-time { color: #999; font-variant-numeric: tabular-nums; flex-shrink: 0; }
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
  border: 1px solid #eee;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  background: #fafafa;
}
.portfolio-chip {
  display: inline-block;
  margin-right: 6px;
}
</style>
