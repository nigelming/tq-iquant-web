import { describe, it, expect } from 'vitest'
import { toInitials, buildPinyinIndex, matchTdx } from '../utils/tdxSearch'
import type { TdxFormulaListItem } from '../api'

const item = (acCode: string, acName = ''): TdxFormulaListItem =>
  ({ acCode, acName, isSys: 0, imported: false, formula_id: null })

describe('toInitials', () => {
  it('中文名 → 拼音首字母', () => {
    expect(toInitials('六脉神剑')).toBe('lmsj')
  })
  it('中英数混合名保留数字、剔除标点空白', () => {
    expect(toInitials('指标共震6红上车，3红下车')).toBe('zbgz6hsc3hxc')
  })
  it('空串返回空串', () => {
    expect(toInitials('')).toBe('')
  })
})

describe('buildPinyinIndex + matchTdx', () => {
  const items = [
    item('六脉神剑', '指标共震6红上车，3红下车'),
    item('逆时钟'),
    item('COSTLINE', '成本线'),
  ]
  const py = buildPinyinIndex(items)

  it('拼音首字母命中中文名（lmsj → 六脉神剑）', () => {
    expect(matchTdx('lmsj', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('中文子串与英文子串仍命中（原行为不回归）', () => {
    expect(matchTdx('六脉', items[0], py.get('六脉神剑'))).toBe(true)
    expect(matchTdx('COST', items[2], py.get('COSTLINE'))).toBe(true)
  })

  it('acName 首字母命中（zbgz6 → 指标共震6…）', () => {
    expect(matchTdx('zbgz6', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('大小写与空格归一（"Lm Sj" → 命中）', () => {
    expect(matchTdx('Lm Sj', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('无匹配 → false', () => {
    expect(matchTdx('xyz', items[1], py.get('逆时钟'))).toBe(false)
  })

  it('acName 为空 / 无索引条目 → 不抛错、按子串与 code 规则', () => {
    expect(matchTdx('nsz', items[1], py.get('逆时钟'))).toBe(true)  // 逆时钟 code=nsz
    expect(matchTdx('xyzabc', items[1], py.get('逆时钟'))).toBe(false)
    expect(matchTdx('顺', item('顺'), undefined)).toBe(true)         // 无索引 → 子串规则
    expect(matchTdx('shun', item('顺'), undefined)).toBe(false)
  })

  it('空 / 全空白 query → 命中（不过滤）', () => {
    expect(matchTdx('', items[1], undefined)).toBe(true)
    expect(matchTdx('   ', items[1], undefined)).toBe(true)
  })
})
