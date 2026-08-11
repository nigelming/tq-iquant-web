<script setup lang="ts">
import { ref, onMounted } from 'vue'
import axios from 'axios'

// 与 core/config.py _defaults() 对齐：回测字段 tdx_path、iQuant 字段 iquant_path、
// max_concurrent_backtest(回测并发上限)、database.sqlite_path(数据库路径)、
// iquant_bridge.base_url(实盘桥地址，绑 loopback 单用户，无 token)
const DEFAULTS = {
  tdx_path: '',
  iquant_path: '',
  max_concurrent_backtest: 1,
  database: { sqlite_path: '' },
  iquant_bridge: { base_url: '' },
}

const config = ref<any>(JSON.parse(JSON.stringify(DEFAULTS)))
const loading = ref(true)
const saved = ref(false)
const errorMsg = ref('')

onMounted(async () => {
  try {
    const res = await axios.get('/api/system/configs')
    const data = res.data?.data || {}
    config.value = {
      tdx_path: data.tdx_path ?? DEFAULTS.tdx_path,
      iquant_path: data.iquant_path ?? DEFAULTS.iquant_path,
      max_concurrent_backtest: data.max_concurrent_backtest ?? DEFAULTS.max_concurrent_backtest,
      database: { sqlite_path: data.database?.sqlite_path ?? DEFAULTS.database.sqlite_path },
      iquant_bridge: { base_url: data.iquant_bridge?.base_url ?? DEFAULTS.iquant_bridge.base_url },
    }
  } catch (e: any) {
    errorMsg.value = '加载配置失败: ' + (e?.message || e)
  } finally {
    loading.value = false
  }
})

async function save() {
  saved.value = false
  errorMsg.value = ''
  try {
    await axios.put('/api/system/configs', config.value)
    saved.value = true
    setTimeout(() => saved.value = false, 2000)
  } catch (e: any) {
    errorMsg.value = '保存失败: ' + (e?.message || e)
  }
}
</script>

<template>
  <div class="card" style="padding:24px;max-width:620px">
    <div v-if="loading" class="empty-state"><p>加载中…</p></div>

    <div v-else>
      <div v-if="errorMsg" class="card" style="padding:12px;color:#c0392b;margin-bottom:16px">
        {{ errorMsg }}
      </div>

      <h3 style="margin:0 0 4px">通达信</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#888">
        通达信客户端安装目录，回测行情 / 日线 / 周线数据的来源（通达信规范）
      </p>
      <div class="field">
        <label>通达信目录</label>
        <input v-model="config.tdx_path" placeholder="D:\new_tdx64" />
      </div>

      <h3 style="margin:20px 0 4px">iQuant</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#888">
        iQuant 客户端安装目录，实盘桥策略所在（HTTP 桥 127.0.0.1:8790）
      </p>
      <div class="field">
        <label>iQuant 目录</label>
        <input v-model="config.iquant_path" placeholder="D:\iquant" />
      </div>

      <h3 style="margin:20px 0 4px">实盘桥</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#888">
        iQuant 客户端内桥策略的 HTTP 地址，绑 loopback 单用户，无鉴权（token 已移除）
      </p>
      <div class="field">
        <label>桥地址</label>
        <input v-model="config.iquant_bridge.base_url" placeholder="http://127.0.0.1:8790" />
      </div>

      <h3 style="margin:20px 0 4px">回测</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#888">
        同一时刻最多允许运行的回测任务数，超出返回 HTTP 409
      </p>
      <div class="field">
        <label>最大并发回测</label>
        <input v-model.number="config.max_concurrent_backtest" type="number" min="1" style="max-width:180px" />
      </div>

      <h3 style="margin:20px 0 4px">数据库</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#888">
        SQLite 数据库文件路径（相对 main/ 目录解析，纯单用户本地工具）
      </p>
      <div class="field">
        <label>数据库文件路径</label>
        <input v-model="config.database.sqlite_path" placeholder="data/dev.db" />
      </div>

      <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
        <button @click="save" class="btn btn-primary">保存</button>
        <span v-if="saved" style="color:#065f46;font-size:13px">已保存</span>
      </div>
    </div>
  </div>
</template>
