import { describe, it, expect } from 'vitest'
import { formatEvent } from '../utils/liveEvents'

describe('formatEvent', () => {
  it('formats signal', () => {
    expect(formatEvent('signal', {
      signal_type: 'OPEN', signal_name: 'open_sig', stock_code: '600000.SH',
    })).toBe('[OPEN] open_sig 600000.SH')
  })

  it('formats order submitted (BUY/SELL)', () => {
    expect(formatEvent('order', {
      trade_type: 'BUY', stock_code: '600000.SH', status: 'submitted',
      quantity: 100, price: 10.5,
    })).toBe('买入 600000.SH 100股 @ 10.5')
    expect(formatEvent('order', {
      trade_type: 'SELL', stock_code: '600000.SH', status: 'submitted',
      quantity: 200, price: null,
    })).toBe('卖出 600000.SH 200股')
  })

  it('formats order filled/partial', () => {
    expect(formatEvent('order', {
      stock_code: '600000.SH', status: 'filled', filled_quantity: 100, filled_price: 10.5,
    })).toBe('成交 600000.SH 100股 @ 10.5')
    expect(formatEvent('order', {
      stock_code: '600000.SH', status: 'partial', filled_quantity: 50, filled_price: 10.5,
    })).toBe('部分成交 600000.SH 50股 @ 10.5')
  })

  it('formats order rejected with error', () => {
    expect(formatEvent('order', {
      stock_code: '600000.SH', status: 'rejected', error_message: 'bridge unavailable',
    })).toBe('拒绝 600000.SH bridge unavailable')
  })

  it('formats trade', () => {
    expect(formatEvent('trade', {
      trade_type: 'SELL', stock_code: '600000.SH', quantity: 100, price: 10.5, amount: 1050,
    })).toBe('卖出 600000.SH 100股 @ 10.5 金额1050')
  })

  it('formats position', () => {
    expect(formatEvent('position', {
      stock_code: '600000.SH', quantity: 600, avg_cost: 10.2, market_value: 6120,
    })).toBe('600000.SH 600股 成本10.2 市值6120')
  })

  it('formats risk (max_drawdown / daily_loss)', () => {
    expect(formatEvent('risk', {
      rule: 'max_drawdown', message: '最大回撤熔断触发（累计 1 次）',
    })).toBe('最大回撤熔断: 最大回撤熔断触发（累计 1 次）')
    expect(formatEvent('risk', {
      rule: 'daily_loss', message: '日内亏损熔断触发，当日暂停新开仓',
    })).toBe('日内亏损熔断: 日内亏损熔断触发，当日暂停新开仓')
  })

  it('ping returns empty (skip)', () => {
    expect(formatEvent('ping', { time: '2026-08-05T14:30:00' })).toBe('')
  })
})
