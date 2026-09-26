import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

vi.mock('../api', () => ({
  getBacktestRecords: vi.fn(),
  getBacktestDetail: vi.fn(),
  runBacktest: vi.fn(),
  getPortfolios: vi.fn(),
  deleteBacktestRecord: vi.fn(),
  recomputeCrisisAlpha: vi.fn(),
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
  deleteBacktestRecord, recomputeCrisisAlpha,
} from '../api'

const scrollTargets: any[] = []

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
  scrollTargets.length = 0
  ;(Element.prototype as any).scrollIntoView = vi.fn(function (this: any) {
    scrollTargets.push(this)
  })
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
    const today = new Date()
    const fmt = (d: Date) => {
      const m = String(d.getMonth() + 1).padStart(2, '0')
      const day = String(d.getDate()).padStart(2, '0')
      return `${d.getFullYear()}-${m}-${day}`
    }
    const dateInputs = w.findAll('input[type="date"]')
    expect((dateInputs[0].element as HTMLInputElement).value).toBe('2021-01-01')
    expect((dateInputs[1].element as HTMLInputElement).value).toBe(fmt(today))
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

// --- 收益分布区块测试 ---
describe('Backtest.vue — 收益分布区块', () => {
  const mockDetailWithReturns = {
    ...mockDetail,
    returns_buckets: {
      monthly_matrix: {
        years: [2026],
        months: ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'],
        matrix: [[null, null, null, null, null, null, null, 0.5, -0.2, 1.0, 0.3, 0.8]],
        text: [['','','','','','','','+0.50%','-0.20%','+1.00%','+0.30%','+0.80%']],
      },
      yearly: [{
        year: 2026,
        daily_returns: [0.01, -0.005, 0.003, 0.02, -0.01],
        monthly_returns: [0.01, -0.005],
        annual_return: 0.02,
      }],
    },
  }
  const mockDetailNoReturns = { ...mockDetail, returns_buckets: null }

  it('有 returns_buckets 时渲染收益分布标题', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailWithReturns)
    const w = mount(Backtest)
    await flushPromises()
    // 点击查看进入详情
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('收益分布')
  })

  it('returns_buckets 为 null 时不渲染收益分布区块', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailNoReturns)
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()
    expect(w.text()).not.toContain('收益分布')
  })
})

// --- 收益分布图表 A 股红涨绿跌配色 ---
describe('Backtest.vue — 收益分布 A 股红涨绿跌配色', () => {
  const mockDetailCN = {
    ...mockDetail,
    returns_buckets: {
      monthly_matrix: {
        years: [2026],
        months: ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'],
        // 含正收益（0.5/1.0/0.8）与负收益（-0.2）→ 正应红、负应绿
        matrix: [[null, null, null, null, null, null, null, 0.5, -0.2, 1.0, 0.3, 0.8]],
        text: [['','','','','','','','+0.50%','-0.20%','+1.00%','+0.30%','+0.80%']],
      },
      yearly: [
        { year: 2025, daily_returns: [0.01, -0.01, 0.02, -0.02, 0.03], monthly_returns: [0.02, -0.01, 0.015, -0.03, 0.01], annual_return: -0.05 },
        { year: 2026, daily_returns: [0.01, -0.005, 0.003, 0.02, -0.01], monthly_returns: [0.01, -0.005, 0.02, -0.01, 0.004], annual_return: 0.02 },
      ],
    },
  }

  it('月度热力图 visualMap 正收益红、负收益绿（inRange 终点=红）', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailCN)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const heat = options.find((o: any) => o.series?.[0]?.type === 'heatmap')
    expect(heat).toBeTruthy()
    const colors = heat.visualMap?.inRange?.color
    expect(colors).toHaveLength(3)
    // A 股惯例：涨（正值）红、跌（负值）绿 → 渐变序列负端绿、正端红
    expect(colors[0]).toBe('#10b981')  // 负收益端（min）→ 绿
    expect(colors[2]).toBe('#ef4444')  // 正收益端（max）→ 红
  })

  it('年收益柱状图正收益红、负收益绿', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailCN)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const barOpt = options.find((o: any) => o.series?.[0]?.type === 'bar')
    expect(barOpt).toBeTruthy()
    const data = barOpt.series[0].data
    expect(data).toHaveLength(2)
    const byName = Object.fromEntries(data.map((d: any) => [d.name, d.itemStyle.color]))
    expect(byName['2025']).toBe('#10b981')  // 年收益 -5% → 绿
    expect(byName['2026']).toBe('#ef4444')  // 年收益 +2% → 红
  })
})

// --- 图表数值精度测试（回撤曲线 / 日收益箱线 / 月收益箱线 → 4 位小数）---
describe('Backtest.vue — 图表数值精度', () => {
  // 构造含多位小数的 daily/monthly 收益，验证 ×100 后仍被 round 到 4 位
  const mockDetailPrecision = {
    ...mockDetail,
    returns_buckets: {
      monthly_matrix: {
        years: [2026],
        months: ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'],
        matrix: [[null, null, null, null, null, null, null, 0.5, -0.2, 1.0, 0.3, 0.8]],
        text: [['','','','','','','','+0.50%','-0.20%','+1.00%','+0.30%','+0.80%']],
      },
      yearly: [{
        year: 2026,
        // 含 5~7 位小数 → ×100 后仍有 5 位以上，断言 boxData 值已是 4 位
        daily_returns: [0.001234567, -0.005432, 0.0033333, 0.02, -0.01],
        monthly_returns: [0.001234567, -0.005432, 0.02, -0.01, 0.004],
        annual_return: 0.02,
      }],
    },
  }
  // 回撤由 mockDetail.snapshots 算：peak=101000 时 99000 → -(2000/101000)*100 ≈ -1.980198...
  // 断言每个回撤/箱线值 round 到 4 位（值 === Number(v.toFixed(4))）
  const assert4Decimals = (values: number[]) => {
    for (const v of values) {
      expect(Number(v.toFixed(4))).toBe(v)
    }
  }

  it('回撤曲线数据 round 到 4 位小数', async () => {
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const dd = options.find((o: any) => o.yAxis?.name === '回撤(%)')
    expect(dd).toBeTruthy()
    const data = dd.series.find((s: any) => s.name === '回撤').data
    assert4Decimals(data)
    // tooltip formatter 也输出 4 位百分比（含 toFixed(4)）
    expect(String(dd.tooltip.formatter)).toContain('toFixed(4)')
  })

  it('年度日收益箱线图五数摘要 round 到 4 位小数', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailPrecision)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const box = options.find((o: any) => o.yAxis?.name === '日收益率(%)')
    expect(box).toBeTruthy()
    const boxData = box.series[0].data
    expect(Array.isArray(boxData)).toBe(true)
    expect(boxData.length).toBe(1)  // 一个年份
    for (const five of boxData) {
      assert4Decimals(five)
    }
  })

  it('各年月收益箱线图五数摘要 round 到 4 位小数', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetailPrecision)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const box = options.find((o: any) => o.yAxis?.name === '月收益率(%)')
    expect(box).toBeTruthy()
    const boxData = box.series[0].data
    expect(Array.isArray(boxData)).toBe(true)
    expect(boxData.length).toBe(1)
    for (const five of boxData) {
      assert4Decimals(five)
    }
  })
})

// --- 回测报告 — 策略对比/摘要卡红涨绿跌 ---
describe('Backtest.vue — 策略对比/摘要卡红涨绿跌', () => {
  it('策略对比收益指标正红负绿（valueClass 反转）', async () => {
    ;(getBacktestDetail as any).mockResolvedValue({
      ...mockDetail,
      strategy_evaluations: [
        { strategy_id: 1, strategy_name: '正收益策略', total_return: 0.05, annual_return: 0.08 },
        { strategy_id: 2, strategy_name: '负收益策略', total_return: -0.03, annual_return: -0.05 },
      ],
    })
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('策略对比分析')
    expect(w.text()).toContain('正收益策略')
    expect(w.text()).toContain('负收益策略')

    // 组合整体卡（.portfolio-card）总收益行：总收益 5.00%（正）→ valueClass → text-red
    // 注意：不能全量 findAll('.metric-row') 后 find('5.00%')——DOM 顺序上 .strategy-card 在前，
    // 首个命中会是正收益策略卡的总收益行；且 includes('5.00%') 也会误匹配 '-5.00%'。
    // 故限定在 .portfolio-card 内按标签定位总收益行，并对值做精确断言。
    const portfolioRows = w.findAll('.portfolio-card .metric-row')
    const posRow = portfolioRows.find(r => r.find('.metric-label').text() === '总收益')!
    expect(posRow.find('span:last-child').text()).toBe('5.00%')
    expect(posRow.find('span:last-child').classes()).toContain('text-red')
    // 负收益策略卡：总收益 -3.00%（负）→ 精确匹配该值且值为 text-green
    const negCard = w.findAll('.strategy-card').find(c => c.text().includes('-3.00%'))!
    const negSpan = negCard.findAll('span').find(s => s.text() === '-3.00%')!
    expect(negSpan.classes()).toContain('text-green')
  })

  it('摘要卡结构：positive（总收益/年化收益）+ danger（最大回撤）存在且各自取值', async () => {
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const positive = w.find('.summary-card.positive')
    expect(positive.exists()).toBe(true)
    expect(positive.text()).toContain('总收益')
    const danger = w.find('.summary-card.danger')
    expect(danger.exists()).toBe(true)
    expect(danger.text()).toContain('最大回撤')
    // 注意：SFC scoped 样式在 vitest(happy-dom) 不注入，边框色无法经 getComputedStyle 断言；
    // 颜色改动（positive→#ef4444、danger 保持 #ef4444）由实现步骤的显式 CSS 修改 + 最终构建验证保证。
  })
})

// --- 回测报告 — 回撤曲线 A股红涨绿跌 ---
describe('Backtest.vue — 回撤曲线红涨绿跌', () => {
  it('回撤曲线（跌幅）为绿色', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const dd = options.find((o: any) => o.yAxis?.name === '回撤(%)')
    expect(dd).toBeTruthy()
    const series = dd.series.find((s: any) => s.name === '回撤')
    expect(series).toBeTruthy()
    expect(series.itemStyle.color).toBe('#10b981')
  })
})

// --- 回测报告 — 净值曲线超额收益 ---
describe('Backtest.vue — 净值曲线超额收益（相对基准）', () => {
  it('有基准数据时净值曲线含超额收益线（组合%−基准%）', async () => {
    ;(getBacktestDetail as any).mockResolvedValue(mockDetail)
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const option = (init as any).mock.results[0].value.setOption.mock.calls[0][0]
    expect(option.legend.data).toContain('超额收益（相对基准）')
    const excess = option.series.find((s: any) => s.name === '超额收益（相对基准）')
    expect(excess).toBeTruthy()
    expect(excess.type).toBe('line')
    expect(excess.smooth).toBe(true)
    expect(excess.symbol).toBe('none')
    expect(excess.lineStyle).toEqual({ width: 1.5, color: '#f59e0b' })
    expect(excess.itemStyle.color).toBe('#f59e0b')
    // 手算：组合收益% [0,1,-1] − 基准% [0,2,1] = [0,-1,-2]
    expect(excess.data).toEqual([0, -1, -2])
  })

  it('无基准数据（benchmark_value 全 null）时不画超额收益线', async () => {
    ;(getBacktestDetail as any).mockResolvedValue({
      ...mockDetail,
      snapshots: mockDetail.snapshots.map((s: any) => ({ ...s, benchmark_value: null })),
    })
    const { init } = await import('echarts')
    const w = mount(Backtest)
    await flushPromises()
    await w.findAll('button').find(b => b.text().includes('查看'))!.trigger('click')
    await flushPromises()

    const option = (init as any).mock.results[0].value.setOption.mock.calls[0][0]
    expect(option.legend.data).not.toContain('超额收益（相对基准）')
    expect(option.series.find((s: any) => s.name === '超额收益（相对基准）')).toBeUndefined()
  })
})

const crisisAlphaCompleted = {
  status: 'completed',
  error: null,
  skipped_benchmarks: ['中证全指', '科创50'],
  computed_at: '2026-09-25T10:00:00',
  benchmark_code: '000300.SH',
  benchmark_name: '沪深300',
  data: {
    strategy_name: '稳健组合',
    benchmark_code: '000300.SH',
    benchmark_name: '沪深300',
    alpha_rating: '优秀',
    n_weeks: 12,
    quadrant: {
      n_crisis: 6, n_down: 6, n_total: 12,
      hit_rate: 0.5, up_rate: 0.5, alpha_ratio: 0.6667, follow_ratio: 0.3333, odds: 1.2,
      counts: { 逆势上涨: 3, 跌得少: 1, 跟着跌: 1, 跌更多: 1 },
      avg_ret: { 逆势上涨: 0.02, 跌得少: -0.03, 跟着跌: -0.08, 跌更多: -0.15 },
      down_avg_s: -0.0283, down_avg_b: -0.07, win_avg: 0.02, loss_avg: 0.08,
    },
    conditions: [
      { threshold: -0.03, n: 4, b_avg: -0.06, s_avg: -0.01, up_rate: 0.5 },
      { threshold: -0.05, n: 2, b_avg: -0.09, s_avg: 0.0, up_rate: 0.5 },
    ],
    extreme_top: [
      { 日期: '2024-02-02', 基准: -0.12, 策略: 0.01, 超额: 0.13, 分类: '逆势上涨' },
      { 日期: '2024-02-09', 基准: -0.1, 策略: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-26', 基准: -0.1, 策略: -0.08, 超额: 0.02, 分类: '跌得少' },
    ],
    deep_dd_events: [
      { start: '2024-01-12', end: '2024-02-02', benchmark_ret: -0.12, strategy_ret: 0.01, excess: 0.13, hedged: true, verdict: '逆势对冲', max_dd: -0.14, days: 21 },
      { start: '2024-02-16', end: '2024-02-23', benchmark_ret: -0.08, strategy_ret: -0.09, excess: -0.01, hedged: false, verdict: '同步下跌', max_dd: -0.09, days: 7 },
    ],
    rolling_corr: [
      { date: '2024-01-05', value: 0.4 },
      { date: '2024-01-12', value: 0.3 },
      { date: '2024-01-19', value: 0.2 },
    ],
    expanding_corr: [
      { date: '2023-12-29', value: 0.6 },
      { date: '2024-01-05', value: 0.4 },
    ],
    monthly: {
      states: {
        大涨: { n: 1, s_avg: 0.03, b_avg: 0.06, win_rate: 1 },
        小涨: { n: 2, s_avg: 0.01, b_avg: 0.02, win_rate: 0.5 },
        小跌: { n: 3, s_avg: -0.01, b_avg: -0.02, win_rate: 0.3333 },
        大跌: { n: 1, s_avg: 0.005, b_avg: -0.07, win_rate: 1 },
      },
      hedge_ratio: 0.5,
      n_down_month: 4,
      big_down: [{ 日期: '2024-02-29', 基准: -0.07, 策略: 0.005 }],
    },
    annual: [
      { 年份: 2024, 下跌周: 6, 危机Alpha: 0.6667, 策略收益: 0.05, 基准收益: -0.08, 完整: false },
    ],
    cost: { drag: 0.25, sync_up: 0.75, elasticity: 0.4, tail_corr: -0.2, q1_corr: -0.15, n_up: 4 },
    performance: {
      s: { cum: 0.05, ann: 0.06, vol: 0.12, mdd: -0.05 },
      b: { cum: -0.08, ann: -0.09, vol: 0.2, mdd: -0.15 },
      corr: 0.25,
      start: '2024-01-01',
      end: '2024-03-29',
    },
    score: {
      total_score: 72.3,
      rating: '优秀',
      dimensions: [
        { 维度: '危机Alpha占比', 类型: '核心', 权重: 0.2, 得分: 78.7, 评级: '优秀', 贡献: 15.7 },
        { 维度: '深跌周抗跌性', 类型: '核心', 权重: 0.15, 得分: 65.0, 评级: '一般', 贡献: 9.8 },
        { 维度: '下尾负相关', 类型: '对冲', 权重: 0.12, 得分: 85.0, 评级: '卓越', 贡献: 10.2 },
      ],
    },
    weekly_detail: [
      { 日期: '2024-01-05', 基准收益: -0.1, 策略收益: 0.02, 超额: 0.12, 分类: '逆势上涨' },
      { 日期: '2024-01-12', 基准收益: -0.1, 策略收益: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-19', 基准收益: -0.1, 策略收益: -0.06, 超额: 0.04, 分类: '跌得少' },
      { 日期: '2024-01-26', 基准收益: -0.1, 策略收益: -0.12, 超额: -0.02, 分类: '跌更多' },
      { 日期: '2024-02-02', 基准收益: 0.05, 策略收益: 0.03, 超额: -0.02, 分类: '跟着跌' },
      { 日期: '2024-02-09', 基准收益: 0.05, 策略收益: -0.01, 超额: -0.06, 分类: '跟着跌' },
    ],
    down_weeks: [
      { 日期: '2024-01-26', 基准收益: -0.1, 策略收益: -0.12, 超额: -0.02, 分类: '跌更多' },
      { 日期: '2024-01-19', 基准收益: -0.1, 策略收益: -0.06, 超额: 0.04, 分类: '跌得少' },
      { 日期: '2024-01-12', 基准收益: -0.1, 策略收益: -0.02, 超额: 0.08, 分类: '跌得少' },
      { 日期: '2024-01-05', 基准收益: -0.1, 策略收益: 0.02, 超额: 0.12, 分类: '逆势上涨' },
    ],
    nav: {
      dates: ['2024-01-01', '2024-02-01', '2024-03-29'],
      strategy: [1, 1.03, 1.05],
      benchmark: [1, 0.95, 0.92],
      shade_spans: [['2024-01-12', '2024-02-02']],
    },
    multi_bench: [
      { key: 'main', name: '沪深300', code: '000300.SH', alpha_ratio: 0.6667, n_down: 6, up_rate: 0.5, is_main: true },
      { key: 'csi500', name: '中证500', code: '000905.SH', alpha_ratio: 0.6, n_down: 6, up_rate: 0.45, is_main: false },
    ],
  },
}

async function mountDetail(detailPayload: any) {
  ;(getBacktestDetail as any).mockResolvedValue(detailPayload)
  const w = mount(Backtest)
  await flushPromises()
  const viewBtn = w.findAll('button').filter((b) => b.text().includes('查看'))[0]
  await viewBtn.trigger('click')
  await flushPromises()
  const caHeader = w.find('.ca-header')
  if (caHeader.exists()) {
    await caHeader.trigger('click')
    await flushPromises()
  }
  return w
}

describe('Backtest.vue — 危机Alpha 上提到整体表现指标（8 卡）', () => {
  it('crisis_alpha 为 null：共 8 张卡，危机两卡为 —', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards.length).toBe(8)
    const labels = cards.map((c) => c.find('.label').text())
    expect(labels[6]).toBe('危机Alpha占比')
    expect(labels[7]).toBe('综合危机保护评分')
    expect(cards[6].find('.value').text()).toBe('—')
    expect(cards[7].find('.value').text()).toBe('—')
  })

  it('status=completed：显示「占比 · 评级」与「评分 · 评级」', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards[6].find('.value').text()).toBe('66.7% · 优秀')
    expect(cards[7].find('.value').text()).toBe('72.3 · 优秀')
  })

  it('status=failed：危机两卡降级为 —', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: '主基准指数无数据', skipped_benchmarks: [],
        computed_at: '2026-09-25T10:00:00', benchmark_code: '000300.SH',
        benchmark_name: '沪深300', data: null,
      },
    })
    const cards = w.findAll('.stats-grid .stat-card')
    expect(cards[6].find('.value').text()).toBe('—')
    expect(cards[7].find('.value').text()).toBe('—')
  })
})

describe('Backtest.vue — 危机Alpha section 骨架', () => {
  it('crisis_alpha=null：显示未计算提示 + 重新计算按钮', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    expect(w.find('.ca-section').exists()).toBe(true)
    expect(w.text()).toContain('🌪️ 危机Alpha 专题分析')
    expect(w.text()).toContain('本次回测未包含危机Alpha分析')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(1)
  })

  it('status=failed：显示失败原因 + 重新计算按钮', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: '主基准指数无数据（TQ 取数失败或代码不被支持）',
        skipped_benchmarks: ['中证全指', '中证500'], computed_at: '2026-09-25T10:00:00',
        benchmark_code: '000300.SH', benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.text()).toContain('主基准指数无数据')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(1)
  })

  it('status=insufficient：显示样本不足说明且无重算按钮', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'insufficient',
        error: '策略与基准共同交易日不足30天（12），无法分析',
        skipped_benchmarks: [], computed_at: '2026-09-25T10:00:00',
        benchmark_code: '000300.SH', benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.text()).toContain('样本不足')
    expect(w.text()).toContain('30')
    const btns = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))
    expect(btns.length).toBe(0)
  })

  it('status=completed：4 张 KPI 卡 + 状态条（跳过基准 + 计算时间 + 重新计算）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const kpis = w.findAll('.ca-kpi')
    expect(kpis.length).toBe(4)
    expect(kpis[0].text()).toContain('危机Alpha占比')
    expect(kpis[0].text()).toContain('66.7%')
    expect(kpis[1].text()).toContain('综合危机保护评分')
    expect(kpis[1].text()).toContain('72.3')
    expect(kpis[2].text()).toContain('下跌周逆势上涨比例')
    expect(kpis[2].text()).toContain('3 / 6 周')
    expect(kpis[3].text()).toContain('策略累计收益')
    expect(kpis[3].text()).toContain('年化 6.0%')
    expect(kpis[3].text()).toContain('MDD -5.0%')
    const status = w.find('.ca-status').text()
    expect(status).toContain('已跳过基准：中证全指、科创50')
    expect(status).toContain('2026-09-25 10:00:00')
    expect(w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算')).length).toBe(1)
  })

  it('点击重新计算 → 调 recomputeCrisisAlpha → 成功后重载详情并渲染 KPI', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(recomputeCrisisAlpha).toHaveBeenCalledWith(1)
    expect(getBacktestDetail).toHaveBeenCalledTimes(2)
    expect(w.find('.ca-msg').text()).toContain('重新计算完成')
    expect(w.findAll('.ca-kpi').length).toBe(4)
    expect(w.text()).toContain('66.7%')
  })

  it('重算返回 failed：显示失败原因，不抛错', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'failed', error: '主基准指数无数据', computed_at: null,
    })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.find('.ca-msg').text()).toContain('主基准指数无数据')
  })

  it('重算接口抛错：显示失败提示', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockRejectedValue(new Error('boom'))
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.find('.ca-msg').text()).toContain('重新计算失败')
  })

  it('重算返回基准不一致：消息含告警样式与新旧基准名', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
      benchmark_resolution: {
        from_snapshot: true, mismatch: true,
        benchmark_code: '000905.SH', benchmark_name: '中证500',
        live_benchmark_code: '000300.SH', live_benchmark_name: '沪深300',
        stored_benchmark_code: '000905.SH', stored_benchmark_name: '中证500',
      },
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    const msg = w.find('.ca-msg')
    expect(msg.classes()).toContain('ca-warn')
    expect(msg.text()).toContain('重新计算完成')
    expect(msg.text()).toContain('中证500')
    expect(msg.text()).toContain('沪深300')
  })

  it('重算返回无基准不一致：不加告警样式', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
      benchmark_resolution: {
        from_snapshot: true, mismatch: false,
        benchmark_code: '000300.SH', benchmark_name: '沪深300',
        live_benchmark_code: '000300.SH', live_benchmark_name: '沪深300',
        stored_benchmark_code: '000300.SH', stored_benchmark_name: '沪深300',
      },
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    const msg = w.find('.ca-msg')
    expect(msg.classes()).not.toContain('ca-warn')
    expect(msg.text()).toBe('重新计算完成')
  })

  it('重算无 benchmark_resolution 字段（老后端）：不报错', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    const msg = w.find('.ca-msg')
    expect(msg.classes()).not.toContain('ca-warn')
    expect(msg.text()).toBe('重新计算完成')
  })

  it('老记录无快照 + 组合基准已被改：消息提示按记录原基准重算', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: null })
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
      benchmark_resolution: {
        from_snapshot: false, mismatch: true,
        benchmark_code: '000300.SH', benchmark_name: '沪深300',
        live_benchmark_code: '000905.SH', live_benchmark_name: '中证500',
        stored_benchmark_code: '000300.SH', stored_benchmark_name: '沪深300',
      },
    })
    ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    const msg = w.find('.ca-msg')
    expect(msg.classes()).toContain('ca-warn')
    expect(msg.text()).toContain('中证500')
    expect(msg.text()).toContain('沪深300')
  })

  it('点击危机卡滚动到危机Alpha section', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    await w.findAll('.stats-grid .stat-card')[6].trigger('click')
    expect(scrollTargets.length).toBe(1)
    expect(String(scrollTargets[0].className)).toContain('ca-section')
  })
})

function caTableByHeader(w: any, header: string) {
  return w.findAll('table').find((t: any) => t.find('thead').text().includes(header))!
}

describe('Backtest.vue — 危机Alpha 模块一~六', () => {
  it('渲染模块一~六标题（含 TOP 数量与下跌周数）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    for (const t of [
      '一、核心结论', '二、产品与基准走势', '三、危机Alpha四档分类',
      '四、极端下跌周 TOP3', '五、全周期四象限分布', '六、深度回撤事件独立性',
    ]) {
      expect(w.text()).toContain(t)
    }
  })

  it('核心结论段落含下跌周均值 / 相关系数 / 回撤事件对冲数 + 两个评级标签', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('在 6 个沪深300下跌周中')
    expect(text).toContain('66.7%')
    expect(text).toContain('50.0%')
    expect(text).toContain('-2.83%')
    expect(text).toContain('-7.00%')
    expect(text).toContain('0.250')
    expect(text).toContain('最深的 2 次基准回撤中有 1 次逆势录得正收益')
    expect(text).toContain('危机Alpha 66.7% · 优秀')
    expect(text).toContain('综合评分 72.3 · 优秀')
  })

  it('二：净值走势图容器 + 绩效表（5 行，含相关系数解读）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.findAll('.ca-chart').length).toBe(9)
    const text = w.text()
    for (const t of ['累计收益', '年化收益', '年化波动', '最大回撤', '相关系数']) {
      expect(text).toContain(t)
    }
    expect(text).toContain('低/负相关，对冲属性显著')
  })

  it('三：分类统计表（4 档 + 危机Alpha合计）与深跌周条件检验（2 行）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    for (const t of ['逆势上涨', '跌得少', '跟着跌', '跌更多', '危机Alpha合计']) {
      expect(text).toContain(t)
    }
    expect(text).toContain('深跌周条件检验（更严苛）')
    expect(text).toContain('沪深300周跌 >3%')
    expect(text).toContain('沪深300周跌 >5%')
    expect(w.findAll('.ca-cond-table tbody tr').length).toBe(2)
  })

  it('四：TOP3 说明含逆势正收益周数与跑赢周数', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('沪深300跌幅最大的 3 周中')
    expect(text).toContain('1 周逆势录得正收益')
    // fixture 3 周 extreme_top 全部 策略>基准（0.01>-0.12 / -0.02>-0.10 / -0.08>-0.10）→ 3 周跑赢
    expect(text).toContain('3 周跑赢基准')
  })

  it('五：四象限图 4 档分类 + 四象限标注（象限文字是 chart graphic，不进 DOM）', async () => {
    const { init } = await import('echarts')
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.text()).toContain('左上象限（股跌+策略涨）即')
    const options = (init as any).mock.results.map((r: any) => r.value.setOption.mock.calls[0][0])
    const sc = options.find((o: any) => o.xAxis?.name === '基准周收益(%)')
    expect(sc).toBeTruthy()
    expect(sc.series.map((s: any) => s.name)).toEqual(['逆势上涨', '跌得少', '跟着跌', '跌更多'])
    const texts = sc.graphic.map((g: any) => g.style.text)
    expect(texts.some((t: string) => t.includes('股跌+稳健组合涨'))).toBe(true)
    expect(texts.some((t: string) => t.includes('股涨+稳健组合跌'))).toBe(true)
    expect(texts).toContain('同涨')
    expect(texts).toContain('同跌')
  })

  it('六：事件表 2 行 + 回撤独立性说明', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(w.findAll('.ca-event-table tbody tr').length).toBe(2)
    expect(text).toContain('2024-01-12')
    expect(text).toContain('2024-02-02')
    expect(text).toContain('✓ 逆势对冲')
    expect(text).toContain('✗ 同步下跌')
    expect(text).toContain('回撤独立性 50%')
  })

  it('无数据时（status=failed）不渲染 12 模块与图表容器', async () => {
    const w = await mountDetail({
      ...mockDetail,
      crisis_alpha: {
        status: 'failed', error: 'x', skipped_benchmarks: [],
        computed_at: null, benchmark_code: '000300.SH',
        benchmark_name: '沪深300', data: null,
      },
    })
    expect(w.findAll('.ca-chart').length).toBe(0)
    expect(w.findAll('.ca-kpi').length).toBe(0)
    expect(w.text()).not.toContain('一、核心结论')
  })

  it('三：分类表平均周收益按符号着色（正红负绿，合计行不着色）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const table = caTableByHeader(w, '平均周收益')
    const cell = (cat: string) => {
      const row = table.findAll('tbody tr').find((r: any) => r.findAll('td')[0].text() === cat)!
      return row.findAll('td')[3]
    }
    expect(cell('逆势上涨').text()).toBe('+2.00%')
    expect(cell('逆势上涨').classes()).toContain('text-red')
    expect(cell('跌得少').text()).toBe('-3.00%')
    expect(cell('跌得少').classes()).toContain('text-green')
    expect(cell('跟着跌').classes()).toContain('text-green')
    expect(cell('跌更多').classes()).toContain('text-green')
    expect(cell('危机Alpha合计').text()).toBe('—')
    expect(cell('危机Alpha合计').classes()).not.toContain('text-red')
    expect(cell('危机Alpha合计').classes()).not.toContain('text-green')
  })

  it('三：条件检验策略平均按符号着色（股票平均列固定绿）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-cond-table tbody tr')
    const sAvg = (i: number) => rows[i].findAll('td')[3]
    const bAvg = (i: number) => rows[i].findAll('td')[2]
    expect(sAvg(0).text()).toBe('-1.00%')
    expect(sAvg(0).classes()).toContain('text-green')
    expect(sAvg(1).text()).toBe('+0.00%')
    expect(sAvg(1).classes()).toContain('text-red')
    expect(bAvg(0).classes()).toContain('text-green')
    expect(bAvg(1).classes()).toContain('text-green')
  })

  it('六：事件表策略/超额两列各自按符号着色（可正可负独立）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-event-table tbody tr')
    const cell = (i: number, idx: number) => rows[i].findAll('td')[idx]
    expect(cell(0, 3).text()).toBe('+1.00%')
    expect(cell(0, 3).classes()).toContain('text-red')
    expect(cell(0, 4).text()).toBe('+13.00%')
    expect(cell(0, 4).classes()).toContain('text-red')
    expect(cell(1, 3).text()).toBe('-9.00%')
    expect(cell(1, 3).classes()).toContain('text-green')
    expect(cell(1, 4).text()).toBe('-1.00%')
    expect(cell(1, 4).classes()).toContain('text-green')
  })
})

describe('Backtest.vue — 危机Alpha 模块七~十二', () => {
  it('渲染模块七~十二标题，累计 9 个图表容器', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    for (const t of [
      '七、多宽基一致性', '八、月度视角', '九、动态相关性与年度趋势',
      '十、代价检验（上涨周表现）', '十一、综合危机保护评分', '十二、全部下跌周明细（6 周）',
    ]) {
      expect(w.text()).toContain(t)
    }
    expect(w.findAll('.ca-chart').length).toBe(9)
  })

  it('12 个模块标题按源顺序渲染', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const titles = [
      '一、核心结论', '二、产品与基准走势', '三、危机Alpha四档分类', '四、极端下跌周 TOP3',
      '五、全周期四象限分布', '六、深度回撤事件独立性', '七、多宽基一致性', '八、月度视角',
      '九、动态相关性与年度趋势', '十、代价检验（上涨周表现）', '十一、综合危机保护评分',
      '十二、全部下跌周明细（6 周）',
    ]
    const text = w.text()
    let last = -1
    for (const t of titles) {
      const i = text.indexOf(t)
      expect(i, `标题缺失或顺序错误: ${t}`).toBeGreaterThan(last)
      last = i
    }
  })

  it('七：多宽基明细表含主基准标记与 2 个基准', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    expect(text).toContain('沪深300（主基准）')
    expect(text).toContain('中证500')
    expect(w.findAll('.ca-multi-table tbody tr').length).toBe(2)
  })

  it('八：月度 4 状态表 + 大跌月明细（1 行）+ 月度对冲比例', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const text = w.text()
    for (const t of ['大涨', '小涨', '小跌', '大跌']) expect(text).toContain(t)
    expect(text).toContain('大跌月明细（股票月跌 >5%）')
    expect(w.findAll('.ca-bigdown-table tbody tr').length).toBe(1)
    expect(text).toContain('下跌月份共 50.0% 实现危机对冲（月度口径）')
  })

  it('九：年度表带 * 标记（非完整年）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.text()).toContain('2024*')
    expect(w.text()).toContain('标 * 的年度为数据区间首尾的非完整年度')
  })

  it('十：代价检验 5 行', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.findAll('.ca-cost-table tbody tr').length).toBe(5)
    const text = w.text()
    for (const t of ['上涨周拖后腿比例', '上涨周同步上涨比例', '收益弹性比', '下尾相关（下跌周内）', 'Q1分位相关（最差25%周）']) {
      expect(text).toContain(t)
    }
  })

  it('十一：评分维度表 3 行 + 综合评分行', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-dim-table tbody tr')
    expect(rows.length).toBe(4)
    expect(w.text()).toContain('综合评分')
  })

  it('十二：下跌周明细表按日期倒序（2024-01-26 在 2024-01-05 之前）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const rows = w.findAll('.ca-down-table tbody tr')
    expect(rows.length).toBe(4)
    const text = w.text()
    expect(text.indexOf('2024-01-26')).toBeLessThan(text.indexOf('2024-01-05'))
  })

  it('四档配色为红/橙/浅绿/深绿（与红涨绿跌一致）', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const html = w.html()
    expect(html).toContain('#d6403a')
    expect(html).toContain('#f0a429')
    expect(html).toContain('#9dc88d')
    expect(html).toContain('#1f6e4f')
  })

  it('重算后重新初始化 9 个危机Alpha 图表', async () => {
    const w = await mountDetail({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    const before = w.findAll('.ca-chart').length
    expect(before).toBe(9)
    ;(recomputeCrisisAlpha as any).mockResolvedValue({
      status: 'completed', error: null, computed_at: '2026-09-25T11:00:00',
    })
    const btn = w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算'))[0]
    await btn.trigger('click')
    await flushPromises()
    expect(w.findAll('.ca-chart').length).toBe(9)
    expect(w.text()).toContain('重新计算完成')
  })
})

describe('Backtest.vue — 危机Alpha section 折叠', () => {
  async function mountCollapsed(detailPayload: any) {
    ;(getBacktestDetail as any).mockResolvedValue(detailPayload)
    const w = mount(Backtest)
    await flushPromises()
    const viewBtn = w.findAll('button').filter((b) => b.text().includes('查看'))[0]
    await viewBtn.trigger('click')
    await flushPromises()
    return w
  }

  it('进入详情默认折叠：标题可见，KPI/图表/模块均不渲染', async () => {
    const w = await mountCollapsed({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.find('.ca-header').exists()).toBe(true)
    expect(w.text()).toContain('🌪️ 危机Alpha 专题分析')
    expect(w.find('.ca-caret').classes()).not.toContain('open')
    expect(w.findAll('.ca-kpi').length).toBe(0)
    expect(w.findAll('.ca-chart').length).toBe(0)
    expect(w.text()).not.toContain('一、核心结论')
  })

  it('点击标题展开：渲染 KPI/9 图表/12 模块，箭头旋转', async () => {
    const w = await mountCollapsed({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    await w.find('.ca-header').trigger('click')
    await flushPromises()
    expect(w.find('.ca-caret').classes()).toContain('open')
    expect(w.findAll('.ca-kpi').length).toBe(4)
    expect(w.findAll('.ca-chart').length).toBe(9)
    expect(w.text()).toContain('十二、全部下跌周明细（6 周）')
  })

  it('展开后再次点击折叠', async () => {
    const w = await mountCollapsed({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    await w.find('.ca-header').trigger('click')
    await flushPromises()
    expect(w.findAll('.ca-chart').length).toBe(9)
    await w.find('.ca-header').trigger('click')
    await flushPromises()
    expect(w.findAll('.ca-chart').length).toBe(0)
    expect(w.find('.ca-caret').classes()).not.toContain('open')
  })

  it('折叠态点危机卡：自动展开内容并滚动到 section', async () => {
    const w = await mountCollapsed({ ...mockDetail, crisis_alpha: crisisAlphaCompleted })
    expect(w.findAll('.ca-chart').length).toBe(0)
    await w.findAll('.stats-grid .stat-card')[6].trigger('click')
    await flushPromises()
    expect(w.findAll('.ca-chart').length).toBe(9)
    expect(scrollTargets.length).toBe(1)
    expect(String(scrollTargets[0].className)).toContain('ca-section')
  })

  it('crisis_alpha=null 折叠态：展开后才显示重新计算按钮', async () => {
    const w = await mountCollapsed({ ...mockDetail, crisis_alpha: null })
    expect(w.text()).not.toContain('本次回测未包含危机Alpha分析')
    await w.find('.ca-header').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('本次回测未包含危机Alpha分析')
    expect(w.findAll('.ca-section button').filter((b) => b.text().includes('重新计算')).length).toBe(1)
  })
})
