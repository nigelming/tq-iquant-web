<script setup lang="ts">
const props = defineProps<{
  page: number
  pageSize: number
  totalPages: number
  displayedCount: number
  totalCount: number
}>()
const emit = defineEmits<{ 'update:page': [number]; 'update:pageSize': [number] }>()
const SIZE_OPTIONS = [20, 50, 100, 0]
function onSize(e: Event) { emit('update:pageSize', Number((e.target as HTMLSelectElement).value)) }
</script>

<template>
  <div class="table-pager pagination">
    <select :value="pageSize" @change="onSize">
      <option v-for="s in SIZE_OPTIONS" :key="s" :value="s">{{ s === 0 ? '全部' : s + ' 条/页' }}</option>
    </select>
    <button class="tp-prev page-btn" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
    <span class="page-info">第 {{ page }} / {{ totalPages }} 页 · 共 {{ displayedCount }} 条</span>
    <button class="tp-next page-btn" :disabled="page >= totalPages" @click="emit('update:page', page + 1)">下一页</button>
  </div>
</template>

<style scoped>
.table-pager { display: flex; align-items: center; gap: 8px; justify-content: center; margin-top: 10px; font-size: 12px; }
.table-pager select { padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg); color: var(--ink); }
</style>