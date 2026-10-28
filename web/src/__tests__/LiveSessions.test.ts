import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// 会话 CRUD/启停 + 工作台历史查询 + 组合选单全走 ../api 客户端（import 的即 mock 的 vi.fn）
const { mockGetLiveSessions, mockCreateLiveSession, mockStartLiveSession, mockStopLiveSession, mockGetLiveChannel, mockTestLiveChannel } = vi.hoisted(() => ({
  mockGetLiveSessions: vi.fn(),
  mockCreateLiveSession: vi.fn(),
  mockStartLiveSession: vi.fn(),
  mockStopLiveSession: vi.fn(),
  mockGetLiveChannel: vi.fn(),
  mockTestLiveChannel: vi.fn(),
}))
vi.mock('../api', () => ({
  getLiveSessions: mockGetLiveSessions,
  createLiveSession: mockCreateLiveSession,
  startLiveSession: mockStartLiveSession,
  stopLiveSession: mockStopLiveSession,
  getLiveChannel: mockGetLiveChannel,
  testLiveChannel: mockTestLiveChannel,
  getLivePositions: vi.fn(),
  getLiveOrders: vi.fn(),
  getLiveTrades: vi.fn(),
  getLiveDecisions: vi.fn(),
  getLiveSignals: vi.fn(),
  getPortfolios: vi.fn(),
  sendUserBlock: vi.fn(),
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
import { getLiveSessions, createLiveSession, startLiveSession, stopLiveSession, getLivePositions, getLiveOrders, getLiveTrades, getLiveDecisions, getLiveSignals, getPortfolios, getLiveChannel, testLiveChannel, sendUserBlock } from '../api'
import { deriveBlockCode } from '../utils/blockCode'

const runningSession = { id: 7, name: '模拟盘A', mode: 'simulation', status: 'running', started_at: null, stopped_at: null, portfolio_ids: [] }
const stoppedSession = { id: 8, name: '模拟盘B', mode: 'simulation', status: 'stopped', started_at: null, stopped_at: null, portfolio_ids: [] }

beforeEach(() => {
  vi.clearAllMocks()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  // 历史查询/组合默认空
  ;(getLiveSessions as any).mockResolvedValue([])
  ;(createLiveSession as any).mockResolvedValue({ id: 99, status: 'stopped' })
  ;(startLiveSession as any).mockResolvedValue({ id: 7, status: 'running' })
  ;(stopLiveSession as any).mockResolvedValue({ id: 7, status: 'stopped' })
  ;(getLivePositions as any).mockResolvedValue([])
  ;(getLiveOrders as any).mockResolvedValue([])
  ;(getLiveTrades as any).mockResolvedValue([])
  ;(getLiveDecisions as any).mockResolvedValue({ summary: [], events: [] })
  ;(getLiveSignals as any).mockResolvedValue({ items: [], total: 0 })
  ;(getPortfolios as any).mockResolvedValue([])
  ;(getLiveChannel as any).mockResolvedValue({
    engine: 'iquant',
    active: 'iquant_bridge',
    label: 'iQuant HTTP 桥',
    online: null,
    engines: [
      { key: 'iquant', channel: 'iquant_bridge', label: 'iQuant HTTP 桥', configured: true, detail: '' },
      { key: 'tdx', channel: 'tdx_tq', label: '通达信 TQ 交易', configured: true, detail: '' },
      { key: 'ths', channel: 'ths_ui', label: '同花顺独立交易（UI 自动化）', configured: true, detail: '' },
      { key: 'qmt', channel: 'qmt', label: 'miniQMT 交易', configured: false, detail: '未配置 QMT 路径/资金账号' },
    ],
    ths_ui: { enabled: false, available: false, window_title: '网上股票交易系统5.0', client_exe_path: '', driver: 'clipboard' },
    bridge: { simulation: 'http://127.0.0.1:8790', live: 'http://127.0.0.1:8791' },
  })
  ;(testLiveChannel as any).mockResolvedValue({ ok: true, channel: 'iquant_bridge', base_url: 'http://127.0.0.1:8790' })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('LiveSessions.vue SSE 事件日志面板', () => {
  it('存在运行中 session 时自动连接其 SSE 流', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession, stoppedSession])
    const w = mount(LiveSessions)
    await flushPromises()

    expect(FakeEventSource.instances.length).toBe(1)
    expect(FakeEventSource.instances[0].url).toBe('/api/live/sessions/7/stream')
    expect(w.text()).toContain('已连接')
    w.unmount()
  })

  it('signal/order/risk 事件渲染为可读日志,ping 跳过', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
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
    ;(getLiveSessions as any).mockResolvedValueOnce([runningSession])
    ;(stopLiveSession as any).mockResolvedValue({ id: 7, status: 'stopped' })
    // 停止后 load 返回 stopped,不再自动重连
    ;(getLiveSessions as any).mockResolvedValueOnce([stoppedSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    const stopBtn = w.findAll('button').find((b) => b.text().includes('停止'))!
    await stopBtn.trigger('click')
    await flushPromises()

    expect(stopLiveSession).toHaveBeenCalledWith(7)
    expect(es.close).toHaveBeenCalled()
    expect(w.text()).toContain('未连接')
    w.unmount()
  })

  it('卸载时关闭事件流', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    w.unmount()
    expect(es.close).toHaveBeenCalled()
  })
})

describe('LiveSessions.vue 工作台(B4b)', () => {
  it('连接运行中 session → 加载三表历史', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    ;(getLivePositions as any).mockResolvedValue([
      { stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350, portfolio_id: 1, strategy_id: 1 },
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
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
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
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
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

  it('持仓表显示序号 + 组合/子策略名,同票多子策略分多行', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    ;(getLivePositions as any).mockResolvedValue([
      { stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000, portfolio_id: 1, strategy_id: 1 },
      { stock_code: '600000.SH', quantity: 200, avg_cost: 12, market_value: 2400, portfolio_id: 1, strategy_id: 2 },
    ])
    ;(getPortfolios as any).mockResolvedValue([
      { id: 1, name: '稳健组合', strategies: [
        { id: 1, name: '日线趋势', portfolio_id: 1 },
        { id: 2, name: '周线低吸', portfolio_id: 1 },
      ] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    // 表头含序号列
    expect(w.text()).toContain('序号')
    // 组合/子策略名
    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('日线趋势')
    expect(w.text()).toContain('周线低吸')
    // 同票两行：代码单元格出现两次
    const codeCells = w.findAll('td').filter((c) => c.text() === '600000.SH')
    expect(codeCells.length).toBe(2)
    w.unmount()
  })

  it('SSE 同票不同子策略 position 事件 → 两行独立 upsert', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]

    es.emit('position', { portfolio_id: 1, strategy_id: 1, stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000 })
    es.emit('position', { portfolio_id: 1, strategy_id: 2, stock_code: '600000.SH', quantity: 200, avg_cost: 12, market_value: 2400 })
    await flushPromises()

    const codeCells = w.findAll('td').filter((c) => c.text() === '600000.SH')
    expect(codeCells.length).toBe(2)
    expect(w.text()).toContain('3000')
    expect(w.text()).toContain('2400')
    w.unmount()
  })

  it('SSE order/trade 事件 → 委托/成交表顶部插入', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
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

  it('决策/拦截 tab → 加载历史聚合+逐事件,SSE decision 实时插入', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    ;(getLiveDecisions as any).mockResolvedValue({
      summary: [
        { gate: 'insufficient_funds', layer: 'capital_gate', action: 'reject',
          param_name: 'cash', param_value: null, count: 3,
          first_bar_time: '2026-08-28T10:00:00', last_bar_time: '2026-08-28T13:00:00',
          stock_count: 2, requested_qty_sum: 2000, final_qty_sum: 0 },
      ],
      events: [
        { id: 1, gate: 'insufficient_funds', layer: 'capital_gate', action: 'reject',
          stock_code: '000001.SZ', strategy_id: 1, bar_time: '2026-08-28T10:00:00',
          param_name: 'cash', param_value: null, actual_value: null,
          requested_qty: 1000, final_qty: 0, message: '开仓资金不足1手' },
      ],
    })
    const w = mount(LiveSessions)
    await flushPromises()
    expect(getLiveDecisions).toHaveBeenCalledWith(7)

    const decTab = w.findAll('button').find((b) => b.text().includes('决策/拦截'))!
    await decTab.trigger('click')

    // 聚合行：闸门中文 + 次数
    expect(w.text()).toContain('资金不足拒单')
    expect(w.text()).toContain('开仓资金不足1手')
    // 历史逐事件行
    expect(w.text()).toContain('000001.SZ')

    // SSE 实时 decision 事件插入
    const es = FakeEventSource.instances[0]
    es.emit('decision', { gate: 'max_positions_full', layer: 'signal_gate', action: 'block',
      stock_code: '600000.SH', strategy_id: null, param_name: 'max_positions',
      param_value: 5, actual_value: 5, requested_qty: null, final_qty: null,
      message: '持股数已达上限 5' })
    await flushPromises()
    expect(w.text()).toContain('持股数达上限')
    expect(w.text()).toContain('600000.SH')
    w.unmount()
  })
})

describe('LiveSessions.vue 新建实盘选组合', () => {
  const mockPortfolios = [
    { id: 1, name: '稳健组合' },
    { id: 2, name: '进取组合' },
  ]

  it('新建实盘弹窗可多选组合策略,提交带 portfolio_ids', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
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

    expect(createLiveSession).toHaveBeenCalledWith(expect.objectContaining({
      name: '', mode: 'simulation', portfolio_ids: [1, 2],
    }))
    w.unmount()
  })

  it('选实盘模式提交 → 带 mode: live', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()
    // 切到实盘 option
    const modeSelect = w.find('select')
    await modeSelect.setValue('live')
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[0].setValue(true)
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(createLiveSession).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'live', portfolio_ids: [1],
    }))
    w.unmount()
  })

  it('未选组合直接提交 → 提示且不发请求', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    vi.stubGlobal('alert', vi.fn())
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(alert).toHaveBeenCalled()
    expect(createLiveSession).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
    w.unmount()
  })

  it('会话列表显示所选组合名称(由 portfolio_ids 解析)', async () => {
    ;(getLiveSessions as any).mockResolvedValue([
      { id: 9, name: '组合盘', mode: 'live', status: 'stopped', started_at: null, stopped_at: null, portfolio_ids: [1, 2] },
    ])
    ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
    const w = mount(LiveSessions)
    await flushPromises()

    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('进取组合')
    w.unmount()
  })

  it('启动失败(桥未启动) → alert 告警且不连接流', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(startLiveSession as any).mockRejectedValue(new Error('桥未启动：仿真（http://127.0.0.1:8790）— 请先在 iQuant 客户端加载并运行对应桥策略'))
    vi.stubGlobal('alert', vi.fn())
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('启动'))!.trigger('click')
    await flushPromises()

    expect(startLiveSession).toHaveBeenCalledWith(stoppedSession.id)
    expect(alert).toHaveBeenCalledWith(expect.stringContaining('桥未启动'))
    // 失败不连流
    expect(FakeEventSource.instances.length).toBe(0)
    vi.unstubAllGlobals()
    w.unmount()
  })
})

describe('LiveSessions.vue 实盘资金基准（live_capital）', () => {
  it('新建弹窗渲染「实盘资金基准（元）」number 输入 + 占位符 + 说明', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue([{ id: 1, name: '稳健组合' }])
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()

    const capInput = w.findAll('input').find((i) => (i.element as HTMLInputElement).type === 'number')
    expect(capInput).toBeTruthy()
    expect((capInput!.element as HTMLInputElement).placeholder).toContain('留空 = 用组合初始资金')
    expect(w.text()).toContain('实盘资金基准（元）')
    expect(w.text()).toContain('比率配置等比缩放')
    expect(w.text()).toContain('多组合各自独立记账')
    w.unmount()
  })

  it('填写 live_capital 提交 → 透传数值', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue([{ id: 1, name: '稳健组合' }])
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()
    const capInput = w.findAll('input').find((i) => (i.element as HTMLInputElement).type === 'number')!
    await capInput.setValue('1000000')
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[0].setValue(true)
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(createLiveSession).toHaveBeenCalledWith(expect.objectContaining({
      name: '', mode: 'simulation', portfolio_ids: [1], live_capital: 1000000,
    }))
    w.unmount()
  })

  it('留空 live_capital 提交 → null（用组合初始资金）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue([{ id: 1, name: '稳健组合' }])
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[0].setValue(true)
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()

    expect(createLiveSession).toHaveBeenCalledWith(expect.objectContaining({
      portfolio_ids: [1], live_capital: null,
    }))
    w.unmount()
  })
})

describe('LiveSessions.vue 执行通道条', () => {
  it('显示当前引擎并支持只读测试连接', async () => {
    ;(getLiveChannel as any).mockResolvedValue({
      engine: 'ths',
      active: 'ths_ui',
      label: '同花顺独立交易（UI 自动化）',
      online: null,
      engines: [
        { key: 'iquant', channel: 'iquant_bridge', label: 'iQuant HTTP 桥', configured: true, detail: '' },
        { key: 'tdx', channel: 'tdx_tq', label: '通达信 TQ 交易', configured: true, detail: '' },
        { key: 'ths', channel: 'ths_ui', label: '同花顺独立交易（UI 自动化）', configured: true, detail: '' },
        { key: 'qmt', channel: 'qmt', label: 'miniQMT 交易', configured: false, detail: '未安装 xtquant' },
      ],
      ths_ui: { enabled: true, available: true, window_title: '网上股票交易系统5.0', client_exe_path: 'D:/xiadan.exe', driver: 'clipboard' },
      bridge: { simulation: 'http://127.0.0.1:8790', live: 'http://127.0.0.1:8791' },
    })
    ;(testLiveChannel as any).mockResolvedValue({ ok: true, channel: 'ths_ui', positions: 2 })
    const w = mount(LiveSessions)
    await flushPromises()

    expect(getLiveChannel).toHaveBeenCalled()
    expect(w.text()).toContain('同花顺独立交易（UI 自动化）')
    // 四个引擎全部可见，未配置的引擎带（未配置）标记
    expect(w.text()).toContain('miniQMT 交易（未配置）')

    await w.findAll('button').find((b) => b.text().includes('测试连接'))!.trigger('click')
    await flushPromises()

    expect(testLiveChannel).toHaveBeenCalledWith('ths_ui')
    expect(w.text()).toContain('已连接，读到 2 条持仓')
    w.unmount()
  })

  it('运行中会话按 session.engine 拉取通道状态', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, engine: 'tdx' }])
    const w = mount(LiveSessions)
    await flushPromises()

    expect(getLiveChannel).toHaveBeenCalledWith('tdx')
    w.unmount()
  })

  it('依赖未安装时以（未配置）红标提示', async () => {
    ;(getLiveChannel as any).mockResolvedValue({
      engine: 'iquant',
      active: 'iquant_bridge',
      label: 'iQuant HTTP 桥',
      online: null,
      engines: [
        { key: 'iquant', channel: 'iquant_bridge', label: 'iQuant HTTP 桥', configured: true, detail: '' },
        { key: 'ths', channel: 'ths_ui', label: '同花顺独立交易（UI 自动化）', configured: false, detail: '依赖未安装（pywinauto/pyperclip）' },
      ],
      ths_ui: { enabled: true, available: false, window_title: '网上股票交易系统5.0', client_exe_path: '', driver: 'clipboard' },
      bridge: { simulation: 'http://127.0.0.1:8790', live: 'http://127.0.0.1:8791' },
    })
    const w = mount(LiveSessions)
    await flushPromises()

    expect(w.text()).toContain('iQuant HTTP 桥')
    expect(w.text()).toContain('同花顺独立交易（UI 自动化）（未配置）')
    w.unmount()
  })
})

describe('LiveSessions.vue 执行模式 + 预警信号', () => {
  it('会话列表按 execution_mode 显示「预警/自动」，alert 会话引擎列显示 —', async () => {
    ;(getLiveSessions as any).mockResolvedValue([
      { id: 11, name: '预警盘', mode: 'live', status: 'stopped', engine: 'iquant', execution_mode: 'alert', started_at: null, stopped_at: null, portfolio_ids: [] },
      { id: 12, name: '自动盘', mode: 'live', status: 'stopped', engine: 'tdx', execution_mode: 'auto', started_at: null, stopped_at: null, portfolio_ids: [] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    expect(rows.length).toBe(2)
    expect(rows[0].text()).toContain('预警')
    expect(rows[0].text()).not.toContain('iQuant 桥')
    expect(rows[1].text()).toContain('自动')
    expect(rows[1].text()).toContain('通达信')
    w.unmount()
  })

  it('新建表单默认 alert，切到 auto 才显示交易引擎 select；提交带 execution_mode', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue([
      { id: 1, name: '稳健组合', strategies: [] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()

    // 默认 alert：只有 名称/模式/执行模式 三个 select 区域，无引擎 select
    const execSelect = w.findAll('select').find((s) => s.html().includes('预警（半自动'))
    expect(execSelect).toBeTruthy()
    expect((execSelect!.element as HTMLSelectElement).value).toBe('alert')
    // alert 模式下不渲染交易引擎 select
    expect(w.findAll('select').some((s) => s.html().includes('iQuant HTTP 桥'))).toBe(false)

    // 切到 auto：出现交易引擎 select
    await execSelect!.setValue('auto')
    await flushPromises()
    expect(w.findAll('select').some((s) => s.html().includes('iQuant HTTP 桥'))).toBe(true)

    // 提交：带 execution_mode=auto
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[0].setValue(true)
    await w.findAll('button').find((b) => b.text().includes('确认'))!.trigger('click')
    await flushPromises()
    expect(createLiveSession).toHaveBeenCalledWith(expect.objectContaining({
      execution_mode: 'auto', portfolio_ids: [1],
    }))
    w.unmount()
  })

  it('新建实盘切 auto：引擎默认跟随后端 default_engine（通道条 engine）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession])
    ;(getPortfolios as any).mockResolvedValue([
      { id: 1, name: '稳健组合', strategies: [] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('新建实盘'))!.trigger('click')
    await flushPromises()
    const execSelect = w.findAll('select').find((s) => s.html().includes('预警（半自动'))
    await execSelect!.setValue('auto')
    await flushPromises()

    // beforeEach mock 的 channel.engine='iquant' → 引擎 select 默认选中 iQuant（而非硬编码）
    const engineSelect = w.findAll('select').find((s) => s.html().includes('iQuant HTTP 桥'))
    expect((engineSelect!.element as HTMLSelectElement).value).toBe('iquant')
    w.unmount()
  })

  it('通道条消重：默认引擎徽章只出现一次，前缀为「默认引擎」', async () => {
    const w = mount(LiveSessions)
    await flushPromises()

    // beforeEach mock engine='iquant' 且 engines 清单含同名项 → 消重后首徽章唯一
    const badges = w.find('.channel-bar').findAll('.badge').map((b) => b.text())
    expect(badges.filter((t) => t === 'iQuant HTTP 桥').length).toBe(1)
    expect(w.text()).toContain('默认引擎')
    w.unmount()
  })

  it('运行中会话时通道条前缀切为「运行中通道」且同样消重', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, engine: 'tdx' }])
    ;(getLiveChannel as any).mockResolvedValue({
      engine: 'tdx',
      active: 'tdx_tq',
      label: '通达信 TQ 交易',
      online: true,
      engines: [
        { key: 'tdx', channel: 'tdx_tq', label: '通达信 TQ 交易', configured: true, detail: '' },
        { key: 'iquant', channel: 'iquant_bridge', label: 'iQuant HTTP 桥', configured: true, detail: '' },
      ],
      ths_ui: { enabled: false, available: false, window_title: '', client_exe_path: '', driver: 'clipboard' },
      bridge: { simulation: '', live: '' },
    })
    const w = mount(LiveSessions)
    await flushPromises()

    expect(w.text()).toContain('运行中通道')
    const badges = w.find('.channel-bar').findAll('.badge').map((b) => b.text())
    expect(badges.filter((t) => t === '通达信 TQ 交易').length).toBe(1)
    w.unmount()
  })

  it('预警信号 tab → 加载信号列表，显示 alert_status/alert_error', async () => {
    ;(getLiveSessions as any).mockResolvedValue([
      { ...runningSession, execution_mode: 'alert' },
    ])
    ;(getLiveSignals as any).mockResolvedValue({
      total: 2,
      items: [
        {
          id: 1, portfolio_id: 1, strategy_id: 10, stock_code: '600000.SH', period: '1d',
          bar_time: '2026-10-26T15:00:00', formula_name: 'MACD金叉', signal_name: 'BUY',
          signal_type: 'OPEN', trigger_value: 1, price: '10.50',
          execution_mode: 'alert', status: 'new',
          alert_status: 'sent', alert_error: null, tdx_run_id: 'R123',
          created_at: '2026-10-26T15:00:01', alerted_at: '2026-10-26T15:00:02',
        },
        {
          id: 2, portfolio_id: 1, strategy_id: 10, stock_code: '000001.SZ', period: '1d',
          bar_time: '2026-10-26T15:00:00', formula_name: 'MACD金叉', signal_name: 'SELL',
          signal_type: 'CLOSE', trigger_value: -1, price: '9.80',
          execution_mode: 'alert', status: 'new',
          alert_status: 'failed', alert_error: 'send_warn 返回 ErrorId=1001', tdx_run_id: null,
          created_at: '2026-10-26T15:00:01', alerted_at: null,
        },
      ],
    })
    ;(getPortfolios as any).mockResolvedValue([
      { id: 1, name: '稳健组合', strategies: [{ id: 10, name: '日线趋势' }] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()

    // 切到「预警信号」tab
    const tab = w.findAll('button').find((b) => b.text() === '预警信号')
    expect(tab).toBeTruthy()
    await tab!.trigger('click')
    await flushPromises()

    expect(getLiveSignals).toHaveBeenCalledWith(7, expect.objectContaining({ limit: 200 }))
    expect(w.text()).toContain('600000.SH')
    expect(w.text()).toContain('MACD金叉')
    expect(w.text()).toContain('已发送')
    expect(w.text()).toContain('发送失败')
    expect(w.text()).toContain('send_warn 返回 ErrorId=1001')
    // 组合/子策略名解析
    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('日线趋势')
    w.unmount()
  })

  it('预警信号 tab 空态：无信号时给出口径说明（audit I3 准确文案）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([
      { ...runningSession, execution_mode: 'alert' },
    ])
    ;(getLiveSignals as any).mockResolvedValue({ items: [], total: 0 })
    const w = mount(LiveSessions)
    await flushPromises()

    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('暂无预警信号')
    expect(w.text()).toContain('send_warn')
    // 准确口径：alert 落库 + send_warn 推送；auto 默认同样推送（审计旁路），均落库
    expect(w.text()).toContain('审计旁路')
    expect(w.text()).toContain('落库')
    // 旧失实文案（auto 仅记录不上送）不再出现
    expect(w.text()).not.toContain('仅记录信号不上送')
    w.unmount()
  })
})

describe('LiveSessions.vue SSE signal → 预警信号 tab 节流重拉', () => {
  afterEach(() => { vi.useRealTimers() })

  it('SSE signal 事件 → 2s 节流后重拉 getLiveSignals 更新 tab（payload 不直接映射成行）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, execution_mode: 'alert' }])
    ;(getLiveSignals as any).mockResolvedValue({ items: [], total: 0 })
    ;(getPortfolios as any).mockResolvedValue([
      { id: 1, name: '稳健组合', strategies: [{ id: 10, name: '日线趋势' }] },
    ])
    const w = mount(LiveSessions)
    await flushPromises()
    // 初始 loadHistory 拉一次
    expect(getLiveSignals).toHaveBeenCalledTimes(1)

    // 只 fake setTimeout/clearTimeout（节流 timer）；flushPromises 走 setImmediate 不受影响
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    ;(getLiveSignals as any).mockResolvedValue({
      total: 1,
      items: [{
        id: 5, portfolio_id: 1, strategy_id: 10, stock_code: '600519.SH', period: '1d',
        bar_time: '2026-10-27T10:30:00', formula_name: 'MACD金叉', signal_name: 'BUY',
        signal_type: 'OPEN', trigger_value: 1, price: '1700.00',
        execution_mode: 'alert', status: 'new',
        alert_status: 'sent', alert_error: null, tdx_run_id: null,
        created_at: '2026-10-27T10:30:01', alerted_at: '2026-10-27T10:30:02',
      }],
    })

    // SSE payload 是精简形状（signal_id 而非 id）→ 只触发重拉，不直接进表
    const es = FakeEventSource.instances[0]
    es.emit('signal', { signal_id: 5, signal_type: 'OPEN', stock_code: '600519.SH' })
    await flushPromises()
    // 节流窗口内不重拉
    expect(getLiveSignals).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(2000)
    await flushPromises()
    expect(getLiveSignals).toHaveBeenCalledTimes(2)
    expect(getLiveSignals).toHaveBeenLastCalledWith(7, { limit: 200 })

    // tab 数据来自重拉结果（完整列形状），非 SSE 精简 payload
    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('600519.SH')
    expect(w.text()).toContain('MACD金叉')
    w.unmount()
  })

  it('节流窗口内多条 signal 事件 → 合并为一次重拉', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const es = FakeEventSource.instances[0]
    es.emit('signal', { signal_id: 1 })
    es.emit('signal', { signal_id: 2 })
    es.emit('signal', { signal_id: 3 })
    vi.advanceTimersByTime(2000)
    await flushPromises()
    // 初始 loadHistory 1 次 + 节流合并后 1 次
    expect(getLiveSignals).toHaveBeenCalledTimes(2)
    w.unmount()
  })
})

describe('LiveSessions.vue loadHistory 与 SSE 竞态补拉（audit C1）', () => {
  /** 让 getLivePositions 首次调用挂起，返回放行函数（模拟历史请求在途窗口）。
   *  先 mockReset 清掉前一测试可能残留的 once 实现（避免 once 队列串测试），再重建默认。 */
  function deferPositions(): () => void {
    let resolve!: (v: unknown) => void
    const m = getLivePositions as any
    m.mockReset()
    m.mockResolvedValue([])
    m.mockImplementationOnce(() => new Promise((res) => { resolve = res }))
    return () => resolve([])
  }

  it('五请求在途期间 SSE 推过 position/order/trade → resolve 后补拉一次三表历史', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const resolvePositions = deferPositions()
    // 补拉（第二次调用）返回最新持仓，验证补拉结果落到工作台
    ;(getLivePositions as any).mockResolvedValueOnce([
      { stock_code: '600519.SH', quantity: 900, avg_cost: 10, market_value: 9000, portfolio_id: 1, strategy_id: 1 },
    ])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]
    es.emit('order', { trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted', quantity: 100, price: 10.5 })
    es.emit('trade', { trade_type: 'BUY', stock_code: '600000.SH', price: 10.5, quantity: 100, amount: 1050 })
    es.emit('position', { stock_code: '600000.SH', quantity: 100, avg_cost: 10, market_value: 1000 })
    await flushPromises()
    resolvePositions()
    await flushPromises()

    expect(getLivePositions).toHaveBeenCalledTimes(2)
    expect(getLiveOrders).toHaveBeenCalledTimes(2)
    expect(getLiveTrades).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('600519.SH')
    w.unmount()
  })

  it('在途期间仅 decision/signal 事件 → 不补拉（计数只算 position/order/trade）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const resolvePositions = deferPositions()
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]
    es.emit('decision', { gate: 'max_positions_full', layer: 'signal_gate', action: 'block', stock_code: '600000.SH', message: 'x' })
    es.emit('signal', { signal_id: 1, signal_type: 'OPEN', stock_code: '600000.SH' })
    await flushPromises()
    resolvePositions()
    await flushPromises()

    expect(getLivePositions).toHaveBeenCalledTimes(1)
    expect(getLiveOrders).toHaveBeenCalledTimes(1)
    expect(getLiveTrades).toHaveBeenCalledTimes(1)
    w.unmount()
  })

  it('在途期间无 SSE 事件 → 不补拉', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const resolvePositions = deferPositions()
    const w = mount(LiveSessions)
    await flushPromises()
    resolvePositions()
    await flushPromises()

    expect(getLivePositions).toHaveBeenCalledTimes(1)
    expect(getLiveOrders).toHaveBeenCalledTimes(1)
    expect(getLiveTrades).toHaveBeenCalledTimes(1)
    w.unmount()
  })
})

describe('LiveSessions.vue SSE 心跳流升级重接（D-5）', () => {
  it('收到 reconnect 事件 → 关闭当前心跳流并重接同一 session 真流，历史重拉', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]
    expect(getLivePositions).toHaveBeenCalledTimes(1) // 初始 loadHistory

    es.emit('reconnect', { session_id: 7 })
    await flushPromises()

    expect(es.close).toHaveBeenCalled()
    expect(FakeEventSource.instances.length).toBe(2)
    expect(FakeEventSource.instances[1].url).toBe('/api/live/sessions/7/stream')
    // startEventStream 末尾 loadHistory 重拉
    expect(getLivePositions).toHaveBeenCalledTimes(2)
    w.unmount()
  })

  it('切换会话后旧流迟到的 reconnect → 不重接旧 session（防循环/串台）', async () => {
    ;(getLiveSessions as any).mockResolvedValueOnce([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es7 = FakeEventSource.instances[0]

    // 停止 7 → load() 切到外部已启动的 8
    ;(stopLiveSession as any).mockResolvedValue({ id: 7, status: 'stopped' })
    ;(getLiveSessions as any).mockResolvedValueOnce([
      { id: 8, name: '模拟盘B', mode: 'simulation', status: 'running', started_at: null, stopped_at: null, portfolio_ids: [] },
    ])
    const stopBtn = w.findAll('button').find((b) => b.text().includes('停止'))!
    await stopBtn.trigger('click')
    await flushPromises()
    expect(FakeEventSource.instances.length).toBe(2)

    // 旧流（7）迟到的 reconnect：当前绑定已是 8 → 忽略
    es7.emit('reconnect', { session_id: 7 })
    await flushPromises()
    expect(FakeEventSource.instances.length).toBe(2)
    expect(FakeEventSource.instances[1].url).toBe('/api/live/sessions/8/stream')
    w.unmount()
  })
})

describe('LiveSessions.vue 会话切换清理 + 停止时序 + 重连状态（audit M3）', () => {
  it('停止会话：先调 stop API 成功后再关事件流（接收清扫 canceled 终态）', async () => {
    const order: string[] = []
    ;(getLiveSessions as any).mockResolvedValueOnce([runningSession])
    ;(stopLiveSession as any).mockImplementation(async () => {
      order.push('api')
      return { id: 7, status: 'stopped' }
    })
    ;(getLiveSessions as any).mockResolvedValueOnce([stoppedSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es = FakeEventSource.instances[0]
    ;(es as any).close = vi.fn(() => { order.push('close') })

    const stopBtn = w.findAll('button').find((b) => b.text().includes('停止'))!
    await stopBtn.trigger('click')
    await flushPromises()

    expect(order).toEqual(['api', 'close'])
    w.unmount()
  })

  it('换会话订阅前清空工作台与事件日志（不与上一会话混排）', async () => {
    ;(getLiveSessions as any).mockResolvedValueOnce([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    const es7 = FakeEventSource.instances[0]
    es7.emit('position', { stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350 })
    es7.emit('order', { trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted', quantity: 100, price: 10.5 })
    es7.emit('trade', { trade_type: 'BUY', stock_code: '600000.SH', price: 10.5, quantity: 100, amount: 1050 })
    es7.emit('decision', { gate: 'max_positions_full', layer: 'signal_gate', action: 'block', stock_code: '600000.SH', message: 'x' })
    await flushPromises()
    expect(w.text()).toContain('600000.SH')

    // 停止 7 → load() 发现 8 已在外部启动为 running → 自动切到 8 的流
    ;(stopLiveSession as any).mockResolvedValue({ id: 7, status: 'stopped' })
    ;(getLiveSessions as any).mockResolvedValueOnce([
      { id: 8, name: '模拟盘B', mode: 'simulation', status: 'running', started_at: null, stopped_at: null, portfolio_ids: [] },
    ])
    const stopBtn = w.findAll('button').find((b) => b.text().includes('停止'))!
    await stopBtn.trigger('click')
    await flushPromises()

    expect(FakeEventSource.instances.length).toBe(2)
    expect(FakeEventSource.instances[1].url).toBe('/api/live/sessions/8/stream')
    // A 会话内容已清空：工作台/事件日志不再混排
    expect(w.text()).not.toContain('600000.SH')
    expect(w.text()).not.toContain('7350')
    expect(w.text()).toContain('暂无持仓')
    w.unmount()
  })

  it('SSE onerror → 状态显示「连接中」（EventSource 自动重连中，非 closed）', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession])
    const w = mount(LiveSessions)
    await flushPromises()
    expect(w.text()).toContain('已连接')
    const es = FakeEventSource.instances[0] as any
    es.onerror()
    await flushPromises()
    expect(w.text()).toContain('连接中')
    expect(w.text()).not.toContain('未连接')
    w.unmount()
  })
})

describe('LiveSessions.vue B6 全局限 1 UI 预防（audit M6）', () => {
  it('存在运行中会话时，其它行的启动按钮 disabled 且 title 提示全局限 1', async () => {
    ;(getLiveSessions as any).mockResolvedValue([runningSession, stoppedSession])
    const w = mount(LiveSessions)
    await flushPromises()

    const startBtn = w.findAll('button').find((b) => b.text() === '启动')!
    expect(startBtn.attributes('disabled')).toBeDefined()
    expect(startBtn.attributes('title')).toContain('已有运行中会话')
    w.unmount()
  })

  it('无运行中会话时启动按钮正常可用', async () => {
    ;(getLiveSessions as any).mockResolvedValue([stoppedSession, { ...stoppedSession, id: 9, name: '模拟盘C' }])
    const w = mount(LiveSessions)
    await flushPromises()

    const startBtn = w.findAll('button').find((b) => b.text() === '启动')!
    expect(startBtn.attributes('disabled')).toBeUndefined()
    w.unmount()
  })
})

describe('LiveSessions.vue 预警信号同步自选（1.3.1）', () => {
  const signalTabItems = {
    total: 3,
    items: [
      { id: 1, portfolio_id: 1, strategy_id: 10, stock_code: '600000.SH', period: '1d', bar_time: null, formula_name: 'F', signal_name: 'BUY', signal_type: 'OPEN', trigger_value: 1, price: null, execution_mode: 'alert', status: 'new', alert_status: 'sent', alert_error: null, tdx_run_id: null, created_at: null, alerted_at: null },
      { id: 2, portfolio_id: 1, strategy_id: 10, stock_code: '000001.SZ', period: '1d', bar_time: null, formula_name: 'F', signal_name: 'SELL', signal_type: 'CLOSE', trigger_value: -1, price: null, execution_mode: 'alert', status: 'new', alert_status: 'sent', alert_error: null, tdx_run_id: null, created_at: null, alerted_at: null },
      { id: 3, portfolio_id: 1, strategy_id: 10, stock_code: '600000.SH', period: '1d', bar_time: null, formula_name: 'F', signal_name: 'BUY', signal_type: 'OPEN', trigger_value: 1, price: null, execution_mode: 'alert', status: 'new', alert_status: 'sent', alert_error: null, tdx_run_id: null, created_at: null, alerted_at: null },
    ],
  }

  it('信号 tab 无信号 → 点同步按钮 alert「暂无信号股票」且不调 sendUserBlock', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, execution_mode: 'alert' }])
    vi.stubGlobal('alert', vi.fn())
    const w = mount(LiveSessions)
    await flushPromises()
    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()

    const btn = w.findAll('button').find((b) => b.text().includes('同步信号股到自选'))
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()

    expect(alert).toHaveBeenCalledWith(expect.stringContaining('暂无信号股票'))
    expect(sendUserBlock).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
    w.unmount()
  })

  it('有信号 → prompt 确认后调 sendUserBlock（codes 去重），成功 alert 写入数', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, execution_mode: 'alert' }])
    ;(getLiveSignals as any).mockResolvedValue(signalTabItems)
    ;(sendUserBlock as any).mockResolvedValue({ block_code: '创懿预警', count: 2 })
    const alertMock = vi.fn()
    vi.stubGlobal('prompt', vi.fn(() => '创懿预警'))
    vi.stubGlobal('alert', alertMock)
    const w = mount(LiveSessions)
    await flushPromises()
    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('同步信号股到自选'))!.trigger('click')
    await flushPromises()

    expect(prompt).toHaveBeenCalledWith('通达信自选板块名', '创懿预警')
    expect(sendUserBlock).toHaveBeenCalledWith({
      block_code: deriveBlockCode('创懿预警'), block_name: '创懿预警',
      stock_codes: ['600000.SH', '000001.SZ'], mode: 'replace',
    })
    expect(alertMock).toHaveBeenCalledWith(expect.stringContaining('已写入 2 只'))
    vi.unstubAllGlobals()
    w.unmount()
  })

  it('sendUserBlock 失败 → alert 提取后端 message（M5），不裸露网络层英文文案', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, execution_mode: 'alert' }])
    ;(getLiveSignals as any).mockResolvedValue(signalTabItems)
    ;(sendUserBlock as any).mockRejectedValue({ response: { data: { message: '通达信未启动' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('prompt', vi.fn(() => '创懿预警'))
    vi.stubGlobal('alert', alertMock)
    const w = mount(LiveSessions)
    await flushPromises()
    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('同步信号股到自选'))!.trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalledWith('通达信未启动')
    vi.unstubAllGlobals()
    w.unmount()
  })

  it('prompt 取消 → 不调用 sendUserBlock', async () => {
    ;(getLiveSessions as any).mockResolvedValue([{ ...runningSession, execution_mode: 'alert' }])
    ;(getLiveSignals as any).mockResolvedValue(signalTabItems)
    vi.stubGlobal('prompt', () => null)
    const w = mount(LiveSessions)
    await flushPromises()
    await w.findAll('button').find((b) => b.text() === '预警信号')!.trigger('click')
    await flushPromises()

    await w.findAll('button').find((b) => b.text().includes('同步信号股到自选'))!.trigger('click')
    await flushPromises()

    expect(sendUserBlock).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
    w.unmount()
  })
})
