import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// LiveSessions.vue 用原生 axios(未走 ../api 客户端),mock 掉避免真实请求
const { axiosGet, axiosPost } = vi.hoisted(() => ({
  axiosGet: vi.fn(),
  axiosPost: vi.fn(),
}))
vi.mock('axios', () => ({
  default: { get: axiosGet, post: axiosPost },
}))

// B4b 工作台历史查询 + 新建实盘选组合走 ../api 客户端（import 的即 mock 的 vi.fn）
vi.mock('../api', () => ({
  getLivePositions: vi.fn(),
  getLiveOrders: vi.fn(),
  getLiveTrades: vi.fn(),
  getPortfolios: vi.fn(),
}))

// happy-dom 无 EventSource,用可手动 emit 的 fake
class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  close = vi.fn()
  onopen: (() => void) | null = null
  private listeners: Record<string, ((e: { data: string }) => void)[]> = {}
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
    // 真实 EventSource 连接成功后异步触发 onopen
    queueMicrotask(() => {
      if (this.onopen) this.onopen()
    })
  }
  addEventListener(type: string, cb: (e: { data: string }) => void) {
    if (!this.listeners[type]) this.listeners[type] = []
    this.listeners[type].push(cb)
  }
  emit(type: string, data: unknown) {
    ;(this.listeners[type] || []).forEach((cb) => cb({ data: JSON.stringify(data) }))
  }
}

import LiveSessions from '../views/LiveSessions.vue'
import { getLivePositions, getLiveOrders, getLiveTrades, getPortfolios } from '../api'

const runningSession = { id: 7, name: '模拟盘A', mode: 'simulation', status: 'running' }
const stoppedSession = { id: 8, name: '模拟盘B', mode: 'simulation', status: 'stopped' }

beforeEach(() => {
  vi.clearAllMocks()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  // 历史查询/组合默认空
  ;(getLivePositions as any).mockResolvedValue([])
  ;(getLiveOrders as any).mockResolvedValue([])
  ;(getLiveTrades as any).mockResolvedValue([])
  ;(getPortfolios as any).mockResolvedValue([])
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('LiveSessions.vue SSE 事件日志面板', () => {
  it('存在运行中 session 时自动连接其 SSE 流', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession, stoppedSession] } })
    const w = mount(LiveSessions)
    await flushPromises()

    expect(FakeEventSource.instances.length).toBe(1)
    expect(FakeEventSource.instances[0].url).toBe('/api/live/sessions/7/stream')
    expect(w.text()).toContain('已连接')
    w.unmount()
  })

  it('signal/order/risk 事件渲染为可读日志,ping 跳过', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    es.emit('signal', { signal_type: 'OPEN', signal_name: 'open_sig', stock_code: '600000.SH' })
    es.emit('order', { trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted', quantity: 100, price: 10.5 })
    es.emit('risk', { rule: 'max_drawdown', message: '最大回撤熔断触发（累计 1 次）' })
    es.emit('ping', { time: '2026-08-05T14:30:00' })
    await flushPromises()

    expect(w.text()).toContain('[OPEN] open_sig 600000.SH')
    expect(w.text()).toContain('买入 600000.SH 100股 @ 10.5')
    expect(w.text()).toContain('最大回撤熔断: 最大回撤熔断触发（累计 1 次）')
    // ping 不进日志
    expect(w.text()).not.toContain('14:30')
    w.unmount()
  })

  it('点停止 → 关闭事件流', async () => {
    axiosGet.mockResolvedValueOnce({ data: { data: [runningSession] } })
    axiosPost.mockResolvedValue({ data: { data: { id: 7, status: 'stopped' } } })
    // 停止后 load 返回 stopped,不再自动重连
    axiosGet.mockResolvedValueOnce({ data: { data: [stoppedSession] } })
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    const stopBtn = w.findAll('button').find((b) => b.text().includes('停止'))!
    await stopBtn.trigger('click')
    await flushPromises()

    expect(axiosPost).toHaveBeenCalledWith('/api/live/sessions/7/stop')
    expect(es.close).toHaveBeenCalled()
    expect(w.text()).toContain('未连接')
    w.unmount()
  })

  it('卸载时关闭事件流', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    w.unmount()
    expect(es.close).toHaveBeenCalled()
  })
})

describe('LiveSessions.vue 工作台(B4b)', () => {
  it('连接运行中 session → 加载三表历史', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    ;(getLivePositions as any).mockResolvedValue([
      { stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350 },
    ])
    ;(getLiveOrders as any).mockResolvedValue([
      { id: 3, stock_code: '600000.SH', trade_type: 'BUY', status: 'filled',
        quantity: 100, price: 10.5, filled_quantity: 100, filled_price: 10.5,
        error_message: null, bar_time: '2026-08-05T10:30:00' },
    ])
    ;(getLiveTrades as any).mockResolvedValue([
      { id: 1, stock_code: '600000.SH', trade_type: 'BUY', price: 10.5,
        quantity: 600, amount: 6300, trade_time: '2026-08-05T10:31:00' },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    expect(getLivePositions).toHaveBeenCalledWith(7)
    expect(getLiveOrders).toHaveBeenCalledWith(7)
    expect(getLiveTrades).toHaveBeenCalledWith(7)

    // 默认展示持仓表历史行
    expect(w.text()).toContain('600000.SH')
    expect(w.text()).toContain('700')
    w.unmount()
  })

  it('持仓 tab 切到委托 → 显示委托历史;切到成交 → 显示成交历史', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    ;(getLiveOrders as any).mockResolvedValue([
      { id: 3, stock_code: '000001.SZ', trade_type: 'SELL', status: 'submitted',
        quantity: 200, price: null, filled_quantity: 0, filled_price: null,
        error_message: null, bar_time: null },
    ])
    ;(getLiveTrades as any).mockResolvedValue([
      { id: 2, stock_code: '000001.SZ', trade_type: 'SELL', price: 11.0,
        quantity: 100, amount: 1100, trade_time: '2026-08-05T10:35:00' },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    const ordersTab = w.findAll('button').find((b) => b.text().includes('委托'))!
    await ordersTab.trigger('click')
    expect(w.text()).toContain('000001.SZ')
    expect(w.text()).toContain('已提交')

    const tradesTab = w.findAll('button').find((b) => b.text().includes('成交'))!
    await tradesTab.trigger('click')
    expect(w.text()).toContain('1100')
    w.unmount()
  })

  it('SSE position 事件 → 持仓表 upsert(新 code 追加/同 code 替换)', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    es.emit('position', { stock_code: '600000.SH', quantity: 600, avg_cost: 10.2, market_value: 6120 })
    await flushPromises()
    expect(w.text()).toContain('6120')

    // 同 code 新快照替换,不重复
    es.emit('position', { stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350 })
    await flushPromises()
    const cells = w.findAll('td').filter((c) => c.text() === '700')
    expect(cells.length).toBe(1)
    w.unmount()
  })

  it('SSE order/trade 事件 → 委托/成交表顶部插入', async () => {
    axiosGet.mockResolvedValue({ data: { data: [runningSession] } })
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    es.emit('order', { trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted', quantity: 100, price: 10.5 })
    es.emit('trade', { trade_type: 'BUY', stock_code: '600000.SH', price: 10.5, quantity: 100, amount: 1050 })
    await flushPromises()

    const ordersTab = w.findAll('button').find((b) => b.text().includes('委托'))!
    await ordersTab.trigger('click')
    expect(w.text()).toContain('买入')

    const tradesTab = w.findAll('button').find((b) => b.text().includes('成交'))!
    await tradesTab.trigger('click')
    expect(w.text()).toContain('1050')
    w.unmount()
  })
})

describe('LiveSessions.vue 新建实盘选组合', () => {
  const mockPortfolios = [
    { id: 1, name: '稳健组合' },
    { id: 2, name: '进取组合' },
  ]

  it('新建实盘弹窗可多选组合策略,提交带 portfolio_ids', async () => {
    axiosGet.mockResolvedValue({ data: { data: [stoppedSession] } })
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()
    const boxes = w.findAll('input[type="checkbox"]')
    expect(boxes.length).toBe(2)
    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('进取组合')

    await boxes[0].setValue(true)
    await boxes[1].setValue(true)
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(axiosPost).toHaveBeenCalledWith('/api/live/sessions', expect.objectContaining({
      name: '', mode: 'simulation', portfolio_ids: [1, 2],
    }))
    w.unmount()
  })

  it('未选组合直接提交 → 提示且不发请求', async () => {
    axiosGet.mockResolvedValue({ data: { data: [stoppedSession] } })
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    vi.stubGlobal('alert', vi.fn())
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(alert).toHaveBeenCalled()
    expect(axiosPost).not.toHaveBeenCalledWith('/api/live/sessions', expect.anything())
    vi.unstubAllGlobals()
    w.unmount()
  })

  it('会话列表显示所选组合名称(由 portfolio_ids 解析)', async () => {
    axiosGet.mockResolvedValue({
      data: { data: [{ id: 9, name: '组合盘', mode: 'live', status: 'stopped', portfolio_ids: [1, 2] }] },
    })
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    const w = mount(LiveSessions)
    await flushPromises()

    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('进取组合')
    w.unmount()
  })
})
