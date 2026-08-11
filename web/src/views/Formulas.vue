<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getFormulas, createFormula, updateFormula, deleteFormula, type SignalItem, type FormulaItem } from '../api'

const formulas = ref<FormulaItem[]>([])
const loading = ref(true)
const showForm = ref(false)
const editingId = ref<number | null>(null)
const errorMsg = ref('')

// 从 axios 错误里提取后端错误消息（统一响应 {code,message} 或 Pydantic 422 detail）
function errMsg(e: any): string {
  const d = e?.response?.data
  if (d?.message) return d.message
  if (Array.isArray(d?.detail)) return d.detail.map((x: { loc?: unknown[]; msg?: string }) => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ')
  if (typeof d?.detail === 'string') return d.detail
  return e?.message || '请求失败'
}

const SIGNAL_TYPES = [
  { value: 'OPEN', label: '开仓' },
  { value: 'ADD', label: '加仓' },
  { value: 'REDUCE', label: '减仓' },
  { value: 'CLOSE', label: '平仓' },
]

const emptyForm = () => ({ name: '', content: '', formula_count: 200, signals: [{ signal_name: '', signal_type: 'OPEN', trigger_value: 1 }] as SignalItem[] })
const form = ref(emptyForm())

async function load() {
  errorMsg.value = ''
  try {
    formulas.value = await getFormulas()
  } catch (e) {
    errorMsg.value = '加载失败：' + errMsg(e)
    formulas.value = []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  form.value = emptyForm()
  showForm.value = true
}

function openEdit(f: FormulaItem) {
  editingId.value = f.id
  form.value = {
    name: f.name,
    content: f.content,
    formula_count: f.formula_count ?? 200,
    signals: f.signals.length
      ? f.signals.map((s) => ({ signal_name: s.signal_name, signal_type: s.signal_type, trigger_value: s.trigger_value }))
      : [{ signal_name: '', signal_type: 'OPEN', trigger_value: 1 }],
  }
  showForm.value = true
}

function addSignal() {
  form.value.signals.push({ signal_name: '', signal_type: 'OPEN', trigger_value: 1 })
}

function removeSignal(idx: number) {
  form.value.signals.splice(idx, 1)
}

async function submit() {
  try {
    if (editingId.value === null) {
      await createFormula(form.value)
    } else {
      await updateFormula(editingId.value, form.value)
    }
  } catch (e) {
    alert(`保存失败：${errMsg(e)}`)
    return  // 弹窗保持打开，供用户修正
  }
  showForm.value = false
  load()
}

async function remove(id: number) {
  if (!confirm('确认删除该公式？')) return
  try {
    await deleteFormula(id)
  } catch (e) {
    alert(`删除失败：${errMsg(e)}`)
    return
  }
  load()
}

onMounted(load)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:#c0392b;margin-bottom:12px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <div v-else class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>公式内容</th><th>count</th><th>信号</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="f in formulas" :key="f.id">
          <td style="color:#888">#{{ f.id }}</td>
          <td>{{ f.name }}</td>
          <td style="color:#888;font-size:13px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ f.content }}</td>
          <td style="color:#888">{{ f.formula_count ?? 200 }}</td>
          <td><span class="badge badge-blue">{{ f.signals.length }} 个信号</span></td>
          <td>
            <button @click="openEdit(f)" class="btn btn-sm btn-primary">编辑</button>
            <button @click="remove(f.id)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="formulas.length === 0" class="empty-state"><p>暂无公式</p></div>
  </div>

  <div v-if="showForm" class="modal-overlay modal-lg" @click.self="showForm = false">
    <div class="modal-content">
      <h3>{{ editingId === null ? '新建公式' : '编辑公式' }}</h3>
      <label>名称</label>
      <input v-model="form.name" placeholder="例如：MACROSSPRO（公式名称）" />
      <label>公式内容</label>
      <textarea v-model="form.content" rows="3" placeholder="通达信公式文本"></textarea>

      <label>注入历史根数 count（公式内最长均线/函数需的 bar 数，默认 200）</label>
      <input v-model.number="form.formula_count" type="number" min="1" placeholder="200" />

      <label>信号配置</label>
      <div v-for="(sig, idx) in form.signals" :key="idx" class="signal-row">
        <input v-model="sig.signal_name" placeholder="信号名称" />
        <select v-model="sig.signal_type">
          <option v-for="t in SIGNAL_TYPES" :key="t.value" :value="t.value">{{ t.label }}（{{ t.value }}）</option>
        </select>
        <select v-model="sig.trigger_value">
          <option :value="1">1</option>
          <option :value="-1">-1</option>
        </select>
        <button @click="removeSignal(idx)" class="btn btn-sm btn-danger">×</button>
      </div>
      <button @click="addSignal" class="btn btn-sm signal-add">+ 添加信号</button>

      <div class="modal-actions">
        <button @click="submit" class="btn btn-primary">确认</button>
        <button @click="showForm = false" class="btn">取消</button>
      </div>
    </div>
  </div>
</template>
