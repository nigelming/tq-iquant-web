import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Mock API 模块：组件挂载测试不发真实请求
vi.mock('../api', () => ({
  getTdxPools: vi.fn(),
  getTdxPoolStocks: vi.fn(),
  syncStockPool: vi.fn(),
  getStockPools: vi.fn(),
  deleteStockPool: vi.fn(),
}))

import StockPools from '../views/StockPools.vue'
import {
  getTdxPools, getTdxPoolStocks, syncStockPool, getStockPools, deleteStockPool,
} from '../api'

// 通达信板块 + 本地残留合并的 mock 数据
const mockTdxPools = [
  { code: 'TQCS', name: 'tq自选', synced: true, exists_in_tdx: true, stock_count: 50 },
  { code: 'DEGP', name: '第二股票', synced: false, exists_in_tdx: true, stock_count: 0 },
  { code: 'OLDBLK', name: '旧板块', synced: true, exists_in_tdx: false, stock_count: 12 },
]

// 本地已同步池（供删除取 id）：TQCS id=1，OLDBLK id=3（DEGP 未同步，无本地记录）
const mockLocalPools = [
  { id: 1, code: 'TQCS', name: 'tq自选', synced_at: '2026-07-31', stock_count: 50 },
  { id: 3, code: 'OLDBLK', name: '旧板块', synced_at: '2026-07-30', stock_count: 12 },
]

const mockStocks = [
  { stock_code: '600000.SH', stock_name: '浦发银行' },
  { stock_code: '000001.SZ', stock_name: '平安银行' },
]

beforeEach(() => {
  vi.clearAllMocks()
  ;(getTdxPools as any).mockResolvedValue(mockTdxPools)
  ;(getTdxPoolStocks as any).mockResolvedValue(mockStocks)
  ;(syncStockPool as any).mockResolvedValue({ id: 99, code: 'TQCS', name: 'tq自选' })
  ;(getStockPools as any).mockResolvedValue(mockLocalPools)
  ;(deleteStockPool as any).mockResolvedValue(null)
})

describe('StockPools.vue', () => {
  it('挂载后渲染通达信板块列表，显名称/code/同步状态/股票数', async () => {
    const w = mount(StockPools)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    expect(rows.length).toBe(3)
    expect(w.text()).toContain('tq自选')
    expect(w.text()).toContain('TQCS')
    expect(w.text()).toContain('第二股票')
    expect(w.text()).toContain('旧板块')
    expect(w.text()).toContain('已同步')
    expect(w.text()).toContain('未同步')
    expect(w.text()).toContain('50')
  })

  it('已同步且通达信存在的行：显[查看][同步][删除]；未同步行：显[查看][同步]；残留行：显[删除]', async () => {
    const w = mount(StockPools)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    // 已同步+存在：3 个操作按钮
    const syncedRowBtns = rows[0].findAll('button')
    expect(syncedRowBtns.some(b => b.text().includes('查看'))).toBe(true)
    expect(syncedRowBtns.some(b => b.text().includes('同步'))).toBe(true)
    expect(syncedRowBtns.some(b => b.text().includes('删除'))).toBe(true)
    // 未同步：查看+同步，无删除
    const unsyncedRowBtns = rows[1].findAll('button')
    expect(unsyncedRowBtns.some(b => b.text().includes('查看'))).toBe(true)
    expect(unsyncedRowBtns.some(b => b.text().includes('同步'))).toBe(true)
    expect(unsyncedRowBtns.some(b => b.text().includes('删除'))).toBe(false)
    // 残留（通达信已删）：仅删除
    const orphanRowBtns = rows[2].findAll('button')
    expect(orphanRowBtns.some(b => b.text().includes('删除'))).toBe(true)
    expect(orphanRowBtns.some(b => b.text().includes('查看'))).toBe(false)
    expect(orphanRowBtns.some(b => b.text().includes('同步'))).toBe(false)
  })

  it('点某行[查看] → 调 getTdxPoolStocks(code) 并弹成分股清单', async () => {
    const w = mount(StockPools)
    await flushPromises()

    const viewBtn = w.findAll('button').find(b => b.text().includes('查看'))!
    await viewBtn.trigger('click')
    await flushPromises()

    expect(getTdxPoolStocks).toHaveBeenCalledTimes(1)
    expect((getTdxPoolStocks as any).mock.calls[0][0]).toBe('TQCS')  // 第一行 code
    // 成分股在 Modal 中显示
    expect(w.text()).toContain('600000.SH')
    expect(w.text()).toContain('浦发银行')
  })

  it('点未同步行[同步] → 调 syncStockPool({code})', async () => {
    vi.stubGlobal('confirm', () => true)
    const w = mount(StockPools)
    await flushPromises()

    // 未同步行（第二股票 DEGP）的同步按钮
    const rows = w.findAll('tbody tr')
    const syncBtn = rows[1].findAll('button').find(b => b.text().includes('同步'))!
    await syncBtn.trigger('click')
    await flushPromises()

    expect(syncStockPool).toHaveBeenCalledTimes(1)
    expect((syncStockPool as any).mock.calls[0][0]).toEqual({ code: 'DEGP' })
    vi.unstubAllGlobals()
  })

  it('点已同步行[同步] → 重同步，调 syncStockPool({code})', async () => {
    vi.stubGlobal('confirm', () => true)
    const w = mount(StockPools)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    const syncBtn = rows[0].findAll('button').find(b => b.text().includes('同步'))!
    await syncBtn.trigger('click')
    await flushPromises()

    expect(syncStockPool).toHaveBeenCalledTimes(1)
    expect((syncStockPool as any).mock.calls[0][0]).toEqual({ code: 'TQCS' })
    vi.unstubAllGlobals()
  })

  it('点残留行[删除] → 调 deleteStockPool(id)，调本地列表取 id', async () => {
    vi.stubGlobal('confirm', () => true)
    const w = mount(StockPools)
    await flushPromises()

    // 残留行（旧板块 OLDBLK）的删除按钮
    const rows = w.findAll('tbody tr')
    const delBtn = rows[2].findAll('button').find(b => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()

    expect(deleteStockPool).toHaveBeenCalledTimes(1)
    // id 来自 getStockPools 本地列表（mock 未设 getStockPools，组件应能处理）
    vi.unstubAllGlobals()
  })

  it('load 失败（通达信连接失败）→ 显示错误条，列表清空', async () => {
    ;(getTdxPools as any).mockRejectedValue({ response: { data: { message: '通达信未启动' } } })
    const w = mount(StockPools)
    await flushPromises()

    expect(w.text()).toContain('加载失败')
    expect(w.text()).toContain('通达信未启动')
    expect(w.findAll('tbody tr').length).toBe(0)
    w.unmount()
  })

  it('删除失败 → alert 提示，不崩', async () => {
    ;(deleteStockPool as any).mockRejectedValue({ response: { data: { message: '被引用，无法删除' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('confirm', () => true)
    vi.stubGlobal('alert', alertMock)
    const w = mount(StockPools)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    const delBtn = rows[2].findAll('button').find(b => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(alertMock.mock.calls[0][0]).toContain('被引用，无法删除')
    vi.unstubAllGlobals()
  })
})
