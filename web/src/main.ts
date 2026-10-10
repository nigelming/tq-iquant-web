import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

// 初始化主题（在 mount 前设置 data-theme，避免闪烁）
const saved = localStorage.getItem('theme')
const initial = saved === 'dark' ? 'dark' : 'light'
document.documentElement.dataset.theme = initial

createApp(App).use(router).mount('#app')
