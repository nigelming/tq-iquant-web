/**
 * 实盘 SSE 事件格式化 —— 纯函数模块,便于单元测试。
 *
 * 与 backend `core/api/live.py` 的 `/sessions/{id}/stream` 事件 payload 对齐。
 * 事件类型:signal / order / trade / position / risk / ping。
 */

export type LiveEventType = 'signal' | 'order' | 'trade' | 'position' | 'risk' | 'ping'

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
}

export const EVENT_TYPE_COLOR: Record<string, string> = {
  signal: 'blue',
  order: 'orange',
  trade: 'green',
  position: 'teal',
  risk: 'red',
}

const TRADE_TYPE_LABEL: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
}

const ORDER_STATUS_LABEL: Record<string, string> = {
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
