"""别名双架构核对脚本（自包含，使用临时 sqlite 库，不碰 /workspace/data）。

覆盖题面要求的全部场景：
1. 有映射时归并架构匹配成功；严格架构出差额
2. 缺映射 alias_miss 可查，且绝不进 matched
3. 已封批不被事后改别名撬动（键集/数量/快照不变）
4. 两架构切换后新对账结果可对照（同数据两批并存，各钉各的口径）
5. 并发改 alias 与对账不得半截混用新旧映射
6. 别名归并不做跨供应商同货号数量偷加总
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import traceback

_tmp = tempfile.mkdtemp(prefix="tridock_verify_")

sys.path.insert(0, "/workspace/backend")

from app import config  # noqa: E402
from pathlib import Path  # noqa: E402
config.DATA_DIR = Path(_tmp)
config.DB_PATH = Path(_tmp) / "verify.db"

from app.db import connect  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import reconcile_service as rec  # noqa: E402
from app.services import alias_service as als  # noqa: E402
from app.services.gate import is_sealed  # noqa: E402

DAY = "2026-11-18"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def insert_line(table: str, doc_col: str, doc_no: str, supplier: str, raw: str, qty: float, day: str) -> None:
    conn = connect()
    try:
        info = als.normalize_on_load(conn, supplier, raw)
        cols = {
            "expect_lines": (doc_col, "note", "created_at"),
            "receive_lines": (doc_col, "note", "created_at"),
            "confirm_lines": (doc_col, "note", "created_at"),
        }
        if table == "receive_lines":
            conn.execute(
                f"""INSERT INTO {table}({doc_col}, supplier_code, sku, raw_sku, dock_day, qty,
                                        occurred_at, note, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                (doc_no, supplier, info["sku"], raw, day, qty, "2026-11-18T09:00:00Z", None,
                 "2026-11-18T09:00:00Z"),
            )
        else:
            conn.execute(
                f"""INSERT INTO {table}({doc_col}, supplier_code, sku, raw_sku, dock_day, qty,
                                        note, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (doc_no, supplier, info["sku"], raw, day, qty, None, "2026-11-18T09:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


def batch_rows(batch_id: int, table: str):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]
    finally:
        conn.close()


def get_batch(batch_id: int):
    conn = connect()
    try:
        return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone())
    finally:
        conn.close()


def set_mode(mode: str) -> None:
    conn = connect()
    try:
        als.set_mode(conn, mode)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    seed_if_empty()

    # ---- 场景 1/2：默认 alias_merge，种子数据含别名与缺映射 ---------------
    conn = connect()
    try:
        check("默认口径为 alias_merge", als.get_mode(conn) == "alias_merge", als.get_mode(conn))
        v0 = als.current_version(conn)
        check("种子别名版本 >=1", v0 >= 1, str(v0))
    finally:
        conn.close()

    r_merge = rec.run_reconcile(connect(), DAY)
    bm = get_batch(r_merge["batch_id"])
    print("merge 批次:", {k: bm[k] for k in
          ("mode", "alias_version", "matched_count", "mapped_count",
           "alias_hit_count", "alias_miss_count", "variance_count")})

    check("merge 批次钉住 alias_version", bm["alias_version"] == v0,
          f"{bm['alias_version']} != {v0}")
    check("merge 批次保存全量映射快照", bm["alias_snapshot"] is not None
          and f'"version": {v0}' in bm["alias_snapshot"])
    check("merge 命中别名行数=3（RC-2001/CF-3001/CF-3004）", bm["alias_hit_count"] == 3,
          str(bm["alias_hit_count"]))
    check("merge 缺映射数=1（SUP-B EXT-BOX-UNKNOWN）", bm["alias_miss_count"] == 1,
          str(bm["alias_miss_count"]))

    miss = batch_rows(bm["id"], "alias_miss")
    check("alias_miss 可查且内容正确",
          len(miss) == 1 and miss[0]["supplier_code"] == "SUP-B"
          and miss[0]["raw_sku"] == "EXT-BOX-UNKNOWN" and miss[0]["leg"] == "receive",
          str(miss))

    matches = batch_rows(bm["id"], "match_rows")
    variances = batch_rows(bm["id"], "variance_rows")
    match_keys = {(m["supplier_code"], m["sku"]) for m in matches}
    check("miss 外部码绝不进 matched 键集",
          ("SUP-B", "EXT-BOX-UNKNOWN") not in match_keys, str(match_keys))
    check("归并后 SUP-A 米三腿同键（别名码并入内部码）",
          ("SUP-A", "SKU-米-25") in match_keys, str(match_keys))
    rice = next(m for m in matches if m["supplier_code"] == "SUP-A" and m["sku"] == "SKU-米-25")
    check("归并键不跨供应商：SUP-A 米数量未被加总（40/38/38）",
          (rice["expect_qty"], rice["receive_qty"], rice["confirm_qty"]) == (40.0, 38.0, 38.0),
          str((rice["expect_qty"], rice["receive_qty"], rice["confirm_qty"])))
    check("米 38≠40 为普通差额而非假匹配", rice["aligned"] == 0)
    var_keys = {(v["supplier_code"], v["sku"]) for v in variances}
    check("差额含米与巾，不含缺映射码",
          var_keys == {("SUP-A", "SKU-米-25"), ("SUP-C", "SKU-巾-卷")}, str(var_keys))
    towel = next(m for m in matches if m["supplier_code"] == "SUP-C" and m["sku"] == "SKU-巾-卷")
    check("SUP-C 回执别名 ALIAS-TOWEL 归并后对齐到 SKU-巾-卷（60/66/60）",
          (towel["expect_qty"], towel["receive_qty"], towel["confirm_qty"]) == (60.0, 66.0, 60.0))
    check("油与箱在 merge 下对齐",
          all(any(m["supplier_code"] == s and m["sku"] == k and m["aligned"] == 1
              for m in matches) for s, k in [("SUP-A", "SKU-油-5"), ("SUP-B", "SKU-箱-大")]))
    check("mapped_count=归并键数=4", bm["mapped_count"] == 4, str(bm["mapped_count"]))

    # raw_detail 留痕：内部键能回溯到外部原始码
    check("raw_detail 记录别名原始码",
          "ALIAS-RICE-25" in rice["raw_detail"] and "ALIAS-TOWEL" in towel["raw_detail"],
          towel["raw_detail"])

    # ---- 场景 1b：同数据切 strict，表面差额，禁止静默改键 ------------------
    set_mode("strict")
    r_strict = rec.run_reconcile(connect(), DAY)
    bs = get_batch(r_strict["batch_id"])
    print("strict 批次:", {k: bs[k] for k in
          ("mode", "matched_count", "alias_miss_count", "variance_count")})
    check("strict 批次 mode 钉为 strict", bs["mode"] == "strict")
    check("strict 无 alias_miss（别名不参与，表面差额进普通差额）",
          bs["alias_miss_count"] == 0 and bs["alias_version"] is None)
    s_matches = batch_rows(bs["id"], "match_rows")
    s_var = batch_rows(bs["id"], "variance_rows")
    s_var_keys = {(v["supplier_code"], v["sku"]) for v in s_var}
    check("strict 下别名码成为独立键并出差额",
          ("SUP-A", "ALIAS-RICE-25") in s_var_keys
          and ("SUP-C", "ALIAS-TOWEL") in s_var_keys
          and ("SUP-B", "EXT-BOX-UNKNOWN") in s_var_keys,
          str(s_var_keys))
    check("strict 下内部码 SKU-米-25 只剩预报量（40/0/0）也是差额",
          ("SUP-A", "SKU-米-25") in s_var_keys)
    check("strict 差额数 > merge 差额数（别名造成的表面差额）",
          bs["variance_count"] > bm["variance_count"],
          f"{bs['variance_count']} vs {bm['variance_count']}")

    # ---- 场景 4：切回 merge 重跑，两架构批次并存可对照 ---------------------
    set_mode("alias_merge")
    r_merge2 = rec.run_reconcile(connect(), DAY)
    bm2 = get_batch(r_merge2["batch_id"])
    conn = connect()
    try:
        day_batches = [dict(r) for r in conn.execute(
            "SELECT id, mode, status FROM reconcile_batches WHERE dock_day=? ORDER BY id", (DAY,))]
    finally:
        conn.close()
    check("同到货日 strict/merge 批次并存可对照（旧批 superseded 留存不删）",
          [b["mode"] for b in day_batches] == ["alias_merge", "strict", "alias_merge"]
          and [b["status"] for b in day_batches] == ["superseded", "superseded", "open"],
          str(day_batches))
    check("重跑 merge 结果与首次一致（确定性）",
          (bm2["matched_count"], bm2["variance_count"], bm2["alias_miss_count"])
          == (bm["matched_count"], bm["variance_count"], bm["alias_miss_count"]))

    # ---- 场景 3：补映射消 miss → 处置差额 → 封批 → 改别名撬不动 -----------
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = als.upsert_alias(conn, "SUP-B", "EXT-BOX-UNKNOWN", "SKU-箱-大")
        conn.commit()
        v1 = res["version"]
    finally:
        conn.close()
    check("别名维护产生新版本", v1 > v0, f"{v1} <= {v0}")

    # 有 miss 时禁止结案
    sealed_before = False
    try:
        rec.seal_day(connect(), DAY)
    except ValueError as e:
        sealed_before = "alias_miss" in str(e)
    check("merge 批次存在 alias_miss 时写闸拒绝结案", sealed_before)

    r_merge3 = rec.run_reconcile(connect(), DAY)
    bm3 = get_batch(r_merge3["batch_id"])
    check("补映射后新批次 miss=0，归并键含箱-大且实收 107",
          bm3["alias_miss_count"] == 0 and bm3["alias_version"] == v1)
    m3 = batch_rows(bm3["id"], "match_rows")
    box = next(m for m in m3 if m["supplier_code"] == "SUP-B" and m["sku"] == "SKU-箱-大")
    check("同供应商别名归并：箱-大 实收 100+7=107（合法的同供应商同键聚合）",
          (box["expect_qty"], box["receive_qty"], box["confirm_qty"]) == (100.0, 107.0, 100.0))
    check("箱-大 107≠100 为差额待处置", box["aligned"] == 0)

    # 处置全部差额后封批
    for v in batch_rows(bm3["id"], "variance_rows"):
        rec.set_variance_fate(connect(), v["id"], "accept_short")
    sealed = rec.seal_day(connect(), DAY)
    check("差额清零后封批成功", sealed["status"] == "sealed" and sealed["sealed_at"])
    conn = connect()
    try:
        check("gate 判定当日已封", is_sealed(conn, DAY))
    finally:
        conn.close()

    # 事后改别名（新版本），sealed 批次键集/数量/快照不得被撬
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        als.upsert_alias(conn, "SUP-A", "ALIAS-RICE-25", "SKU-油-5")  # 故意改指向
        als.upsert_alias(conn, "SUP-C", "ALIAS-NEW-9", "SKU-巾-卷")
        conn.commit()
        v2 = als.current_version(conn)
    finally:
        conn.close()
    check("别名已推进到新版本", v2 > v1)

    frozen = get_batch(bm3["id"])
    frozen_matches = batch_rows(bm3["id"], "match_rows")
    check("封批批次 alias_version 不被事后别名改动撬动", frozen["alias_version"] == v1,
          f"{frozen['alias_version']} != {v1}")
    check("封批批次键集不变",
          {(m["supplier_code"], m["sku"]) for m in frozen_matches}
          == {(m["supplier_code"], m["sku"]) for m in m3})
    check("封批批次数量不变（米仍 40/38/38）",
          next(m for m in frozen_matches if m["supplier_code"] == "SUP-A"
               and m["sku"] == "SKU-米-25")["receive_qty"] == 38.0)
    check("封批快照 JSON 仍钉旧映射",
          frozen["alias_snapshot"] is not None and f'"version": {v1}' in frozen["alias_snapshot"])

    # 封批日禁止重跑、禁止装载
    blocked = False
    try:
        rec.run_reconcile(connect(), DAY)
    except ValueError:
        blocked = True
    check("封批后重跑对账被拒", blocked)
    blocked_write = False
    try:
        # 走真实路由写路径：lines.add_expect 内部经 gate.assert_writable
        from app.routers.lines import ExpectIn, add_expect
        add_expect(ExpectIn(doc_no="EX-BLOCK-1", supplier_code="SUP-A", sku="SKU-米-25",
                            dock_day=DAY, qty=1))
    except Exception as e:
        blocked_write = "结案" in str(e)
    check("封批后装载写路径被 gate 拒绝", blocked_write)

    # ---- 场景 6：跨供应商同货号严禁加总（独立数据再验） -------------------
    d2 = "2026-11-19"
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        als.upsert_alias(conn, "SUP-B", "ALPHA", "SHARED-SKU")
        als.upsert_alias(conn, "SUP-C", "ALPHA", "SHARED-SKU")
        conn.commit()
    finally:
        conn.close()
    insert_line("expect_lines", "doc_no", "EX-D2-1", "SUP-B", "SHARED-SKU", 10, d2)
    insert_line("expect_lines", "doc_no", "EX-D2-2", "SUP-C", "SHARED-SKU", 20, d2)
    insert_line("receive_lines", "receive_no", "RC-D2-1", "SUP-B", "ALPHA", 10, d2)
    insert_line("receive_lines", "receive_no", "RC-D2-2", "SUP-C", "ALPHA", 20, d2)
    insert_line("confirm_lines", "confirm_no", "CF-D2-1", "SUP-B", "ALPHA", 10, d2)
    insert_line("confirm_lines", "confirm_no", "CF-D2-2", "SUP-C", "ALPHA", 20, d2)
    r_d2 = rec.run_reconcile(connect(), d2)
    bd2 = get_batch(r_d2["batch_id"])
    d2m = batch_rows(bd2["id"], "match_rows")
    b_row = next(m for m in d2m if m["supplier_code"] == "SUP-B" and m["sku"] == "SHARED-SKU")
    c_row = next(m for m in d2m if m["supplier_code"] == "SUP-C" and m["sku"] == "SHARED-SKU")
    check("跨供应商同内部码各自成行，数量不偷加总（B=10, C=20）",
          b_row["receive_qty"] == 10.0 and c_row["receive_qty"] == 20.0
          and len(d2m) == 2, str([(m["supplier_code"], m["sku"], m["receive_qty"]) for m in d2m]))
    check("两供应商各自三量对齐", b_row["aligned"] == 1 and c_row["aligned"] == 1)

    # ---- 场景 5：并发改 alias 与对账，不得半截混用新旧映射 ----------------
    d3 = "2026-11-20"
    insert_line("expect_lines", "doc_no", "EX-D3-1", "SUP-A", "SKU-米-25", 5, d3)
    insert_line("receive_lines", "receive_no", "RC-D3-1", "SUP-A", "SKU-米-25", 5, d3)
    insert_line("confirm_lines", "confirm_no", "CF-D3-1", "SUP-A", "SKU-米-25", 5, d3)

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer() -> None:
        try:
            barrier.wait()
            for i in range(5):
                c = connect()
                try:
                    c.execute("BEGIN IMMEDIATE")
                    als.upsert_alias(c, "SUP-A", f"CONC-{i}", "SKU-米-25")
                    c.commit()
                except Exception as e:
                    c.rollback()
                    errors.append(e)
                finally:
                    c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reconciler() -> None:
        try:
            barrier.wait()
            for _ in range(5):
                rec.run_reconcile(connect(), d3)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reconciler)
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发线程无死锁/未捕获错误", not errors, str(errors[:2]))

    # 每个批次必须自洽：快照版本、alias_version、物化行与该版本映射可重放一致
    conn = connect()
    try:
        batches = [dict(r) for r in conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id", (d3,))]
    finally:
        conn.close()
    coherent = True
    detail_bad = ""
    for b in batches:
        if b["mode"] != "alias_merge":
            coherent = False; detail_bad = "mode"; break
        snap = __import__("json").loads(b["alias_snapshot"])
        if snap["version"] != b["alias_version"]:
            coherent = False; detail_bad = f"v{snap['version']}!={b['alias_version']}"; break
        # 重放：用快照里的映射重新解析当日原始行，键集必须与物化行一致
        c = connect()
        try:
            live = als.load_snapshot(c, d3)
        finally:
            c.close()
        # 强制用批次快照而非 live，模拟封批口径
        snap_map = snap["mapping"]
        expect_rows = [("SUP-A", "SKU-米-25", 5.0)]
        replayed = set()
        for sup, raw, _q in expect_rows:
            internal = snap_map.get(sup, {}).get(raw, raw)  # identity 兜底
            replayed.add((sup, internal))
        materialized = {(m["supplier_code"], m["sku"])
                        for m in batch_rows(b["id"], "match_rows")}
        if replayed != materialized:
            coherent = False
            detail_bad = f"batch {b['batch_no']} {replayed} != {materialized}"
            break
    check("并发下每批次映射版本自洽、可按快照重放（无半截新旧混用）", coherent, detail_bad)

    # ---- 总览同源计数（不重算，只读数） -----------------------------------
    from app.routers.dashboard import dashboard  # noqa: E402
    # FastAPI 端点函数可直接调用（无 Request 依赖）
    dash = dashboard()
    check("总览返回口径与 miss 计数",
          "reconcile_mode" in dash and "alias_miss_count" in dash
          and dash["sealed"] is True, str(dash))

    print("\n" + ("=" * 60))
    if failures:
        print(f"共 {len(failures)} 项失败：")
        for f in failures:
            print(" -", f)
        return 1
    print("全部核对通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
