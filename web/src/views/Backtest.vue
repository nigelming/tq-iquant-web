<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getBacktestRecords } from '../api'

const records = ref<any[]>([])

onMounted(async () => {
  records.value = await getBacktestRecords()
})
</script>

<template>
  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>状态</th><th>进度</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="r in records" :key="r.id">
          <td style="color:#888">#{{ r.id }}</td>
          <td>{{ r.name }}</td>
          <td><span class="badge" :class="r.status === 'completed' ? 'badge-green' : r.status === 'running' ? 'badge-blue' : 'badge-gray'">{{ {completed:'已完成',running:'运行中',failed:'失败'}[r.status] || r.status }}</span></td>
          <td><span v-if="r.progress != null">{{ r.progress }}%</span><span v-else style="color:#888">-</span></td>
          <td><button class="btn btn-sm">查看</button></td>
        </tr>
      </tbody>
    </table>
    <div v-if="records.length === 0" class="empty-state"><p>暂无回测记录</p></div>
  </div>
</template>
