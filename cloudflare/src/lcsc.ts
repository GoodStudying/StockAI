/**
 * 立创商城物料信息抓取（Workers 版，与本地 lcsc.py 同思路）
 *
 * 云端策略（应"抓一次存链接"的需求设计）：
 * - 英文站 lcsc.com（__NEXT_DATA__ JSON）拿 productId / 价格 / 库存 / 图片 / 手册PDF直链
 *   —— 海外站，对 Cloudflare 出口 IP 友好，成功率高
 * - 中文站 item.szlcsc.com（productRecord JSON）补中文名称/封装/品牌/分类 —— 尽力而为，
 *   被风控时回退英文站数据（名称用型号兜底）
 * - 文件不下载：图片/手册只存外链 URL 进 D1，前端展示时 302 跳转到立创 CDN，
 *   不占 Worker 存储、无需 R2，链接永久有效（除非立创改版）
 * - 关键字搜索：Bing 搜 site:item.szlcsc.com 反查 productId -> 转 C 编号（链路较长，尽力而为）
 */

/** 抓取请求头（模拟普通浏览器，避免裸 UA 被一眼识别） */
const HEADERS: Record<string, string> = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    + "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
};

/** 英文站详情页 URL（C 编号） */
const detailUrl = (code: string) => `https://lcsc.com/product-detail/${code}.html`;

/** 单条物料信息（与本地版字段一一对应，前端零改动） */
export interface LcscItem {
  code: string; product_id: string; model: string; name: string; brand: string;
  package: string; category: string; description: string; stock: string; price: string;
  image: string; datasheet_url: string; url: string;
}

/** 抓取页面文本；失败（网络错/非2xx）返回 null */
async function getHtml(url: string): Promise<string | null> {
  try {
    const r = await fetch(url, { headers: HEADERS, redirect: "follow" });
    if (!r.ok) return null;
    return await r.text();
  } catch {
    return null;
  }
}

/** 递归查找 JSON 对象中的 key（限深度 10，对应本地 _find_key） */
function findKey(obj: unknown, key: string, depth = 0): unknown {
  if (depth > 10 || obj == null) return undefined;
  if (typeof obj === "object") {
    if (!Array.isArray(obj) && key in (obj as Record<string, unknown>)) return (obj as Record<string, unknown>)[key];
    for (const v of Object.values(obj as Record<string, unknown>)) {
      const f = findKey(v, key, depth + 1);
      if (f !== undefined && f !== null) return f;
    }
  }
  return undefined;
}

/** 从字符串开头提取第一个完整 JSON 对象（括号配对，忽略字符串字面量内的括号） */
function extractJsonObject(s: string): unknown | null {
  let depth = 0, inStr = false, esc = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === "\\") esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === "{") depth++;
    else if (ch === "}") { depth--; if (depth === 0) { try { return JSON.parse(s.slice(0, i + 1)); } catch { return null; } } }
  }
  return null;
}

/** 解析中文站页面的 productRecord JSON（对应本地 _parse_cn_record） */
function parseCnRecord(html: string): Record<string, any> {
  const idx = html.indexOf('"productRecord"');
  if (idx < 0) return {};
  const seg = html.slice(idx, idx + 300000);
  const start = seg.indexOf("{");
  if (start < 0) return {};
  const obj = extractJsonObject(seg.slice(start));
  return (obj && typeof obj === "object" && !Array.isArray(obj)) ? obj as Record<string, any> : {};
}

/**
 * 按 C 编号抓详情：英文站(productId/价格/库存/手册/图) + 中文站(中文名/封装/品牌，尽力而为)。
 * 返回 0 或 1 条完整详情（与本地版 fetch_detail 返回结构一致）。
 */
export async function fetchDetail(codeRaw: string): Promise<LcscItem[]> {
  const code = (codeRaw ?? "").trim().toUpperCase();
  if (!/^C\d+$/.test(code)) return [];

  // ---- 英文站 ----
  const html = await getHtml(detailUrl(code));
  let en: any = {};
  if (html) {
    const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
    if (m) { try { en = JSON.parse(m[1]); } catch { en = {}; } }
  }
  const eg = (k: string): string => {
    const v = findKey(en, k);
    if (typeof v === "string") return v.trim();
    return v != null ? String(v) : "";
  };

  // 阶梯价取第一档（对应本地 productPriceList/ladderPrice 逻辑）
  let price = "";
  const prices = (findKey(en, "productPriceList") ?? findKey(en, "ladderPrice") ?? []) as unknown[];
  if (Array.isArray(prices) && prices.length && typeof prices[0] === "object" && prices[0]) {
    for (const pk of ["currencyPrice", "usdPrice", "price"] as const) {
      const v = (prices[0] as Record<string, unknown>)[pk];
      if (v != null && v !== "") { price = String(v); break; }
    }
  }

  const item: LcscItem = {
    code,
    product_id: eg("productId"),
    model: eg("productModel"),
    name: "",            // 中文站优先填充
    brand: "",
    package: "",
    category: "",
    description: eg("productIntroEn"),
    stock: eg("stockNumber") || eg("stockJs") || eg("stockJhsz"),
    price,
    image: eg("productPicturePath") || eg("breviaryImageUrl") || eg("imageUrl"),
    datasheet_url: eg("pdfUrl") || eg("dataManualUrl"),
    url: detailUrl(code),
  };

  // ---- 中文站（中文名称/封装/品牌/分类优先；被风控时静默回退英文站数据） ----
  if (item.product_id) {
    const cnHtml = await getHtml(`https://item.szlcsc.com/${item.product_id}.html`);
    if (cnHtml) {
      const cn = parseCnRecord(cnHtml);
      if (cn && Object.keys(cn).length) {
        item.name = String(cn.productName ?? "");
        item.model = cn.productModel || item.model;
        item.brand = String(cn.productGradePlateName ?? "");
        item.package = String(cn.encapsulationModel ?? "");
        item.category = String(cn.productType ?? "");
        item.description = String(cn.remark || item.description);
        item.stock = String(cn.stockNumber || item.stock);
        if (!item.image) item.image = String(cn.breviaryImageUrl ?? "");
        item.url = `https://item.szlcsc.com/${item.product_id}.html`;
        // 中文站手册（英文站缺失时兜底）
        if (!item.datasheet_url) {
          for (const ft of (cn.fileTypeVOList ?? []) as Record<string, any>[]) {
            if (String(ft.fileType ?? "").includes("pdf_property")) {
              const fu = ft.detailVOList?.[0]?.fileUrl ?? "";
              if (fu) { item.datasheet_url = fu.startsWith("/") ? "https://szlcsc.com" + fu : fu; break; }
            }
          }
        }
      }
    }
  }

  // 名称兜底：型号
  if (!item.name) item.name = item.model;
  return item.model || item.name ? [item] : [];
}

/** 中文详情页 productId -> C 编号（读内嵌 productRecord.productCode） */
async function cnItemCode(pid: string): Promise<string | null> {
  const html = await getHtml(`https://item.szlcsc.com/${pid}.html`);
  if (!html) return null;
  const cn = parseCnRecord(html);
  return cn.productCode ?? null;
}

/**
 * 型号/名称关键字 -> Bing 搜 site:item.szlcsc.com 反查 productId -> 转 C 编号。
 * 返回轻量列表（前端选中后再抓详情）。子请求 ≤ 1(Bing) + limit(详情页) 个，远低于 Workers 50 上限。
 */
export async function searchByKeyword(keyword: string, limit = 6): Promise<LcscItem[]> {
  const pids: string[] = [];
  for (const host of ["cn.bing.com", "www.bing.com"]) {
    const html = await getHtml(`https://${host}/search?q=${encodeURIComponent(keyword + " site:item.szlcsc.com")}`);
    if (!html) continue;
    for (const m of html.matchAll(/item\.szlcsc\.com\/(\d{3,7})\.html/g)) {
      if (!pids.includes(m[1])) pids.push(m[1]);
      if (pids.length >= limit) break;
    }
    if (pids.length) break;
  }
  const blank = (code: string, pid: string): LcscItem => ({
    code, product_id: pid, model: "", name: "", brand: "", package: "", category: "",
    description: "", stock: "", price: "", image: "", datasheet_url: "", url: detailUrl(code),
  });
  const results: LcscItem[] = [];
  for (const pid of pids) {
    const code = await cnItemCode(pid);
    if (code) results.push(blank(code, pid));
  }
  return results;
}

/** 统一入口：C 编号直查详情；否则关键字搜索反查（对应本地 search_lcsc） */
export async function searchLcsc(keyword: string): Promise<LcscItem[]> {
  const kw = (keyword ?? "").trim();
  if (!kw) return [];
  if (/^C\d+$/i.test(kw)) return fetchDetail(kw);
  return searchByKeyword(kw);
}
