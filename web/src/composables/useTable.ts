import { computed, ref, watch, type Ref } from 'vue'

export interface ColumnSpec<T = any> {
  key: string
  label: string
  sortable?: boolean
  sortValue?: (row: T) => number | string | null | undefined
  searchable?: boolean
}

export type SelectOptions = { value: string; label: string }[]

export type FilterSpec<T = any> =
  | { kind: 'select'; key: string; label: string; get: (r: T) => string;
      options: SelectOptions | (() => SelectOptions) }
  | { kind: 'dateRange'; key: string; label: string; get: (r: T) => string }
  | { kind: 'numRange'; key: string; label: string; get: (r: T) => number | null | undefined; unit?: string }

export interface UseTableOptions<T = any> {
  columns: ColumnSpec<T>[]
  filters?: FilterSpec<T>[]
  keyword?: { label: string; placeholder?: string }
  pageSize?: number
  initialSort?: { key: string; dir: 'asc' | 'desc' }
}

export function useTable<T extends Record<string, any>>(rows: Ref<T[]>, opts: UseTableOptions<T>) {
  const filterSpecs = opts.filters ?? []
  const searchText = ref('')
  const filterState = ref<Record<string, any>>({})
  const sortKey = ref<string | null>(opts.initialSort?.key ?? null)
  const sortDir = ref<'asc' | 'desc' | null>(opts.initialSort?.dir ?? null)
  const page = ref(1)
  const pageSize = ref(opts.pageSize ?? 20)

  const searchableKeys = opts.columns.filter((c) => c.searchable !== false).map((c) => c.key)
  const rowText = (r: T, k: string): string => {
    const v = (r as any)[k]
    return v === null || v === undefined ? '' : String(v)
  }

  const filtered = computed<T[]>(() => {
    let list = rows.value
    const kw = searchText.value.trim().toLowerCase()
    if (kw) list = list.filter((r) => searchableKeys.some((k) => rowText(r, k).toLowerCase().includes(kw)))
    for (const f of filterSpecs) {
      const v = filterState.value[f.key]
      if (v === undefined || v === null || v === '') continue
      if (f.kind === 'select') {
        list = list.filter((r) => f.get(r) === v)
      } else if (f.kind === 'dateRange') {
        const [from, to] = (v as [string, string]) ?? ['', '']
        if (!from && !to) continue
        list = list.filter((r) => {
          const d = f.get(r)
          if (!d) return false
          if (from && d < from) return false
          if (to && d > to) return false
          return true
        })
      } else {
        const [min, max] = (v as [number | '', number | '']) ?? ['', '']
        if (min === '' && max === '') continue
        list = list.filter((r) => {
          const n = f.get(r)
          if (n === null || n === undefined || !Number.isFinite(Number(n))) return false
          if (min !== '' && Number(n) < Number(min)) return false
          if (max !== '' && Number(n) > Number(max)) return false
          return true
        })
      }
    }
    return list
  })

  const sorted = computed<T[]>(() => {
    const key = sortKey.value
    if (!key || !sortDir.value) return filtered.value
    const col = opts.columns.find((c) => c.key === key)
    if (!col) return filtered.value
    const dir = sortDir.value === 'asc' ? 1 : -1
    const getVal = (r: T) => (col.sortValue ? col.sortValue(r) : (r as any)[key])
    return [...filtered.value].sort((a, b) => {
      const va = getVal(a); const vb = getVal(b)
      if (va === null || va === undefined) return 1
      if (vb === null || vb === undefined) return -1
      const na = Number(va); const nb = Number(vb)
      if (Number.isFinite(na) && Number.isFinite(nb)) return na === nb ? 0 : (na < nb ? -1 : 1) * dir
      const sa = String(va); const sb = String(vb)
      return sa === sb ? 0 : (sa < sb ? -1 : 1) * dir
    })
  })

  const displayedCount = computed(() => filtered.value.length)
  const totalCount = computed(() => rows.value.length)
  const totalPages = computed(() => (pageSize.value <= 0 ? 1 : Math.max(1, Math.ceil(displayedCount.value / pageSize.value))))
  const pagedRows = computed<T[]>(() => {
    if (pageSize.value <= 0) return sorted.value
    const p = Math.min(Math.max(1, page.value), totalPages.value)
    const start = (p - 1) * pageSize.value
    return sorted.value.slice(start, start + pageSize.value)
  })

  function toggleSort(key: string) {
    const col = opts.columns.find((c) => c.key === key)
    if (!col || !col.sortable) return
    if (sortKey.value !== key) { sortKey.value = key; sortDir.value = 'asc'; return }
    if (sortDir.value === 'asc') { sortDir.value = 'desc'; return }
    if (sortDir.value === 'desc') { sortKey.value = null; sortDir.value = null; return }
    sortDir.value = 'asc'
  }
  function setPage(n: number) { page.value = Math.min(Math.max(1, Math.floor(n) || 1), totalPages.value) }
  function setPageSize(n: number) { pageSize.value = n; page.value = 1 }
  function reset() {
    searchText.value = ''
    filterState.value = {}
    sortKey.value = opts.initialSort?.key ?? null
    sortDir.value = opts.initialSort?.dir ?? null
    page.value = 1
  }

  watch([searchText, filterState, sortKey, sortDir], () => { page.value = 1 }, { deep: true, flush: 'sync' })

  return {
    columns: opts.columns, filterSpecs, filterState, searchText,
    sortKey, sortDir, page, pageSize, totalPages, pagedRows,
    displayedCount, totalCount, toggleSort, setPage, setPageSize, reset,
  }
}