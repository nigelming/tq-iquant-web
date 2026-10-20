<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import {
  getParamPerturbRuns, getParamPerturbRun, runParamPerturb, deleteParamPerturbRun,
  type PortfolioItem, type ParamPerturbRunItem, type ParamPerturbRequest, type PerturbRound,
} from '../api'
import { fmtPct, fmtSignedPct, fmtRatio, DASH } from '../utils/format'

const props = withDefaults(defineProps<{ portfolios?: PortfolioItem[] }>(), { portfolios: () => [] })
const emit = defineEmits<{ done: [] }>()

const runs = ref<ParamPerturbRunItem[]>([])
const loading = ref(false)
const errMsg = ref('')
const showForm = ref(false)
const detail = ref<ParamPerturbRunItem | null>(null)

function emptyForm(): ParamPerturbRequest {
  return {
    portfolio_strategy_id: props.portfolios[0]?.id ?? 0,
    name: '',
    start_date: '',
    end_date: '',
    perturb_pct: 0.1,
    dividend_mode: 'front',
    evaluation_profile: 'absolute',
  }
}
const form = reactive<ParamPerturbRequest>(emptyForm())

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function load() {
  loading.value = true
  errMsg.value = ''
  try {
    runs.value = await getParamPerturbRuns()
  } catch (e) {
    errMsg.value = errText(e)
    runs.value = []
  } finally {
    loading.value = false
  }
}

function openForm() {
  Object.assign(form, emptyForm())
  showForm.value = true
}
defineExpose({ openForm })

async function submit() {
  if (!form.portfolio_strategy_id) { alert('请选择组合'); return }
  if (!form.name.trim()) { alert('请填写名称'); return }
  if (!form.start_date || !form.end_date) { alert('请填写起止日期'); return }
  if (form.start_date > form.end_date) { alert('开始日期不能晚于结束日期'); return }
  if (!(form.perturb_pct > 0 && form.perturb_pct <= 0.5)) { alert('扰动幅度需在 (0, 0.5] 之间'); return }
  try {
    const res = await runParamPerturb({ ...form })
    if (res.status === 'failed') {
      alert('执行失败：' + (res.error || ''))
    }
    showForm.value = false
    await load()
    emit('done')
  } catch (e) {
    alert(errText(e))
  }
}

async function view(run: ParamPerturbRunItem) {
  try {
    detail.value = await getParamPerturbRun(run.id)
  } catch (e) {
    alert(errText(e))
  }
}

async function remove(run: ParamPerturbRunItem) {
  if (!confirm(`确认删除参数微扰「${run.name}」及其全部子回测记录？`)) return
  try {
    await deleteParamPerturbRun(run.id)
    if (detail.value?.id === run.id) detail.value = null
    await load()
  } catch (e) {
    alert(`删除失败：${errText(e)}`)
  }
}

function poolName(id: number): string {
  return props.portfolios.find((p) => p.id === id)?.name ?? `#${id}`
}

function roundPct(v: number | null): string { return v === null ? DASH : fmtPct(v) }

onMounted(load)
</script>

<template>
  <div>
    <p class="hint" style="color:var(--ink-soft);font-size:12px;margin:0 0 12px 0">
      对各策略参数按 ±扰动幅度微扰一次，比较年化收益差值以识别最敏感参数。
    </p>

    <div v-if="errMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:12px">{{ errMsg }}</div>

    <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
    <div v-else-if="runs.length === 0" class="card"><div class="empty-state"><p>暂无参数敏感度记录</p></div></div>
    <div v-else class="card table-wrap">
      <table>
        <thead>
          <tr><th>ID</th><th>名称</th><th>组合</th><th>区间</th><th>扰动幅度</th><th>最敏感参数</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td style="color:var(--ink-faint)">#{{ r.id }}</td>
            <td>{{ r.name }}</td>
            <td>{{ poolName(r.portfolio_strategy_id) }}</td>
            <td style="color:var(--ink-soft);font-size:12px">{{ r.start_date }} ~ {{ r.end_date }}</td>
            <td>{{ r.perturb_pct === null ? DASH : fmtPct(r.perturb_pct) }}</td>
            <td>{{ r.summary?.most_sensitive || DASH }}</td>
            <td><span class="badge" :class="r.status === 'completed' ? 'badge-green' : r.status === 'running' ? 'badge-blue' : 'badge-gray'">{{ r.status }}</span></td>
            <td style="white-space:nowrap">
              <button @click="view(r)" class="btn btn-sm btn-primary">查看</button>
              <button @click="remove(r)" class="btn btn-sm btn-danger" :disabled="r.status === 'running'">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 详情 -->
    <div v-if="detail" class="card" style="margin-top:16px;padding:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h3 style="margin:0">{{ detail.name }} · 敏感性详情</h3>
        <button @click="detail = null" class="btn btn-sm">关闭</button>
      </div>
      <div v-if="detail.error_message" style="color:var(--red);margin-bottom:8px">{{ detail.error_message }}</div>

      <template v-if="detail.summary">
        <h4 style="margin:4px 0">基准</h4>
        <div class="table-wrap" style="margin-bottom:16px">
          <table>
            <thead>
              <tr><th>年化</th><th>总收益</th><th>最大回撤</th><th>胜率</th><th>盈亏比</th><th>夏普</th><th>交易数</th><th>状态</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>{{ roundPct(detail.summary.baseline.annual_return) }}</td>
                <td>{{ roundPct(detail.summary.baseline.total_return) }}</td>
                <td>{{ roundPct(detail.summary.baseline.max_drawdown) }}</td>
                <td>{{ detail.summary.baseline.win_rate === null ? DASH : fmtPct(detail.summary.baseline.win_rate) }}</td>
                <td>{{ fmtRatio(detail.summary.baseline.profit_factor) }}</td>
                <td>{{ fmtRatio(detail.summary.baseline.sharpe_ratio) }}</td>
                <td>{{ detail.summary.baseline.total_trades ?? DASH }}</td>
                <td>{{ detail.summary.baseline.status || DASH }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <h4 style="margin:4px 0">参数敏感性（年化收益差值 = 高方向 − 低方向）</h4>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>参数</th><th>低方向年化</th><th>高方向年化</th><th>年化收益差值</th><th>最敏感</th></tr>
            </thead>
            <tbody>
              <tr v-for="p in detail.summary.params" :key="p.param">
                <td>{{ p.param }}</td>
                <td>{{ p.low ? roundPct(p.low.annual_return) : '不可扰动' }}</td>
                <td>{{ p.high ? roundPct(p.high.annual_return) : '不可扰动' }}</td>
                <td>{{ p.delta_cagr === null ? DASH : fmtSignedPct(p.delta_cagr) }}</td>
                <td><span v-if="p.param === detail.summary.most_sensitive" class="badge badge-gold">★ 最敏感</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <template v-if="detail.summary.rounds.some((r: PerturbRound) => r.error)">
          <h4 style="margin:16px 0 4px 0;color:var(--red)">错误轮次</h4>
          <div class="table-wrap">
            <table>
              <thead><tr><th>参数</th><th>方向</th><th>系数</th><th>错误</th></tr></thead>
              <tbody>
                <tr v-for="(r, i) in detail.summary.rounds.filter((x: PerturbRound) => x.error)" :key="i" class="row-error">
                  <td>{{ r.param }}</td>
                  <td>{{ r.direction }}</td>
                  <td>{{ fmtRatio(r.factor) }}</td>
                  <td style="color:var(--red)">{{ r.error }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>
    </div>

    <!-- 发起 Modal -->
    <div v-if="showForm" class="modal-overlay modal-lg" @click.self="showForm = false">
      <div class="modal-content">
        <h3>发起参数敏感度</h3>
        <div class="modal-body">
          <div class="form-group">
            <label class="group-label">基本信息</label>
            <div class="field-grid">
              <div class="field-row field-span-2">
                <span class="field-label">组合</span>
                <select v-model.number="form.portfolio_strategy_id">
                  <option v-for="p in portfolios" :key="p.id" :value="p.id">{{ p.name }}</option>
                </select>
              </div>
              <div class="field-row"><span class="field-label">名称</span><input v-model="form.name" placeholder="如 PP-A" /></div>
              <div class="field-row">
                <span class="field-label">评估口径</span>
                <select v-model="form.evaluation_profile">
                  <option value="absolute">绝对</option><option value="relative">相对</option>
                </select>
              </div>
              <div class="field-row"><span class="field-label">开始日期</span><input type="date" v-model="form.start_date" /></div>
              <div class="field-row"><span class="field-label">结束日期</span><input type="date" v-model="form.end_date" /></div>
              <div class="field-row"><span class="field-label">扰动幅度</span><input type="number" step="0.01" min="0.01" max="0.5" v-model.number="form.perturb_pct" /><span class="field-suffix">（0~0.5）</span></div>
              <div class="field-row">
                <span class="field-label">复权模式</span>
                <select v-model="form.dividend_mode">
                  <option value="front">前复权（默认）</option><option value="back">后复权</option><option value="dual">前复权+后复权记账（推荐）</option>
                </select>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-actions">
          <button @click="submit" class="btn btn-primary">确认</button>
          <button @click="showForm = false" class="btn">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>
