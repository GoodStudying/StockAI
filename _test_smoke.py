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

# ---------- 清理 ----------
for suffix in ("", "-journal", "-wal", "-shm"):
    p = TEST_DB + suffix
    if os.path.exists(p):
        os.remove(p)

print(f"\n结果：通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("失败项：", failed)
    raise SystemExit(1)
