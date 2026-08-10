import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Mock API 模块：所有组件挂载测试都不发真实请求
vi.mock('../api', () => ({
  getFormulas: vi.fn(),
  getFormulaDetail: vi.fn(),
  createFormula: vi.fn(),
  updateFormula: vi.fn(),
  deleteFormula: vi.fn(),
}))

import Formulas from '../views/Formulas.vue'
import {
  getFormulas, createFormula, deleteFormula,
} from '../api'

const mockFormulas = [
  {
    id: 1, name: 'MACROSSPRO', content: 'REF(CLOSE,1)', formula_count: 500,
    signals: [
      { id: 1, signal_name: '开仓', signal_type: 'OPEN', trigger_value: 1 },
      { id: 2, signal_name: '平仓', signal_type: 'CLOSE', trigger_value: 1 },
    ],
  },
  {
    id: 2, name: 'OPEN_FORMULA', content: 'MA(CLOSE,5);', formula_count: 200,
    signals: [{ id: 3, signal_name: '开仓', signal_type: 'OPEN', trigger_value: 1 }],
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  ;(getFormulas as any).mockResolvedValue(mockFormulas)
})

describe('Formulas.vue', () => {
  it('挂载后渲染公式列表，每行显名称+信号数', async () => {
    const w = mount(Formulas)
    await flushPromises()

    const rows = w.findAll('tbody tr')
    expect(rows.length).toBe(2)
    expect(w.text()).toContain('MACROSSPRO')
    expect(w.text()).toContain('OPEN_FORMULA')
    // 信号数：第一条 2 个，第二条 1 个
    expect(w.text()).toContain('2 个信号')
    expect(w.text()).toContain('1 个信号')
  })

  it('点[+新建公式]弹出 Modal，含名称/内容/信号行/添加信号按钮', async () => {
    const w = mount(Formulas)
    await flushPromises()

    expect(w.find('.modal-overlay').exists()).toBe(false)
    await w.find('button.btn-primary').trigger('click')  // +新建公式
    expect(w.find('.modal-overlay').exists()).toBe(true)
    expect(w.text()).toContain('新建公式')
    expect(w.find('input[placeholder*="名称"]').exists()).toBe(true)
    expect(w.find('textarea').exists()).toBe(true)
    expect(w.text()).toContain('添加信号')
  })

  it('点[+添加信号]新增一行信号配置（信号行数 +1）', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    const before = w.findAll('.signal-row').length
    await w.find('button.signal-add').trigger('click')
    expect(w.findAll('.signal-row').length).toBe(before + 1)
  })

  it('填表 + 提交 → 调 createFormula 且参数含 name/content/signals', async () => {
    ;(createFormula as any).mockResolvedValue({ id: 99 })
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    // 填名称
    await w.find('input[placeholder*="名称"]').setValue('NEW_F')
    // 填公式内容
    await w.find('textarea').setValue('MA(CLOSE,5);')
    // 提交
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createFormula).toHaveBeenCalledTimes(1)
    const arg = (createFormula as any).mock.calls[0][0]
    expect(arg.name).toBe('NEW_F')
    expect(arg.content).toBe('MA(CLOSE,5);')
    expect(Array.isArray(arg.signals)).toBe(true)
  })

  it('列表渲染 count 列（公式级注入根数）', async () => {
    const w = mount(Formulas)
    await flushPromises()

    // 第一条 formula_count=500，第二条默认 200
    expect(w.text()).toContain('500')
    expect(w.text()).toContain('200')
  })

  it('新建弹窗默认 formula_count=200，提交时随请求发出', async () => {
    ;(createFormula as any).mockResolvedValue({ id: 99 })
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal

    // 默认 count=200
    expect((w.find('input[type="number"]').element as HTMLInputElement).value).toBe('200')
    // 填表 + 修改 count=500
    await w.find('input[placeholder*="名称"]').setValue('NEW_F')
    await w.find('textarea').setValue('MA(CLOSE,5);')
    await w.find('input[type="number"]').setValue('500')
    await w.find('.modal-actions button.btn-primary').trigger('click')
    await flushPromises()

    expect(createFormula).toHaveBeenCalledTimes(1)
    const arg = (createFormula as any).mock.calls[0][0]
    expect(arg.formula_count).toBe(500)
  })

  it('编辑时回填 formula_count（无字段回退 200）', async () => {
    const w = mount(Formulas)
    await flushPromises()

    // 第一条（id=1, formula_count=500）编辑 → 回填 500
    const editBtns = w.findAll('button.btn-sm.btn-primary')
    await editBtns[0].trigger('click')
    expect((w.find('input[type="number"]').element as HTMLInputElement).value).toBe('500')
    w.unmount()

    // 无 formula_count 的老数据 → 回退默认 200
    ;(getFormulas as any).mockResolvedValue([{ id: 9, name: 'OLD', content: 'X', signals: [] }])
    const w2 = mount(Formulas)
    await flushPromises()
    await w2.findAll('button.btn-sm.btn-primary')[0].trigger('click')
    expect((w2.find('input[type="number"]').element as HTMLInputElement).value).toBe('200')
  })

  it('点某行[删除] → 调 deleteFormula(id)', async () => {
    ;(deleteFormula as any).mockResolvedValue(null)
    // happy-dom 的 confirm 默认返回 undefined（→ !undefined=true 提前 return），
    // stub 成 true 让删除流程继续，以验证真实行为：点删除调 deleteFormula
    vi.stubGlobal('confirm', () => true)
    const w = mount(Formulas)
    await flushPromises()

    // 第一行的删除按钮
    const delBtn = w.findAll('button.btn-danger').find(b => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()

    expect(deleteFormula).toHaveBeenCalledTimes(1)
    // 删除的 id 是 mockFormulas 第一条（id=1）
    expect((deleteFormula as any).mock.calls[0][0]).toBe(1)
    vi.unstubAllGlobals()
  })
})
