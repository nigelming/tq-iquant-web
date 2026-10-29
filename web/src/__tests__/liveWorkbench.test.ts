import { describe, it, expect } from 'vitest'
import {
  orderEventToRow, orderHistoryToRows,
  tradeEventToRow, tradeHistoryToRows,
  upsertPositionRows, positionHistoryToRows, prependCapped, positionRowKey,
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
})
