import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/stock-pools' },
    { path: '/stock-pools', name: 'stock-pools', component: () => import('../views/StockPools.vue') },
    { path: '/portfolios', name: 'portfolios', component: () => import('../views/Portfolios.vue') },
    { path: '/backtest', name: 'backtest', component: () => import('../views/Backtest.vue') },
    { path: '/live', name: 'live', component: () => import('../views/LiveSessions.vue') },
    { path: '/system', name: 'system', component: () => import('../views/SystemConfig.vue') },
  ],
})

export default router
