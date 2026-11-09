import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// SystemConfig.vue 走 ../api 客户端（getSystemConfigs/updateSystemConfigs）
const { mockGetSystemConfigs, mockUpdateSystemConfigs, mockTestLiveChannel } = vi.hoisted(() => ({
  mockGetSystemConfigs: vi.fn(),
  mockUpdateSystemConfigs: vi.fn(),
  mockTestLiveChannel: vi.fn(),
}))
vi.mock('../api', () => ({
  getSystemConfigs: mockGetSystemConfigs,
  updateSystemConfigs: mockUpdateSystemConfigs,
  testLiveChannel: mockTestLiveChannel,
}))

import SystemConfig from '../views/SystemConfig.vue'
import { getSystemConfigs, updateSystemConfigs, testLiveChannel } from '../api'

// 与 config.yaml / core/config.py 一致的真实字段
const mockConfig = {
  tdx_path: 'D:\\zd_zyb',
  iquant_path: 'D:\\iquant',
  max_concurrent_backtest: 1,
  database: { sqlite_path: 'data/dev.db' },
  iquant_bridge: {
    simulation: { base_url: 'http://127.0.0.1:8790' },
    live: { base_url: 'http://127.0.0.1:8791' },
  },
  ths_ui: {
    enabled: false,
    window_title: '网上股票交易系统5.0',
    client_exe_path: 'D:/同花顺远航版/transaction/xiadan.exe',
    driver: 'clipboard',
    captcha_ocr: false,
    execute_mode: 'file_builtin',
    execute_interval_seconds: 5,
    work_dir: 'data/ths',
  },
  tq_trade: {
    account: '88866666',
    account_type: 'stock',
    price_mode: 'my',
    client_path: 'D:\\zd_zyb',
    default_engine: 'tdx',
    cancel_unfilled_minutes: 3,
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(getSystemConfigs as any).mockResolvedValue(mockConfig)
  ;(updateSystemConfigs as any).mockResolvedValue(null)
  ;(testLiveChannel as any).mockResolvedValue({ ok: true, channel: 'ths_ui', positions: 3 })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SystemConfig.vue', () => {
  it('挂载后 GET /api/system/configs 并渲染各字段值', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(getSystemConfigs).toHaveBeenCalled()
    const inputs = w.findAll('input')
    const v = (el: any) => (el.element as HTMLInputElement).value
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('tdx')))).toBe('D:\\zd_zyb')
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('dev.db')))).toBe('data/dev.db')
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('7801')))).toBe('http://127.0.0.1:7801')
    expect(w.text()).toContain('最大并发回测')
    w.unmount()
  })

  it('渲染同花顺 UI 通道配置并可只读测试连接', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).toContain('同花顺 UI 下单通道')
    const inputs = w.findAll('input')
    const clientInput = inputs.find(i => i.attributes('placeholder')?.includes('xiadan.exe'))
    expect((clientInput!.element as HTMLInputElement).value).toBe('D:/同花顺远航版/transaction/xiadan.exe')

    await w.findAll('button').find(b => b.text().includes('测试连接（只读）'))!.trigger('click')
    await flushPromises()

    expect(testLiveChannel).toHaveBeenCalledWith('ths_ui')
    expect(w.text()).toContain('连接成功，读到 3 条持仓')
    w.unmount()
  })

  it('不渲染已废弃的 database.host / database.port 字段', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).not.toContain('数据库主机')
    expect(w.text()).not.toContain('数据库端口')
    w.unmount()
  })

  it('渲染 TQ 通道默认引擎下拉与超时撤单分钟数（A-6/A-7）', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).toContain('默认交易引擎')
    expect(w.text()).toContain('超时撤单(分钟)')
    // 默认引擎 select 选中 tdx
    const selects = w.findAll('select')
    const engineSelect = selects.find(s => s.findAll('option').some(o => o.element.value === 'tdx'))
    expect(engineSelect).toBeTruthy()
    expect((engineSelect!.element as HTMLSelectElement).value).toBe('tdx')
    // 超时撤单 number 输入回显 3
    const numInput = w.findAll('input').find(i => i.attributes('type') === 'number' && (i.element as HTMLInputElement).value === '3')
    expect(numInput).toBeTruthy()
    w.unmount()
  })

  it('渲染同花顺文件交接层三字段（执行模式/轮询间隔/工作目录）', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).toContain('执行模式')
    expect(w.text()).toContain('执行器轮询间隔')
    expect(w.text()).toContain('交接文件工作目录')
    // 执行模式 select 选中 file_builtin，含 direct 选项
    const selects = w.findAll('select')
    const modeSelect = selects.find(s => s.findAll('option').some(o => o.element.value === 'file_builtin'))
    expect(modeSelect).toBeTruthy()
    expect((modeSelect!.element as HTMLSelectElement).value).toBe('file_builtin')
    expect(modeSelect!.findAll('option').map(o => o.element.value)).toContain('direct')
    // 轮询间隔 number 回显 5；工作目录回显 data/ths
    const intervalInput = w.findAll('input').find(
      i => i.attributes('type') === 'number' && (i.element as HTMLInputElement).value === '5')
    expect(intervalInput).toBeTruthy()
    const workDirInput = w.findAll('input').find(i => (i.element as HTMLInputElement).value === 'data/ths')
    expect(workDirInput).toBeTruthy()
    // hint 说明 {work_dir}/{session_id}/ 目录结构
    expect(w.text()).toContain('{work_dir}/{session_id}/')
    w.unmount()
  })

  it('保存 → PUT 提交与 config.yaml 一致的完整配置并提示已保存', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    await w.findAll('button').find(b => b.text().includes('保存'))!.trigger('click')
    await flushPromises()

    expect(updateSystemConfigs).toHaveBeenCalledWith(expect.objectContaining({
      tdx_path: 'D:\\zd_zyb',
      iquant_path: 'D:\\iquant',
      max_concurrent_backtest: 1,
      database: expect.objectContaining({ sqlite_path: 'data/dev.db' }),
      iquant_bridge: expect.objectContaining({
        simulation: expect.objectContaining({ base_url: 'http://127.0.0.1:8790' }),
        live: expect.objectContaining({ base_url: 'http://127.0.0.1:8791' }),
      }),
      ths_ui: expect.objectContaining({
        enabled: false,
        window_title: '网上股票交易系统5.0',
        client_exe_path: 'D:/同花顺远航版/transaction/xiadan.exe',
        execute_mode: 'file_builtin',
        execute_interval_seconds: 5,
        work_dir: 'data/ths',
      }),
      tq_trade: expect.objectContaining({
        account: '88866666',
        default_engine: 'tdx',
        cancel_unfilled_minutes: 3,
      }),
    }))
    expect(w.text()).toContain('已保存')
    w.unmount()
  })

  it('保存失败 → 显示错误信息,不显示已保存', async () => {
    ;(updateSystemConfigs as any).mockRejectedValue(new Error('network down'))
    const w = mount(SystemConfig)
    await flushPromises()

    await w.findAll('button').find(b => b.text().includes('保存'))!.trigger('click')
    await flushPromises()

    expect(w.text()).not.toContain('已保存')
    expect(w.text()).toContain('保存失败')
    w.unmount()
  })

  it('GET 失败 → 显示加载失败信息,页面不崩', async () => {
    ;(getSystemConfigs as any).mockRejectedValue(new Error('连接被拒绝'))
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).toContain('加载配置失败')
    // 模板仍安全渲染(默认值),无抛错
    expect(w.findAll('input').length).toBeGreaterThan(0)
    w.unmount()
  })
})
