<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getFormulas, createFormula, updateFormula, deleteFormula, type SignalItem } from '../api'

const formulas = ref<any[]>([])
const showForm = ref(false)
const editingId = ref<number | null>(null)

const SIGNAL_TYPES = [
  { value: 'OPEN', label: '开仓' },
  { value: 'ADD', label: '加仓' },
  { value: 'REDUCE', label: '减仓' },
  { value: 'CLOSE', label: '平仓' },
]

const emptyForm = () => ({ name: '', content: '', signals: [{ signal_name: '', signal_type: 'OPEN', trigger_value: 1 }] as SignalItem[] })
const form = ref(emptyForm())

async function load() {
  formulas.value = await getFormulas()
}

function openCreate() {
  editingId.value = null
  form.value = emptyForm()
  showForm.value = true
}

function openEdit(f: any) {
  editingId.value = f.id
  form.value = {
    name: f.name,
    content: f.content,
    signals: f.signals.length
      ? f.signals.map((s: any) => ({ signal_name: s.signal_name, signal_type: s.signal_type, trigger_value: s.trigger_value }))
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
  if (editingId.value === null) {
    await createFormula(form.value)
  } else {
    await updateFormula(editingId.value, form.value)
  }
  showForm.value = false
  load()
}

async function remove(id: number) {
  if (!confirm('确认删除该公式？')) return
  await deleteFormula(id)
  load()
}

onMounted(load)
</script>

<template>
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end">
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </div>

  <div class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>公式内容</th><th>信号</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="f in formulas" :key="f.id">
          <td style="color:#888">#{{ f.id }}</td>
          <td>{{ f.name }}</td>
          <td style="color:#888;font-size:13px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ f.content }}</td>
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
