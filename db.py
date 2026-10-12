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
    """PostgreSQL 后端（预留接口）。安装 psycopg2-binary 后可用。"""
    def conn(self):
        import psycopg2
        import psycopg2.extras
        c = psycopg2.connect(POSTGRES_DSN)
        return c

    @staticmethod
    def cursor(conn):
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


_backend = PostgresBackend() if DB_TYPE == "postgres" else SQLiteBackend()


def get_conn():
    return _backend.conn()


def execute(sql, params=()):
    conn = get_conn()
    try:
        cur = conn.cursor()
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
        cur = conn.cursor()
        cur.execute(_convert(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def execute_write(sql, params=()):
    """写操作，返回 lastrowid / affected"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(_convert(sql), params)
        conn.commit()
        last_id = cur.lastrowid if DB_TYPE == "sqlite" else None
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
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS material_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,   -- datasheet / user_manual / other
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
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

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS warehouses (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES warehouses(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- ... 其余表结构同 sqlite（类型作对应调整） ...
"""


def init_schema():
    conn = get_conn()
    try:
        if DB_TYPE == "sqlite":
            conn.executescript(SCHEMA_SQLITE)
            conn.commit()
        else:
            cur = conn.cursor()
            # 实际切换时按 SCHEMA_POSTGRES 逐条执行
            cur.execute(_convert(SCHEMA_POSTGRES))
            conn.commit()
    finally:
        conn.close()
