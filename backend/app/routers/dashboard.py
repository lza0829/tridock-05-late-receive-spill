from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import is_sealed

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        day = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()["value"]
        expect_n = conn.execute("SELECT COUNT(*) c FROM expect_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        receive_n = conn.execute("SELECT COUNT(*) c FROM receive_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        confirm_n = conn.execute("SELECT COUNT(*) c FROM confirm_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        pending = 0
        miss = 0
        if batch:
            pending = conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
                (batch["id"],),
            ).fetchone()["c"]
            # 总览计数与对账物化同源：直接取批次快照数字，不另起炉灶重算
            miss = conn.execute(
                "SELECT COUNT(*) c FROM alias_miss WHERE batch_id=?", (batch["id"],)
            ).fetchone()["c"]
        mode_row = conn.execute(
            "SELECT value FROM settings WHERE key='reconcile_mode'"
        ).fetchone()
        return {
            "dock_day": day,
            "reconcile_mode": mode_row["value"] if mode_row else "alias_merge",
            "sealed": is_sealed(conn, day),
            "expect_count": expect_n,
            "receive_count": receive_n,
            "confirm_count": confirm_n,
            "latest_batch": dict(batch) if batch else None,
            "pending_variance": pending,
            "alias_miss_count": miss,
        }
    finally:
        conn.close()
