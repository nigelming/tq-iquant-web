<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import axios from 'axios'
import { formatEvent, nextEventId, EVENT_TYPE_COLOR, type LiveEvent } from '../utils/liveEvents'

const sessions = ref<any[]>([])
const showCreate = ref(false)
const form = ref({ name: '', mode: 'simulation', portfolio_ids: [] })

// ---- B4a: 实时事件日志面板（SSE）----
const events = ref<LiveEvent[]>([])
const connState = ref<'closed' | 'connecting' | 'open'>('closed')
const connLabel: Record<string, string> = { closed: '未连接', connecting: '连接中', open: '已连接' }
let es: EventSource | null = null
const EVENT_TYPES = ['signal', 'order', 'trade', 'position', 'risk'] as const
const LOG_CAP = 200

function pushLog(type: string, data: Record<string, unknown>) {
  const text = formatEvent(type, data)
  if (!text) return // ping 心跳不进日志
  events.value.push({
    id: nextEventId(),
    type: type as LiveEvent['type'],
    time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    text,
  })
  if (events.value.length > LOG_CAP) events.value = events.value.slice(-LOG_CAP)
}

function startEventStream(id: number) {
  closeEventStream()
  connState.value = 'connecting'
  const source = new EventSource(`/api/live/sessions/${id}/stream`)
  EVENT_TYPES.forEach((t) => {
    source.addEventListener(t, (e: MessageEvent) => {
      pushLog(t, JSON.parse(e.data))
    })
  })
  source.onopen = () => { connState.value = 'open' }
  source.onerror = () => { connState.value = 'closed' } // EventSource 自动重连,不主动 close
  es = source
}

function closeEventStream() {
  if (es) { es.close(); es = null }
  connState.value = 'closed'
}

async function load() {
  const res = await axios.get('/api/live/sessions')
  sessions.value = res.data.data
  // B6 全局限 1 个运行 session,自动接它的流
  const running = sessions.value.find((s: any) => s.status === 'running')
  if (running) startEventStream(running.id)
}

async function create() {
  await axios.post('/api/live/sessions', form.value)
  showCreate.value = false
  form.value = { name: '', mode: 'simulation', portfolio_ids: [] }
  load()
}

async function startSession(id: number) {
  await axios.post(`/api/live/sessions/${id}/start`)
  load() // load 内自动连接新运行 session 的流
}

async function stopSession(id: number) {
  closeEventStream()
  await axios.post(`/api/live/sessions/${id}/stop`)
  load()
}

onMounted(load)
onUnmounted(closeEventStream)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="showCreate = true" class="btn btn-primary">+ 新建实盘</button>
  </div>

  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>模式</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="s in sessions" :key="s.id">
          <td style="color:#888">#{{ s.id }}</td>
          <td>{{ s.name }}</td>
          <td>{{ s.mode === 'simulation' ? '模拟' : '实盘' }}</td>
          <td><span class="badge" :class="s.status === 'running' ? 'badge-green' : 'badge-gray'">{{ s.status === 'running' ? '运行中' : '已停止' }}</span></td>
          <td>
            <button v-if="s.status === 'stopped'" @click="startSession(s.id)" class="btn btn-sm btn-primary">启动</button>
            <button v-if="s.status === 'running'" @click="stopSession(s.id)" class="btn btn-sm btn-danger">停止</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="sessions.length === 0" class="empty-state"><p>暂无实盘 session</p></div>
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
      <label>模式</label><select v-model="form.mode"><option value="simulation">模拟</option><option value="live">实盘</option></select>
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
</style>
