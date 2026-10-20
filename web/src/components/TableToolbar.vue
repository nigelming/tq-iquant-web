<script setup lang="ts">
import { computed } from 'vue'
import type { FilterSpec } from '../composables/useTable'

const props = defineProps<{
  filters: FilterSpec[]
  keyword?: { label: string; placeholder?: string }
  searchText: string
  filterState: Record<string, any>
  displayedCount: number
  totalCount: number
}>()
const emit = defineEmits<{
  'update:searchText': [string]
  'update:filterState': [Record<string, any>]
  reset: []
}>()

function optionsOf(f: Extract<FilterSpec, { kind: 'select' }>) {
  return typeof f.options === 'function' ? f.options() : f.options
}
function setFilter(key: string, value: any) {
  emit('update:filterState', { ...props.filterState, [key]: value })
}
function rangeVal(key: string, i: number): any {
  const v = props.filterState[key]
  return Array.isArray(v) ? (v[i] ?? '') : ''
}
function setRange(key: string, i: number, value: any) {
  const cur = Array.isArray(props.filterState[key]) ? [...props.filterState[key]] : ['', '']
  cur[i] = value
  setFilter(key, cur)
}
const hasAny = computed(() => !!props.keyword || (props.filters?.length ?? 0) > 0)
</script>

<template>
  <div v-if="hasAny" class="table-toolbar">
    <input v-if="keyword" class="tb-keyword" type="text"
      :placeholder="keyword.placeholder || keyword.label"
      :value="searchText" @input="emit('update:searchText', ($event.target as HTMLInputElement).value)" />
    <template v-for="f in filters" :key="f.key">
      <select v-if="f.kind === 'select'" class="tb-control" :value="filterState[f.key] ?? ''"
        @change="setFilter(f.key, ($event.target as HTMLSelectElement).value)">
        <option value="">{{ f.label }}：全部</option>
        <option v-for="o in optionsOf(f)" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <span v-else-if="f.kind === 'dateRange'" class="tb-range">
        <span>{{ f.label }}</span>
        <input type="date" :value="rangeVal(f.key, 0)" @input="setRange(f.key, 0, ($event.target as HTMLInputElement).value)" />
        <span>~</span>
        <input type="date" :value="rangeVal(f.key, 1)" @input="setRange(f.key, 1, ($event.target as HTMLInputElement).value)" />
      </span>
      <span v-else class="tb-range">
        <span>{{ f.label }}<template v-if="f.unit">({{ f.unit }})</template></span>
        <input type="number" :value="rangeVal(f.key, 0)" @input="setRange(f.key, 0, ($event.target as HTMLInputElement).value)" />
        <span>~</span>
        <input type="number" :value="rangeVal(f.key, 1)" @input="setRange(f.key, 1, ($event.target as HTMLInputElement).value)" />
      </span>
    </template>
    <button class="tb-reset" @click="emit('reset')">重置</button>
    <span class="tb-count">显示 {{ displayedCount }} / 共 {{ totalCount }} 条</span>
  </div>
</template>

<style scoped>
.table-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.tb-keyword, .tb-control, .tb-range input { padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg); color: var(--ink); font-size: 12px; }
.tb-range { display: inline-flex; align-items: center; gap: 4px; color: var(--ink-soft); }
.tb-reset { padding: 4px 10px; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--ink-soft); cursor: pointer; }
.tb-reset:hover { color: var(--accent); border-color: var(--accent); }
.tb-count { margin-left: auto; color: var(--ink-soft); }
</style>