<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  getTdxPools, getTdxPoolStocks, syncStockPool, getStockPools, deleteStockPool,
  type TdxPoolItem, type TdxPoolStockItem,
} from '../api'

const pools = ref<TdxPoolItem[]>([])
const localIdByCode = ref<Record<string, number>>({})  // 删除用：code → 本地池 id
const errorMsg = ref('')
const showStocks = ref(false)
const stocksList = ref<TdxPoolStockItem[]>([])
const stocksPoolName = ref('')

// 从 axios 错误里提取后端错误消息（统一响应 {code,message} 或 Pydantic 422 detail）
function errMsg(e: any): string {
  const d = e?.response?.data
  if (d?.message) return d.message
  if (Array.isArray(d?.detail)) return d.detail.map((x: { loc?: unknown[]; msg?: string }) => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ')
  if (typeof d?.detail === 'string') return d.detail
  return e?.message || '请求失败'
}

async function load() {
  errorMsg.value = ''
  try {
    // 并行拉通达信板块 + 本地已同步池（取 id 供删除）
    const [tdxRes, localRes] = await Promise.all([
      getTdxPools(),
      getStockPools().catch(() => []),  // 本地列表失败不阻塞
    ])
    pools.value = tdxRes
    localIdByCode.value = {}
    for (const p of localRes) {
      localIdByCode.value[p.code] = p.id
    }
  } catch (e) {
    // getTdxPools 失败（拦截器 reject 或 HTTP 错误）→ 提示，清空列表
    errorMsg.value = '加载失败：' + errMsg(e)
    pools.value = []
  }
}

async function viewStocks(p: TdxPoolItem) {
  try {
    stocksPoolName.value = p.name
    stocksList.value = await getTdxPoolStocks(p.code)
    showStocks.value = true
  } catch (e) {
    alert(`查看成分股失败：${errMsg(e)}`)
  }
}

async function syncPool(p: TdxPoolItem) {
  if (!confirm(`确认从通达信同步「${p.name}」的股票清单？将全量替换现有股票。`)) return
  try {
    await syncStockPool({ code: p.code })
  } catch (e) {
    alert(`同步失败：${errMsg(e)}`)
    return
  }
  load()
}

async function remove(p: TdxPoolItem) {
  const id = localIdByCode.value[p.code]
  if (!id) return
  if (!confirm(`确认删除股票池「${p.name}」？`)) return
  try {
    await deleteStockPool(id)
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
    <button @click="load" class="btn">刷新</button>
  </div>

  <div v-if="errorMsg" class="card" style="padding:12px;color:#c0392b;margin-bottom:12px">
    {{ errorMsg }}
  </div>

  <div class="card table-wrap">
    <table>
      <thead><tr><th>名称</th><th>代码</th><th>状态</th><th>股票数</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="p in pools" :key="p.code">
          <td>{{ p.name }}</td>
          <td style="color:#888">{{ p.code }}</td>
          <td>
            <span v-if="!p.exists_in_tdx" class="badge" style="background:#f0f0f0;color:#888">通达信已删除</span>
            <span v-else-if="p.synced" class="badge badge-green">已同步</span>
            <span v-else class="badge badge-gray">未同步</span>
          </td>
          <td>{{ p.synced ? p.stock_count + ' 只' : '-' }}</td>
          <td>
            <button v-if="p.exists_in_tdx" @click="viewStocks(p)" class="btn btn-sm">查看</button>
            <button v-if="p.exists_in_tdx" @click="syncPool(p)" class="btn btn-sm btn-primary" style="margin-left:6px">
              {{ p.synced ? '同步' : '同步' }}
            </button>
            <button v-if="p.synced" @click="remove(p)" class="btn btn-sm btn-danger" style="margin-left:6px">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="pools.length === 0 && !errorMsg" class="empty-state"><p>暂无股票池</p></div>
  </div>

  <!-- 成分股清单 Modal -->
  <div v-if="showStocks" class="modal-overlay modal-lg" @click.self="showStocks = false">
    <div class="modal-content">
      <h3>{{ stocksPoolName }} 成分股（{{ stocksList.length }}）</h3>
      <div class="card table-wrap" style="margin-bottom:12px">
        <table>
          <thead><tr><th>股票代码</th><th>名称</th></tr></thead>
          <tbody>
            <tr v-for="s in stocksList" :key="s.stock_code">
              <td>{{ s.stock_code }}</td>
              <td style="color:#888">{{ s.stock_name || '-' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="stocksList.length === 0" class="empty-state"><p>该板块暂无成分股</p></div>
      </div>
      <div class="modal-actions">
        <button @click="showStocks = false" class="btn">关闭</button>
      </div>
    </div>
  </div>
</template>
