<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import {
  getStockPools, getFormulas, getPortfolios, getBacktestRecords,
  getLiveSessions, getSystemConfigs, getSystemStatus, getLiveChannel,
  type BacktestRecordItem, type SystemConfig, type SystemStatus,
  type LiveSessionItem,
} from '../api'

const loading = ref(true)
const errorMsg = ref('')

const poolCount = ref(0)
const formulaCount = ref(0)
const portfolioCount = ref(0)
const backtestCount = ref(0)
const liveCount = ref(0)
const liveRunning = ref(0)
const recentBacktests = ref<BacktestRecordItem[]>([])
const cfg = ref<SystemConfig | null>(null)
const status = ref<SystemStatus | null>(null)
const liveSessions = ref<LiveSessionItem[]>([])
const channels = ref<{ key: string; label: string; online: boolean | null }[]>([])

const ENGINE_LABEL: Record<string, string> = {
  tdx: '通达信 TQ', ths: '同花顺', iquant: 'iQuant 桥',
}
const EXEC_MODE_LABEL: Record<string, string> = {
  alert: '预警', auto: '自动',
}

const liveRunningSessions = computed(() =>
  liveSessions.value.filter((s) => s.status === 'running'))

async function load() {
  errorMsg.value = ''
  // allSettled：任一查询失败不阻塞其余卡片，失败项保持 0/空展示
  const results = await Promise.allSettled([
    getStockPools(), getFormulas(), getPortfolios(), getBacktestRecords(),
    getLiveSessions(), getSystemConfigs(), getSystemStatus(),
    getLiveChannel('tdx'), getLiveChannel('ths'), getLiveChannel('iquant'),
  ])
  const [pools, formulas, portfolios, backtests, lives, configs, sys,
    chTdx, chThs, chIquant] = results

  if (pools.status === 'fulfilled') poolCount.value = pools.value.length
  if (formulas.status === 'fulfilled') formulaCount.value = formulas.value.length
  if (portfolios.status === 'fulfilled') portfolioCount.value = portfolios.value.length
  if (backtests.status === 'fulfilled') {
    backtestCount.value = backtests.value.length
    // 最近 5 条回测记录，新的在前
    recentBacktests.value = backtests.value.slice(0, 5)
  }
  if (lives.status === 'fulfilled') {
    liveSessions.value = lives.value
    liveCount.value = lives.value.length
    liveRunning.value = lives.value.filter((s) => s.status === 'running').length
  }
  if (configs.status === 'fulfilled') cfg.value = configs.value
  if (sys.status === 'fulfilled') status.value = sys.value

  const channelDefs: [string, any][] = [
    ['tdx', chTdx], ['ths', chThs], ['iquant', chIquant],
  ]
  channels.value = channelDefs.map(([key, r]) => {
    const fallback = { key, label: ENGINE_LABEL[key] || key, online: null }
    if (r.status !== 'fulfilled') return fallback
    const p = r.value
    return { key, label: p.label || fallback.label, online: p.online }
  })

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
            <span class="metric-value" :style="{ fontSize: '15px' }">
              运行中 {{ liveRunning }} · 共 {{ liveCount }}
            </span>
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
        <span class="frame-title">实盘会话</span>
      </div>
      <div class="frame-body">
        <div class="table-wrap">
          <table>
            <thead><tr><th>名称</th><th>引擎</th><th>执行模式</th><th>组合</th><th>状态</th><th>对账差异</th></tr></thead>
            <tbody>
              <tr v-for="s in liveSessions" :key="s.id">
                <td>{{ s.name }}</td>
                <td>{{ ENGINE_LABEL[s.engine] || s.engine }}</td>
                <td>{{ EXEC_MODE_LABEL[s.execution_mode] || s.execution_mode }}</td>
                <td style="color:var(--ink-soft)">{{ (s.portfolio_ids || []).length }} 个组合</td>
                <td>
                  <span v-if="s.status === 'running'" class="badge badge-green">运行中</span>
                  <span v-else class="badge badge-gray">已停止</span>
                </td>
                <td>
                  <span v-if="(s.reconcile_mismatches || []).length"
                        class="badge badge-red"
                        :title="(s.reconcile_mismatches || []).map(m => `${m.code} 虚${m.virtual}/实${m.real}`).join('；')">
                    {{ (s.reconcile_mismatches || []).length }} 项
                  </span>
                  <span v-else style="color:var(--ink-soft)">—</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="liveSessions.length === 0" class="empty-state"><p>暂无实盘会话</p></div>
        </div>
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">03</span>
        <span class="frame-title">交易通道</span>
      </div>
      <div class="frame-body">
        <div class="metric-grid" style="gap:12px;margin-bottom:0">
          <div v-for="c in channels" :key="c.key" class="metric-card">
            <span class="metric-label">{{ c.label }}</span>
            <span class="metric-value" :style="{ fontSize: '15px' }">
              {{ c.online === true ? '在线' : c.online === false ? '离线' : '未知' }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <div class="frame">
      <div class="frame-head">
        <span class="frame-num">04</span>
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
        <span class="frame-num">05</span>
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
