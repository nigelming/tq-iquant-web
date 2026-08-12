import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Dashboard.vue 走 ../api 客户端（7 个 getter）
const mocks = vi.hoisted(() => ({
  mockGetStockPools: vi.fn(),
  mockGetFormulas: vi.fn(),
  mockGetPortfolios: vi.fn(),
  mockGetBacktestRecords: vi.fn(),
  mockGetLiveSessions: vi.fn(),
  mockGetSystemConfigs: vi.fn(),
  mockGetSystemStatus: vi.fn(),
}))
vi.mock('../api', () => ({
  getStockPools: mocks.mockGetStockPools,
  getFormulas: mocks.mockGetFormulas,
  getPortfolios: mocks.mockGetPortfolios,
  getBacktestRecords: mocks.mockGetBacktestRecords,
  getLiveSessions: mocks.mockGetLiveSessions,
  getSystemConfigs: mocks.mockGetSystemConfigs,
  getSystemStatus: mocks.mockGetSystemStatus,
}))

import Dashboard from '../views/Dashboard.vue'
import {
  getStockPools, getFormulas, getPortfolios, getBacktestRecords,
  getLiveSessions, getSystemConfigs, getSystemStatus,
} from '../api'

const mockCfg = {
  tdx_path: 'D:\\new_tdx64',
  iquant_path: 'D:\\iquant',
  max_concurrent_backtest: 1,
  database: { sqlite_path: 'data/dev.db' },
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
    status: 'completed', progress: 1, error_message: null, created_at: null, completed_at: null,
  }])
  ;(getLiveSessions as any).mockResolvedValue([])
  ;(getSystemConfigs as any).mockResolvedValue(mockCfg)
  ;(getSystemStatus as any).mockResolvedValue({ core: { online: true, version: '1.0', uptime: '0h0m' } })
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

    // 统计卡：股票池=1、公式=1、回测=1；系统卡在线；配置路径
    expect(w.text()).toContain('股票池')
    expect(w.text()).toContain('回测记录')
    expect(w.text()).toContain('Core 状态')
    expect(w.text()).toContain('在线')
    expect(w.text()).toContain('回测A')
    expect(w.text()).toContain('D:\\new_tdx64')
    expect(w.text()).toContain('http://127.0.0.1:8790')
    w.unmount()
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
})
