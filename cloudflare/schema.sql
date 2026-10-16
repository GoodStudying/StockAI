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

-- ===== AI 对话 + 采购清单 模块（2026-10）：与本地 SQLite/PG 版同构 =====

-- 系统设置（key-value）：AI 接口配置（ai_base_url / ai_api_key / ai_model）
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- AI 对话记录：actions_json 存"待确认操作卡片"数组（人确认后服务端原地改状态）
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    actions_json TEXT,
    usage_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 采购清单条目（material_id 可空=未入库物料；name/code/model 快照；状态读时派生）
CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    code TEXT DEFAULT '',
    model TEXT DEFAULT '',
    need_qty INTEGER NOT NULL CHECK(need_qty > 0),
    unit TEXT DEFAULT '个',
    remark TEXT DEFAULT '',
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 采购批次（同一物料分多次采购，每笔一条；已购 = SUM(qty)）
CREATE TABLE IF NOT EXISTS purchase_batches (
    id INTEGER PRIMARY KEY,
    purchase_item_id INTEGER NOT NULL REFERENCES purchase_items(id) ON DELETE CASCADE,
    qty INTEGER NOT NULL CHECK(qty > 0),
    remark TEXT DEFAULT '',
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id, id);
CREATE INDEX IF NOT EXISTS idx_pitem_project ON purchase_items(project);
CREATE INDEX IF NOT EXISTS idx_pbatch_item ON purchase_batches(purchase_item_id);

-- 默认管理员 admin / admin123（pbkdf2:sha256 + 10万次迭代：Workers WebCrypto 迭代上限即10万，登录后请尽快改密）
INSERT OR IGNORE INTO users(username, password_hash, role)
VALUES ('admin', 'pbkdf2:sha256:100000$7m5DxMTM0jyIfEnz$67263b8deecfbbb8286d2418502981f183f3df5545ebd4b9199bddd2b229a42f', 'admin');
