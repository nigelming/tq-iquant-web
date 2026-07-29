<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getPortfolios } from '../api'

const portfolios = ref<any[]>([])

onMounted(async () => {
  portfolios.value = await getPortfolios()
})
</script>

<template>
  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>初始资金</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="p in portfolios" :key="p.id">
          <td style="color:#888">#{{ p.id }}</td>
          <td>{{ p.name }}</td>
          <td>{{ p.initial_capital ? '¥' + Number(p.initial_capital).toLocaleString() : '-' }}</td>
          <td><span class="badge" :class="p.status === 'active' ? 'badge-green' : 'badge-gray'">{{ p.status === 'active' ? '运行中' : '已归档' }}</span></td>
        </tr>
      </tbody>
    </table>
    <div v-if="portfolios.length === 0" class="empty-state"><p>暂无组合策略</p></div>
  </div>
</template>
