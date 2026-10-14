# Cloudflare 网页部署教程（全程无需本地命令行）

> 前提：代码已在本仓库（GitHub `GoodStudying/StockAI`）。部署后访问地址形如
> `https://stockai.<你的账户子域>.workers.dev`，以后 `git push` 到 master 自动重新部署。

---

## 第 1 步：创建 D1 数据库（约 2 分钟）

1. 登录 https://dash.cloudflare.com
2. 左侧菜单 **Storage & Databases → D1 SQL Database**
3. 点 **Create database**，名字填 `erp`，确认创建
4. 创建完成后进入数据库详情页，找到 **Database ID**（一串十六进制，形如 `a1b2c3d4-...`），**复制下来**

## 第 2 步：把 Database ID 填进仓库配置（约 2 分钟）

1. 打开 GitHub 本仓库的 `cloudflare/wrangler.toml` 文件
2. 点右上角 **铅笔图标**（编辑），把最后一行的 `请替换为你的D1数据库ID` 换成刚复制的 ID
3. 点 **Commit changes** 提交

## 第 3 步：导入仓库部署 Worker（约 3 分钟）

1. Cloudflare 控制台左侧 **Compute (Workers)** → **Create**（创建应用）
2. 选择 **Import a repository**（导入 Git 仓库）
3. 首次会要求授权 GitHub：点 **Connect GitHub**，在 GitHub 页面只勾选 `StockAI` 仓库并授权
4. 回到 Cloudflare 配置页：
   | 配置项 | 填写值 |
   |---|---|
   | Project name | `stockai` |
   | Production branch | `master` |
   | Root directory | `cloudflare`（**必须填，Worker 代码在这个子目录**） |
   | Build command | 留空（默认） |
   | Deploy command | `npx wrangler deploy`（默认） |
5. 点 **Save and Deploy**，等待 1~2 分钟构建完成（状态变 Success）

## 第 4 步：配置密钥（约 2 分钟，必做）

1. 进入刚部署的 Worker（Compute → stockai）→ **Settings** → **Variables and Secrets**
2. 点 **Add**，添加两个变量：

   | 名称 | 类型 | 值 |
   |---|---|---|
   | `WORKER_SECRET` | **Secret** | 随机 32 位以上字符串（生成方式见下方） |
   | `API_TOKEN` | Secret（可选） | 随机 48 位字符串，启用 Cherry Studio / 开放接口时用 |

   随机串生成方式（任选其一）：
   - 浏览器打开 https://www.random.org/strings/ 生成（勾选数字+字母，长度 32+）
   - 或用你密码管理器的密码生成器

3. 保存后**必须重新部署一次**才生效：Worker 页面 → **Deployments** → 最新一条 → 右侧菜单 **Retry deployment**

## 第 5 步：D1 建表 + 导入旧数据（约 3 分钟，必做）

> 顺序注意：先跑本步骤再访问页面，否则登录会报 `no such table: users`。
> ⚠ Console 直接粘贴 `schema.sql` 会报 "Requests without any query are not supported"
> （网页 Console 对带 `--` 注释的多语句解析有 bug），**请粘贴 `schema-console.sql`（无注释版）**。

1. **Storage & Databases → D1 → erp → Console** 标签页
2. 打开本仓库 `cloudflare/schema-console.sql`（**注意是带 -console 后缀的这个**），
   全选复制文件内容，粘贴进 Console 输入框，点 **Execute**
   （建 5 张表 + 默认管理员 `admin/admin123`）
3. （可选）导入旧数据：同样方式粘贴 `cloudflare/data-migrate-console.sql` 全文并 Execute
   （包含本地版的仓库/物料/流水/用户；旧 admin 密码若改过，见下方说明）
4. 若粘贴多语句仍报错：改用 Console 旁的 **Import** 按钮直接上传对应 .sql 文件（从 GitHub
   文件页面 Download 原始文件），或逐条粘贴执行

## 第 6 步：验证

1. 浏览器打开 `https://stockai.<你的账户子域>.workers.dev`
2. 用 `admin / admin123` 登录（**登录后请立即在 系统管理→账户 里改密码**）
3. 试一遍：仓库管理建库 → 物料查询 → 入库/出库 → 库存流水

---

## 旧数据说明（导入了 data-migrate.sql 的话）

- **admin 密码**：若本地旧库改过 admin 密码，云端仍是默认 `admin123`（旧密码哈希被幂等跳过）。
  登录后在 系统管理 → 账户管理 里重置为你常用的密码即可（云端与本地密码体系互通，格式一致）
- **图片/手册**：本地版的物料图片和 PDF 手册暂未迁移（阶段2 接入 R2 后处理），列表页不显示图不影响使用
- **立创抓取**：云端版暂不支持在线搜索/抓取立创数据（海外 IP 被风控），这些功能继续用本地/NAS 版

## Cherry Studio 接入云端

Worker 的 `/api/v1` 开放接口与本地版完全一致。本地 `mcp_server.py` 只需把环境变量改为：

```
ERP_API_BASE=https://stockai.<你的账户子域>.workers.dev
ERP_API_TOKEN=<第 4 步配置的 API_TOKEN>
```

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| 页面打不开/转圈 | `*.workers.dev` 国内直连不稳，挂代理或绑自定义域名（Cloudflare 海外边缘无需备案） |
| 登录报 `no such table: users` | 第 5 步没执行，去 D1 Console 跑 schema-console.sql |
| Console 报 `Requests without any query` | 粘贴的是带注释的 schema.sql，改用 **schema-console.sql**（无注释版），或用 Import 按钮上传文件 |
| 登录报 500 提示 WORKER_SECRET | 第 4 步没配，或配完没重新部署 |
| git push 后没更新 | Worker → Deployments 查看构建日志；确认 push 的是 master 分支 |
| 构建失败 `database_id` 相关 | 第 2 步的 ID 没替换或复制不完整 |
| 想改 Worker 名/域名 | wrangler.toml 里 `name = "stockai"` 改后 push 即可 |

## 架构说明（给未来的你）

- `src/index.ts`：入口，挂载三组路由 + 全局 JSON 错误处理
- `src/auth.ts`：登录/JWT 会话/密码哈希（pbkdf2，与本地 Flask 版互通，同一账号体系）
- `src/routes.ts`：网页端全部业务 API（仓库/物料/出入库/账户），与 Flask 版 1:1
- `src/apiv1.ts`：/api/v1 开放接口（Token 认证，Cherry Studio/MCP 用）
- `../static/`：前端页面由 Workers 静态资源直接托管，零改动
- 数据在 D1（控制台可随时 Export 备份 / Time Travel 恢复）
