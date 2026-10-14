# 开放接口（/api/v1）与 Cherry Studio 对接指南

本项目在网页端之外预留了一套 **API Token 认证的开放接口**，专供
Cherry Studio（MCP 对话）、脚本、自动化工具进行数据管理。
本文档说明：接口清单、Token 配置、MCP 服务器部署、对话用法示例。

---

## 一、开启方法（服务端）

1. 生成一个 Token（随机 48 位十六进制）：

   ```bash
   python -c "import secrets; print(secrets.token_hex(24))"
   ```

2. 写入项目根目录 `.env`（多个 Token 用英文逗号分隔，可分别发给不同设备）：

   ```ini
   API_TOKEN=a1b2c3...你生成的token...
   ```

3. 重启服务（本地重启 app.py；NAS 上重启容器）。

> 未配置 `API_TOKEN` 时，所有 /api/v1 接口返回 503（明确提示未启用），不会误放行。

## 二、接口清单（均需请求头 `Authorization: Bearer <token>`）

### 1. 物料搜索 `GET /api/v1/materials`

| 参数 | 说明 |
|---|---|
| `q` | 按名称/编码/型号模糊搜索 |
| `code` | 按物料编码精确查询（与 q 二选一，code 优先） |

```bash
curl -H "Authorization: Bearer $TOKEN" \
     "http://127.0.0.1:5000/api/v1/materials?q=STM32"
```

返回：`{"count": 2, "materials": [{id, name, code, model, category, package, brand, quantity, unit, warehouse_id}, ...]}`

### 2. 入库 `POST /api/v1/stock/in`

```json
{"material_id": 12, "quantity": 100, "remark": "采购单 PO-2026-001"}
```

返回：`{"ok": true, "material_id": 12, "quantity": 350}`（quantity 为入库后最新库存）

### 3. 出库 `POST /api/v1/stock/out`

请求体同入库。库存不足时返回 400 + `{"error": "库存不足，当前库存 5"}`，
**绝不扣成负数**（单语句原子扣减，并发安全）。

### 4. 库存流水 `GET /api/v1/stock/records?material_id=12`

`material_id` 可选，缺省返回全部，最近 500 条。每条含
`created_at / type(in入库,out出库) / quantity / operator / remark / material_name / material_code`。
通过 Token 的操作，`operator` 记录为 `api:xxxx***`（Token 前 4 位，不落全量，防泄露又能溯源）。

### 5. BOM 核对 `POST /api/v1/bom/check`

```json
{"items": [
  {"code": "C621395", "quantity": 2},
  {"model": "STM32F103C8T6", "quantity": 5},
  {"name": "贴片电阻 10K 0603", "quantity": 50}
]}
```

逐行匹配（优先级：**编码精确 > 型号精确 > 名称精确 > 名称模糊**），返回：

| status | 含义 |
|---|---|
| `ok` | 库存充足（附 available 当前库存） |
| `shortage` | 缺料，附 `shortage` 缺额数量 |
| `not_found` | 库里没有该物料 |
| `ambiguous` | 名称/型号匹配到多个，附 candidates，需改用 code 精确定位 |
| `invalid` | 该行入参格式错误 |

```json
{"summary": {"total": 3, "ok": 1, "shortage": 1, "not_found": 0, "ambiguous": 1, "invalid": 0},
 "results": [{"input": {...}, "need": 5, "status": "shortage", "shortage": 2, "material": {...}}, ...]}
```

单次上限 500 行。

### 6. 重复物料核对 `GET /api/v1/duplicates`

按 **编码 / 型号 / 名称** 三个维度分组，列出所有出现 ≥2 次的物料组（含每条记录的
id、数量、所在仓库），供人工确认后去重。

---

## 三、Cherry Studio 通过 MCP 对接（推荐）

项目根目录已自带 MCP 服务器 [mcp_server.py](../mcp_server.py)，把上述接口包装成
6 个对话工具：`search_materials` / `stock_in` / `stock_out` / `get_stock_records` /
`check_bom` / `find_duplicates`。

### 配置步骤

1. 本机安装 MCP SDK（一次）：

   ```bash
   pip install "mcp[cli]"
   ```

2. Cherry Studio → 设置 → MCP 服务器 → 添加：

   | 项 | 值 |
   |---|---|
   | 名称 | personal-erp |
   | 类型 | 标准输入输出（stdio） |
   | 命令 | `python` |
   | 参数 | `F:\Code\个人ERP管理\个人ERP管理\mcp_server.py`（填你的实际路径） |
   | 环境变量 | `ERP_API_BASE=http://127.0.0.1:5000`<br>`ERP_API_TOKEN=<服务端 .env 里的 token>` |

   > NAS 部署：`ERP_API_BASE` 填 `http://NAS_IP:端口`；
   > 上云后（见《Cloudflare部署方案》）：改填 `https://你的域名`，其余不动。

3. 保存后在对话窗口勾选启用 `personal-erp` 服务器即可。

### 对话用法示例

- 「查一下库存里有没有 STM32F103，还剩多少」
- 「帮我把 C621395 出库 5 个，备注给样板组」
- 「核对这份 BOM：STM32F103C8T6 x2、0603 电容 100nF x20、AMS1117-3.3 x4」
  → AI 会调用 check_bom，逐行告诉你 缺几个 / 没入库 / 有歧义
- 「帮我查一下有没有重复录入的物料」
- 「看下最近谁动过 C621395 的库存」

### 安全说明

- Token 等同操作密码，只填在 Cherry Studio 环境变量里，不要发到对话/群组。
- 所有 Token 操作在流水里留痕（operator=`api:xxxx***`），可审计。
- 出库动作建议在提示词里要求 AI 先 `search_materials` 确认 id 和数量，再执行
  `stock_out`，避免拿错物料。

---

## 四、与 Cloudflare 版的关系

迁移到 Cloudflare 后 /api/v1 路径与请求/响应格式保持不变（见
[Cloudflare部署方案.md](Cloudflare部署方案.md) 第四节），MCP 服务器只需改
`ERP_API_BASE` 即可无缝切换，无需改代码。
