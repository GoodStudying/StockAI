/**
 * 网页端业务 API（与 Flask 版 app.py 的路由 1:1 对应）
 * 包含：仓库管理 / 物料管理 / 出入库 / 流水 / 账户管理 / 数据库信息
 *
 * 云端版功能边界（阶段1）：
 * - 立创商城抓取（/api/lcsc/*、fetch_datasheet、fetch_image）：海外出口 IP 被立创风控，暂不支持 → 501
 * - 文件类（图片/手册上传下载）：待阶段2接入 R2 后开放 → 501/404
 * - /api/v1 开放接口（Cherry Studio 用）完整支持，见 apiv1.ts
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { requireLogin, requireAdmin, getJson, parseIntSafe, nowStr, hashPassword } from "./auth";

const api = new Hono<AppEnv>();

/** D1 查询 helper：bind 参数 + 返回结果数组 */
async function q<T = any>(db: D1Database, sql: string, ...params: unknown[]): Promise<T[]> {
  const r = await db.prepare(sql).bind(...params).all<T>();
  return r.results ?? [];
}

const MAX_WAREHOUSE_LEVEL = 4;

/** 云端暂不支持的功能统一占位响应 */
const NOT_SUPPORTED = { error: "云端版暂不支持该功能（立创抓取/文件存储），请在本地或 NAS 版使用；文件功能将在 R2 阶段开放" };

// ---------------- 仓库管理 ----------------

/** 内存递归收集仓库及全部子孙库 id（数据量小，比递归 CTE 直观且两端行为一致） */
function descendantIds(rows: { id: number; parent_id: number | null }[], wid: number): number[] {
  const ids = [wid];
  for (const r of rows) {
    if (r.parent_id === wid) ids.push(...descendantIds(rows, r.id));
  }
  return ids;
}

api.get("/api/warehouses", requireLogin, async (c) => {
  const rows = await q(c.env.DB, "SELECT * FROM warehouses ORDER BY level, id");
  // 各库物料数一次查回，内存汇总「本库+子孙库」总数（对应 Flask 版 material_count 语义）
  const counts = await q<{ warehouse_id: number; c: number }>(
    c.env.DB, "SELECT warehouse_id, COUNT(*) AS c FROM materials GROUP BY warehouse_id");
  const cntMap = new Map(counts.map((r) => [r.warehouse_id, r.c]));
  for (const w of rows) {
    const ids = descendantIds(rows, w.id);
    w.material_count = ids.reduce((s, id) => s + (cntMap.get(id) ?? 0), 0);
  }
  return c.json(rows);
});

api.post("/api/warehouses", requireLogin, async (c) => {
  const d = await getJson(c);
  const name = String(d.name ?? "").trim();
  if (!name) return c.json({ error: "库名不能为空" }, 400);
  const parentId = d.parent_id || null;
  let level = 1;
  if (parentId) {
    const parent = await q(c.env.DB, "SELECT level FROM warehouses WHERE id=?", parentId);
    if (!parent.length) return c.json({ error: "父级库不存在" }, 404);
    level = parent[0].level + 1;
  }
  if (level > MAX_WAREHOUSE_LEVEL) return c.json({ error: `最多支持 ${MAX_WAREHOUSE_LEVEL} 级库` }, 400);
  const r = await c.env.DB.prepare(
    "INSERT INTO warehouses(name, parent_id, level) VALUES (?,?,?)").bind(name, parentId, level).run();
  return c.json({ id: r.meta.last_row_id, name, parent_id: parentId, level }, 201);
});

api.put("/api/warehouses/:id", requireLogin, async (c) => {
  const wid = Number(c.req.param("id"));
  const d = await getJson(c);
  const name = String(d.name ?? "").trim();
  if (!name) return c.json({ error: "库名不能为空" }, 400);
  const r = await c.env.DB.prepare("UPDATE warehouses SET name=? WHERE id=?").bind(name, wid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "库不存在" }, 404);
  return c.json({ ok: true });
});

api.delete("/api/warehouses/:id", requireLogin, async (c) => {
  const wid = Number(c.req.param("id"));
  // 防御：库（含子库）内还有物料时拒绝删除（与 Flask 版一致，防外键约束炸 500）
  const rows = await q(c.env.DB, "SELECT id, parent_id FROM warehouses");
  const ids = descendantIds(rows, wid);
  const ph = ids.map(() => "?").join(",");
  const cnt = await q<{ c: number }>(
    c.env.DB, `SELECT COUNT(*) AS c FROM materials WHERE warehouse_id IN (${ph})`, ...ids);
  if (cnt[0]?.c > 0) {
    return c.json({ error: `该库及其子库下还有 ${cnt[0].c} 件物料，请先移出或删除物料` }, 400);
  }
  const r = await c.env.DB.prepare("DELETE FROM warehouses WHERE id=?").bind(wid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "库不存在" }, 404);
  return c.json({ ok: true });
});

// ---------------- 物料管理 ----------------

api.get("/api/materials", requireLogin, async (c) => {
  const kw = (c.req.query("q") ?? "").trim();
  const widRaw = c.req.query("warehouse_id");
  const includeChildren = (c.req.query("include_children") ?? "1") !== "0"; // 默认含子库
  let sql = `SELECT m.id, m.name, m.code, m.model, m.category, m.package, m.brand,
                    m.description, m.quantity, m.unit, m.warehouse_id, m.lcsc_url,
                    0 AS has_image, m.created_at, m.updated_at, w.name AS warehouse_name
             FROM materials m LEFT JOIN warehouses w ON m.warehouse_id = w.id`;
  const conds: string[] = [];
  const params: unknown[] = [];
  if (kw) {
    conds.push("(m.name LIKE ? OR m.code LIKE ? OR m.model LIKE ?)");
    params.push(`%${kw}%`, `%${kw}%`, `%${kw}%`);
  }
  if (widRaw) {
    const wid = parseIntSafe(widRaw, 1);
    if (wid === null) return c.json({ error: "warehouse_id 不合法" }, 400);
    if (includeChildren) {
      const rows = await q(c.env.DB, "SELECT id, parent_id FROM warehouses");
      const ids = descendantIds(rows, wid);
      conds.push(`m.warehouse_id IN (${ids.map(() => "?").join(",")})`);
      params.push(...ids);
    } else {
      conds.push("m.warehouse_id = ?");
      params.push(wid);
    }
  }
  if (conds.length) sql += " WHERE " + conds.join(" AND ");
  sql += " ORDER BY m.id DESC LIMIT 500";
  const rows = await q(c.env.DB, sql, ...params);
  // 手册列表一次查全表后内存分组（避免逐物料 N+1 查询，D1 每次查询都有开销）
  const docs = await q(
    c.env.DB, "SELECT material_id, id, doc_type, file_name FROM material_documents");
  const docMap = new Map<number, any[]>();
  for (const d of docs) {
    const arr = docMap.get(d.material_id) ?? [];
    arr.push({ id: d.id, doc_type: d.doc_type, file_name: d.file_name });
    docMap.set(d.material_id, arr);
  }
  for (const r of rows) r.documents = docMap.get(r.id) ?? [];
  return c.json(rows);
});

api.post("/api/materials", requireLogin, async (c) => {
  const d = await getJson(c);
  const name = String(d.name ?? "").trim();
  if (!name) return c.json({ error: "物料名称不能为空" }, 400);
  const qty = parseIntSafe(d.quantity ?? 0, 0);
  if (qty === null) return c.json({ error: "数量必须是不小于0的整数" }, 400);
  const r = await c.env.DB.prepare(
    `INSERT INTO materials(name, code, model, category, package, brand,
                           description, quantity, unit, warehouse_id, lcsc_url)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
    .bind(name, String(d.code ?? ""), String(d.model ?? ""), String(d.category ?? ""),
          String(d.package ?? ""), String(d.brand ?? ""), String(d.description ?? ""),
          qty, String(d.unit ?? "个"), d.warehouse_id || null, String(d.lcsc_url ?? ""))
    .run();
  return c.json({ id: r.meta.last_row_id }, 201);
});

api.put("/api/materials/:id", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const d = await getJson(c);
  const name = String(d.name ?? "").trim();
  if (!name) return c.json({ error: "物料名称不能为空" }, 400);
  const qty = parseIntSafe(d.quantity ?? 0, 0);
  if (qty === null) return c.json({ error: "数量必须是不小于0的整数" }, 400);
  const r = await c.env.DB.prepare(
    `UPDATE materials SET name=?, code=?, model=?, category=?, package=?,
        brand=?, description=?, quantity=?, unit=?, warehouse_id=?, lcsc_url=?, updated_at=? WHERE id=?`)
    .bind(name, String(d.code ?? ""), String(d.model ?? ""), String(d.category ?? ""),
          String(d.package ?? ""), String(d.brand ?? ""), String(d.description ?? ""),
          qty, String(d.unit ?? "个"), d.warehouse_id || null, String(d.lcsc_url ?? ""),
          nowStr(), mid)
    .run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "物料不存在" }, 404);
  return c.json({ ok: true });
});

api.delete("/api/materials/:id", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const r = await c.env.DB.prepare("DELETE FROM materials WHERE id=?").bind(mid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "物料不存在" }, 404);
  return c.json({ ok: true });
});

// ---------------- 出入库 / 流水（原子扣减，防并发超卖） ----------------

/** 出入库核心（与 Flask 版 _apply_stock 同逻辑）：单语句原子扣减 + 事后写流水 */
async function applyStock(
  db: D1Database, op: "in" | "out", mid: number, qty: number, operator: string, remark: string,
): Promise<{ qty?: number; err?: [string, number] }> {
  const mat = await db.prepare("SELECT id FROM materials WHERE id=?").bind(mid).first();
  if (!mat) return { err: ["物料不存在", 404] };
  const ts = nowStr();
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

api.post("/api/stock/:op", requireLogin, async (c) => {
  const op = c.req.param("op");
  if (op !== "in" && op !== "out") return c.json({ error: "操作类型不合法" }, 400);
  const d = await getJson(c);
  const mid = parseIntSafe(d.material_id, 1);
  const qty = parseIntSafe(d.quantity, 1);
  if (mid === null) return c.json({ error: "material_id 不合法" }, 400);
  if (qty === null) return c.json({ error: "数量必须是大于0的整数" }, 400);
  const r = await applyStock(c.env.DB, op, mid, qty, c.get("user")!.username, String(d.remark ?? ""));
  if (r.err) return c.json({ error: r.err[0] }, r.err[1] as 400 | 404);
  return c.json({ ok: true, quantity: r.qty });
});

api.get("/api/stock/records", requireLogin, async (c) => {
  const mid = c.req.query("material_id");
  let sql = `SELECT r.*, m.name AS material_name, m.code AS material_code
             FROM stock_records r JOIN materials m ON r.material_id = m.id`;
  const params: unknown[] = [];
  if (mid) { sql += " WHERE r.material_id = ?"; params.push(Number(mid) || 0); }
  sql += " ORDER BY r.id DESC LIMIT 500";
  return c.json(await q(c.env.DB, sql, ...params));
});

// ---------------- 立创抓取 / 文件类：云端暂不支持（阶段1 占位） ----------------
api.get("/api/lcsc/search", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));
api.get("/api/lcsc/detail", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));
api.post("/api/materials/:id/fetch_datasheet", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));
api.post("/api/materials/:id/fetch_image", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));
api.get("/api/materials/:id/image", requireLogin, (c) => c.json({ error: "无图片" }, 404));
api.post("/api/materials/:id/documents", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));
api.get("/api/documents/:id", requireLogin, (c) => c.json({ error: "文件不存在" }, 404));
api.delete("/api/documents/:id", requireLogin, (c) => c.json({ error: "记录不存在" }, 404));

// 在线更新接口：云端由 git push 自动部署取代
const UPDATE_DEPRECATED = { error: "云端版无需在线更新：git push 到 master 分支后 Cloudflare 自动重新部署" };
api.get("/api/update/status", requireAdmin, (c) => c.json(UPDATE_DEPRECATED, 400));
api.post("/api/update/apply", requireAdmin, (c) => c.json(UPDATE_DEPRECATED, 400));

// ---------------- 账户管理（管理员） ----------------

api.get("/api/users", requireAdmin, async (c) => {
  return c.json(await q(c.env.DB, "SELECT id, username, role, created_at FROM users"));
});

api.post("/api/users", requireAdmin, async (c) => {
  const d = await getJson(c);
  const username = String(d.username ?? "").trim();
  const password = String(d.password ?? "");
  const role = String(d.role ?? "user");
  if (!username || !password) return c.json({ error: "用户名和密码不能为空" }, 400);
  if (role !== "admin" && role !== "user") return c.json({ error: "角色不合法" }, 400);
  const exists = await q(c.env.DB, "SELECT id FROM users WHERE username=?", username);
  if (exists.length) return c.json({ error: "用户名已存在" }, 400);
  const r = await c.env.DB.prepare(
    "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)")
    .bind(username, await hashPassword(password), role).run();
  return c.json({ id: r.meta.last_row_id }, 201);
});

api.put("/api/users/:id", requireAdmin, async (c) => {
  const uid = Number(c.req.param("id"));
  const me = c.get("user")!;
  const d = await getJson(c);
  const rows = await q<{ id: number; role: string }>(c.env.DB, "SELECT id, role FROM users WHERE id=?", uid);
  if (!rows.length) return c.json({ error: "用户不存在" }, 404);
  const oldRole = rows[0].role;
  const newRole = d.role;
  const roleChanged = (newRole === "admin" || newRole === "user") && newRole !== oldRole;
  // 与 Flask 版一致的防御：不自降级 / 至少保留一名管理员
  if (roleChanged) {
    if (uid === me.id) return c.json({ error: "不能修改自己的角色（防止误降级失去管理权限）" }, 400);
    if (oldRole === "admin") {
      const admins = await q<{ c: number }>(c.env.DB, "SELECT COUNT(*) AS c FROM users WHERE role='admin'");
      if (admins[0].c <= 1) return c.json({ error: "至少保留一名管理员" }, 400);
    }
  }
  if (d.password) {
    await c.env.DB.prepare("UPDATE users SET password_hash=? WHERE id=?")
      .bind(await hashPassword(String(d.password)), uid).run();
  }
  if (roleChanged) {
    await c.env.DB.prepare("UPDATE users SET role=? WHERE id=?").bind(newRole, uid).run();
  }
  return c.json({ ok: true });
});

api.delete("/api/users/:id", requireAdmin, async (c) => {
  const uid = Number(c.req.param("id"));
  const me = c.get("user")!;
  if (uid === me.id) return c.json({ error: "不能删除当前登录账户" }, 400);
  const rows = await q<{ role: string }>(c.env.DB, "SELECT role FROM users WHERE id=?", uid);
  if (rows.length && rows[0].role === "admin") {
    const admins = await q<{ c: number }>(c.env.DB, "SELECT COUNT(*) AS c FROM users WHERE role='admin'");
    if (admins[0].c <= 1) return c.json({ error: "不能删除最后一名管理员" }, 400);
  }
  await c.env.DB.prepare("DELETE FROM users WHERE id=?").bind(uid).run();
  return c.json({ ok: true });
});

// ---------------- 数据库信息（管理员） ----------------

api.get("/api/db/info", requireAdmin, async (c) => {
  const info: Record<string, unknown> = { type: "d1", path: "(cloudflare d1)" };
  for (const t of ["warehouses", "materials", "material_documents", "stock_records", "users"]) {
    const r = await q<{ c: number }>(c.env.DB, `SELECT COUNT(*) AS c FROM ${t}`);
    info[t] = r[0]?.c ?? 0;
  }
  return c.json(info);
});

// 备份：云端用 D1 控制台导出/时间旅行恢复，无文件下载
api.get("/api/db/backup", requireAdmin, (c) =>
  c.json({ error: "云端备份请使用 Cloudflare 控制台：D1 → erp → Export/Time Travel" }, 400));

export default api;
