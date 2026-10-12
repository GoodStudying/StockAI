# -*- coding: utf-8 -*-
"""
数据库抽象层
统一提供 conn() / execute / executemany / init_schema
当前使用 SQLite，切换到 PostgreSQL 只需修改 .env 的 DB_TYPE，
并实现 PostgresBackend 的 conn() 即可（占位符统一使用 ?，内部自动转换）。
"""
import os
import sqlite3
from config import DB_TYPE, SQLITE_PATH, POSTGRES_DSN

# ---------- 统一参数占位符 ----------
# 上层代码全部使用 ? 占位符；postgres 后端会替换为 %s
def _convert(sql: str) -> str:
    if DB_TYPE == "postgres":
        return sql.replace("?", "%s")
    return sql


class SQLiteBackend:
    def conn(self):
        os.makedirs(os.path.dirname(SQLITE_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(SQLITE_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c


class PostgresBackend:
    """PostgreSQL 后端（NAS Docker 部署使用）"""
    def conn(self):
        import psycopg2
        import psycopg2.extras
        c = psycopg2.connect(POSTGRES_DSN)
        return c

    @staticmethod
    def cursor(conn):
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# 统一取 cursor：sqlite 用默认（Row 工厂），postgres 用 RealDictCursor
def _cursor(conn):
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        # psycopg2 默认工厂即 tuple，改用 dict 工厂便于统一 dict(r)
        import psycopg2.extras
        cur.close()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return cur


_backend = PostgresBackend() if DB_TYPE == "postgres" else SQLiteBackend()


def get_conn():
    return _backend.conn()


def execute(sql, params=()):
    conn = get_conn()
    try:
        cur = _cursor(conn)
        cur.execute(_convert(sql), params)
        conn.commit()
        return cur
    except Exception:
        conn.rollback()
        raise
    finally:
        # SQLite 需保持连接直到 fetch 完；这里由调用方 fetchall 后关闭
        pass


def query(sql, params=()):
    """查询并返回 list[dict]"""
    conn = get_conn()
    try:
        cur = _cursor(conn)
        cur.execute(_convert(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def execute_write(sql, params=()):
    """写操作，返回 lastrowid / affected"""
    conn = get_conn()
    try:
        cur = _cursor(conn)
        # postgres：INSERT 自动追加 RETURNING id（无 RETURNING 语法时退化为 lastrowid=None）
        if DB_TYPE == "postgres":
            if sql.lstrip().upper().startswith("INSERT"):
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
            cur.execute(_convert(sql), params)
            if cur.description:
                row = cur.fetchone()
                last_id = list(dict(row).values())[0] if row else None
            else:
                last_id = None
        else:
            cur.execute(_convert(sql), params)
            last_id = cur.lastrowid
        conn.commit()
        return last_id, cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES warehouses(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 4),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT,                -- 物料编码 / 立创编号
    model TEXT,               -- 型号
    category TEXT,            -- 分类
    package TEXT,             -- 封装
    brand TEXT,               -- 品牌
    description TEXT,
    quantity INTEGER DEFAULT 0,
    unit TEXT DEFAULT '个',
    warehouse_id INTEGER REFERENCES warehouses(id),
    lcsc_url TEXT,            -- 立创商城链接
    image_path TEXT,          -- 立创图片本地路径（旧字段，保留兼容）
    image_blob BLOB,          -- 图片内容（存数据库，优先于 image_path）
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS material_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,   -- datasheet / user_manual / other
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_blob BLOB,           -- 文件内容（存数据库，与 file_path 二选一，优先 BLOB）
    uploaded_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',   -- admin / user
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS stock_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('in','out')),   -- in=入库 out=出库
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    operator TEXT,               -- 操作人
    remark TEXT,                 -- 备注
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_materials_name ON materials(name);
CREATE INDEX IF NOT EXISTS idx_materials_code ON materials(code);
CREATE INDEX IF NOT EXISTS idx_materials_wh ON materials(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_wh_parent ON warehouses(parent_id);
CREATE INDEX IF NOT EXISTS idx_records_mat ON stock_records(material_id);
"""


def ensure_default_admin():
    """首次启动时创建默认管理员 admin / admin123"""
    import hashlib
    if not query("SELECT id FROM users WHERE username='admin'"):
        pw = hashlib.sha256("admin123".encode()).hexdigest()
        execute_write("INSERT INTO users(username, password_hash, role) VALUES ('admin', ?, 'admin')", (pw,))


def run_migrations():
    """轻量迁移：补充新增列（已有表不重建）"""
    if DB_TYPE == "postgres":
        # postgres 的 BLOB 列迁移已在 init_schema 内完成；磁盘文件迁移同样执行
        _migrate_files_to_blob()
        return

    cols = {r["name"] for r in query("PRAGMA table_info(materials)")}
    if "image_path" not in cols:
        execute_write("ALTER TABLE materials ADD COLUMN image_path TEXT")
    if "image_blob" not in cols:
        execute_write("ALTER TABLE materials ADD COLUMN image_blob BLOB")

    doc_cols = {r["name"] for r in query("PRAGMA table_info(material_documents)")}
    if "file_blob" not in doc_cols:
        execute_write("ALTER TABLE material_documents ADD COLUMN file_blob BLOB")

    _migrate_files_to_blob()


def _migrate_files_to_blob():
    """
    存量文件入库迁移：把 uploads/ 下已存在的图片和手册读入数据库 BLOB。
    仅迁移一次（有 BLOB 的记录跳过），文件保留在磁盘作备份，新数据一律走 BLOB。
    """
    import config
    # 图片：materials.image_path -> image_blob
    for r in query("SELECT id, image_path FROM materials WHERE image_blob IS NULL AND image_path IS NOT NULL"):
        p = r["image_path"]
        if p and os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    execute_write("UPDATE materials SET image_blob=? WHERE id=?", (f.read(), r["id"]))
            except OSError:
                pass

    # 手册：material_documents.file_path -> file_blob
    for r in query("SELECT id, file_path FROM material_documents WHERE file_blob IS NULL"):
        p = r["file_path"]
        if p and os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    execute_write("UPDATE material_documents SET file_blob=? WHERE id=?", (f.read(), r["id"]))
            except OSError:
                pass



SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS warehouses (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES warehouses(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS materials (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT,
    model TEXT,
    category TEXT,
    package TEXT,
    brand TEXT,
    description TEXT,
    quantity INTEGER DEFAULT 0,
    unit TEXT DEFAULT '个',
    warehouse_id INTEGER REFERENCES warehouses(id),
    lcsc_url TEXT,
    image_path TEXT,
    image_blob BYTEA,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS material_documents (
    id SERIAL PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_blob BYTEA,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stock_records (
    id SERIAL PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('in','out')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    operator TEXT,
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_materials_name ON materials(name);
CREATE INDEX IF NOT EXISTS idx_materials_code ON materials(code);
CREATE INDEX IF NOT EXISTS idx_materials_wh ON materials(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_wh_parent ON warehouses(parent_id);
CREATE INDEX IF NOT EXISTS idx_records_mat ON stock_records(material_id);
"""


def _pg_columns(table):
    """postgres：返回表的列名集合（表不存在返回空集）"""
    return {r["column_name"] for r in query(
        "SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))}


def init_schema():
    conn = get_conn()
    try:
        if DB_TYPE == "sqlite":
            conn.executescript(SCHEMA_SQLITE)
            conn.commit()
        else:
            # postgres 逐条执行（psycopg2 无 executescript）
            import re as _re
            cur = conn.cursor()
            for stmt in _re.split(r";\s*\n", SCHEMA_POSTGRES):
                sql_one = stmt.strip()
                if sql_one and not sql_one.startswith("--"):
                    cur.execute(sql_one)
            conn.commit()
            # postgres 迁移：补 BLOB 列（老库升级）
            mats = _pg_columns("materials")
            if "image_blob" not in mats:
                cur.execute("ALTER TABLE materials ADD COLUMN image_blob BYTEA")
                conn.commit()
            docs = _pg_columns("material_documents")
            if "file_blob" not in docs:
                cur.execute("ALTER TABLE material_documents ADD COLUMN file_blob BYTEA")
                conn.commit()
    finally:
        conn.close()
