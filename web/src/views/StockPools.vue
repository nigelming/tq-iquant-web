<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getStockPools } from '../api'

const pools = ref<any[]>([])

onMounted(async () => {
  pools.value = await getStockPools()
})
</script>

<template>
  <div class="page">
    <h2>股票池</h2>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>同步时间</th></tr></thead>
      <tbody>
        <tr v-for="p in pools" :key="p.id">
          <td>{{ p.id }}</td><td>{{ p.name }}</td><td>{{ p.synced_at }}</td>
        </tr>
        <tr v-if="pools.length === 0"><td colspan="3">暂无数据</td></tr>
      </tbody>
    </table>
  </div>
</template>
