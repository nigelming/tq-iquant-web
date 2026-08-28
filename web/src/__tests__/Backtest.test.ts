import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

vi.mock('../api', () => ({
  getBacktestRecords: vi.fn(),
  getBacktestDetail: vi.fn(),
  runBacktest: vi.fn(),
  getPortfolios: vi.fn(),
  deleteBacktestRecord: vi.fn(),
}))

// echarts 在 jsdom 下无真实 canvas，init 会失败 → mock 成最小桩
vi.mock('echarts', () => {
  const stub = {
    init: vi.fn(() => ({
      setOption: vi.fn(),
      resize: vi.fn(),
      dispose: vi.fn(),
    })),
    // 必须是可 new 的普通函数（组件用 new echarts.graphic.LinearGradient(...)）
    graphic: { LinearGradient: function () { return {} } },
  }
  return { default: stub, ...stub }
})

import Backtest from '../views/Backtest.vue'
import {
  getBacktestRecords, getBacktestDetail, runBacktest, getPortfolios,
  deleteBacktestRecord,
} from '../api'

const mockRecords = [
  {
    id: 1, name: '回测A', portfolio_strategy_id: 1, status: 'completed', progress: 100,
    start_date: '2026-07-01', end_date: '2026-07-31', created_at: '2026-08-01T10:00:00',
  },
  {
    id: 2, name: '回测B', portfolio_strategy_id: 2, status: 'failed', progress: 30,
    start_date: '2026-06-01', end_date: '2026-06-30', created_at: '2026-08-02T10:00:00',
  },
]
const mockPortfolios = [
  { id: 1, name: '稳健组合', stock_pool_id: 1, strategies: [] },
  { id: 2, name: '激进组合', stock_pool_id: 2, strategies: [] },
]
const mockDetail = {
  record: { id: 1, name: '回测A', status: 'completed', start_date: '2026-07-01', end_date: '2026-07-31' },
  snapshots: [
    { snap_date: '2026-07-01', total_value: 100000, cash: 100000, market_value: 0, daily_return: null, cumulative_return: 0, benchmark_value: 3000 },
    { snap_date: '2026-07-02', total_value: 101000, cash: 50000, market_value: 51000, daily_return: 0.01, cumulative_return: 0.01, benchmark_value: 3060 },
    { snap_date: '2026-07-03', total_value: 99000, cash: 50000, market_value: 49000, daily_return: -0.0198, cumulative_return: -0.01, benchmark_value: 3030 },
  ],
  trades: [
    { id: 1, stock_code: '000001.SZ', trade_type: 'BUY', price: 10.0, quantity: 1000, amount: 10000, commission: 5, stamp_duty: 0, bar_time: '2026-07-02T09:30:00', signal_name: 'open_sig', signal_type: 'OPEN' },
    { id: 2, stock_code: '000001.SZ', trade_type: 'SELL', price: 10.5, quantity: 1000, amount: 10500, commission: 5, stamp_duty: 5.25, bar_time: '2026-07-03T09:30:00', signal_name: 'close_sig', signal_type: 'CLOSE' },
  ],
  evaluations: {
    total_return: 0.05, annual_return: 0.8, max_drawdown: 0.02, volatility: 0.15,
    sharpe_ratio: 1.2, sortino_ratio: 1.5, calmar_ratio: 4.0, win_rate: 0.6,
    profit_factor: 2.0, total_trades: 2, benchmark_return: 0.03, avg_holding_days: 1.5,
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(getBacktestRecords as any).mockResolvedValue(mockRecords)
  ;(getPortfolios as any).mockResolvedValue(mockPortfolios)
  ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
})

describe('Backtest.vue — 列表视图', () => {
  it('挂载后渲染回测记录列表', async () => {
    const w = mount(Backtest)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    expect(rows.length).toBe(2)
    expect(w.text()).toContain('回测A')
    expect(w.text()).toContain('回测B')
  })

  it('记录状态显示中文（已完成/失败），含进度', async () => {
    const w = mount(Backtest)
    await flushPromises()
    expect(w.text()).toContain('已完成')
    expect(w.text()).toContain('失败')
    expect(w.text()).toContain('100%')
  })

  it('每行含[查看]按钮', async () => {
    const w = mount(Backtest)
    await flushPromises()
    expect(w.findAll('button').filter(b => b.text().includes('查看')).length).toBe(2)
  })

  it('每行含[删除]按钮，点击确认后调 deleteBacktestRecord 并刷新列表', async () => {
    ;(deleteBacktestRecord as any).mockResolvedValue({})
    vi.stubGlobal('confirm', () => true)  // 用户点确认
    const w = mount(Backtest)
    await flushPromises()

    const delBtns = w.findAll('button').filter(b => b.text().includes('删除'))
    expect(delBtns.length).toBe(2)  // 每行一个
    await delBtns[0].trigger('click')
    await flushPromises()

    expect(deleteBacktestRecord).toHaveBeenCalledWith(1)  // 删第一条
    // 列表刷新：getBacktestRecords 被再次调用
    expect(getBacktestRecords).toHaveBeenCalledTimes(2)  // 初始 1 + 删除后 1
    vi.unstubAllGlobals()
  })

  it('删除点取消 → 不调 deleteBacktestRecord', async () => {
    ;(deleteBacktestRecord as any).mockClear()
    vi.stubGlobal('confirm', () => false)  // 用户点取消
    const w = mount(Backtest)
    await flushPromises()

    const delBtn = w.findAll('button').filter(b => b.text().includes('删除'))[0]
    await delBtn.trigger('click')
    await flushPromises()

    expect(deleteBacktestRecord).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('load 失败 → 显示错误条，列表清空，页面不崩', async () => {
    ;(getBacktestRecords as any).mockRejectedValue({ response: { data: { message: '网络中断' } } })
    const w = mount(Backtest)
    await flushPromises()

    expect(w.text()).toContain('加载失败')
    expect(w.text()).toContain('网络中断')
    expect(w.findAll('tbody tr').length).toBe(0)
    w.unmount()
  })

  it('查看详情失败 → alert 提示，不进详情视图', async () => {
    ;(getBacktestDetail as any).mockRejectedValue({ response: { data: { message: '记录不存在' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Backtest)
    await flushPromises()

    const viewBtn = w.findAll('button').filter(b => b.text().includes('查看'))[0]
    await viewBtn.trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(alertMock.mock.calls[0][0]).toContain('加载详情失败')
    // 仍在列表视图（未切到详情报告页）
    expect(w.find('.report-page').exists()).toBe(false)
    vi.unstubAllGlobals()
  })
})

describe('Backtest.vue — 发起回测', () => {
  it('点[+发起回测]弹 Modal，含组合下拉/名称/起止日期', async () => {
    const w = mount(Backtest)
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +发起回测
    expect(w.find('.modal-overlay').exists()).toBe(true)
    // 组合下拉含已加载组合
    const poolSelect = w.findAll('select').find(s => s.html().includes('稳健组合'))
    expect(poolSelect).toBeTruthy()
    // 起止日期 input
    expect(w.find('input[type="date"]').exists()).toBe(true)
  })

  it('填表提交 → 调 runBacktest，参数含 portfolio_strategy_id/name/起止日期', async () => {
    ;(runBacktest as any).mockResolvedValue({ record_id: 99 })
    vi.stubGlobal('alert', () => {})  // 吞掉校验 alert，避免噪声
    const w = mount(Backtest)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    await w.find('input[placeholder*="回测"]').setValue('NEW_BT')
    const dateInputs = w.findAll('input[type="date"]')
    await dateInputs[0].setValue('2026-07-01')
    await dateInputs[1].setValue('2026-07-31')
    await w.find('.modal-actions button.btn-primary').trigger('click')  // 确定
    await flushPromises()

    expect(runBacktest).toHaveBeenCalledTimes(1)
    const arg = (runBacktest as any).mock.calls[0][0]
    expect(arg.name).toBe('NEW_BT')
    expect(arg.portfolio_strategy_id).toBe(1)  // 默认选第一个
    expect(arg.start_date).toBe('2026-07-01')
    expect(arg.end_date).toBe('2026-07-31')
    vi.unstubAllGlobals()
  })

  it('提交失败 → alert 错误且弹窗保持打开', async () => {
    ;(runBacktest as any).mockRejectedValue({ response: { data: { message: '组合不存在' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Backtest)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')
    await w.find('input[placeholder*="回测"]').setValue('X')
    const dateInputs = w.findAll('input[type="date"]')
    await dateInputs[0].setValue('2026-07-01')
    await dateInputs[1].setValue('2026-07-31')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(w.text()).toContain('发起回测')  // 弹窗仍开
    vi.unstubAllGlobals()
  })

  it('开始日期晚于结束日期 → 前端拦截，不调 runBacktest', async () => {
    ;(runBacktest as any).mockClear()
    const alertMock = vi.fn()
    vi.stubGlobal('alert', alertMock)
    const w = mount(Backtest)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal
    await w.find('input[placeholder*="回测"]').setValue('RANGE_BT')
    const dateInputs = w.findAll('input[type="date"]')
    await dateInputs[0].setValue('2026-08-31')  // start 晚于 end
    await dateInputs[1].setValue('2026-08-01')
    await w.find('.modal-actions button.btn-primary').trigger('click')  // 确定
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(runBacktest).not.toHaveBeenCalled()  // 前端拦截，不发请求
    vi.unstubAllGlobals()
  })
})

describe('Backtest.vue — 详情视图', () => {
  it('点[查看] → 切换详情，调 getBacktestDetail，显示指标卡/曲线/交易表', async () => {
    const w = mount(Backtest)
    await flushPromises()

    const viewBtn = w.findAll('button').find(b => b.text().includes('查看'))!
    await viewBtn.trigger('click')
    await flushPromises()

    expect(getBacktestDetail).toHaveBeenCalledTimes(1)
    expect((getBacktestDetail as any).mock.calls[0][0]).toBe(1)
    // 评估指标卡：核心指标
    expect(w.text()).toContain('总收益')
    expect(w.text()).toContain('最大回撤')
    expect(w.text()).toContain('夏普')
    // 净值曲线 echarts 容器
    expect(w.find('.chart-container').exists()).toBe(true)
    // 交易明细表（trade_type BUY → 显示"买入"）
    expect(w.text()).toContain('000001.SZ')
    expect(w.text()).toContain('买入')
  })

  it('详情有[←返回] → 回列表', async () => {
    const w = mount(Backtest)
    await flushPromises()
    const viewBtn = w.findAll('button').find(b => b.text().includes('查看'))!
    await viewBtn.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('总收益')  // 在详情

    const backBtn = w.findAll('button').find(b => b.text().includes('返回'))!
    await backBtn.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('回测A')  // 回到列表
    expect(w.text()).not.toContain('总收益')
  })

  it('评估指标百分比显示（total_return 0.05 → 5%）', async () => {
    const w = mount(Backtest)
    await flushPromises()
    const viewBtn = w.findAll('button').find(b => b.text().includes('查看'))!
    await viewBtn.trigger('click')
    await flushPromises()

    // total_return=0.05 → 5.00%
    expect(w.text()).toContain('5.00%')
    // max_drawdown=0.02 → 2.00%
    expect(w.text()).toContain('2.00%')
  })

  it('净值曲线含基准线（snapshots 带 benchmark_value → echarts setOption legend 含"基准"）', async () => {
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    const viewBtn = w.findAll('button').find(b => b.text().includes('查看'))!
    await viewBtn.trigger('click')
    await flushPromises()

    // echarts.init 被调用，返回的实例 setOption 含基准 series
    expect(init).toHaveBeenCalled()
    const chartInstance = (init as any).mock.results[0].value
    expect(chartInstance.setOption).toHaveBeenCalled()
    const option = chartInstance.setOption.mock.calls[0][0]
    expect(option.legend.data).toContain('基准')
    const benchSeries = option.series.find((s: any) => s.name === '基准')
    expect(benchSeries).toBeTruthy()
    // 基准归一化：3000→0%，3060→2%，3030→1%
    expect(benchSeries.data).toHaveLength(3)
  })

  it('风控与拦截统计：渲染聚合闸门行，点行下钻逐笔明细', async () => {
    ;(getBacktestDetail as any).mockResolvedValueOnce({
      ...mockDetail,
      decision_summary: [
        { gate: 'stop_loss', layer: 'strategy_risk', action: 'trigger',
          param_name: 'stop_loss_ratio', param_value: 0.05, count: 2,
          first_bar_time: '2026-07-02T10:00:00', last_bar_time: '2026-07-03T10:00:00',
          stock_count: 1, requested_qty_sum: 1000, final_qty_sum: 1000 },
        { gate: 'insufficient_funds', layer: 'capital_gate', action: 'reject',
          param_name: 'cash', param_value: null, count: 5,
          first_bar_time: '2026-07-02T09:35:00', last_bar_time: '2026-07-03T14:00:00',
          stock_count: 2, requested_qty_sum: 3000, final_qty_sum: 0 },
      ],
      decisions: [
        { id: 101, gate: 'stop_loss', layer: 'strategy_risk', action: 'trigger',
          stock_code: '000001.SZ', strategy_id: 1, bar_time: '2026-07-02T10:00:00',
          param_name: 'stop_loss_ratio', param_value: 0.05, actual_value: 0.052,
          requested_qty: 1000, final_qty: 1000, message: '亏损 5.2% 超止损线 5%' },
        { id: 102, gate: 'insufficient_funds', layer: 'capital_gate', action: 'reject',
          stock_code: '600000.SH', strategy_id: 2, bar_time: '2026-07-02T09:35:00',
          param_name: 'cash', param_value: null, actual_value: null,
          requested_qty: 1000, final_qty: 0, message: '开仓资金不足1手' },
      ],
    })
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    // 面板标题 + 两个闸门中文名
    expect(w.text()).toContain('风控与拦截统计')
    expect(w.text()).toContain('止损')
    expect(w.text()).toContain('资金不足拒单')
    // 比率阈值转百分比
    expect(w.text()).toContain('5.00%')

    // 点「资金不足拒单」行下钻：该闸门逐笔（600000.SH）出现，止损笔（000001.SZ）不在下钻区
    const rows = w.findAll('tr.decision-row')
    const fundsRow = rows.find(r => r.text().includes('资金不足拒单'))!
    await fundsRow.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('开仓资金不足1手')
    expect(w.text()).toContain('600000.SH')
  })
})
