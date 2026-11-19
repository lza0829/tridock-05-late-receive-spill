"""HTTP 端到端冒烟：TestClient + 临时库，验证双架构切换全链路 API。"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="tridock_http_")
sys.path.insert(0, "/workspace/backend")
from app import config  # noqa: E402
config.DATA_DIR = Path(_tmp)
config.DB_PATH = Path(_tmp) / "http.db"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
DAY = "2026-11-18"
ok = True


def must(label, cond, payload=""):
    global ok
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f" — {payload}"))
    ok = ok and cond


r = c.get("/api/settings")
must("设置含 reconcile_mode", r.json()["reconcile_mode"] == "alias_merge", r.text)

r = c.get("/api/aliases")
must("别名列表可查（种子3条）", len(r.json()) == 3, r.text)

r = c.post("/api/aliases/preview", json={"supplier_code": "SUP-A", "external_sku": "ALIAS-RICE-25"})
must("预览别名解析为内部码", r.json()["sku"] == "SKU-米-25" and r.json()["status"] == "alias_hit", r.text)

r = c.post("/api/aliases/preview", json={"supplier_code": "SUP-A", "external_sku": "UNKNOWN-X"})
must("预览未知码返回 miss", r.json()["status"] == "miss", r.text)

# 装载走规范化：SUP-A 用别名码登记实收
r = c.post("/api/receives", json={"receive_no": "RC-T1", "supplier_code": "SUP-A",
                                  "sku": "ALIAS-RICE-25", "dock_day": DAY, "qty": 5})
must("装载规范化返回 alias_hit", r.status_code == 200 and r.json()["alias_status"] == "alias_hit", r.text)
r = c.get("/api/receives", params={"dock_day": DAY})
row = next(x for x in r.json() if x["receive_no"] == "RC-T1")
must("落库 sku=内部码、raw_sku=外部码", row["sku"] == "SKU-米-25" and row["raw_sku"] == "ALIAS-RICE-25", str(row))

# 非法口径拒绝
r = c.post("/api/settings/reconcile-mode", json={"mode": "bogus"})
must("非法口径 400", r.status_code == 400, r.text)

# merge 跑批
r = c.post("/api/reconcile/run", json={"dock_day": DAY})
bm = r.json()
must("merge 批次含 miss=1", bm["alias_miss_count"] == 1 and bm["mode"] == "alias_merge", str(bm))
r = c.get("/api/reconcile/alias-miss", params={"batch_id": bm["batch_id"]})
must("alias-miss 端点可查", len(r.json()) == 1 and r.json()[0]["raw_sku"] == "EXT-BOX-UNKNOWN", r.text)

# 有 miss 时结案 400
r = c.post("/api/reconcile/seal", json={"dock_day": DAY})
must("有 miss 结案被拒 400", r.status_code == 400 and "alias_miss" in r.json()["detail"], r.text)

# 切 strict 再跑，两批并存
r = c.post("/api/settings/reconcile-mode", json={"mode": "strict"})
must("切 strict 成功", r.json()["reconcile_mode"] == "strict", r.text)
r = c.post("/api/reconcile/run", json={"dock_day": DAY})
bs = r.json()
must("strict 批次无 miss、差额更多", bs["alias_miss_count"] == 0 and bs["variance_count"] > bm["variance_count"], str(bs))
r = c.get("/api/reconcile/batches", params={"dock_day": DAY})
must("批次列表保留两种架构", [b["mode"] for b in r.json()] == ["strict", "alias_merge"], r.text)
must("旧批为 superseded", [b["status"] for b in r.json()] == ["open", "superseded"], r.text)

# dashboard
r = c.get("/api/dashboard")
d = r.json()
must("总览含口径/miss/批次", d["reconcile_mode"] == "strict" and "alias_miss_count" in d and d["latest_batch"], str(d))

# strict 下处置差额并封批（strict 无 miss 闸）
for v in c.get("/api/reconcile/variances", params={"batch_id": bs["batch_id"]}).json():
    rr = c.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"})
    must("差额处置 200", rr.status_code == 200, rr.text)
r = c.post("/api/reconcile/seal", json={"dock_day": DAY})
must("strict 封批成功", r.status_code == 200 and r.json()["status"] == "sealed", r.text)

# 封批后写被 gate 拦
r = c.post("/api/expects", json={"doc_no": "EX-T-X", "supplier_code": "SUP-A",
                                 "sku": "SKU-米-25", "dock_day": DAY, "qty": 1})
must("封批后装载 400", r.status_code == 400 and "结案" in r.json()["detail"], r.text)

# 别名维护仍允许（不封写别名），但 sealed 批次不可被撬
r = c.post("/api/aliases", json={"supplier_code": "SUP-A", "external_sku": "ALIAS-RICE-25",
                                 "internal_sku": "SKU-油-5"})
must("封批日后别名仍可维护并产生新版本", r.status_code == 200 and "version" in r.json(), r.text)
frozen = c.get("/api/reconcile/batches", params={"dock_day": DAY}).json()
sealed_batch = next(b for b in frozen if b["status"] == "sealed")
must("sealed 批次口径仍为 strict 不被撬动", sealed_batch["mode"] == "strict", str(sealed_batch))

print("\n" + ("HTTP 冒烟全部通过 ✅" if ok else "存在失败 ❌"))
sys.exit(0 if ok else 1)
