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
