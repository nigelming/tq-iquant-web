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
