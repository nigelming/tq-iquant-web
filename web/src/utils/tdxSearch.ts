import { pinyin } from 'pinyin-pro'
import type { TdxFormulaListItem } from '../api'

export interface TdxPinyin { code: string; name: string }

// 文本 → 拼音首字母小写串（剔除非 [a-z0-9]，如标点/空白；数字保留）
export function toInitials(text: string): string {
  if (!text) return ''
  const letters = pinyin(text, { pattern: 'first', toneType: 'none', type: 'array' })
  return letters.join('').toLowerCase().replace(/[^a-z0-9]/g, '')
}

// 列表加载后建一次索引；key=acCode（前端派生数据，不进 API 类型）
export function buildPinyinIndex(items: TdxFormulaListItem[]): Map<string, TdxPinyin> {
  const map = new Map<string, TdxPinyin>()
  for (const it of items) {
    map.set(it.acCode, { code: toInitials(it.acCode), name: toInitials(it.acName || '') })
  }
  return map
}

// 匹配：空 query 全命中；子串（原行为，trim+lower）∪ 拼音首字母（去全部空白）
export function matchTdx(
  query: string,
  item: TdxFormulaListItem,
  py: TdxPinyin | undefined,
): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  if (item.acCode.toLowerCase().includes(q)) return true
  if ((item.acName || '').toLowerCase().includes(q)) return true
  const qp = q.replace(/\s+/g, '')
  if (qp && py) {
    if (py.code.includes(qp)) return true
    if (py.name.includes(qp)) return true
  }
  return false
}
