/**
 * 共享业务核心：物料匹配 / BOM 核对循环 / 出入库原子操作 / 物料搜索
 * 供 apiv1.ts（开放接口）、routes.ts（网页端）、ai.ts（AI 对话）三处共用，
 * 消灭此前 apiv1.ts 与 routes.ts 各持一份的复制粘贴（与 Flask 版 app.py 的
 * _search_core / _bom_check_core / _apply_stock 一一对应，行为保持一致）。
 */
import { parseIntSafe, nowStr } from "./auth";

/** D1 查询 helper：bind 参数 + 返回结果数组 */
export async function q<T = any>(db: D1Database, sql: string, ...params: unknown[]): Promise<T[]> {
  const r = await db.prepare(sql).bind(...params).all<T>();
  return r.results ?? [];
}

// ---------------- 物料搜索核心（对应 Flask 版 _search_core） ----------------

/** 物料搜索核心：code 精确查询，否则 q 关键字模糊（name/code/model），两参皆空返回 null（调用方报 400） */
export async function searchCore(
  db: D1Database, kw: string, code: string, limit = 20,
): Promise<any[] | null> {
  const cols = "id, name, code, model, category, package, brand, quantity, unit, warehouse_id";
  code = (code ?? "").trim();
  kw = (kw ?? "").trim();
  if (code) {
    return await q(db, `SELECT ${cols} FROM materials WHERE code=? LIMIT ?`, code, limit);
  }
  if (kw) {
    const like = `%${kw}%`;
    return await q(db,
      `SELECT ${cols} FROM materials WHERE name LIKE ? OR code LIKE ? OR model LIKE ?
       ORDER BY id DESC LIMIT ?`, like, like, like, limit);
  }
  return null;
}

// ---------------- BOM 核对核心（对应 Flask 版 _bom_check_core） ----------------

/** BOM 条目匹配：编码精确 > 型号精确 > 名称精确 > 名称模糊（与 Flask 版 _match_material 一致） */
export async function matchMaterial(db: D1Database, item: Record<string, any>):
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

/**
 * BOM 核对循环核心：入参 items 数组，返回核对结果；参数不合法返回 {error, status} 由调用方包装响应。
 * 成功返回 { summary, results }——结构与 /api/v1/bom/check 响应体逐字段一致（网页端/AI 共用）。
 */
export async function bomCheckCore(db: D1Database, items: unknown):
  Promise<{ summary: any; results: any[] } | { error: string; status: number }> {
  if (!Array.isArray(items) || !items.length) return { error: "items 必须是非空数组", status: 400 };
  if (items.length > 500) return { error: "单次最多核对 500 行", status: 400 };
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
    const { mat, how, cands } = await matchMaterial(db, it as Record<string, any>);
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
  return { summary, results };
}

// ---------------- 出入库核心（对应 Flask 版 _apply_stock：原子扣减防并发超卖） ----------------

/** 出入库核心：单语句原子扣减 + 事后写流水；失败返回 {err:[信息,状态码]} */
export async function applyStock(
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
