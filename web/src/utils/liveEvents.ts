/**
 * 实盘 SSE 事件格式化 —— 纯函数模块,便于单元测试。
 *
 * 与 backend `core/api/live.py` 的 `/sessions/{id}/stream` 事件 payload 对齐。
 * 事件类型:signal / order / trade / position / risk / decision / ping。
 */

export type LiveEventType =
  | 'signal' | 'order' | 'trade' | 'position' | 'risk' | 'decision' | 'ping'

export interface LiveEvent {
  id: number
  type: LiveEventType
  time: string
  text: string
  /** 连接关闭 / 会话不存在等非事件流标记,不进日志条 */
  meta?: 'stream-closed' | 'stream-open' | 'network-error'
}

export const EVENT_TYPE_LABEL: Record<string, string> = {
  signal: '信号',
  order: '委托',
  trade: '成交',
  position: '持仓',
  risk: '风控',
  decision: '拦截',
}

export const EVENT_TYPE_COLOR: Record<string, string> = {
  signal: 'blue',
  order: 'orange',
  trade: 'green',
  position: 'teal',
  risk: 'red',
  decision: 'purple',
}

// 决策闸门代码 → 中文（调参可观测性，对齐 backend decision.py gate 词汇表）
export const GATE_LABEL: Record<string, string> = {
  // 策略风控触发
  stop_loss: '止损',
  take_profit: '止盈',
  trailing_stop: '移动止损',
  // 组合熔断
  max_drawdown: '最大回撤熔断',
  daily_loss: '日内亏损暂停',
  risk_recover: '熔断恢复',
  halted_buy_strip: '熔断期剥买单',
  // 信号闸门
  halted_bar_no_price: '停牌/无价格',
  slave_master_block: '从策略主仓限制',
  open_already_holding: '已持仓不再开仓',
  max_positions_full: '持股数达上限',
  reduce_qty_too_small: '减仓量过小',
  add_threshold_not_met: '加仓阈值未达',
  add_count_exceeded: '加仓次数超限',
  missing_strategy_risk: '缺风控配置',
  // 资金闸门
  open_qty_too_small: '开仓资金不足1手',
  add_qty_too_small: '加仓资金不足1手',
  insufficient_funds: '资金不足拒单',
  order_shrunk: '资金不足缩量',
  // T+1
  t1_clamp: 'T+1可用钳量',
  t1_insufficient: 'T+1可用不足',
  // 实盘专属闸门
  after_close_block: '收盘后拦单',
  non_trading_block: '非交易时段拦单',
  dup_skip: '重复单跳过',
  inflight_skip: '在途单跳过',
  bridge_unavailable: '桥离线拒单',
  bridge_rejected: '桥业务拒单',
  approval_failed: '发单兜底失败',
}

// 闸门层 → 中文
export const LAYER_LABEL: Record<string, string> = {
  strategy_risk: '策略风控',
  portfolio_risk: '组合风控',
  signal_gate: '信号拦截',
  capital_gate: '资金拦截',
  t1: 'T+1',
  live_gate: '实盘闸门',
}

// 闸门动作 → 中文
export const ACTION_LABEL: Record<string, string> = {
  trigger: '触发',
  halt: '熔断',
  recover: '恢复',
  strip: '剥单',
  block: '拦截',
  reject: '拒单',
  shrink: '缩量',
  clamp: '钳量',
}

/** 闸门代码 → 中文标签（未知 gate 回退原代码）。 */
export function gateLabel(gate: string): string {
  return GATE_LABEL[gate] || gate
}

export const TRADE_TYPE_LABEL: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
}

export const ORDER_STATUS_LABEL: Record<string, string> = {
  submitted: '已提交',
  filled: '成交',
  partial: '部分成交',
  rejected: '拒绝',
  canceled: '已撤单',
}

const RISK_RULE_LABEL: Record<string, string> = {
  max_drawdown: '最大回撤熔断',
  daily_loss: '日内亏损熔断',
}

const fmt = (v: unknown): string => (v === null || v === undefined ? '' : String(v))

/** 委托:已提交/成交/部分成交/拒绝/撤单,读 trade_type + quantity/price 或 filled_*。 */
function formatOrder(data: Record<string, unknown>): string {
  const status = String(data.status || 'submitted')
  const stock = fmt(data.stock_code)
  const label = ORDER_STATUS_LABEL[status] || status
  if (status === 'rejected') {
    return `${label} ${stock} ${fmt(data.error_message)}`
  }
  if (status === 'submitted') {
    const t = TRADE_TYPE_LABEL[String(data.trade_type)] || fmt(data.trade_type)
    const price = data.price ? ` @ ${data.price}` : ''
    return `${t} ${stock} ${fmt(data.quantity)}股${price}`
  }
  // filled / partial
  return `${label} ${stock} ${fmt(data.filled_quantity)}股 @ ${fmt(data.filled_price)}`
}

/** 数值格式化：保留有效位，去掉多余尾零。 */
function fmtNum(v: unknown): string {
  if (v === null || v === undefined || v === '') return ''
  const n = Number(v)
  if (!Number.isFinite(n)) return fmt(v)
  // 比率类（0~1）转百分比更直观；其余保留至多 4 位有效小数。
  if (Math.abs(n) > 0 && Math.abs(n) < 1) return `${(n * 100).toFixed(2)}%`
  return String(Math.round(n * 10000) / 10000)
}

/**
 * 决策闸门事件 → 人类可读文本，例：
 * 「资金不足拒单 000001.SZ 需1000股/实发0股 阈值— 实际现金不足 — 开仓资金不足1手」
 * 调参时一眼看出：哪个闸门、哪只票、阈值 vs 实际、拦掉多少量、原因。
 */
function formatDecision(data: Record<string, unknown>): string {
  const gate = String(data.gate || '')
  const parts: string[] = [gateLabel(gate)]
  const stock = fmt(data.stock_code)
  if (stock) parts.push(stock)

  // 拦截量：请求 vs 实发（final=0 整单被拦；0<final<请求 缩量；final 缺省只报请求量）
  const req = data.requested_qty
  const fin = data.final_qty
  if (req !== null && req !== undefined && req !== '') {
    const finN = fin === null || fin === undefined || fin === '' ? null : Number(fin)
    if (finN === null) {
      parts.push(`${fmt(req)}股被拦`)
    } else if (finN === 0) {
      parts.push(`${fmt(req)}股被拦`)
    } else if (finN !== Number(req)) {
      parts.push(`需${fmt(req)}股/实发${fmt(fin)}股`)
    }
  }

  // 阈值 vs 实际（比率/数量）
  const pv = fmtNum(data.param_value)
  const av = fmtNum(data.actual_value)
  if (pv || av) {
    parts.push(`阈值${pv || '—'} 实际${av || '—'}`)
  }

  const msg = fmt(data.message)
  if (msg) parts.push(msg)
  return parts.join(' ')
}

/** SSE 事件 → 人类可读日志文本。ping 返回空串(心跳不进日志)。 */
export function formatEvent(type: string, data: Record<string, unknown>): string {
  switch (type) {
    case 'signal':
      return `[${fmt(data.signal_type)}] ${fmt(data.signal_name)} ${fmt(data.stock_code)}`
    case 'order':
      return formatOrder(data)
    case 'trade': {
      const t = TRADE_TYPE_LABEL[String(data.trade_type)] || fmt(data.trade_type)
      const amount = data.amount ? ` 金额${data.amount}` : ''
      return `${t} ${fmt(data.stock_code)} ${fmt(data.quantity)}股 @ ${fmt(data.price)}${amount}`
    }
    case 'position':
      return `${fmt(data.stock_code)} ${fmt(data.quantity)}股 成本${fmt(data.avg_cost)} 市值${fmt(data.market_value)}`
    case 'risk': {
      const label = RISK_RULE_LABEL[String(data.rule)] || fmt(data.rule)
      return `${label}: ${fmt(data.message)}`
    }
    case 'decision':
      return formatDecision(data)
    case 'ping':
      return ''
    default:
      return `${type}: ${JSON.stringify(data)}`
  }
}

let _id = 0

/** 生成全局递增日志条 id(SSE 事件本身无 id 字段,前端自增)。 */
export function nextEventId(): number {
  _id += 1
  return _id
}
