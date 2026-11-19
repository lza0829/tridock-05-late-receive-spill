from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import alias_service

router = APIRouter(tags=["settings"])


class DayIn(BaseModel):
    dock_day: str


class ModeIn(BaseModel):
    mode: str


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {r["key"]: r["value"] for r in rows}
        out.setdefault("reconcile_mode", alias_service.DEFAULT_MODE)
        out["alias_version"] = alias_service.current_version(conn)
        return out
    finally:
        conn.close()


@router.post("/settings/dock-day")
def set_dock_day(body: DayIn):
    if len(body.dock_day) != 10:
        raise HTTPException(400, "dock_day 格式应为 YYYY-MM-DD")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('dock_day', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.dock_day,),
        )
        conn.commit()
        return {"dock_day": body.dock_day}
    finally:
        conn.close()


@router.post("/settings/reconcile-mode")
def set_reconcile_mode(body: ModeIn):
    """切换全局口径。只影响之后新建的批次；已 sealed 批次永久保留原口径，
    因此切换前后两架构的对账结果可在批次列表里直接对照。"""
    conn = connect()
    try:
        mode = alias_service.set_mode(conn, body.mode)  # 非法值在 service 内抛 ValueError
        conn.commit()
        return {"reconcile_mode": mode}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()
