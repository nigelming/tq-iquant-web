<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
const titles: Record<string, string> = {
  'dashboard': '首页', 'stock-pools': '股票池', 'formulas': '公式管理', 'portfolios': '组合策略',
  'backtest': '回测管理', 'live': '实盘交易', 'system': '系统配置',
}
</script>

<template>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-header"><h1>创懿量化</h1></div>
      <nav class="sidebar-nav">
        <router-link to="/dashboard" class="nav-item" active-class="active">🏠 首页</router-link>
        <router-link to="/stock-pools" class="nav-item" active-class="active">📋 股票池</router-link>
        <router-link to="/formulas" class="nav-item" active-class="active">📐 公式管理</router-link>
        <router-link to="/portfolios" class="nav-item" active-class="active">📦 组合策略</router-link>
        <router-link to="/backtest" class="nav-item" active-class="active">📊 回测管理</router-link>
        <router-link to="/live" class="nav-item" active-class="active">⚡ 实盘交易</router-link>
        <router-link to="/system" class="nav-item" active-class="active">⚙️ 系统配置</router-link>
      </nav>
    </aside>
    <div class="main-container">
      <header class="topbar"><span class="topbar-title">{{ titles[route.name as string] || '创懿量化' }}</span></header>
      <main class="content"><router-view /></main>
    </div>
  </div>
</template>
