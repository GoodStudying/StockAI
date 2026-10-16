/**
 * AI 对话模块（与 Flask 版 app.py 的 /api/chat* 路由 1:1 镜像）
 * OpenAI 兼容协议（Kimi / DeepSeek / GLM 等）+ function calling tool loop + 待确认卡片。
 *
 * 安全模式（人确认后执行）：
 * - 只读工具（query 只读SQL / lcsc_search / check_bom）真执行，结果回给模型；
 * - 写工具（stock/material/warehouse/purchase，按资源合并）一律拦截转 action 卡片，
 *   用户在前端点「确认执行」后由 /api/chat/execute 落库，operator 留痕为 ai:用户名；
 * - 幂等状态机：pending → executed / cancelled / failed，非 pending 返回 409 防双击重放。
 *
 * v261016.1 整合：12 个工具 → 7 个（只读合并为 query；写操作按资源合并，
 * 同时补齐 物料改/删、仓库改/删、采购改/删/登记批次 等此前"没有工具可用"的场景）。
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { requireLogin, getJson, parseIntSafe } from "./auth";
import { q, applyStock, bomCheckCore } from "./core";
import { searchLcsc } from "./lcsc";
import { aiCfg, autoFetchMaterial, purchaseBulkCore, descendantIds, MAX_WAREHOUSE_LEVEL } from "./routes";

const chat = new Hono<AppEnv>();

// tool loop 双保险上限：LLM 轮次 与 工具调用总数 都封顶，防模型死循环刷调用
const MAX_LLM_ROUNDS = 8;
const MAX_TOOL_CALLS = 12;

/** 生成 8 位随机 hex（卡片 id），对齐 Flask 版 secrets.token_hex(4) */
function randHex8(): string {
  const b = new Uint8Array(4);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

/** 当前时间字符串（卡片 executed_at 用，与流水时间戳同格式） */
function nowStrLocal(): string {
  return new Date().toISOString().slice(0, 19).replace("T", " ");
}

// ---------------- 轻量迁移：chat_messages.usage_json（D1 无 IF NOT EXISTS 列语法，逐 isolate 试一次） ----------------
let usageColReady = false;
async function ensureUsageCol(db: D1Database): Promise<void> {
  if (usageColReady) return;
  try {
    await db.prepare("ALTER TABLE chat_messages ADD COLUMN usage_json TEXT").run();
  } catch { /* 列已存在（duplicate column）静默忽略 */ }
  usageColReady = true;
}

/** OpenAI 兼容 /chat/completions 单一收敛调用点。
 *  返回 {content, toolCalls, usage}；非 200 抛错（外层负责落失败消息并返 502）。
 *  鲁棒性（借鉴参考实现）：temperature 三档降级 0.3 → 不带 → 1
 *  （部分模型如 Kimi 思考模型限制取值）；
 *  429 限速不做长等待（Worker 挂钟成本高），直接抛出明确提示让前端稍后重试。 */
async function llmChat(
  cfg: { base_url: string; api_key: string; model: string },
  messages: any[],
  tools?: any[] | null,
): Promise<{ content: string; toolCalls: any[]; usage: any }> {
  const url = cfg.base_url.replace(/\/+$/, "") + "/chat/completions";
  const tempVariants: Record<string, number>[] = [{ temperature: 0.3 }, {}, { temperature: 1 }];
  let ti = 0;
  for (;;) {
    const payload: Record<string, unknown> = { model: cfg.model, messages, ...tempVariants[ti] };
    if (tools) {
      payload.tools = tools;
      payload.tool_choice = "auto";
    }
    const r = await fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${cfg.api_key}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(90_000),      // 对齐 Flask 版 timeout=(10, 90)
    });
    if (r.status === 200) {
      const body = (await r.json()) as any;
      const msg = body.choices[0].message;
      return { content: msg.content ?? "", toolCalls: msg.tool_calls ?? [], usage: body.usage ?? {} };
    }
    const errTxt = (await r.text()).slice(0, 300);
    if (r.status === 429) {
      throw new Error("AI 接口限速（HTTP 429）：请稍后几秒重发（免费档常见 3 次/分钟限制）");
    }
    if (errTxt.toLowerCase().includes("temperature") && ti < tempVariants.length - 1) {
      ti++;                                      // 该模型限制 temperature，换下一档重试
      continue;
    }
    throw new Error(`HTTP ${r.status}: ${errTxt.slice(0, 200)}`);
  }
}

/** 纯文本对话（不带工具），供 BOM 智能匹配等确定性任务用；复用降级逻辑 */
export async function llmPlain(
  cfg: { base_url: string; api_key: string; model: string },
  messages: any[],
): Promise<string> {
  const r = await llmChat(cfg, messages, null);
  return r.content;
}

/** 系统提示词：角色 + 实时仓库/物料概况 + 表结构 + 工具规范 + 安全模式说明 + 电子常识 + 回复风格。
 *  仓库列表与物料总数每次请求现查注入；表结构清单让 query 工具开箱可用。（与 Flask 版同文） */
async function systemPrompt(db: D1Database, username: string): Promise<string> {
  const today = new Date().toISOString().slice(0, 10);
  const whs = await q<{ name: string; level: number }>(
    db, "SELECT name, level FROM warehouses ORDER BY level, id LIMIT 30");
  const whStr = whs.map((w) => "　".repeat(w.level - 1) + w.name).join("、") || "（暂无仓库）";
  const totalRow = await db.prepare("SELECT COUNT(*) AS n FROM materials").first<{ n: number }>();
  return `你是「个人ERP」的库存管理 AI 助手。当前用户：${username}；今天：${today}。
现有仓库：${whStr}
库存物料总数：${totalRow?.n ?? 0} 种

【职责】帮助用户查询物料库存、办理出入库、核对 BOM、管理采购清单。

【数据库表结构】（query 工具只能查这些表）
- materials(id, name 名称, code 编码/立创C编号, model 型号, category 分类, package 封装, brand 品牌, quantity 库存数量, unit 单位, warehouse_id 所属仓库id, description 描述, created_at)
- warehouses(id, name, parent_id 父库id, level 层级1-4)
- stock_records(id, material_id, type 类型in入库/out出库, quantity, operator 操作人, remark 备注, created_at) —— 出入库流水
- purchase_items(id, project 项目, material_id, name, code, model, need_qty 需求数量, unit, remark, created_at) —— 采购条目（已购量=其 purchase_batches 的 qty 合计）
- purchase_batches(id, purchase_item_id, qty 数量, remark, created_at) —— 采购批次
- material_documents(id, material_id, doc_type, file_name, created_at) —— 物料手册

【工具使用规范】
1. 一切查询（库存/流水/仓库/采购/统计）都用 query：用户问"有哪些库存/库存情况"直接 SELECT * FROM materials，不要反问用户要关键词。LIKE 模糊匹配记得带 %；默认加 LIMIT 50 以内。
2. stock（出入库）/ material（物料增改删）/ warehouse（仓库增改删）/ purchase（采购管理）是写操作：调用后只会生成"待确认卡片"，并未真正执行。你必须向用户清晰复述操作内容（物料、数量、仓库），并提醒用户点击卡片上的「确认执行」按钮；用户没确认就当作未执行。
3. 出入库/修改/删除物料前必须先定位物料：material_id 只能来自 query 结果，禁止编造；用户直接给了编码（如 C621395）时可跳过查询，直接给工具传 code。多候选时把候选列给用户选，不要猜。
4. 库存中搜不到的物料：用 lcsc_search 搜立创商城（结果含立创C编号），把候选列成简表让用户选；用户确认编号后用 material 的 create 建料（填了 C 编号会自动抓取图片/手册/参数，无需用户手填），再按需用 stock 入库。
5. BOM 核对用 check_bom；用户想采购缺料时用 purchase 的 add（问清项目名，BOM 缺料可批量导入）；登记一次采购到货用 add_batch。
6. 用户一次给多行/多种物料要办理时（如批量入库、整份BOM建料）：逐行连续调用对应工具，每行生成一张卡片，全部列完后一次性提醒用户逐张确认；不得以"只能一次一个""系统不支持批量"等理由拒绝或只做一部分。
7. 用户要"清空所有物料/全部删掉重来"时：先 query 列出全部物料，再对每一行连续调用 material 的 delete 生成删除卡片（标题会标注库存将一并清空），全部列出后提醒用户逐张确认。

【电子领域常识】
- 电容容值换算：1µF = 1000nF（0.1µF = 100nF；三位数标注 104 = 100nF、105 = 1µF）。用户给出的不同写法若等值，主动指出并确认是否同一种物料。
- 电阻：10k = 10kΩ = 10000Ω；贴片三位数标注 103 = 10kΩ。
- 封装 0402/0603/0805/1206 是尺寸规格；同容值不同封装/耐压/精度是不同物料，用户未说明时要问清。

【回复风格】简体中文；简洁分点，必要时用小表格；涉及写操作的回复末尾必须提醒用户确认卡片。`;
}

// 7 个工具的 OpenAI function calling 声明（v261016.1 整合：12 → 7）
// 只读 3 个真执行（query 只读SQL / lcsc_search / check_bom）+ 写 4 个按资源合并转卡片
// description 写成"使用说明书"（用途+示例+前置条件），是引导模型正确调用的关键。（与 Flask 版同构）
const AI_TOOLS = [
  { type: "function", function: {
    name: "query",
    description: "只读 SQL 查询（仅 SELECT，自动限量）。查库存/流水/仓库/采购/统计都用它："
      + "查全部库存 SELECT * FROM materials ORDER BY id DESC；"
      + "模糊查 SELECT * FROM materials WHERE name LIKE '%电容%'；"
      + "查流水 SELECT r.*,m.name,m.code FROM stock_records r JOIN materials m ON m.id=r.material_id ORDER BY r.id DESC；"
      + "统计 SELECT warehouse_id,COUNT(*),SUM(quantity) FROM materials GROUP BY warehouse_id。"
      + "可查表与字段见系统提示词；禁止 UPDATE/DELETE/INSERT。",
    parameters: { type: "object", required: ["sql"], properties: {
      sql: { type: "string", description: "SELECT 语句（单条，不带分号）" },
      params: { type: "array", description: "可选，? 占位符参数数组" } } } } },
  { type: "function", function: {
    name: "lcsc_search",
    description: "搜索立创商城（库存没有的物料来这里找候选），返回立创C编号/型号/品牌/封装/库存/价格。用户选定 C 编号后用 material 工具 create 建料并自动补全参数。",
    parameters: { type: "object", properties: {
      keyword: { type: "string", description: "搜索关键词，如 '100nF 0603 电容'" } },
      required: ["keyword"] } } },
  { type: "function", function: {
    name: "check_bom",
    description: "BOM 核对：逐项匹配库存并比对数量，返回每行状态（ok充足/shortage缺料/not_found未找到/ambiguous多个匹配）。",
    parameters: { type: "object", properties: {
      items: { type: "array", description: "BOM 行数组", items: { type: "object", properties: {
        code: { type: "string" }, model: { type: "string" },
        name: { type: "string" },
        quantity: { type: "integer", description: "需求数量，默认1" } } } } } } } },
  { type: "function", function: {
    name: "stock",
    description: "出入库（写操作，生成待确认卡片）。material_id 来自 query 结果；用户直接给了编码时也可只传 code"
      + "（精确匹配编码或型号，多候选会返回列表让你请用户确认，此时无需先查询）。"
      + "入库可传 warehouse_id 同时把物料挂到该仓库（id 用 query 查 warehouses 表）。",
    parameters: { type: "object", required: ["op", "quantity"], properties: {
      op: { type: "string", enum: ["in", "out"], description: "in=入库 out=出库" },
      material_id: { type: "integer", description: "物料 id（与 code 二选一）" },
      code: { type: "string", description: "物料编码/型号（与 material_id 二选一）" },
      quantity: { type: "integer", description: "数量>0" },
      warehouse_id: { type: "integer", description: "仓库 id（可选，仅入库：同时把物料归属到该仓库）" },
      remark: { type: "string", description: "备注（可选）" } } } } },
  { type: "function", function: {
    name: "material",
    description: "物料管理（写操作，生成待确认卡片）。action=create 新建（填立创C编号自动抓图片/手册/参数）；"
      + "update 修改资料字段（改名称/编码/型号/分类/封装/品牌/单位/仓库/描述；⚠️改库存数量必须用 stock 工具）；"
      + "delete 删除（现库存一并清空，标题会标注）。update/delete 用 material_id 或 code 定位。",
    parameters: { type: "object", required: ["action"], properties: {
      action: { type: "string", enum: ["create", "update", "delete"] },
      material_id: { type: "integer", description: "物料 id（update/delete 用，与 code 二选一）" },
      code: { type: "string", description: "立创C编号（create 建议填自动补全；update/delete 可用于定位）" },
      name: { type: "string", description: "物料名称（create 必填）" },
      model: { type: "string" }, category: { type: "string" },
      package: { type: "string" }, brand: { type: "string" }, unit: { type: "string" },
      warehouse_id: { type: "integer", description: "仓库 id（query 查 warehouses 表）" },
      quantity: { type: "integer", description: "仅 create：初始库存，默认0" },
      description: { type: "string" } } } } },
  { type: "function", function: {
    name: "warehouse",
    description: "仓库管理（写操作，生成待确认卡片）。action=create 新建（最深4级，parent_id 填父库 id 缺省=一级库）；"
      + "update 改名或换父级（需 id）；delete 删除（需 id，仅空库可删——有物料或有子库会被拒）。",
    parameters: { type: "object", required: ["action"], properties: {
      action: { type: "string", enum: ["create", "update", "delete"] },
      id: { type: "integer", description: "仓库 id（update/delete 必填，query 查 warehouses 表）" },
      name: { type: "string", description: "仓库名称（create 必填；update 改名用）" },
      parent_id: { type: "integer", description: "父仓库 id（create/update 可选，0=一级库）" } } } } },
  { type: "function", function: {
    name: "purchase",
    description: "采购清单管理（写操作，生成待确认卡片）。action=add 批量添加条目（同项目同物料自动合并；BOM 缺料批量采购用这个）；"
      + "update 改条目（需 item_id）；delete 删条目（需 item_id）；add_batch 登记一笔采购（需 item_id+qty）；"
      + "del_batch 删除一笔采购批次（需 batch_id）。条目 id 用 query 查 purchase_items 表。",
    parameters: { type: "object", required: ["action"], properties: {
      action: { type: "string", enum: ["add", "update", "delete", "add_batch", "del_batch"] },
      project: { type: "string", description: "add 用：项目名（归档用）" },
      items: { type: "array", description: "add 用：条目数组", items: { type: "object",
        required: ["name", "need_qty"], properties: {
        material_id: { type: "integer", description: "库存物料 id（有则填）" },
        name: { type: "string" }, code: { type: "string" },
        model: { type: "string" },
        need_qty: { type: "integer", description: "需求数量>0" },
        unit: { type: "string" }, remark: { type: "string" } } } },
      item_id: { type: "integer", description: "update/delete/add_batch 用：采购条目 id" },
      batch_id: { type: "integer", description: "del_batch 用：批次 id" },
      qty: { type: "integer", description: "add_batch 用：本次采购数量>0" },
      name: { type: "string" }, code: { type: "string" }, model: { type: "string" },
      need_qty: { type: "integer", description: "update 用：需求量>0" },
      unit: { type: "string" }, remark: { type: "string" } } } } },
];

// 写工具的固定回话：驱动模型产出面向用户的确认文案，而不是继续调工具
const ACTION_NOTE = "已生成待确认操作卡片（用户点击「确认执行」前不会执行任何写操作）。请向用户复述操作内容（对象/数量/仓库），并提醒用户在前端卡片上点击确认。不要重复调用本工具。";

// ---------------- query 工具：AI 只读 SQL 查询防线 ----------------

/** AI 只读查询允许的表（白名单）：只读 + 无敏感字段的业务表（users/app_settings/chat_messages 不给） */
const QUERY_TABLES = ["materials", "warehouses", "stock_records",
                      "purchase_items", "purchase_batches", "material_documents"];

/** 只读查询防线：仅 SELECT/WITH 单语句 + 表白名单 + 自动 LIMIT（与 Flask 版 _run_query 逐条对齐） */
async function runQuery(db: D1Database, sqlRaw: unknown, paramsRaw: unknown): Promise<any> {
  let sql = String(sqlRaw ?? "").trim();
  const low = sql.toLowerCase();
  if (!low.startsWith("select") && !low.startsWith("with")) {
    return { error: "query 只允许 SELECT 只读查询（写操作请用 stock/material/warehouse/purchase 工具）" };
  }
  if (sql.includes(";")) return { error: "只支持单条语句（不要带分号）" };
  const tables = [...low.matchAll(/(?:from|join)\s+([a-zA-Z_]\w*)/g)].map((m) => m[1]);
  if (!tables.length) return { error: "查询必须包含 FROM 子句" };
  const bad = tables.find((t) => !QUERY_TABLES.includes(t));
  if (bad) return { error: `表 ${bad} 不在允许范围（可查：${QUERY_TABLES.join("、")}）` };
  if (paramsRaw !== undefined && paramsRaw !== null && !Array.isArray(paramsRaw)) {
    return { error: "params 必须是数组（? 占位符对应的值）" };
  }
  const m = /\blimit\s+(\d+)/i.exec(sql);
  if (m) {
    if (Number(m[1]) > 200) sql = sql.slice(0, m.index) + "LIMIT 200" + sql.slice(m.index + m[0].length);
  } else {
    sql += " LIMIT 100";
  }
  let rows: any[];
  try {
    rows = await q(db, sql, ...((paramsRaw as unknown[]) ?? []));
  } catch (e) {
    return { error: `SQL 执行失败：${e instanceof Error ? e.message : String(e)}` };
  }
  const result: Record<string, unknown> = { count: rows.length, rows };
  if (rows.length >= 100) result.note = "结果已达上限被截断，请加 WHERE/LIMIT 缩小范围";
  return result;
}

/** 出入库/物料定位：material_id 优先；否则按 code 精确匹配编码 → 型号；再按名称/型号模糊。
 *  多候选时不猜：err 带 candidates 列表，由 AI 列给用户确认（借鉴参考实现）。 */
async function resolveStockTarget(db: D1Database, args: Record<string, any>):
  Promise<{ mid?: number; mname?: string; err?: any }> {
  const mid = parseIntSafe(args.material_id, 1);
  if (mid !== null) {
    const mat = await q<{ name: string }>(db, "SELECT name FROM materials WHERE id=?", mid);
    if (!mat.length) {
      return { err: { error: `物料 #${mid} 不存在，请先通过 query 查询获取真实 material_id` } };
    }
    return { mid, mname: mat[0].name };
  }
  const kw = String(args.code ?? "").trim();
  if (!kw) return { err: { error: "material_id 与 code 至少提供一个" } };
  const cols = "id, name, code, model, quantity, unit";
  let rows = await q<any>(db, `SELECT ${cols} FROM materials WHERE code=? OR model=?`, kw, kw);
  if (!rows.length) {
    const like = `%${kw}%`;
    rows = await q<any>(db,
      `SELECT ${cols} FROM materials WHERE name LIKE ? OR model LIKE ? LIMIT 10`, like, like);
  }
  if (!rows.length) {
    return { err: { error: `未找到「${kw}」对应的物料，可用 query 搜索确认` } };
  }
  if (rows.length > 1) {
    return { err: { error: `「${kw}」匹配到 ${rows.length} 个物料，请把候选列给用户确认后再用 material_id 重试`,
                    candidates: rows } };
  }
  return { mid: rows[0].id, mname: rows[0].name };
}

/** 写工具 → 待确认卡片。title 由服务端从参数生成（不信任 AI 的展示文案）。
 *  入参 name 是合并后的资源工具名（stock/material/warehouse/purchase），
 *  卡片 type 落库为细分动作（stock_in/update_material/...），执行端按 type 分发。
 *  返回 {action} 或 {err}（err 为 str 或带 candidates 的 dict）。 */
async function buildAction(db: D1Database, name: string, args: Record<string, any>):
  Promise<{ action?: any; err?: any }> {
  const base: Record<string, any> = {
    id: randHex8(), type: null, params: args,
    status: "pending", result: null, executed_at: null,
  };

  // ---------------- 出入库 ----------------
  if (name === "stock") {
    const op = String(args.op ?? "").trim();
    if (op !== "in" && op !== "out") return { err: "op 必须是 in（入库）或 out（出库）" };
    const qty = parseIntSafe(args.quantity, 1);
    if (qty === null) return { err: "quantity 必须是大于0的整数" };
    const target = await resolveStockTarget(db, args);
    if (target.err) return { err: target.err };
    // 卡片参数落库为解析后的 material_id（执行时不再依赖 code 二次解析）
    base.type = op === "in" ? "stock_in" : "stock_out";
    base.params = { ...args, material_id: target.mid };
    delete base.params.code;
    delete base.params.op;
    base.title = `${op === "in" ? "入库" : "出库"}：${target.mname}（#${target.mid}）×${qty}`;
    // 入库可指定挂到哪个仓库（仓库是物料的归属属性，出库不支持）
    if (args.warehouse_id) {
      if (op === "out") return { err: "出库不支持 warehouse_id（仓库归属在入库时指定）" };
      const wid = parseIntSafe(args.warehouse_id, 1);
      if (wid === null) return { err: "warehouse_id 必须是正整数" };
      const wh = await q<{ name: string }>(db, "SELECT name FROM warehouses WHERE id=?", wid);
      if (!wh.length) return { err: `仓库 #${wid} 不存在，先用 query 查 warehouses 表确认现有仓库 id` };
      base.title += `，挂到仓库「${wh[0].name}」`;
    }
    return { action: base };
  }

  // ---------------- 物料 ----------------
  if (name === "material") {
    const act = String(args.action ?? "").trim();
    if (act === "create") {
      const mname = String(args.name ?? "").trim();
      if (!mname) return { err: "create 时 name 不能为空" };
      const qty = parseIntSafe(args.quantity ?? 0, 0);
      if (qty === null) return { err: "quantity 必须是不小于0的整数" };
      let title = `新建物料：${mname}`;
      const code = String(args.code ?? "").trim();
      if (code) title += `（${code}）`;
      if (qty > 0) title += `，初始库存 ×${qty}`;
      base.type = "create_material";
      base.title = title;
      return { action: base };
    }
    if (act === "update") {
      // 仅改资料字段；数量必须走 stock 工具（保证流水留痕）
      if (args.quantity !== undefined && args.quantity !== null) {
        return { err: "update 不能改 quantity（库存数量请用 stock 工具出入库，保证流水留痕）" };
      }
      const target = await resolveStockTarget(db, args);
      if (target.err) return { err: target.err };
      const fields: Record<string, any> = {};
      for (const k of ["name", "code", "model", "category", "package", "brand", "unit", "description"]) {
        if (args[k] !== undefined && args[k] !== null) fields[k] = String(args[k]).trim();
      }
      if (args.warehouse_id !== undefined && args.warehouse_id !== null) {
        const wid = parseIntSafe(args.warehouse_id, 1);
        if (wid === null) return { err: "warehouse_id 必须是正整数" };
        const wh = await q(db, "SELECT id FROM warehouses WHERE id=?", wid);
        if (!wh.length) return { err: `仓库 #${wid} 不存在，先用 query 查 warehouses 表确认` };
        fields.warehouse_id = wid;
      }
      if (!Object.keys(fields).length) {
        return { err: "update 至少给一个要修改的字段（name/code/model/.../warehouse_id）" };
      }
      base.type = "update_material";
      base.params = { ...fields, material_id: target.mid };
      base.title = `修改物料：${target.mname}（#${target.mid}）——${Object.keys(fields).join("、")}`;
      return { action: base };
    }
    if (act === "delete") {
      const target = await resolveStockTarget(db, args);
      if (target.err) return { err: target.err };
      const row = await q<{ quantity: number }>(
        db, "SELECT quantity FROM materials WHERE id=?", target.mid!);
      let title = `删除物料：${target.mname}（#${target.mid}）`;
      if ((row[0]?.quantity ?? 0) > 0) title += `，⚠️ 现库存 ×${row[0].quantity} 将一并清空`;
      base.type = "delete_material";
      base.title = title;
      base.params = { material_id: target.mid };
      return { action: base };
    }
    return { err: "action 必须是 create / update / delete" };
  }

  // ---------------- 仓库 ----------------
  if (name === "warehouse") {
    const act = String(args.action ?? "").trim();
    if (act === "create") {
      const wname = String(args.name ?? "").trim();
      if (!wname) return { err: "create 时 name（仓库名）不能为空" };
      let parentId: number | null = null;
      if (args.parent_id) {
        parentId = parseIntSafe(args.parent_id, 1);
        if (parentId === null) return { err: "parent_id 必须是正整数" };
      }
      // 预校验父级/层级（与网页端一致），让卡片生成前就发现明显错误
      if (parentId) {
        const rows = await q<{ level: number; name: string }>(
          db, "SELECT level, name FROM warehouses WHERE id=?", parentId);
        if (!rows.length) return { err: `父级库 #${parentId} 不存在，先用 query 查 warehouses 表确认` };
        const lvl = rows[0].level + 1;
        if (lvl > MAX_WAREHOUSE_LEVEL) {
          return { err: `父级「${rows[0].name}」已是 ${MAX_WAREHOUSE_LEVEL} 级（最深），不能再建子库` };
        }
        base.title = `新建仓库：${wname}（${lvl} 级，父级「${rows[0].name}」）`;
      } else {
        base.title = `新建仓库：${wname}（1 级）`;
      }
      base.type = "create_warehouse";
      base.params = { name: wname, parent_id: parentId };
      return { action: base };
    }
    const wid = parseIntSafe(args.id, 1);
    if (wid === null) return { err: `${act} 需要 id（仓库 id，用 query 查 warehouses 表）` };
    const cur = await q<{ name: string; parent_id: number | null; level: number }>(
      db, "SELECT name, parent_id, level FROM warehouses WHERE id=?", wid);
    if (!cur.length) return { err: `仓库 #${wid} 不存在，先用 query 查 warehouses 表确认` };
    if (act === "update") {
      const newName = String(args.name ?? "").trim();
      const hasParent = args.parent_id !== undefined && args.parent_id !== null;
      if (!newName && !hasParent) {
        return { err: "update 至少给一个修改项（name 改名 / parent_id 换父级）" };
      }
      const params: Record<string, any> = { id: wid };
      let title = `修改仓库：${cur[0].name}（#${wid}）`;
      if (newName) { params.name = newName; title += ` 改名为「${newName}」`; }
      if (hasParent) {
        const pid = args.parent_id ? parseIntSafe(args.parent_id, 1) : 0;
        if (args.parent_id && pid === null) return { err: "parent_id 必须是正整数（0=提升为一级库）" };
        if (pid === wid) return { err: "不能把仓库挂到自己下面" };
        if (pid) {
          // 防环+层级预检：父级不能是自己的子孙；新层级=父级+1，且子树最深不超上限
          const all = await q<{ id: number; parent_id: number | null }>(
            db, "SELECT id, parent_id FROM warehouses");
          const descIds = descendantIds(all, wid);
          if (descIds.includes(pid!)) return { err: "不能把仓库挂到它自己的子库下面" };
          const prows = await q<{ level: number }>(db, "SELECT level FROM warehouses WHERE id=?", pid);
          if (!prows.length) return { err: `父级库 #${pid} 不存在` };
          const maxRow = await q<{ m: number }>(
            db, `SELECT MAX(level) AS m FROM warehouses WHERE id IN (${descIds.map(() => "?").join(",")})`,
            ...descIds);
          const depth = (maxRow[0]?.m ?? cur[0].level) - cur[0].level;  // 子树相对深度
          if (prows[0].level + 1 + depth > MAX_WAREHOUSE_LEVEL) {
            return { err: `移动后子树将超过 ${MAX_WAREHOUSE_LEVEL} 级上限` };
          }
        }
        params.parent_id = pid || null;
        title += "，换父级";
      }
      base.type = "update_warehouse";
      base.params = params;
      base.title = title;
      return { action: base };
    }
    if (act === "delete") {
      // 预校验（与网页端同防御）：库（含子库）有物料或有子库时拒绝
      const all = await q<{ id: number; parent_id: number | null }>(
        db, "SELECT id, parent_id FROM warehouses");
      const ids = descendantIds(all, wid);
      if (ids.length > 1) return { err: `「${cur[0].name}」下还有 ${ids.length - 1} 个子库，请先删除子库` };
      const cnt = await q<{ c: number }>(
        db, "SELECT COUNT(*) AS c FROM materials WHERE warehouse_id=?", wid);
      if (cnt[0]?.c > 0) {
        return { err: `「${cur[0].name}」下还有 ${cnt[0].c} 件物料，请先移出或删除物料` };
      }
      base.type = "delete_warehouse";
      base.params = { id: wid };
      base.title = `删除仓库：${cur[0].name}（#${wid}，${cur[0].level} 级）`;
      return { action: base };
    }
    return { err: "action 必须是 create / update / delete" };
  }

  // ---------------- 采购清单 ----------------
  if (name === "purchase") {
    const act = String(args.action ?? "").trim();
    if (act === "add") {
      const project = String(args.project ?? "").trim();
      const items = args.items;
      if (!project) return { err: "add 时 project（项目名）不能为空" };
      if (!Array.isArray(items) || !items.length) return { err: "add 时 items 必须是非空数组" };
      base.type = "add_purchase_items";
      base.title = `导入采购清单：项目「${project}」${items.length} 项`;
      return { action: base };
    }
    if (act === "update" || act === "delete" || act === "add_batch") {
      const iid = parseIntSafe(args.item_id, 1);
      if (iid === null) return { err: `${act} 需要 item_id（用 query 查 purchase_items 表）` };
      const rows = await q<{ project: string; name: string; need_qty: number }>(
        db, "SELECT project, name, need_qty FROM purchase_items WHERE id=?", iid);
      if (!rows.length) return { err: `采购条目 #${iid} 不存在，先用 query 查 purchase_items 表确认` };
      const item = rows[0];
      if (act === "update") {
        const fields: Record<string, any> = {};
        for (const k of ["name", "code", "model", "unit", "remark"]) {
          if (args[k] !== undefined && args[k] !== null) fields[k] = String(args[k]).trim();
        }
        if (args.need_qty !== undefined && args.need_qty !== null) {
          const nq = parseIntSafe(args.need_qty, 1);
          if (nq === null) return { err: "need_qty 必须是大于0的整数" };
          fields.need_qty = nq;
        }
        if (!Object.keys(fields).length) {
          return { err: "update 至少给一个要修改的字段（name/code/model/need_qty/unit/remark）" };
        }
        base.type = "update_purchase_item";
        base.params = { ...fields, item_id: iid };
        base.title = `修改采购项：${item.name}（#${iid}，项目「${item.project}」）`;
        return { action: base };
      }
      if (act === "delete") {
        base.type = "delete_purchase_item";
        base.params = { item_id: iid };
        base.title = `删除采购项：${item.name}（#${iid}，项目「${item.project}」，需求 ×${item.need_qty}）`;
        return { action: base };
      }
      // add_batch
      const qty = parseIntSafe(args.qty, 1);
      if (qty === null) return { err: "add_batch 时 qty（采购数量）必须是大于0的整数" };
      base.type = "add_purchase_batch";
      base.params = { item_id: iid, qty, remark: String(args.remark ?? "").trim() };
      base.title = `登记采购：${item.name}（项目「${item.project}」）+${qty}`;
      return { action: base };
    }
    if (act === "del_batch") {
      const bid = parseIntSafe(args.batch_id, 1);
      if (bid === null) return { err: "del_batch 需要 batch_id（用 query 查 purchase_batches 表）" };
      const rows = await q<{ qty: number; name: string; project: string }>(db,
        `SELECT b.qty, p.name, p.project FROM purchase_batches b
         JOIN purchase_items p ON p.id = b.purchase_item_id WHERE b.id=?`, bid);
      if (!rows.length) return { err: `采购批次 #${bid} 不存在，先用 query 查 purchase_batches 表确认` };
      base.type = "del_purchase_batch";
      base.params = { batch_id: bid };
      base.title = `删除采购批次：#${bid}（${rows[0].name} +${rows[0].qty}，项目「${rows[0].project}」）`;
      return { action: base };
    }
    return { err: "action 必须是 add / update / delete / add_batch / del_batch" };
  }
  return { err: `未知工具 ${name}` };
}

/** 执行一次 AI 工具调用。只读工具真执行；写工具拦截转卡片。
 *  返回 {result: 给模型的结果, action?: 待确认卡片} */
async function runTool(db: D1Database, name: string, args: Record<string, any>):
  Promise<{ result: any; action?: any }> {
  if (name === "query") {
    return { result: await runQuery(db, args.sql, args.params) };
  }
  if (name === "lcsc_search") {
    const kw = String(args.keyword ?? "").trim();
    if (!kw) return { result: { error: "keyword 不能为空" } };
    let rows: any[] = [];
    try {
      rows = await searchLcsc(kw);
    } catch {
      rows = [];
    }
    const slim = rows.map((r) => ({
      code: r.code ?? "", model: r.model ?? "", name: r.name ?? "",
      brand: r.brand ?? "", package: r.package ?? "", stock: r.stock ?? "", price: r.price ?? "",
    }));
    if (!slim.length) {
      return { result: { count: 0, items: [],
        note: "立创搜索无结果（可能被风控或网络受限），建议用户提供立创C编号" } };
    }
    return { result: { count: slim.length, items: slim } };
  }
  if (name === "check_bom") {
    const r = await bomCheckCore(db, args.items);
    if ("error" in r) return { result: { error: r.error } };
    if (r.summary.total > 30) {        // token 经济：超 30 行只回汇总 + 非 ok 行
      const bad = r.results.filter((x: any) => x.status !== "ok");
      return { result: { summary: r.summary, results: bad,
        note: `共 ${r.summary.total} 行，超出 30 行仅返回非 ok 行（${bad.length} 条）` } };
    }
    return { result: r };
  }
  // ---------- 写操作：拦截转"待确认卡片" ----------
  const built = await buildAction(db, name, args);
  if (built.err) {
    // err 可能是 str（普通参数错误）或对象（多候选等带附加数据的错误）
    return { result: typeof built.err === "string" ? { error: built.err } : built.err };
  }
  return { result: { ok: true, action_id: built.action.id, note: ACTION_NOTE }, action: built.action };
}

/** 构造 tool 角色消息（OpenAI 协议）：结果序列化为 JSON 文本回给模型。
 *  截断 4000 字符防爆 token（借鉴参考实现）。 */
function toolMsg(tcId: string, result: any) {
  let content = JSON.stringify(result);
  if (content.length > 4000) content = content.slice(0, 4000) + "…(结果过长已截断)";
  return { role: "tool", tool_call_id: tcId, content };
}

// ---------------- AI 对话主入口 ----------------

chat.post("/api/chat", requireLogin, async (c) => {
  const d = await getJson(c);
  const message = String(d.message ?? "").trim();
  if (!message) return c.json({ error: "消息不能为空" }, 400);
  const db = c.env.DB;
  const cfg = await aiCfg(db);
  if (!cfg.base_url || !cfg.api_key || !cfg.model) {
    return c.json({ error: "AI 接口未配置，请联系管理员在「系统管理」页配置" }, 400);
  }
  const u = c.get("user")!;
  await ensureUsageCol(db);

  // 1) 落库用户消息（先落库再查历史，本轮 user 消息自然包含在上下文里）
  await db.prepare("INSERT INTO chat_messages(user_id, role, content) VALUES (?,?,?)")
    .bind(u.id, "user", message).run();

  // 2) 组装上下文。★历史重放必须降级：库里没有 tool_calls 结构，
  //   卡片以文字摘要拼进 content，避免发给模型缺 tool 消息导致 400
  const hRows = await q<any>(
    db, "SELECT role, content, actions_json FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 20", u.id);
  const history = hRows.reverse();
  const msgs: any[] = [{ role: "system", content: await systemPrompt(db, u.username) }];
  for (const h of history) {
    let content = h.content ?? "";
    if (h.actions_json) {
      try {
        for (const a of JSON.parse(h.actions_json)) {
          const res = a.result ?? {};
          const tail = `（结果：${res.error ?? res.quantity ?? res.material_id ?? "完成"}）`;
          content += `\n[操作卡片 ${a.status}: ${a.title}]${tail}`;
        }
      } catch { /* 损坏 JSON 静默跳过 */ }
    }
    msgs.push({ role: h.role, content });
  }

  // 3) tool loop：模型给纯文本即结束；给 tool_calls 则逐个执行后继续
  const actions: any[] = [];
  let toolCount = 0;
  let content = "";
  // 全程 token 用量累加（每轮 LLM 调用的 usage 求和）
  const usageTotal: Record<string, number> = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
  const addUsage = (u: any) => {
    for (const k of Object.keys(usageTotal)) usageTotal[k] += Number(u?.[k]) || 0;
  };
  let done = false;
  for (let round = 0; round < MAX_LLM_ROUNDS; round++) {
    let r: { content: string; toolCalls: any[]; usage: any };
    try {
      r = await llmChat(cfg, msgs, AI_TOOLS);
    } catch (e) {
      // LLM 失败也落一条 assistant 消息，保证历史连续可追溯
      const emsg = e instanceof Error ? e.message : String(e);
      await db.prepare("INSERT INTO chat_messages(user_id, role, content) VALUES (?,?,?)")
        .bind(u.id, "assistant", `[AI调用失败] ${emsg}`).run();
      return c.json({ error: `AI 调用失败：${emsg}` }, 502);
    }
    addUsage(r.usage);
    content = r.content;
    if (!r.toolCalls.length) { done = true; break; }        // 模型给出最终文本，正常出口
    msgs.push({ role: "assistant", content: content || null, tool_calls: r.toolCalls });
    for (const tc of r.toolCalls) {
      toolCount++;
      const fn = tc.function ?? {};
      const tname = String(fn.name ?? "");
      let args: Record<string, any> | null = null;
      try {
        const parsed = JSON.parse(fn.arguments || "{}");
        if (typeof parsed === "object" && parsed !== null) args = parsed;
      } catch { /* 非法 JSON 走 args=null */ }
      if (!args) {
        msgs.push(toolMsg(tc.id, { error: "arguments 必须是合法的 JSON 对象" }));
        continue;
      }
      if (toolCount > MAX_TOOL_CALLS) {
        msgs.push(toolMsg(tc.id, { error: "已达本轮工具调用上限，请直接用现有信息回答用户" }));
        continue;
      }
      // 单个工具抛异常不许炸整轮对话：转为错误结果让模型自纠/向用户说明
      let result: any, action: any;
      try {
        ({ result, action } = await runTool(db, tname, args));
      } catch (e) {
        result = { error: `${e instanceof Error ? e.message : String(e)}` };
        action = undefined;
      }
      if (action) actions.push(action);
      msgs.push(toolMsg(tc.id, result));
    }
  }
  if (!done) {
    // 8 轮用尽模型仍在要工具：追加一次不带 tools 的请求逼出文本总结
    try {
      const r = await llmChat(cfg, msgs, null);
      addUsage(r.usage);
      content = r.content;
    } catch {
      content = content || "（本轮工具调用已达上限，未生成总结。请换种问法或缩小范围后重试。）";
    }
  }

  // 4) 落库 assistant 消息 + 待确认卡片 + token 用量
  const actionsJson = actions.length ? JSON.stringify(actions) : null;
  const usageJson = usageTotal.total_tokens > 0 ? JSON.stringify(usageTotal) : null;
  const ins = await db.prepare(
    "INSERT INTO chat_messages(user_id, role, content, actions_json, usage_json) VALUES (?,?,?,?,?)")
    .bind(u.id, "assistant", content || "", actionsJson, usageJson).run();
  return c.json({ message_id: ins.meta.last_row_id, content: content || "", actions,
                  usage: usageJson ? usageTotal : null });
});

// ---------------- 历史加载 / 清空 ----------------

chat.get("/api/chat/history", requireLogin, async (c) => {
  const u = c.get("user")!;
  await ensureUsageCol(c.env.DB);
  const before = parseIntSafe(c.req.query("before_id") ?? "0", 1) ?? 0;
  let limit = parseIntSafe(c.req.query("limit") ?? "30", 1) ?? 30;
  limit = Math.min(limit, 100);
  const sql = before
    ? "SELECT id, role, content, actions_json, usage_json, created_at FROM chat_messages WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?"
    : "SELECT id, role, content, actions_json, usage_json, created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT ?";
  const params: unknown[] = before ? [u.id, before, limit] : [u.id, limit];
  const rows = await q<any>(c.env.DB, sql, ...params);
  const out = rows.reverse().map((r) => {   // 反转成 旧→新，前端按序渲染
    let acts: any[] = [];
    if (r.actions_json) {
      try { acts = JSON.parse(r.actions_json); } catch { acts = []; }
    }
    let usage: any = null;
    if (r.usage_json) {
      try { usage = JSON.parse(r.usage_json); } catch { usage = null; }
    }
    return { id: r.id, role: r.role, content: r.content,
             created_at: r.created_at, actions: acts, usage };
  });
  return c.json({ messages: out, has_more: rows.length === limit });
});

chat.delete("/api/chat/history", requireLogin, async (c) => {
  const u = c.get("user")!;
  await c.env.DB.prepare("DELETE FROM chat_messages WHERE user_id=?").bind(u.id).run();
  return c.json({ ok: true });
});

// ---------------- 卡片确认执行（幂等状态机） ----------------

/** 执行一张已确认卡片（operator 留痕 ai:用户名）。
 *  出错带 error；keep_pending=true（库存不足/删除条件不满足）表示保持待确认可重试 */
async function executeAction(db: D1Database, act: any, username: string): Promise<any> {
  const t = act.type as string;
  const p = act.params ?? {};
  const operator = `ai:${username}`;
  if (t === "stock_in" || t === "stock_out") {
    const mid = parseIntSafe(p.material_id, 1);
    const qty = parseIntSafe(p.quantity, 1);
    if (mid === null || qty === null) return { error: "参数无效（material_id / quantity）" };
    const r = await applyStock(db, t === "stock_in" ? "in" : "out", mid, qty, operator,
      String(p.remark ?? "").trim() || "AI 对话确认执行");
    if (r.err) {
      // 库存不足(400)：保持 pending，补货后可重试；物料不存在(404)等永久失败置 failed
      return { error: r.err[0], keep_pending: r.err[1] === 400 };
    }
    const result: Record<string, unknown> = { material_id: mid, quantity: r.qty };
    // 入库卡片带 warehouse_id：入库成功后同步物料归属仓库
    if (t === "stock_in" && p.warehouse_id) {
      const wid = parseIntSafe(p.warehouse_id, 1);
      const wh = wid ? await q(db, "SELECT id FROM warehouses WHERE id=?", wid) : [];
      if (wid === null || !wh.length) {
        result.error = `仓库 #${p.warehouse_id} 已不存在，物料已入库但未挂库（可在物料管理页手动指定）`;
        return result;
      }
      await db.prepare("UPDATE materials SET warehouse_id=? WHERE id=?").bind(wid, mid).run();
      result.warehouse_id = wid;
    }
    return result;
  }
  if (t === "delete_material") {
    const mid = parseIntSafe(p.material_id, 1);
    if (mid === null) return { error: "参数无效（material_id）" };
    const rows = await q<{ name: string; quantity: number }>(
      db, "SELECT name, quantity FROM materials WHERE id=?", mid);
    if (!rows.length) return { error: `物料 #${mid} 不存在（可能已被删除）` };
    await db.prepare("DELETE FROM materials WHERE id=?").bind(mid).run();
    const res: Record<string, unknown> = { material_id: mid, name: rows[0].name };
    if (rows[0].quantity > 0) res.discarded_stock = rows[0].quantity;
    return res;
  }
  if (t === "create_material") {
    const mname = String(p.name ?? "").trim();
    if (!mname) return { error: "name 缺失" };
    const qty = parseIntSafe(p.quantity ?? 0, 0) ?? 0;
    const wid = parseIntSafe(p.warehouse_id ?? 0, 1) ?? 0;
    const ins = await db.prepare(
      `INSERT INTO materials(name, code, model, category, package, brand,
                             description, quantity, unit, warehouse_id, lcsc_url)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
      .bind(mname, String(p.code ?? "").trim(), String(p.model ?? "").trim(),
            String(p.category ?? "").trim(), String(p.package ?? "").trim(),
            String(p.brand ?? "").trim(), String(p.description ?? "").trim(),
            qty, String(p.unit ?? "个").trim() || "个", wid || null, "")
      .run();
    const newId = ins.meta.last_row_id;
    const note = await autoFetchMaterial(db, newId);   // C编号自动抓图/手册外链+参数（失败静默）
    const res: Record<string, unknown> = { material_id: newId };
    if (note) res.note = note;
    return res;
  }
  if (t === "add_purchase_items") {
    const r = await purchaseBulkCore(db, p.project, p.items, operator);
    if ("error" in r) return { error: r.error };
    return r;
  }
  if (t === "create_warehouse") {
    const name = String(p.name ?? "").trim();
    if (!name) return { error: "库名不能为空" };
    const parentId = p.parent_id ?? null;
    let level = 1;
    if (parentId) {
      const parent = await q<{ level: number }>(db, "SELECT level FROM warehouses WHERE id=?", parentId);
      if (!parent.length) return { error: `父级库 #${parentId} 不存在` };
      level = parent[0].level + 1;
    }
    if (level > MAX_WAREHOUSE_LEVEL) return { error: `最多支持 ${MAX_WAREHOUSE_LEVEL} 级库` };
    const r = await db.prepare("INSERT INTO warehouses(name, parent_id, level) VALUES (?,?,?)")
      .bind(name, parentId, level).run();
    return { warehouse_id: r.meta.last_row_id, name, level };
  }
  // ---------------- v261016.1 新增卡片类型 ----------------
  if (t === "update_material") {
    const mid = parseIntSafe(p.material_id, 1);
    if (mid === null) return { error: "参数无效（material_id）" };
    const exist = await q(db, "SELECT id FROM materials WHERE id=?", mid);
    if (!exist.length) return { error: `物料 #${mid} 不存在（可能已被删除）` };
    // 白名单字段动态拼 SET（quantity 在建卡阶段已被拦截，这里再兜底）
    const allowed = ["name", "code", "model", "category", "package", "brand",
                     "unit", "description", "warehouse_id"];
    const sets: string[] = [];
    const vals: unknown[] = [];
    for (const k of allowed) {
      if (k in p) { sets.push(`${k}=?`); vals.push(p[k]); }
    }
    if (!sets.length) return { error: "没有要修改的字段" };
    sets.push("updated_at=?");
    vals.push(nowStrLocal());
    vals.push(mid);
    await db.prepare(`UPDATE materials SET ${sets.join(", ")} WHERE id=?`).bind(...vals).run();
    const note = await autoFetchMaterial(db, mid);   // 补了 C 编号时自动抓图/手册/参数（幂等，失败静默）
    const res: Record<string, unknown> = { material_id: mid, updated: allowed.filter((k) => k in p) };
    if (note) res.note = note;
    return res;
  }
  if (t === "update_warehouse") {
    const wid = parseIntSafe(p.id, 1);
    const rows = wid ? await q<{ level: number }>(db, "SELECT level FROM warehouses WHERE id=?", wid) : [];
    if (!rows.length) return { error: `仓库 #${p.id} 不存在（可能已被删除）` };
    if (p.name) {
      await db.prepare("UPDATE warehouses SET name=? WHERE id=?").bind(String(p.name).trim(), wid).run();
    }
    if ("parent_id" in p) {
      const pid = p.parent_id ?? null;
      let newLevel = 1;
      if (pid) {
        const prows = await q<{ level: number }>(db, "SELECT level FROM warehouses WHERE id=?", pid);
        if (!prows.length) {
          return { error: p.name ? `父级库 #${pid} 已不存在，改名部分已生效` : `父级库 #${pid} 已不存在` };
        }
        newLevel = prows[0].level + 1;
      }
      // 移动本节点并同步所有子孙的 level（相对深度不变）
      const delta = newLevel - rows[0].level;
      const all = await q<{ id: number; parent_id: number | null }>(
        db, "SELECT id, parent_id FROM warehouses");
      const ids = descendantIds(all, wid!);
      await db.prepare(
        `UPDATE warehouses SET level=level+? WHERE id IN (${ids.map(() => "?").join(",")})`)
        .bind(delta, ...ids).run();
      await db.prepare("UPDATE warehouses SET parent_id=? WHERE id=?").bind(pid, wid).run();
    }
    return { warehouse_id: wid };
  }
  if (t === "delete_warehouse") {
    const wid = parseIntSafe(p.id, 1);
    const rows = wid ? await q<{ name: string }>(db, "SELECT name FROM warehouses WHERE id=?", wid) : [];
    if (!rows.length) return { error: `仓库 #${p.id} 不存在（可能已被删除）` };
    // 执行时再校验一次（卡片生成后库里可能又进了物料/子库）
    const all = await q<{ id: number; parent_id: number | null }>(
      db, "SELECT id, parent_id FROM warehouses");
    const ids = descendantIds(all, wid!);
    if (ids.length > 1) {
      return { error: `「${rows[0].name}」下已有子库，请先删除子库`, keep_pending: true };
    }
    const cnt = await q<{ c: number }>(
      db, "SELECT COUNT(*) AS c FROM materials WHERE warehouse_id=?", wid);
    if (cnt[0]?.c > 0) {
      return { error: `「${rows[0].name}」下还有 ${cnt[0].c} 件物料，请先移出或删除物料`,
               keep_pending: true };
    }
    await db.prepare("DELETE FROM warehouses WHERE id=?").bind(wid).run();
    return { warehouse_id: wid, name: rows[0].name };
  }
  if (t === "update_purchase_item") {
    const iid = parseIntSafe(p.item_id, 1);
    const exist = iid ? await q(db, "SELECT id FROM purchase_items WHERE id=?", iid) : [];
    if (!exist.length) return { error: `采购条目 #${p.item_id} 不存在（可能已被删除）` };
    const allowed = ["name", "code", "model", "need_qty", "unit", "remark"];
    const sets: string[] = [];
    const vals: unknown[] = [];
    for (const k of allowed) {
      if (k in p) { sets.push(`${k}=?`); vals.push(p[k]); }
    }
    if (!sets.length) return { error: "没有要修改的字段" };
    vals.push(iid);
    await db.prepare(`UPDATE purchase_items SET ${sets.join(", ")} WHERE id=?`).bind(...vals).run();
    return { item_id: iid, updated: allowed.filter((k) => k in p) };
  }
  if (t === "delete_purchase_item") {
    const iid = parseIntSafe(p.item_id, 1);
    const exist = iid ? await q(db, "SELECT id FROM purchase_items WHERE id=?", iid) : [];
    if (!exist.length) return { error: `采购条目 #${p.item_id} 不存在（可能已被删除）` };
    // 先删批次再删条目（不依赖外键级联，三种库行为一致）
    await db.prepare("DELETE FROM purchase_batches WHERE purchase_item_id=?").bind(iid).run();
    await db.prepare("DELETE FROM purchase_items WHERE id=?").bind(iid).run();
    return { item_id: iid };
  }
  if (t === "add_purchase_batch") {
    const iid = parseIntSafe(p.item_id, 1);
    const qty = parseIntSafe(p.qty, 1);
    if (iid === null || qty === null) return { error: "参数无效（item_id / qty）" };
    const exist = await q(db, "SELECT id FROM purchase_items WHERE id=?", iid);
    if (!exist.length) return { error: `采购条目 #${iid} 不存在（可能已被删除）` };
    const r = await db.prepare(
      "INSERT INTO purchase_batches(purchase_item_id, qty, remark, created_by) VALUES (?,?,?,?)")
      .bind(iid, qty, String(p.remark ?? "").trim(), operator).run();
    return { batch_id: r.meta.last_row_id, item_id: iid, qty };
  }
  if (t === "del_purchase_batch") {
    const bid = parseIntSafe(p.batch_id, 1);
    if (bid === null) return { error: "参数无效（batch_id）" };
    const r = await db.prepare("DELETE FROM purchase_batches WHERE id=?").bind(bid).run();
    if (!(r.meta.changes ?? 0)) return { error: `采购批次 #${bid} 不存在（可能已被删除）` };
    return { batch_id: bid };
  }
  return { error: `未知操作类型 ${t}` };
}

chat.post("/api/chat/execute", requireLogin, async (c) => {
  const d = await getJson(c);
  const msgId = parseIntSafe(d.message_id, 1);
  const actionId = String(d.action_id ?? "").trim();
  // all=true 为批量模式（一键确认/取消本消息全部 pending 卡片），无需 action_id
  if (msgId === null || (!actionId && !d.all)) {
    return c.json({ error: "message_id / action_id 不能为空" }, 400);
  }
  const u = c.get("user")!;
  const rows = await q<{ user_id: number; actions_json: string | null }>(
    c.env.DB, "SELECT user_id, actions_json FROM chat_messages WHERE id=?", msgId);
  if (!rows.length || rows[0].user_id !== u.id) {
    return c.json({ error: "消息不存在或不属于当前用户" }, 403);
  }
  let acts: any[] = [];
  try { acts = rows[0].actions_json ? JSON.parse(rows[0].actions_json) : []; } catch { acts = []; }
  const now = nowStrLocal();

  // ---------- 批量模式：一键确认/取消本消息全部 pending 卡片（与 Flask 端同构） ----------
  if (d.all) {
    const pendings = acts.filter((a) => a?.status === "pending");
    if (!pendings.length) return c.json({ error: "该消息没有待确认的卡片" }, 409);
    let okN = 0, holdN = 0, failN = 0;
    for (const act of pendings) {
      if (d.cancel) {   // 批量取消：全部直接置 cancelled
        act.status = "cancelled";
        act.executed_at = now;
        continue;
      }
      const result = await executeAction(c.env.DB, act, u.username);
      act.result = result;
      if (result?.error && result?.keep_pending) {
        // 库存不足等可恢复失败：保持 pending 可重试，计入 held
        holdN++;
        continue;
      }
      act.status = result?.error ? "failed" : "executed";
      act.executed_at = now;
      if (result?.error) failN++; else okN++;
    }
    await c.env.DB.prepare("UPDATE chat_messages SET actions_json=? WHERE id=?")
      .bind(JSON.stringify(acts), msgId).run();
    if (d.cancel) return c.json({ ok: true, actions: acts, summary: { cancelled: pendings.length } });
    return c.json({ ok: true, actions: acts, summary: { executed: okN, held: holdN, failed: failN } });
  }

  const act = acts.find((a) => a?.id === actionId);
  if (!act) return c.json({ error: "操作卡片不存在" }, 404);
  if (act.status !== "pending") {
    return c.json({ error: `该卡片已处理（状态：${act.status}），不可重复操作` }, 409);
  }

  if (d.cancel) {
    act.status = "cancelled";
    act.executed_at = now;
  } else {
    const result = await executeAction(c.env.DB, act, u.username);
    act.result = result;
    if (result?.error && result?.keep_pending) {
      // 库存不足/删除条件不满足：保持 pending（处理后可再点确认），不推进状态机
      await c.env.DB.prepare("UPDATE chat_messages SET actions_json=? WHERE id=?")
        .bind(JSON.stringify(acts), msgId).run();
      return c.json({ error: result.error, action: act }, 400);
    }
    act.status = result?.error ? "failed" : "executed";
    act.executed_at = now;
  }
  await c.env.DB.prepare("UPDATE chat_messages SET actions_json=? WHERE id=?")
    .bind(JSON.stringify(acts), msgId).run();
  return c.json({ ok: true, action: act });
});

export default chat;
