<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getPortfolios } from '../api'

const portfolios = ref<any[]>([])

onMounted(async () => {
  portfolios.value = await getPortfolios()
})
</script>

<template>
  <div class="page">
    <h2>组合策略</h2>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>资金</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="p in portfolios" :key="p.id">
          <td>{{ p.id }}</td><td>{{ p.name }}</td><td>{{ p.initial_capital }}</td><td>{{ p.status }}</td>
        </tr>
        <tr v-if="portfolios.length === 0"><td colspan="4">暂无数据</td></tr>
      </tbody>
    </table>
  </div>
</template>
