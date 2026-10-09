<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import {
  getFormulas, createFormula, updateFormula, deleteFormula,
  getTdxFormulas, getTdxInfo, importTdxFormula,
  type SignalItem, type FormulaItem, type TdxFormulaListItem,
} from '../api'

const formulas = ref<FormulaItem[]>([])
const loading = ref(true)
const showForm = ref(false)
const editingId = ref<number | null>(null)
const errorMsg = ref('')

// 通达信导入弹窗（三步流程：列表 → 导入 → 信号编辑）
const showTdx = ref(false)
const tdxLoading = ref(false)
const tdxItems = ref<TdxFormulaListItem[]>([])
// 搜索 + 分页（自编公式可能上百条）
const TDX_PAGE_SIZE = 10
const tdxQuery = ref('')
const tdxPage = ref(1)
const filteredTdx = computed(() => {
  const q = tdxQuery.value.trim().toLowerCase()
  if (!q) return tdxItems.value
  return tdxItems.value.filter(it =>
    it.acCode.toLowerCase().includes(q) || (it.acName || '').toLowerCase().includes(q))
})
const tdxTotalPages = computed(() => Math.max(1, Math.ceil(filteredTdx.value.length / TDX_PAGE_SIZE)))
const pagedTdx = computed(() =>
  filteredTdx.value.slice((tdxPage.value - 1) * TDX_PAGE_SIZE, tdxPage.value * TDX_PAGE_SIZE))
watch(tdxQuery, () => { tdxPage.value = 1 })
// 线名选项（tdx-info Line），信号编辑的 signal_name 预填
const lineNames = ref<string[]>([])

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
  lineNames.value = []
  showForm.value = true
}

// 通达信公式元数据的线名（公式不在通达信或接口失败时静默忽略，不阻塞编辑）
async function fetchLineNames(name: string) {
  lineNames.value = []
  try {
    const info = await getTdxInfo(name, 0)
    lineNames.value = (info.Line || []).map((l) => l.LineName).filter(Boolean)
  } catch {
    /* 非通达信公式或接口不可用：无预填选项即可 */
  }
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
  fetchLineNames(f.name)
}

async function openTdx() {
  showTdx.value = true
  tdxLoading.value = true
  tdxQuery.value = ''
  tdxPage.value = 1
  try {
    tdxItems.value = await getTdxFormulas(true)
  } catch (e) {
    alert(`获取通达信公式列表失败：${errMsg(e)}`)
    showTdx.value = false
  } finally {
    tdxLoading.value = false
  }
}

async function doImport(item: TdxFormulaListItem) {
  let created: FormulaItem
  try {
    created = await importTdxFormula(item.acCode)
  } catch (e) {
    alert(`导入失败：${errMsg(e)}`)
    return
  }
  // 第二步完成 → 直接进第三步：打开编辑弹窗配信号（线名预填）
  showTdx.value = false
  load()
  openEdit(created)
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
  <div style="margin-bottom:16px;display:flex;justify-content:flex-end;gap:8px">
    <button @click="openTdx" class="btn tdx-import-btn">从通达信导入</button>
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:12px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <div v-else class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>公式内容</th><th>count</th><th>信号</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="f in formulas" :key="f.id">
          <td style="color:var(--ink-soft)">#{{ f.id }}</td>
          <td>{{ f.name }}</td>
          <td style="color:var(--ink-soft);font-size:13px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ f.content }}</td>
          <td style="color:var(--ink-soft)">{{ f.formula_count ?? 200 }}</td>
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

  <div v-if="showTdx" class="modal-overlay" @click.self="showTdx = false">
    <div class="modal-content">
      <h3>从通达信导入公式</h3>
      <p style="color:var(--ink-soft);font-size:13px">列出通达信自编公式（isSys=0）；导入后请在编辑弹窗中配置信号。</p>
      <input v-model="tdxQuery" class="tdx-search" placeholder="搜索公式名 / 中文名" style="margin-bottom:8px;width:100%" />
      <div v-if="tdxLoading" style="padding:12px">加载中…</div>
      <template v-else>
        <table class="tdx-table">
          <thead><tr><th>公式名</th><th>中文名</th><th style="width:100px">操作</th></tr></thead>
          <tbody>
            <tr v-for="it in pagedTdx" :key="it.acCode">
              <td>{{ it.acCode }}</td>
              <td style="color:var(--ink-soft)">{{ it.acName || '—' }}</td>
              <td>
                <span v-if="it.imported" class="badge badge-blue">已导入</span>
                <button v-else @click="doImport(it)" class="btn btn-sm btn-primary">导入</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="filteredTdx.length === 0" class="empty-state"><p>无匹配公式</p></div>
        <div v-if="tdxTotalPages > 1" class="tdx-pagination" style="display:flex;align-items:center;gap:12px;margin-top:8px">
          <button class="btn btn-sm" :disabled="tdxPage <= 1" @click="tdxPage--">上一页</button>
          <span style="color:var(--ink-soft);font-size:13px">第 {{ tdxPage }} / {{ tdxTotalPages }} 页（共 {{ filteredTdx.length }} 条）</span>
          <button class="btn btn-sm" :disabled="tdxPage >= tdxTotalPages" @click="tdxPage++">下一页</button>
        </div>
      </template>
      <div class="modal-actions">
        <button @click="showTdx = false" class="btn">关闭</button>
      </div>
    </div>
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
      <datalist id="tdx-line-names">
        <option v-for="n in lineNames" :key="n" :value="n" />
      </datalist>
      <div v-for="(sig, idx) in form.signals" :key="idx" class="signal-row">
        <input v-model="sig.signal_name" list="tdx-line-names" placeholder="信号名称（可从通达信线名中选）" />
        <select v-model="sig.signal_type">
          <option v-for="t in SIGNAL_TYPES" :key="t.value" :value="t.value">{{ t.label }}（{{ t.value }}）</option>
        </select>
        <select v-model="sig.trigger_value" title="触发值：公式输出线等于该值时产生信号。0 是无信号常态值，不可用">
          <option :value="-2">-2</option>
          <option :value="-1">-1</option>
          <option :value="1">1</option>
          <option :value="2">2</option>
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
