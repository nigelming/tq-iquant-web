<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getStockPools } from '../api'

const pools = ref<any[]>([])

onMounted(async () => {
  pools.value = await getStockPools()
})
</script>

<template>
  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>同步时间</th></tr></thead>
      <tbody>
        <tr v-for="p in pools" :key="p.id">
          <td style="color:#888">#{{ p.id }}</td>
          <td>{{ p.name }}</td>
          <td style="color:#888;font-size:13px">{{ p.synced_at || '-' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="pools.length === 0" class="empty-state"><p>暂无股票池</p></div>
  </div>
</template>
