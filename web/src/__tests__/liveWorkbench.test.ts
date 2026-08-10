import { describe, it, expect } from 'vitest'
import {
  orderEventToRow, orderHistoryToRows,
  tradeEventToRow, tradeHistoryToRows,
  upsertPositionRows, positionHistoryToRows, prependCapped,
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
      const rows = [{ stock_code: '600000.SH', quantity: 100, avg_cost: 10, market_value: 1000 }]
      const afterAdd = upsertPositionRows(rows, { stock_code: '000001.SZ', quantity: 50, avg_cost: 5, market_value: 250 })
      expect(afterAdd).toHaveLength(2)

      const afterUpdate = upsertPositionRows(afterAdd, { stock_code: '600000.SH', quantity: 200, avg_cost: 10.2, market_value: 2040 })
      expect(afterUpdate).toHaveLength(2)
      expect(afterUpdate[0]).toEqual({ stock_code: '600000.SH', quantity: 200, avg_cost: 10.2, market_value: 2040 })
    })
  })

  it('positionHistoryToRows 映射持仓历史', () => {
    const rows = positionHistoryToRows([{ stock_code: '600000.SH', quantity: 700, avg_cost: 10.5, market_value: 7350 }])
    expect(rows[0].market_value).toBe(7350)
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
