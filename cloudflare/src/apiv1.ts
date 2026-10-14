/**
 * 开放接口 /api/v1（供 Cherry Studio / MCP / 脚本程序化调用）
 * 认证：请求头 Authorization: Bearer <API_TOKEN>（环境变量 API_TOKEN，逗号分隔多个）
 * 与 Flask 版的 /api/v1 请求/响应格式完全一致，mcp_server.py 改个 ERP_API_BASE 即可切云端。
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { getJson, parseIntSafe } from "./auth";

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

/** D1 查询 helper */
async function q<T = any>(db: D1Database, sql: string, ...params: unknown[]): Promise<T[]> {
  const r = await db.prepare(sql).bind(...params).all<T>();
  return r.results ?? [];
}

// ---------------- 1. 物料搜索 ----------------
v1.get("/api/v1/materials", async (c) => {
  const code = (c.req.query("code") ?? "").trim();
  const kw = (c.req.query("q") ?? "").trim();
  const cols = "id, name, code, model, category, package, brand, quantity, unit, warehouse_id";
  let rows: any[];
  if (code) {
    rows = await q(c.env.DB, `SELECT ${cols} FROM materials WHERE code=? LIMIT 50`, code);
  } else if (kw) {
    const like = `%${kw}%`;
    rows = await q(c.env.DB,
      `SELECT ${cols} FROM materials WHERE name LIKE ? OR code LIKE ? OR model LIKE ? ORDER BY id DESC LIMIT 50`,
      like, like, like);
  } else {
    return c.json({ error: "请提供 q 或 code 参数" }, 400);
  }
  return c.json({ count: rows.length, materials: rows });
});

// ---------------- 2/3. 出入库（与网页端同一原子逻辑） ----------------
async function applyStock(
  db: D1Database, op: "in" | "out", mid: number, qty: number, operator: string, remark: string,
): Promise<{ qty?: number; err?: [string, number] }> {
  const mat = await db.prepare("SELECT id FROM materials WHERE id=?").bind(mid).first();
  if (!mat) return { err: ["物料不存在", 404] };
  const ts = new Date().toISOString().slice(0, 19).replace("T", " ");
  const updSql = op === "out"
    ? "UPDATE materials SET quantity=quantity-?, updated_at=? WHERE id=? AND quantity>=?"
    : "UPDATE materials SET quantity=quantity+?, updated_at=? WHERE id=?";
  const updParams: unknown[] = op === "out" ? [qty, ts, mid, qty] : [qty, ts, mid];
  const upd = await db.prepare(updSql).bind(...updParams).run();
  if ((upd.meta.changes ?? 0) === 0) {
    const cur = await db.prepare("SELECT quantity FROM materials WHERE id=?").bind(mid).first<any>();
    return { err: [`库存不足，当前库存 ${cur?.quantity ?? 0}`, 400] };
  }
  await db.prepare(
    "INSERT INTO stock_records(material_id, type, quantity, operator, remark) VALUES (?,?,?,?,?)")
    .bind(mid, op, qty, operator, remark).run();
  const after = await db.prepare("SELECT quantity FROM materials WHERE id=?").bind(mid).first<any>();
  return { qty: after?.quantity ?? 0 };
}

v1.post("/api/v1/stock/:op", async (c) => {
  const op = c.req.param("op");
  if (op !== "in" && op !== "out") return c.json({ error: "操作类型不合法" }, 400);
  const d = await getJson(c);
  const mid = parseIntSafe(d.material_id, 1);
  const qty = parseIntSafe(d.quantity, 1);
  if (mid === null) return c.json({ error: "material_id 不合法" }, 400);
  if (qty === null) return c.json({ error: "数量必须是大于0的整数" }, 400);
  const r = await applyStock(c.env.DB, op, mid, qty, c.get("apiOperator") ?? "api", String(d.remark ?? ""));
  if (r.err) return c.json({ error: r.err[0] }, r.err[1] as 400 | 404);
  return c.json({ ok: true, material_id: mid, quantity: r.qty });
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

// ---------------- 5. BOM 核对 ----------------

/** BOM 条目匹配：编码精确 > 型号精确 > 名称精确 > 名称模糊（与 Flask 版 _match_material 一致） */
async function matchMaterial(db: D1Database, item: Record<string, any>):
  Promise<{ mat: any | null; how: string; cands: any[] }> {
  const code = typeof item.code === "string" ? item.code.trim() : "";
  const model = typeof item.model === "string" ? item.model.trim() : "";
  const name = typeof item.name === "string" ? item.name.trim() : "";
  const cols = "id, name, code, model, quantity, unit, warehouse_id";
  if (code) {
    const rows = await q(db, `SELECT ${cols} FROM materials WHERE code=?`, code);
    if (rows.length) return { mat: rows[0], how: "code", cands: [] };
  }
  if (model) {
    const rows = await q(db, `SELECT ${cols} FROM materials WHERE model=?`, model);
    if (rows.length === 1) return { mat: rows[0], how: "model", cands: [] };
    if (rows.length > 1) return { mat: null, how: "ambiguous", cands: rows };
  }
  if (name) {
    const rows = await q(db, `SELECT ${cols} FROM materials WHERE name=?`, name);
    if (rows.length === 1) return { mat: rows[0], how: "name", cands: [] };
    if (rows.length > 1) return { mat: null, how: "ambiguous", cands: rows };
    const fuzzy = await q(db, `SELECT ${cols} FROM materials WHERE name LIKE ? LIMIT 10`, `%${name}%`);
    if (fuzzy.length === 1) return { mat: fuzzy[0], how: "name_fuzzy", cands: [] };
    if (fuzzy.length > 1) return { mat: null, how: "ambiguous", cands: fuzzy };
  }
  return { mat: null, how: "not_found", cands: [] };
}

v1.post("/api/v1/bom/check", async (c) => {
  const d = await getJson(c);
  const items = d.items;
  if (!Array.isArray(items) || !items.length) return c.json({ error: "items 必须是非空数组" }, 400);
  if (items.length > 500) return c.json({ error: "单次最多核对 500 行" }, 400);
  const results: any[] = [];
  const summary = { total: 0, ok: 0, shortage: 0, not_found: 0, ambiguous: 0, invalid: 0 };
  for (const it of items) {
    if (typeof it !== "object" || it === null) {
      results.push({ input: it, status: "invalid", message: "条目格式错误（应为对象）" });
      summary.invalid++; summary.total++;
      continue;
    }
    const qty = parseIntSafe((it as any).quantity ?? 1, 1);
    if (qty === null) {
      results.push({ input: it, status: "invalid", message: "quantity 必须是大于0的整数" });
      summary.invalid++; summary.total++;
      continue;
    }
    const { mat, how, cands } = await matchMaterial(c.env.DB, it as Record<string, any>);
    const row: any = {
      input: { code: (it as any).code, model: (it as any).model, name: (it as any).name },
      need: qty,
    };
    if (how === "ambiguous") {
      row.status = "ambiguous";
      row.message = "匹配到多个物料，请改用编码(code)精确定位";
      row.candidates = cands;
      summary.ambiguous++;
    } else if (mat === null) {
      row.status = "not_found";
      row.message = "未找到匹配物料";
      summary.not_found++;
    } else {
      const avail = mat.quantity ?? 0;
      row.material = mat; row.matched_by = how; row.available = avail;
      if (avail >= qty) { row.status = "ok"; summary.ok++; }
      else { row.status = "shortage"; row.shortage = qty - avail; summary.shortage++; }
    }
    summary.total++;
    results.push(row);
  }
  return c.json({ summary, results });
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

export default v1;
