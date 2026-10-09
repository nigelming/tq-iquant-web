import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// Mock API 模块：所有组件挂载测试都不发真实请求
vi.mock('../api', () => ({
  getFormulas: vi.fn(),
  getFormulaDetail: vi.fn(),
  createFormula: vi.fn(),
  updateFormula: vi.fn(),
  deleteFormula: vi.fn(),
  getTdxFormulas: vi.fn(),
  getTdxInfo: vi.fn(),
  importTdxFormula: vi.fn(),
}))

import Formulas from '../views/Formulas.vue'
import {
  getFormulas, createFormula, deleteFormula,
  getTdxFormulas, getTdxInfo, importTdxFormula,
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
  ;(getTdxFormulas as any).mockResolvedValue([
    { acCode: 'QZQ', acName: '', isSys: 0, imported: true, formula_id: 2 },
    { acCode: 'COSTLINE', acName: '', isSys: 0, imported: false, formula_id: null },
  ])
  ;(getTdxInfo as any).mockResolvedValue({
    acCode: 'COSTLINE', acName: '', isSys: 0, ParaNum: 0,
    LineNum: 2, Line: [{ LineName: '建仓' }, { LineName: '清仓' }],
  })
  ;(importTdxFormula as any).mockResolvedValue({
    id: 99, name: 'COSTLINE', content: '', formula_count: 200, signals: [],
  })
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

  it('触发值下拉不含 0（0 是无信号常态值，只保留 ±1/±2）', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.btn-primary').trigger('click')  // 打开 Modal
    await w.find('button.signal-add').trigger('click')  // 新增信号行

    const triggerSelect = w.findAll('.signal-row select')[1]  // 第二个 select=触发值
    const values = triggerSelect.findAll('option').map(o => o.attributes().value)
    expect(values).not.toContain('0')
    expect(values).toEqual(expect.arrayContaining(['-2', '-1', '1', '2']))
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

  it('load 失败 → 显示错误条，列表清空，页面不崩', async () => {
    ;(getFormulas as any).mockRejectedValue({ response: { data: { message: '数据库不可用' } } })
    const w = mount(Formulas)
    await flushPromises()

    expect(w.text()).toContain('加载失败')
    expect(w.text()).toContain('数据库不可用')
    expect(w.findAll('tbody tr').length).toBe(0)
    w.unmount()
  })

  it('删除失败 → alert 提示，不崩', async () => {
    ;(deleteFormula as any).mockRejectedValue({ response: { data: { message: '被引用，无法删除' } } })
    const alertMock = vi.fn()
    vi.stubGlobal('confirm', () => true)
    vi.stubGlobal('alert', alertMock)
    const w = mount(Formulas)
    await flushPromises()

    const delBtn = w.findAll('button.btn-danger').find(b => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()

    expect(alertMock).toHaveBeenCalled()
    expect(alertMock.mock.calls[0][0]).toContain('被引用，无法删除')
    vi.unstubAllGlobals()
  })

  // -------------------------------------------------------------------------
  // 从通达信导入（三步流程）：列表弹窗 → 导入落库 → 信号编辑线名预填
  // -------------------------------------------------------------------------
  it('点[从通达信导入] → 弹窗调 getTdxFormulas，渲染 acCode，已导入的带标记', async () => {
    const w = mount(Formulas)
    await flushPromises()

    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()

    expect(getTdxFormulas).toHaveBeenCalledWith(true)  // 默认只看自编
    expect(w.text()).toContain('COSTLINE')
    expect(w.text()).toContain('QZQ')
    // QZQ 已导入 → 标记；COSTLINE 未导入 → 显示「导入」按钮
    expect(w.text()).toContain('已导入')
    const importBtn = w.findAll('button').find(b => b.text() === '导入')
    expect(importBtn).toBeTruthy()
  })

  it('点未导入行的[导入] → 调 importTdxFormula + getTdxInfo，打开编辑弹窗并预填名称', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()

    const importBtn = w.findAll('button').find(b => b.text() === '导入')!
    await importBtn.trigger('click')
    await flushPromises()

    expect(importTdxFormula).toHaveBeenCalledWith('COSTLINE')
    expect(getTdxInfo).toHaveBeenCalledWith('COSTLINE', 0)
    // tdx 弹窗关闭，编辑弹窗打开，名称预填 acCode
    expect(w.text()).toContain('编辑公式')
    const nameInput = w.find('input[placeholder*="名称"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('COSTLINE')
    // content 预填空（通达信拿不到源码）
    expect((w.find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })

  it('信号编辑弹窗打开时，signal_name 输入挂 datalist（线名预填选项）', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()
    const importBtn = w.findAll('button').find(b => b.text() === '导入')!
    await importBtn.trigger('click')
    await flushPromises()

    const sigInput = w.find('.signal-row input')
    expect(sigInput.exists()).toBe(true)
    expect(sigInput.attributes('list')).toBe('tdx-line-names')
    const datalist = w.find('datalist#tdx-line-names')
    expect(datalist.exists()).toBe(true)
    const options = datalist.findAll('option')
    expect(options.map(o => o.element.value)).toEqual(['建仓', '清仓'])
  })

  it('编辑已有公式时也拉 tdx-info 填充线名选项（404 静默忽略）', async () => {
    ;(getTdxInfo as any).mockRejectedValue({ response: { data: { code: 404, message: '不存在' } } })
    const w = mount(Formulas)
    await flushPromises()

    // 编辑 id=1 MACROSSPRO
    await w.findAll('button.btn-sm.btn-primary')[0].trigger('click')
    await flushPromises()

    expect(getTdxInfo).toHaveBeenCalledWith('MACROSSPRO', 0)
    // 404 不崩，编辑弹窗照常打开
    expect(w.text()).toContain('编辑公式')
    expect(w.find('datalist#tdx-line-names').exists()).toBe(true)
  })

  it('通达信弹窗支持关键字搜索（acCode/acName 过滤，清空恢复）', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()

    await w.find('input.tdx-search').setValue('COST')
    let rows = w.findAll('.tdx-table tbody tr')
    expect(rows.length).toBe(1)
    expect(rows[0].text()).toContain('COSTLINE')
    expect(w.text()).not.toContain('QZQ')

    // 清空 → 恢复全量
    await w.find('input.tdx-search').setValue('')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(2)
  })

  it('通达信弹窗搜索无匹配 → 显示空状态', async () => {
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()

    await w.find('input.tdx-search').setValue('NOSUCH')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(0)
    expect(w.text()).toContain('无匹配公式')
  })

  it('通达信弹窗分页：每页 10 条，可翻页，搜索后回到第 1 页', async () => {
    ;(getTdxFormulas as any).mockResolvedValue(
      Array.from({ length: 25 }, (_, i) => ({
        acCode: `F${String(i + 1).padStart(2, '0')}`, acName: '', isSys: 0, imported: false, formula_id: null,
      })),
    )
    const w = mount(Formulas)
    await flushPromises()
    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()

    // 第 1 页 10 条
    expect(w.findAll('.tdx-table tbody tr').length).toBe(10)
    expect(w.text()).toContain('F01')
    expect(w.text()).not.toContain('F11')
    expect(w.text()).toContain('共 25 条')

    // 翻到第 2 页
    const nextBtn = w.findAll('.tdx-pagination button').find(b => b.text().includes('下一页'))!
    await nextBtn.trigger('click')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(10)
    expect(w.text()).toContain('F11')
    expect(w.text()).not.toContain('F01')

    // 搜索 F2 → 只剩 6 条（≤10 → 分页条隐藏），且从匹配区头部开始
    await w.find('input.tdx-search').setValue('F2')
    await flushPromises()
    let rows = w.findAll('.tdx-table tbody tr')
    expect(rows.length).toBe(6)  // F20-F25 共 6 条匹配
    expect(rows[0].text()).toContain('F20')
    expect(w.find('.tdx-pagination').exists()).toBe(false)

    // 清空 → 回到第 1 页（watch 复位，而非停留在第 2 页）
    await w.find('input.tdx-search').setValue('')
    await flushPromises()
    rows = w.findAll('.tdx-table tbody tr')
    expect(rows.length).toBe(10)
    expect(rows[0].text()).toContain('F01')
    expect(w.text()).toContain('第 1 / 3 页')
  })
})
