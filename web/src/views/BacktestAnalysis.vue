<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getPortfolios, getWalkForwardRuns, getParamPerturbRuns,
  type PortfolioItem, type WalkForwardRunItem, type ParamPerturbRunItem,
} from '../api'
import WalkForwardPanel from './WalkForwardPanel.vue'
import ParamPerturbPanel from './ParamPerturbPanel.vue'
import PageHeader from '../components/PageHeader.vue'

const activeTab = ref<'wf' | 'pp'>('wf')
const wfPanel = ref<{ openForm: () => void } | null>(null)
const ppPanel = ref<{ openForm: () => void } | null>(null)
const portfolios = ref<PortfolioItem[]>([])
const wfRuns = ref<WalkForwardRunItem[]>([])
const ppRuns = ref<ParamPerturbRunItem[]>([])

async function reloadWf() {
  wfRuns.value = await getWalkForwardRuns().catch(() => [])
}
async function reloadPp() {
  ppRuns.value = await getParamPerturbRuns().catch(() => [])
}

onMounted(async () => {
  portfolios.value = await getPortfolios().catch(() => [])
  await Promise.all([reloadWf(), reloadPp()])
})
</script>

<template>
  <div>
    <PageHeader title="回归验证">
      <button class="btn" :class="{ 'btn-primary': activeTab === 'wf' }" @click="activeTab = 'wf'">滚动前推</button>
      <button class="btn" :class="{ 'btn-primary': activeTab === 'pp' }" @click="activeTab = 'pp'" style="margin-left:6px">参数敏感度</button>
      <button v-if="activeTab === 'wf'" @click="wfPanel?.openForm()" class="btn btn-primary" style="margin-left:6px">+ 发起滚动前推</button>
      <button v-else @click="ppPanel?.openForm()" class="btn btn-primary" style="margin-left:6px">+ 发起参数敏感度</button>
    </PageHeader>

    <div v-show="activeTab === 'wf'">
      <WalkForwardPanel ref="wfPanel" :portfolios="portfolios" @done="reloadWf" />
    </div>
    <div v-show="activeTab === 'pp'">
      <ParamPerturbPanel ref="ppPanel" :portfolios="portfolios" @done="reloadPp" />
    </div>
  </div>
</template>
