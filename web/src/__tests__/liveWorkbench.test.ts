import { describe, it, expect } from 'vitest'
import {
  orderEventToRow, orderHistoryToRows, upsertOrderRow,
  tradeEventToRow, tradeHistoryToRows,
  upsertPositionRows, positionHistoryToRows, prependCapped, positionRowKey,
  parseBeijingTime, beijingDayKey, filterRowsByRange, summarizeDecisionRows,
  rangeStartTs,
} from '../utils/liveWorkbench'

describe('liveWorkbench', () => {
  describe('orderEventToRow', () => {
    it('委托事件 → 委托行', () => {
      const row = orderEventToRow({
        trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted',
        quantity: 100, price: 10.5, bar_time: '2026-08-05T10:30:00',
      })
      expect(row.stock_code).toBe('600000.SH')
      expect(row.trade_type).toBe('BUY')
      expect(row.status).toBe('submitted')
      expect(row.quantity).toBe(100)
      expect(row.price).toBe(10.5)
      expect(row.time).toBe('10:30:00')
    })

    it('成交类事件读 filled_* 字段', () => {
      const row = orderEventToRow({
        stock_code: '600000.SH', status: 'filled',
        filled_quantity: 100, filled_price: 10.5,
      })
      expect(row.quantity).toBe(100)
      expect(row.price).toBe(10.5)
      expect(row.filled_quantity).toBe(100)
    })

    it('rejected 事件 → error_message 透传（委托表错误列）', () => {
      const row = orderEventToRow({
        trade_type: 'BUY', stock_code: '600000.SH', status: 'rejected',
        quantity: 100, error_message: 'ths ui captcha_ocr disabled',
      })
      expect(row.status).toBe('rejected')
      expect(row.error_message).toBe('ths ui captcha_ocr disabled')
    })

    it('无 error_message 的事件 → 错误列为 null', () => {
      const row = orderEventToRow({
        trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted', quantity: 100,
      })
      expect(row.error_message).toBeNull()
    })
  })

  it('orderHistoryToRows 用 -id 作 key(稳定去重)', () => {
    const rows = orderHistoryToRows([{ id: 3, stock_code: '600000.SH', trade_type: 'BUY',
      status: 'filled', quantity: 100, price: 10.5, filled_quantity: 100, filled_price: 10.5,
      error_message: null, bar_time: '2026-08-05T10:30:00' } as any])
    expect(rows[0].key).toBe(-3)
    expect(rows[0].time).toBe('10:30:00')
  })

  it('tradeEventToRow 映射成交事件', () => {
    const row = tradeEventToRow({
      trade_type: 'SELL', stock_code: '600000.SH', price: 11.0, quantity: 100, amount: 1100,
    })
    expect(row.trade_type).toBe('SELL')
    expect(row.quantity).toBe(100)
    expect(row.amount).toBe(1100)
  })

  it('tradeHistoryToRows 映射成交历史', () => {
    const rows = tradeHistoryToRows([{ id: 1, stock_code: '600000.SH', trade_type: 'BUY',
      price: 10.5, quantity: 600, amount: 6300, trade_time: '2026-08-05T10:31:00' } as any])
    expect(rows[0].time).toBe('10:31:00')
    expect(rows[0].amount).toBe(6300)
  })

  describe('upsertPositionRows', () => {
    it('新 code 追加,已有 code 原位替换', () => {
      const rows = [{ stock_code: '600000.SH', quantity: 100, avg_cost: 10, market_value: 1000, portfolio_id: null, strategy_id: null, source: 'strategy' }]
      const afterAdd = upsertPositionRows(rows, { stock_code: '000001.SZ', quantity: 50, avg_cost: 5, market_value: 250 })
      expect(afterAdd).toHaveLength(2)

      const afterUpdate = upsertPositionRows(afterAdd, { stock_code: '600000.SH', quantity: 200, avg_cost: 10.2, market_value: 2040 })
      expect(afterUpdate).toHaveLength(2)
      expect(afterUpdate[0]).toEqual({ stock_code: '600000.SH', quantity: 200, avg_cost: 10.2, market_value: 2040, portfolio_id: null, strategy_id: null, source: 'strategy' })
    })

    it('同 code 不同子策略 → 独立两行', () => {
      const rows = upsertPositionRows([], { stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000, portfolio_id: 1, strategy_id: 1 })
      const rows2 = upsertPositionRows(rows, { stock_code: '600000.SH', quantity: 200, avg_cost: 12, market_value: 2400, portfolio_id: 1, strategy_id: 2 })
      expect(rows2).toHaveLength(2)
      expect(rows2[1]).toMatchObject({ portfolio_id: 1, strategy_id: 2, quantity: 200, market_value: 2400 })
    })

    it('同 code 同子策略新快照 → 原位替换不重复', () => {
      const rows = upsertPositionRows([], { stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000, portfolio_id: 1, strategy_id: 1 })
      const rows2 = upsertPositionRows(rows, { stock_code: '600000.SH', quantity: 500, avg_cost: 10.4, market_value: 5200, portfolio_id: 1, strategy_id: 1 })
      expect(rows2).toHaveLength(1)
      expect(rows2[0].quantity).toBe(500)
    })

    it('清仓（quantity=0）→ 该行从数组移除（audit I1 幽灵持仓行）', () => {
      const rows = upsertPositionRows([], { stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000, portfolio_id: 1, strategy_id: 1 })
      const after = upsertPositionRows(rows, { stock_code: '600000.SH', quantity: 0, avg_cost: 10, market_value: 0, portfolio_id: 1, strategy_id: 1 })
      expect(after).toHaveLength(0)
    })

    it('quantity 为 0 的新事件不入表', () => {
      const after = upsertPositionRows([], { stock_code: '000001.SZ', quantity: 0, avg_cost: 0, market_value: 0, portfolio_id: null, strategy_id: null })
      expect(after).toHaveLength(0)
    })

    it('SSE payload 无 source → 默认 strategy；带 external → 透传', () => {
      const strategy = upsertPositionRows([], { stock_code: '600000.SH', quantity: 100, avg_cost: 10, market_value: 1000, portfolio_id: 1, strategy_id: 1 })
      expect(strategy[0].source).toBe('strategy')
      const external = upsertPositionRows([], { stock_code: '000001.SZ', quantity: 1000, avg_cost: 12, market_value: 13000, source: 'external' })
      expect(external[0].source).toBe('external')
    })

    it('外部行（组合/子策略为 null）与同码策略行互不覆盖（键不同）', () => {
      const strategy = upsertPositionRows([], { stock_code: '600000.SH', quantity: 300, avg_cost: 10, market_value: 3000, portfolio_id: 1, strategy_id: 1 })
      const external = upsertPositionRows(strategy, { stock_code: '600000.SH', quantity: 500, avg_cost: 8, market_value: 4000, source: 'external' })
      expect(external).toHaveLength(2)
      // 外部行新快照按 symbol 键原地替换
      const replaced = upsertPositionRows(external, { stock_code: '600000.SH', quantity: 600, avg_cost: 8, market_value: 4800, source: 'external' })
      expect(replaced).toHaveLength(2)
      expect(replaced.filter((r) => r.source === 'external')[0].quantity).toBe(600)
    })
  })

  it('positionRowKey：策略行用 股票+组合+子策略，外部行用股票代码', () => {
    expect(positionRowKey({ stock_code: '600000.SH', portfolio_id: 1, strategy_id: 2 })).toBe('600000.SH|1|2')
    expect(positionRowKey({ stock_code: '000001.SZ', portfolio_id: null, strategy_id: null })).toBe('ext|000001.SZ')
  })

  it('positionHistoryToRows 映射持仓历史', () => {
    const rows = positionHistoryToRows([{ stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350, portfolio_id: 1, strategy_id: 2 }])
    expect(rows[0].market_value).toBe(7350)
    expect(rows[0].portfolio_id).toBe(1)
    expect(rows[0].strategy_id).toBe(2)
    expect(rows[0].source).toBe('strategy')
  })

  it('positionHistoryToRows 透传外部行 source（原有持仓）', () => {
    const rows = positionHistoryToRows([
      { stock_code: '000001.SZ', quantity: 1000, avg_cost: 12, market_value: 13000, portfolio_id: null, strategy_id: null, source: 'external' },
    ])
    expect(rows[0].source).toBe('external')
    expect(rows[0].portfolio_id).toBeNull()
  })

  it('prependCapped 头部插入并封顶', () => {
    const list = [1, 2, 3]
    expect(prependCapped(list, 0)).toEqual([0, 1, 2, 3])
    const full = Array.from({ length: 200 }, (_, i) => i)
    const capped = prependCapped(full, -1, 200)
    expect(capped).toHaveLength(200)
    expect(capped[0]).toBe(-1)
    expect(capped[199]).toBe(198)
  })

  describe('upsertOrderRow', () => {
    it('同一 order_id 多次状态事件原地更新，只保留一行', () => {
      const first = upsertOrderRow([], {
        order_id: 7, trade_type: 'BUY', stock_code: '600000.SH',
        status: 'submitted', quantity: 100, price: 10.5,
        bar_time: '2026-08-05T10:30:00',
      }, '10:30:00')
      expect(first).toHaveLength(1)
      const second = upsertOrderRow(first, {
        order_id: 7, trade_type: 'BUY', stock_code: '600000.SH',
        status: 'filled', filled_quantity: 100, filled_price: 10.5,
      }, '10:31:00')
      expect(second).toHaveLength(1)
      expect(second[0].status).toBe('filled')
      // 成交回报事件不带 price → 保留原委托价
      expect(second[0].price).toBe(10.5)
      expect(second[0].filled_quantity).toBe(100)
      expect(second[0].order_id).toBe(7)
    })

    it('不同 order_id / 无 order_id 均新建行', () => {
      const rows = upsertOrderRow([], { order_id: 1, stock_code: 'A', status: 'submitted' })
      const next = upsertOrderRow(rows, { order_id: 2, stock_code: 'B', status: 'submitted' })
      expect(next).toHaveLength(2)
      const noId = upsertOrderRow(next, { stock_code: 'C', status: 'submitted' })
      expect(noId).toHaveLength(3)
    })
  })

  describe('时间口径统一为 Asia/Shanghai', () => {
    it('UTC → 北京（跨日边界 11-04 23:00Z = 11-05 07:00）', () => {
      const r = parseBeijingTime('2026-11-04T23:00:00+00:00')
      expect(r.text).toBe('07:00:00')
      expect(beijingDayKey(r.ts!)).toBe('2026-11-05')
    })

    it('+08:00 显式时区原样（上海墙钟）', () => {
      expect(parseBeijingTime('2026-11-05T09:50:00+08:00').text).toBe('09:50:00')
    })

    it('无时区旧数据降级：不猜时区、保留原样且 ts=null', () => {
      const r = parseBeijingTime('2026-11-05T09:50:00')
      expect(r.ts).toBeNull()
      expect(r.text).toBe('09:50:00')
    })

    it('缺失/非法时间不抛错', () => {
      expect(parseBeijingTime(null)).toEqual({ ts: null, text: '' })
      expect(parseBeijingTime('not-a-time')).toEqual({ ts: null, text: '' })
    })
  })

  describe('时间区间筛选（北京自然日）', () => {
    const NOW = Date.parse('2026-11-05T10:00:00+08:00')
    const row = (iso: string | null) => {
      const ts = iso ? Date.parse(iso) : null
      return { ts, name: iso ?? 'legacy' }
    }

    it('今日：只保留北京当日；近3日/近7日逐级放宽；全部保留', () => {
      const rows = [
        row('2026-11-05T09:00:00+08:00'),   // 今日
        row('2026-11-04T09:00:00+08:00'),   // 昨日
        row('2026-11-02T09:00:00+08:00'),   // 3 天前
        row('2026-10-29T09:00:00+08:00'),   // 7 天前
      ]
      // 近3日 = 11-03/04/05；近7日 = 10-30 ~ 11-05
      expect(filterRowsByRange(rows, 'today', NOW)).toHaveLength(1)
      expect(filterRowsByRange(rows, '3d', NOW)).toHaveLength(2)
      expect(filterRowsByRange(rows, '7d', NOW)).toHaveLength(3)
      expect(filterRowsByRange(rows, 'all', NOW)).toHaveLength(4)
    })

    it('区间起点是北京当日 00:00（= 前一日 UTC 16:00）', () => {
      expect(rangeStartTs('today', NOW)).toBe(Date.parse('2026-11-05T00:00:00+08:00'))
    })

    it('ts 缺失的旧数据降级保留（不被筛掉）', () => {
      const rows = [row('2026-11-05T09:00:00+08:00'), row(null)]
      expect(filterRowsByRange(rows, 'today', NOW)).toHaveLength(2)
    })
  })

  describe('summarizeDecisionRows 聚合随筛选重算', () => {
    const mk = (id: number, ts: number, gate: string) => ({
      key: -id, time: '', ts, gate, layer: 'risk', action: 'sell',
      stock_code: '600000.SH', strategy_id: 1, param_name: gate,
      param_value: 5, actual_value: 4.9, requested_qty: 100, final_qty: 100,
      message: '',
    })

    it('按 (gate,param_name) 聚合数量/股票数/首次末次', () => {
      const now = Date.parse('2026-11-05T10:00:00+08:00')
      const rows = [
        mk(1, now - 3600_000, 'stop_loss'),
        mk(2, now - 7200_000, 'stop_loss'),
        mk(3, now - 1800_000, 'max_drawdown'),
        mk(4, Date.parse('2026-10-30T09:00:00+08:00'), 'stop_loss'),
      ]
      const today = summarizeDecisionRows(filterRowsByRange(rows, 'today', now))
      const sl = today.find((s) => s.gate === 'stop_loss')!
      expect(sl.count).toBe(2)                       // 不是全时段 3
      expect(sl.stock_count).toBe(1)
      expect(today.find((s) => s.gate === 'max_drawdown')!.count).toBe(1)

      const all = summarizeDecisionRows(filterRowsByRange(rows, 'all', now))
      expect(all.find((s) => s.gate === 'stop_loss')!.count).toBe(3)
    })
  })

  describe('成交转化（触发次数 vs 成交笔数，按 order_id 去重）', () => {
    const now = Date.parse('2026-11-05T10:00:00+08:00')
    const dec = (id: number, code: string, gate = 'stop_loss') => ({
      key: -id, time: '', ts: now, gate, layer: 'risk', action: 'sell',
      stock_code: code, strategy_id: 1, param_name: gate,
      param_value: 5, actual_value: 4.9, requested_qty: 100, final_qty: 100,
      message: '',
    })
    const order = (id: number, code: string, status: string) => ({
      key: -id, order_id: id, stock_code: code, status, ts: now,
    }) as any

    it('同一订单多次重复触发/多条状态事件 → 只算 1 笔成交', () => {
      const rows = [dec(1, '600000.SH'), dec(2, '600000.SH'), dec(3, '600000.SH'), dec(4, '600000.SH')]
      const orders = [order(7, '600000.SH', 'filled'), order(7, '600000.SH', 'partial')]
      const s = summarizeDecisionRows(rows, orders)[0]
      expect(s.count).toBe(4)
      expect(s.filled_count).toBe(1)
      expect(s.conversion_rate).toBeCloseTo(0.25)
    })

    it('未传 orders（旧调用/旧数据）→ 两列 null 降级', () => {
      const s = summarizeDecisionRows([dec(1, '600000.SH')])[0]
      expect(s.filled_count).toBeNull()
      expect(s.conversion_rate).toBeNull()
    })

    it('非本组股票 / 未成交委托不计；0 成交 → 转化率 0', () => {
      const s = summarizeDecisionRows(
        [dec(1, '600000.SH')],
        [order(1, '000001.SZ', 'filled'), order(2, '600000.SH', 'submitted')],
      )[0]
      expect(s.filled_count).toBe(0)
      expect(s.conversion_rate).toBe(0)
    })

    it('成交笔数随筛选区间变化（区间外委托被筛掉 → 0，切全部 → 1）', () => {
      const old = Date.parse('2026-11-04T09:00:00+08:00')
      const oldOrder = { ...order(9, '600000.SH', 'filled'), ts: old }
      const decRows = [dec(1, '600000.SH')]
      // 今日区间：昨日成交不计入
      expect(summarizeDecisionRows(decRows, filterRowsByRange([oldOrder], 'today', now))[0].filled_count).toBe(0)
      // 全部区间：会计入
      expect(summarizeDecisionRows(decRows, filterRowsByRange([oldOrder], 'all', now))[0].filled_count).toBe(1)
    })
  })
})
