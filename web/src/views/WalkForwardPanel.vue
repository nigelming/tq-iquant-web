<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import {
  getWalkForwardRuns, getWalkForwardRun, runWalkForward, deleteWalkForwardRun,
  type PortfolioItem, type WalkForwardRunItem, type WalkForwardRequest,
} from '../api'
import { fmtPct, fmtSignedPct, DASH } from '../utils/format'

const props = withDefaults(defineProps<{ portfolios?: PortfolioItem[] }>(), { portfolios: () => [] })
const emit = defineEmits<{ done: [] }>()

const runs = ref<WalkForwardRunItem[]>([])
const loading = ref(false)
const errMsg = ref('')
const showForm = ref(false)
const detail = ref<WalkForwardRunItem | null>(null)

function emptyForm(): WalkForwardRequest {
  return {
    portfolio_strategy_id: props.portfolios[0]?.id ?? 0,
    name: '',
    start_date: '',
    end_date: '',
    train_years: 3,
    test_years: 1,
    oos_gate_min_ratio: 0.5,
    dividend_mode: 'front',
    evaluation_profile: 'absolute',
  }
}
const form = reactive<WalkForwardRequest>(emptyForm())

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function load() {
  loading.value = true
  errMsg.value = ''
  try {
    runs.value = await getWalkForwardRuns()
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
  try {
    const res = await runWalkForward({ ...form })
    if (res.status === 'failed') {
      alert('区间不足或执行失败：' + (res.error || ''))
    }
    showForm.value = false
    await load()
    emit('done')
  } catch (e) {
    alert(errText(e))
  }
}

async function view(run: WalkForwardRunItem) {
  try {
    detail.value = await getWalkForwardRun(run.id)
  } catch (e) {
    alert(errText(e))
  }
}

async function remove(run: WalkForwardRunItem) {
  if (!confirm(`确认删除 walk-forward「${run.name}」及其全部子回测记录？`)) return
  try {
    await deleteWalkForwardRun(run.id)
    if (detail.value?.id === run.id) detail.value = null
    await load()
  } catch (e) {
    alert(`删除失败：${errText(e)}`)
  }
}

function poolName(id: number): string {
  return props.portfolios.find((p) => p.id === id)?.name ?? `#${id}`
}

onMounted(load)
</script>

<template>
  <div>
    <div v-if="errMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:12px">{{ errMsg }}</div>

    <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
    <div v-else-if="runs.length === 0" class="card"><div class="empty-state"><p>暂无滚动前推记录</p></div></div>
    <div v-else class="card table-wrap">
      <table>
        <thead>
          <tr><th>ID</th><th>名称</th><th>组合</th><th>区间</th><th>训练+验证</th><th>通过</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td style="color:var(--ink-faint)">#{{ r.id }}</td>
            <td>{{ r.name }}</td>
            <td>{{ poolName(r.portfolio_strategy_id) }}</td>
            <td style="color:var(--ink-soft);font-size:12px">{{ r.start_date }} ~ {{ r.end_date }}</td>
            <td>{{ r.train_years }}年 + {{ r.test_years }}年</td>
            <td>
              <span v-if="r.summary" class="badge" :class="r.summary.overall_pass ? 'badge-green' : 'badge-red'">
                {{ r.summary.overall_pass ? '全窗通过' : '未全通过' }} {{ r.summary.windows_passed }}
              </span>
              <span v-else style="color:var(--ink-faint)">—</span>
            </td>
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
        <h3 style="margin:0">{{ detail.name }} · 窗口详情</h3>
        <button @click="detail = null" class="btn btn-sm">关闭</button>
      </div>
      <div style="margin-bottom:8px">
        <span v-if="detail.summary" class="badge" :class="detail.summary.overall_pass ? 'badge-green' : 'badge-red'">
          {{ detail.summary.overall_pass ? '全窗通过' : '未全通过' }} {{ detail.summary.windows_passed }}
        </span>
        <span v-if="detail.summary && (detail.summary.windows_failed > 0 || detail.summary.windows_errored > 0)" class="badge badge-red">
          失败 {{ detail.summary.windows_failed }} / 出错 {{ detail.summary.windows_errored }}
        </span>
      </div>
      <div v-if="detail.error_message" style="color:var(--red);margin-bottom:8px">{{ detail.error_message }}</div>
      <p v-if="detail.summary" class="ca-note" style="margin-bottom:8px;color:var(--gold)">
        OOS 仅防时段崩塌，不防全样本调参
      </p>

      <div v-if="detail.summary" class="table-wrap">
        <table>
          <thead>
            <tr><th>窗口</th><th>训练区间</th><th>验证区间</th><th>IS年化</th><th>OOS年化</th><th>OOS收益</th><th>OOS基准</th><th>门槛</th><th>record_id</th></tr>
          </thead>
          <tbody>
            <tr v-for="w in detail.summary.windows" :key="w.index" :class="{ 'row-error': w.error }">
              <td>{{ w.index }}</td>
              <td style="font-size:12px;color:var(--ink-soft)">{{ w.train.start }} ~ {{ w.train.end }}</td>
              <td style="font-size:12px;color:var(--ink-soft)">{{ w.test.start }} ~ {{ w.test.end }}</td>
              <td>{{ w.is_cagr === null ? DASH : fmtPct(w.is_cagr) }}</td>
              <td>{{ w.oos_cagr === null ? DASH : fmtPct(w.oos_cagr) }}</td>
              <td>{{ w.oos_return === null ? DASH : fmtSignedPct(w.oos_return) }}</td>
              <td>{{ w.oos_benchmark_return === null ? DASH : fmtSignedPct(w.oos_benchmark_return) }}</td>
              <td>
                <span v-if="w.gate.status === 'passed'" class="badge badge-green">过</span>
                <span v-else-if="w.gate.status === 'rejected'" class="badge badge-red" :title="w.gate.reasons.join('；')">未过</span>
                <span v-else class="badge badge-gray" :title="w.gate.reasons.join('；')">未评估</span>
              </td>
              <td>
                <span v-if="w.error" style="color:var(--red)" :title="w.error">⚠ {{ w.error }}</span>
                <span v-else>{{ w.record_id ?? DASH }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 发起 Modal -->
    <div v-if="showForm" class="modal-overlay modal-lg" @click.self="showForm = false">
      <div class="modal-content">
        <h3>发起滚动前推</h3>
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
              <div class="field-row"><span class="field-label">名称</span><input v-model="form.name" placeholder="如 WF-A" /></div>
              <div class="field-row">
                <span class="field-label">评估口径</span>
                <select v-model="form.evaluation_profile">
                  <option value="absolute">绝对</option><option value="relative">相对</option>
                </select>
              </div>
              <div class="field-row"><span class="field-label">开始日期</span><input type="date" v-model="form.start_date" /></div>
              <div class="field-row"><span class="field-label">结束日期</span><input type="date" v-model="form.end_date" /></div>
              <div class="field-row"><span class="field-label">训练年数</span><input type="number" min="1" v-model.number="form.train_years" /></div>
              <div class="field-row"><span class="field-label">验证年数</span><input type="number" min="1" v-model.number="form.test_years" /></div>
              <div class="field-row"><span class="field-label">OOS 门槛</span><input type="number" step="0.1" min="0.1" max="2" v-model.number="form.oos_gate_min_ratio" /></div>
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
