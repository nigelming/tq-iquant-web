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
  // 后端 live_order.id（SSE order 事件与历史行共用）；用于同一笔委托的
  // 多次状态事件原地 upsert，避免「已提交/已成交/已撤单」各占一行。
  order_id?: number | null
  time: string
  /** 事件时间 epoch（ms）：筛选区间用；旧数据/无时区 → null（降级显示、不筛掉）。 */
  ts?: number | null
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
  ts?: number | null
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
  // 归属（组合策略/子策略）；同票多子策略持有时各一行；外部持仓（原有持仓）为 null
  portfolio_id: number | null
  strategy_id: number | null
  // 来源：strategy=策略持仓 / external=账户原有持仓（非策略买入，仅供监视）
  source: string
  // 归属：pool=在组合股票池内 / fallback=不在池内、按兜底规则挂到首个组合；
  // 缺省（旧接口/SSE 事件未带）时不显示徽标，按 pool 降级
  attribution?: 'pool' | 'fallback'
}

/** 决策闸门事件行（调参可观测性）。 */
export interface DecisionRow {
  key: number
  time: string
  ts?: number | null
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

/** ISO datetime → HH:MM:SS（无则空串）。仅用于无时区旧数据的降级显示。 */
const shortTime = (iso?: string | null): string =>
  iso && iso.length >= 19 ? iso.slice(11, 19) : ''

// 时间口径统一为 Asia/Shanghai：后端已给 created_at 标 +00:00、bar_time/trade_time
// 标 +08:00，前端按显式时区换算成北京时钟；naive 旧数据不猜时区、原样降级显示。
const BJ_CLOCK = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit',
  second: '2-digit', hourCycle: 'h23',
})
const BJ_DAY = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})

/** 当前北京时钟 HH:MM:SS（SSE 实时事件用，与历史行同一口径）。 */
export function beijingNowHms(): string {
  return BJ_CLOCK.format(new Date())
}

/** 解析 ISO 时间（需带时区）→ 北京时钟文本 + epoch(ms)。非法/naive → 降级。 */
export function parseBeijingTime(iso?: string | null): { ts: number | null; text: string } {
  if (!iso) return { ts: null, text: '' }
  const norm = iso.includes('T') ? iso : iso.replace(' ', 'T')
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(norm)) {
    // 无时区旧数据：不猜（无法判断是 UTC 还是上海墙钟），原样显示且不参与筛选
    return { ts: null, text: shortTime(norm) }
  }
  const ts = Date.parse(norm)
  if (Number.isNaN(ts)) return { ts: null, text: shortTime(norm) }
  return { ts, text: BJ_CLOCK.format(new Date(ts)) }
}

/** epoch(ms) → 北京 ISO（YYYY-MM-DDTHH:mm:ss，聚合表首次/末次列用）。 */
export function beijingIso(ts: number): string {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(ts))
  const v = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return `${v('year')}-${v('month')}-${v('day')}T${v('hour')}:${v('minute')}:${v('second')}`
}

// ---- 时间区间筛选（今日 / 近3日 / 近7日 / 全部，按北京自然日）----

export type TimeRange = 'today' | '3d' | '7d' | 'all'

export const TIME_RANGE_OPTIONS: Array<{ value: TimeRange; label: string }> = [
  { value: 'today', label: '今日' },
  { value: '3d', label: '近3日' },
  { value: '7d', label: '近7日' },
  { value: 'all', label: '全部' },
]

export const TIME_RANGE_LABEL: Record<TimeRange, string> = {
  today: '今日', '3d': '近3日', '7d': '近7日', all: '全部',
}

/** 北京自然日键 YYYY-MM-DD。 */
export function beijingDayKey(ts: number): string {
  const parts = BJ_DAY.formatToParts(new Date(ts))
  const v = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return `${v('year')}-${v('month')}-${v('day')}`
}

/** 区间起点 epoch：今日=北京当日 00:00；近N日=含今日往前 N-1 天的 00:00；全部=null。 */
export function rangeStartTs(range: TimeRange, nowTs: number = Date.now()): number | null {
  if (range === 'all') return null
  const days = range === 'today' ? 1 : range === '3d' ? 3 : 7
  const key = beijingDayKey(nowTs - (days - 1) * 86400000)
  const [y, m, d] = key.split('-').map(Number)
  // 中国无夏令时：北京当日 00:00 = 前一日 UTC 16:00
  return Date.UTC(y, m - 1, d) - 8 * 3600 * 1000
}

/** 按区间过滤；ts 缺失/不可解析（旧数据）不筛掉，保证能降级看到。 */
export function filterRowsByRange<T extends { ts?: number | null }>(
  rows: T[], range: TimeRange, nowTs: number = Date.now(),
): T[] {
  const start = rangeStartTs(range, nowTs)
  if (start === null) return rows
  return rows.filter((r) => (r.ts == null ? true : r.ts >= start))
}

/** 聚合统计行（口径对齐后端 summarize_decisions，但按当前筛选区间重算）。 */
export interface DecisionSummaryRow {
  gate: string
  layer: string
  action: string
  param_name: string | null
  param_value: number | null
  count: number
  first_bar_time: string | null
  last_bar_time: string | null
  stock_count: number
  requested_qty_sum: number | null
  final_qty_sum: number | null
  /** 成交笔数：本组股票命中的 filled/partial 委托，按 order_id 去重（重复触发不重复计）。
   *  未传委托行（旧调用/旧数据）→ null，前端降级显示「—」。 */
  filled_count?: number | null
  /** 转化率 = 成交笔数 / 触发次数；无触发或未统计 → null。 */
  conversion_rate?: number | null
}

/**
 * 按 (gate, param_name) 聚合当前筛选后的决策行；首次/末次用事件时间（北京）。
 *
 * 成交转化（传入当前筛选区间的委托行时计算）：
 * - 触发次数 = 决策事件条数（同一订单重复触发算多次触发）
 * - 成交笔数 = 本组股票代码命中的、终态 filled/partial 的委托，按 order_id 去重
 *   （同一订单的多次触发/多次状态事件只算 1 笔）
 * - 转化率 = 成交笔数 / 触发次数
 * 未传 orders（旧调用）→ 两列为 null，前端降级显示「—」。
 */
export function summarizeDecisionRows(
  rows: DecisionRow[], orders?: OrderRow[],
): DecisionSummaryRow[] {
  const groups = new Map<string, DecisionSummaryRow & {
    _stocks: Set<string>; _minTs: number | null; _maxTs: number | null
    _hasReq: boolean; _hasFin: boolean
  }>()
  for (const r of rows) {
    const key = `${r.gate}|${r.param_name ?? ''}`
    let g = groups.get(key)
    if (!g) {
      g = {
        gate: r.gate, layer: r.layer, action: r.action, param_name: r.param_name,
        param_value: r.param_value, count: 0, first_bar_time: null,
        last_bar_time: null, stock_count: 0, requested_qty_sum: null,
        final_qty_sum: null, _stocks: new Set(), _minTs: null, _maxTs: null,
        _hasReq: false, _hasFin: false,
      }
      groups.set(key, g)
    }
    g.count += 1
    if (g.param_value == null) g.param_value = r.param_value
    if (r.ts != null) {
      if (g._minTs == null || r.ts < g._minTs) g._minTs = r.ts
      if (g._maxTs == null || r.ts > g._maxTs) g._maxTs = r.ts
    }
    if (r.stock_code) g._stocks.add(r.stock_code)
    if (r.requested_qty != null) {
      g.requested_qty_sum = (g.requested_qty_sum ?? 0) + r.requested_qty
      g._hasReq = true
    }
    if (r.final_qty != null) {
      g.final_qty_sum = (g.final_qty_sum ?? 0) + r.final_qty
      g._hasFin = true
    }
  }
  // 成交笔数：按组内股票代码匹配终态 filled/partial 委托，order_id 去重。
  // 同一订单被多次触发（多次决策事件）只算 1 笔成交；未传 orders → null（旧数据降级）。
  const fillsByKey = new Map<string, number>()
  if (orders !== undefined) {
    const filled = orders.filter((o) => o.status === 'filled' || o.status === 'partial')
    for (const [key, g] of groups) {
      const ids = new Set<string>()
      for (const o of filled) {
        if (!o.stock_code || !g._stocks.has(o.stock_code)) continue
        ids.add(o.order_id != null ? `oid:${o.order_id}` : `row:${o.key}`)
      }
      fillsByKey.set(key, ids.size)
    }
  }
  const out: DecisionSummaryRow[] = []
  for (const [key, g] of groups) {
    const filledCount = orders === undefined ? null : (fillsByKey.get(key) ?? 0)
    out.push({
      gate: g.gate, layer: g.layer, action: g.action, param_name: g.param_name,
      param_value: g.param_value, count: g.count,
      first_bar_time: g._minTs == null ? null : beijingIso(g._minTs),
      last_bar_time: g._maxTs == null ? null : beijingIso(g._maxTs),
      stock_count: g._stocks.size,
      requested_qty_sum: g._hasReq ? g.requested_qty_sum : null,
      final_qty_sum: g._hasFin ? g.final_qty_sum : null,
      filled_count: filledCount,
      conversion_rate: filledCount == null || g.count <= 0 ? null : filledCount / g.count,
    })
  }
  return out.sort((a, b) => (b.count - a.count)
    || String(a.gate).localeCompare(String(b.gate))
    || String(a.param_name ?? '').localeCompare(String(b.param_name ?? '')))
}

/** SSE order 事件 → 委托行；time 优先取外部日志时间,缺省回退 bar_time。 */
export function orderEventToRow(ev: Ev, liveTime = ''): OrderRow {
  const oid = (ev.order_id as number) ?? null
  const t = parseBeijingTime(ev.bar_time as string | undefined)
  return {
    key: oid !== null ? oid : nextEventId(),
    order_id: oid,
    time: liveTime || t.text,
    ts: liveTime ? Date.now() : t.ts,
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
  return items.map((i) => {
    const bt = parseBeijingTime(i.bar_time)
    const ct = parseBeijingTime(i.created_at)
    return {
      key: -i.id,
      order_id: i.id,
      time: bt.text || ct.text,
      ts: bt.ts ?? ct.ts,
      stock_code: i.stock_code,
      trade_type: i.trade_type,
      status: i.status,
      quantity: i.quantity,
      price: i.price,
      filled_quantity: i.filled_quantity,
      filled_price: i.filled_price,
      error_message: i.error_message,
    }
  })
}

/** SSE trade 事件 → 成交行。 */
export function tradeEventToRow(ev: Ev, liveTime = ''): TradeRow {
  return {
    key: nextEventId(),
    time: liveTime,
    ts: liveTime ? Date.now() : null,
    stock_code: (ev.stock_code as string) || '',
    trade_type: (ev.trade_type as string) || '',
    price: (ev.price as number) ?? 0,
    quantity: (ev.quantity as number) ?? 0,
    amount: (ev.amount as number) ?? 0,
  }
}

/** 成交历史 → 行（key=-id）。 */
export function tradeHistoryToRows(items: LiveTradeItem[]): TradeRow[] {
  return items.map((i) => {
    const t = parseBeijingTime(i.trade_time)
    return {
      key: -i.id,
      time: t.text,
      ts: t.ts,
      stock_code: i.stock_code,
      trade_type: i.trade_type,
      price: i.price,
      quantity: i.quantity,
      amount: i.amount,
    }
  })
}

/** 持仓行复合键：策略行为 股票+组合+子策略（同票多子策略各占一行）；
 *  外部行（组合/子策略均为 null 的原有持仓）用股票代码作键。 */
export function positionRowKey(r: Pick<PositionRow, 'stock_code' | 'portfolio_id' | 'strategy_id'>): string {
  if ((r.portfolio_id ?? null) === null && (r.strategy_id ?? null) === null) {
    return `ext|${r.stock_code}`
  }
  return `${r.stock_code}|${r.portfolio_id ?? null}|${r.strategy_id ?? null}`
}

/** SSE position 事件 → 持仓行（按 股票+组合+子策略 复合键原地 upsert，不重复）。
 *  清仓（quantity<=0）→ 移除该行（与历史口径 quantity!=0 一致，不留幽灵持仓行）。
 *  payload 无 source（SSE 仅推策略持仓）→ 默认 strategy。 */
export function upsertPositionRows(rows: PositionRow[], ev: Ev): PositionRow[] {
  const row: PositionRow = {
    stock_code: (ev.stock_code as string) || '',
    quantity: (ev.quantity as number) ?? 0,
    avg_cost: (ev.avg_cost as number) ?? 0,
    market_value: (ev.market_value as number) ?? 0,
    portfolio_id: (ev.portfolio_id as number) ?? null,
    strategy_id: (ev.strategy_id as number) ?? null,
    source: (ev.source as string) || 'strategy',
    attribution: (ev.attribution as PositionRow['attribution']) || undefined,
  }
  const idx = rows.findIndex((r) => positionRowKey(r) === positionRowKey(row))
  // SSE position 事件不携带归属 → 保留已有行的标注，避免徽标闪没
  if (!row.attribution && idx >= 0) row.attribution = rows[idx].attribution
  if (row.quantity <= 0) {
    if (idx < 0) return rows
    const next = [...rows]
    next.splice(idx, 1)
    return next
  }
  if (idx >= 0) {
    const next = [...rows]
    next[idx] = row
    return next
  }
  return [...rows, row]
}

/** 持仓历史 → 行（归属 id 缺省为 null；source 缺省 strategy，外部行透传 external）。 */
export function positionHistoryToRows(items: LivePositionItem[]): PositionRow[] {
  return items.map((i) => ({
    stock_code: i.stock_code,
    quantity: i.quantity,
    avg_cost: i.avg_cost,
    market_value: i.market_value,
    portfolio_id: i.portfolio_id ?? null,
    strategy_id: i.strategy_id ?? null,
    source: i.source || 'strategy',
    attribution: i.attribution,
  }))
}

/** SSE decision 事件 → 决策闸门行（key 正自增）。 */
export function decisionEventToRow(ev: Ev, liveTime = ''): DecisionRow {
  const t = parseBeijingTime(ev.bar_time as string | undefined)
  return {
    key: nextEventId(),
    time: liveTime || t.text,
    ts: liveTime ? Date.now() : t.ts,
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

/** 决策历史 → 行（key=-id，稳定）；历史按 bar_time 升序返回，前端展示倒序（最新在上）。
 * 时间列用 created_at（事件落库时刻）——日线事件 bar_time 恒 00:00:00，无法区分
 * 何时触发（2026-11-03 止损 defer 57 次无法排重问题）；bar_time 兜底。 */
export function decisionHistoryToRows(items: DecisionEventItem[]): DecisionRow[] {
  return items.map((i) => {
    const ct = parseBeijingTime(i.created_at || undefined)
    const bt = parseBeijingTime(i.bar_time || undefined)
    return {
      key: -(i.id ?? 0),
      time: ct.text || bt.text,
      ts: ct.ts ?? bt.ts,
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
    }
  }).reverse()
}

/** 头部插入 + 封顶（订单/成交/决策表）。 */
export function prependCapped<T>(list: T[], row: T, cap = 200): T[] {
  return [row, ...list].slice(0, cap)
}

/** SSE order 事件 → 委托表（按 order_id 原地 upsert，同一笔委托多状态只占一行）。
 *
 * 后端对同一笔 live_order 依次推 submitted → partial/filled → canceled/rejected，
 * 每次 payload 都带 order_id。原实现每条事件新建一行，导致一笔委托出现 2~4 行、
 * 且旧行状态永久停留在「已提交」。历史行（key=-id）与 SSE 行共用 order_id，
 * 因此命中历史行时也能原地推进状态。
 */
export function upsertOrderRow(
  rows: OrderRow[], ev: Ev, liveTime = '',
): OrderRow[] {
  const oid = (ev.order_id as number) ?? null
  if (oid === null) return prependCapped(rows, orderEventToRow(ev, liveTime))
  const idx = rows.findIndex((r) => r.order_id === oid || r.key === -oid)
  if (idx < 0) return prependCapped(rows, orderEventToRow(ev, liveTime))
  const prev = rows[idx]
  const incoming = orderEventToRow(ev, liveTime)
  const next = [...rows]
  next[idx] = {
    ...prev,
    time: incoming.time || prev.time,
    ts: incoming.ts ?? prev.ts,
    status: incoming.status,
    // 成交回报事件不带 price（只有 filled_price）→ 保留原委托价
    price: (ev.price as number) ?? prev.price,
    quantity: incoming.quantity || prev.quantity,
    filled_quantity: incoming.filled_quantity ?? prev.filled_quantity,
    filled_price: incoming.filled_price ?? prev.filled_price,
    error_message: incoming.error_message ?? prev.error_message,
  }
  return next
}
