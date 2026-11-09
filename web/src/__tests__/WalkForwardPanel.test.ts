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

  it('详情给出结果判读与逐列读法说明', async () => {
    const w = mount(WalkForwardPanel)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('结果判读')
    expect(w.text()).toContain('训练段平均年化')
    expect(w.text()).toContain('样本外平均超额')
    expect(w.text()).toContain('全窗通过且样本外平均跑赢基准')
    expect(w.text()).toContain('OOS 年化')
    expect(w.text()).toContain('门槛比例')
  })

  async function openAndFill(w: ReturnType<typeof mount>) {
    ;(w.vm as unknown as { openForm: () => void }).openForm()
    await w.vm.$nextTick()
    const nameInput = w.findAll('input').find(i => i.attributes('placeholder') === '如 WF-A')!
    await nameInput.setValue('WF-B')
    const dates = w.findAll('input[type="date"]')
    await dates[0].setValue('2020-01-01')
    await dates[1].setValue('2024-01-01')  // 4 年 = train 3 + test 1，过完整窗口预检
  }

  it('提交遇并发 409 时提示后端中文 detail 而非 axios 原生英文', async () => {
    const { runWalkForward } = await import('../api')
    const err409 = Object.assign(new Error('Request failed with status code 409'), {
      response: { data: { detail: '回测正在进行中，请等待当前回测完成后再启动（已达并发上限）' } },
    })
    vi.mocked(runWalkForward).mockRejectedValueOnce(err409)
    const alerts: string[] = []
    vi.stubGlobal('alert', (m: string) => alerts.push(m))
    const w = mount(WalkForwardPanel, { props: { portfolios: [{ id: 1, name: '组合A' }] as never } })
    await flushPromises()
    await openAndFill(w)
    await w.find('.modal-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(alerts.some(a => a.includes('回测正在进行中'))).toBe(true)
    expect(alerts.some(a => a.includes('status code 409'))).toBe(false)
    vi.unstubAllGlobals()
  })

  it('提交进行中禁用确认按钮，重复点击仅发起一次', async () => {
    const { runWalkForward } = await import('../api')
    let resolveFn: (v: unknown) => void = () => {}
    vi.mocked(runWalkForward).mockImplementationOnce(
      () => new Promise((r) => { resolveFn = r }) as never,
    )
    const w = mount(WalkForwardPanel, { props: { portfolios: [{ id: 1, name: '组合A' }] as never } })
    await flushPromises()
    await openAndFill(w)
    const btn = () => w.find('.modal-actions .btn-primary')
    const alerts: string[] = []
    vi.stubGlobal('alert', (m: string) => alerts.push(m))
    await btn().trigger('click')
    expect(btn().attributes('disabled')).toBeDefined()
    expect(btn().text()).toContain('运行中')
    await btn().trigger('click')
    resolveFn({ status: 'completed', overall_pass: true, windows_passed: '1/1', error: null })
    await flushPromises()
    expect(vi.mocked(runWalkForward).mock.calls.length).toBe(1)
    vi.unstubAllGlobals()
  })

  it('openForm 支持预填（回测详情页关联发起）', async () => {
    const w = mount(WalkForwardPanel, { props: { portfolios: [{ id: 1, name: '组合A' }] as never } })
    await flushPromises()
    ;(w.vm as unknown as { openForm: (p?: Record<string, unknown>) => void }).openForm({
      portfolio_strategy_id: 1, name: '成长组合-WF', start_date: '2020-01-01',
      end_date: '2026-01-01', evaluation_profile: 'relative',
    })
    await w.vm.$nextTick()
    const nameInput = w.findAll('input').find(i => i.attributes('placeholder') === '如 WF-A')!
    expect((nameInput.element as HTMLInputElement).value).toBe('成长组合-WF')
    const dates = w.findAll('input[type="date"]')
    expect((dates[0].element as HTMLInputElement).value).toBe('2020-01-01')
    expect((dates[1].element as HTMLInputElement).value).toBe('2026-01-01')
    const selects = w.findAll('select')
    expect((selects[1].element as HTMLSelectElement).value).toBe('relative')
  })

  it('区间不足一个完整窗口时前端拦截并提示，不发起请求', async () => {
    const { runWalkForward } = await import('../api')
    const alerts: string[] = []
    vi.stubGlobal('alert', (m: string) => alerts.push(m))
    const w = mount(WalkForwardPanel, { props: { portfolios: [{ id: 1, name: '组合A' }] as never } })
    await flushPromises()
    ;(w.vm as unknown as { openForm: () => void }).openForm()
    await w.vm.$nextTick()
    const nameInput = w.findAll('input').find(i => i.attributes('placeholder') === '如 WF-A')!
    await nameInput.setValue('WF-C')
    const dates = w.findAll('input[type="date"]')
    await dates[0].setValue('2020-01-01')
    await dates[1].setValue('2022-06-01')  // < train 3 + test 1 = 4 年
    await w.find('.modal-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(alerts.some(a => a.includes('区间不足以构成一个完整窗口'))).toBe(true)
    expect(vi.mocked(runWalkForward).mock.calls.length).toBe(0)
    vi.unstubAllGlobals()
  })

  it('成功完成后弹出结果汇总反馈', async () => {
    const { runWalkForward } = await import('../api')
    vi.mocked(runWalkForward).mockResolvedValueOnce(
      { status: 'completed', overall_pass: true, windows_passed: '2/2', error: null } as never,
    )
    const alerts: string[] = []
    vi.stubGlobal('alert', (m: string) => alerts.push(m))
    const w = mount(WalkForwardPanel, { props: { portfolios: [{ id: 1, name: '组合A' }] as never } })
    await flushPromises()
    await openAndFill(w)
    await w.find('.modal-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(alerts.some(a => a.includes('验证完成：2/2') && a.includes('全窗通过'))).toBe(true)
    vi.unstubAllGlobals()
  })
})
