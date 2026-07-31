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
  getStrategies: vi.fn(),
  createStrategy: vi.fn(),
  updateStrategy: vi.fn(),
  deleteStrategy: vi.fn(),
}))

import Portfolios from '../views/Portfolios.vue'
import {
  getPortfolios, getPortfolioDetail, createPortfolio, updatePortfolio, deletePortfolio,
  getStockPools, getFormulas,
  getStrategies, createStrategy, updateStrategy, deleteStrategy,
} from '../api'

const mockPortfolios = [
  {
    id: 1, name: '稳健组合', stock_pool_id: 1, status: 'active',
    strategies: [{ id: 10, name: '主策略', role: 'master' }],
    benchmark_index: '000300.SH', initial_capital: 500000,
    max_drawdown: 0.2, daily_loss_limit: 0.05, max_holdings: 10,
    trading_session: 'full',
    min_commission: 5, buy_commission_rate: 0.00025, sell_commission_rate: 0.00025,
    stamp_duty_rate: 0.0005, slippage: 0,
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
// 第二层：组合 1 的子策略列表
const mockStrategies = [
  { id: 10, name: '主策略', role: 'master', formula_id: 1, period: '1d',
    capital_ratio: 0.6, max_positions: 5, master_strategy_id: null,
    stop_loss_ratio: 0.05, take_profit_ratio: 0.15, trailing_stop_ratio: 0.03,
    add_position_threshold: 0.05, max_add_count: 2, add_position_ratio: 0.1, reduce_position_ratio: 0.3,
    single_open_ratio: 0.1 },
  { id: 11, name: '从策略', role: 'slave', formula_id: 2, period: '1d',
    capital_ratio: 0.4, max_positions: 3, master_strategy_id: 10,
    stop_loss_ratio: 0.04, take_profit_ratio: 0.12, trailing_stop_ratio: 0.02,
    add_position_threshold: 0.05, max_add_count: 1, add_position_ratio: 0.08, reduce_position_ratio: 0.25,
    single_open_ratio: 0.08 },
]

beforeEach(() => {
  vi.clearAllMocks()
  ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
  ;(getStockPools as any).mockResolvedValue(mockPools)
  ;(getFormulas as any).mockResolvedValue(mockFormulas)
  ;(getStrategies as any).mockResolvedValue(mockStrategies)
})

describe('Portfolios.vue — 第一层 组合列表', () => {
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

  it('每行含[编辑]与[子策略]两个按钮', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const firstRow = w.findAll('tbody tr')[0]
    expect(firstRow.text()).toContain('编辑')
    expect(firstRow.text()).toContain('子策略')
  })

  it('点[+新建组合]弹组合 Modal，含名称/股票池下拉，但无子策略卡片', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +新建组合
    expect(w.find('.modal-overlay').exists()).toBe(true)
    expect(w.text()).toContain('新建组合')
    // 股票池下拉选项
    const poolSelect = w.findAll('select').find(s => s.html().includes('tq自选'))
    expect(poolSelect).toBeTruthy()
    // 两层设计：组合 Modal 不再含子策略配置
    expect(w.text()).not.toContain('添加子策略')
    expect(w.findAll('.strategy-card').length).toBe(0)
  })

  it('填组合表 + 提交 → 调 createPortfolio，strategies 为空数组', async () => {
    ;(createPortfolio as any).mockResolvedValue({ id: 99 })
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')

    await w.find('input[placeholder*="名称"]').setValue('NEW_PS')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createPortfolio).toHaveBeenCalledTimes(1)
    const arg = (createPortfolio as any).mock.calls[0][0]
    expect(arg.name).toBe('NEW_PS')
    expect(arg.strategies).toEqual([])
  })

  it('组合 Modal 含交易成本字段（手续费/印花税/滑点），提交时带默认值', async () => {
    ;(createPortfolio as any).mockResolvedValue({ id: 99 })
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // +新建组合

    // 交易成本 placeholder：买佣金/卖佣金/最低佣金/印花税/滑点
    expect(w.find('input[placeholder*="买佣金"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="卖佣金"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="最低佣金"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="印花税"]').exists()).toBe(true)
    expect(w.find('input[placeholder*="滑点"]').exists()).toBe(true)

    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    const arg = (createPortfolio as any).mock.calls[0][0]
    // 交易成本字段已带入提交体（与后端 PortfolioCreate 对齐）
    expect(arg).toHaveProperty('min_commission')
    expect(arg).toHaveProperty('buy_commission_rate')
    expect(arg).toHaveProperty('sell_commission_rate')
    expect(arg).toHaveProperty('stamp_duty_rate')
    expect(arg).toHaveProperty('slippage')
  })

  it('点某行[编辑] → 回填并提交调 updatePortfolio', async () => {
    ;(updatePortfolio as any).mockResolvedValue({ id: 1 })
    ;(getPortfolioDetail as any).mockResolvedValue(mockPortfolios[0])
    const w = mount(Portfolios)
    await flushPromises()

    const editBtn = w.findAll('button').find(b => b.text() === '编辑')!
    await editBtn.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('编辑组合')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updatePortfolio).toHaveBeenCalledTimes(1)
    expect((updatePortfolio as any).mock.calls[0][0]).toBe(1)  // id
    // 交易成本字段回填后带入提交体
    const arg = (updatePortfolio as any).mock.calls[0][1]
    expect(arg.min_commission).toBe(5)
    expect(arg.buy_commission_rate).toBe(0.00025)
    expect(arg.stamp_duty_rate).toBe(0.0005)
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

describe('Portfolios.vue — 第二层 子策略列表', () => {
  it('点某行[子策略] → 切换到子策略列表，调 getStrategies(pid)', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    // 第一层的"子策略"按钮（第一行）
    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()

    expect(getStrategies).toHaveBeenCalledTimes(1)
    expect((getStrategies as any).mock.calls[0][0]).toBe(1)  // pid
    // 第二层渲染子策略名称
    expect(w.text()).toContain('主策略')
    expect(w.text()).toContain('从策略')
  })

  it('第二层有[← 返回]按钮 → 返回第一层', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('主策略')  // 在第二层

    const backBtn = w.findAll('button').find(b => b.text().includes('返回'))!
    await backBtn.trigger('click')
    await flushPromises()

    // 回到第一层：组合列表标题再现，子策略列表消失
    expect(w.text()).toContain('稳健组合')
    expect(getStrategies).toHaveBeenCalledTimes(1)  // 返回后未重复加载
  })

  it('点[+新建子策略]弹单个子策略 Modal，含风控/加仓参数字段', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    // 进入第二层
    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +新建子策略
    expect(w.find('.modal-overlay').exists()).toBe(true)

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

  it('填子策略表 + 提交 → 调 createStrategy(pid, req)', async () => {
    ;(createStrategy as any).mockResolvedValue({ id: 20 })
    const w = mount(Portfolios)
    await flushPromises()
    // 进入第二层（组合 1）
    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // +新建子策略

    await w.find('input[placeholder*="名称"]').setValue('NEW_S')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createStrategy).toHaveBeenCalledTimes(1)
    const args = (createStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)  // pid
    expect(args[1].name).toBe('NEW_S')
  })

  it('点子策略行[编辑] → 回填并提交调 updateStrategy(pid, sid, req)', async () => {
    ;(updateStrategy as any).mockResolvedValue({ id: 10 })
    const w = mount(Portfolios)
    await flushPromises()
    // 进入第二层
    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()

    // 子策略行的编辑按钮
    const editBtn = w.findAll('tbody tr button').find(b => b.text() === '编辑')!
    await editBtn.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('编辑子策略')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updateStrategy).toHaveBeenCalledTimes(1)
    const args = (updateStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)   // pid
    expect(args[1]).toBe(10)  // sid
  })

  it('点子策略行[删除] → 调 deleteStrategy(pid, sid)', async () => {
    ;(deleteStrategy as any).mockResolvedValue(null)
    vi.stubGlobal('confirm', () => true)
    const w = mount(Portfolios)
    await flushPromises()
    // 进入第二层
    const subBtn = w.findAll('button').find(b => b.text() === '子策略')!
    await subBtn.trigger('click')
    await flushPromises()

    const delBtn = w.findAll('tbody tr button.btn-danger').find(b => b.text() === '删除')!
    await delBtn.trigger('click')
    await flushPromises()

    expect(deleteStrategy).toHaveBeenCalledTimes(1)
    const args = (deleteStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)   // pid
    expect(args[1]).toBe(10)  // sid
    vi.unstubAllGlobals()
  })
})
