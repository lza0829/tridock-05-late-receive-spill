from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import alias_service as svc

router = APIRouter(tags=["aliases"])


class AliasIn(BaseModel):
    supplier_code: str
    external_sku: str
    internal_sku: str


class PreviewIn(BaseModel):
    supplier_code: str
    external_sku: str


@router.get("/aliases")
def list_aliases(supplier_code: str | None = None):
    conn = connect()
    try:
        return svc.list_aliases(conn, supplier_code)
    finally:
        conn.close()


@router.get("/aliases/version")
def alias_version():
    conn = connect()
    try:
        return {"alias_version": svc.current_version(conn), "reconcile_mode": svc.get_mode(conn)}
    finally:
        conn.close()


@router.post("/aliases")
def upsert_alias(body: AliasIn):
    conn = connect()
    try:
        # BEGIN IMMEDIATE：与对账运行串行化，杜绝运行中别名被改造成半截新旧映射
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = svc.upsert_alias(conn, body.supplier_code, body.external_sku, body.internal_sku)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.delete("/aliases/{alias_id}")
def deactivate_alias(alias_id: int):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = svc.deactivate_alias(conn, alias_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.post("/aliases/preview")
def preview_normalize(body: PreviewIn):
    """装载前预览某外部码在当前口径下会被解析成什么（不落库）。"""
    conn = connect()
    try:
        return svc.normalize_on_load(conn, body.supplier_code, body.external_sku)
    finally:
        conn.close()
