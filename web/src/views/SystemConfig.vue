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
  <div class="page">
    <h2>系统配置</h2>
    <div class="config-form">
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
      <button @click="save" class="btn">保存</button>
      <span v-if="saved" class="saved">已保存</span>
    </div>
  </div>
</template>

<style scoped>
.config-form { max-width: 500px; }
.field { margin-bottom: 1rem; }
.field label { display: block; font-weight: 600; margin-bottom: 0.3rem; color: #555; }
.field input { width: 100%; padding: 0.5rem; border: 1px solid #ddd; border-radius: 4px; }
.btn { padding: 0.5rem 1.5rem; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: #fff; }
.saved { margin-left: 0.5rem; color: #4caf50; }
</style>
