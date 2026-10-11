import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TablePager from '../components/TablePager.vue'

describe('TablePager', () => {
  it('渲染页码与计数', () => {
    const w = mount(TablePager, { props: { page: 2, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    expect(w.text()).toContain('第 2 / 5 页')
    expect(w.text()).toContain('共 95 条')
  })
  it('上一页/下一页 emit', async () => {
    const w = mount(TablePager, { props: { page: 2, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    await w.find('button.tp-prev').trigger('click')
    expect(w.emitted('update:page')![0]).toEqual([1])
    await w.find('button.tp-next').trigger('click')
    expect(w.emitted('update:page')![1]).toEqual([3])
  })
  it('切换每页条数 emit', async () => {
    const w = mount(TablePager, { props: { page: 1, pageSize: 20, totalPages: 5, displayedCount: 95, totalCount: 95 } })
    await w.find('select').setValue('50')
    expect(w.emitted('update:pageSize')![0]).toEqual([50])
  })
})