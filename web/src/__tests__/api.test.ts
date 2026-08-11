import { describe, it, expect, vi } from 'vitest'
import { api } from '../api'

// #14：响应拦截器测试 — 直接用真实 api 实例，桩掉 adapter 返回可控响应。
// 拦截器在 api/index.ts 创建实例时已注册，此处验证其行为：
//   - HTTP 200 + body.code === 0 → 正常 resolve（返回完整 response）
//   - HTTP 200 + body.code !== 0 → reject（带 response.data，供 errMsg 读 message）
//   - HTTP 200 + 无 code 字段（非统一格式）→ 透传 resolve，不误拦
//   - HTTP 404/500 → axios 自身 reject，拦截器不动（errMsg 已处理 response.data.detail）

function stubAdapter(data: any, status = 200) {
  return vi.fn(async () => ({
    data,
    status,
    statusText: status === 200 ? 'OK' : 'ERR',
    headers: {},
    config: {} as any,
  }))
}

describe('api 响应拦截器 (#14)', () => {
  it('code === 0 → resolve，返回完整 response（调用方读 res.data.data）', async () => {
    ;(api as any).defaults.adapter = stubAdapter({ code: 0, message: 'ok', data: { id: 1 } })
    const res = await api.get('/x')
    expect(res.data.code).toBe(0)
    expect(res.data.data).toEqual({ id: 1 })
  })

  it('code !== 0 → reject 一个带 response 的错误（errMsg 能读 message）', async () => {
    ;(api as any).defaults.adapter = stubAdapter({ code: 400, message: '组合不存在' })
    await expect(api.get('/x')).rejects.toThrow('组合不存在')
    try {
      await api.get('/x')
    } catch (e: any) {
      // reject 的 error 带 response.data，errMsg 的 e?.response?.data?.message 路径生效
      expect(e.response?.data?.code).toBe(400)
      expect(e.response?.data?.message).toBe('组合不存在')
    }
  })

  it('无 code 字段（非统一格式，如 {"ok":true}）→ 透传 resolve，不误拦', async () => {
    ;(api as any).defaults.adapter = stubAdapter({ ok: true })
    const res = await api.get('/x')
    expect(res.data.ok).toBe(true)
  })

  it('HTTP 500 → axios reject（拦截器不干预，错误体是服务端返回）', async () => {
    // #13 后端未捕获异常 → {code:500,message,data:null} + HTTP 500
    ;(api as any).defaults.adapter = stubAdapter({ code: 500, message: '服务器内部错误', data: null }, 500)
    await expect(api.get('/x')).rejects.toMatchObject({ response: { status: 500 } })
  })
})
