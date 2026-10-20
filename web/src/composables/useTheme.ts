import { ref, watch, type Ref } from 'vue'

type Theme = 'light' | 'dark'

function getInitialTheme(): Theme {
  const saved = localStorage.getItem('theme')
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

let _theme: Ref<Theme> | null = null

function createTheme(): Ref<Theme> {
  const t = ref<Theme>(getInitialTheme())
  watch(t, (val) => {
    document.documentElement.dataset.theme = val
    localStorage.setItem('theme', val)
  }, { immediate: true, flush: 'sync' })
  return t
}

export function useTheme() {
  if (!_theme) {
    _theme = createTheme()
  }
  const theme = _theme

  function toggle() {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }
  return { theme, toggle }
}

export function resetTheme() {
  _theme = null
}
