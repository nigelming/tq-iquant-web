<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useTheme } from './composables/useTheme'
import { getSystemStatus, type SystemStatus } from './api'
import { ICONS } from './icons'

const route = useRoute()
const { theme, toggle: toggleTheme } = useTheme()

const titles: Record<string, string> = {
  'dashboard': '首页', 'stock-pools': '股票池', 'formulas': '公式管理', 'portfolios': '组合策略',
  'backtest': '回测管理', 'live': '实盘交易', 'system': '系统配置',
}

interface NavEntry { to: string; label: string; icon: string }
interface NavGroup { label: string; items: NavEntry[] }

// 「总览」独立于三组之外（无组标签），保持改动前的导航顺序：总览 → 研究 → 交易 → 系统
const NAV_STANDALONE: NavEntry = { to: '/dashboard', label: '总览', icon: 'dashboard' }

const NAV_GROUPS: NavGroup[] = [
  {
    label: '研究',
    items: [
      { to: '/stock-pools', label: '股票池', icon: 'pools' },
      { to: '/formulas', label: '公式管理', icon: 'formula' },
      { to: '/portfolios', label: '组合策略', icon: 'portfolio' },
    ],
  },
  {
    label: '交易',
    items: [
      { to: '/backtest', label: '回测管理', icon: 'backtest' },
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

onMounted(() => {
  refreshStatus()
  statusTimer = window.setInterval(refreshStatus, 30000)
})

onUnmounted(() => {
  if (statusTimer !== null) window.clearInterval(statusTimer)
})
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>创懿量化</h1>
        <div class="sidebar-subtitle">量化交易平台</div>
      </div>
      <nav class="sidebar-nav">
        <router-link
          :to="NAV_STANDALONE.to"
          class="nav-item"
          active-class="active"
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
          >
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="ICONS[item.icon]" />
            </svg>
            <span>{{ item.label }}</span>
          </router-link>
        </template>
      </nav>
    </aside>
    <div class="main-container">
      <header class="topbar">
        <div class="topbar-left">
          <span class="topbar-title">{{ titles[route.name as string] || '创懿量化' }}</span>
          <span class="topbar-path">{{ route.path }}</span>
        </div>
        <div class="topbar-right">
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
        </div>
      </header>
      <main class="content"><router-view /></main>
    </div>
  </div>
</template>
