# -*- coding: utf-8 -*-
"""临时验证：/api/bom/analyze 端到端（临时库，跑完即删）"""
import os
import io
os.environ["SQLITE_PATH"] = "data/_test_bom_analyze.db"
TEST_DB = os.path.join("data", "_test_bom_analyze.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import app as app_module
app_module._auto_fetch_material = lambda mid: None

c = app_module.app.test_client()
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  ✓ " + name)
    else: fail += 1; print("  ✗ " + name + (f"  [{detail}]" if detail else ""))

# 登录
r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
check("登录", r.status_code == 200)

# 造数据：一个充足物料、一个缺料物料
c.post("/api/materials", json={"name": "100nF电容0603", "code": "C621395",
                               "model": "CL10B104KB8NNNC", "quantity": 50})
c.post("/api/materials", json={"name": "10k电阻0603", "code": "C25804",
                               "model": "RC0603FR-0710KL", "quantity": 3})

# 1) CSV 分析：立创编号列 + 数量列 + 位号列（无表头关键词的数量列走形态识别）
csv_data = (
    'Comment,Quantity,Designator,LCSC\n'
    '100nF,5,"C1,C2,C3",C621395\n'
    '10k,10,R1,C25804\n'
    '1uF,7,C9,C999999\n'
).encode("utf-8-sig")
r = c.post("/api/bom/analyze",
           content_type="multipart/form-data",
           data={"file": (io.BytesIO(csv_data), "bom.csv", "text/csv"), "use_ai": "0"})
check("CSV 分析 200", r.status_code == 200, r.get_data(as_text=True)[:200])
d = r.get_json()
s = d["summary"]
check("汇总 total=3", s["total"] == 3, str(s))
check("充足=1", s["enough"] == 1, str(s))
check("缺口=1", s["shortage"] == 1, str(s))
check("未建档=1", s["unmatched"] == 1, str(s))
by_code = {it["code"]: it for it in d["items"]}
check("C621395 匹配且库存足够", by_code["C621395"]["matched"] and by_code["C621395"]["enough"])
check("C25804 缺口=7", by_code["C25804"].get("shortage") == 7, str(by_code["C25804"]))
check("C999999 未建档", not by_code["C999999"]["matched"])
check("位号聚合 C621395 refs", "C1,C2,C3" in by_code["C621395"]["refs"], by_code["C621395"]["refs"])
check("need_buy=2 条", len(d["need_buy"]) == 2, str(d["need_buy"]))
check("返回识别列 columns", d["columns"].get("lcsc") == 3 and d["columns"].get("qty") == 1,
      str(d["columns"]))

# 2) 需求聚合：同料多行合并
csv2 = ("LCSC,Qty\nC621395,2\nC621395,3\n").encode("utf-8")
r = c.post("/api/bom/analyze", content_type="multipart/form-data",
           data={"file": (io.BytesIO(csv2), "b2.csv", "text/csv"), "use_ai": "0"})
d = r.get_json()
check("聚合后 total=1", d["summary"]["total"] == 1, str(d["summary"]))
check("聚合 need=5", d["items"][0]["need"] == 5, str(d["items"][0]))

# 3) 错误分支
r = c.post("/api/bom/analyze", content_type="multipart/form-data",
           data={"file": (io.BytesIO(b"not a zip"), "b.xlsx", "application/octet-stream")})
check("坏 xlsx 返回400", r.status_code == 400)
r = c.post("/api/bom/analyze", content_type="multipart/form-data",
           data={"file": (io.BytesIO(b"hello"), "b.txt", "text/plain")})
check("不支持扩展名 400", r.status_code == 400)
csv_bad = ("A,B\n1,2\n").encode("utf-8")
r = c.post("/api/bom/analyze", content_type="multipart/form-data",
           data={"file": (io.BytesIO(csv_bad), "b3.csv", "text/csv"), "use_ai": "0"})
check("无法识别列 400", r.status_code == 400, r.get_data(as_text=True)[:100])
r = c.post("/api/bom/analyze", content_type="multipart/form-data", data={"use_ai": "0"})
check("缺文件 400", r.status_code == 400)

# 4) xlsx 真解析：手工构造最小 xlsx
import io, zipfile
def make_xlsx(rows):
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    shared = []
    for row in rows:
        for v in row:
            if isinstance(v, str) and v not in shared:
                shared.append(v)
    z.writestr("xl/sharedStrings.xml",
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f'<si><t>{s}</t></si>' for s in shared) + "</sst>")
    body = ""
    for ri, row in enumerate(rows, 1):
        cells = ""
        for ci, v in enumerate(row):
            col = chr(65 + ci)
            if isinstance(v, str):
                cells += f'<c r="{col}{ri}" t="s"><v>{shared.index(v)}</v></c>'
            else:
                cells += f'<c r="{col}{ri}"><v>{v}</v></c>'
        body += f'<row r="{ri}">{cells}</row>'
    z.writestr("xl/worksheets/sheet1.xml",
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{body}</sheetData></worksheet>')
    z.close()
    return buf.getvalue()

xlsx = make_xlsx([
    ["器件型号", "数量", "位号"],
    ["CL10B104KB8NNNC", 60, "C1"],
    ["RC0603FR-0710KL", 1, "R1"],
])
r = c.post("/api/bom/analyze", content_type="multipart/form-data",
           data={"file": (io.BytesIO(xlsx), "bom.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                 "use_ai": "0"})
check("xlsx 分析 200", r.status_code == 200, r.get_data(as_text=True)[:200])
if r.status_code == 200:
    d = r.get_json()
    check("xlsx 型号匹配足够", d["summary"]["enough"] == 1, str(d["summary"]))
    check("xlsx 型号匹配缺口", d["summary"]["shortage"] == 1, str(d["summary"]))
    check("xlsx how=型号", d["items"][0]["how"] == "型号", str(d["items"][0]))

os.remove(TEST_DB)
print(f"\n结果：通过 {ok} 项，失败 {fail} 项")
