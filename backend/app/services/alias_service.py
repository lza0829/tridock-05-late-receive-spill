"""别名服务：别名维护、装载规范化、快照物化的唯一同源实现。

口径（alias_merge 架构）：
- 归并键恒为 (supplier_code, internal_sku)，映射只在同一供应商作用域内生效，
  因此绝不会把跨供应商的同货号数量偷加总。
- 外部码解析顺序：先查 alias 表（alias_hit）；查不到但该码本身就是内部码
  （预报货号或 alias 指向的内部码）则 identity 直通；仍无法解析 → miss，
  miss 只允许进 alias_miss 桶，禁止静默改键。
- 每次别名维护产生一个单调递增的 alias_version；对账运行开始时把当时的
  全量映射与版本号一起钉进批次快照，运行内不再重读，杜绝新旧映射半截混用。
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3

MODES = ("alias_merge", "strict")
DEFAULT_MODE = "alias_merge"
LEGS = ("expect", "receive", "confirm")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 全局口径
# ---------------------------------------------------------------------------
def get_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='reconcile_mode'").fetchone()
    return row["value"] if row else DEFAULT_MODE


def set_mode(conn: sqlite3.Connection, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"非法对账口径，只允许 {MODES}")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('reconcile_mode', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (mode,),
    )
    return mode


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------
def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) v FROM alias_versions").fetchone()
    return int(row["v"] or 0)


def _bump_version(conn: sqlite3.Connection, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO alias_versions(created_at, note) VALUES (?,?)",
        (_now(), note),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 别名维护（所有写路径走 BEGIN IMMEDIATE，与对账运行串行化）
# ---------------------------------------------------------------------------
def list_aliases(conn: sqlite3.Connection, supplier_code: str | None = None) -> list[dict]:
    if supplier_code:
        rows = conn.execute(
            "SELECT * FROM aliases WHERE supplier_code=? ORDER BY supplier_code, external_sku",
            (supplier_code,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM aliases ORDER BY supplier_code, external_sku").fetchall()
    return [dict(r) for r in rows]


def upsert_alias(conn: sqlite3.Connection, supplier_code: str, external_sku: str, internal_sku: str) -> dict:
    supplier_code = (supplier_code or "").strip()
    external_sku = (external_sku or "").strip()
    internal_sku = (internal_sku or "").strip()
    if not supplier_code or not external_sku or not internal_sku:
        raise ValueError("供应商、外部码、内部货号均不能为空")
    if external_sku == internal_sku:
        raise ValueError("外部码与内部货号相同，无需建立别名")
    # 同一外部码只能指向一个内部货号；UNIQUE(supplier_code, external_sku) 兜底
    conn.execute(
        '''INSERT INTO aliases(supplier_code, external_sku, internal_sku, active, updated_at)
           VALUES (?,?,?,1,?)
           ON CONFLICT(supplier_code, external_sku) DO UPDATE SET
             internal_sku=excluded.internal_sku, active=1, updated_at=excluded.updated_at''',
        (supplier_code, external_sku, internal_sku, _now()),
    )
    version = _bump_version(conn, f"upsert {supplier_code}:{external_sku}->{internal_sku}")
    return {"version": version, "supplier_code": supplier_code,
            "external_sku": external_sku, "internal_sku": internal_sku}


def deactivate_alias(conn: sqlite3.Connection, alias_id: int) -> dict:
    row = conn.execute("SELECT * FROM aliases WHERE id=?", (alias_id,)).fetchone()
    if not row:
        raise ValueError("别名不存在")
    conn.execute("UPDATE aliases SET active=0, updated_at=? WHERE id=?", (_now(), alias_id))
    version = _bump_version(conn, f"deactivate alias#{alias_id}")
    return {"version": version, "id": alias_id, "active": 0}


# ---------------------------------------------------------------------------
# 快照：一次对账运行内唯一使用的映射视图
# ---------------------------------------------------------------------------
@dataclass
class AliasSnapshot:
    version: int
    # supplier_code -> external_sku -> internal_sku（仅 active）
    mapping: dict[str, dict[str, str]] = field(default_factory=dict)
    # supplier_code -> set(内部码)：alias 指向的内部码 ∪ 当日预报原始码
    internal_universe: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def to_json(self) -> str:
        return json.dumps(
            {"version": self.version,
             "mapping": {s: dict(m) for s, m in self.mapping.items()}},
            ensure_ascii=False, sort_keys=True,
        )

    def resolve(self, supplier_code: str, raw_sku: str) -> tuple[str | None, str]:
        """返回 (internal_sku 或 None, status)。status ∈ alias_hit/identity/miss。"""
        m = self.mapping.get(supplier_code, {})
        if raw_sku in m:
            return m[raw_sku], "alias_hit"
        if raw_sku in self.internal_universe.get(supplier_code, set()):
            return raw_sku, "identity"
        return None, "miss"


def load_snapshot(conn: sqlite3.Connection, dock_day: str | None = None) -> AliasSnapshot:
    """装载映射快照。dock_day 仅用于对账当日的内部码全集补全；
    identity 判定本身跨到货日有效，故预报原始码全量纳入。"""
    version = current_version(conn)
    snap = AliasSnapshot(version=version)
    for r in conn.execute("SELECT supplier_code, external_sku, internal_sku FROM aliases WHERE active=1"):
        snap.mapping.setdefault(r["supplier_code"], {})[r["external_sku"]] = r["internal_sku"]
        snap.internal_universe.setdefault(r["supplier_code"], set()).add(r["internal_sku"])
    # 预报腿的原始货号即内部货号（预报是内部口径），纳入内部码全集
    sql = "SELECT DISTINCT supplier_code, raw_sku FROM expect_lines WHERE raw_sku<>''"
    params: tuple = ()
    if dock_day is not None:
        sql += " AND dock_day=?"
        params = (dock_day,)
    for r in conn.execute(sql, params):
        snap.internal_universe.setdefault(r["supplier_code"], set()).add(r["raw_sku"])
    return snap


def normalize_on_load(conn: sqlite3.Connection, supplier_code: str, raw_sku: str) -> dict:
    """装载写路径规范化：与对账物化同源（基于当前版本即时快照）。

    仅返回解析结果供展示与落 raw_sku；merge 模式下对账运行仍会以钉版快照重算，
    避免装载后改别名导致口径漂移。
    """
    mode = get_mode(conn)
    raw_sku = (raw_sku or "").strip()
    if mode == "strict":
        return {"mode": "strict", "raw_sku": raw_sku, "sku": raw_sku, "status": "strict_raw"}
    snap = load_snapshot(conn)
    internal, status = snap.resolve(supplier_code, raw_sku)
    if status == "miss":
        # 装载不拦，miss 留给对账 alias_miss 桶；sku 先存原始码占位
        return {"mode": mode, "raw_sku": raw_sku, "sku": raw_sku, "status": "miss",
                "alias_version": snap.version}
    return {"mode": mode, "raw_sku": raw_sku, "sku": internal, "status": status,
            "alias_version": snap.version}
