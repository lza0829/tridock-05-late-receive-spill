"""三联对账物化服务。

两种架构全局只贯彻一种（settings.reconcile_mode），每个批次在创建时钉住口径：
- alias_merge：外部码经别名表（同供应商作用域）映射到内部 sku 再比三腿；
  映射失败进 alias_miss 桶，绝不进 matched；批次快照记录 mapped_count 与
  所用 alias 版本/全量映射；改别名后已 sealed 批键集不得被撬。
- strict：只按原始码比三腿；别名表仅展示；别名造成的表面差额进普通差额，
  人工处置，禁止静默映射改键。

并发：run/seal 以 BEGIN IMMEDIATE 拿写锁，与别名维护写事务串行化；
一次运行内只使用载入时的同一份 AliasSnapshot，杜绝新旧映射半截混用。
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
import json
import sqlite3

from app.services import alias_service as aliases

MISS_REASON = "外部码在别名表无映射且非内部货号"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw_rows(conn: sqlite3.Connection, table: str, dock_day: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT supplier_code, sku, raw_sku, qty FROM {table} WHERE dock_day=?",
        (dock_day,),
    ).fetchall()


def _leg_aggregates(rows) -> dict[str, list[tuple[str, str, float]]]:
    """把一条腿的行拆成 (供应商, 原始码, 数量) 序列。"""
    out = []
    for r in rows:
        raw = r["raw_sku"] or r["sku"]
        out.append((r["supplier_code"], raw, float(r["qty"])))
    return out


def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    # 写锁先行：与别名 upsert/deactivate、其他 run/seal 全串行
    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

        mode = aliases.get_mode(conn)

        # 当日旧 open 批次标记 superseded 而非删除：物化行/快照全部留存，
        # 切换架构后的新旧批次可长期对照；sealed 批次在上一步已被拦截。
        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute(
                "UPDATE reconcile_batches SET status='superseded' WHERE id=?", (r["id"],)
            )

        legs_raw = {
            "expect": _leg_aggregates(_raw_rows(conn, "expect_lines", dock_day)),
            "receive": _leg_aggregates(_raw_rows(conn, "receive_lines", dock_day)),
            "confirm": _leg_aggregates(_raw_rows(conn, "confirm_lines", dock_day)),
        }

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        now = _now()

        # ---- 严格码架构：原始码即键，别名完全不参与 ----------------------
        if mode == "strict":
            sums: dict[str, dict[tuple[str, str], float]] = {}
            for leg, tuples in legs_raw.items():
                m: dict[tuple[str, str], float] = defaultdict(float)
                for supplier_code, raw, qty in tuples:
                    m[(supplier_code, raw)] += qty
                sums[leg] = m
            keys = set(sums["expect"]) | set(sums["receive"]) | set(sums["confirm"])
            cur = conn.execute(
                '''INSERT INTO reconcile_batches(batch_no, dock_day, mode, alias_version, alias_snapshot,
                      matched_count, mapped_count, alias_hit_count, alias_miss_count,
                      variance_count, status, sealed_at, note)
                   VALUES (?,?, 'strict', NULL, NULL, 0,0,0,0, 0,'open',NULL,?)''',
                (batch_no, dock_day, "严格码对齐（别名表仅展示，不参与改键）"),
            )
            batch_id = int(cur.lastrowid)
            matched = variance = 0
            for supplier_code, sku in sorted(keys):
                eq = sums["expect"].get((supplier_code, sku), 0.0)
                rq = sums["receive"].get((supplier_code, sku), 0.0)
                cq = sums["confirm"].get((supplier_code, sku), 0.0)
                aligned = int(eq == rq == cq)
                detail = json.dumps({"expect": [sku], "receive": [sku], "confirm": [sku]},
                                    ensure_ascii=False)
                conn.execute(
                    '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, raw_detail,
                           expect_qty, receive_qty, confirm_qty, aligned)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (batch_id, dock_day, supplier_code, sku, detail, eq, rq, cq, aligned),
                )
                if aligned:
                    matched += 1
                else:
                    variance += 1
                    conn.execute(
                        '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, raw_detail,
                               expect_qty, receive_qty, confirm_qty, fate, created_at)
                           VALUES (?,?,?,?,?,?,?,?,'pending',?)''',
                        (batch_id, dock_day, supplier_code, sku, detail, eq, rq, cq, now),
                    )
            conn.execute(
                '''UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?''',
                (matched, variance, batch_id),
            )
            conn.commit()
            return {"batch_id": batch_id, "batch_no": batch_no, "dock_day": dock_day, "mode": mode,
                    "matched_count": matched, "mapped_count": 0, "alias_hit_count": 0,
                    "alias_miss_count": 0, "variance_count": variance, "status": "open"}

        # ---- 别名归并架构：钉版快照，同供应商作用域内映射 ----------------
        snap = aliases.load_snapshot(conn, dock_day)
        # 键内聚合：(supplier, internal) -> 腿 -> 数量；原始码留痕
        agg: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        raw_detail: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
            lambda: {"expect": set(), "receive": set(), "confirm": set()})
        miss_rows: list[tuple[str, str, str, float]] = []
        alias_hit_lines = 0

        for leg in aliases.LEGS:
            for supplier_code, raw, qty in legs_raw[leg]:
                internal, status = snap.resolve(supplier_code, raw)
                if status == "miss":
                    # 映射失败：只进 alias_miss，绝不进 matched/普通差额
                    miss_rows.append((supplier_code, leg, raw, qty))
                    continue
                if status == "alias_hit":
                    alias_hit_lines += 1
                key = (supplier_code, internal)
                agg[key][leg] += qty
                raw_detail[key][leg].add(raw)

        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, mode, alias_version, alias_snapshot,
                  matched_count, mapped_count, alias_hit_count, alias_miss_count,
                  variance_count, status, sealed_at, note)
               VALUES (?,?, 'alias_merge', ?, ?, 0,0,0,0, 0,'open',NULL,?)''',
            (batch_no, dock_day, snap.version, snap.to_json(),
             "别名归并后三联对齐（归并键含供应商，禁止跨供应商加总）"),
        )
        batch_id = int(cur.lastrowid)

        matched = variance = 0
        mapped_keys = len(agg)
        for supplier_code, internal in sorted(agg):
            eq = agg[(supplier_code, internal)].get("expect", 0.0)
            rq = agg[(supplier_code, internal)].get("receive", 0.0)
            cq = agg[(supplier_code, internal)].get("confirm", 0.0)
            aligned = int(eq == rq == cq)
            detail = json.dumps(
                {leg: sorted(raw_detail[(supplier_code, internal)][leg]) for leg in aliases.LEGS},
                ensure_ascii=False,
            )
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, raw_detail,
                       expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, internal, detail, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, raw_detail,
                           expect_qty, receive_qty, confirm_qty, fate, created_at)
                       VALUES (?,?,?,?,?,?,?,?,'pending',?)''',
                    (batch_id, dock_day, supplier_code, internal, detail, eq, rq, cq, now),
                )

        miss_count = 0
        for supplier_code, leg, raw, qty in miss_rows:
            miss_count += 1
            conn.execute(
                '''INSERT INTO alias_miss(batch_id, dock_day, supplier_code, leg, raw_sku, qty, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, leg, raw, qty,
                 MISS_REASON, now),
            )

        conn.execute(
            '''UPDATE reconcile_batches
               SET matched_count=?, mapped_count=?, alias_hit_count=?, alias_miss_count=?,
                   variance_count=? WHERE id=?''',
            (matched, mapped_keys, alias_hit_lines, miss_count, variance, batch_id),
        )
        conn.commit()
        return {"batch_id": batch_id, "batch_no": batch_no, "dock_day": dock_day, "mode": mode,
                "alias_version": snap.version, "matched_count": matched,
                "mapped_count": mapped_keys, "alias_hit_count": alias_hit_lines,
                "alias_miss_count": miss_count, "variance_count": variance, "status": "open"}
    except Exception:
        conn.rollback()
        raise


def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        blockers = []
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            blockers.append(f"仍有 {pending} 条差额未处置")
        if batch["mode"] == "alias_merge":
            miss = conn.execute(
                "SELECT COUNT(*) c FROM alias_miss WHERE batch_id=?", (batch["id"],)
            ).fetchone()["c"]
            if miss:
                blockers.append(
                    f"仍有 {miss} 条 alias_miss 未映射：请先在别名页补映射后重跑对账"
                )
        if blockers:
            raise ValueError("不能结案：" + "；".join(blockers))
        now = _now()
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=?",
            (now, batch["id"]),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())
    except Exception:
        conn.rollback()
        raise


def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    allowed = {"pending", "accept_short", "accept_over", "quarantine"}
    if fate not in allowed:
        raise ValueError("非法差额命运")
    row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
    if not row:
        raise ValueError("差额行不存在")
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)).fetchone()
    if batch and batch["status"] == "sealed":
        raise ValueError("已结案批次不可改差额命运")
    if batch and batch["status"] != "open":
        raise ValueError(f"批次状态为 {batch['status']}，只有 open 批次可改差额命运")
    conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
