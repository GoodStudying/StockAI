# -*- coding: utf-8 -*-
"""
个人ERP系统 - Flask 主应用
页面1：物料查询 / 入库 / 出库
页面2：仓库管理（建库 / 删库 / 改名，1~4级）
页面3：数据库管理 / 账户管理
数据存储：本地 SQLite（可切换 PostgreSQL，见 db.py / .env）
"""
import os
import re
import hashlib
import shutil
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, send_file, session
from config import UPLOAD_DIR, MAX_WAREHOUSE_LEVEL, BASE_DIR, SQLITE_PATH, DB_TYPE
import db
import lcsc

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.urandom(24)

db.init_schema()
db.ensure_default_admin()


# ---------------- 认证 ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "未登录"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = session.get("user")
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u.get("role") != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(force=True)
    pw = hashlib.sha256((d.get("password") or "").encode()).hexdigest()
    rows = db.query("SELECT id, username, role FROM users WHERE username=? AND password_hash=?",
                    ((d.get("username") or "").strip(), pw))
    if not rows:
        return jsonify({"error": "用户名或密码错误"}), 401
    session["user"] = rows[0]
    return jsonify({"ok": True, "user": rows[0]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    u = session.get("user")
    return jsonify({"user": u})


# ---------------- 页面路由 ----------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ---------------- 仓库管理 ----------------
@app.route("/api/warehouses", methods=["GET"])
@login_required
def list_warehouses():
    return jsonify(db.query("SELECT * FROM warehouses ORDER BY level, id"))


@app.route("/api/warehouses", methods=["POST"])
@login_required
def create_warehouse():
    """新建仓库节点，1~4级"""
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    parent_id = d.get("parent_id")
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
@login_required
def rename_warehouse(wid):
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "库名不能为空"}), 400
    _, n = db.execute_write("UPDATE warehouses SET name=? WHERE id=?", (name, wid))
    if n == 0:
        return jsonify({"error": "库不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/warehouses/<int:wid>", methods=["DELETE"])
@login_required
def delete_warehouse(wid):
    _, n = db.execute_write("DELETE FROM warehouses WHERE id=?", (wid,))
    if n == 0:
        return jsonify({"error": "库不存在"}), 404
    return jsonify({"ok": True})


# ---------------- 物料管理 ----------------
@app.route("/api/materials", methods=["GET"])
@login_required
def list_materials():
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
    for r in rows:
        r["documents"] = db.query(
            "SELECT id, doc_type, file_name FROM material_documents WHERE material_id=?",
            (r["id"],))
    return jsonify(rows)


@app.route("/api/materials", methods=["POST"])
@login_required
def create_material():
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
@login_required
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
@login_required
def delete_material(mid):
    _, n = db.execute_write("DELETE FROM materials WHERE id=?", (mid,))
    if n == 0:
        return jsonify({"error": "物料不存在"}), 404
    return jsonify({"ok": True})


# ---------------- 入库 / 出库 ----------------
@app.route("/api/stock/<op>", methods=["POST"])
@login_required
def stock_inout(op):
    """op: in=入库 out=出库。body: {material_id, quantity, remark}"""
    if op not in ("in", "out"):
        return jsonify({"error": "操作类型不合法"}), 400
    d = request.get_json(force=True)
    mid = d.get("material_id")
    qty = int(d.get("quantity") or 0)
    if qty <= 0:
        return jsonify({"error": "数量必须大于0"}), 400
    rows = db.query("SELECT quantity FROM materials WHERE id=?", (mid,))
    if not rows:
        return jsonify({"error": "物料不存在"}), 404
    cur_qty = rows[0]["quantity"]
    if op == "out" and qty > cur_qty:
        return jsonify({"error": f"库存不足，当前库存 {cur_qty}"}), 400

    new_qty = cur_qty + qty if op == "in" else cur_qty - qty
    operator = session["user"]["username"]
    db.execute_write("UPDATE materials SET quantity=?, updated_at=datetime('now','localtime') WHERE id=?",
                     (new_qty, mid))
    db.execute_write(
        "INSERT INTO stock_records(material_id, type, quantity, operator, remark) VALUES (?,?,?,?,?)",
        (mid, op, qty, operator, d.get("remark", "")))
    return jsonify({"ok": True, "quantity": new_qty})


@app.route("/api/stock/records", methods=["GET"])
@login_required
def stock_records():
    """库存流水，支持按物料过滤"""
    mid = request.args.get("material_id")
    sql = """SELECT r.*, m.name AS material_name, m.code AS material_code
             FROM stock_records r JOIN materials m ON r.material_id = m.id"""
    params = ()
    if mid:
        sql += " WHERE r.material_id = ?"
        params = (mid,)
    sql += " ORDER BY r.id DESC LIMIT 500"
    return jsonify(db.query(sql, params))


# ---------------- 立创商城 ----------------
@app.route("/api/lcsc/search", methods=["GET"])
@login_required
def lcsc_search():
    kw = (request.args.get("q") or "").strip()
    if not kw:
        return jsonify({"error": "关键字不能为空"}), 400
    return jsonify(lcsc.search_lcsc(kw))


# ---------------- 手册上传 / 下载 ----------------
ALLOWED_DOC_TYPES = {"datasheet", "user_manual", "other"}


@app.route("/api/materials/<int:mid>/documents", methods=["POST"])
@login_required
def upload_document(mid):
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
@login_required
def download_document(did):
    rows = db.query("SELECT file_path, file_name FROM material_documents WHERE id=?", (did,))
    if not rows or not os.path.exists(rows[0]["file_path"]):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(rows[0]["file_path"], as_attachment=True,
                     download_name=rows[0]["file_name"])


@app.route("/api/documents/<int:did>", methods=["DELETE"])
@login_required
def delete_document(did):
    rows = db.query("SELECT file_path FROM material_documents WHERE id=?", (did,))
    if not rows:
        return jsonify({"error": "记录不存在"}), 404
    db.execute_write("DELETE FROM material_documents WHERE id=?", (did,))
    if os.path.exists(rows[0]["file_path"]):
        os.remove(rows[0]["file_path"])
    return jsonify({"ok": True})


# ---------------- 账户管理（管理员） ----------------
@app.route("/api/users", methods=["GET"])
@admin_required
def list_users():
    return jsonify(db.query("SELECT id, username, role, created_at FROM users"))


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    role = d.get("role", "user")
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if role not in ("admin", "user"):
        return jsonify({"error": "角色不合法"}), 400
    if db.query("SELECT id FROM users WHERE username=?", (username,)):
        return jsonify({"error": "用户名已存在"}), 400
    pw = hashlib.sha256(password.encode()).hexdigest()
    last_id, _ = db.execute_write(
        "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)", (username, pw, role))
    return jsonify({"id": last_id}), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def update_user(uid):
    """重置密码 / 修改角色"""
    d = request.get_json(force=True)
    if d.get("password"):
        pw = hashlib.sha256(d["password"].encode()).hexdigest()
        db.execute_write("UPDATE users SET password_hash=? WHERE id=?", (pw, uid))
    if d.get("role") in ("admin", "user"):
        db.execute_write("UPDATE users SET role=? WHERE id=?", (d["role"], uid))
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def delete_user(uid):
    if session["user"]["id"] == uid:
        return jsonify({"error": "不能删除当前登录账户"}), 400
    db.execute_write("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"ok": True})


# ---------------- 数据库管理（管理员） ----------------
@app.route("/api/db/info", methods=["GET"])
@admin_required
def db_info():
    info = {"type": DB_TYPE, "path": SQLITE_PATH if DB_TYPE == "sqlite" else "(postgres)"}
    if DB_TYPE == "sqlite" and os.path.exists(SQLITE_PATH):
        info["size_kb"] = round(os.path.getsize(SQLITE_PATH) / 1024, 1)
    for t in ("warehouses", "materials", "material_documents", "stock_records", "users"):
        info[t] = db.query(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"]
    return jsonify(info)


@app.route("/api/db/backup", methods=["GET"])
@admin_required
def db_backup():
    """备份 SQLite 数据库文件并下载"""
    if DB_TYPE != "sqlite":
        return jsonify({"error": "仅 SQLite 支持文件备份"}), 400
    bak = SQLITE_PATH + ".bak"
    shutil.copyfile(SQLITE_PATH, bak)
    return send_file(bak, as_attachment=True, download_name="erp_backup.db")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
