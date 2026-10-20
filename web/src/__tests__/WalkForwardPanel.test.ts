import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import WalkForwardPanel from '../views/WalkForwardPanel.vue'

const { runs } = vi.hoisted(() => ({
  runs: [{
    id: 1, portfolio_strategy_id: 1, name: 'WF-A', start_date: '2020-01-01', end_date: '2024-01-01',
    train_years: 3, test_years: 1, oos_gate_min_ratio: 0.5, status: 'completed', error_message: null,
    summary: { windows: [{ index: 1, train: { start: '2020-01-01', end: '2023-01-01' }, test: { start: '2023-01-01', end: '2024-01-01' }, record_id: 10, is_cagr: 0.12, oos_cagr: 0.08, oos_return: 0.09, oos_benchmark_return: 0.03, gate: { status: 'passed', reasons: [] } }], windows_passed: '1/1', overall_pass: true },
    created_at: '2026-01-01T00:00:00', completed_at: null,
  }],
}))

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getWalkForwardRuns: vi.fn().mockResolvedValue(runs),
  getWalkForwardRun: vi.fn().mockResolvedValue(runs[0]),
  runWalkForward: vi.fn(),
}))

describe('WalkForwardPanel', () => {
  beforeEach(() => vi.clearAllMocks())
  it('渲染运行列表与通过徽章', async () => {
    const w = mount(WalkForwardPanel)
    await flushPromises()
    expect(w.text()).toContain('WF-A')
    expect(w.text()).toContain('1/1')
  })
  it('点击查看展开窗口表', async () => {
    const w = mount(WalkForwardPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('IS年化')
  })
})
