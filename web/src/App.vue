<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useTheme } from './composables/useTheme'
import { getSystemStatus, type SystemStatus } from './api'
import { ICONS } from './icons'

const route = useRoute()
const { theme, toggle: toggleTheme } = useTheme()

const titles: Record<string, string> = {
  'dashboard': '状态总览', 'stock-pools': '备选股池', 'formulas': '公式管理', 'portfolios': '组合策略',
  'backtest': '回测管理', 'backtest-analysis': '回归验证', 'live': '实盘交易', 'system': '系统配置',
}

interface NavEntry { to: string; label: string; icon: string }
interface NavGroup { label: string; items: NavEntry[] }

// 「状态总览」独立于三组之外（无组标签），保持导航顺序：状态总览 → 研究 → 交易 → 系统
const NAV_STANDALONE: NavEntry = { to: '/dashboard', label: '状态总览', icon: 'dashboard' }

const NAV_GROUPS: NavGroup[] = [
  {
    label: '研究',
    items: [
      { to: '/stock-pools', label: '备选股池', icon: 'pools' },
      { to: '/formulas', label: '公式管理', icon: 'formula' },
      { to: '/portfolios', label: '组合策略', icon: 'portfolio' },
    ],
  },
  {
    label: '交易',
    items: [
      { to: '/backtest', label: '回测管理', icon: 'backtest' },
      { to: '/backtest/analysis', label: '回归验证', icon: 'backtest' },
      { to: '/live', label: '实盘交易', icon: 'live' },
    ],
  },
  {
    label: '系统',
    items: [
      { to: '/system', label: '系统配置', icon: 'system' },
    ],
  },
]

const status = ref<SystemStatus | null>(null)
const statusFailed = ref(false)
let statusTimer: number | null = null
let heartbeatTimer: number | null = null

function beacon(url: string) {
  try { navigator.sendBeacon?.(url) } catch { /* noop */ }
}

function onPageHide() {
  beacon('/api/system/closing')
}

function exitApp() {
  beacon('/api/system/shutdown')
  window.close()
}

async function refreshStatus() {
  try {
    status.value = await getSystemStatus()
    statusFailed.value = false
  } catch {
    statusFailed.value = true
  }
}

function coreLabel(): string {
  if (statusFailed.value || !status.value) return 'Core 未知'
  return status.value.core.online ? 'Core 在线' : 'Core 离线'
}

function coreDotClass(): string {
  if (statusFailed.value || !status.value) return ''
  return status.value.core.online ? 'online' : 'offline'
}

// 浏览器标题
watch(
  () => route.name,
  () => { document.title = '创懿量化回测平台 · ' + (titles[route.name as string] || '创懿量化') },
  { immediate: true },
)

onMounted(() => {
  refreshStatus()
  statusTimer = window.setInterval(refreshStatus, 30000)
  beacon('/api/system/heartbeat')
  heartbeatTimer = window.setInterval(() => beacon('/api/system/heartbeat'), 5000)
  window.addEventListener('pagehide', onPageHide)
})

onUnmounted(() => {
  if (statusTimer !== null) window.clearInterval(statusTimer)
  if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer)
  window.removeEventListener('pagehide', onPageHide)
})
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-header">
        <div class="sidebar-brand">
          <h1 class="sidebar-title">创懿量化</h1>
          <div class="sidebar-subtitle">CHUANGYI QUANT</div>
        </div>
      </div>
      <nav class="sidebar-nav">
        <router-link
          :to="NAV_STANDALONE.to"
          class="nav-item"
          active-class="active"
          :title="NAV_STANDALONE.label"
        >
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="ICONS[NAV_STANDALONE.icon]" />
          </svg>
          <span>{{ NAV_STANDALONE.label }}</span>
        </router-link>
        <template v-for="g in NAV_GROUPS" :key="g.label">
          <div class="nav-group-label">{{ g.label }}</div>
          <router-link
            v-for="item in g.items"
            :key="item.to"
            :to="item.to"
            class="nav-item"
            active-class="active"
            :title="item.label"
          >
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="ICONS[item.icon]" />
            </svg>
            <span>{{ item.label }}</span>
          </router-link>
        </template>
      </nav>
      <div class="sidebar-footbar">
        <span class="status-chip">
          <span class="status-dot" :class="coreDotClass()" />
          <span>{{ coreLabel() }}</span>
          <span v-if="status">v{{ status.core.version }} · {{ status.core.uptime }}</span>
        </span>
        <button class="btn btn-sm theme-toggle" @click="toggleTheme" :aria-label="theme === 'dark' ? '切换到浅色模式' : '切换到暗色模式'">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="theme === 'dark' ? ICONS.sun : ICONS.moon" />
          </svg>
          <span>{{ theme === 'dark' ? '浅色' : '暗色' }}</span>
        </button>
        <button class="btn btn-sm" @click="exitApp" title="退出：关闭后台服务并释放端口">退出</button>
      </div>
    </aside>
    <div class="main-container">
      <header class="topbar">
        <h1 class="app-banner"><span class="wb-1">创懿私募基金</span><span class="wb-2">量化回测平台</span></h1>
      </header>
      <main class="content"><router-view /></main>
    </div>
  </div>
</template>
