import { vi } from 'vitest'

const CSS_VARS: Record<string, string> = {
  '--red': '#d6403a',
  '--green': '#1f6e4f',
  '--gold': '#f0a429',
  '--blue': '#4a7ec4',
  '--ink': '#1f3a68',
  '--ink-soft': '#6b7280',
  '--ink-faint': '#9ca3af',
  '--line': '#e5e7eb',
  '--card-bg': '#ffffff',
  '--accent': '#667eea',
  '--accent-light': '#dbeafe',
}

vi.stubGlobal('getComputedStyle', () => ({
  getPropertyValue: (name: string) => CSS_VARS[name] ?? '',
}))

// 测试环境不发起真实心跳/关闭信标（happy-dom 的 sendBeacon 内部走 fetch → 未处理拒绝）
// 只在现有 navigator 上加 sendBeacon，保留 userAgent 等原型属性（zrender 依赖 navigator.userAgent）
Object.defineProperty(navigator, 'sendBeacon', {
  value: vi.fn(() => true),
  configurable: true,
})
