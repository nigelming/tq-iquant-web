import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ParamPerturbPanel from '../views/ParamPerturbPanel.vue'

const { run } = vi.hoisted(() => ({
  run: {
    id: 2, portfolio_strategy_id: 1, name: 'PP-A', start_date: '2020-01-01', end_date: '2024-01-01',
    perturb_pct: 0.1, status: 'completed', error_message: null, created_at: 'x', completed_at: null,
    summary: {
      baseline: { record_id: 20, status: 'completed', annual_return: 0.11, total_return: 0.5, max_drawdown: 0.12, win_rate: 0.55, profit_factor: 1.6, sharpe_ratio: 1.2, total_trades: 40, error: null },
      rounds: [],
      params: [{ param: 'stop_loss_ratio', low: null, high: null, delta_cagr: 0.03 }],
      most_sensitive: 'stop_loss_ratio',
    },
  },
}))

vi.mock('../api', () => ({
  getPortfolios: vi.fn().mockResolvedValue([{ id: 1, name: '组合A' }]),
  getParamPerturbRuns: vi.fn().mockResolvedValue([run]),
  getParamPerturbRun: vi.fn().mockResolvedValue(run),
  runParamPerturb: vi.fn(),
}))

describe('ParamPerturbPanel', () => {
  beforeEach(() => vi.clearAllMocks())
  it('渲染列表与最敏感参数', async () => {
    const w = mount(ParamPerturbPanel)
    await flushPromises()
    expect(w.text()).toContain('PP-A')
  })
  it('查看详情渲染 baseline 与参数块', async () => {
    const w = mount(ParamPerturbPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('基准')
    expect(w.text()).toContain('stop_loss_ratio')
  })
})
