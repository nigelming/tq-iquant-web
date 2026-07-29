<script setup lang="ts">
</script>

<template>
  <div class="layout">
    <nav class="sidebar">
      <h2>创懿量化</h2>
      <ul>
        <li><router-link to="/stock-pools">股票池</router-link></li>
        <li><router-link to="/portfolios">组合策略</router-link></li>
        <li><router-link to="/backtest">回测管理</router-link></li>
      </ul>
    </nav>
    <main class="content">
      <router-view />
    </main>
  </div>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, sans-serif; }
.layout { display: flex; height: 100vh; }
.sidebar { width: 200px; background: #f5f5f5; padding: 1rem; border-right: 1px solid #ddd; }
.sidebar h2 { font-size: 1.1rem; margin-bottom: 1rem; }
.sidebar ul { list-style: none; }
.sidebar li { margin-bottom: 0.5rem; }
.sidebar a { text-decoration: none; color: #333; padding: 0.3rem 0.5rem; display: block; border-radius: 4px; }
.sidebar a:hover { background: #e0e0e0; }
.content { flex: 1; padding: 2rem; overflow-y: auto; }
.page h2 { margin-bottom: 1rem; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #eee; }
th { font-weight: 600; color: #666; }
</style>
