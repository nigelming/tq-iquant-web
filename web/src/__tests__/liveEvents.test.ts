import { describe, it, expect } from 'vitest'
import { formatEvent, gateLabel, GATE_LABEL, EVENT_TYPE_LABEL, EVENT_TYPE_COLOR } from '../utils/liveEvents'

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

describe('formatEvent decision', () => {
  it('maps decision type to label/color', () => {
    expect(EVENT_TYPE_LABEL.decision).toBe('拦截')
    expect(EVENT_TYPE_COLOR.decision).toBe('purple')
  })

  it('gateLabel maps known gates and falls back to raw code', () => {
    expect(gateLabel('stop_loss')).toBe('止损')
    expect(gateLabel('insufficient_funds')).toBe('资金不足拒单')
    expect(gateLabel('max_positions_full')).toBe('持股数达上限')
    expect(gateLabel('some_unknown_gate')).toBe('some_unknown_gate')
  })

  it('GATE_LABEL covers the full backend gate vocabulary', () => {
    const gates = [
      'stop_loss', 'take_profit', 'trailing_stop',
      'max_drawdown', 'daily_loss', 'risk_recover', 'halted_buy_strip',
      'halted_bar_no_price', 'slave_master_block', 'open_already_holding',
      'max_positions_full', 'reduce_qty_too_small', 'add_threshold_not_met',
      'add_count_exceeded', 'missing_strategy_risk',
      'open_qty_too_small', 'add_qty_too_small', 'insufficient_funds', 'order_shrunk',
      't1_clamp', 't1_insufficient',
      'after_close_block', 'non_trading_block', 'dup_skip', 'inflight_skip',
      'bridge_unavailable', 'bridge_rejected', 'approval_failed',
    ]
    for (const g of gates) {
      expect(GATE_LABEL[g], `missing label for ${g}`).toBeTruthy()
    }
  })

  it('formats insufficient_funds with blocked qty and message', () => {
    const text = formatEvent('decision', {
      gate: 'insufficient_funds', layer: 'capital_gate', action: 'reject',
      stock_code: '000001.SZ', requested_qty: 1000, final_qty: 0,
      message: '开仓资金不足1手',
    })
    expect(text).toBe('资金不足拒单 000001.SZ 1000股被拦 开仓资金不足1手')
  })

  it('formats order_shrunk with requested vs final qty', () => {
    const text = formatEvent('decision', {
      gate: 'order_shrunk', layer: 'capital_gate', action: 'shrink',
      stock_code: '600000.SH', requested_qty: 1000, final_qty: 300,
    })
    expect(text).toContain('资金不足缩量 600000.SH 需1000股/实发300股')
  })

  it('formats threshold vs actual as percentage for ratio params', () => {
    const text = formatEvent('decision', {
      gate: 'stop_loss', layer: 'strategy_risk', action: 'trigger',
      stock_code: '000001.SZ', param_name: 'stop_loss_ratio',
      param_value: 0.05, actual_value: 0.052,
      message: '亏损 5.2% 超止损线 5%',
    })
    expect(text).toContain('止损 000001.SZ')
    expect(text).toContain('阈值5.00% 实际5.20%')
    expect(text).toContain('亏损 5.2% 超止损线 5%')
  })

  it('formats max_drawdown halt without qty', () => {
    const text = formatEvent('decision', {
      gate: 'max_drawdown', layer: 'portfolio_risk', action: 'halt',
      param_name: 'max_drawdown', param_value: 0.2, actual_value: 0.21,
      message: '回撤 21.0% 超熔断线 20.0%',
    })
    expect(text).toBe('最大回撤熔断 阈值20.00% 实际21.00% 回撤 21.0% 超熔断线 20.0%')
  })

  it('formats max_positions_full with count threshold (non-ratio number)', () => {
    const text = formatEvent('decision', {
      gate: 'max_positions_full', layer: 'signal_gate', action: 'block',
      stock_code: '600000.SH', param_name: 'max_positions',
      param_value: 5, actual_value: 5,
    })
    expect(text).toContain('持股数达上限 600000.SH')
    expect(text).toContain('阈值5 实际5')
  })
})
