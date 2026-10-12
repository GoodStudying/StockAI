# -*- coding: utf-8 -*-
"""
立创商城物料信息抓取
通过立创商城搜索接口按编号/型号查询物料信息。
接口失败时返回错误提示，不影响手工新建。
"""
import re
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
}
TIMEOUT = 10


def search_lcsc(keyword: str):
    """
    按关键字（立创编号 Cxxxxx 或型号）搜索立创商城物料。
    返回 list[dict]: [{code, model, name, brand, package, category, description, url}]
    """
    results = []
    # 尝试立创开放搜索接口
    try:
        url = "https://so.szlcsc.com/global.html"
        # 直接调内部搜索 API
        api = "https://wwwapi.szlcsc.com/search/product"
        resp = requests.post(api, json={
            "keyword": keyword, "currentPage": 1, "pageSize": 10
        }, headers={**HEADERS, "Content-Type": "application/json"}, timeout=TIMEOUT)
        if resp.ok:
            data = resp.json()
            items = (data.get("result") or {}).get("productSearchResultVO", {}).get("productList") or []
            for it in items:
                results.append({
                    "code": it.get("productCode", ""),           # C 编号
                    "model": it.get("productModel", ""),
                    "name": it.get("productIntroEn", "") or it.get("productModel", ""),
                    "brand": it.get("brandNameEn", "") or it.get("brandName", ""),
                    "package": it.get("encapsulation", ""),
                    "category": it.get("catalogName", ""),
                    "description": it.get("productIntro", ""),
                    "url": f"https://item.szlcsc.com/{it.get('productId')}.html" if it.get("productId") else "",
                })
            if results:
                return results
    except Exception:
        pass

    # 兜底：若是 C 编号，直接构造详情链接
    if re.fullmatch(r"C\d+", keyword.strip(), re.I):
        results.append({
            "code": keyword.strip().upper(),
            "model": "", "name": "", "brand": "",
            "package": "", "category": "",
            "description": "立创接口未返回详情，请打开链接核对",
            "url": f"https://so.szlcsc.com/global.html?k={keyword.strip()}",
        })
    return results
