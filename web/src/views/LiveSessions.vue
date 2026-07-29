<script setup lang="ts">
import { ref, onMounted } from 'vue'
import axios from 'axios'

const sessions = ref<any[]>([])
const showCreate = ref(false)
const form = ref({ name: '', mode: 'simulation', portfolio_ids: [] })

async function load() {
  const res = await axios.get('/api/live/sessions')
  sessions.value = res.data.data
}

async function create() {
  await axios.post('/api/live/sessions', form.value)
  showCreate.value = false
  form.value = { name: '', mode: 'simulation', portfolio_ids: [] }
  load()
}

async function startSession(id: number) {
  await axios.post(`/api/live/sessions/${id}/start`)
  load()
}

async function stopSession(id: number) {
  await axios.post(`/api/live/sessions/${id}/stop`)
  load()
}

onMounted(load)
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
