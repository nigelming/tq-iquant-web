<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getBacktestRecords } from '../api'

const records = ref<any[]>([])

onMounted(async () => {
  records.value = await getBacktestRecords()
})
</script>

<template>
  <div class="page">
    <h2>回测管理</h2>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>状态</th><th>进度</th></tr></thead>
      <tbody>
        <tr v-for="r in records" :key="r.id">
          <td>{{ r.id }}</td><td>{{ r.name }}</td><td>{{ r.status }}</td><td>{{ r.progress }}%</td>
        </tr>
        <tr v-if="records.length === 0"><td colspan="4">暂无数据</td></tr>
      </tbody>
    </table>
  </div>
</template>
