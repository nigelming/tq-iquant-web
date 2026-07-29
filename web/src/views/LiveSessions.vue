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
  <div class="page">
    <div class="header">
      <h2>实盘交易</h2>
      <button @click="showCreate = true" class="btn">新建实盘</button>
    </div>

    <div v-if="showCreate" class="modal">
      <div class="modal-content">
        <h3>新建实盘</h3>
        <input v-model="form.name" placeholder="名称" />
        <select v-model="form.mode">
          <option value="simulation">模拟</option>
          <option value="live">实盘</option>
        </select>
        <div class="modal-actions">
          <button @click="create" class="btn">确认</button>
          <button @click="showCreate = false" class="btn btn-cancel">取消</button>
        </div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th>ID</th><th>名称</th><th>模式</th><th>状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in sessions" :key="s.id">
          <td>{{ s.id }}</td>
          <td>{{ s.name }}</td>
          <td>{{ s.mode === 'simulation' ? '模拟' : '实盘' }}</td>
          <td>{{ s.status === 'running' ? '运行中' : '已停止' }}</td>
          <td>
            <button v-if="s.status === 'stopped'" @click="startSession(s.id)" class="btn btn-sm">启动</button>
            <button v-if="s.status === 'running'" @click="stopSession(s.id)" class="btn btn-sm btn-cancel">停止</button>
          </td>
        </tr>
        <tr v-if="sessions.length === 0"><td colspan="5">暂无数据</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.btn { padding: 0.4rem 0.8rem; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: #fff; }
.btn-sm { padding: 0.2rem 0.5rem; font-size: 0.85rem; }
.btn-cancel { color: #999; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; }
.modal-content { background: #fff; padding: 2rem; border-radius: 8px; min-width: 300px; }
.modal-content h3 { margin-bottom: 1rem; }
.modal-content input, .modal-content select { width: 100%; padding: 0.5rem; margin-bottom: 0.5rem; border: 1px solid #ddd; border-radius: 4px; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 1rem; }
</style>
