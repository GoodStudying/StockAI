# -*- coding: utf-8 -*-
"""
立创商城物料信息抓取（双通道 + 双站合并）

背景：立创官方搜索接口有反爬/域名限制，服务端不可用。
抓取策略：
  1) 英文站详情页 https://lcsc.com/product-detail/Cxxxxx.html（__NEXT_DATA__ JSON）
     -> 拿 productId / 价格 / 库存 / 手册PDF直链
  2) 中文站详情页 https://item.szlcsc.com/<productId>.html（productRecord JSON）
     -> 拿中文名称 / 封装 / 品牌 / 分类 / 描述（中文信息优先）
  两站合并输出。
关键字搜索：Bing 搜 site:item.szlcsc.com 反查 productId -> 转 C 编号。
"""
import os
import re
import json
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


def _parse_cn_record(html_text: str):
    """解析中文站页面的 productRecord JSON"""
    idx = html_text.find('"productRecord"')
    if idx < 0:
        return {}
    seg = html_text[idx:idx + 300000]
    try:
        obj, _ = json.JSONDecoder().raw_decode(seg[seg.find("{"):])
        return obj
    except Exception:
        return {}


def fetch_detail(code: str):
    """
    按 C 编号抓取：英文站(productId/价格/库存/手册) + 中文站(中文名称/封装/品牌/分类)。
    返回 list[dict]（0或1条完整详情）。
    """
    code = code.strip().upper()
    if not re.fullmatch(r"C\d+", code):
        return []

    # ---- 英文站 ----
    en = {}
    try:
        r = requests.get(DETAIL_URL.format(code=code), headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if m:
                en = json.loads(m.group(1))
    except Exception:
        en = {}

    def eg(k):
        v = _find_key(en, k) if en else None
        return v.strip() if isinstance(v, str) else (v if v is not None else "")

    price = ""
    prices = _find_key(en, "productPriceList") or _find_key(en, "ladderPrice") or []
    if isinstance(prices, list) and prices:
        try:
            first = prices[0]
            for pk in ("currencyPrice", "usdPrice", "price"):
                if isinstance(first, dict) and first.get(pk) not in (None, ""):
                    price = str(first[pk])
                    break
        except Exception:
            pass

    datasheet_url = eg("pdfUrl") or eg("dataManualUrl")

    item = {
        "code": code,
        "product_id": eg("productId"),
        "model": eg("productModel"),
        "name": "",            # 中文站优先填充
        "brand": "",
        "package": "",
        "category": "",
        "description": eg("productIntroEn"),
        "stock": str(eg("stockNumber") or eg("stockJs") or eg("stockJhsz") or ""),
        "price": price,
        "image": eg("productPicturePath") or eg("breviaryImageUrl") or eg("imageUrl"),
        "datasheet_url": datasheet_url,
        "url": DETAIL_URL.format(code=code),
    }

    # ---- 中文站（中文名称/封装/品牌/分类优先） ----
    pid = item["product_id"]
    if pid:
        try:
            r2 = requests.get(f"https://item.szlcsc.com/{pid}.html", headers=HEADERS, timeout=TIMEOUT)
            cn = _parse_cn_record(r2.text)
            if cn:
                item["name"] = str(cn.get("productName") or "")
                item["model"] = cn.get("productModel") or item["model"]
                item["brand"] = str(cn.get("productGradePlateName") or "")
                item["package"] = str(cn.get("encapsulationModel") or "")
                item["category"] = str(cn.get("productType") or "")
                item["description"] = str(cn.get("remark") or item["description"])
                item["stock"] = str(cn.get("stockNumber") or item["stock"])
                if not item["image"]:
                    item["image"] = str(cn.get("breviaryImageUrl") or "")
                item["url"] = f"https://item.szlcsc.com/{pid}.html"
                # 中文站手册（英文站缺失时兜底）
                if not item["datasheet_url"]:
                    for ft in (cn.get("fileTypeVOList") or []):
                        if "pdf_property" in str(ft.get("fileType", "")):
                            det = (ft.get("detailVOList") or [{}])[0]
                            fu = det.get("fileUrl", "")
                            if fu:
                                item["datasheet_url"] = ("https://szlcsc.com" + fu) if fu.startswith("/") else fu
                                break
        except Exception:
            pass

    # 名称兜底：型号
    if not item["name"]:
        item["name"] = item["model"]
    return [item] if (item["model"] or item["name"]) else []


def _cn_item_code(pid: str):
    """中文详情页 productId -> C编号（读内嵌 productRecord.productCode）"""
    try:
        r = requests.get(f"https://item.szlcsc.com/{pid}.html", headers=HEADERS, timeout=TIMEOUT)
        cn = _parse_cn_record(r.text)
        return cn.get("productCode") or None
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
                        "image": "", "datasheet_url": "", "product_id": pid,
                        "url": DETAIL_URL.format(code=code)})
    return results


def download_datasheet(datasheet_url: str, save_dir: str):
    """
    下载立创手册 PDF 到本地（失败自动重试一次）。
    返回 (保存路径, 文件名) 或 (None, None)。
    """
    if not datasheet_url:
        return None, None

    def _try_download():
        r = requests.get(datasheet_url, headers=HEADERS, timeout=60, stream=True)
        if r.status_code != 200:
            return None
        os.makedirs(save_dir, exist_ok=True)
        # 文件名：优先 URL 里的哈希，否则用默认名
        m = re.search(r'/([0-9a-fA-F]{16,})\.pdf', datasheet_url)
        stem = m.group(1) if m else "datasheet"
        fname = f"{stem}.pdf"
        path = os.path.join(save_dir, fname)
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return path, fname

    for _ in range(2):  # 重试一次
        try:
            result = _try_download()
            if result:
                return result
        except Exception:
            continue
    return None, None


def search_lcsc(keyword: str):
    """统一入口：C编号直查详情；否则关键字搜索反查"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    if re.fullmatch(r"C\d+", kw, re.I):
        return fetch_detail(kw)
    return search_by_keyword_via_bing(kw)
