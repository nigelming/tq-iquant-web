<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import {
  getFormulas, createFormula, updateFormula, deleteFormula,
  getTdxFormulas, getTdxInfo, importTdxFormula, precheckFormula,
  normalizePreview, testRunFormula,
  type SignalItem, type FormulaItem, type TdxFormulaListItem, type PrecheckResult,
  type NormalizePlan, type NormalizePreviewResult, type TestRunResult,
} from '../api'
import PageHeader from '../components/PageHeader.vue'
import { buildPinyinIndex, matchTdx, type TdxPinyin } from '../utils/tdxSearch'

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
// 拼音首字母索引（加载后建一次；查询时空 query 全命中）
const tdxPy = ref(new Map<string, TdxPinyin>())
const filteredTdx = computed(() =>
  tdxItems.value.filter(it => matchTdx(tdxQuery.value, it, tdxPy.value.get(it.acCode))))
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

const emptyForm = () => ({ name: '', content: '', formula_count: 200, engine: 'tq', no_leak_cross: false, signals: [{ signal_name: '', signal_type: 'OPEN', trigger_value: 1 }] as SignalItem[] })
const form = ref(emptyForm())

// 语法预检（内置引擎无数据干跑；tq 公式也可用，作能力闸门提示）
const prechecking = ref(false)
const precheckResult = ref<PrecheckResult | null>(null)

async function runPrecheck() {
  if (!form.value.content.trim()) {
    alert('请先填写公式内容')
    return
  }
  prechecking.value = true
  precheckResult.value = null
  try {
    precheckResult.value = await precheckFormula(form.value.content)
  } catch (e) {
    alert(`预检失败：${errMsg(e)}`)
  } finally {
    prechecking.value = false
  }
}

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
  precheckResult.value = null
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
    engine: f.engine ?? 'tq',
    no_leak_cross: f.no_leak_cross ?? false,
    signals: f.signals.length
      ? f.signals.map((s) => ({ signal_name: s.signal_name, signal_type: s.signal_type, trigger_value: s.trigger_value }))
      : [{ signal_name: '', signal_type: 'OPEN', trigger_value: 1 }],
  }
  precheckResult.value = null
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
    tdxPy.value = buildPinyinIndex(tdxItems.value)
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

// ---------------------------------------------------------------------------
// 0016 信号标准化向导（三步）：①勾选输出线 ②逐线选规则+参数 ③预览并保存为新公式
// 源公式零改动；入口先跑语法预检（E6：语法错误/无 `:` 输出线不给进向导）
// ---------------------------------------------------------------------------
interface NormSel {
  line: string
  rule: NormalizePlan['rule']  // cross_zero | above_threshold | map_values
  direction: string  // cross_zero: both|up|down；above_threshold: gt|lt
  threshold: string
  mappings: { from: string; to: number }[]
}
const NORM_RULES = [
  { value: 'cross_zero', label: '上穿/下穿 0 轴' },
  { value: 'above_threshold', label: '大于/小于阈值' },
  { value: 'map_values', label: '值域映射（精确相等）' },
]
const NORM_TARGETS = [-2, -1, 1, 2]

const showNorm = ref(false)
const normStep = ref(1)
const normSource = ref<FormulaItem | null>(null)
const normLines = ref<{ name: string; checked: boolean }[]>([])
const normSels = ref<NormSel[]>([])
const normPreview = ref<NormalizePreviewResult | null>(null)
const normSaving = ref(false)
const normForm = ref({
  name: '', engine: 'tq', no_leak_cross: false, formula_count: 200,
  signals: [] as SignalItem[],
})

async function openNorm(f: FormulaItem) {
  try {
    const res = await precheckFormula(f.content)
    if (!res.ok) {
      alert('语法预检未通过，请先在「编辑」中修复公式错误后再做信号标准化')
      return
    }
    if (!res.outputs.length) {
      alert('该公式没有 `:` 输出线（:= 中间变量与绘图指令不可配信号），无法做信号标准化')
      return
    }
    normSource.value = f
    normLines.value = res.outputs.map((n) => ({ name: n, checked: false }))
    normSels.value = []
    normPreview.value = null
    normStep.value = 1
    showNorm.value = true
  } catch (e) {
    alert(`预检失败：${errMsg(e)}`)
  }
}

function toNormStep2() {
  const checked = normLines.value.filter((l) => l.checked).map((l) => l.name)
  if (!checked.length) {
    alert('请先勾选至少一条输出线')
    return
  }
  normSels.value = checked.map((line) => ({
    line, rule: 'cross_zero', direction: 'both', threshold: '0',
    mappings: [{ from: '1', to: 1 }],
  }))
  normStep.value = 2
}

function addMapping(sel: NormSel) {
  if (sel.mappings.length >= 4) {
    alert('映射对最多 4 个')
    return
  }
  sel.mappings.push({ from: '', to: 1 })
}

async function toNormStep3() {
  if (!normSource.value) return
  // 前端先做数值合法性检查，再交给后端 normalize-preview
  for (const sel of normSels.value) {
    if (sel.rule === 'above_threshold' && !Number.isFinite(Number(sel.threshold))) {
      alert(`输出线 ${sel.line} 的阈值不是数字`)
      return
    }
    if (sel.rule === 'map_values') {
      for (const m of sel.mappings) {
        if (m.from === '' || !Number.isFinite(Number(m.from))) {
          alert(`输出线 ${sel.line} 的映射原值不是数字`)
          return
        }
      }
    }
  }
  const plans: NormalizePlan[] = normSels.value.map((sel) => {
    const params: Record<string, unknown> = {}
    if (sel.rule === 'cross_zero') {
      params.direction = sel.direction
    } else if (sel.rule === 'above_threshold') {
      params.threshold = Number(sel.threshold)
      params.direction = sel.direction
    } else {
      params.mappings = sel.mappings.map((m) => ({ from: Number(m.from), to: m.to }))
    }
    return { line: sel.line, rule: sel.rule, params }
  })
  try {
    const res = await normalizePreview(normSource.value.content, plans)
    normPreview.value = res
    // 默认名 原名-STD；engine/跨周期开关/注入根数继承源公式（保存后仍可在编辑中修改）
    normForm.value = {
      name: `${normSource.value.name}-STD`,
      engine: normSource.value.engine ?? 'tq',
      no_leak_cross: normSource.value.no_leak_cross ?? false,
      formula_count: normSource.value.formula_count ?? 200,
      // 生成线预填为开仓信号 + 触发值 1，用户可改类型与触发值
      signals: res.output_lines.map((l) => ({
        signal_name: l.name, signal_type: 'OPEN', trigger_value: 1,
      })),
    }
    normStep.value = 3
  } catch (e) {
    alert(`生成预览失败：${errMsg(e)}`)
  }
}

async function saveNorm() {
  if (!normPreview.value) return
  if (!normForm.value.name.trim()) {
    alert('公式名称不能为空')
    return
  }
  for (const sig of normForm.value.signals) {
    if (!String(sig.signal_name ?? '').trim()) {
      alert('信号名称不能为空')
      return
    }
  }
  normSaving.value = true
  try {
    await createFormula({ ...normForm.value, content: normPreview.value.new_content })
    showNorm.value = false
    load()
  } catch (e) {
    alert(`保存失败：${errMsg(e)}`)
  } finally {
    normSaving.value = false
  }
}

async function submit() {
  if (!form.value.name.trim()) {
    alert('公式名称不能为空')
    return
  }
  // formula_count 空/非法回落默认 200（后端要求 ≥1）
  const count = Number(form.value.formula_count)
  if (!Number.isFinite(count) || count < 1) form.value.formula_count = 200
  for (const sig of form.value.signals) {
    if (!String(sig.signal_name ?? '').trim()) {
      alert('信号名称不能为空')
      return
    }
  }
  try {
    if (editingId.value === null) {
      await createFormula(form.value)
    } else {
      await updateFormula(editingId.value, form.value)
    }
  } catch (e) {
    const msg = `保存失败：${errMsg(e)}`
    errorMsg.value = msg
    alert(msg)
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

// ---------------------------------------------------------------------------
// 1.3.1 公式试算：任选股票/周期/bar 数 → POST /formulas/test-run → 结果小表
// ---------------------------------------------------------------------------
const TEST_PERIODS = ['1m', '5m', '15m', '30m', '1h', '1d', '1w', '1mon']
const TEST_RUN_MAX_CODES = 20

const showTestRun = ref(false)
const testRunning = ref(false)
const testRunSource = ref<FormulaItem | null>(null)
const testResult = ref<TestRunResult | null>(null)
const testRunForm = ref({ codes: '', period: '1d', count: 20 })

// 解析股票代码：每行一个或逗号/分号/空白分隔，去重（保序）
function parseTestRunCodes(raw: string): string[] {
  return Array.from(new Set(
    raw.split(/[\n\r,，;；\s]+/).map((s) => s.trim()).filter(Boolean),
  ))
}

function openTestRun(f: FormulaItem) {
  testRunSource.value = f
  testRunForm.value = { codes: '', period: '1d', count: 20 }
  testResult.value = null
  showTestRun.value = true
}

async function doTestRun() {
  if (!testRunSource.value || testRunning.value) return
  // 参数错误前端快速失败，不发请求
  const codes = parseTestRunCodes(testRunForm.value.codes)
  if (!codes.length) {
    alert('请先填写至少一个股票代码（每行一个或逗号分隔）')
    return
  }
  if (codes.length > TEST_RUN_MAX_CODES) {
    alert(`股票代码最多 ${TEST_RUN_MAX_CODES} 只（当前 ${codes.length} 只）`)
    return
  }
  testRunning.value = true
  try {
    testResult.value = await testRunFormula({
      // 通达信导入公式平台库不存源码（content 空）→ 后端按 formula_id 走
      // 公式名计算（与回测/实盘同路径）；手工新建公式有源码 → 源码试算
      content: testRunSource.value.content || '',
      formula_id: testRunSource.value.id,
      stock_codes: codes,
      period: testRunForm.value.period,
      count: testRunForm.value.count || 20,
    })
  } catch (e) {
    // 失败提示后弹窗保留，供修正后重试
    alert(`试算失败：${errMsg(e)}`)
  } finally {
    testRunning.value = false
  }
}

onMounted(load)
</script>

<template>
  <PageHeader title="公式管理">
    <button @click="openTdx" class="btn tdx-import-btn">从通达信导入</button>
    <button @click="openCreate" class="btn btn-primary">+ 新建公式</button>
  </PageHeader>

  <div v-if="errorMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:12px">
    {{ errorMsg }}
  </div>

  <div v-if="loading" class="card" style="padding:12px"><p>加载中…</p></div>
  <div v-else class="card table-wrap">
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>公式内容</th><th>count</th><th>引擎</th><th>信号</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="f in formulas" :key="f.id">
          <td style="color:var(--ink-soft)">#{{ f.id }}</td>
          <td>{{ f.name }}</td>
          <td style="color:var(--ink-soft);font-size:13px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ f.content }}</td>
          <td style="color:var(--ink-soft)">{{ f.formula_count ?? 200 }}</td>
          <td><span class="badge" :class="f.engine === 'builtin' ? 'badge-blue' : ''">{{ f.engine === 'builtin' ? '内置' : '通达信' }}</span></td>
          <td><span class="badge badge-blue">{{ f.signals.length }} 个信号</span></td>
          <td>
            <button @click="openEdit(f)" class="btn btn-sm btn-primary">编辑</button>
            <button @click="openTestRun(f)" class="btn btn-sm" style="margin-left:6px" title="任选股票/周期/bar 数跑一遍公式，看各输出线的触发统计">试算</button>
            <button @click="openNorm(f)" class="btn btn-sm norm-entry" style="margin-left:6px" title="改造为投资平台可接收的信号标识（±1/±2/0 离散输出线）">信号标准化</button>
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
      <input v-model="tdxQuery" class="tdx-search" placeholder="搜索公式名 / 中文名 / 拼音首字母" style="margin-bottom:8px;width:100%" />
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
      <div class="modal-body">
        <div class="form-group">
          <div class="field-row field-span-2">
            <span class="field-label">名称</span>
            <input v-model="form.name" placeholder="例如：MACROSSPRO（公式名称）" />
          </div>
          <div class="field-row field-span-2">
            <span class="field-label">公式内容</span>
            <textarea v-model="form.content" rows="3" placeholder="通达信公式文本"></textarea>
          </div>
          <div class="field-row field-span-2">
            <span class="field-label">计算引擎</span>
            <select v-model="form.engine">
              <option value="tq">通达信计算（默认，需本地通达信）</option>
              <option value="builtin">内置引擎（不依赖通达信，仅回测）</option>
            </select>
          </div>
          <div v-if="form.engine === 'builtin'" class="field-row field-span-2">
            <span class="field-label"></span>
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
              <input v-model="form.no_leak_cross" type="checkbox" />
              跨周期引用无泄露（默认关=通达信语义；开启后 C#WEEK 等仅引用已完成上级 bar）
            </label>
          </div>
          <div class="field-row field-span-2">
            <span class="field-label"></span>
            <button @click="runPrecheck" class="btn btn-sm" :disabled="prechecking">
              {{ prechecking ? '预检中…' : '语法预检' }}
            </button>
            <span class="form-hint" style="margin:0;align-self:center">检查内置引擎不支持的函数/语法错误（不影响保存）</span>
          </div>
          <div v-if="precheckResult" class="precheck-result field-span-2" style="grid-column:1/-1;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px">
            <p v-if="precheckResult.ok" style="color:var(--green);margin:0 0 4px">预检通过</p>
            <p v-else style="color:var(--red);margin:0 0 4px">预检未通过</p>
            <p v-for="(e, i) in precheckResult.errors" :key="'e' + i" style="color:var(--red);margin:0 0 2px">{{ e }}</p>
            <p v-for="(it, i) in precheckResult.issues" :key="'i' + i" :style="{ color: it.level === 'error' ? 'var(--red)' : 'var(--warn)', margin: '0 0 2px' }">
              [{{ it.level === 'error' ? '错误' : '警告' }}] {{ it.message }}
            </p>
            <p v-if="precheckResult.outputs.length" style="color:var(--ink-soft);margin:4px 0 0">
              输出线：{{ precheckResult.outputs.join('、') }}（信号名称需与输出线名一致）
            </p>
          </div>
          <div class="field-row field-span-2">
            <span class="field-label">注入根数</span>
            <input v-model.number="form.formula_count" type="number" min="1" placeholder="200" />
            <span class="form-hint" style="margin:0;align-self:center">公式内最长均线/函数需的 bar 数，默认 200</span>
          </div>
        </div>

        <label class="group-label">信号配置</label>
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
      </div>

      <div class="modal-actions">
        <button @click="submit" class="btn btn-primary">确认</button>
        <button @click="showForm = false" class="btn">取消</button>
      </div>
    </div>
  </div>

  <!-- 0016 信号标准化向导：选线 → 配规则 → 预览并保存为新公式（源公式零改动） -->
  <div v-if="showNorm" class="modal-overlay modal-lg" @click.self="showNorm = false">
    <div class="modal-content">
      <h3>
        信号标准化向导
        <span style="color:var(--ink-soft);font-size:13px;margin-left:8px">源公式：{{ normSource?.name }}</span>
      </h3>
      <p style="color:var(--ink-soft);font-size:13px;margin:4px 0">
        第 {{ normStep }} / 3 步：{{ normStep === 1 ? '勾选要标准化的输出线' : normStep === 2 ? '为每条线选择规则与参数' : '预览新公式并保存' }}
      </p>

      <div v-if="normStep === 1" class="modal-body">
        <p style="color:var(--ink-soft);font-size:13px;margin:0 0 6px">
          仅 `:` 输出线可配信号；未勾选的输出线同样会降级为中间变量（原公式不受影响）。
        </p>
        <label v-for="l in normLines" :key="l.name" class="norm-line-row">
          <input type="checkbox" v-model="l.checked" />
          <code>{{ l.name }}</code>
        </label>
      </div>

      <div v-else-if="normStep === 2" class="modal-body">
        <div v-for="sel in normSels" :key="sel.line" class="norm-sel-block">
          <h4 style="margin:10px 0 4px"><code>{{ sel.line }}</code></h4>
          <div class="norm-param-row">
            <span class="field-label">规则</span>
            <select v-model="sel.rule">
              <option v-for="r in NORM_RULES" :key="r.value" :value="r.value">{{ r.label }}</option>
            </select>
          </div>
          <div v-if="sel.rule === 'cross_zero'" class="norm-param-row">
            <span class="field-label">方向</span>
            <select v-model="sel.direction">
              <option value="both">双向（上穿 XG + 下穿 XD）</option>
              <option value="up">仅上穿 0</option>
              <option value="down">仅下穿 0</option>
            </select>
          </div>
          <div v-if="sel.rule === 'above_threshold'" class="norm-param-row">
            <span class="field-label">阈值</span>
            <input v-model="sel.threshold" type="number" step="any" style="width:120px" />
            <select v-model="sel.direction" style="margin-left:8px">
              <option value="gt">线 &gt; 阈值 → 1</option>
              <option value="lt">线 &lt; 阈值 → 1</option>
            </select>
          </div>
          <div v-if="sel.rule === 'map_values'" class="norm-param-col">
            <div v-for="(m, mi) in sel.mappings" :key="mi" class="norm-param-row">
              <span style="font-size:13px">原值</span>
              <input v-model="m.from" type="number" step="any" style="width:100px" />
              <span style="font-size:13px">→</span>
              <select v-model.number="m.to" style="width:70px">
                <option v-for="t in NORM_TARGETS" :key="t" :value="t">{{ t }}</option>
              </select>
              <button class="btn btn-sm btn-danger" @click="sel.mappings.splice(mi, 1)">×</button>
            </div>
            <div style="margin-top:4px">
              <button class="btn btn-sm" @click="addMapping(sel)">+ 添加映射对</button>
              <span class="form-hint">最多 4 对；按原值精确相等匹配（浮点原值需与公式输出完全一致），未命中的 bar 输出 0</span>
            </div>
          </div>
        </div>
      </div>

      <div v-else class="modal-body">
        <div v-for="(w, i) in normPreview?.warnings || []" :key="'w' + i" style="color:var(--warn);font-size:13px;margin:0 0 2px">{{ w }}</div>
        <div class="field-row field-span-2">
          <span class="field-label">新公式名称</span>
          <input v-model="normForm.name" placeholder="默认：原公式名-STD" />
        </div>
        <div class="field-row field-span-2">
          <span class="field-label">生成源码</span>
          <pre class="norm-preview-src">{{ normPreview?.new_content }}</pre>
        </div>
        <label class="group-label">生成的信号线（已预填开仓/触发值 1，可修改）</label>
        <table class="norm-out-table">
          <thead><tr><th>线名</th><th>触发含义</th><th>表达式</th></tr></thead>
          <tbody>
            <tr v-for="l in normPreview?.output_lines || []" :key="l.name">
              <td><code>{{ l.name }}</code></td>
              <td>{{ l.meaning }}</td>
              <td style="font-size:12px;color:var(--ink-soft)">{{ l.expr }}</td>
            </tr>
          </tbody>
        </table>
        <div v-for="(sig, idx) in normForm.signals" :key="idx" class="signal-row" style="margin-top:6px">
          <input v-model="sig.signal_name" placeholder="信号名称（生成线名）" />
          <select v-model="sig.signal_type">
            <option v-for="t in SIGNAL_TYPES" :key="t.value" :value="t.value">{{ t.label }}（{{ t.value }}）</option>
          </select>
          <select v-model="sig.trigger_value" title="触发值：生成线等于该值时产生信号。0 是无信号常态值，不可用">
            <option :value="-2">-2</option>
            <option :value="-1">-1</option>
            <option :value="1">1</option>
            <option :value="2">2</option>
          </select>
          <button @click="normForm.signals.splice(idx, 1)" class="btn btn-sm btn-danger">×</button>
        </div>
        <button @click="normForm.signals.push({ signal_name: '', signal_type: 'OPEN', trigger_value: 1 })" class="btn btn-sm signal-add">+ 添加信号</button>
      </div>

      <div class="modal-actions">
        <button v-if="normStep > 1" @click="normStep--" class="btn">上一步</button>
        <button v-if="normStep === 1" @click="toNormStep2" class="btn btn-primary">下一步</button>
        <button v-if="normStep === 2" @click="toNormStep3" class="btn btn-primary">生成预览</button>
        <button v-if="normStep === 3" @click="saveNorm" class="btn btn-primary" :disabled="normSaving">
          {{ normSaving ? '保存中…' : '保存为新公式' }}
        </button>
        <button @click="showNorm = false" class="btn">取消</button>
      </div>
    </div>
  </div>

  <!-- 1.3.1 公式试算：任选股票/周期/bar 数 → 结果按股票分组小表 -->
  <div v-if="showTestRun" class="modal-overlay modal-lg" @click.self="showTestRun = false">
    <div class="modal-content">
      <h3>
        公式试算
        <span style="color:var(--ink-soft);font-size:13px;margin-left:8px">公式：{{ testRunSource?.name }}</span>
      </h3>
      <div class="modal-body">
        <div class="field-row field-span-2">
          <span class="field-label">股票代码</span>
          <textarea v-model="testRunForm.codes" rows="3" placeholder="每行一个或逗号分隔，如 000001.SZ（最多 20 只）"></textarea>
        </div>
        <div class="field-row field-span-2">
          <span class="field-label">周期</span>
          <select v-model="testRunForm.period">
            <option v-for="p in TEST_PERIODS" :key="p" :value="p">{{ p }}</option>
          </select>
          <span class="field-label" style="margin-left:8px">bar 数</span>
          <input v-model.number="testRunForm.count" type="number" min="1" style="width:100px" />
        </div>
        <div v-if="testResult" class="field-span-2">
          <p v-if="testResult.warning" style="color:var(--red);font-size:13px;margin:0 0 6px">⚠️ {{ testResult.warning }}</p>
          <div v-for="(lines, code) in testResult.results" :key="code" style="margin-bottom:10px">
            <h4 style="margin:6px 0 2px">{{ code }}</h4>
            <table class="norm-out-table">
              <thead><tr><th>线名</th><th>n</th><th>值=1 次数</th><th>末 3 值</th></tr></thead>
              <tbody>
                <tr v-for="(line, name) in lines" :key="name">
                  <td><code>{{ name }}</code></td>
                  <td>{{ line.n }}</td>
                  <td>{{ line.ones }}</td>
                  <td style="font-size:12px;color:var(--ink-soft)">{{ (line.last_values || []).join(', ') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="Object.keys(testResult.results).length === 0" class="empty-state"><p>无结果</p></div>
        </div>
      </div>
      <div class="modal-actions">
        <button @click="doTestRun" class="btn btn-primary" :disabled="testRunning">
          {{ testRunning ? '试算中…' : '试算' }}
        </button>
        <button @click="showTestRun = false" class="btn">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.norm-line-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; }
.norm-sel-block { border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px 8px; margin-bottom: 8px; }
.norm-param-row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
.norm-param-col { display: flex; flex-direction: column; margin: 4px 0; }
.norm-preview-src {
  background: var(--bg, #f7f8fa); padding: 8px 10px; border-radius: 6px;
  font-size: 13px; overflow: auto; max-height: 200px; margin: 0; white-space: pre-wrap;
  font-family: Consolas, Menlo, monospace;
}
.norm-out-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px; }
.norm-out-table th, .norm-out-table td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; }
</style>
