import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import Dashboard from '../views/Dashboard.vue'

const mocks = vi.hoisted(() => ({
  mockGetStockPools: vi.fn(),
  mockGetFormulas: vi.fn(),
  mockGetPortfolios: vi.fn(),
  mockGetBacktestRecords: vi.fn(),
  mockGetLiveSessions: vi.fn(),
  mockGetSystemConfigs: vi.fn(),
  mockGetSystemStatus: vi.fn(),
  mockGetLiveChannel: vi.fn(),
}))

vi.mock('../api', () => ({
  getStockPools: mocks.mockGetStockPools,
  getFormulas: mocks.mockGetFormulas,
  getPortfolios: mocks.mockGetPortfolios,
  getBacktestRecords: mocks.mockGetBacktestRecords,
  getLiveSessions: mocks.mockGetLiveSessions,
  getSystemConfigs: mocks.mockGetSystemConfigs,
  getSystemStatus: mocks.mockGetSystemStatus,
  getLiveChannel: mocks.mockGetLiveChannel,
}))

import {
  getStockPools, getFormulas, getPortfolios, getBacktestRecords,
  getLiveSessions, getSystemConfigs, getSystemStatus, getLiveChannel,
} from '../api'

const mockCfg = {
  tdx_path: 'D:\\new_tdx64',
  iquant_path: 'D:\\iquant',
  max_concurrent_backtest: 1,
  database: { sqlite_path: 'data/dev.db' },
  goldminer: { base_url: 'http://127.0.0.1:7801', token: '', account_id: '0e5343e4' },
  iquant_bridge: {
    simulation: { base_url: 'http://127.0.0.1:8790' },
    live: { base_url: 'http://127.0.0.1:8791' },
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(getStockPools as any).mockResolvedValue([{ id: 1, code: 'CS', name: '自选', synced_at: null, stock_count: 0 }])
  ;(getFormulas as any).mockResolvedValue([{ id: 1, name: '均线', content: '', formula_count: 1, created_at: null, updated_at: null, signals: [] }])
  ;(getPortfolios as any).mockResolvedValue([])
  ;(getBacktestRecords as any).mockResolvedValue([{
    id: 1, portfolio_strategy_id: 1, name: '回测A', start_date: '2026-01-01', end_date: '2026-01-31',
    status: 'completed', error_message: null, created_at: null, completed_at: null,
  }])
  ;(getLiveSessions as any).mockResolvedValue([])
  ;(getSystemConfigs as any).mockResolvedValue(mockCfg)
  ;(getSystemStatus as any).mockResolvedValue({ core: { online: true, version: '1.0', uptime: '0h0m' } })
  ;(getLiveChannel as any).mockImplementation((engine: string) => Promise.resolve({
    engine,
    label: engine === 'tdx' ? '通达信 TQ' : engine === 'ths' ? '同花顺' : 'QMT 通道',
    online: null,
  }))
})

describe('Dashboard.vue', () => {
  it('挂载后并行拉取各统计源并渲染统计卡', async () => {
    const w = mount(Dashboard)
    await flushPromises()

    expect(getStockPools).toHaveBeenCalled()
    expect(getFormulas).toHaveBeenCalled()
    expect(getPortfolios).toHaveBeenCalled()
    expect(getBacktestRecords).toHaveBeenCalled()
    expect(getLiveSessions).toHaveBeenCalled()
    expect(getSystemConfigs).toHaveBeenCalled()
    expect(getSystemStatus).toHaveBeenCalled()
    expect(getLiveChannel).toHaveBeenCalledTimes(3)   // tdx/ths/goldminer 三通道

    // 统计卡：股票池=1、公式=1、回测=1；系统卡在线；配置路径
    expect(w.text()).toContain('股票池')
    expect(w.text()).toContain('回测记录')
    expect(w.text()).toContain('Core 状态')
    expect(w.text()).toContain('在线')
    expect(w.text()).toContain('回测A')
    expect(w.text()).toContain('D:\\new_tdx64')
    expect(w.text()).toContain('http://127.0.0.1:7801')
    w.unmount()
  })

  it('通道接口慢 → 主区块先渲染，通道卡片 5s 后出超时占位', async () => {
    vi.useFakeTimers()
    ;(getLiveChannel as any).mockImplementation(() => new Promise(() => {}))  // 永不返回
    const w = mount(Dashboard)
    await vi.advanceTimersByTimeAsync(10)

    // 主数据（<100ms 的真实接口）已渲染，不被通道探活 gate 卡住
    expect(w.text()).toContain('核心指标')
    expect(w.text()).toContain('最近回测')
    expect(w.text()).toContain('加载中…')   // 通道卡片独立占位

    await vi.advanceTimersByTimeAsync(5000)
    expect(w.text()).toContain('通道状态超时，稍后自动重试')

    w.unmount()
    vi.useRealTimers()
  })

  it('某查询失败 → 其余卡片仍渲染,仅提示部分失败', async () => {
    ;(getLiveSessions as any).mockRejectedValue(new Error('network down'))
    const w = mount(Dashboard)
    await flushPromises()

    expect(w.text()).toContain('部分数据加载失败')
    expect(w.text()).toContain('回测记录')  // 其余卡片不受影响
    expect(w.text()).toContain('在线')
    w.unmount()
  })

  it('对账差异 tooltip 标注 computed_at；旧数据缺字段时降级不报错', async () => {
    ;(getLiveSessions as any).mockResolvedValue([
      {
        id: 1, name: '实盘A', mode: 'live', status: 'running', engine: 'tdx',
        execution_mode: 'auto', started_at: null, stopped_at: null, portfolio_ids: [1],
        reconcile_mismatches: [{
          code: '002745.SZ', virtual: 0, real: 5300, diff: -5300,
          computed_at: '2026-11-05T11:41:18+08:00',
        }],
      },
      {
        id: 2, name: '实盘B', mode: 'live', status: 'running', engine: 'tdx',
        execution_mode: 'auto', started_at: null, stopped_at: null, portfolio_ids: [1],
        reconcile_mismatches: [{ code: '600000.SH', virtual: 100, real: 0, diff: 100 }],
      },
    ])
    const w = mount(Dashboard)
    await flushPromises()

    expect(w.html()).toContain('算于 11:41:18')
    expect(w.text()).toContain('1 项')
    w.unmount()
  })

  it('Hero 保留统计与刷新，已移除品牌标题', async () => {
    const w = mount(Dashboard)
    await flushPromises()
    expect(w.find('.dash-hero').exists()).toBe(true)
    expect(w.find('.dash-hero-stats').exists()).toBe(true)
    expect(w.find('.dash-hero-stats').text()).toContain('股票池')
    expect(w.find('.dash-hero-actions').find('button').text()).toContain('刷新')
    expect(w.find('.dash-brand').exists()).toBe(false)
    expect(w.find('.dash-brand-sub').exists()).toBe(false)
    w.unmount()
  })
})
