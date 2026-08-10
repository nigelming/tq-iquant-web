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

const runningSession = { id: 7, name: '模拟盘A', mode: 'simulation', status: 'running' }
const stoppedSession = { id: 8, name: '模拟盘B', mode: 'simulation', status: 'stopped' }

beforeEach(() => {
  vi.clearAllMocks()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
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
