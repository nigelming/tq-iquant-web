<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getStockPools, getFormulas, getPortfolios, getBacktestRecords,
  getLiveSessions, getSystemConfigs, getSystemStatus,
  type BacktestRecordItem, type SystemConfig, type SystemStatus,
} from '../api'

const loading = ref(true)
const errorMsg = ref('')

const poolCount = ref(0)
const formulaCount = ref(0)
const portfolioCount = ref(0)
const backtestCount = ref(0)
const liveCount = ref(0)
const recentBacktests = ref<BacktestRecordItem[]>([])
const cfg = ref<SystemConfig | null>(null)
const status = ref<SystemStatus | null>(null)

async function load() {
  errorMsg.value = ''
  // allSettled：任一查询失败不阻塞其余卡片，失败项保持 0/空展示
  const results = await Promise.allSettled([
    getStockPools(), getFormulas(), getPortfolios(), getBacktestRecords(),
    getLiveSessions(), getSystemConfigs(), getSystemStatus(),
  ])
  const [pools, formulas, portfolios, backtests, lives, configs, sys] = results

  if (pools.status === 'fulfilled') poolCount.value = pools.value.length
  if (formulas.status === 'fulfilled') formulaCount.value = formulas.value.length
  if (portfolios.status === 'fulfilled') portfolioCount.value = portfolios.value.length
  if (backtests.status === 'fulfilled') {
    backtestCount.value = backtests.value.length
    // 最近 5 条回测记录，新的在前
    recentBacktests.value = backtests.value.slice(0, 5)
  }
  if (lives.status === 'fulfilled') liveCount.value = lives.value.length
  if (configs.status === 'fulfilled') cfg.value = configs.value
  if (sys.status === 'fulfilled') status.value = sys.value

  const failed = results.filter(r => r.status === 'rejected')
  if (failed.length > 0) {
    errorMsg.value = `部分数据加载失败：${failed.length} 项（其余卡片正常）`
  }
  loading.value = false
}

function fmtDate(d: string | null): string {
  return d ? d.slice(0, 10) : '-'
}

onMounted(load)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="load" class="btn">刷新</button>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:#c0392b;margin-bottom:12px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <template v-else>
    <div class="metric-grid">
      <div class="metric-card">
        <span class="metric-label">股票池</span>
        <span class="metric-value">{{ poolCount }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">公式</span>
        <span class="metric-value">{{ formulaCount }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">组合策略</span>
        <span class="metric-value">{{ portfolioCount }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">回测记录</span>
        <span class="metric-value">{{ backtestCount }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">实盘会话</span>
        <span class="metric-value">{{ liveCount }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">Core 状态</span>
        <span class="metric-value" :style="{ fontSize: '15px' }">
          {{ status ? (status.core.online ? '在线' : '离线') : '-' }}
        </span>
        <span class="metric-label" style="font-size:11px">
          {{ status ? `v${status.core.version} · 运行 ${status.core.uptime}` : '未获取到状态' }}
        </span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3 style="margin:0 0 8px">最近回测</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>名称</th><th>区间</th><th>状态</th><th>进度</th></tr></thead>
          <tbody>
            <tr v-for="r in recentBacktests" :key="r.id">
              <td>{{ r.name }}</td>
              <td style="color:#888">{{ fmtDate(r.start_date) }} ~ {{ fmtDate(r.end_date) }}</td>
              <td>
                <span v-if="r.status === 'completed'" class="badge badge-green">完成</span>
                <span v-else-if="r.status === 'running'" class="badge badge-blue">运行中</span>
                <span v-else class="badge badge-gray">{{ r.status }}</span>
              </td>
              <td>{{ r.progress != null ? Math.round(r.progress * 100) + '%' : '-' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="recentBacktests.length === 0" class="empty-state"><p>暂无回测记录</p></div>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px">系统配置</h3>
      <p v-if="cfg" style="margin:0;color:var(--text-secondary)">
        通达信路径：{{ cfg.tdx_path }}<br>
        iQuant 路径：{{ cfg.iquant_path }}<br>
        仿真桥：{{ cfg.iquant_bridge.simulation.base_url }}<br>
        实盘桥：{{ cfg.iquant_bridge.live.base_url }}
      </p>
      <p v-else style="margin:0;color:var(--text-secondary)">配置加载失败</p>
    </div>
  </template>
</template>
