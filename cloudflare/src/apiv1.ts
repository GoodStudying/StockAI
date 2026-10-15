/**
 * 开放接口 /api/v1（供 Cherry Studio / MCP / 脚本程序化调用）
 * 认证：请求头 Authorization: Bearer <API_TOKEN>（环境变量 API_TOKEN，逗号分隔多个）
 * 与 Flask 版的 /api/v1 请求/响应格式完全一致，mcp_server.py 改个 ERP_API_BASE 即可切云端。
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { getJson, parseIntSafe } from "./auth";
import { q, applyStock, bomCheckCore } from "./core";

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

// ---------------- 2/3. 出入库（核心逻辑见 core.ts 的 applyStock，与网页端共用） ----------------
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

export default v1;
