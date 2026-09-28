import { ref, watch } from 'vue'

type Theme = 'light' | 'dark'

function getInitialTheme(): Theme {
  const saved = localStorage.getItem('theme')
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useTheme() {
  const theme = ref<Theme>(getInitialTheme())

  watch(theme, (val) => {
    document.documentElement.dataset.theme = val
    localStorage.setItem('theme', val)
  }, { immediate: true, flush: 'sync' })

  function toggle() {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }
  return { theme, toggle }
}
