<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getPortfolios, getWalkForwardRuns, getParamPerturbRuns,
  type PortfolioItem, type WalkForwardRunItem, type ParamPerturbRunItem,
  type WalkForwardRequest,
} from '../api'
import WalkForwardPanel from './WalkForwardPanel.vue'
import ParamPerturbPanel from './ParamPerturbPanel.vue'
import PageHeader from '../components/PageHeader.vue'

const activeTab = ref<'wf' | 'pp'>('wf')
const wfPanel = ref<{ openForm: (prefill?: Partial<WalkForwardRequest>) => void } | null>(null)
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

// 回测详情页「样本外验证 → 立即发起验证」接力：消费 sessionStorage 预填并自动开表单
function consumeWfPrefill() {
  const raw = sessionStorage.getItem('wf_prefill')
  if (!raw) return
  sessionStorage.removeItem('wf_prefill')
  try {
    const prefill = JSON.parse(raw) as Partial<WalkForwardRequest>
    activeTab.value = 'wf'
    wfPanel.value?.openForm(prefill)
  } catch { /* 非法内容忽略 */ }
}

onMounted(async () => {
  portfolios.value = await getPortfolios().catch(() => [])
  await Promise.all([reloadWf(), reloadPp()])
  consumeWfPrefill()
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
