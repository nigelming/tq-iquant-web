<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getSystemConfigs, updateSystemConfigs, testLiveChannel, type SystemConfig } from '../api'
import PageHeader from '../components/PageHeader.vue'

// 与 core/config.py _defaults() 对齐：回测字段 tdx_path、QMT 字段 iquant_path、
// max_concurrent_backtest(回测并发上限)、database.sqlite_path(数据库路径)、
// iquant_bridge.simulation/live.base_url(两个模式位当前都指 8790；该账户为 QMT 仿真账户/仿真资金，8791 信用腿未启用)
// SystemConfig 接口定义见 api/index.ts（对齐 config.py _defaults()）
const DEFAULTS: SystemConfig = {
  tdx_path: 'D:\\zd_zyb',
  iquant_path: '',
  max_concurrent_backtest: 1,
  database: { sqlite_path: '' },
  iquant_bridge: {
    simulation: { base_url: '' },
    live: { base_url: '' },
  },
  ths_ui: {
    enabled: false,
    window_title: '网上股票交易系统5.0',
    client_exe_path: '',
    driver: 'clipboard',
    captcha_ocr: false,
    execute_mode: 'file_builtin',
    execute_interval_seconds: 5,
    work_dir: 'data/ths',
  },
  tq_trade: {
    account: '',
    account_type: 'stock',
    price_mode: 'my',
    client_path: '',
    default_engine: 'tdx',
    cancel_unfilled_minutes: 3,
  },
  goldminer: {
    base_url: 'http://127.0.0.1:7801',
    token: '',
    account_id: '',
  },
}

const config = ref<SystemConfig>(JSON.parse(JSON.stringify(DEFAULTS)))
const loading = ref(true)
const saved = ref(false)
const errorMsg = ref('')

// 左侧分组导航：分组多时一屏到底难定位，改为「导航 + 单分组面板」。
// 面板用 v-show 保持 DOM 常驻（输入态由 config 持有，切换不丢）。
const GROUPS = [
  { key: 'base', label: '基础环境' },
  { key: 'ths', label: '同花顺通道' },
  { key: 'tq', label: '通达信 TQ 通道' },
  { key: 'misc', label: '回测与数据' },
] as const
type GroupKey = (typeof GROUPS)[number]['key']
const activeGroup = ref<GroupKey>('base')

onMounted(async () => {
  try {
    const data = await getSystemConfigs()
    config.value = {
      tdx_path: data.tdx_path ?? DEFAULTS.tdx_path,
      iquant_path: data.iquant_path ?? DEFAULTS.iquant_path,
      max_concurrent_backtest: data.max_concurrent_backtest ?? DEFAULTS.max_concurrent_backtest,
      database: { sqlite_path: data.database?.sqlite_path ?? DEFAULTS.database.sqlite_path },
      iquant_bridge: {
        simulation: { base_url: data.iquant_bridge?.simulation?.base_url ?? DEFAULTS.iquant_bridge.simulation.base_url },
        live: { base_url: data.iquant_bridge?.live?.base_url ?? DEFAULTS.iquant_bridge.live.base_url },
      },
      goldminer: {
        base_url: data.goldminer?.base_url ?? DEFAULTS.goldminer!.base_url,
        token: data.goldminer?.token ?? DEFAULTS.goldminer!.token,
        account_id: data.goldminer?.account_id ?? DEFAULTS.goldminer!.account_id,
      },
      ths_ui: {
        enabled: data.ths_ui?.enabled ?? DEFAULTS.ths_ui.enabled,
        window_title: data.ths_ui?.window_title ?? DEFAULTS.ths_ui.window_title,
        client_exe_path: data.ths_ui?.client_exe_path ?? DEFAULTS.ths_ui.client_exe_path,
        driver: data.ths_ui?.driver ?? DEFAULTS.ths_ui.driver,
        captcha_ocr: data.ths_ui?.captcha_ocr ?? DEFAULTS.ths_ui.captcha_ocr,
        execute_mode: data.ths_ui?.execute_mode ?? DEFAULTS.ths_ui.execute_mode,
        execute_interval_seconds:
          data.ths_ui?.execute_interval_seconds ?? DEFAULTS.ths_ui.execute_interval_seconds,
        work_dir: data.ths_ui?.work_dir ?? DEFAULTS.ths_ui.work_dir,
      },
      tq_trade: {
        account: data.tq_trade?.account ?? DEFAULTS.tq_trade!.account,
        account_type: data.tq_trade?.account_type ?? DEFAULTS.tq_trade!.account_type,
        price_mode: data.tq_trade?.price_mode ?? DEFAULTS.tq_trade!.price_mode,
        client_path: data.tq_trade?.client_path ?? DEFAULTS.tq_trade!.client_path,
        default_engine: data.tq_trade?.default_engine ?? DEFAULTS.tq_trade!.default_engine,
        cancel_unfilled_minutes:
          data.tq_trade?.cancel_unfilled_minutes ?? DEFAULTS.tq_trade!.cancel_unfilled_minutes,
      },
    }
  } catch (e) {
    errorMsg.value = '加载配置失败: ' + (e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
})

async function save() {
  saved.value = false
  errorMsg.value = ''
  try {
    await updateSystemConfigs(config.value)
    saved.value = true
    setTimeout(() => saved.value = false, 2000)
  } catch (e) {
    errorMsg.value = '保存失败: ' + (e instanceof Error ? e.message : String(e))
  }
}

// 通道连通性探测（只读：连接窗口/账户 + 读一次持仓，不下单）。
// 后端读取已保存的 config.yaml，故修改路径/账号后需先「保存」再测试。
// 测试结果按通道分别持有，切换分组互不影响。
const testing = ref('')
const testResults = ref<Record<string, { ok: boolean; text: string }>>({})

async function testChannel(channel: 'ths_ui' | 'tdx_tq') {
  testing.value = channel
  try {
    const r = await testLiveChannel(channel)
    testResults.value[channel] = r.ok
      ? { ok: true, text: `连接成功，读到 ${r.positions ?? 0} 条持仓` }
      : { ok: false, text: r.error || '连接失败' }
  } catch (e) {
    testResults.value[channel] = { ok: false, text: e instanceof Error ? e.message : String(e) }
  } finally {
    testing.value = ''
  }
}

function testThs() { return testChannel('ths_ui') }
function testTdx() { return testChannel('tdx_tq') }
</script>

<template>
  <PageHeader title="系统配置" />
  <div class="card cfg-card">
    <div v-if="loading" class="empty-state"><p>加载中…</p></div>

    <div v-else>
      <div v-if="errorMsg" class="card" style="padding:12px;color:var(--red);margin-bottom:16px">
        {{ errorMsg }}
      </div>

      <div class="cfg-layout">
        <nav class="cfg-nav">
          <button
            v-for="g in GROUPS" :key="g.key"
            class="cfg-nav-item" :class="{ active: activeGroup === g.key }"
            @click="activeGroup = g.key"
          >{{ g.label }}</button>
        </nav>

        <div class="cfg-pane">
          <section v-show="activeGroup === 'base'">
            <h3 class="cfg-h">通达信</h3>
            <p class="cfg-desc">
              通达信客户端安装目录，回测行情 / 日线 / 周线数据的来源（通达信规范）
            </p>
            <div class="field">
              <label>通达信目录</label>
              <input v-model="config.tdx_path" placeholder="D:\new_tdx64" />
            </div>

            <h3 class="cfg-h">掘金量化桥（goldminer）</h3>
            <p class="cfg-desc">
              桥策略（goldminer_bridge.py）跑在掘金终端内，本地 HTTP 服务供平台下单/查询。
              下方 Token / 账户 ID 仅用于核对展示，<b>不会</b>传给桥；真正生效的是桥文件顶部的
              TOKEN / ACCOUNT / STRATEGY_ID 常量。保存后新建会话生效，运行中的会话需停止再启动。
            </p>
            <div class="field">
              <label>桥服务地址</label>
              <input v-model="config.goldminer.base_url" placeholder="http://127.0.0.1:7801" />
            </div>
            <div class="field">
              <label>掘金 Token</label>
              <input v-model="config.goldminer.token" placeholder="掘金终端「账户管理」中的 token" />
            </div>
            <div class="field">
              <label>账户 ID</label>
              <input v-model="config.goldminer.account_id" placeholder="仿真账户 ID（长线法宝）" />
            </div>
          </section>

          <section v-show="activeGroup === 'ths'">
            <h3 class="cfg-h">同花顺 UI 下单通道</h3>
            <p class="cfg-desc">
              同花顺通道是每会话可选引擎：在「新建实盘」选择同花顺便由独立交易客户端（xiadan.exe）
              执行下单，投资平台只生成信号。此开关仅影响 engine 为空的历史会话在 default_engine
              未配置/非法时的回退判定（启用→ths，否则→iquant），<b>不会</b>强制切换或禁用已有会话；
              切换引擎需停止再启动会话。
            </p>
            <div class="field">
              <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                <input v-model="config.ths_ui.enabled" type="checkbox" style="width:auto" />
                启用同花顺通道回退（仅影响 engine 为空的历史会话，不代表启用/禁用该引擎）
              </label>
            </div>
            <div class="field">
              <label>交易客户端路径</label>
              <input v-model="config.ths_ui.client_exe_path" placeholder="D:/同花顺远航版/transaction/xiadan.exe" />
            </div>
            <div class="field">
              <label>窗口标题</label>
              <input v-model="config.ths_ui.window_title" placeholder="网上股票交易系统5.0" />
            </div>
            <div class="field">
              <label>表格读取方式</label>
              <input v-model="config.ths_ui.driver" placeholder="clipboard" />
            </div>
            <div class="field">
              <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                <input v-model="config.ths_ui.captcha_ocr" type="checkbox" style="width:auto" />
                启用验证码 OCR（使用 Windows 内置 OCR，默认关闭）
              </label>
            </div>
            <div class="field">
              <label>执行模式</label>
              <select v-model="config.ths_ui.execute_mode">
                <option value="file_builtin">文件内置执行器（信号/指令落 JSONL，后台按频率执行）</option>
                <option value="direct">直接执行（同步 UI 自动化）</option>
              </select>
              <p class="cfg-hint">
                file_builtin：place_order 写入指令文件秒回，内置执行器按轮询间隔执行键鼠自动化；
                成交/持仓仍走 UI 读回回填。撤单已自动化（F3 撤单页双击委托行）；
                读后游标即推进（at-most-once），执行前崩溃的单不重复执行，超时走 expire rejected。
              </p>
            </div>
            <div class="field">
              <label>执行器轮询间隔(秒)</label>
              <input
                v-model.number="config.ths_ui.execute_interval_seconds"
                type="number" min="1" style="max-width:180px"
              />
              <p class="cfg-hint">内置执行器每 N 秒读一次指令文件（默认 5 秒）</p>
            </div>
            <div class="field">
              <label>交接文件工作目录</label>
              <input v-model="config.ths_ui.work_dir" placeholder="data/ths" />
              <p class="cfg-hint">
                相对服务进程目录解析（默认 main/data/ths），每个 session 一个子目录
                {work_dir}/{session_id}/：signals.jsonl（信号）/ risk.jsonl（风控决策）/
                orders.jsonl（下单指令）/ results.jsonl（执行回执）/ cursor.json（消费游标）
              </p>
            </div>
            <div class="cfg-test">
              <button @click="testThs" class="btn btn-sm" :disabled="testing !== ''">
                {{ testing === 'ths_ui' ? '测试中…' : '测试连接（只读）' }}
              </button>
              <span v-if="testResults.ths_ui" :style="{ color: testResults.ths_ui.ok ? 'var(--green)' : 'var(--red)', fontSize: '13px' }">
                {{ testResults.ths_ui.text }}
              </span>
            </div>
          </section>

          <section v-show="activeGroup === 'tq'">
            <h3 class="cfg-h">通达信 TQ 交易通道</h3>
            <p class="cfg-desc">
              走通达信客户端内置的 TQ 交易接口（tqcenter），与回测取数同一条连接。
              前置条件：通达信客户端已启动并登录资金账号，PYPlugins 下存在 tqcenter.py。
              下单确认面板默认自动处置（config.tq_trade.auto_confirm_dialog，默认 true）；
              若关闭该自动处置，则需在客户端关闭「下单确认」，否则会被判定为半自动模式拒单。
            </p>
            <div class="field">
              <label>资金账号</label>
              <input v-model="config.tq_trade!.account" placeholder="留空则用客户端当前登录账号" />
            </div>
            <div class="field">
              <label>账号类型</label>
              <input v-model="config.tq_trade!.account_type" placeholder="stock" />
            </div>
            <div class="field">
              <label>下单价格模式</label>
              <select v-model="config.tq_trade!.price_mode">
                <option value="my">自填价（策略价，默认）</option>
                <option value="latest">最新价</option>
                <option value="ztj">涨停价</option>
                <option value="dtj">跌停价</option>
              </select>
            </div>
            <div class="field">
              <label>TQ 插件目录（留空用上方通达信目录）</label>
              <input v-model="config.tq_trade!.client_path" placeholder="D:\zd_zyb" />
            </div>
            <div class="field">
              <label>默认交易引擎</label>
              <select v-model="config.tq_trade!.default_engine">
                <option value="tdx">通达信 TQ（tdx）</option>
                <option value="goldminer">掘金量化 HTTP 桥（goldminer）</option>
                <option value="ths">同花顺 UI 自动化（ths）</option>
              </select>
              <p class="cfg-hint">engine 为空的历史会话/旧 qmt(miniQMT) 会话回退使用；未配置或非法时回退 ths_ui.enabled 且依赖可用→ths，否则 iquant</p>
            </div>
            <div class="field">
              <label>超时撤单(分钟)</label>
              <input
                v-model.number="config.tq_trade!.cancel_unfilled_minutes"
                type="number" min="0" style="max-width:180px"
              />
              <p class="cfg-hint">&gt;0：在途单超时未成交自动撤单并释放同向闸；0=禁用</p>
            </div>
            <div class="cfg-test">
              <button @click="testTdx" class="btn btn-sm" :disabled="testing !== ''">
                {{ testing === 'tdx_tq' ? '测试中…' : '测试连接（只读）' }}
              </button>
              <span v-if="testResults.tdx_tq" :style="{ color: testResults.tdx_tq.ok ? 'var(--green)' : 'var(--red)', fontSize: '13px' }">
                {{ testResults.tdx_tq.text }}
              </span>
            </div>
          </section>

          <section v-show="activeGroup === 'misc'">
            <h3 class="cfg-h">回测</h3>
            <p class="cfg-desc">
              同一时刻最多允许运行的回测任务数，超出返回 HTTP 409
            </p>
            <div class="field">
              <label>最大并发回测</label>
              <input v-model.number="config.max_concurrent_backtest" type="number" min="1" style="max-width:180px" />
            </div>

            <h3 class="cfg-h">数据库</h3>
            <p class="cfg-desc">
              SQLite 数据库文件路径（相对 main/ 目录解析，纯单用户本地工具）
            </p>
            <div class="field">
              <label>数据库文件路径</label>
              <input v-model="config.database.sqlite_path" placeholder="data/dev.db" />
            </div>
          </section>

          <div class="cfg-foot">
            <button @click="save" class="btn btn-primary">保存</button>
            <span v-if="saved" style="color:var(--green);font-size:13px">已保存</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cfg-card { padding: 24px; max-width: 900px; }
.cfg-layout { display: flex; gap: 20px; align-items: flex-start; }
.cfg-nav {
  width: 150px;
  flex: none;
  display: grid;
  gap: 4px;
  position: sticky;
  top: 12px;
}
.cfg-nav-item {
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--ink-soft);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.cfg-nav-item:hover { background: var(--hover-bg); }
.cfg-nav-item.active {
  background: var(--accent-light);
  color: var(--accent);
  font-weight: 600;
  box-shadow: inset 2px 0 0 var(--accent);
}
.cfg-pane { flex: 1; min-width: 0; }
.cfg-h { margin: 18px 0 4px; font-size: 14px; }
.cfg-h:first-child { margin-top: 0; }
.cfg-desc { margin: 0 0 12px; font-size: 12px; color: var(--ink-soft); }
.cfg-hint { margin: 4px 0 0; font-size: 12px; color: var(--ink-soft); }
.cfg-test { display: flex; align-items: center; gap: 10px; margin: 4px 0 8px; }
.cfg-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
@media (max-width: 720px) {
  .cfg-layout { flex-direction: column; }
  .cfg-nav { width: 100%; display: flex; flex-wrap: wrap; position: static; }
}
</style>
