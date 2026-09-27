# 0014 — TQ 行业数据源验证记录（regime-alpha-suite Task 15 spike）

- 日期：2026-09-27
- 结论：**通过**。通达信 TQ（tqcenter）可直接提供行业列表、行业名称、行业指数日线、个股→行业映射，无需手工映射表，Task 16 可继续。
- 参考实现：`D:\行业评级系统`（同为 tqcenter 方案），其行业代码口径为通达信二级行业 `881xxx.SH`（共 128 个）。

## 接口形态（均为项目现有 `core/tq` 可直接封装的 SDK 调用）

| 能力 | 调用 | 返回 |
|---|---|---|
| 行业代码列表 | `tq.get_sector_list(list_type=0)` | 587 个板块代码（str），其中 `startswith("881")` 的 **128 个**为二级行业，如 `881002.SH` |
| 行业名称字典 | `tq.get_sector_list(list_type=1)` | `[{"Code": "881002.SH", "Name": "煤炭开采"}, ...]`，881 项 128 个，全部带名称 |
| 行业成分股 | `tq.get_stock_list_in_sector("881002.SH", block_type=0)` | `['000552.SZ', ...]`（带后缀，25 只） |
| 行业指数日线 | `tq.get_market_data(field_list=["Close",...], stock_list=["881002.SH"], period="1d", start_time="20240101", end_time="20240201", ...)` | 与个股/宽基同构的 DataFrame（实测 100 行）；**时间格式必须 `YYYYMMDD`**（带连字符报 ValueError） |
| 个股→行业反查 | `tq.get_relation("000552.SZ")` | `[{"BlockCode","BlockName","BlockType","GPNume"}]`，`BlockType ∈ 行业/地区/概念/风格`；过滤 `=="行业"` 即得唯一二级行业（实测每只股票恰 1 个行业块） |

## 证据样例（实测）

- `881002.SH` = 煤炭开采，成分 25 只：000552.SZ、000571.SZ、000937.SZ、000983.SZ、002128.SZ …
- `get_relation("000552.SZ")` 行业块：`{'BlockCode': '881002.SH', 'BlockName': '煤炭开采', 'BlockType': '行业', 'GPNume': '25'}`
- 行业指数 K 线：`881002.SH` Close 2024-01-30=1923.00 / 01-31=1958.03 / 02-01=1923.24

## 覆盖度（TR-15.1）

跨 5 个行业各取成分股，混合 **20 只样本股**逐一 `get_relation`：**20/20 成功映射到唯一二级行业**（煤炭开采/焦炭加工/油气开采/油服工程/石油化工）。

## Task 16 落地方案

1. 映射来源用 `get_relation`（按持仓个股现查，天然处理行业归属变更）；行业中文名用 `get_sector_list(list_type=1)` 建 `881xxx.SH → Name` 表。
2. 行业指数日线复用现有 `build_index_close_series`（TQ 取数与宽基同链路，代码 `881xxx.SH`）。
3. 128 行业全量取数成本高：归因只拉「持仓涉及行业 + 领涨 Top-N 行业」指数（按需集合），不做全市场。
4. 行业口径为**通达信二级行业（881）**，非申万；报告/界面注明口径。
