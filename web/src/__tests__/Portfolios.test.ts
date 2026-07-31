import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Mock API 模块：组件挂载测试不发真实请求
vi.mock('../api', () => ({
  getPortfolios: vi.fn(),
  getPortfolioDetail: vi.fn(),
  createPortfolio: vi.fn(),
  updatePortfolio: vi.fn(),
  deletePortfolio: vi.fn(),
  getStockPools: vi.fn(),
  getFormulas: vi.fn(),
}))

import Portfolios from '../views/Portfolios.vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas,
} from '../api'

const mockPortfolios = [
  {
    id: 1, name: '稳健组合', stock_pool_id: 1, status: 'active',
    strategies: [{ id: 10, name: '主策略', role: 'master' }],
  },
  {
    id: 2, name: '激进组合', stock_pool_id: 2, status: 'archived',
    strategies: [],
  },
]
const mockPools = [
  { id: 1, code: 'TQCS', name: 'tq自选' },
  { id: 2, code: 'DEGP', name: '第二股票' },
]
const mockFormulas = [
  { id: 1, name: 'OPEN_FORMULA' },
  { id: 2, name: 'MACROSS' },
]

beforeEach(() => {
  vi.clearAllMocks()
  ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
  ;(getStockPools as any).mockResolvedValue(mockPools)
  ;(getFormulas as any).mockResolvedValue(mockFormulas)
})

describe('Portfolios.vue', () => {
  it('挂载后渲染组合列表，显名称/子策略数/状态', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    expect(rows.length).toBe(2)
    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('激进组合')
    expect(w.text()).toContain('1 个子策略')
    expect(w.text()).toContain('0 个子策略')
  })

  it('点[+新建组合]弹 Modal，含名称/股票池下拉/子策略行', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +新建组合
    expect(w.find('.modal-overlay').exists()).toBe(true)
    expect(w.text()).toContain('新建组合')
    // 股票池下拉选项
    const poolSelect = w.findAll('select').find(s => s.html().includes('tq自选'))
    expect(poolSelect).toBeTruthy()
    expect(w.text()).toContain('添加子策略')
  })

  it('点[+添加子策略]子策略卡片 +1', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    const before = w.findAll('.strategy-card').length
    await w.find('button.signal-add').trigger('click')
    expect(w.findAll('.strategy-card').length).toBe(before + 1)
  })

  it('子策略卡片含风控参数（止损/止盈/移动止损）与加仓参数字段', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    // 风控参数 placeholder
    expect(w.find('input[placeholder*="止损"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="止盈"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="移动止损"]').exists()).toBe(true)
    // 加仓参数 placeholder
    expect(w.find('input[placeholder*="加仓阈值"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="加仓次数"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="加仓比例"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="减仓比例"]').exists()).toBe(true)
  })

  it('填表 + 提交 → 调 createPortfolio，参数含 name/strategies/stock_pool_id', async () => {
    ;(createPortfolio as any).mockResolvedValue({ id: 99 })
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')

    await w.find('input[placeholder*="名称"]').setValue('NEW_PS')
    // 提交
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createPortfolio).toHaveBeenCalledTimes(1)
    const arg = (createPortfolio as any).mock.calls[0][0]
    expect(arg.name).toBe('NEW_PS')
    expect(Array.isArray(arg.strategies)).toBe(true)
  })

  it('点某行[编辑] → 回填并提交调 updatePortfolio', async () => {
    ;(updatePortfolio as any).mockResolvedValue({ id: 1 })
    ;(getPortfolioDetail as any).mockResolvedValue(mockPortfolios[0])
    const w = mount(Portfolios)
    await flushPromises()

    // 第一行的编辑按钮
    const editBtn = w.findAll('button').find(b => b.text().includes('编辑'))!
    await editBtn.trigger('click')
    await flushPromises()

    // Modal 打开且标题为编辑
    expect(w.text()).toContain('编辑组合')
    // 提交
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updatePortfolio).toHaveBeenCalledTimes(1)
    expect((updatePortfolio as any).mock.calls[0][0]).toBe(1)  // id
  })

  it('点某行[删除] → 调 deletePortfolio(id)', async () => {
    ;(deletePortfolio as any).mockResolvedValue(null)
    vi.stubGlobal('confirm', () => true)
    const w = mount(Portfolios)
    await flushPromises()

    const delBtn = w.findAll('button.btn-danger').find(b => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()

    expect(deletePortfolio).toHaveBeenCalledTimes(1)
    expect((deletePortfolio as any).mock.calls[0][0]).toBe(1)
    vi.unstubAllGlobals()
  })
})
