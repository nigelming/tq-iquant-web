import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'
import { ICONS } from '../icons'
import { resetTheme } from '../composables/useTheme'

// 路由 mock：App.vue 调 useRoute() 且模板用 <router-link>，
// 不装 router 插件时 useRoute() 返回 undefined 会直接抛错。
const routeState = reactive({ path: '/dashboard', name: 'dashboard' })
vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  RouterLink: {
    props: ['to'],
    template: '<a class="router-link-stub" :href="to"><slot /></a>',
  },
}))

vi.mock('../api', () => ({
  getSystemStatus: vi.fn().mockResolvedValue({
    core: { online: true, version: '1.0', uptime: '0h53m' },
  }),
}))

import App from '../App.vue'

const KEYS = ['dashboard', 'pools', 'formula', 'portfolio', 'backtest', 'live', 'system', 'sun', 'moon', 'search']

describe('icons.ts', () => {
  it('导出 10 个图标 key，值均为合法 path d 字符串', () => {
    expect(Object.keys(ICONS).sort()).toEqual([...KEYS].sort())
    for (const k of KEYS) {
      expect(typeof ICONS[k]).toBe('string')
      expect(ICONS[k].length).toBeGreaterThan(4)
      expect(ICONS[k]).toMatch(/^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.\-+]+$/)
    }
  })

  it('无 Emoji 混入（专业终端不用 Emoji 图标）', () => {
    for (const k of KEYS) {
      expect(ICONS[k]).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
    }
  })
})

describe('App.vue — 图标渲染', () => {
  beforeEach(() => {
    resetTheme()
    localStorage.clear()
  })

  it('侧边栏 8 个导航项各渲染一个内联 SVG 图标（非 Emoji 文本节点）', async () => {
    const w = mount(App)
    await flushPromises()
    const items = w.findAll('.sidebar-nav .nav-item')
    expect(items).toHaveLength(8)
    items.forEach((it) => {
      const svg = it.find('svg.icon')
      expect(svg.exists()).toBe(true)
      expect(svg.findAll('path')).toHaveLength(1)
      expect(svg.text()).toBe('')
    })
    expect(w.text()).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
  })
})

describe('App.vue — 外壳结构', () => {
  beforeEach(() => {
    resetTheme()
    localStorage.clear()
  })

  it('侧边栏导航分三组（研究/交易/系统）并有组标签', async () => {
    const w = mount(App)
    await flushPromises()
    const labels = w.findAll('.nav-group-label').map((e) => e.text())
    expect(labels).toEqual(['研究', '交易', '系统'])
    expect(w.findAll('.nav-group-label')).toHaveLength(3)
  })

  it('主题切换按钮在侧边栏底部（与 Core 状态同区）', async () => {
    const w = mount(App)
    await flushPromises()
    const foot = w.find('.sidebar-footbar')
    expect(foot.exists()).toBe(true)
    expect(foot.find('.theme-toggle').exists()).toBe(true)
    expect(w.find('.sidebar-footer').exists()).toBe(false)
  })

  it('Core 在线时状态点为 online 且显示版本与运行时长', async () => {
    const w = mount(App)
    await flushPromises()
    const dot = w.find('.status-dot')
    expect(dot.exists()).toBe(true)
    expect(dot.classes()).toContain('online')
    expect(w.find('.status-chip').text()).toContain('Core 在线')
    expect(w.find('.status-chip').text()).toContain('v1.0')
    expect(w.find('.status-chip').text()).toContain('0h53m')
  })

  it('顶栏横幅显示平台大标题（居中双色）', async () => {
    const w = mount(App)
    await flushPromises()
    const banner = w.find('.topbar .app-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('创懿私募基金量化回测平台')
  })
})

