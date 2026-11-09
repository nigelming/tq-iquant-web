import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ParamPerturbPanel from '../views/ParamPerturbPanel.vue'
import { runParamPerturb } from '../api'

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
  it('详情给出参数敏感度判读与档位说明', async () => {
    const w = mount(ParamPerturbPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('结果判读')
    expect(w.text()).toContain('最敏感参数')
    expect(w.text()).toContain('2~5pp')
    expect(w.text()).toContain('最大年化摆动')
  })

  async function openFilled() {
    const m = mount(ParamPerturbPanel, {
      props: { portfolios: [{ id: 1, name: '组合A' }] as never },
    })
    await flushPromises()
    ;(m.vm as unknown as { openForm: () => void }).openForm()
    await flushPromises()
    await m.find('input[placeholder="如 PP-A"]').setValue('PP-B')
    const dates = m.findAll('input[type="date"]')
    await dates[0].setValue('2020-01-01')
    await dates[1].setValue('2024-01-01')
    return { m, confirm: m.findAll('button').find(b => b.text().includes('确认'))! }
  }

  it('并发 409 时提示后端中文 detail，不裸露 axios 英文', async () => {
    const err409 = Object.assign(new Error('Request failed with status code 409'), {
      response: { data: { detail: '回测正在进行中，请等待当前回测完成后再启动（已达并发上限）' } },
    })
    vi.mocked(runParamPerturb).mockRejectedValueOnce(err409)
    const alerts: string[] = []
    vi.stubGlobal('alert', (m: unknown) => { alerts.push(String(m)) })
    const { confirm } = await openFilled()
    await confirm.trigger('click')
    await flushPromises()
    expect(alerts.some(a => a.includes('回测正在进行中'))).toBe(true)
    expect(alerts.some(a => a.includes('status code 409'))).toBe(false)
    vi.unstubAllGlobals()
  })

  it('执行中重复点击只提交一次（防自冲突 409）', async () => {
    let resolve!: (v: unknown) => void
    const pending = new Promise(r => { resolve = r })
    vi.mocked(runParamPerturb).mockReturnValueOnce(pending as never)
    const { confirm } = await openFilled()
    await confirm.trigger('click')
    await confirm.trigger('click')
    expect(runParamPerturb).toHaveBeenCalledTimes(1)
    expect(confirm.attributes('disabled')).toBeDefined()
    resolve({ id: 3, status: 'completed', error: null })
    await flushPromises()
  })
})
