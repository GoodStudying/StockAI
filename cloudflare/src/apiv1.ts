/**
 * 开放接口 /api/v1（供 Cherry Studio / MCP / 脚本程序化调用）
 * 认证：请求头 Authorization: Bearer <API_TOKEN>（环境变量 API_TOKEN，逗号分隔多个）
 * 与 Flask 版的 /api/v1 请求/响应格式完全一致，mcp_server.py 改个 ERP_API_BASE 即可切云端。
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { getJson, parseIntSafe } from "./auth";
import { q, applyStock, bomCheckCore } from "./core";
import { MAX_WAREHOUSE_LEVEL } from "./routes";

/** 建库核心：网页端 / AI / 开放接口同一层级规则（一级库 parent_id 落 NULL） */
async function createWarehouse(db: D1Database, nameRaw: unknown, parentRaw: unknown):
  Promise<{ row?: any; error?: string; status?: 400 | 404 }> {
  const name = String(nameRaw ?? "").trim();
  if (!name) return { error: "库名不能为空", status: 400 };
  const parentId = parentRaw ? parseIntSafe(parentRaw, 1) : 0;
  if (parentRaw && parentId === null) return { error: "parent_id 必须是正整数", status: 400 };
  let level = 1;
  if (parentId) {
    const parent = await q<{ level: number }>(db, "SELECT level FROM warehouses WHERE id=?", parentId);
    if (!parent.length) return { error: `父级库 #${parentId} 不存在`, status: 400 };
    level = parent[0].level + 1;
  }
  if (level > MAX_WAREHOUSE_LEVEL) {
    return { error: `最多支持 ${MAX_WAREHOUSE_LEVEL} 级库（父级已是 ${MAX_WAREHOUSE_LEVEL} 级）`, status: 400 };
  }
  const r = await db.prepare("INSERT INTO warehouses(name, parent_id, level) VALUES (?,?,?)")
    .bind(name, parentId || null, level).run();
  return { row: { id: r.meta.last_row_id, name, parent_id: parentId || null, level } };
}

const v1 = new Hono<AppEnv>();

/** 常数时间字符串比较（对应 Python hmac.compare_digest，防时序侧信道逐字节泄露） */
function safeEq(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ---------------- Token 认证中间件 ----------------
v1.use("/api/v1/*", async (c, next) => {
  const tokens = (c.env.API_TOKEN ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!tokens.length) {
    return c.json({ error: "服务端未配置 API_TOKEN（Worker Settings → Variables and Secrets 添加后生效）" }, 503);
  }
  const auth = c.req.header("Authorization") ?? "";
  const tok = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!tok || !tokens.some((t) => safeEq(tok, t))) {
    return c.json({ error: "API Token 无效或缺失（请求头 Authorization: Bearer <token>）" }, 401);
  }
  // 流水操作人留痕：只记前 4 位，不落完整 token
  c.set("apiOperator", `api:${tok.slice(0, 4)}***`);
  await next();
});

// ---------------- 1. 物料搜索 ----------------
v1.get("/api/v1/materials", async (c) => {
  const code = (c.req.query("code") ?? "").trim();
  const kw = (c.req.query("q") ?? "").trim();
  const cols = "id, name, code, model, category, package, brand, quantity, unit, warehouse_id, location_code";
  let rows: any[];
  if (code) {
    rows = await q(c.env.DB, `SELECT ${cols} FROM materials WHERE code=? LIMIT 50`, code);
  } else if (kw) {
    const like = `%${kw}%`;
    rows = await q(c.env.DB,
      `SELECT ${cols} FROM materials WHERE name LIKE ? OR code LIKE ? OR model LIKE ? OR location_code LIKE ? ORDER BY id DESC LIMIT 50`,
      like, like, like, like);
  } else {
    return c.json({ error: "请提供 q 或 code 参数" }, 400);
  }
  return c.json({ count: rows.length, materials: rows });
});

// ---------------- 2/3. 出入库（核心逻辑见 core.ts 的 applyStock，与网页端共用） ----------------
v1.post("/api/v1/stock/:op", async (c) => {
  const op = c.req.param("op");
  if (op !== "in" && op !== "out") return c.json({ error: "操作类型不合法" }, 400);
  const d = await getJson(c);
  const mid = parseIntSafe(d.material_id, 1);
  const qty = parseIntSafe(d.quantity, 1);
  if (mid === null) return c.json({ error: "material_id 不合法" }, 400);
  if (qty === null) return c.json({ error: "数量必须是大于0的整数" }, 400);
  if (op !== "in" && (d.warehouse_id || d.warehouse_name)) {
    return c.json({ error: "出库不支持指定仓库（仓库归属在入库时指定）" }, 400);
  }
  // 入库挂库：id 必须已存在；名字按一级库匹配，不存在则自动创建（同名复用）
  let wid: number | null = null;
  let autoCreated = false;
  if (d.warehouse_id) {
    wid = parseIntSafe(d.warehouse_id, 1);
    if (wid === null) return c.json({ error: "warehouse_id 必须是正整数" }, 400);
    const wh = await q(c.env.DB, "SELECT id FROM warehouses WHERE id=?", wid);
    if (!wh.length) return c.json({ error: `仓库 #${wid} 不存在` }, 400);
  } else if (d.warehouse_name !== undefined) {
    const wn = String(d.warehouse_name ?? "").trim();
    if (!wn) return c.json({ error: "warehouse_name 不能为空" }, 400);
    const rows = await q<{ id: number }>(c.env.DB,
      "SELECT id FROM warehouses WHERE name=? AND (parent_id IS NULL OR parent_id=0)", wn);
    if (rows.length) {
      wid = rows[0].id;
    } else {
      const created = await createWarehouse(c.env.DB, wn, 0);
      if (created.error) return c.json({ error: created.error }, created.status ?? 400);
      wid = created.row.id;
      autoCreated = true;
    }
  }
  const r = await applyStock(c.env.DB, op, mid, qty, c.get("apiOperator") ?? "api", String(d.remark ?? ""));
  if (r.err) return c.json({ error: r.err[0] }, r.err[1] as 400 | 404);
  const resp: Record<string, unknown> = { ok: true, material_id: mid, quantity: r.qty };
  if (wid !== null) {
    await c.env.DB.prepare("UPDATE materials SET warehouse_id=? WHERE id=?").bind(wid, mid).run();
    const wh = await q<{ name: string }>(c.env.DB, "SELECT name FROM warehouses WHERE id=?", wid);
    resp.warehouse_id = wid;
    resp.warehouse_name = wh[0]?.name;
    if (autoCreated) resp.warehouse_auto_created = true;
  }
  return c.json(resp);
});

// ---------------- 4. 库存流水 ----------------
v1.get("/api/v1/stock/records", async (c) => {
  const mid = c.req.query("material_id");
  let sql = `SELECT r.*, m.name AS material_name, m.code AS material_code
             FROM stock_records r JOIN materials m ON r.material_id = m.id`;
  const params: unknown[] = [];
  if (mid) { sql += " WHERE r.material_id = ?"; params.push(Number(mid) || 0); }
  sql += " ORDER BY r.id DESC LIMIT 500";
  return c.json({ records: await q(c.env.DB, sql, ...params) });
});

// ---------------- 5. BOM 核对（核心逻辑见 core.ts 的 bomCheckCore，网页端/AI 共用） ----------------

v1.post("/api/v1/bom/check", async (c) => {
  const d = await getJson(c);
  const r = await bomCheckCore(c.env.DB, d.items);
  if ("error" in r) return c.json({ error: r.error }, r.status as 400);
  return c.json(r);
});

// ---------------- 6. 重复物料核对 ----------------
v1.get("/api/v1/duplicates", async (c) => {
  const groups: any[] = [];
  for (const [field, label] of [["code", "编码"], ["model", "型号"], ["name", "名称"]] as const) {
    const dup = await q<{ v: string; c: number }>(
      c.env.DB,
      `SELECT ${field} AS v, COUNT(*) AS c FROM materials
       WHERE ${field} IS NOT NULL AND TRIM(${field}) <> ''
       GROUP BY ${field} HAVING COUNT(*) > 1 ORDER BY c DESC`);
    for (const r of dup) {
      const mats = await q(
        c.env.DB,
        `SELECT id, name, code, model, quantity, unit, warehouse_id FROM materials WHERE ${field}=? ORDER BY id`,
        r.v);
      groups.push({ field, field_label: label, value: r.v, count: r.c, materials: mats });
    }
  }
  return c.json({ total_groups: groups.length, duplicates: groups });
});

// ---------------- 7. 仓库列表 / 新建仓库（MCP 建库、挂库前取 id） ----------------
v1.get("/api/v1/warehouses", async (c) => {
  const rows = await q(c.env.DB, "SELECT id, name, parent_id, level FROM warehouses ORDER BY level, id");
  return c.json({ count: rows.length, warehouses: rows });
});

v1.post("/api/v1/warehouses", async (c) => {
  const d = await getJson(c);
  const r = await createWarehouse(c.env.DB, d.name, d.parent_id);
  if (r.error) return c.json({ error: r.error }, r.status ?? 400);
  return c.json({ ok: true, ...r.row }, 201);
});

// ---------------- 8. 删除物料（与网页端同语义：库存随删除一并清空） ----------------
v1.delete("/api/v1/materials/:id", async (c) => {
  const mid = Number(c.req.param("id"));
  const rows = await q<{ name: string; quantity: number }>(
    c.env.DB, "SELECT name, quantity FROM materials WHERE id=?", mid);
  if (!rows.length) return c.json({ error: "物料不存在" }, 404);
  await c.env.DB.prepare("DELETE FROM materials WHERE id=?").bind(mid).run();
  const resp: Record<string, unknown> = { ok: true, material_id: mid, name: rows[0].name };
  if (rows[0].quantity > 0) resp.discarded_stock = rows[0].quantity;
  return c.json(resp);
});

export default v1;
