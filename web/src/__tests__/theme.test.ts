import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, ref, watch, onMounted, nextTick } from 'vue'
import { useTheme, resetTheme } from '../composables/useTheme'

const mockDispose = vi.fn()
const mockSetOption = vi.fn()
const mockInit = vi.fn((_el: any) => ({ dispose: mockDispose, setOption: mockSetOption }))

vi.mock('echarts', () => ({
  init: (_el: any) => mockInit(_el),
}))

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    resetTheme()
    mockDispose.mockClear()
    mockSetOption.mockClear()
    mockInit.mockClear()
  })

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

  it('rebuilds ECharts on theme toggle', async () => {
    const chartRef = ref<HTMLElement>()
    let chartInstance: any = null

    const TestComponent = {
      setup() {
        const { theme } = useTheme()

        onMounted(() => {
          chartInstance = mockInit(chartRef.value)
          chartInstance.setOption({ series: [{ type: 'line', data: [1, 2, 3] }] })
        })

        watch(theme, () => {
          setTimeout(() => {
            chartInstance?.dispose()
            chartInstance = mockInit(chartRef.value)
            chartInstance.setOption({ series: [{ type: 'line', data: [1, 2, 3] }] })
          }, 60)
        })

        return { chartRef }
      },
      render() {
        return h('div', { ref: chartRef, style: 'width:400px;height:300px' })
      },
    }

    mount(TestComponent)
    await flushPromises()
    expect(mockInit).toHaveBeenCalledTimes(1)

    const { toggle } = useTheme()
    toggle()
    await new Promise(r => setTimeout(r, 100))
    await nextTick()
    await flushPromises()

    expect(mockDispose).toHaveBeenCalledTimes(1)
    expect(mockInit).toHaveBeenCalledTimes(2)
  })
})
