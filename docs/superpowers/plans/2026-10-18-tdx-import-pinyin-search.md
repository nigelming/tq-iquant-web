# 通达信导入弹窗拼音首字母搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「从通达信导入公式」弹窗搜索框支持拼音首字母匹配（`lmsj` → 六脉神剑），仅前端改动。

**Architecture:** 新建纯函数模块 `web/src/utils/tdxSearch.ts`（pinyin-pro 建首字母索引 + 匹配规则），`Formulas.vue` 的 `filteredTdx` 过滤改走该模块。后端零改动。

**Tech Stack:** Vue 3 `<script setup>` + TS、vitest（`npx vitest`，web/ 目录）、`pinyin-pro`（`pattern: 'first'`）。

## Global Constraints

- 仅改 `web/`；后端与 `TdxFormulaListItem` API 类型零改动（拼音是前端派生数据，存 `Map`，不加字段）。
- TDD：每个任务先写失败测试再实现。
- 测试命令在 `web/` 目录执行；类型检查 `npx vue-tsc -b`。
- 工作区有大量无关未提交改动；commit 只 `git add` 本任务列出的文件，禁止 `git add -A`。
- 搜索行为兼容：原中文/公式名子串匹配、空 query 不过滤、分页与已导入标记不变。

---

### Task 1: tdxSearch 纯函数模块（索引 + 匹配）

**Files:**
- Create: `web/src/utils/tdxSearch.ts`
- Create: `web/src/__tests__/tdxSearch.test.ts`

**Interfaces:**
- Consumes: `TdxFormulaListItem`（`web/src/api/index.ts` 已有，仅 type-only 引用）；`pinyin-pro` 的 `pinyin(text, opts)`。
- Produces（Task 2 依赖，签名必须一致）:
  - `interface TdxPinyin { code: string; name: string }`
  - `function toInitials(text: string): string`
  - `function buildPinyinIndex(items: TdxFormulaListItem[]): Map<string, TdxPinyin>`
  - `function matchTdx(query: string, item: TdxFormulaListItem, py: TdxPinyin | undefined): boolean`

- [ ] **Step 1: 写失败测试**

创建 `web/src/__tests__/tdxSearch.test.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { toInitials, buildPinyinIndex, matchTdx } from '../utils/tdxSearch'
import type { TdxFormulaListItem } from '../api'

const item = (acCode: string, acName = ''): TdxFormulaListItem =>
  ({ acCode, acName, isSys: 0, imported: false, formula_id: null })

describe('toInitials', () => {
  it('中文名 → 拼音首字母', () => {
    expect(toInitials('六脉神剑')).toBe('lmsj')
  })
  it('中英数混合名保留数字、剔除标点空白', () => {
    expect(toInitials('指标共震6红上车，3红下车')).toBe('zbgz6hsc3hxc')
  })
  it('空串返回空串', () => {
    expect(toInitials('')).toBe('')
  })
})

describe('buildPinyinIndex + matchTdx', () => {
  const items = [
    item('六脉神剑', '指标共震6红上车，3红下车'),
    item('逆时钟'),
    item('COSTLINE', '成本线'),
  ]
  const py = buildPinyinIndex(items)

  it('拼音首字母命中中文名（lmsj → 六脉神剑）', () => {
    expect(matchTdx('lmsj', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('中文子串与英文子串仍命中（原行为不回归）', () => {
    expect(matchTdx('六脉', items[0], py.get('六脉神剑'))).toBe(true)
    expect(matchTdx('COST', items[2], py.get('COSTLINE'))).toBe(true)
  })

  it('acName 首字母命中（zbgz6 → 指标共震6…）', () => {
    expect(matchTdx('zbgz6', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('大小写与空格归一（"Lm Sj" → 命中）', () => {
    expect(matchTdx('Lm Sj', items[0], py.get('六脉神剑'))).toBe(true)
  })

  it('无匹配 → false', () => {
    expect(matchTdx('xyz', items[1], py.get('逆时钟'))).toBe(false)
  })

  it('acName 为空 / 无索引条目 → 不抛错、按子串与 code 规则', () => {
    expect(matchTdx('nsz', items[1], py.get('逆时钟'))).toBe(true)  // 逆时钟 code=nsz
    expect(matchTdx('xyzabc', items[1], py.get('逆时钟'))).toBe(false)
    expect(matchTdx('顺', item('顺'), undefined)).toBe(true)         // 无索引 → 子串规则
    expect(matchTdx('shun', item('顺'), undefined)).toBe(false)
  })

  it('空 / 全空白 query → 命中（不过滤）', () => {
    expect(matchTdx('', items[1], undefined)).toBe(true)
    expect(matchTdx('   ', items[1], undefined)).toBe(true)
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/__tests__/tdxSearch.test.ts`（workdir `web/`）
Expected: FAIL — `Cannot find module '../utils/tdxSearch'`

- [ ] **Step 3: 安装依赖**

Run: `npm install pinyin-pro`（workdir `web/`）
Expected: 加入 `dependencies`，lockfile 更新。

- [ ] **Step 4: 写最小实现**

创建 `web/src/utils/tdxSearch.ts`：

```ts
import { pinyin } from 'pinyin-pro'
import type { TdxFormulaListItem } from '../api'

export interface TdxPinyin { code: string; name: string }

// 文本 → 拼音首字母小写串（剔除非 [a-z0-9]，如标点/空白；数字保留）
export function toInitials(text: string): string {
  if (!text) return ''
  const letters = pinyin(text, { pattern: 'first', toneType: 'none', type: 'array' })
  return letters.join('').toLowerCase().replace(/[^a-z0-9]/g, '')
}

// 列表加载后建一次索引；key=acCode（前端派生数据，不进 API 类型）
export function buildPinyinIndex(items: TdxFormulaListItem[]): Map<string, TdxPinyin> {
  const map = new Map<string, TdxPinyin>()
  for (const it of items) {
    map.set(it.acCode, { code: toInitials(it.acCode), name: toInitials(it.acName || '') })
  }
  return map
}

// 匹配：空 query 全命中；子串（原行为，trim+lower）∪ 拼音首字母（去全部空白）
export function matchTdx(
  query: string,
  item: TdxFormulaListItem,
  py: TdxPinyin | undefined,
): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  if (item.acCode.toLowerCase().includes(q)) return true
  if ((item.acName || '').toLowerCase().includes(q)) return true
  const qp = q.replace(/\s+/g, '')
  if (qp && py) {
    if (py.code.includes(qp)) return true
    if (py.name.includes(qp)) return true
  }
  return false
}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `npx vitest run src/__tests__/tdxSearch.test.ts`（workdir `web/`）
Expected: PASS（全部用例）

- [ ] **Step 6: Commit**

```bash
git add web/src/utils/tdxSearch.ts web/src/__tests__/tdxSearch.test.ts web/package.json web/package-lock.json
git commit -m "feat(web): 通达信导入拼音首字母搜索纯函数 tdxSearch"
```

---

### Task 2: Formulas.vue 接线（过滤 + placeholder）

**Files:**
- Modify: `web/src/views/Formulas.vue`（`filteredTdx` computed 约 24-29 行；`openTdx()` 约 100-113 行；搜索框 placeholder 约 216 行）
- Test: `web/src/__tests__/Formulas.test.ts`（在 `describe('Formulas.vue')` 内、现有「搜索无匹配」用例之后追加）

**Interfaces:**
- Consumes: Task 1 的 `buildPinyinIndex`、`matchTdx`、`TdxPinyin`（从 `../utils/tdxSearch` 导入）。
- Produces: 无下游任务。

- [ ] **Step 1: 写失败组件测试**

在 `web/src/__tests__/Formulas.test.ts` 的 `describe('Formulas.vue')` 块内（现有「通达信弹窗搜索无匹配 → 显示空状态」用例后）追加：

```ts
  it('通达信弹窗搜索支持拼音首字母（lmsj → 六脉神剑，zb → acName 首字母）', async () => {
    ;(getTdxFormulas as any).mockResolvedValue([
      { acCode: '六脉神剑', acName: '指标共震6红上车，3红下车', isSys: 0, imported: false, formula_id: null },
      { acCode: '逆时钟', acName: '', isSys: 0, imported: false, formula_id: null },
    ])
    const w = mount(Formulas)
    await flushPromises()

    await w.find('button.tdx-import-btn').trigger('click')
    await flushPromises()
    expect(w.find('input.tdx-search').attributes('placeholder')).toContain('拼音首字母')

    await w.find('input.tdx-search').setValue('lmsj')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(1)
    expect(w.text()).toContain('六脉神剑')

    await w.find('input.tdx-search').setValue('zb')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(1)

    await w.find('input.tdx-search').setValue('nsz')
    expect(w.findAll('.tdx-table tbody tr').length).toBe(1)
    expect(w.text()).toContain('逆时钟')
  })
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/__tests__/Formulas.test.ts`（workdir `web/`）
Expected: FAIL — placeholder 断言失败（当前为 `搜索公式名 / 中文名`），且 `lmsj` 过滤后行数为 0。

- [ ] **Step 3: 实现 Formulas.vue 改动**

3a. 导入（`import PageHeader ...` 行后加）：

```ts
import { buildPinyinIndex, matchTdx, type TdxPinyin } from '../utils/tdxSearch'
```

3b. 替换 `filteredTdx` computed（原 24-29 行的子串过滤），并在其上方加索引 ref：

```ts
const tdxPy = ref(new Map<string, TdxPinyin>())
const filteredTdx = computed(() =>
  tdxItems.value.filter(it => matchTdx(tdxQuery.value, it, tdxPy.value.get(it.acCode))))
```

（原 `filteredTdx` 的三行 `const q = ...; if (!q) ...; return ...filter...` 整体删除。）

3c. `openTdx()` 中拉取成功后建索引：

```ts
tdxItems.value = await getTdxFormulas(true)
tdxPy.value = buildPinyinIndex(tdxItems.value)
```

3d. 搜索框 placeholder（约 216 行）：

```html
<input v-model="tdxQuery" class="tdx-search" placeholder="搜索公式名 / 中文名 / 拼音首字母" style="margin-bottom:8px;width:100%" />
```

- [ ] **Step 4: 运行组件测试确认通过**

Run: `npx vitest run src/__tests__/Formulas.test.ts`（workdir `web/`）
Expected: PASS（含既有搜索/分页用例不回归）

- [ ] **Step 5: 全量测试**

Run: `npx vitest run`（workdir `web/`）
Expected: 全部 Test Files PASS

- [ ] **Step 6: 类型检查**

Run: `npx vue-tsc -b`（workdir `web/`）
Expected: 无错误退出

- [ ] **Step 7: Commit**

```bash
git add web/src/views/Formulas.vue web/src/__tests__/Formulas.test.ts
git commit -m "feat(web): 通达信导入弹窗搜索支持拼音首字母匹配"
```
