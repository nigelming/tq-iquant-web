<script setup lang="ts">
defineProps<{ title: string; crumbs?: string[] }>()
</script>

<template>
  <div class="page-head">
    <div class="page-head-left">
      <span v-if="crumbs && crumbs.length" class="page-head-crumb">{{ crumbs.join(' / ') }}</span>
      <h2 class="page-head-title">{{ title }}</h2>
    </div>
    <div class="page-head-actions"><slot /></div>
  </div>
</template>
