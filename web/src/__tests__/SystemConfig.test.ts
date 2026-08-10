import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// SystemConfig.vue 用原生 axios(GET/PUT),mock 掉避免真实请求
const { axiosGet, axiosPut } = vi.hoisted(() => ({
  axiosGet: vi.fn(),
  axiosPut: vi.fn(),
}))
vi.mock('axios', () => ({
  default: { get: axiosGet, put: axiosPut },
}))

import SystemConfig from '../views/SystemConfig.vue'

// 与 config.yaml / core/config.py 一致的真实字段
const mockConfig = {
  tdx_path: 'D:\\new_tdx64',
  iquant_path: 'D:\\iquant',
  max_concurrent_backtest: 1,
  database: { sqlite_path: 'data/dev.db' },
}

beforeEach(() => {
  vi.clearAllMocks()
  axiosGet.mockResolvedValue({ data: { code: 0, data: mockConfig } })
  axiosPut.mockResolvedValue({ data: { code: 0 } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SystemConfig.vue', () => {
  it('挂载后 GET /api/system/configs 并渲染各字段值', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(axiosGet).toHaveBeenCalledWith('/api/system/configs')
    const inputs = w.findAll('input')
    const v = (el: any) => (el.element as HTMLInputElement).value
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('tdx')))).toBe('D:\\new_tdx64')
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('iquant')))).toBe('D:\\iquant')
    expect(v(inputs.find(i => i.attributes('placeholder')?.includes('dev.db')))).toBe('data/dev.db')
    expect(w.text()).toContain('最大并发回测')
    w.unmount()
  })

  it('不渲染已废弃的 database.host / database.port 字段', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).not.toContain('数据库主机')
    expect(w.text()).not.toContain('数据库端口')
    w.unmount()
  })

  it('保存 → PUT 提交与 config.yaml 一致的完整配置并提示已保存', async () => {
    const w = mount(SystemConfig)
    await flushPromises()

    await w.findAll('button').find(b => b.text().includes('保存'))!.trigger('click')
    await flushPromises()

    expect(axiosPut).toHaveBeenCalledWith('/api/system/configs', expect.objectContaining({
      tdx_path: 'D:\\new_tdx64',
      iquant_path: 'D:\\iquant',
      max_concurrent_backtest: 1,
      database: expect.objectContaining({ sqlite_path: 'data/dev.db' }),
    }))
    expect(w.text()).toContain('已保存')
    w.unmount()
  })

  it('保存失败 → 显示错误信息,不显示已保存', async () => {
    axiosPut.mockRejectedValue(new Error('network down'))
    const w = mount(SystemConfig)
    await flushPromises()

    await w.findAll('button').find(b => b.text().includes('保存'))!.trigger('click')
    await flushPromises()

    expect(w.text()).not.toContain('已保存')
    expect(w.text()).toContain('保存失败')
    w.unmount()
  })

  it('GET 失败 → 显示加载失败信息,页面不崩', async () => {
    axiosGet.mockRejectedValue(new Error('连接被拒绝'))
    const w = mount(SystemConfig)
    await flushPromises()

    expect(w.text()).toContain('加载配置失败')
    // 模板仍安全渲染(默认值),无抛错
    expect(w.findAll('input').length).toBeGreaterThan(0)
    w.unmount()
  })
})
