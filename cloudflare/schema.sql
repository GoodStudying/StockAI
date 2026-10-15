-- 个人ERP - Cloudflare D1 建表脚本（幂等，可重复执行）
-- 使用方式：Cloudflare 控制台 → Storage & Databases → D1 → erp → Console → 粘贴本文件全部内容 → Execute
-- 说明：由本地版 db.py 的 SCHEMA_SQLITE 改写：AUTOINCREMENT 去掉（D1 无意义）、时间统一 UTC

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES warehouses(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 4),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
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
    image_path TEXT,          -- 阶段2 接入 R2 后存对象 key；本地版存磁盘路径
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS material_documents (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,   -- datasheet / user_manual / other
    file_path TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',   -- admin / user
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stock_records (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('in','out')),   -- in=入库 out=出库
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    operator TEXT,               -- 操作人（API 调用记为 api:xxxx***）
    remark TEXT,                 -- 备注
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_materials_name ON materials(name);
CREATE INDEX IF NOT EXISTS idx_materials_code ON materials(code);
CREATE INDEX IF NOT EXISTS idx_materials_wh ON materials(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_wh_parent ON warehouses(parent_id);
CREATE INDEX IF NOT EXISTS idx_records_mat ON stock_records(material_id);

-- 默认管理员 admin / admin123（pbkdf2:sha256 + 10万次迭代：Workers WebCrypto 迭代上限即10万，登录后请尽快改密）
INSERT OR IGNORE INTO users(username, password_hash, role)
VALUES ('admin', 'pbkdf2:sha256:100000$7m5DxMTM0jyIfEnz$67263b8deecfbbb8286d2418502981f183f3df5545ebd4b9199bddd2b229a42f', 'admin');
