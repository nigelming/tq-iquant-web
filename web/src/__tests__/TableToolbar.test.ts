import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TableToolbar from '../components/TableToolbar.vue'

const filters = [
  { kind: 'select', key: 'type', label: '买卖', get: (r: any) => r.type,
    options: [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }] },
  { kind: 'numRange', key: 'amount', label: '金额', get: (r: any) => r.amount },
] as any

describe('TableToolbar', () => {
  it('渲染关键词框与筛选控件、计数', () => {
    const w = mount(TableToolbar, { props: { filters, keyword: { label: '搜索' },
      searchText: '', filterState: {}, displayedCount: 3, totalCount: 10 } })
    expect(w.find('input[type="text"]').exists()).toBe(true)
    expect(w.text()).toContain('买卖')
    expect(w.text()).toContain('显示 3 / 共 10 条')
  })
  it('输入关键词 emit update:searchText', async () => {
    const w = mount(TableToolbar, { props: { filters: [], keyword: { label: '搜索' },
      searchText: '', filterState: {}, displayedCount: 0, totalCount: 0 } })
    await w.find('input[type="text"]').setValue('000001')
    expect(w.emitted('update:searchText')![0]).toEqual(['000001'])
  })
  it('重置 emit reset', async () => {
    const w = mount(TableToolbar, { props: { filters: [], keyword: { label: '搜索' },
      searchText: 'x', filterState: { type: 'BUY' }, displayedCount: 0, totalCount: 0 } })
    await w.find('button.tb-reset').trigger('click')
    expect(w.emitted('reset')).toBeTruthy()
  })
})