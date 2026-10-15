/**
 * AI 对话模块（与 Flask 版 app.py 的 /api/chat* 路由 1:1 镜像）
 * OpenAI 兼容协议（Kimi / DeepSeek / GLM 等）+ function calling tool loop + 待确认卡片。
 *
 * 安全模式（人确认后执行）：
 * - 只读工具（搜索/仓库/立创/BOM/采购查询）真执行，结果回给模型；
 * - 写工具（建料/出入库/导入采购）一律拦截转 action 卡片，用户在前端点「确认执行」
 *   后由 /api/chat/execute 落库，operator 留痕为 ai:用户名；
 * - 幂等状态机：pending → executed / cancelled / failed，非 pending 返回 409 防双击重放。
 */
import { Hono } from "hono";
import type { AppEnv } from "./index";
import { requireLogin, getJson, parseIntSafe } from "./auth";
import { q, applyStock, searchCore, bomCheckCore } from "./core";
import { searchLcsc } from "./lcsc";
import { aiCfg, autoFetchMaterial, purchaseBulkCore, purchaseListCore } from "./routes";

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

/** OpenAI 兼容 /chat/completions 单一收敛调用点。
 *  返回 {content, toolCalls}；非 200 抛错（外层负责落失败消息并返 502）。
 *  鲁棒性（借鉴参考实现）：temperature 三档降级 0.3 → 不带 → 1
 *  （部分模型如 Kimi 思考模型限制取值）；
 *  429 限速不做长等待（Worker 挂钟成本高），直接抛出明确提示让前端稍后重试。 */
async function llmChat(
  cfg: { base_url: string; api_key: string; model: string },
  messages: any[],
  tools?: any[] | null,
): Promise<{ content: string; toolCalls: any[] }> {
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
      const msg = (await r.json()).choices[0].message;
      return { content: msg.content ?? "", toolCalls: msg.tool_calls ?? [] };
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

/** 系统提示词：角色 + 实时仓库/物料概况 + 工具规范 + 安全模式说明 + 电子领域常识 + 回复风格。
 *  仓库列表与物料总数每次请求现查注入（参考实现的做法），让 AI 建料/答问时有实时依据。 */
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

【工具使用规范】
1. 用户问"有哪些库存/库存情况/看看库存"等无关键词问题时，直接调 search_materials 不带参数列出全部，不要反问用户要关键词。
2. 出入库前必须先用 search_materials 找到物料，material_id 只能来自搜索结果，禁止编造；用户直接给了编码（如 C621395）时可跳过搜索，直接给 stock_in/stock_out 传 code。
3. stock_in / stock_out / create_material / add_purchase_items 是写操作：调用后只会生成"待确认卡片"，并未真正执行。你必须向用户清晰复述操作内容（物料、数量、仓库），并提醒用户点击卡片上的「确认执行」按钮；用户没确认就当作未执行。
4. 查仓库列表用 list_warehouses；查出入库历史用 stock_records；信息不足（数量/仓库/编码）时先问清再动手，禁止猜测执行。
5. 库存中搜不到的物料：用 lcsc_search 搜立创商城（结果含立创C编号），把候选列成简表让用户选；用户确认编号后用 create_material 建料（填了 C 编号会自动抓取图片/手册/参数，无需用户手填），再按需办理入库。
6. BOM 核对用 check_bom；用户想采购缺料时用 add_purchase_items（问清项目名，BOM 缺料可批量导入）。

【电子领域常识】
- 电容容值换算：1µF = 1000nF（0.1µF = 100nF；三位数标注 104 = 100nF、105 = 1µF）。用户给出的不同写法若等值，主动指出并确认是否同一种物料。
- 电阻：10k = 10kΩ = 10000Ω；贴片三位数标注 103 = 10kΩ。
- 封装 0402/0603/0805/1206 是尺寸规格；同容值不同封装/耐压/精度是不同物料，用户未说明时要问清。

【回复风格】简体中文；简洁分点，必要时用小表格；涉及写操作的回复末尾必须提醒用户确认卡片。`;
}

// 10 个工具的 OpenAI function calling 声明（只读 6 个真执行 + 写 4 个转卡片）
// description 写成"使用说明书"（用途+示例+前置条件），是引导模型正确调用的关键（借鉴参考实现）
const AI_TOOLS = [
  { type: "function", function: {
    name: "search_materials",
    description: "查询库存物料。不带任何参数 = 列出全部库存（用户问'有哪些库存/库存情况'时用）；"
      + "q 模糊匹配名称/编码/型号（如 '电容'、'10k'、'0603'）；code 精确匹配编码（如立创C编号 C621395）。"
      + "出入库/BOM 核对前先用它拿到 material_id。",
    parameters: { type: "object", properties: {
      q: { type: "string", description: "模糊关键词（匹配名称/编码/型号），留空=全部" },
      code: { type: "string", description: "物料编码精确匹配（如立创C编号 C621395）" } } } } },
  { type: "function", function: {
    name: "stock_records",
    description: "查询出入库流水记录。不带参数 = 最近 10 条（用户问'最近谁动了库存/出入库记录'时用）；"
      + "keyword 按物料名称/编码模糊过滤（如 'C25804'、'电容'）。",
    parameters: { type: "object", properties: {
      keyword: { type: "string", description: "物料名称/编码关键词，留空=最近记录" },
      limit: { type: "integer", description: "返回条数，默认10，上限30" } } } } },
  { type: "function", function: {
    name: "list_warehouses",
    description: "列出全部仓库（id/名称/层级/父库），入库前用它确认仓库 id。",
    parameters: { type: "object", properties: {} } } },
  { type: "function", function: {
    name: "lcsc_search",
    description: "搜索立创商城（库存没有的物料来这里找候选），返回立创C编号/型号/品牌/封装/库存/价格。用户选定 C 编号后可用 create_material 建料并自动补全参数。",
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
    name: "query_purchase",
    description: "查询采购清单（可按项目过滤），返回每条的需求量/已购量/状态（open未采/partial部分/done完成）。",
    parameters: { type: "object", properties: {
      project: { type: "string", description: "项目名（空=查全部）" } } } } },
  { type: "function", function: {
    name: "create_material",
    description: "新建物料（写操作，生成待确认卡片）。填了立创C编号会自动抓取图片/数据手册/参数。",
    parameters: { type: "object", required: ["name"], properties: {
      name: { type: "string", description: "物料名称" },
      code: { type: "string", description: "立创C编号（强烈建议填，自动补全参数）" },
      model: { type: "string", description: "型号" },
      category: { type: "string" }, package: { type: "string" },
      brand: { type: "string" }, unit: { type: "string" },
      warehouse_id: { type: "integer", description: "仓库 id（list_warehouses 查）" },
      quantity: { type: "integer", description: "初始库存，默认0" },
      description: { type: "string" } } } } },
  { type: "function", function: {
    name: "stock_in",
    description: "入库（写操作，生成待确认卡片）。material_id 来自 search_materials 结果；"
      + "用户直接给了编码时也可只传 code（精确匹配编码或型号，多候选会返回列表让你请用户确认，此时无需先搜索）。",
    parameters: { type: "object", required: ["quantity"], properties: {
      material_id: { type: "integer", description: "物料 id（与 code 二选一）" },
      code: { type: "string", description: "物料编码/型号（与 material_id 二选一）" },
      quantity: { type: "integer", description: "数量>0" },
      remark: { type: "string", description: "备注（可选）" } } } } },
  { type: "function", function: {
    name: "stock_out",
    description: "出库（写操作，生成待确认卡片）。material_id 来自 search_materials 结果；"
      + "用户直接给了编码时也可只传 code（精确匹配编码或型号，多候选会返回列表让你请用户确认）。出库前确认库存充足。",
    parameters: { type: "object", required: ["quantity"], properties: {
      material_id: { type: "integer", description: "物料 id（与 code 二选一）" },
      code: { type: "string", description: "物料编码/型号（与 material_id 二选一）" },
      quantity: { type: "integer", description: "数量>0" },
      remark: { type: "string", description: "备注（可选）" } } } } },
  { type: "function", function: {
    name: "add_purchase_items",
    description: "向采购清单批量添加条目（写操作，生成待确认卡片）。同项目同物料自动合并累加需求量。BOM 缺料批量采购用这个工具。",
    parameters: { type: "object", required: ["project", "items"], properties: {
      project: { type: "string", description: "项目名（归档用）" },
      items: { type: "array", items: { type: "object", required: ["name", "need_qty"], properties: {
        material_id: { type: "integer", description: "库存物料 id（有则填）" },
        name: { type: "string" }, code: { type: "string" },
        model: { type: "string" },
        need_qty: { type: "integer", description: "需求数量>0" },
        unit: { type: "string" }, remark: { type: "string" } } } } } } } },
];

// 写工具的固定回话：驱动模型产出面向用户的确认文案，而不是继续调工具
const ACTION_NOTE = "已生成待确认操作卡片（用户点击「确认执行」前不会执行任何写操作）。请向用户复述操作内容（对象/数量/仓库），并提醒用户在前端卡片上点击确认。不要重复调用本工具。";

/** 出入库目标物料解析：material_id 优先；否则按 code 精确匹配编码 → 型号；再按名称/型号模糊。
 *  多候选时不猜：err 带 candidates 列表，由 AI 列给用户确认（借鉴参考实现）。 */
async function resolveStockTarget(db: D1Database, args: Record<string, any>):
  Promise<{ mid?: number; mname?: string; err?: any }> {
  const mid = parseIntSafe(args.material_id, 1);
  if (mid !== null) {
    const mat = await q<{ name: string }>(db, "SELECT name FROM materials WHERE id=?", mid);
    if (!mat.length) {
      return { err: { error: `物料 #${mid} 不存在，请先通过 search_materials 获取真实 material_id` } };
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
    return { err: { error: `未找到「${kw}」对应的物料，可用 search_materials 搜索确认` } };
  }
  if (rows.length > 1) {
    return { err: { error: `「${kw}」匹配到 ${rows.length} 个物料，请把候选列给用户确认后再用 material_id 重试`,
                    candidates: rows } };
  }
  return { mid: rows[0].id, mname: rows[0].name };
}

/** 写工具 → 待确认卡片。title 由服务端从参数生成（不信任 AI 的展示文案）。
 *  返回 {action} 或 {err}（err 是给模型自纠的参数错误说明） */
function buildAction(name: string, args: Record<string, any>):
  { action?: any; err?: string } {
  const base: Record<string, any> = {
    id: randHex8(), type: name, params: args,
    status: "pending", result: null, executed_at: null,
  };
  if (name === "create_material") {
    const mname = String(args.name ?? "").trim();
    if (!mname) return { err: "name 不能为空" };
    const qty = parseIntSafe(args.quantity ?? 0, 0);
    if (qty === null) return { err: "quantity 必须是不小于0的整数" };
    let title = `新建物料：${mname}`;
    const code = String(args.code ?? "").trim();
    if (code) title += `（${code}）`;
    if (qty > 0) title += `，初始库存 ×${qty}`;
    base.title = title;
    return { action: base };
  }
  if (name === "add_purchase_items") {
    const project = String(args.project ?? "").trim();
    const items = args.items;
    if (!project) return { err: "project（项目名）不能为空" };
    if (!Array.isArray(items) || !items.length) return { err: "items 必须是非空数组" };
    base.title = `导入采购清单：项目「${project}」${items.length} 项`;
    return { action: base };
  }
  return { err: `未知工具 ${name}` };
}

/** 执行一次 AI 工具调用。只读工具真执行；写工具拦截转卡片。
 *  返回 {result: 给模型的结果, action?: 待确认卡片} */
async function runTool(db: D1Database, name: string, args: Record<string, any>):
  Promise<{ result: any; action?: any }> {
  if (name === "search_materials") {
    // 空参数合法（=列出全部库存）：用户问"有哪些库存"时 AI 有合法路径可走
    const rows = await searchCore(db, String(args.q ?? ""), String(args.code ?? ""), 30, true);
    const totalRow = await db.prepare("SELECT COUNT(*) AS n FROM materials").first<{ n: number }>();
    const total = totalRow?.n ?? 0;
    const result: Record<string, unknown> = { total, count: rows!.length, materials: rows };
    if (rows!.length < total) {
      result.note = `共 ${total} 种物料，已列出最新 ${rows!.length} 条，可用 q 关键词缩小范围`;
    }
    return { result };
  }
  if (name === "stock_records") {
    // 出入库流水：空关键词=最近记录；有关键词按物料名称/编码模糊过滤
    const kw = String(args.keyword ?? "").trim();
    let limit = parseIntSafe(args.limit ?? 10, 1) ?? 10;
    limit = Math.min(limit, 30);
    const sql = kw
      ? `SELECT r.created_at, r.type, r.quantity, r.operator, r.remark,
                m.name AS material_name, m.code AS material_code
         FROM stock_records r JOIN materials m ON m.id = r.material_id
         WHERE m.name LIKE ? OR m.code LIKE ?
         ORDER BY r.id DESC LIMIT ?`
      : `SELECT r.created_at, r.type, r.quantity, r.operator, r.remark,
                m.name AS material_name, m.code AS material_code
         FROM stock_records r JOIN materials m ON m.id = r.material_id
         ORDER BY r.id DESC LIMIT ?`;
    const rows = kw
      ? await q(db, sql, `%${kw}%`, `%${kw}%`, limit)
      : await q(db, sql, limit);
    return { result: { count: rows.length, records: rows } };
  }
  if (name === "list_warehouses") {
    const rows = await q(db, "SELECT id, name, parent_id, level FROM warehouses ORDER BY level, id");
    return { result: { count: rows.length, warehouses: rows } };
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
  if (name === "query_purchase") {
    const proj = String(args.project ?? "").trim() || undefined;
    const rows = await purchaseListCore(db, proj);
    const slim = rows.map((r: any) => ({
      id: r.id, project: r.project, name: r.name, code: r.code, model: r.model,
      need_qty: r.need_qty, purchased: r.purchased, unit: r.unit, status: r.status,
    }));
    return { result: { count: slim.length, items: slim } };
  }
  // ---------- 写操作：拦截转"待确认卡片" ----------
  if (name === "stock_in" || name === "stock_out") {
    const qty = parseIntSafe(args.quantity, 1);
    if (qty === null) return { result: { error: "quantity 必须是大于0的整数" } };
    // 目标解析（material_id 或 code 二选一；多候选返回列表让 AI 请用户确认）
    const target = await resolveStockTarget(db, args);
    if (target.err) return { result: target.err };
    const base: Record<string, any> = {
      id: randHex8(), type: name,
      // 卡片参数落库为解析后的 material_id（执行时不再依赖 code 二次解析）
      params: { ...args, material_id: target.mid, code: undefined },
      status: "pending", result: null, executed_at: null,
      title: `${name === "stock_in" ? "入库" : "出库"}：${target.mname}（#${target.mid}）×${qty}`,
    };
    delete base.params.code;
    return { result: { ok: true, action_id: base.id, note: ACTION_NOTE }, action: base };
  }
  const built = buildAction(name, args);
  if (built.err) return { result: { error: built.err } };
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
  let done = false;
  for (let round = 0; round < MAX_LLM_ROUNDS; round++) {
    let r: { content: string; toolCalls: any[] };
    try {
      r = await llmChat(cfg, msgs, AI_TOOLS);
    } catch (e) {
      // LLM 失败也落一条 assistant 消息，保证历史连续可追溯
      const emsg = e instanceof Error ? e.message : String(e);
      await db.prepare("INSERT INTO chat_messages(user_id, role, content) VALUES (?,?,?)")
        .bind(u.id, "assistant", `[AI调用失败] ${emsg}`).run();
      return c.json({ error: `AI 调用失败：${emsg}` }, 502);
    }
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
      const { result, action } = await runTool(db, tname, args);
      if (action) actions.push(action);
      msgs.push(toolMsg(tc.id, result));
    }
  }
  if (!done) {
    // 8 轮用尽模型仍在要工具：追加一次不带 tools 的请求逼出文本总结
    try {
      const r = await llmChat(cfg, msgs, null);
      content = r.content;
    } catch {
      content = content || "（本轮工具调用已达上限，未生成总结。请换种问法或缩小范围后重试。）";
    }
  }

  // 4) 落库 assistant 消息 + 待确认卡片
  const actionsJson = actions.length ? JSON.stringify(actions) : null;
  const ins = await db.prepare(
    "INSERT INTO chat_messages(user_id, role, content, actions_json) VALUES (?,?,?,?)")
    .bind(u.id, "assistant", content || "", actionsJson).run();
  return c.json({ message_id: ins.meta.last_row_id, content: content || "", actions });
});

// ---------------- 历史加载 / 清空 ----------------

chat.get("/api/chat/history", requireLogin, async (c) => {
  const u = c.get("user")!;
  const before = parseIntSafe(c.req.query("before_id") ?? "0", 1) ?? 0;
  let limit = parseIntSafe(c.req.query("limit") ?? "30", 1) ?? 30;
  limit = Math.min(limit, 100);
  const sql = before
    ? "SELECT id, role, content, actions_json, created_at FROM chat_messages WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?"
    : "SELECT id, role, content, actions_json, created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT ?";
  const params: unknown[] = before ? [u.id, before, limit] : [u.id, limit];
  const rows = await q<any>(c.env.DB, sql, ...params);
  const out = rows.reverse().map((r) => {   // 反转成 旧→新，前端按序渲染
    let acts: any[] = [];
    if (r.actions_json) {
      try { acts = JSON.parse(r.actions_json); } catch { acts = []; }
    }
    return { id: r.id, role: r.role, content: r.content,
             created_at: r.created_at, actions: acts };
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
 *  出错带 error；keep_pending=true（库存不足）表示保持待确认可重试 */
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
    return { material_id: mid, quantity: r.qty };
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
  return { error: `未知操作类型 ${t}` };
}

chat.post("/api/chat/execute", requireLogin, async (c) => {
  const d = await getJson(c);
  const msgId = parseIntSafe(d.message_id, 1);
  const actionId = String(d.action_id ?? "").trim();
  if (msgId === null || !actionId) return c.json({ error: "message_id / action_id 不能为空" }, 400);
  const u = c.get("user")!;
  const rows = await q<{ user_id: number; actions_json: string | null }>(
    c.env.DB, "SELECT user_id, actions_json FROM chat_messages WHERE id=?", msgId);
  if (!rows.length || rows[0].user_id !== u.id) {
    return c.json({ error: "消息不存在或不属于当前用户" }, 403);
  }
  let acts: any[] = [];
  try { acts = rows[0].actions_json ? JSON.parse(rows[0].actions_json) : []; } catch { acts = []; }
  const act = acts.find((a) => a?.id === actionId);
  if (!act) return c.json({ error: "操作卡片不存在" }, 404);
  if (act.status !== "pending") {
    return c.json({ error: `该卡片已处理（状态：${act.status}），不可重复操作` }, 409);
  }
  const now = nowStrLocal();

  if (d.cancel) {
    act.status = "cancelled";
    act.executed_at = now;
  } else {
    const result = await executeAction(c.env.DB, act, u.username);
    act.result = result;
    if (result?.error && result?.keep_pending) {
      // 库存不足：保持 pending（补货后可再点确认），不推进状态机
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
