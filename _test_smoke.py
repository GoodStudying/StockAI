# -*- coding: utf-8 -*-
"""
冒烟测试：验证本轮全部 bug 修复与 /api/v1 开放接口。
使用临时数据库 data/_test_smoke.db（跑完自动删除），不影响真实数据。
运行：python _test_smoke.py
"""
import os

# 必须在 import config/app 之前设置：临时库 + 测试 Token
os.environ["SQLITE_PATH"] = "data/_test_smoke.db"
os.environ["API_TOKEN"] = "test-token-abc123"

import json

TEST_DB = os.path.join("data", "_test_smoke.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import app as app_module  # noqa: E402  (import 即完成建表/默认管理员/迁移)

# 建料自动抓取依赖外网（立创），既有测试段全程用假实现避免真实网络请求；
# 原函数留到 [14] 自动抓取专项段（配合假 lcsc 数据）验证
_orig_auto_fetch = app_module._auto_fetch_material
app_module._auto_fetch_material = lambda mid: None

c = app_module.app.test_client()
H = {"Authorization": "Bearer test-token-abc123"}
passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ---------- 1. 登录 / 密码升级 ----------
print("[1] 登录与会话")
r = c.post("/api/login", json={"username": "admin", "password": "错误密码"})
check("错误密码返回401", r.status_code == 401)
r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
check("默认密码可登录", r.status_code == 200 and r.get_json()["user"]["role"] == "admin")
import db as dbm
h = dbm.query("SELECT password_hash FROM users WHERE username='admin'")[0]["password_hash"]
check("登录后哈希已升级为PBKDF2(加盐)", h.startswith("pbkdf2:sha256"), h[:20])
r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
check("升级后仍能用原密码登录", r.status_code == 200)

# ---------- 2. 仓库：删除防御 / 层级 ----------
print("[2] 仓库管理")
r = c.post("/api/warehouses", json={"name": "一级库A"})
w1 = r.get_json()["id"]
r = c.post("/api/warehouses", json={"name": "二级库B", "parent_id": w1})
w2 = r.get_json()["id"]
r = c.post("/api/warehouses", json={"name": "三级库C", "parent_id": w2})
w3 = r.get_json()["id"]
r = c.post("/api/warehouses", json={"name": "四级库D", "parent_id": w3})
w4 = r.get_json()["id"]
r = c.post("/api/warehouses", json={"name": "五级库", "parent_id": w4})
check("第5级被拒(上限4级)", r.status_code == 400)

# ---------- 3. 物料：非法数量 / 建料 ----------
print("[3] 物料管理")
r = c.post("/api/materials", json={"name": "测试电容", "quantity": "abc"})
check("数量非法返回400(不再500)", r.status_code == 400, str(r.status_code))
r = c.post("/api/materials", json={"name": "贴片电容100nF", "code": "C123456",
                                   "model": "CL10B104KB8NNNC", "quantity": 50, "warehouse_id": w2})
m1 = r.get_json()["id"]
r = c.post("/api/materials", json={"name": "主控芯片", "code": "C77343",
                                   "model": "STM32F103C8T6", "quantity": 3, "warehouse_id": w3})
m2 = r.get_json()["id"]
# 重复物料（同编码同型号，供 duplicates / bom ambiguous 用）
r = c.post("/api/materials", json={"name": "贴片电容100nF", "code": "C123456",
                                   "model": "CL10B104KB8NNNC", "quantity": 10})
m3 = r.get_json()["id"]
r = c.put(f"/api/materials/{m1}", json={"name": "", "quantity": 5})
check("改物料名为空被拒", r.status_code == 400)
r = c.put(f"/api/materials/{m1}", json={"name": "贴片电容100nF", "code": "C123456",
                                        "model": "CL10B104KB8NNNC", "quantity": 50, "warehouse_id": w2})
check("正常修改物料", r.status_code == 200)
r = c.get("/api/materials", query_string={"warehouse_id": "xyz"})
check("warehouse_id非法返回400", r.status_code == 400)

# ---------- 4. 删除非空仓库 ----------
print("[4] 删除非空仓库防御")
r = c.delete(f"/api/warehouses/{w1}")
check("删含物料的仓库被拒(不再500)", r.status_code == 400 and "物料" in r.get_json()["error"],
      r.get_data(as_text=True))
r = c.delete(f"/api/warehouses/{w4}")
check("删除空的四级库成功", r.status_code == 200)

# ---------- 5. 出入库：原子性 / 超卖防御 / 非法输入 ----------
print("[5] 出入库")
r = c.post("/api/stock/in", json={"material_id": m1, "quantity": 100, "remark": "采购"})
check("入库成功", r.status_code == 200 and r.get_json()["quantity"] == 150)
r = c.post("/api/stock/out", json={"material_id": m1, "quantity": 30})
check("出库成功", r.status_code == 200 and r.get_json()["quantity"] == 120)
r = c.post("/api/stock/out", json={"material_id": m1, "quantity": 99999})
check("超额出库被拒", r.status_code == 400 and "库存不足" in r.get_json()["error"])
r = c.post("/api/stock/out", json={"material_id": m1, "quantity": "十"})
check("出库数量非法返回400", r.status_code == 400)
r = c.post("/api/stock/out", json={"material_id": m1, "quantity": 120})
check("恰好清零出库成功", r.status_code == 200 and r.get_json()["quantity"] == 0)
q = dbm.query("SELECT quantity FROM materials WHERE id=?", (m1,))[0]["quantity"]
check("库存与流水一致(=0,未负数)", q == 0)
rc = dbm.query("SELECT COUNT(*) AS n FROM stock_records WHERE material_id=?", (m1,))[0]["n"]
check("流水条数=3(1进2出,被拒的不留痕)", rc == 3, str(rc))

# ---------- 6. /api/v1 开放接口 ----------
print("[6] /api/v1 开放接口(Token认证)")
r = c.get("/api/v1/materials?q=电容")
check("无Token返回401", r.status_code == 401)
r = c.get("/api/v1/materials?q=电容", headers=H)
j = r.get_json()
check("Token搜索物料", r.status_code == 200 and j["count"] == 2)
r = c.get("/api/v1/materials?code=C77343", headers=H)
check("code精确查询", r.status_code == 200 and r.get_json()["count"] == 1)
r = c.get("/api/v1/materials", headers=H)
check("缺参数返回400", r.status_code == 400)
r = c.post("/api/v1/stock/out", headers=H,
           json={"material_id": m2, "quantity": 2, "remark": "AI出库"})
j = r.get_json()
check("v1出库成功", r.status_code == 200 and j["quantity"] == 1)
row = dbm.query("SELECT operator FROM stock_records WHERE material_id=? ORDER BY id DESC LIMIT 1",
                (m2,))[0]
check("操作人留痕为 api:xxxx***", row["operator"].startswith("api:test"), row["operator"])
r = c.post("/api/v1/stock/out", headers=H, json={"material_id": m2, "quantity": 99})
check("v1超额出库被拒", r.status_code == 400)
r = c.get("/api/v1/stock/records", headers=H)
check("v1流水查询", r.status_code == 200 and len(r.get_json()["records"]) >= 4)

# BOM 核对（m1/m3 同型号同编码 → ambiguous；m1 库存 0 → C123456 行 shortage）
r = c.post("/api/v1/bom/check", headers=H, json={"items": [
    {"code": "C77343", "quantity": 1},            # ok（剩1）
    {"code": "C123456", "quantity": 5},           # shortage（0库存,缺5）
    {"name": "不存在的物料", "quantity": 1},       # not_found
    {"model": "CL10B104KB8NNNC", "quantity": 1},  # ambiguous（两条同型号）
    {"quantity": 3},                               # not_found（无任何标识字段）
]})
j = r.get_json()
s = j["summary"]
check("BOM汇总正确", r.status_code == 200 and s["total"] == 5 and s["shortage"] == 1
      and s["ambiguous"] >= 1, json.dumps(s, ensure_ascii=False))
sts = [x["status"] for x in j["results"]]
check("BOM逐行状态含ok/shortage/not_found/ambiguous",
      "ok" in sts and "shortage" in sts and "not_found" in sts and "ambiguous" in sts, str(sts))
r = c.post("/api/v1/bom/check", headers=H, json={"items": "不是数组"})
check("BOM入参非法返回400", r.status_code == 400)

# 重复核对
r = c.get("/api/v1/duplicates", headers=H)
j = r.get_json()
check("重复核对找到编码重复组", r.status_code == 200 and j["total_groups"] >= 1
      and any(g["field"] == "code" and g["count"] == 2 for g in j["duplicates"]),
      json.dumps(j, ensure_ascii=False)[:120])

# 未配置 Token 的场景已在上面 401 覆盖；503 分支用空 Token 集合模拟
app_module.API_TOKENS.clear()
r = c.get("/api/v1/materials?q=x", headers=H)
check("服务端未配置Token时返回503", r.status_code == 503)
app_module.API_TOKENS.add("test-token-abc123")

# ---------- 7. 账户管理边界 ----------
print("[7] 账户管理边界")
r = c.post("/api/users", json={"username": "u1", "password": "p1", "role": "user"})
u1 = r.get_json()["id"]
r = c.put(f"/api/users/{u1}", json={"role": "admin"})
check("提升他人为admin允许", r.status_code == 200)
r = c.put(f"/api/users/{u1}", json={"role": "user"})
check("降级他人(还剩2个admin)允许", r.status_code == 200)
admin_id = dbm.query("SELECT id FROM users WHERE username='admin'")[0]["id"]
r = c.put(f"/api/users/{admin_id}", json={"role": "user"})
check("不能把自己降级", r.status_code == 400)
r = c.put(f"/api/users/{u1}", json={"role": "admin"})
r = c.delete(f"/api/users/{admin_id}")
check("不能删除当前登录账户", r.status_code == 400)
r = c.put("/api/users/99999", json={"password": "x"})
check("改不存在用户返回404", r.status_code == 404)
# 唯一admin(u1现在降回user了) — 删除admin前先删u1场景：admin是唯一管理员
r = c.delete(f"/api/users/{u1}")
r = c.delete(f"/api/users/{admin_id}")
check("不能删除最后一名管理员", r.status_code == 400 or r.status_code == 400)

# ---------- 8. 备份(在线快照) ----------
print("[8] 数据库备份")
c.post("/api/login", json={"username": "admin", "password": "admin123"})
r = c.get("/api/db/backup")
check("备份下载成功且非空", r.status_code == 200 and len(r.data) > 1000)
with open(TEST_DB + ".bak2", "wb") as f:  # 校验备份文件可打开（一致性快照）
    f.write(r.data)
import sqlite3
try:
    con = sqlite3.connect(TEST_DB + ".bak2")
    n = con.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
    con.close()
    check("备份文件是有效SQLite库", n >= 3, str(n))
except Exception as e:
    check("备份文件是有效SQLite库", False, str(e))
os.remove(TEST_DB + ".bak2")

# ---------- 9. 登录限流 ----------
print("[9] 登录限流")
app_module._LOGIN_ATTEMPTS.clear()
last = None
for i in range(12):
    last = c.post("/api/login", json={"username": "admin", "password": "错的"})
check("连续失败触发429限流", last.status_code == 429, str(last.status_code))

# ---------- 10. AI 接口配置 ----------
print("[10] AI 接口配置")
r = c.get("/api/ai/status")
check("未配置时 status.configured=false", r.get_json()["configured"] is False)
r = c.post("/api/ai/config", json={"base_url": "", "model": "x"})
check("空 base_url 被拒", r.status_code == 400)
r = c.post("/api/ai/config", json={"base_url": "https://api.fake/v1", "model": "",
                                   "api_key": "sk-test123456789"})
check("空 model 被拒", r.status_code == 400)
r = c.post("/api/ai/config", json={"base_url": "https://api.fake/v1", "model": "fake-model",
                                   "api_key": "sk-test123456789"})
check("保存配置成功", r.status_code == 200)
d = c.get("/api/ai/config").get_json()
check("配置回显打码(不泄露key)", d["has_key"] is True and "sk-test123456789" not in json.dumps(d)
      and d["api_key_masked"].startswith("sk-"), d.get("api_key_masked", ""))
# 空 key 保存 = 保留原值
c.post("/api/ai/config", json={"base_url": "https://api.fake/v1", "model": "fake-model", "api_key": ""})
d = c.get("/api/ai/config").get_json()
check("api_key留空=保留原值", d["has_key"] is True)
r = c.get("/api/ai/status")
check("配置后 status.configured=true", r.get_json()["configured"] is True)

# ---------- 11. 采购清单：合并 / 批次 / 状态派生 ----------
print("[11] 采购清单")
r = c.post("/api/purchase/items", json={"project": "  ", "items": [{"name": "x", "need_qty": 1}]})
check("空项目名被拒", r.status_code == 400)
r = c.post("/api/purchase/items", json={"project": "测试项目A", "items": [
    {"name": "电容A", "code": "C111", "need_qty": 100},
    {"name": "电阻B", "need_qty": 50, "unit": "只"}]})
check("批量导入 created=2", r.get_json() == {"created": 2, "merged": 0}, str(r.get_json()))
r = c.post("/api/purchase/items", json={"project": "测试项目A", "items": [
    {"code": "C111", "name": "电容A", "need_qty": 30}]})
check("同code二次导入走合并", r.get_json() == {"created": 0, "merged": 1}, str(r.get_json()))
rows = c.get("/api/purchase/items", query_string={"project": "测试项目A"}).get_json()
cap = next(x for x in rows if x["code"] == "C111")
res = next(x for x in rows if x["name"] == "电阻B")
check("合并后 need_qty=130", cap["need_qty"] == 130, str(cap["need_qty"]))
check("未采购状态 open", cap["status"] == "open" and cap["purchased"] == 0)
check("单位字段生效", res["unit"] == "只")
# 批次：80 → partial；再 50 → done；删一笔 → 回退
r = c.post(f"/api/purchase/items/{cap['id']}/batches", json={"qty": 80, "remark": "第一车"})
b1 = r.get_json()["id"]
check("登记批次返回201", r.status_code == 201)
rows = c.get("/api/purchase/items", query_string={"project": "测试项目A", "status": "partial"}).get_json()
check("80/130 为 partial 且状态过滤生效", len(rows) == 1 and rows[0]["purchased"] == 80)
r = c.post(f"/api/purchase/items/{cap['id']}/batches", json={"qty": 50})
b2 = r.get_json()["id"]
rows = c.get("/api/purchase/items", query_string={"project": "测试项目A"}).get_json()
cap2 = next(x for x in rows if x["code"] == "C111")
check("130/130 为 done", cap2["status"] == "done")
c.delete(f"/api/purchase/batches/{b2}")
rows = c.get("/api/purchase/items", query_string={"project": "测试项目A"}).get_json()
cap3 = next(x for x in rows if x["code"] == "C111")
check("删批次后状态回退 partial 且已购回退", cap3["status"] == "partial" and cap3["purchased"] == 80)
projs = c.get("/api/purchase/projects").get_json()
pa = next(p for p in projs if p["project"] == "测试项目A")
check("项目聚合正确", pa["item_count"] == 2 and pa["done_count"] == 0
      and pa["need_total"] == 180 and pa["purchased_total"] == 80, str(pa))
r = c.delete("/api/purchase/items/999999")
check("删除不存在条目404", r.status_code == 404)

# ---------- 12. 网页 BOM 核对（session 认证） ----------
print("[12] 网页 BOM 核对")
r = c.post("/api/bom/check", json={"items": [{"code": "C123456", "quantity": 999}]})
d = r.get_json()
row0 = d["results"][0]
check("网页核对 shortage 行", d["summary"]["shortage"] == 1
      and row0["shortage"] == 999 - row0["available"], str(d["summary"]))
r = c.post("/api/bom/check", json={"items": []})
check("空 items 返回400", r.status_code == 400)

# ---------- 13. AI 对话：tool loop / 卡片 / execute 状态机 ----------
print("[13] AI 对话")


def scripted_llm(seq):
    """monkeypatch _llm_chat：按脚本序列返回 (文本, 工具调用列表)。
    seq 元素 = "纯文本" 或 ("中间文本", [("工具名", {参数}), ...])"""
    it = iter(seq)
    def fake(cfg, messages, tools=None):
        item = next(it)
        content, calls = item if isinstance(item, tuple) else (item, [])
        tcs = [{"id": f"call_{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
               for i, (n, a) in enumerate(calls)]
        return content, tcs
    return fake


real_llm = app_module._llm_chat

# 场景a：只读工具真执行（search_materials）
app_module._llm_chat = scripted_llm([
    ("先搜索", [("search_materials", {"q": "电容"})]),
    "找到 3 条物料，请告诉我数量。",
])
r = c.post("/api/chat", json={"message": "有没有电容"})
d = r.get_json()
check("只读对话正常返回文本", r.status_code == 200 and d["content"] == "找到 3 条物料，请告诉我数量。")
check("只读对话无卡片", d["actions"] == [])
hist = c.get("/api/chat/history").get_json()
check("对话双方消息落库", hist["messages"][-2]["role"] == "user"
      and hist["messages"][-1]["role"] == "assistant")

# 场景b：写工具拦截转卡片 → 确认执行 → 重复 409
m1_qty_before = dbm.query("SELECT quantity FROM materials WHERE code='C123456' ORDER BY id")[0]["quantity"]
app_module._llm_chat = scripted_llm([
    ("", [("stock_in", {"material_id": m1, "quantity": 5, "remark": "AI测试入库"})]),
    "已生成入库卡片，请确认。",
])
r = c.post("/api/chat", json={"message": "入库5个电容"})
d = r.get_json()
check("写工具生成1张pending卡片", len(d["actions"]) == 1
      and d["actions"][0]["status"] == "pending", str(d["actions"]))
aid = d["actions"][0]["id"]
msg_id = d["message_id"]
m1_qty_after_gen = dbm.query("SELECT quantity FROM materials WHERE id=?", (m1,))[0]["quantity"]
check("确认前库存未变(拦截生效)", m1_qty_after_gen == m1_qty_before)
r = c.post("/api/chat/execute", json={"message_id": msg_id, "action_id": aid})
check("确认执行成功", r.status_code == 200 and r.get_json()["action"]["status"] == "executed")
m1_qty_after_exec = dbm.query("SELECT quantity FROM materials WHERE id=?", (m1,))[0]["quantity"]
check("执行后库存+5", m1_qty_after_exec == m1_qty_before + 5,
      f"{m1_qty_before}->{m1_qty_after_exec}")
op = dbm.query("SELECT operator FROM stock_records ORDER BY id DESC")[0]["operator"]
check("流水留痕 ai:admin", op == "ai:admin", op)
r = c.post("/api/chat/execute", json={"message_id": msg_id, "action_id": aid})
check("重复确认返回409(幂等)", r.status_code == 409)

# 场景c：出库库存不足 → 卡片保持 pending 可重试
app_module._llm_chat = scripted_llm([
    ("", [("stock_out", {"material_id": m1, "quantity": 999999})]),
    "已生成出库卡片。",
])
r = c.post("/api/chat", json={"message": "出库一大堆"})
d = r.get_json()
r = c.post("/api/chat/execute", json={"message_id": d["message_id"], "action_id": d["actions"][0]["id"]})
check("库存不足执行返回400", r.status_code == 400)
hist = c.get("/api/chat/history?limit=5").get_json()
last_acts = [m for m in hist["messages"] if m["actions"]][-1]["actions"]
check("库存不足卡片保持pending(可补货后重试)", last_acts[0]["status"] == "pending", last_acts[0]["status"])

# 场景d：非本人卡片 403（先清登录限流：[9]段按IP记录，同IP的u2会被连坐429）
c.post("/api/users", json={"username": "u2", "password": "u2pass123", "role": "user"})
app_module._LOGIN_ATTEMPTS.clear()
c2 = app_module.app.test_client()
c2.post("/api/login", json={"username": "u2", "password": "u2pass123"})
r = c2.post("/api/chat/execute", json={"message_id": msg_id, "action_id": aid})
check("非本人确认卡片返回403", r.status_code == 403)
r = c2.get("/api/ai/config")
check("非admin读AI配置返回403", r.status_code == 403)

# 场景e：轮次用尽 → 追加无 tools 请求逼出文本
app_module._llm_chat = scripted_llm(
    [("继续", [("search_materials", {"q": f"轮{i}"})]) for i in range(8)] + ["已到上限的总结文本。"])
r = c.post("/api/chat", json={"message": "疯狂搜索"})
d = r.get_json()
check("8轮用尽后逼出总结文本", r.status_code == 200 and d["content"] == "已到上限的总结文本。"
      and d["actions"] == [], d.get("content", ""))

# 场景f：非法 JSON 参数 → 模型收到 error 不 500
app_module._llm_chat = scripted_llm([
    ("", [("search_materials", "不是对象")]) if False else None,  # 占位不用
])
# 直接构造非法 arguments：脚本的 arguments 是合法 JSON 字符串，这里测工具级参数错误
app_module._llm_chat = scripted_llm([
    ("", [("search_materials", {})]),          # q/code 全空 → 工具返回 error 给模型
    "参数不足。",
])
r = c.post("/api/chat", json={"message": "随便搜"})
check("工具参数错误不炸(模型自纠)", r.status_code == 200 and r.get_json()["content"] == "参数不足。")

# 场景g：AI 未配置 → 400
c.post("/api/ai/config", json={"base_url": "https://api.fake/v1", "model": "m", "api_key": "!"})
r = c.post("/api/chat", json={"message": "你好"})
check("AI未配置返回400", r.status_code == 400)
app_module._llm_chat = real_llm   # 恢复，避免影响后续

# ---------- 14. 物料自动抓取（假立创数据） ----------
print("[14] 物料自动抓取")
import lcsc as lcsc_mod
FAKE_DETAIL = {
    "code": "C999888", "model": "FAKE-MODEL-X", "name": "假物料中文名",
    "brand": "假品牌", "package": "0603", "category": "贴片电容",
    "description": "fake desc", "image": "https://fake.cdn/img.png",
    "datasheet_url": "https://fake.cdn/abc123def456.pdf", "url": "https://item.szlcsc.com/1.html",
}
lcsc_mod.fetch_detail = lambda code: [FAKE_DETAIL] if code.upper() == "C999888" else []
lcsc_mod.fetch_image_bytes = lambda url: b"\x89PNG\r\n\x1a\n-fake-image"
lcsc_mod.fetch_datasheet_bytes = lambda url: (b"%FAKE-PDF", "fake-manual.pdf")
app_module._auto_fetch_material = _orig_auto_fetch    # 恢复真实实现（lcsc 已换假数据）
r = c.post("/api/materials", json={"name": "自动抓取测试", "code": "C999888", "quantity": 0})
mid_fake = r.get_json()["id"]
row = dbm.query("SELECT * FROM materials WHERE id=?", (mid_fake,))[0]
check("空字段自动补全(型号/品牌/封装)", row["model"] == "FAKE-MODEL-X" and row["brand"] == "假品牌"
      and row["package"] == "0603", str(row["model"]))
check("已有名称不覆盖", row["name"] == "自动抓取测试")
check("商城链接自动填充", row["lcsc_url"].startswith("https://item.szlcsc.com"))
check("图片已下载入库(BLOB)", row["image_blob"] is not None)
docs = dbm.query("SELECT * FROM material_documents WHERE material_id=?", (mid_fake,))
check("手册已自动登记", len(docs) == 1 and docs[0]["doc_type"] == "datasheet")
# 二次编辑：全字段已填 → 快速路径跳过网络（fetch_detail 若被调用会 raise 验证）
called = {"n": 0}
def guard_detail(code):
    called["n"] += 1
    return [FAKE_DETAIL]
lcsc_mod.fetch_detail = guard_detail
r = c.put(f"/api/materials/{mid_fake}", json={"name": "自动抓取测试", "code": "C999888",
                                              "model": "FAKE-MODEL-X", "brand": "假品牌",
                                              "package": "0603", "category": "贴片电容",
                                              "quantity": 7, "lcsc_url": row["lcsc_url"]})
check("编辑已补全物料成功", r.status_code == 200)
check("全字段已填时跳过重复抓取", called["n"] == 0, f"fetch_detail called {called['n']}")

# ---------- 清理 ----------
for suffix in ("", "-journal", "-wal", "-shm"):
    p = TEST_DB + suffix
    if os.path.exists(p):
        os.remove(p)

print(f"\n结果：通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("失败项：", failed)
    raise SystemExit(1)
