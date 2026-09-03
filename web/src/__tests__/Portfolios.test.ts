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
    strategies: [{ id: 10, name: '主策略', role: 'master' }, { id: 11, name: '从策略', role: 'slave' }],
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
// 组合 1 的子策略列表
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

describe('Portfolios.vue — 组合列表（树状）', () => {
  it('挂载后渲染组合列表，显名称/子策略数/状态', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const rows = w.findAll('tbody tr').filter(r => !r.classes('strategy-sub-row'))
    expect(rows.length).toBe(2)
    expect(w.text()).toContain('稳健组合')
    expect(w.text()).toContain('激进组合')
    expect(w.text()).toContain('2 个子策略')
    expect(w.text()).toContain('0 个子策略')
  })

  it('每行含[编辑]与展开按钮，未展开时无子策略子行', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    expect(w.findAll('.strategy-sub-row').length).toBe(0)
    const firstRow = w.findAll('tbody tr').filter(r => !r.classes('strategy-sub-row'))[0]
    expect(firstRow.text()).toContain('编辑')
    expect(firstRow.find('button.toggle-expand').exists()).toBe(true)
  })

  it('点展开按钮 → 调 getStrategies(pid)，子策略子行出现', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const toggle = w.findAll('button.toggle-expand')[0]  // 组合1
    await toggle.trigger('click')
    await flushPromises()

    expect(getStrategies).toHaveBeenCalledTimes(1)
    expect((getStrategies as any).mock.calls[0][0]).toBe(1)  // pid
    // 子策略子行渲染
    const subRows = w.findAll('.strategy-sub-row')
    expect(subRows.length).toBe(2)
    expect(w.text()).toContain('主策略')
    expect(w.text()).toContain('从策略')
  })

  it('再点展开按钮 → 折叠，子策略子行消失', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    const toggle = w.findAll('button.toggle-expand')[0]
    await toggle.trigger('click')
    await flushPromises()
    expect(w.findAll('.strategy-sub-row').length).toBe(2)

    await toggle.trigger('click')
    await flushPromises()
    expect(w.findAll('.strategy-sub-row').length).toBe(0)
  })

  it('点[+新建组合]弹 Modal，含名称/股票池下拉，无子策略卡片', async () => {
    const w = mount(Portfolios)
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +新建组合
    expect(w.find('.modal-overlay').exists()).toBe(true)
    expect(w.text()).toContain('新建组合')
    const poolSelect = w.findAll('select').find(s => s.html().includes('tq自选'))
    expect(poolSelect).toBeTruthy()
    expect(w.text()).not.toContain('添加子策略')
    expect(w.findAll('.strategy-card').length).toBe(0)
  })

  it('组合 Modal 含交易成本字段，且每个参数显示名称（label）', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')

    // 参数名称以 label 显示
    expect(w.text()).toContain('最低佣金')
    expect(w.text()).toContain('买佣金率')
    expect(w.text()).toContain('卖佣金率')
    expect(w.text()).toContain('印花税率')
    expect(w.text()).toContain('滑点')
  })

  it('比例字段弹窗显示百分比，提交转小数；金额字段不转', async () => {
    ;(createPortfolio as any).mockResolvedValue({ id: 99 })
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 新建组合（取默认值）

    // 默认 max_drawdown=0.2 → 显示 20（百分比）
    const drawdownInput = w.find('input[data-field="max_drawdown"]')
    expect(Number((drawdownInput.element as HTMLInputElement).value)).toBe(20)
    // buy_commission_rate=0.00025 → 显示 0.025
    const commInput = w.find('input[data-field="buy_commission_rate"]')
    expect(Number((commInput.element as HTMLInputElement).value)).toBeCloseTo(0.025, 3)
    // min_commission 是金额 → 显示 5，不转百分比
    const minCommInput = w.find('input[data-field="min_commission"]')
    expect(Number((minCommInput.element as HTMLInputElement).value)).toBe(5)

    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    const arg = (createPortfolio as any).mock.calls[0][0]
    // 提交体：比例转回小数
    expect(arg.max_drawdown).toBeCloseTo(0.2)
    expect(arg.buy_commission_rate).toBeCloseTo(0.00025, 6)
    expect(arg.stamp_duty_rate).toBeCloseTo(0.0005, 6)
    // 金额不转
    expect(arg.min_commission).toBe(5)
    expect(arg.initial_capital).toBe(500000)
    // select 字段保持字符串/原值，不被 Number() 转成 NaN
    expect(arg.trading_session).toBe('full')
    expect(arg.stock_pool_id).toBe(0)
  })

  it('填组合表 + 提交 → 调 createPortfolio，strategies 为空数组', async () => {
    ;(createPortfolio as any).mockResolvedValue({ id: 99 })
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')

    await w.find('input[data-field="name"]').setValue('NEW_PS')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createPortfolio).toHaveBeenCalledTimes(1)
    const arg = (createPortfolio as any).mock.calls[0][0]
    expect(arg.name).toBe('NEW_PS')
    expect(arg.strategies).toEqual([])
  })

  it('提交失败（422）→ alert 错误且弹窗保持打开，不静默卡死', async () => {
    ;(createPortfolio as any).mockRejectedValue({ response: { data: { message: '股票池不存在' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Portfolios)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 新建组合
    await w.find('input[data-field="name"]').setValue('X')
    await w.find('.modal-actions button.btn-primary').trigger('click')  // 确定
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(w.text()).toContain('新建组合')  // 弹窗仍打开
    vi.unstubAllGlobals()
  })

  it('点某行[编辑] → 回填（比例转百分比显示）并提交调 updatePortfolio（比例转回小数）', async () => {
    ;(updatePortfolio as any).mockResolvedValue({ id: 1 })
    ;(getPortfolioDetail as any).mockResolvedValue(mockPortfolios[0])
    const w = mount(Portfolios)
    await flushPromises()

    const editBtn = w.findAll('button').find(b => b.text() === '编辑')!
    await editBtn.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('编辑组合')
    // 回填后 max_drawdown 显示百分比 20
    const drawdownInput = w.find('input[data-field="max_drawdown"]')
    expect(Number((drawdownInput.element as HTMLInputElement).value)).toBe(20)

    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updatePortfolio).toHaveBeenCalledTimes(1)
    expect((updatePortfolio as any).mock.calls[0][0]).toBe(1)
    const arg = (updatePortfolio as any).mock.calls[0][1]
    expect(arg.max_drawdown).toBeCloseTo(0.2)
    expect(arg.min_commission).toBe(5)
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

describe('Portfolios.vue — 子策略（树状子行 + 弹窗）', () => {
  async function expandFirst(w: any) {
    await w.findAll('button.toggle-expand')[0].trigger('click')
    await flushPromises()
  }

  it('展开后子策略子行含[编辑]/[删除]，区域含[+新建子策略]', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)

    const subRows = w.findAll('.strategy-sub-row')
    expect(subRows[0].text()).toContain('编辑')
    expect(subRows[0].text()).toContain('删除')
    expect(w.text()).toContain('+ 新建子策略')
  })

  it('点[+新建子策略]弹 Modal，含风控/加仓参数字段且显示名称', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)

    expect(w.find('.modal-overlay').exists()).toBe(false)
    const newSubBtn = w.findAll('button').find(b => b.text().includes('新建子策略'))!
    await newSubBtn.trigger('click')
    expect(w.find('.modal-overlay').exists()).toBe(true)

    // 参数名称 label
    expect(w.text()).toContain('止损')
    expect(w.text()).toContain('止盈')
    expect(w.text()).toContain('移动止损')
    expect(w.text()).toContain('加仓阈值')
    expect(w.text()).toContain('加仓次数')
    expect(w.text()).toContain('加仓比例')
    expect(w.text()).toContain('减仓比例')
  })

  it('子策略 Modal 比例字段显示百分比，提交转小数', async () => {
    ;(createStrategy as any).mockResolvedValue({ id: 20 })
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)
    const newSubBtn = w.findAll('button').find(b => b.text().includes('新建子策略'))!
    await newSubBtn.trigger('click')

    // 默认 capital_ratio=0.6 → 显示 60
    const capInput = w.find('input[data-field="capital_ratio"]')
    expect(Number((capInput.element as HTMLInputElement).value)).toBe(60)
    // max_positions 是数量 → 显示 5，不转
    const posInput = w.find('input[data-field="max_positions"]')
    expect(Number((posInput.element as HTMLInputElement).value)).toBe(5)

    await w.find('input[data-field="name"]').setValue('NEW_S')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createStrategy).toHaveBeenCalledTimes(1)
    const args = (createStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)  // pid
    expect(args[1].name).toBe('NEW_S')
    expect(args[1].capital_ratio).toBeCloseTo(0.6)  // 转回小数
    expect(args[1].max_positions).toBe(5)           // 数量不转
  })

  it('新建子策略默认加仓阈值为 -1（任何价都加）', async () => {
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)
    const newSubBtn = w.findAll('button').find(b => b.text().includes('新建子策略'))!
    await newSubBtn.trigger('click')

    const input = w.find('input[data-field="add_position_threshold"]')
    expect(Number((input.element as HTMLInputElement).value)).toBe(-1)
  })

  it('加仓阈值填 -1（任何价都加）→ 提交保持 -1，不除以 100', async () => {
    ;(createStrategy as any).mockResolvedValue({ id: 20 })
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)
    const newSubBtn = w.findAll('button').find(b => b.text().includes('新建子策略'))!
    await newSubBtn.trigger('click')

    await w.find('input[data-field="add_position_threshold"]').setValue('-1')
    await w.find('input[data-field="name"]').setValue('NEW_S')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createStrategy).toHaveBeenCalledTimes(1)
    const args = (createStrategy as any).mock.calls[0]
    expect(args[1].add_position_threshold).toBe(-1)
  })

  it('点子策略子行[编辑] → 回填（比例转百分比）并提交调 updateStrategy(pid, sid, req)', async () => {
    ;(updateStrategy as any).mockResolvedValue({ id: 10 })
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)

    const editBtn = w.findAll('.strategy-sub-row button').find(b => b.text() === '编辑')!
    await editBtn.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('编辑子策略')
    // 回填 capital_ratio=0.6 → 显示 60
    const capInput = w.find('input[data-field="capital_ratio"]')
    expect(Number((capInput.element as HTMLInputElement).value)).toBe(60)

    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(updateStrategy).toHaveBeenCalledTimes(1)
    const args = (updateStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)   // pid
    expect(args[1]).toBe(10)  // sid
    expect(args[2].capital_ratio).toBeCloseTo(0.6)  // 转回小数
  })

  it('点子策略子行[删除] → 调 deleteStrategy(pid, sid)', async () => {
    ;(deleteStrategy as any).mockResolvedValue({ code: 0, data: null })
    vi.stubGlobal('confirm', () => true)
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)

    const delBtn = w.findAll('.strategy-sub-row button.btn-danger').find(b => b.text() === '删除')!
    await delBtn.trigger('click')
    await flushPromises()

    expect(deleteStrategy).toHaveBeenCalledTimes(1)
    const args = (deleteStrategy as any).mock.calls[0]
    expect(args[0]).toBe(1)   // pid
    expect(args[1]).toBe(10)  // sid
    vi.unstubAllGlobals()
  })

  it('删除被引用子策略 → 拦截器/HTTP 错误 reject，前端 alert 提示且不刷新列表', async () => {
    // #42 + 拦截器后：deleteStrategy 返回 res.data.data（成功）或 reject（业务/HTTP 错误）。
    // 模拟后端 code:400 → 拦截器 reject 一个带 response.data.message 的错误。
    const err: any = new Error('请求失败')
    err.response = { data: { code: 400, message: '该子策略被回测或实盘交易记录引用，无法删除。请先删除相关的回测记录或实盘会话。' } }
    ;(deleteStrategy as any).mockRejectedValue(err)
    const alertMock = vi.fn()
    vi.stubGlobal('confirm', () => true)
    vi.stubGlobal('alert', alertMock)
    const w = mount(Portfolios)
    await flushPromises()
    await expandFirst(w)

    const delBtn = w.findAll('.strategy-sub-row button.btn-danger').find(b => b.text() === '删除')!
    await delBtn.trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(alertMock.mock.calls[0][0]).toContain('被回测或实盘交易记录引用')
    vi.unstubAllGlobals()
  })
})
