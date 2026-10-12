# -*- coding: utf-8 -*-
"""
立创商城物料信息抓取（双通道）

背景：立创官方搜索接口有反爬/域名限制，服务端不可用。
可用通道：
  1) C 编号 -> 英文站详情页 https://lcsc.com/product-detail/Cxxxxx.html
     页面内嵌 __NEXT_DATA__ JSON，字段齐全（型号/品牌/封装/分类/描述/价格/库存/图片/手册）。
  2) 型号关键字 -> DuckDuckGo HTML 搜索 site:lcsc.com 反查 C 编号 -> 再走通道1。
失败时返回错误信息，不影响手工新建。
"""
import re
import json
import html as htmllib
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
TIMEOUT = 12
DETAIL_URL = "https://lcsc.com/product-detail/{code}.html"


def _find_key(obj, key, depth=0):
    """递归查找 JSON 中的 key"""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            f = _find_key(v, key, depth + 1)
            if f is not None:
                return f
    elif isinstance(obj, list):
        for v in obj:
            f = _find_key(v, key, depth + 1)
            if f is not None:
                return f
    return None


def fetch_detail(code: str):
    """
    按 C 编号抓取英文站详情页，解析 __NEXT_DATA__。
    返回 list[dict]（0或1条完整详情）。
    """
    code = code.strip().upper()
    if not re.fullmatch(r"C\d+", code):
        return []
    try:
        r = requests.get(DETAIL_URL.format(code=code), headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
    except Exception:
        return []

    def g(k):
        v = _find_key(data, k)
        return v.strip() if isinstance(v, str) else (v if v is not None else "")

    # 价格梯度（梯价列表）
    price = ""
    prices = _find_key(data, "productPriceList") or _find_key(data, "ladderPrice") or []
    if isinstance(prices, list) and prices:
        try:
            first = prices[0]
            for pk in ("currencyPrice", "usdPrice", "price"):
                if isinstance(first, dict) and first.get(pk) not in (None, ""):
                    price = str(first[pk])
                    break
        except Exception:
            pass

    # 手册PDF直链（datasheet 字段或 fileTypeVOList）
    datasheet_url = g("pdfUrl") or g("dataManualUrl")
    if not datasheet_url:
        ftl = _find_key(data, "fileTypeVOList")
        if isinstance(ftl, list):
            for ft in ftl:
                if isinstance(ft, dict) and "pdf_property" in str(ft.get("fileType", "")):
                    det = (ft.get("detailVOList") or [{}])[0]
                    fu = det.get("fileUrl", "")
                    if fu:
                        datasheet_url = "https://szlcsc.com" + fu if fu.startswith("/") else fu
                        break

    stock = g("stockNumber") or g("stockJs") or g("stockJhsz") or ""

    item = {
        "code": code,
        "model": g("productModel"),
        "name": g("productIntroEn") or g("productName"),
        "brand": g("brandNameEn") or g("productGradePlateName") or g("brandName"),
        "package": g("encapsulationStandard") or g("encapsulationModel"),
        "category": g("catalogName") or g("productType"),
        "description": g("productIntroEn") or g("remark"),
        "stock": str(stock),
        "price": price,
        "image": g("productPicturePath") or g("breviaryImageUrl") or g("imageUrl"),
        "datasheet_url": datasheet_url,
        "url": DETAIL_URL.format(code=code),
    }
    # 中文详情页补充（品牌/分类更准，仅当英文字段缺失时）
    if not item["brand"] or not item["category"]:
        try:
            pid = g("productId")
            if pid:
                r2 = requests.get(f"https://item.szlcsc.com/{pid}.html", headers=HEADERS, timeout=TIMEOUT)
                idx = r2.text.find('"productRecord"')
                if idx > 0:
                    seg = r2.text[idx:idx + 300000]
                    obj, _ = json.JSONDecoder().raw_decode(seg[seg.find("{"):])
                    item["brand"] = item["brand"] or str(obj.get("productGradePlateName") or "")
                    item["category"] = item["category"] or str(obj.get("productType") or "")
                    item["package"] = item["package"] or str(obj.get("encapsulationModel") or "")
                    item["name"] = item["name"] or str(obj.get("productName") or "")
                    item["url"] = f"https://item.szlcsc.com/{pid}.html"
        except Exception:
            pass
    return [item] if (item["model"] or item["name"]) else []


def _cn_item_code(pid: str):
    """中文详情页 productId -> C编号（读内嵌 productRecord.productCode）"""
    try:
        r = requests.get(f"https://item.szlcsc.com/{pid}.html", headers=HEADERS, timeout=TIMEOUT)
        idx = r.text.find('"productRecord"')
        if idx < 0:
            return None
        seg = r.text[idx:idx + 300000]
        obj, _ = json.JSONDecoder().raw_decode(seg[seg.find("{"):])
        return obj.get("productCode") or None
    except Exception:
        return None


def search_by_keyword_via_bing(keyword: str, limit=6):
    """
    型号/名称关键字 -> Bing 搜索 site:item.szlcsc.com 反查 productId -> 转 C 编号。
    返回 list[dict]（轻量列表），前端选中后再抓详情。
    """
    pids = []
    for host in ("cn.bing.com", "www.bing.com"):
        try:
            r = requests.get(f"https://{host}/search",
                             params={"q": f"{keyword} site:item.szlcsc.com"},
                             headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            found = re.findall(r'item\.szlcsc\.com/(\d{3,7})\.html', r.text)
            for p in found:
                if p not in pids:
                    pids.append(p)
                if len(pids) >= limit:
                    break
            if pids:
                break
        except Exception:
            continue

    results = []
    for pid in pids:
        code = _cn_item_code(pid)
        if not code:
            continue
        results.append({"code": code, "model": "", "name": "", "brand": "", "package": "",
                        "category": "", "description": "", "stock": "", "price": "",
                        "image": "", "datasheet_url": "",
                        "url": DETAIL_URL.format(code=code)})
    return results


def search_lcsc(keyword: str):
    """统一入口：C编号直查详情；否则关键字搜索反查"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    if re.fullmatch(r"C\d+", kw, re.I):
        return fetch_detail(kw)
    return search_by_keyword_via_bing(kw)
