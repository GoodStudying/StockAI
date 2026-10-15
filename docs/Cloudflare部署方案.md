# Cloudflare 部署方案（Workers + D1 + R2）

> 目标：把现有 Flask + SQLite + Docker(NAS) 架构迁移到 Cloudflare 全球边缘网络，
> 数据放 Cloudflare D1（托管 SQLite），文件（物料图片 / 手册 PDF）放 R2 对象存储。
> 本文档同时给出迁移路径、API 映射、认证兼容、风险取舍与分阶段实施计划。

---

## 一、总体架构

```
浏览器 / Cherry Studio / MCP
        │
        ▼
Cloudflare CDN（全球边缘节点）
        │
        ├── 静态页面（static/）        → Worker Assets（原 Pages 静态资源）
        ├── /api/*  业务接口           → Worker（Hono 路由，替代 Flask）
        ├── /api/v1/* 开放接口(Token)  → 同一 Worker，逻辑与现在完全一致
        │        │
        │        ├── 结构化数据        → D1（SQLite 语法，免运维，自动只读副本）
        │        └── 图片/手册 BLOB    → R2（对象存储；D1 不适合存大二进制）
        │
        └── GitHub Actions（push master → wrangler deploy，替代现在的"在线更新"）
```

### 组件选型说明

| 现有组件 | 云端替代 | 理由 |
|---|---|---|
| Flask (Python) | **Workers**（TypeScript + Hono） | 冷启动毫秒级、免服务器；Pages Functions 已被官方导向 Worker Assets，新项目直接用 Workers |
| SQLite 文件 | **D1** | Cloudflare 托管 SQLite，SQL 语法几乎一致，自带备份/时间旅行恢复 |
| BLOB 存在 SQLite 里 | **R2** | D1 单行上限 1MB(建议)、单库 10GB；PDF 手册放 R2，D1 只存 `r2_key` |
| Docker + NAS | 不需要 | 云端免运维；NAS 版可保留作备份/抓取节点 |
| GitHub 在线更新（app.py 内置） | **GitHub Actions + wrangler-action** | 代码推上 GitHub 自动部署，云端无需自更新逻辑 |
| gunicorn 多进程 | 无概念 | Workers 天然多实例，且共享 D1 |

---

## 二、目录结构（建议新增 `cloudflare/` 目录，与 Flask 版并存过渡）

```
cloudflare/
├── wrangler.toml            # Worker 配置（绑定 D1 / R2 / 环境变量）
├── package.json
├── schema.sql               # D1 建表语句（由 db.py 的 SCHEMA_SQLITE 改写）
├── scripts/
│   └── migrate-sqlite-to-d1.py   # 本地 SQLite → D1 + R2 一次性迁移脚本
└── src/
    ├── index.ts             # 入口：Hono app，挂全部路由
    ├── auth.ts              # 登录 / 会话 JWT / API Token 校验
    ├── materials.ts         # 物料 CRUD / 搜索
    ├── stock.ts             # 出入库（原子扣减 + 流水）
    ├── warehouses.ts        # 仓库层级
    ├── bom.ts               # BOM 核对 / 重复核对（/api/v1）
    ├── files.ts             # R2 图片与手册的上传/下载
    └── update.ts            # 不迁移：由 GitHub Actions 取代
```

`wrangler.toml` 关键配置示例：

```toml
name = "personal-erp"
main = "src/index.ts"
compatibility_date = "2026-01-01"
assets = { directory = "../static", binding = "ASSETS" }   # 前端页面原样上云

[[d1_databases]]
binding = "DB"
database_name = "erp"
database_id = "<wrangler d1 create 后填入>"

[[r2_buckets]]
binding = "FILES"
bucket_name = "erp-files"

[vars]
# 非敏感配置；敏感值用 wrangler secret put
# API_TOKEN / WORKER_SECRET / GITHUB_TOKEN 走 secret
```

---

## 三、数据迁移（SQLite → D1 + R2）

### 3.1 结构化数据

1. 建库：`wrangler d1 create erp`，把返回的 `database_id` 填进 wrangler.toml
2. 建表：`wrangler d1 execute erp --remote --file=schema.sql`
   - `AUTOINCREMENT` 去掉（D1 兼容但无意义，`INTEGER PRIMARY KEY` 本身自增）
   - `datetime('now','localtime')` 改 `CURRENT_TIMESTAMP`（UTC，展示层转时区）
3. 导数据：迁移脚本逐表 `SELECT` → 生成 `INSERT` → `wrangler d1 execute erp --remote --file=data.sql`
   - 或用 `wrangler d1 execute erp --remote --command "..."` 逐批执行

### 3.2 BLOB（图片 / 手册 PDF）

迁移脚本对 `image_blob` / `file_blob` 非空的记录：

```
读出 bytes → wrangler r2 object put erp-files/materials/<id>.png --file=tmp
           → UPDATE materials SET image_r2_key='materials/<id>.png', image_blob=NULL
```

D1 新表字段：`image_r2_key TEXT`、`file_r2_key TEXT`（替代 image_blob/file_blob）。

### 3.3 用户密码（可无缝兼容）

密码哈希为 werkzeug 的 `pbkdf2:sha256:100000$salt$hash` 格式（10 万次迭代）。
Worker 端用 Web Crypto 原生能力即可校验，**用户无需改密码**：

```ts
// 解析 "pbkdf2:sha256:100000$<salt>$<hash_b64>"，
// crypto.subtle.deriveBits(PBKDF2-SHA256, 密码, salt, 100000) 与存量 hash 比对
```

> ★ 历史踩坑记录（2026-10 线上实测）：Workers 的 WebCrypto **硬性上限 10 万次迭代**，
> 26 万次的旧哈希会抛 `iteration counts above 100000 are not supported` 且被 try/catch
> 吞成"密码错误"。因此两端统一 10 万次；旧的 26 万次哈希只能在本地 Flask 端验证
> （werkzeug 格式自带迭代数，本地不受影响），若导入云端需先重置密码。

---

## 四、API 映射（路由不变，前端零改动）

| Flask 路由 | Worker 处理 | 备注 |
|---|---|---|
| `/` + static/* | Worker Assets | 直接复用现有 static/ |
| `/api/login` `/api/logout` `/api/me` | auth.ts | 会话改 JWT(HS256) 存 HttpOnly Cookie，`WORKER_SECRET` 签名 |
| `/api/warehouses` CRUD | warehouses.ts | 递归查子库逻辑用一条 SQL 递归 CTE（`WITH RECURSIVE`，D1 支持） |
| `/api/materials` CRUD | materials.ts | LIKE 搜索一致 |
| `/api/stock/<op>` `/api/stock/records` | stock.ts | 原子扣减：D1 单语句 `UPDATE ... WHERE quantity>=?` 判断 rowcount，天然防超卖 |
| `/api/materials/<id>/image` `/api/documents/<id>` | files.ts | 从 R2 取流返回；下载重命名逻辑照搬 |
| `/api/lcsc/*`、fetch_datasheet/fetch_image | **暂不迁移** | 见第六节风险说明 |
| `/api/users`、`/api/db/info` | 照搬 | `/api/db/backup` 改为 `wrangler d1 export` 说明页 |
| `/api/update/*` | 删除 | GitHub Actions 取代 |
| **`/api/v1/*`（Cherry Studio 用）** | bom.ts + stock.ts | 与现版请求/响应格式完全一致，mcp_server.py 只需改 `ERP_API_BASE` 即可切到云端 |

---

## 五、认证与安全

1. **网页会话**：登录成功后签发 JWT（含 id/username/role，2 小时有效期），
   HttpOnly + Secure + SameSite=Lax Cookie；`WORKER_SECRET` 用 `openssl rand -hex 32` 生成，
   `wrangler secret put WORKER_SECRET` 注入。
2. **API Token（/api/v1）**：与现版相同的 Bearer Token 方案；
   `wrangler secret put API_TOKEN`。HMAC 比对逻辑照搬。
3. **登录限流**：改用 Cloudflare 原生 WAF Rate Limiting 规则（如 `/api/login` 每 IP 10次/5分钟），
   免费 plan 即可配置，比代码内实现更可靠。
4. **D1 权限**：只有该 Worker 能访问绑定的 D1，外界无法直连。

---

## 六、立创商城抓取——主要风险点（重要）

现版 `lcsc.py` 依赖 `requests` 访问 `lcsc.com` / `item.szlcsc.com` / Bing 搜索。
**Worker 的出口 IP 在海外**，实测大概率遇到：

- 中文站 `item.szlcsc.com` 对海外 IP 风控/验证码
- Bing 国际版结果结构与国内版不同，反查 productId 可能失效
- Worker 单次请求 CPU 上限 10ms（免费版），解析大 HTML 可能超限（付费版 30s 墙钟但 CPU 仍受限）

**对策（按推荐顺序）：**

1. **抓取留在内网（推荐）**：NAS 版仅保留 `lcsc.py` 抓取功能，Worker 通过一个只有你自己知道的
   `NAS_PROXY_URL` secret 调 NAS 转发（NAS 需公网可达，如 DDNS + HTTPS）。
   云端负责库存主数据，抓到后写入 Worker API。
2. 抓取逻辑改前端浏览器执行（CORS 大概率被立创拦，不乐观）。
3. 放弃在线抓取，改手动录入 + 直接粘贴手册 URL（R2 直存）。

---

## 七、MCP / Cherry Studio 上云后的接入

- **方式 A（零改动）**：现有 `mcp_server.py` 继续本地跑，仅把环境变量改为
  `ERP_API_BASE=https://你的域名` —— /api/v1 格式完全一致。
- **方式 B（二期）**：Worker 内直接挂 `/mcp` 路由（streamable HTTP MCP，
  `@modelcontextprotocol/sdk` 有 Workers 适配），Cherry Studio 填 URL + Bearer Token 即连，
  连本地脚本都不用跑。工具定义与 mcp_server.py 保持一致。

---

## 八、CI/CD（替代在线更新）

`.github/workflows/deploy.yml`：

```yaml
on:
  push:
    branches: [master]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: cloudflare/wrangler-action@v3
        with:
          apiToken: ${{ secrets.CLOUDFLARE_API_TOKEN }}   # 只给 Workers Scripts:Edit 权限
          workingDirectory: cloudflare
```

以后「改代码 → git push → 30 秒后全球生效」，版本号即 git sha，不再需要 .version 与自更新。

---

## 九、费用与额度（免费档，个人 ERP 绰绰有余）

| 项目 | 免费额度 |
|---|---|
| Workers | 10 万请求/天 |
| D1 | 5 GB 存储，500 万行读/天，10 万行写/天 |
| R2 | 10 GB 存储，永久免费出口流量 |
| Worker Assets | 静态资源请求免费 |

超出再考虑 $5/月 Workers Paid，短期完全用不到。

---

## 十、分阶段实施计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| 1 | `cloudflare/` 骨架：Hono + D1 建表 + 登录/会话 + 物料/仓库/出入库 CRUD | 网页端全功能可用 |
| 2 | R2 文件通道 + 迁移脚本跑通（含 BLOB 搬家） | 历史图片/手册可看可下 |
| 3 | /api/v1 五个开放接口照搬 + mcp_server.py 指向云端 | Cherry Studio 对话操作成功 |
| 4 | GitHub Actions 部署 + 自定义域名绑定 | push 即上线 |
| 5（可选） | Worker 原生 /mcp 端点；NAS 抓取代理打通 | 全云端 |

> 域名说明：workers.dev 默认域名国内直连不稳，建议绑定自有域名（Cloudflare 海外边缘无需备案）。

---

## 十一、风险与取舍总结

1. **抓取功能是最大不确定项**（第六节），方案上已隔离为核心数据管理（上云）+ 抓取（留内网）。
2. D1 写入有最终一致性窗口（不同边缘读副本秒级延迟），个人场景无感；
   出入库原子性由单语句 UPDATE 保证，不受影响。
3. 数据主权：随时 `wrangler d1 export erp --remote --output=backup.sql` 导出全量，
   不锁定。
4. 双轨过渡期：Flask 版（NAS）与 Worker 版可并存，D1 为主库后 NAS 版停用或仅作抓取节点。
