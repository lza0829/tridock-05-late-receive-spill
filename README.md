# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 别名双架构（全局只贯彻一种）

供应商回执货号与预报货号常是别名关系。系统在 `settings.reconcile_mode` 中维护唯一口径，
所有链路（别名维护页、装载写路径、对齐物化、alias_miss 分桶、批次快照、结案写闸、总览、差额列表）同源：

### 1. `alias_merge` 别名归并（默认）

- 外部码经 `aliases` 表在**同一供应商作用域**内映射到内部 sku，再按
  `(supplier_code, internal_sku)` 比三腿。归并键恒带供应商，**严禁跨供应商同货号数量偷加总**。
- 解析顺序：alias 命中（`alias_hit`）→ 内部码直通（`identity`）→ 失败（`miss`）。
- 映射失败只进独立 `alias_miss` 桶，**绝不进 matched / 普通差额**；存在 miss 时结案写闸拒绝封印。
- 每次别名维护推进单调递增的 `alias_version`；对账批次创建时把版本号与全量映射 JSON
  钉进 `reconcile_batches.alias_snapshot`，并记录 `mapped_count / alias_hit_count / alias_miss_count`。
- 一次对账运行只使用载入时的同一份快照（`BEGIN IMMEDIATE` 写锁与别名维护串行化），
  杜绝并发改 alias 时新旧映射半截混用。
- 已 `sealed` 批次的物化行与快照永不重算，事后改别名撬不动封批键集。

### 2. `strict` 严格码

- 只按原始码 `(supplier_code, raw_sku)` 比三腿；别名表仅展示，不参与改键。
- 别名造成的表面差额进普通差额（`variance_rows`），由人工处置，禁止静默映射。
- 批次 `alias_version / alias_snapshot` 为空。

### 切换与对照

- 口径切换只影响**之后新建**的批次。重跑对账时旧 open 批次标记为 `superseded`（物化行留存不删），
  因此同到货日下两种架构的批次可在对账页长期对照；sealed 批次永久保留创建时口径。

## 数据模型要点

- 三腿表（`expect_lines / receive_lines / confirm_lines`）同时保存
  `raw_sku`（原始外部码）与 `sku`（归并后内部码），装载时与对账物化共用同一套规范化逻辑
  （`app/services/alias_service.py`）。
- `match_rows / variance_rows.raw_detail` 以 JSON 留痕每个归并键由哪些原始码汇聚而成。
- `reconcile_batches` 钉住 `mode / alias_version / alias_snapshot` 与三档计数。

## 验证脚本（自包含，使用临时库）

```bash
cd backend && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python ../scripts/verify_alias_architecture.py   # 37 项架构核对
python ../scripts/smoke_http.py                  # HTTP 端到端冒烟（需 httpx）
```

覆盖：有映射时归并匹配/严格出差额、缺映射 alias_miss 可查且不进 matched、
封批后改别名撬不动、两架构批次并存对照、并发改 alias 与对账不混用、跨供应商不加总。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```
