# 通达信导入弹窗拼音首字母搜索 — 设计

日期：2026-10-18
状态：已确认

## 目标

「从通达信导入公式」弹窗的搜索框，在现有中文/公式名子串匹配之外，新增**拼音首字母匹配**：输入 `lmsj` 可命中「六脉神剑」。仅该弹窗生效，不改后端接口。

## 背景与现状

- 中文公式名已全链路支持（`tdx-list` 返回 `acCode`/`acName`，落库与展示均正常）。
- 导入弹窗已有搜索框（`Formulas.vue` `tdxQuery`），过滤为纯子串匹配（`acCode`/`acName` 小写包含），不支持拼音。

## 方案取舍

- **A（选定）前端 `pinyin-pro`**：列表本就全量在前端，加载后一次性建首字母索引，客户端过滤即时；不动后端。
- B 后端 `pypinyin` 附带字段：唯一消费方在前端，引入后端依赖与接口变更属于过度设计，弃。
- C 自建首字母映射表：多音字与维护成本差，弃。

## 设计

### 依赖

`web/package.json` 新增 `pinyin-pro`（纯前端库，Node 环境 vitest 可用）。

### 索引构建

`openTdx()` 拉取列表成功后，一次性为每条公式构建拼音索引，存
`Map<string, { code: string; name: string }>`，key 为 `acCode`：

- `code`：`acCode` 的首字母串；`name`：`acName` 的首字母串（空值 → `''`）。
- 计算方式：`pinyin-pro` 的 `pinyin(text, { pattern: 'first' })`，结果再剔除非 `[a-z0-9]` 字符并小写。
- 索引是前端派生数据，不污染 API 返回类型 `TdxFormulaListItem`。
- 数字/标点混合名（如「指标共震6红上车，3红下车」）→ 首字母串保留数字（`zbgz6hsc3hxc`）；多音字交给 pinyin-pro 词组判定。

### 匹配逻辑（独立纯函数模块）

新建 `web/src/utils/tdxSearch.ts`：

- `buildPinyinIndex(items: TdxFormulaListItem[]): Map<string, { code: string; name: string }>`
- `matchTdx(query: string, item: TdxFormulaListItem, py: { code: string; name: string } | undefined): boolean`
  - 子串匹配（原行为）：`query` 小写去首尾空白后，`acCode` 或 `acName` 包含即命中；
  - 拼音匹配：规范化 query（小写、去全部空白）后，与 `py.code` 或 `py.name` 包含即命中；
  - 空 query → 命中（不过滤）。

### 组件接线

`Formulas.vue`：

- `openTdx()` 成功后 `tdxPy = buildPinyinIndex(tdxItems.value)`；
- `filteredTdx` 改走 `matchTdx(tdxQuery.value, it, tdxPy.get(it.acCode))`；
- 搜索框 placeholder 改为 `搜索公式名 / 中文名 / 拼音首字母`。

分页、已导入标记、导入动作均不变；后端零改动。

## 错误处理

- 拉取列表失败：沿用现有 alert + 关闭弹窗流程，不建索引。
- `acName` 为空：`name` 索引为空串，不影响匹配；不抛错。

## 测试

`web/src/__tests__/` 新增 `tdxSearch` 纯函数用例：

1. 拼音首字母命中中文名（`lmsj` → 六脉神剑）；
2. 中文子串仍命中（原行为不回归）；
3. `acName` 首字母命中；
4. 大小写与空格归一（`Lm Sj` → 命中）；
5. 无匹配 → false；
6. `acName` 为空 / 无索引条目 → 不抛错、按子串规则匹配；
7. 中英数混合名首字母（`zbgz6` → 指标共震6红上车）。
