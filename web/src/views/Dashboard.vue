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
  <div class="dash-hero">
    <div class="dash-hero-main">
      <div class="dash-hero-stats">
        <span><b>{{ poolCount }}</b> 股票池</span>
        <span><b>{{ formulaCount }}</b> 公式</span>
        <span><b>{{ portfolioCount }}</b> 组合</span>
        <span><b>{{ backtestCount }}</b> 回测</span>
        <span><b class="db-accent">{{ liveCount }}</b> 实盘</span>
      </div>
    </div>
    <div class="dash-hero-actions">
      <button @click="load" class="btn">刷新</button>
    </div>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:16px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:16px"><p>加载中…</p></div>
  <template v-else>
    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">01</span>
        <span class="frame-title">核心指标</span>
      </div>
      <div class="frame-body">
        <div class="metric-grid" style="gap:12px;margin-bottom:0">
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
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">02</span>
        <span class="frame-title">最近回测</span>
      </div>
      <div class="frame-body">
        <div class="table-wrap">
          <table>
            <thead><tr><th>名称</th><th>区间</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="r in recentBacktests" :key="r.id">
                <td>{{ r.name }}</td>
                <td style="color:var(--ink-soft);font-variant-numeric:tabular-nums">{{ fmtDate(r.start_date) }} ~ {{ fmtDate(r.end_date) }}</td>
                <td>
                  <span v-if="r.status === 'completed'" class="badge badge-green">完成</span>
                  <span v-else-if="r.status === 'running'" class="badge badge-blue">运行中</span>
                  <span v-else class="badge badge-gray">{{ r.status }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="recentBacktests.length === 0" class="empty-state"><p>暂无回测记录</p></div>
        </div>
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">03</span>
        <span class="frame-title">系统配置</span>
      </div>
      <div class="frame-body">
        <p v-if="cfg" style="margin:0;color:var(--ink-soft);line-height:1.8">
          通达信路径：{{ cfg.tdx_path }}<br>
          iQuant 路径：{{ cfg.iquant_path }}<br>
          仿真桥：{{ cfg.iquant_bridge.simulation.base_url }}<br>
          实盘桥：{{ cfg.iquant_bridge.live.base_url }}
        </p>
        <p v-else style="margin:0;color:var(--ink-soft)">配置加载失败</p>
      </div>
    </div>
  </template>
</template>

<style scoped>
.dash-hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  padding: 4px 2px 20px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--line);
}
.dash-hero-stats { display: flex; flex-wrap: wrap; gap: 8px 22px; font-size: 12px; color: var(--ink-soft) }
.dash-hero-stats b { font-family: var(--font-serif); font-size: 18px; font-weight: 800; color: var(--ink); margin-right: 5px; font-variant-numeric: tabular-nums }
.dash-hero-stats .db-accent { color: var(--red) }
.dash-hero-actions { flex-shrink: 0 }
</style>
