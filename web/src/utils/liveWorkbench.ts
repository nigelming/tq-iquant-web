/**
 * 实盘工作台纯函数 —— 订单/成交/持仓行构建 + 历史合并 + SSE 事件增量。
 *
 * 与 backend `core/api/live.py` 三个历史查询端点 + `/stream` SSE payload 对齐。
 * 纯函数便于单元测试;key 用负 id 表示历史行(稳定),SSE 事件行用正自增 id。
 */
import { nextEventId } from './liveEvents'
import type { LiveOrderItem, LiveTradeItem, LivePositionItem, DecisionEventItem } from '../api'

export interface OrderRow {
  key: number
  time: string
  stock_code: string
  trade_type: string
  status: string
  quantity: number
  price: number | null
  filled_quantity: number | null
  filled_price: number | null
  error_message: string | null
}

export interface TradeRow {
  key: number
  time: string
  stock_code: string
  trade_type: string
  price: number
  quantity: number
  amount: number
}

export interface PositionRow {
  stock_code: string
  quantity: number
  avg_cost: number
  market_value: number
  // 归属（组合策略/子策略）；同票多子策略持有时各一行
  portfolio_id: number | null
  strategy_id: number | null
}

/** 决策闸门事件行（调参可观测性）。 */
export interface DecisionRow {
  key: number
  time: string
  gate: string
  layer: string
  action: string
  stock_code: string
  strategy_id: number | null
  param_name: string | null
  param_value: number | null
  actual_value: number | null
  requested_qty: number | null
  final_qty: number | null
  message: string
}

type Ev = Record<string, unknown>

/** ISO datetime → HH:MM:SS（无则空串）。 */
const shortTime = (iso?: string | null): string =>
  iso && iso.length >= 19 ? iso.slice(11, 19) : ''

/** SSE order 事件 → 委托行；time 优先取外部日志时间,缺省回退 bar_time。 */
export function orderEventToRow(ev: Ev, liveTime = ''): OrderRow {
  return {
    key: nextEventId(),
    time: liveTime || shortTime(ev.bar_time as string | undefined),
    stock_code: (ev.stock_code as string) || '',
    trade_type: (ev.trade_type as string) || '',
    status: (ev.status as string) || 'submitted',
    quantity: (ev.quantity as number) ?? (ev.filled_quantity as number) ?? 0,
    price: (ev.price as number) ?? (ev.filled_price as number) ?? null,
    filled_quantity: (ev.filled_quantity as number) ?? null,
    filled_price: (ev.filled_price as number) ?? null,
    error_message: (ev.error_message as string) ?? null,
  }
}

/** 委托历史 → 行（key=-id，稳定标识历史单）。 */
export function orderHistoryToRows(items: LiveOrderItem[]): OrderRow[] {
  return items.map((i) => ({
    key: -i.id,
    time: shortTime(i.bar_time) || shortTime(i.created_at),
    stock_code: i.stock_code,
    trade_type: i.trade_type,
    status: i.status,
    quantity: i.quantity,
    price: i.price,
    filled_quantity: i.filled_quantity,
    filled_price: i.filled_price,
    error_message: i.error_message,
  }))
}

/** SSE trade 事件 → 成交行。 */
export function tradeEventToRow(ev: Ev, liveTime = ''): TradeRow {
  return {
    key: nextEventId(),
    time: liveTime,
    stock_code: (ev.stock_code as string) || '',
    trade_type: (ev.trade_type as string) || '',
    price: (ev.price as number) ?? 0,
    quantity: (ev.quantity as number) ?? 0,
    amount: (ev.amount as number) ?? 0,
  }
}

/** 成交历史 → 行（key=-id）。 */
export function tradeHistoryToRows(items: LiveTradeItem[]): TradeRow[] {
  return items.map((i) => ({
    key: -i.id,
    time: shortTime(i.trade_time),
    stock_code: i.stock_code,
    trade_type: i.trade_type,
    price: i.price,
    quantity: i.quantity,
    amount: i.amount,
  }))
}

/** 持仓行复合键：股票 + 组合 + 子策略（同票多子策略各占一行）。 */
export function positionRowKey(r: Pick<PositionRow, 'stock_code' | 'portfolio_id' | 'strategy_id'>): string {
  return `${r.stock_code}|${r.portfolio_id ?? null}|${r.strategy_id ?? null}`
}

/** SSE position 事件 → 持仓行（按 股票+组合+子策略 复合键原地 upsert，不重复）。 */
export function upsertPositionRows(rows: PositionRow[], ev: Ev): PositionRow[] {
  const row: PositionRow = {
    stock_code: (ev.stock_code as string) || '',
    quantity: (ev.quantity as number) ?? 0,
    avg_cost: (ev.avg_cost as number) ?? 0,
    market_value: (ev.market_value as number) ?? 0,
    portfolio_id: (ev.portfolio_id as number) ?? null,
    strategy_id: (ev.strategy_id as number) ?? null,
  }
  const idx = rows.findIndex((r) => positionRowKey(r) === positionRowKey(row))
  if (idx >= 0) {
    const next = [...rows]
    next[idx] = row
    return next
  }
  return [...rows, row]
}

/** 持仓历史 → 行（归属 id 缺省为 null）。 */
export function positionHistoryToRows(items: LivePositionItem[]): PositionRow[] {
  return items.map((i) => ({
    stock_code: i.stock_code,
    quantity: i.quantity,
    avg_cost: i.avg_cost,
    market_value: i.market_value,
    portfolio_id: i.portfolio_id ?? null,
    strategy_id: i.strategy_id ?? null,
  }))
}

/** SSE decision 事件 → 决策闸门行（key 正自增）。 */
export function decisionEventToRow(ev: Ev, liveTime = ''): DecisionRow {
  return {
    key: nextEventId(),
    time: liveTime || shortTime(ev.bar_time as string | undefined),
    gate: (ev.gate as string) || '',
    layer: (ev.layer as string) || '',
    action: (ev.action as string) || '',
    stock_code: (ev.stock_code as string) || '',
    strategy_id: (ev.strategy_id as number) ?? null,
    param_name: (ev.param_name as string) ?? null,
    param_value: (ev.param_value as number) ?? null,
    actual_value: (ev.actual_value as number) ?? null,
    requested_qty: (ev.requested_qty as number) ?? null,
    final_qty: (ev.final_qty as number) ?? null,
    message: (ev.message as string) || '',
  }
}

/** 决策历史 → 行（key=-id，稳定）；历史按 bar_time 升序返回，前端展示倒序（最新在上）。 */
export function decisionHistoryToRows(items: DecisionEventItem[]): DecisionRow[] {
  return items.map((i) => ({
    key: -(i.id ?? 0),
    time: shortTime(i.bar_time || undefined) || shortTime(i.created_at || undefined),
    gate: i.gate,
    layer: i.layer,
    action: i.action,
    stock_code: i.stock_code || '',
    strategy_id: i.strategy_id ?? null,
    param_name: i.param_name ?? null,
    param_value: i.param_value ?? null,
    actual_value: i.actual_value ?? null,
    requested_qty: i.requested_qty ?? null,
    final_qty: i.final_qty ?? null,
    message: i.message || '',
  })).reverse()
}

/** 头部插入 + 封顶（订单/成交/决策表）。 */
export function prependCapped<T>(list: T[], row: T, cap = 200): T[] {
  return [row, ...list].slice(0, cap)
}
