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
  it('消费 sessionStorage 预填并自动打开滚动前推表单（回测详情页关联发起）', async () => {
    sessionStorage.setItem('wf_prefill', JSON.stringify({
      portfolio_strategy_id: 1, name: '成长组合-WF', start_date: '2020-01-01',
      end_date: '2026-01-01', evaluation_profile: 'absolute',
    }))
    const w = mount(BacktestAnalysis)
    await flushPromises()
    expect(w.text()).toContain('发起滚动前推')
    const nameInput = w.findAll('input').find(i => i.attributes('placeholder') === '如 WF-A')!
    expect((nameInput.element as HTMLInputElement).value).toBe('成长组合-WF')
    expect(sessionStorage.getItem('wf_prefill')).toBeNull()
  })
})
