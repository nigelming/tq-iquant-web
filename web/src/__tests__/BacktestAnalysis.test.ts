import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import BacktestAnalysis from '../views/BacktestAnalysis.vue'

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getWalkForwardRuns: vi.fn().mockResolvedValue([]),
  getWalkForwardRun: vi.fn(),
  runWalkForward: vi.fn(),
  getParamPerturbRuns: vi.fn().mockResolvedValue([]),
  getParamPerturbRun: vi.fn(),
  runParamPerturb: vi.fn(),
}))

describe('BacktestAnalysis', () => {
  beforeEach(() => vi.clearAllMocks())
  it('默认渲染滚动前推 tab', async () => {
    const w = mount(BacktestAnalysis)
    await flushPromises()
    expect(w.text()).toContain('滚动前推')
    expect(w.text()).toContain('参数敏感度')
  })
  it('切到参数敏感度 tab', async () => {
    const w = mount(BacktestAnalysis)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('参数敏感度'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('扰动幅度')
  })
})
