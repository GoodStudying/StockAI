# -*- coding: utf-8 -*-
"""
个人ERP系统 - Flask 主应用
功能：多级仓库(1~4级)、物料新建/查询、立创商城抓取、手册上传
数据存储：本地 SQLite（可切换 PostgreSQL，见 db.py / .env）
"""
import os
import re
from flask import Flask, request, jsonify, send_from_directory, send_file
from config import UPLOAD_DIR, MAX_WAREHOUSE_LEVEL, BASE_DIR
import db
import lcsc

app = Flask(__name__, static_folder="static", static_url_path="")

db.init_schema()


# ---------------- 首页 ----------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ---------------- 仓库管理 ----------------
@app.route("/api/warehouses", methods=["GET"])
def list_warehouses():
    """返回全部仓库（前端组树）"""
    return jsonify(db.query("SELECT * FROM warehouses ORDER BY level, id"))


@app.route("/api/warehouses", methods=["POST"])
def create_warehouse():
    """新建仓库节点，1~4级"""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    parent_id = data.get("parent_id")
    if not name:
        return jsonify({"error": "库名不能为空"}), 400

    if parent_id:
        rows = db.query("SELECT level FROM warehouses WHERE id=?", (parent_id,))
        if not rows:
            return jsonify({"error": "父级库不存在"}), 404
        level = rows[0]["level"] + 1
    else:
        level = 1

    if level > MAX_WAREHOUSE_LEVEL:
        return jsonify({"error": f"最多支持 {MAX_WAREHOUSE_LEVEL} 级库"}), 400

    last_id, _ = db.execute_write(
        "INSERT INTO warehouses(name, parent_id, level) VALUES (?,?,?)",
        (name, parent_id, level))
    return jsonify({"id": last_id, "name": name, "parent_id": parent_id, "level": level}), 201


@app.route("/api/warehouses/<int:wid>", methods=["PUT"])
def rename_warehouse(wid):
    """修改库名"""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "库名不能为空"}), 400
    _, n = db.execute_write("UPDATE warehouses SET name=? WHERE id=?", (name, wid))
    if n == 0:
        return jsonify({"error": "库不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/warehouses/<int:wid>", methods=["DELETE"])
def delete_warehouse(wid):
    _, n = db.execute_write("DELETE FROM warehouses WHERE id=?", (wid,))
    if n == 0:
        return jsonify({"error": "库不存在"}), 404
    return jsonify({"ok": True})


# ---------------- 物料管理 ----------------
@app.route("/api/materials", methods=["GET"])
def list_materials():
    """查询物料：支持关键字(名称/编码/型号)、仓库ID"""
    kw = (request.args.get("q") or "").strip()
    wid = request.args.get("warehouse_id")
    sql = """SELECT m.*, w.name AS warehouse_name FROM materials m
             LEFT JOIN warehouses w ON m.warehouse_id = w.id"""
    conds, params = [], []
    if kw:
        conds.append("(m.name LIKE ? OR m.code LIKE ? OR m.model LIKE ?)")
        like = f"%{kw}%"
        params += [like, like, like]
    if wid:
        conds.append("m.warehouse_id = ?")
        params.append(wid)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY m.id DESC LIMIT 500"
    rows = db.query(sql, tuple(params))
    # 附带手册列表
    for r in rows:
        r["documents"] = db.query(
            "SELECT id, doc_type, file_name FROM material_documents WHERE material_id=?",
            (r["id"],))
    return jsonify(rows)


@app.route("/api/materials", methods=["POST"])
def create_material():
    """新建物料"""
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "物料名称不能为空"}), 400
    last_id, _ = db.execute_write(
        """INSERT INTO materials(name, code, model, category, package, brand,
                                 description, quantity, unit, warehouse_id, lcsc_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (name, d.get("code", ""), d.get("model", ""), d.get("category", ""),
         d.get("package", ""), d.get("brand", ""), d.get("description", ""),
         int(d.get("quantity") or 0), d.get("unit", "个"),
         d.get("warehouse_id") or None, d.get("lcsc_url", "")))
    return jsonify({"id": last_id}), 201


@app.route("/api/materials/<int:mid>", methods=["PUT"])
def update_material(mid):
    d = request.get_json(force=True)
    _, n = db.execute_write(
        """UPDATE materials SET name=?, code=?, model=?, category=?, package=?,
           brand=?, description=?, quantity=?, unit=?, warehouse_id=?, lcsc_url=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("name", ""), d.get("code", ""), d.get("model", ""), d.get("category", ""),
         d.get("package", ""), d.get("brand", ""), d.get("description", ""),
         int(d.get("quantity") or 0), d.get("unit", "个"),
         d.get("warehouse_id") or None, d.get("lcsc_url", ""), mid))
    if n == 0:
        return jsonify({"error": "物料不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/materials/<int:mid>", methods=["DELETE"])
def delete_material(mid):
    _, n = db.execute_write("DELETE FROM materials WHERE id=?", (mid,))
    if n == 0:
        return jsonify({"error": "物料不存在"}), 404
    return jsonify({"ok": True})


# ---------------- 立创商城 ----------------
@app.route("/api/lcsc/search", methods=["GET"])
def lcsc_search():
    kw = (request.args.get("q") or "").strip()
    if not kw:
        return jsonify({"error": "关键字不能为空"}), 400
    return jsonify(lcsc.search_lcsc(kw))


# ---------------- 手册上传 / 下载 ----------------
ALLOWED_DOC_TYPES = {"datasheet", "user_manual", "other"}


@app.route("/api/materials/<int:mid>/documents", methods=["POST"])
def upload_document(mid):
    """上传物料手册：multipart 表单 file + doc_type"""
    if not db.query("SELECT id FROM materials WHERE id=?", (mid,)):
        return jsonify({"error": "物料不存在"}), 404
    f = request.files.get("file")
    doc_type = request.form.get("doc_type", "datasheet")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    if doc_type not in ALLOWED_DOC_TYPES:
        return jsonify({"error": "doc_type 不合法"}), 400

    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", f.filename)
    mat_dir = os.path.join(UPLOAD_DIR, str(mid))
    os.makedirs(mat_dir, exist_ok=True)
    save_path = os.path.join(mat_dir, safe_name)
    f.save(save_path)

    last_id, _ = db.execute_write(
        "INSERT INTO material_documents(material_id, doc_type, file_path, file_name) VALUES (?,?,?,?)",
        (mid, doc_type, save_path, safe_name))
    return jsonify({"id": last_id, "file_name": safe_name}), 201


@app.route("/api/documents/<int:did>", methods=["GET"])
def download_document(did):
    rows = db.query("SELECT file_path, file_name FROM material_documents WHERE id=?", (did,))
    if not rows or not os.path.exists(rows[0]["file_path"]):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(rows[0]["file_path"], as_attachment=True,
                     download_name=rows[0]["file_name"])


@app.route("/api/documents/<int:did>", methods=["DELETE"])
def delete_document(did):
    rows = db.query("SELECT file_path FROM material_documents WHERE id=?", (did,))
    if not rows:
        return jsonify({"error": "记录不存在"}), 404
    db.execute_write("DELETE FROM material_documents WHERE id=?", (did,))
    if os.path.exists(rows[0]["file_path"]):
        os.remove(rows[0]["file_path"])
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
