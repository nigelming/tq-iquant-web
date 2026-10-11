import { describe, it, expect } from 'vitest'
import { ref } from 'vue'
import { useTable } from '../composables/useTable'

const rows = ref([
  { code: '000001.SZ', type: 'BUY', amount: 100, date: '2026-01-02', tag: 'a' },
  { code: '000002.SZ', type: 'SELL', amount: 300, date: '2026-01-01', tag: 'b' },
  { code: '600000.SH', type: 'BUY', amount: 200, date: '2026-01-03', tag: 'a' },
])

const opts = {
  columns: [
    { key: 'date', label: '时间', sortable: true },
    { key: 'code', label: '代码' },
    { key: 'amount', label: '金额', sortable: true },
  ],
  keyword: { label: '搜索' },
  filters: [
    { kind: 'select' as const, key: 'type', label: '买卖', get: (r: any) => r.type,
      options: [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }] },
    { kind: 'numRange' as const, key: 'amount', label: '金额', get: (r: any) => r.amount },
    { kind: 'dateRange' as const, key: 'date', label: '时间', get: (r: any) => r.date },
  ],
  pageSize: 2,
}

describe('useTable', () => {
  it('关键词跨列过滤并计数', () => {
    const t = useTable(rows, opts)
    expect(t.displayedCount.value).toBe(3)
    t.searchText.value = '000001'
    expect(t.displayedCount.value).toBe(1)
    expect(t.totalCount.value).toBe(3)
  })
  it('select 筛选', () => {
    const t = useTable(rows, opts)
    t.filterState.value.type = 'BUY'
    expect(t.pagedRows.value.map((r) => r.code)).toEqual(['000001.SZ', '600000.SH'])
  })
  it('numRange / dateRange 筛选', () => {
    const t = useTable(rows, opts)
    t.filterState.value.amount = [150, '']
    expect(t.displayedCount.value).toBe(2)
    t.filterState.value.amount = ['', '']
    t.filterState.value.date = ['2026-01-02', '2026-01-03']
    expect(t.displayedCount.value).toBe(2)
  })
  it('排序三态切换', () => {
    const t = useTable(rows, opts)
    t.toggleSort('amount'); expect(t.sortDir.value).toBe('asc')
    expect(t.pagedRows.value.map((r) => r.amount)).toEqual([100, 200])
    t.toggleSort('amount'); expect(t.sortDir.value).toBe('desc')
    expect(t.pagedRows.value.map((r) => r.amount)).toEqual([300, 200])
    t.toggleSort('amount'); expect(t.sortDir.value).toBe(null)
    expect(t.pagedRows.value.map((r) => r.date)).toEqual(['2026-01-02', '2026-01-01'])
  })
  it('非 sortable 列点击无效', () => {
    const t = useTable(rows, opts)
    t.toggleSort('code'); expect(t.sortKey.value).toBe(null)
  })
  it('分页钳制与 pageSize=0 全部', () => {
    const t = useTable(rows, opts)
    t.setPage(99); expect(t.page.value).toBe(2)
    t.setPageSize(0); expect(t.pagedRows.value.length).toBe(3)
  })
  it('筛选变化回第 1 页', () => {
    const t = useTable(rows, opts)
    t.toggleSort('amount'); t.setPage(2)
    t.filterState.value.type = 'BUY'
    expect(t.page.value).toBe(1)
  })
  it('reset 清空', () => {
    const t = useTable(rows, opts)
    t.searchText.value = 'x'; t.filterState.value.type = 'BUY'; t.toggleSort('amount')
    t.reset()
    expect(t.searchText.value).toBe(''); expect(t.filterState.value).toEqual({})
    expect(t.sortKey.value).toBe(null); expect(t.page.value).toBe(1)
  })
  it('sortValue 解格式化字符串', () => {
    const r2 = ref([{ s: '+0.90%' }, { s: '-1.10%' }, { s: '+0.10%' }])
    const t = useTable(r2, { columns: [{ key: 's', label: 's', sortable: true, sortValue: (r: any) => parseFloat(r.s) }] })
    t.toggleSort('s')
    expect(t.pagedRows.value.map((r) => r.s)).toEqual(['-1.10%', '+0.10%', '+0.90%'])
  })
})