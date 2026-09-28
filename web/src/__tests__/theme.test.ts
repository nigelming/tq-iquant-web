import { describe, it, expect, beforeEach } from 'vitest'
import { useTheme, resetTheme } from '../composables/useTheme'

describe('useTheme', () => {
  beforeEach(() => { localStorage.clear(); resetTheme() })

  it('defaults to light when no localStorage', () => {
    const { theme } = useTheme()
    expect(theme.value).toBe('light')
  })

  it('reads localStorage on init', () => {
    localStorage.setItem('theme', 'dark')
    const { theme } = useTheme()
    expect(theme.value).toBe('dark')
  })

  it('toggles theme and persists', () => {
    const { theme, toggle } = useTheme()
    toggle()
    expect(theme.value).toBe('dark')
    expect(localStorage.getItem('theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('applies data-theme to documentElement', () => {
    const { toggle } = useTheme()
    toggle()
    expect(document.documentElement.dataset.theme).toBe('dark')
    toggle()
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
