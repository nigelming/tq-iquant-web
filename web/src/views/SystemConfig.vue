<script setup lang="ts">
import { ref, onMounted } from 'vue'
import axios from 'axios'

const config = ref<any>({})
const saved = ref(false)

onMounted(async () => {
  const res = await axios.get('/api/system/configs')
  config.value = res.data.data
})

async function save() {
  await axios.put('/api/system/configs', config.value)
  saved.value = true
  setTimeout(() => saved.value = false, 2000)
}
</script>

<template>
  <div class="card" style="padding:24px;max-width:520px">
    <div class="field">
      <label>回测通达信目录</label>
      <input v-model="config.tdx_backtest_path" placeholder="D:\new_tdx64" />
    </div>
    <div class="field">
      <label>实盘通达信目录</label>
      <input v-model="config.tdx_live_path" placeholder="D:\new_tdx64_live" />
    </div>
    <div class="field">
      <label>iQuant 目录</label>
      <input v-model="config.iquant_path" placeholder="D:\iquant" />
    </div>
    <div class="field">
      <label>NATS 地址</label>
      <input v-model="config.nats.url" />
    </div>
    <div class="field">
      <label>数据库主机</label>
      <input v-model="config.database.host" />
    </div>
    <div class="field">
      <label>数据库端口</label>
      <input v-model.number="config.database.port" type="number" />
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <button @click="save" class="btn btn-primary">保存</button>
      <span v-if="saved" style="color:#065f46;font-size:13px">已保存</span>
    </div>
  </div>
</template>
