import { describe, it, expect } from 'vitest'
import { deriveBlockCode } from '../utils/blockCode'

describe('deriveBlockCode — 用户板块代码确定性派生（终审 I1）', () => {
  it('同名同码：幂等重推成立', () => {
    expect(deriveBlockCode('创懿回测')).toBe(deriveBlockCode('创懿回测'))
    expect(deriveBlockCode('创懿预警')).toBe(deriveBlockCode('创懿预警'))
  })

  it('异名异码', () => {
    expect(deriveBlockCode('创懿回测')).not.toBe(deriveBlockCode('创懿预警'))
  })

  it('中文输入稳定：多次调用结果一致', () => {
    const code = deriveBlockCode('创懿量化策略A')
    expect(deriveBlockCode('创懿量化策略A')).toBe(code)
    expect(deriveBlockCode('创懿量化策略A')).toBe(code)
  })

  it('输出纯 ASCII：CY 前缀 + base36 大写 6 位', () => {
    for (const name of ['创懿回测', '创懿预警', 'ABC', '组合-1']) {
      expect(deriveBlockCode(name)).toMatch(/^CY[0-9A-Z]{6}$/)
    }
  })

  it('与既有调用点默认名对应：创懿回测/创懿预警 均为合法派生码', () => {
    expect(deriveBlockCode('创懿回测')).not.toBe(deriveBlockCode(''))
  })
})
