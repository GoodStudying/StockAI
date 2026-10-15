/**
 * 网页端业务 API（与 Flask 版 app.py 的路由 1:1 对应）
 * 包含：仓库管理 / 物料管理 / 出入库 / 流水 / 账户管理 / 数据库信息 / 立创抓取
 *
 * 云端版功能边界：
 * - 立创抓取已支持（存外链模式）：图片/手册只存立创 CDN 的 URL，展示时 302 跳转，
 *   抓取一次后链接永久存于 D1，不占 Worker 存储、无需 R2（英文站 lcsc.com 对 CF 出口友好）
 * - 手动上传文件（POST /api/materials/:id/documents）：需 R2，仍为 501
 * - /api/v1 开放接口（Cherry Studio 用）完整支持，见 apiv1.ts
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { requireLogin, requireAdmin, getJson, parseIntSafe, nowStr, hashPassword } from "./auth";
import { searchLcsc, fetchDetail } from "./lcsc";
import { q, applyStock, bomCheckCore } from "./core";

const api = new Hono<AppEnv>();

const MAX_WAREHOUSE_LEVEL = 4;

/** 手动上传文件类功能仍需 R2（阶段2），保留占位提示 */
const NOT_SUPPORTED = { error: "云端版暂不支持手动上传文件（待 R2 阶段开放）；立创自动抓取手册/图片已支持（存外链）" };

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
                    CASE WHEN m.image_path IS NOT NULL AND m.image_path != '' THEN 1 ELSE 0 END AS has_image,
                    m.image_path, m.created_at, m.updated_at, w.name AS warehouse_name
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

/** 物料自动补全（新建/编辑共用，外链模式）：编码为立创C编号时抓详情——
 *  参数字段仅填空不覆盖（lcsc_url 列对应详情的 url 键）；图片外链写 image_path；
 *  手册外链登记 material_documents。失败静默返回原因说明，不阻塞物料创建/编辑。
 *  返回 null=无需抓取或成功；字符串=失败原因（调用方放进响应 note） */
export async function autoFetchMaterial(db: D1Database, mid: number): Promise<string | null> {
  const rows = await q<any>(
    db, "SELECT code, name, model, brand, package, category, description, lcsc_url, image_path FROM materials WHERE id=?", mid);
  if (!rows.length) return "物料不存在";
  const r = rows[0];
  const code = String(r.code ?? "").trim();
  if (!/^C\d+$/i.test(code)) return null;       // 不是立创C编号，无需抓取

  // 快速路径：参数全填 + 已有图片外链 + 已有手册 → 无可补全，跳过网络请求
  const hasDoc = await q(
    db, "SELECT id FROM material_documents WHERE material_id=? AND doc_type='datasheet'", mid);
  const fieldsFull = ["name", "model", "brand", "package", "category", "lcsc_url"]
    .every((c) => String(r[c] ?? "").trim() !== "");
  if (fieldsFull && r.image_path && hasDoc.length) return null;

  const detail = await fetchDetail(code);
  if (!detail.length) return "未获取到立创详情（编号可能无效），参数未自动补全";
  const d = detail[0] as any;

  // 1) 参数字段：仅填空不覆盖
  const map: Record<string, string> = {
    name: "name", model: "model", brand: "brand", package: "package",
    category: "category", description: "description", lcsc_url: "url",
  };
  const updates: string[] = [];
  const params: unknown[] = [];
  for (const [col, key] of Object.entries(map)) {
    const cur = String(r[col] ?? "").trim();
    const nv = String(d[key] ?? "").trim();
    if (!cur && nv) { updates.push(`${col}=?`); params.push(nv); }
  }
  // 2) 图片：无外链时写立创 CDN 链接（302 跳转展示，不下载文件本体）
  if (!r.image_path && d.image) {
    let img = String(d.image);
    if (img.startsWith("//")) img = "https:" + img;   // 协议相对地址补全
    updates.push("image_path=?");
    params.push(img);
  }
  if (updates.length) {
    params.push(mid);
    await db.prepare(`UPDATE materials SET ${updates.join(", ")} WHERE id=?`).bind(...params).run();
  }
  // 3) 手册：无 datasheet 记录时登记外链（同一 PDF 查重）
  if (!hasDoc.length && d.datasheet_url) {
    const exists = await q(
      db, "SELECT id FROM material_documents WHERE material_id=? AND doc_type='datasheet' AND file_path=?",
      mid, d.datasheet_url);
    if (!exists.length) {
      const stem = (String(d.datasheet_url).match(/\/([0-9a-fA-F]{16,})\.pdf/) ?? [])[1] ?? "datasheet";
      await db.prepare(
        "INSERT INTO material_documents(material_id, doc_type, file_path, file_name) VALUES (?,?,?,?)")
        .bind(mid, "datasheet", d.datasheet_url, `${stem}.pdf`).run();
    }
  }
  return null;
}

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
  // 立创C编号自动补全：图片/手册外链 + 参数填空（失败静默不阻塞建料）
  const note = await autoFetchMaterial(c.env.DB, r.meta.last_row_id);
  const resp: Record<string, unknown> = { id: r.meta.last_row_id };
  if (note) resp.note = note;
  return c.json(resp, 201);
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
  // 后补立创编号/尚有空字段时自动补全（幂等：已全填则快速路径直接跳过）
  const note = await autoFetchMaterial(c.env.DB, mid);
  const resp: Record<string, unknown> = { ok: true };
  if (note) resp.note = note;
  return c.json(resp);
});

api.delete("/api/materials/:id", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const r = await c.env.DB.prepare("DELETE FROM materials WHERE id=?").bind(mid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "物料不存在" }, 404);
  return c.json({ ok: true });
});

// ---------------- 出入库 / 流水（原子扣减核心见 core.ts 的 applyStock，与开放接口共用） ----------------

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

// ---------------- 立创抓取（存外链模式：元数据+URL 入库，文件本体留在立创 CDN） ----------------

/** 立创搜索：C 编号直查详情；关键字走 Bing 反查（海外出口对 lcsc.com 友好，中文站尽力而为） */
api.get("/api/lcsc/search", requireLogin, async (c) => {
  const kw = (c.req.query("q") ?? "").trim();
  if (!kw) return c.json([]);
  const r = await searchLcsc(kw);
  if (!r.length) {
    return c.json({ error: "未搜到结果（可能被风控或网络受限），建议直接用立创C编号（形如 C621395）重试" }, 404);
  }
  return c.json(r);
});

/** 立创详情：按 C 编号抓完整参数（搜索结果选中后调用） */
api.get("/api/lcsc/detail", requireLogin, async (c) => {
  const code = (c.req.query("code") ?? "").trim();
  const r = await fetchDetail(code);
  if (!r.length) return c.json({ error: "未获取到详情（编号可能无效或网络受限），请稍后重试" }, 404);
  return c.json(r);
});

/** 按物料编码自动抓手册：只登记立创 PDF 直链（file_path 存外链），不下载文件本体 */
api.post("/api/materials/:id/fetch_datasheet", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const rows = await q<{ code: string }>(c.env.DB, "SELECT code FROM materials WHERE id=?", mid);
  if (!rows.length) return c.json({ error: "物料不存在" }, 404);
  const code = (rows[0].code ?? "").trim();
  if (!/^C\d+$/i.test(code)) {
    return c.json({ error: `编码「${code}」不是立创C编号（形如 C621395），无法自动获取` }, 400);
  }
  const detail = await fetchDetail(code);
  if (!detail.length) return c.json({ error: "未获取到立创详情（编号可能无效或网络受限），请稍后重试" }, 404);
  const url = detail[0].datasheet_url;
  if (!url) return c.json({ error: "立创未提供该物料的手册PDF" }, 404);
  // 按外链查重（同一 PDF 不重复登记）
  const exists = await q(
    c.env.DB, "SELECT id FROM material_documents WHERE material_id=? AND doc_type='datasheet' AND file_path=?", mid, url);
  if (exists.length) return c.json({ ok: true, file_name: "datasheet.pdf", note: "手册已存在" });
  const stem = (url.match(/\/([0-9a-fA-F]{16,})\.pdf/) ?? [])[1] ?? "datasheet";
  await c.env.DB.prepare(
    "INSERT INTO material_documents(material_id, doc_type, file_path, file_name) VALUES (?,?,?,?)")
    .bind(mid, "datasheet", url, `${stem}.pdf`).run();
  return c.json({ ok: true, file_name: `${stem}.pdf` });
});

/** 按物料编码自动抓图片：外链 URL 写入 image_path 列（有值即认为已存在） */
api.post("/api/materials/:id/fetch_image", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const rows = await q<{ code: string; image_path: string | null }>(
    c.env.DB, "SELECT code, image_path FROM materials WHERE id=?", mid);
  if (!rows.length) return c.json({ error: "物料不存在" }, 404);
  const code = (rows[0].code ?? "").trim();
  if (!/^C\d+$/i.test(code)) return c.json({ error: "该物料没有立创C编号，无法获取图片" }, 400);
  if (rows[0].image_path) return c.json({ ok: true, note: "图片已存在" });
  const detail = await fetchDetail(code);
  if (!detail.length) return c.json({ error: "未获取到立创详情（编号可能无效或网络受限），请稍后重试" }, 404);
  let img = detail[0].image ?? "";
  if (!img) return c.json({ error: "立创未提供该物料的图片" }, 404);
  if (img.startsWith("//")) img = "https:" + img; // 协议相对地址补全
  await c.env.DB.prepare("UPDATE materials SET image_path=? WHERE id=?").bind(img, mid).run();
  return c.json({ ok: true });
});

// ---------------- 文件访问 / 手动上传 ----------------

/** 物料图片：image_path 为外链时 302 跳转到立创 CDN（前端 <img> 零改动兼容） */
api.get("/api/materials/:id/image", requireLogin, async (c) => {
  const mid = Number(c.req.param("id"));
  const rows = await q<{ image_path: string | null }>(
    c.env.DB, "SELECT image_path FROM materials WHERE id=?", mid);
  const p = rows[0]?.image_path;
  if (p && /^https?:\/\//.test(p)) return c.redirect(p, 302);
  return c.json({ error: "无图片" }, 404);
});

/** 手动上传文件：需 R2 存储（阶段2），暂不支持 */
api.post("/api/materials/:id/documents", requireLogin, (c) => c.json(NOT_SUPPORTED, 501));

/** 手册/文档：file_path 为外链时 302 跳转（浏览器直接打开立创 CDN 的 PDF） */
api.get("/api/documents/:id", requireLogin, async (c) => {
  const id = Number(c.req.param("id"));
  const rows = await q<{ file_path: string | null }>(
    c.env.DB, "SELECT file_path FROM material_documents WHERE id=?", id);
  const p = rows[0]?.file_path;
  if (p && /^https?:\/\//.test(p)) return c.redirect(p, 302);
  return c.json({ error: "文件不存在" }, 404);
});

/** 删除手册登记（外链模式下只删记录，无文件需清理） */
api.delete("/api/documents/:id", requireLogin, async (c) => {
  const id = Number(c.req.param("id"));
  const r = await c.env.DB.prepare("DELETE FROM material_documents WHERE id=?").bind(id).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "记录不存在" }, 404);
  return c.json({ ok: true });
});

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

// ---------------- 系统设置 app_settings（AI 配置存这里，key-value） ----------------

/** 读单个设置项，不存在返回空串 */
async function getSetting(db: D1Database, key: string): Promise<string> {
  const rows = await q<{ value: string }>(db, "SELECT value FROM app_settings WHERE key=?", key);
  return rows[0]?.value ?? "";
}

/** 写设置项（UPSERT，D1 与 Flask 两侧 SQLite/PG 均支持 ON CONFLICT 语法） */
async function setSetting(db: D1Database, key: string, value: string): Promise<void> {
  await db.prepare(
    "INSERT INTO app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    .bind(key, value).run();
}

/** API Key 打码回显：保头 3 尾 4（太短只保头 2），防配置页回显泄露 */
function maskKey(k: string): string {
  if (!k) return "";
  if (k.length <= 8) return k.slice(0, 2) + "***";
  return k.slice(0, 3) + "***" + k.slice(-4);
}

/** 读取 AI 接口配置（OpenAI 兼容三件套） */
export async function aiCfg(db: D1Database) {
  return {
    base_url: (await getSetting(db, "ai_base_url")).trim(),
    api_key: (await getSetting(db, "ai_api_key")).trim(),
    model: (await getSetting(db, "ai_model")).trim(),
  };
}

// ---------------- AI 接口配置（管理员，与 Flask 版 /api/ai/* 1:1） ----------------

api.get("/api/ai/config", requireAdmin, async (c) => {
  const cfg = await aiCfg(c.env.DB);
  return c.json({ base_url: cfg.base_url, model: cfg.model,
                  api_key_masked: maskKey(cfg.api_key), has_key: !!cfg.api_key });
});

api.post("/api/ai/config", requireAdmin, async (c) => {
  const d = await getJson(c);
  const baseUrl = String(d.base_url ?? "").trim();
  const model = String(d.model ?? "").trim();
  const apiKey = String(d.api_key ?? "").trim();
  if (!baseUrl) return c.json({ error: "接口地址（base_url）不能为空" }, 400);
  if (!model) return c.json({ error: "模型名（model）不能为空" }, 400);
  await setSetting(c.env.DB, "ai_base_url", baseUrl);
  await setSetting(c.env.DB, "ai_model", model);
  if (apiKey === "!") {
    await setSetting(c.env.DB, "ai_api_key", "");     // 显式清空
  } else if (apiKey) {
    await setSetting(c.env.DB, "ai_api_key", apiKey); // ★空 = 保留原值（防打码回显覆盖清空）
  }
  return c.json({ ok: true });
});

api.post("/api/ai/test", requireAdmin, async (c) => {
  // 测试连通性：发一条最小 chat/completions，body 缺的字段回退用已存配置
  const d = await getJson(c);
  const saved = await aiCfg(c.env.DB);
  const baseUrl = String(d.base_url ?? "").trim() || saved.base_url;
  const apiKey = String(d.api_key ?? "").trim() || saved.api_key;
  const model = String(d.model ?? "").trim() || saved.model;
  if (!baseUrl || !apiKey || !model) return c.json({ error: "请先填写完整的 接口地址/API Key/模型名" }, 400);
  const t0 = Date.now();
  let resp: Response;
  try {
    resp = await fetch(baseUrl.replace(/\/+$/, "") + "/chat/completions", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages: [{ role: "user", content: "ping" }], max_tokens: 5 }),
      signal: AbortSignal.timeout(30_000),
    });
  } catch (e) {
    return c.json({ error: `连接失败：${e instanceof Error ? e.name : "网络错误"}（检查地址/网络）` }, 502);
  }
  if (resp.status !== 200) {
    return c.json({ error: `HTTP ${resp.status}：${(await resp.text()).slice(0, 200)}` }, 502);
  }
  return c.json({ ok: true, ms: Date.now() - t0 });
});

api.get("/api/ai/status", requireLogin, async (c) => {
  const cfg = await aiCfg(c.env.DB);
  const ok = !!(cfg.base_url && cfg.api_key && cfg.model);
  return c.json({ configured: ok, model: ok ? cfg.model : "" });
});

// ---------------- 采购清单（与 Flask 版 /api/purchase/* 1:1；状态读时派生不存列） ----------------

/** 采购批量导入核心（BOM 一键导入 & AI 卡片共用）：
 *  同项目命中（material_id 相同或 code 非空相同）→ need_qty 累加，否则新建。
 *  返回 {created, merged} 或 {error} */
export async function purchaseBulkCore(
  db: D1Database, project: unknown, items: unknown, operator: string,
): Promise<{ created: number; merged: number } | { error: string }> {
  const proj = String(project ?? "").trim();
  if (!proj) return { error: "项目名不能为空" };
  if (!Array.isArray(items) || !items.length) return { error: "items 必须是非空数组" };
  if (items.length > 200) return { error: "单次最多导入 200 行" };
  let created = 0, merged = 0;
  for (const it of items) {
    if (typeof it !== "object" || it === null) return { error: "items 内每行必须是对象" };
    const row = it as Record<string, unknown>;
    const name = String(row.name ?? "").trim();
    if (!name) return { error: "每行的 name 不能为空" };
    const qty = parseIntSafe(row.need_qty, 1);
    if (qty === null) return { error: `「${name}」的 need_qty 必须是大于0的整数` };
    const mid = parseIntSafe(row.material_id ?? 0, 1) ?? 0;
    const code = String(row.code ?? "").trim();
    // 合并查找：material_id 优先，其次 code（code 非空才参与，防把不同无码物料误合并）
    let hit = 0;
    if (mid) {
      const r = await q<{ id: number }>(db, "SELECT id FROM purchase_items WHERE project=? AND material_id=?", proj, mid);
      hit = r[0]?.id ?? 0;
    } else if (code) {
      const r = await q<{ id: number }>(db, "SELECT id FROM purchase_items WHERE project=? AND code=?", proj, code);
      hit = r[0]?.id ?? 0;
    }
    if (hit) {
      await db.prepare("UPDATE purchase_items SET need_qty=need_qty+? WHERE id=?").bind(qty, hit).run();
      merged++;
    } else {
      await db.prepare(
        `INSERT INTO purchase_items(project, material_id, name, code, model, need_qty, unit, remark, created_by)
         VALUES (?,?,?,?,?,?,?,?,?)`)
        .bind(proj, mid || null, name, code, String(row.model ?? "").trim(), qty,
              String(row.unit ?? "个").trim() || "个", String(row.remark ?? "").trim(), operator)
        .run();
      created++;
    }
  }
  return { created, merged };
}

/** 采购条目查询核心：LEFT JOIN 批次 SUM 派生 purchased/status（open/partial/done） */
export async function purchaseListCore(db: D1Database, project?: string, status?: string) {
  let sql = `SELECT p.*, COALESCE(b.purchased, 0) AS purchased,
                    CASE WHEN COALESCE(b.purchased, 0) >= p.need_qty THEN 'done'
                         WHEN COALESCE(b.purchased, 0) > 0 THEN 'partial'
                         ELSE 'open' END AS status
             FROM purchase_items p
             LEFT JOIN (SELECT purchase_item_id, SUM(qty) AS purchased
                        FROM purchase_batches GROUP BY purchase_item_id) b
               ON b.purchase_item_id = p.id`;
  const params: unknown[] = [];
  if (project) { sql += " WHERE p.project=?"; params.push(project); }
  sql += " ORDER BY p.id DESC LIMIT 500";
  let rows = await q<any>(db, sql, ...params);
  if (status) rows = rows.filter((r) => r.status === status);  // 派生列过滤放内存（两端行为一致）
  return rows;
}

api.get("/api/purchase/items", requireLogin, async (c) => {
  const project = (c.req.query("project") ?? "").trim() || undefined;
  const status = (c.req.query("status") ?? "").trim() || undefined;
  return c.json(await purchaseListCore(c.env.DB, project, status));
});

api.post("/api/purchase/items", requireLogin, async (c) => {
  const d = await getJson(c);
  const r = await purchaseBulkCore(c.env.DB, d.project, d.items, c.get("user")!.username);
  if ("error" in r) return c.json({ error: r.error }, 400);
  return c.json(r, 201);
});

api.put("/api/purchase/items/:id", requireLogin, async (c) => {
  const pid = Number(c.req.param("id"));
  const d = await getJson(c);
  const name = String(d.name ?? "").trim();
  if (!name) return c.json({ error: "名称不能为空" }, 400);
  const qty = parseIntSafe(d.need_qty, 1);
  if (qty === null) return c.json({ error: "需求量必须是大于0的整数" }, 400);
  const r = await c.env.DB.prepare(
    "UPDATE purchase_items SET project=?, name=?, code=?, model=?, need_qty=?, unit=?, remark=? WHERE id=?")
    .bind(String(d.project ?? "").trim(), name, String(d.code ?? "").trim(),
          String(d.model ?? "").trim(), qty, String(d.unit ?? "个").trim() || "个",
          String(d.remark ?? "").trim(), pid)
    .run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "条目不存在" }, 404);
  return c.json({ ok: true });
});

api.delete("/api/purchase/items/:id", requireLogin, async (c) => {
  const pid = Number(c.req.param("id"));
  const r = await c.env.DB.prepare("DELETE FROM purchase_items WHERE id=?").bind(pid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "条目不存在" }, 404);
  return c.json({ ok: true });
});

api.post("/api/purchase/items/:id/batches", requireLogin, async (c) => {
  const pid = Number(c.req.param("id"));
  const exists = await q(c.env.DB, "SELECT id FROM purchase_items WHERE id=?", pid);
  if (!exists.length) return c.json({ error: "采购条目不存在" }, 404);
  const d = await getJson(c);
  const qty = parseIntSafe(d.qty, 1);
  if (qty === null) return c.json({ error: "数量必须是大于0的整数" }, 400);
  const r = await c.env.DB.prepare(
    "INSERT INTO purchase_batches(purchase_item_id, qty, remark, created_by) VALUES (?,?,?,?)")
    .bind(pid, qty, String(d.remark ?? "").trim(), c.get("user")!.username).run();
  return c.json({ id: r.meta.last_row_id }, 201);
});

api.get("/api/purchase/items/:id/batches", requireLogin, async (c) => {
  // 查某采购条目的全部采购批次（前端行展开用）
  const pid = Number(c.req.param("id"));
  const exists = await q(c.env.DB, "SELECT id FROM purchase_items WHERE id=?", pid);
  if (!exists.length) return c.json({ error: "采购条目不存在" }, 404);
  return c.json(await q(
    c.env.DB, "SELECT * FROM purchase_batches WHERE purchase_item_id=? ORDER BY id DESC", pid));
});

api.delete("/api/purchase/batches/:id", requireLogin, async (c) => {
  const bid = Number(c.req.param("id"));
  const r = await c.env.DB.prepare("DELETE FROM purchase_batches WHERE id=?").bind(bid).run();
  if ((r.meta.changes ?? 0) === 0) return c.json({ error: "批次不存在" }, 404);
  return c.json({ ok: true });
});

api.get("/api/purchase/projects", requireLogin, async (c) => {
  // 项目聚合视图：条数/已完成数/需求与已购总量（采购页项目列表）
  const rows = await q(c.env.DB,
    `SELECT p.project,
            COUNT(*) AS item_count,
            SUM(CASE WHEN COALESCE((SELECT SUM(qty) FROM purchase_batches
                      WHERE purchase_item_id = p.id), 0) >= p.need_qty THEN 1 ELSE 0 END) AS done_count,
            SUM(p.need_qty) AS need_total,
            COALESCE(SUM(COALESCE((SELECT SUM(qty) FROM purchase_batches
                      WHERE purchase_item_id = p.id), 0)), 0) AS purchased_total,
            MAX(p.id) AS last_id
     FROM purchase_items p GROUP BY p.project ORDER BY last_id DESC`);
  return c.json(rows);
});

// ---------------- 网页端 BOM 核对（session 认证；核心在 core.ts，与 /api/v1 版一致） ----------------

api.post("/api/bom/check", requireLogin, async (c) => {
  const d = await getJson(c);
  const r = await bomCheckCore(c.env.DB, d.items);
  if ("error" in r) return c.json({ error: r.error }, r.status as 400);
  return c.json(r);
});

// ---------------- 数据库信息（管理员） ----------------

api.get("/api/db/info", requireAdmin, async (c) => {
  const info: Record<string, unknown> = { type: "d1", path: "(cloudflare d1)" };
  for (const t of ["warehouses", "materials", "material_documents", "stock_records", "users",
                   "app_settings", "chat_messages", "purchase_items", "purchase_batches"]) {
    const r = await q<{ c: number }>(c.env.DB, `SELECT COUNT(*) AS c FROM ${t}`);
    info[t] = r[0]?.c ?? 0;
  }
  return c.json(info);
});

// 备份：云端用 D1 控制台导出/时间旅行恢复，无文件下载
api.get("/api/db/backup", requireAdmin, (c) =>
  c.json({ error: "云端备份请使用 Cloudflare 控制台：D1 → erp → Export/Time Travel" }, 400));

export default api;
