from __future__ import annotations
from app.db import connect

# ---------------------------------------------------------------------------
# 架构说明（全局只贯彻一种口径，由 settings.reconcile_mode 决定）
#
# alias_merge（别名归并）：
#   外部码经 aliases 表在「同一供应商作用域」内映射到内部 sku，再按
#   (supplier_code, internal_sku) 比三腿；映射失败进 alias_miss 桶，绝不进
#   matched。批次物化时把当时的 alias_version 与全量映射 JSON 钉进快照，
#   事后改别名撬不动已 sealed 的批次。
#
# strict（严格码）：
#   只按原始码 (supplier_code, raw_sku) 比三腿；别名表仅展示不参与，
#   别名造成的表面差额进普通差额，由人工处置，禁止静默映射改键。
#
# 归并键恒带 supplier_code，严禁跨供应商同货号数量偷加总。
# ---------------------------------------------------------------------------

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alias_versions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
  id INTEGER PRIMARY KEY,
  supplier_code TEXT NOT NULL,
  external_sku TEXT NOT NULL,
  internal_sku TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(supplier_code, external_sku)
);
CREATE TABLE IF NOT EXISTS expect_lines (
  id INTEGER PRIMARY KEY,
  doc_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  raw_sku TEXT NOT NULL DEFAULT '',
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receive_lines (
  id INTEGER PRIMARY KEY,
  receive_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  raw_sku TEXT NOT NULL DEFAULT '',
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirm_lines (
  id INTEGER PRIMARY KEY,
  confirm_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  raw_sku TEXT NOT NULL DEFAULT '',
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  dock_day TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'alias_merge',
  alias_version INTEGER,
  alias_snapshot TEXT,
  matched_count INTEGER NOT NULL,
  mapped_count INTEGER NOT NULL DEFAULT 0,
  alias_hit_count INTEGER NOT NULL DEFAULT 0,
  alias_miss_count INTEGER NOT NULL DEFAULT 0,
  variance_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  sealed_at TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS match_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  raw_detail TEXT,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  aligned INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  raw_detail TEXT,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  fate TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alias_miss (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  leg TEXT NOT NULL,
  raw_sku TEXT NOT NULL,
  qty REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_spill (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
'''


def _has_column(conn, table: str, column: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _migrate(conn) -> None:
    """老库幂等加列；新库 SCHEMA 已含全部列。"""
    for table in ("expect_lines", "receive_lines", "confirm_lines"):
        if not _has_column(conn, table, "raw_sku"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN raw_sku TEXT NOT NULL DEFAULT ''")
            # 历史行：原始码即现存 sku
            conn.execute(f"UPDATE {table} SET raw_sku=sku WHERE raw_sku=''")
    if not _has_column(conn, "reconcile_batches", "mode"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN mode TEXT NOT NULL DEFAULT 'strict'")
    if not _has_column(conn, "reconcile_batches", "alias_version"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN alias_version INTEGER")
    if not _has_column(conn, "reconcile_batches", "alias_snapshot"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN alias_snapshot TEXT")
    if not _has_column(conn, "reconcile_batches", "mapped_count"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN mapped_count INTEGER NOT NULL DEFAULT 0")
    if not _has_column(conn, "reconcile_batches", "alias_hit_count"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN alias_hit_count INTEGER NOT NULL DEFAULT 0")
    if not _has_column(conn, "reconcile_batches", "alias_miss_count"):
        conn.execute("ALTER TABLE reconcile_batches ADD COLUMN alias_miss_count INTEGER NOT NULL DEFAULT 0")
    for table in ("match_rows", "variance_rows"):
        if not _has_column(conn, table, "raw_detail"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN raw_detail TEXT")
    # 老库若有 reconcile_batches 但没有版本行，补一条版本 0
    if conn.execute("SELECT COUNT(*) c FROM alias_versions").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO alias_versions(id, created_at, note) VALUES (0, '1970-01-01T00:00:00Z', '基线版本')"
        )


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)

        # 默认对账口径：别名归并。可在设置页切换为 strict。
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('reconcile_mode','alias_merge') "
            "ON CONFLICT(key) DO NOTHING"
        )

        fresh = conn.execute("SELECT COUNT(*) c FROM suppliers").fetchone()["c"] == 0
        if fresh:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('dock_day', ?) ON CONFLICT(key) DO NOTHING",
                ("2026-11-18",),
            )
            for code, title in [
                ("SUP-A", "东海食材"),
                ("SUP-B", "北岭包装"),
                ("SUP-C", "南湾百货"),
            ]:
                conn.execute("INSERT INTO suppliers(code, title) VALUES (?,?)", (code, title))
            conn.execute(
                "INSERT INTO alias_versions(id, created_at, note) VALUES (1, '2026-11-18T07:00:00Z', '初始别名版本')"
            )

        # 别名（仅在同供应商作用域内生效）。幂等：按 (supplier_code, external_sku) 去重。
        aliases = [
            ("SUP-A", "ALIAS-RICE-25", "SKU-米-25"),
            ("SUP-A", "ALIAS-OIL-5", "SKU-油-5"),
            ("SUP-C", "ALIAS-TOWEL", "SKU-巾-卷"),
        ]
        for supplier_code, external_sku, internal_sku in aliases:
            exists = conn.execute(
                "SELECT 1 FROM aliases WHERE supplier_code=? AND external_sku=?",
                (supplier_code, external_sku),
            ).fetchone()
            if not exists:
                conn.execute(
                    '''INSERT INTO aliases(supplier_code, external_sku, internal_sku, active, updated_at)
                       VALUES (?,?,?,1,'2026-11-18T07:00:00Z')''',
                    (supplier_code, external_sku, internal_sku),
                )

        # 演示三腿：SUP-A 米走别名码；SUP-C 回执走别名码；SUP-B 缺映射演示 alias_miss。
        expects = [
            ("EX-1001", "SUP-A", "SKU-米-25", "2026-11-18", 40, "早班预报"),
            ("EX-1002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("EX-1003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            ("EX-1004", "SUP-C", "SKU-巾-卷", "2026-11-18", 60, None),
            ("EX-0901", "SUP-A", "SKU-米-25", "2026-11-17", 10, "昨日"),
        ]
        for doc_no, supplier_code, sku, dock_day, qty, note in expects:
            if not conn.execute("SELECT 1 FROM expect_lines WHERE doc_no=?", (doc_no,)).fetchone():
                conn.execute(
                    '''INSERT INTO expect_lines(doc_no, supplier_code, sku, raw_sku, dock_day, qty, note, created_at)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (doc_no, supplier_code, sku, sku, dock_day, qty, note, "2026-11-18T08:00:00Z"),
                )

        receives = [
            # 实收用供应商别名码 ALIAS-RICE-25，归并后应对齐 SKU-米-25（38 对预报 40 → 短 2 差额）
            ("RC-2001", "SUP-A", "ALIAS-RICE-25", "2026-11-18", 38, "2026-11-18T09:10:00Z", "少2"),
            ("RC-2002", "SUP-A", "SKU-油-5", "2026-11-18", 20, "2026-11-18T09:20:00Z", None),
            ("RC-2003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, "2026-11-18T10:00:00Z", None),
            # 供应商 B 的外部码，aliases 表无映射 → alias_miss，不得进 matched
            ("RC-2005", "SUP-B", "EXT-BOX-UNKNOWN", "2026-11-18", 7, "2026-11-18T10:20:00Z", "缺映射演示"),
            ("RC-2004", "SUP-C", "SKU-巾-卷", "2026-11-18", 66, "2026-11-18T11:00:00Z", "多6"),
        ]
        for receive_no, supplier_code, raw_sku, dock_day, qty, occurred_at, note in receives:
            if not conn.execute("SELECT 1 FROM receive_lines WHERE receive_no=?", (receive_no,)).fetchone():
                conn.execute(
                    '''INSERT INTO receive_lines(receive_no, supplier_code, sku, raw_sku, dock_day, qty, occurred_at, note, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (receive_no, supplier_code, raw_sku, raw_sku, dock_day, qty, occurred_at, note, occurred_at),
                )

        confirms = [
            ("CF-3001", "SUP-A", "ALIAS-RICE-25", "2026-11-18", 38, "回执按实收（别名码）"),
            ("CF-3002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("CF-3003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            # 回执别名码 ALIAS-TOWEL → SKU-巾-卷（60），与实收 66 形成差额
            ("CF-3004", "SUP-C", "ALIAS-TOWEL", "2026-11-18", 60, "回执仍报预报数"),
        ]
        for confirm_no, supplier_code, raw_sku, dock_day, qty, note in confirms:
            if not conn.execute("SELECT 1 FROM confirm_lines WHERE confirm_no=?", (confirm_no,)).fetchone():
                conn.execute(
                    '''INSERT INTO confirm_lines(confirm_no, supplier_code, sku, raw_sku, dock_day, qty, note, created_at)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (confirm_no, supplier_code, raw_sku, raw_sku, dock_day, qty, note, "2026-11-18T12:00:00Z"),
                )
        conn.commit()
    finally:
        conn.close()
