import { describe, it, expect } from 'vitest'
import { fmtMoney, fmtPrice, fmtQty, fmtPct, fmtSignedPct, fmtRatio } from '../utils/format'

describe('format.ts', () => {
  it('金额千分位 2 位', () => {
    expect(fmtMoney(1369882.7275)).toBe('1,369,882.73')
    expect(fmtMoney(-62322.46875)).toBe('-62,322.47')
    expect(fmtMoney(0)).toBe('0.00')
    expect(fmtMoney(null)).toBe('—')
  })
  it('价格 3 位', () => {
    expect(fmtPrice(7.42)).toBe('7.420')
    expect(fmtPrice(undefined)).toBe('—')
  })
  it('数量整数千分位', () => {
    expect(fmtQty(27100)).toBe('27,100')
    expect(fmtQty(NaN)).toBe('—')
  })
  it('百分比', () => {
    expect(fmtPct(0.0623)).toBe('6.23%')
    expect(fmtSignedPct(0.009)).toBe('+0.90%')
    expect(fmtSignedPct(-0.01)).toBe('-1.00%')
    expect(fmtRatio(1.2345)).toBe('1.23')
  })
})