// 「机遇Alpha · 中性Alpha」栏目图表渲染回归测试。
//
// 为什么单独一个文件、且**不 mock echarts**：历史 bug 的根因是热力图 option 里
// `xAxis.boundaryGap: false` 触发 ECharts 抛错
// （"Heatmap on cartesian must have two axes with boundaryGap true"），
// 该异常从 initStCorrChart 一路冒泡出 initMrCharts()，导致七/七-B 图空白、
// 且七-B/八/九 三张图根本没被初始化。Backtest.test.ts 把 echarts 整个 mock 掉、
// 只断言 DOM 里 .ca-chart 容器个数（=7），所以容器在、图是空的，测试照样全绿。
// 本文件用真实 ECharts 渲染（SVG renderer，happy-dom 无 canvas），
// 断言「容器里真的画出了东西」。
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const initEls: HTMLElement[] = []

vi.mock('echarts', async (importOriginal) => {
  const real: any = await importOriginal()
  const init = vi.fn((el: any) => {
    initEls.push(el as HTMLElement)
    return real.init(el, undefined, { renderer: 'svg' })
  })
  return { ...real, init, default: { ...real, init } }
})

/** 本栏目（.mr-section）内已初始化的图表，按 DOM 顺序 */
function mrInited(): HTMLElement[] {
  return initEls.filter((el) => el.classList.contains('ca-chart') && el.closest('.mr-section'))
}

vi.mock('../api', () => ({
  getBacktestRecords: vi.fn(),
  getBacktestDetail: vi.fn(),
  runBacktest: vi.fn(),
  getPortfolios: vi.fn(),
  deleteBacktestRecord: vi.fn(),
  recomputeCrisisAlpha: vi.fn(),
  recomputeMarketRegime: vi.fn(),
}))

import Backtest from '../views/Backtest.vue'
import { getBacktestRecords, getBacktestDetail, getPortfolios } from '../api'

const mockRecords = [
  { id: 1, name: '回测A', portfolio_strategy_id: 1, status: 'completed', progress: 100,
    start_date: '2026-07-01', end_date: '2026-07-31', created_at: '2026-08-01T10:00:00' },
]

const mockDetail = {
  record: { id: 1, name: '回测A', status: 'completed', start_date: '2026-07-01', end_date: '2026-07-31' },
  snapshots: [
    { snap_date: '2026-07-01', total_value: 100000, cash: 100000, market_value: 0, daily_return: null, cumulative_return: 0, benchmark_value: 3000 },
    { snap_date: '2026-07-02', total_value: 101000, cash: 50000, market_value: 51000, daily_return: 0.01, cumulative_return: 0.01, benchmark_value: 3060 },
    { snap_date: '2026-07-03', total_value: 99000, cash: 50000, market_value: 49000, daily_return: -0.0198, cumulative_return: -0.01, benchmark_value: 3030 },
  ],
  trades: [],
  evaluations: { total_return: 0.05, annual_return: 0.8, max_drawdown: 0.02, volatility: 0.15, sharpe_ratio: 1.2, total_trades: 0, benchmark_return: 0.03 },
}

const CORR_DATES = ['2024-01-05', '2024-01-12', '2024-01-19', '2024-01-26', '2024-02-02']

function exposureBlock(codes: string[], names: string[], skipped: string[]) {
  const corr: Record<string, number[]> = {}
  const beta: Record<string, number[]> = {}
  codes.forEach((c, i) => {
    corr[c] = CORR_DATES.map((_, k) => Number((0.2 + i * 0.1 + k * 0.01).toFixed(3)))
    beta[c] = CORR_DATES.map((_, k) => Number((0.3 + i * 0.15 + k * 0.01).toFixed(3)))
  })
  return { available_codes: codes, available_names: names, skipped, dates: CORR_DATES, corr, beta, window: 52 }
}

const marketRegimeCompleted = {
  status: 'completed',
  error: null,
  computed_at: '2026-09-27T12:00:00',
  benchmark_code: '000300.SH',
  benchmark_name: '沪深300',
  data: {
    meta: { strategy_name: '回测A组合', benchmark_name: '沪深300', n_weeks_labeled: 50, n_weeks_total: 56,
      state_week_counts: { bull: 20, bear: 18, rebound: 4, range: 8 } },
    regime_table: {
      bull: { label: 'bull', label_cn: '趋势上涨', weeks: 20, ratio: 0.4, strategy_return: 0.25, benchmark_return: 0.2, excess_return: 0.05, win_rate: 0.7, max_drawdown: 0.08, turnover: 12.5, cost_ratio: 0.02 },
      bear: { label: 'bear', label_cn: '趋势下跌', weeks: 18, ratio: 0.36, strategy_return: -0.1, benchmark_return: -0.2, excess_return: 0.1, win_rate: 0.6, max_drawdown: 0.12, turnover: 15.0, cost_ratio: 0.03 },
      range: { label: 'range', label_cn: '震荡', weeks: 8, ratio: 0.16, strategy_return: -0.01, benchmark_return: 0.01, excess_return: -0.02, win_rate: 0.45, max_drawdown: 0.05, turnover: 8.0, cost_ratio: 0.04 },
      rebound: { label: 'rebound', label_cn: '反弹', weeks: 4, ratio: 0.08, strategy_return: 0.12, benchmark_return: 0.1, excess_return: 0.02, win_rate: 0.75, max_drawdown: 0.02, turnover: 10.0, cost_ratio: 0.02 },
    },
    opportunity: { n_bull_weeks: 20, up_capture: 0.85, miss_ratio: 0.1,
      big_up_tests: [{ threshold: 0.03, n: 6, avg_strategy: 0.04, avg_benchmark: 0.045, win_rate: 0.8 }],
      cats: { 同步涨: { count: 12, avg_strategy: 0.03 }, 涨得少: { count: 4, avg_strategy: 0.01 }, 踏空: { count: 2, avg_strategy: 0 }, 逆势跌: { count: 2, avg_strategy: -0.01 } } },
    rebound: { n_segments: 1, avg_elasticity: 1.2, segments: [
      { start_date: '2024-01-12', end_date: '2024-02-02', trough_date: '2024-01-19', weeks: 3,
        benchmark_rebound_pct: 0.08, strategy_rebound_pct: 0.096, elasticity: 1.2,
        benchmark_recover_days: null, benchmark_recovered: false, strategy_recover_days: null, strategy_recovered: false },
    ] },
    neutral: { n_range_weeks: 8, range_ratio: 0.16, cumulative_return: -0.01, annualized_return: -0.12,
      win_rate: 0.45, n_range_days: 40, monthly_trades: 2.5, annual_turnover: 8.0, cost_gross_ratio: 0.35,
      whipsaw_count: 6, whipsaw_days: 120, avg_trade_pnl: { trend: 1000, trend_count: 10, range: -14.29, range_count: 7 } },
    style: { available_codes: ['000300.SH', '000905.SH'], skipped: [], weeks: [
        { date: '2024-02-02', label: '科技成长', leader_code: '000905.SH', leader_return: 0.04 },
      ],
      matrix: {
        大盘价值: { label: '大盘价值', weeks: 5, ratio: 0.1, strategy_return: 0.08, leader_return: 0.06, excess_return: 0.02, win_rate: 0.7, max_drawdown: -0.05 },
        中小盘: { label: '中小盘', weeks: 10, ratio: 0.2, strategy_return: 0.15, leader_return: 0.12, excess_return: 0.03, win_rate: 0.65, max_drawdown: -0.07 },
      } },
    dispersion: { available_codes: ['000300.SH', '000905.SH'], skipped: [],
      series: [{ date: '2024-01-05', dispersion: 0.02 }, { date: '2024-01-12', dispersion: 0.015 }, { date: '2024-01-19', dispersion: 0.025 }],
      buckets: { low: { count: 1, mean_excess: 0.005, win_rate: 0.6, threshold: 0.018 },
        mid: { count: 1, mean_excess: 0.01, win_rate: 0.7, threshold: 0.022 },
        high: { count: 1, mean_excess: 0.02, win_rate: 0.8, threshold: null } } },
    exposure: exposureBlock(['000300.SH', '000905.SH'], ['沪深300', '中证500'], []),
    quadrant_exposure: exposureBlock(['399372.SZ', '399373.SZ'], ['大盘成长', '大盘价值'], []),
    industry_attribution: { status: 'ok', taxonomy: '通达信二级行业(881)', n_days: 60, unmapped_codes: [],
      exposure: { dates: ['2024-01-05', '2024-01-12'],
        series: [{ code: '881001.SH', name: '半导体', avg_weight: 0.6, weights: [0.6, 0.6] }],
        cash: [0.4, 0.4] },
      hit_rate: { weekly: { top_n: 3, periods: 12, periods_holding: 10, hits: 7, hit_rate: 0.7, details: [] },
        monthly: { top_n: 3, periods: 3, periods_holding: 3, hits: 2, hit_rate: 0.667, details: [] } },
      contribution: { available: true, total_contribution: 0.135, rows: [
        { code: '881001.SH', name: '半导体', avg_weight: 0.6, index_return: 0.2, contribution: 0.12 }] } },
    robustness: { status: 'ok', n_days: 600, start_date: '2024-01-02', end_date: '2026-06-30',
      half_year_matrix: [{ period: '2024H1', start_date: '2024-01-02', end_date: '2024-06-28',
        strategy_return: 0.08, benchmark_return: 0.05, excess_return: 0.03, max_drawdown: 0.04, sharpe: 1.2 }],
      rolling: { weekly_points: 130, windows: [{ window: 26, n_samples: 80,
        returns: { median: 0.06, min: -0.08, max: 0.15, negative_ratio: 0.2 },
        sharpe: { median: 0.8, min: -0.5, max: 1.6, negative_ratio: 0.25 },
        histogram: [{ label: '≤-20%', count: 0 }, { label: '0%~5%', count: 30 }, { label: '5%~10%', count: 24 }, { label: '>20%', count: 0 }] }] },
      longest_loss_window: { start_date: '2024-09-10', trough_date: '2024-10-18', end_date: '2024-12-05', days: 86, drawdown: 0.09 },
      benchmark_sensitivity: [] },
  },
}

beforeEach(() => {
  initEls.length = 0
  ;(Element.prototype as any).scrollIntoView = vi.fn()
  ;(getBacktestRecords as any).mockResolvedValue(mockRecords)
  ;(getPortfolios as any).mockResolvedValue([])
  ;(getBacktestDetail as any).mockResolvedValue({ ...mockDetail, market_regime: marketRegimeCompleted })
})

async function expandMr() {
  const w = mount(Backtest)
  await flushPromises()
  const viewBtn = w.findAll('button').filter((b) => b.text().includes('查看'))[0]
  await viewBtn.trigger('click')
  await flushPromises()
  await w.find('.mr-section .ca-header').trigger('click')
  await flushPromises()
  return w
}

/** 容器内真实画出了图形（SVG 有 path/polygon/rect 等），而非空壳 */
function painted(el: HTMLElement): number {
  return el.querySelectorAll('path, polygon, rect, polyline').length
}

describe('机遇Alpha·中性Alpha 栏目 — 图表真实渲染（真实 ECharts，不 mock）', () => {
  it('展开栏目时 7 张图全部完成初始化，无 option 抛错中断后续图表', async () => {
    const w = await expandMr()
    expect(w.findAll('.mr-section .ca-chart').length).toBe(7)
    // 1(状态) 3(反弹) 6(分化度) 7(暴露) 7-B(四象限) 8(行业) 9(稳健性)
    expect(mrInited().length).toBe(7)
  })

  it('七 / 七-B 热力图容器内画出了色块（历史 bug：boundaryGap:false 抛错 → 空白）', async () => {
    const w = await expandMr()
    const els = w.findAll('.mr-section .ca-chart').map((n) => n.element as HTMLElement)
    expect(painted(els[3])).toBeGreaterThan(0)
    expect(painted(els[4])).toBeGreaterThan(0)
  })

  it('七 / 七-B 热力图必须是 visualMap 热力（缺 visualMap 或两轴非 band 都会让 ECharts 抛错）', async () => {
    const { init } = await import('echarts')
    await expandMr()
    const results = (init as any).mock.results as any[]
    const opts = initEls
      .map((el, i) => ({ el, opt: results[i]?.value?.getOption?.() }))
      .filter(({ el, opt }) => el.classList.contains('ca-chart') && el.closest('.mr-section') && opt)
      .map(({ opt }) => opt)
    const heatmaps = opts.filter((o: any) => o?.series?.some((s: any) => s.type === 'heatmap'))
    expect(heatmaps.length).toBe(2)
    for (const o of heatmaps) {
      expect(o.visualMap).toBeTruthy()
      const axes = [o.xAxis?.[0] ?? o.xAxis, o.yAxis?.[0] ?? o.yAxis]
      for (const ax of axes) expect(ax.boundaryGap).not.toBe(false)
    }
  })

  it('八 行业归因 与 九 稳健性 直方图 在七/七-B 之后仍被初始化（不被前序异常吞掉）', async () => {
    const w = await expandMr()
    const els = w.findAll('.mr-section .ca-chart').map((n) => n.element as HTMLElement)
    expect(painted(els[5])).toBeGreaterThan(0)
    expect(painted(els[6])).toBeGreaterThan(0)
  })
})
