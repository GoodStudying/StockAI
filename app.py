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
db.run_migrations()


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
def _descendant_ids(wid):
    """递归收集仓库及全部子孙库 id 列表"""
    ids = [wid]
    children = db.query("SELECT id FROM warehouses WHERE parent_id=?", (wid,))
    for c in children:
        ids += _descendant_ids(c["id"])
    return ids


@app.route("/api/warehouses", methods=["GET"])
@login_required
def list_warehouses():
    """返回全部仓库，附带本库及子孙库物料总数"""
    rows = db.query("SELECT * FROM warehouses ORDER BY level, id")
    for w in rows:
        ids = _descendant_ids(w["id"])
        ph = ",".join("?" * len(ids))
        w["material_count"] = db.query(
            f"SELECT COUNT(*) AS c FROM materials WHERE warehouse_id IN ({ph})", tuple(ids))[0]["c"]
    return jsonify(rows)


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
    include_children = request.args.get("include_children", "1") != "0"  # 默认含子库
    sql = """SELECT m.*, w.name AS warehouse_name FROM materials m
             LEFT JOIN warehouses w ON m.warehouse_id = w.id"""
    conds, params = [], []
    if kw:
        conds.append("(m.name LIKE ? OR m.code LIKE ? OR m.model LIKE ?)")
        like = f"%{kw}%"
        params += [like, like, like]
    if wid:
        if include_children:
            # 仓库层级包含关系：查该库及其全部子孙库的物料
            ph = ",".join("?" * len(ids := _descendant_ids(int(wid))))
            conds.append(f"m.warehouse_id IN ({ph})")
            params += ids
        else:
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


DOC_TYPE_LABELS = {"datasheet": "数据手册", "user_manual": "用户手册", "other": "其他文档"}


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

    # 立创建库时自动下载物料图片（失败不影响建物料）
    image_url = (d.get("image_url") or "").strip()
    if image_url and (d.get("code") or "").strip():
        code = d["code"].strip()
        path = lcsc.download_image(image_url, code, os.path.join(UPLOAD_DIR, str(last_id)))
        if path:
            db.execute_write("UPDATE materials SET image_path=? WHERE id=?", (path, last_id))
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


@app.route("/api/lcsc/detail", methods=["GET"])
@login_required
def lcsc_detail():
    """按 C 编号抓取完整详情"""
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "编号不能为空"}), 400
    result = lcsc.fetch_detail(code)
    if not result:
        return jsonify({"error": "未获取到详情（编号可能无效或网络受限）"}), 404
    return jsonify(result[0])


@app.route("/api/materials/<int:mid>/fetch_datasheet", methods=["POST"])
@login_required
def fetch_datasheet(mid):
    """按物料编码自动从立创下载手册PDF并登记（无需手动下载上传）"""
    rows = db.query("SELECT code FROM materials WHERE id=?", (mid,))
    if not rows:
        return jsonify({"error": "物料不存在"}), 404
    code = (rows[0]["code"] or "").strip()
    if not code:
        return jsonify({"error": "该物料没有立创编码（C编号），无法自动下载"}), 400
    if not re.fullmatch(r"C\d+", code, re.I):
        return jsonify({"error": f"编码「{code}」不是立创C编号（形如 C621395），无法自动下载"}), 400

    try:
        detail = lcsc.fetch_detail(code)
    except Exception as e:
        return jsonify({"error": f"访问立创失败：{type(e).__name__}，请稍后重试"}), 502
    if not detail:
        return jsonify({"error": "未获取到立创详情（编号可能无效或网络受限），请稍后重试"}), 404
    datasheet_url = detail[0].get("datasheet_url")
    if not datasheet_url:
        return jsonify({"error": "立创未提供该物料的手册PDF"}), 404

    mat_dir = os.path.join(UPLOAD_DIR, str(mid))
    path, fname = lcsc.download_datasheet(datasheet_url, mat_dir)
    if not path:
        return jsonify({"error": "手册PDF下载失败（网络受限或超时），请稍后重试"}), 502

    # 避免重复登记同一手册
    exists = db.query("SELECT id FROM material_documents WHERE material_id=? AND file_name=?",
                      (mid, fname))
    if exists:
        return jsonify({"ok": True, "file_name": fname, "note": "手册已存在"})

    db.execute_write(
        "INSERT INTO material_documents(material_id, doc_type, file_path, file_name) VALUES (?,?,?,?)",
        (mid, "datasheet", path, fname))
    return jsonify({"ok": True, "file_name": fname})


@app.route("/api/materials/<int:mid>/image", methods=["GET"])
@login_required
def material_image(mid):
    """物料图片（本地文件）"""
    rows = db.query("SELECT image_path FROM materials WHERE id=?", (mid,))
    if not rows or not rows[0]["image_path"] or not os.path.exists(rows[0]["image_path"]):
        return jsonify({"error": "无图片"}), 404
    return send_file(rows[0]["image_path"])


@app.route("/api/materials/<int:mid>/fetch_image", methods=["POST"])
@login_required
def fetch_image(mid):
    """按编号从立创补取物料图片"""
    rows = db.query("SELECT code, image_path FROM materials WHERE id=?", (mid,))
    if not rows:
        return jsonify({"error": "物料不存在"}), 404
    code = (rows[0]["code"] or "").strip()
    if not code:
        return jsonify({"error": "该物料没有立创C编号，无法获取图片"}), 400
    if rows[0]["image_path"] and os.path.exists(rows[0]["image_path"]):
        return jsonify({"ok": True, "note": "图片已存在"})
    try:
        detail = lcsc.fetch_detail(code)
    except Exception:
        detail = []
    if not detail or not detail[0].get("image"):
        return jsonify({"error": "立创未提供图片或获取失败"}), 404
    path = lcsc.download_image(detail[0]["image"], code, os.path.join(UPLOAD_DIR, str(mid)))
    if not path:
        return jsonify({"error": "图片下载失败"}), 502
    db.execute_write("UPDATE materials SET image_path=? WHERE id=?", (path, mid))
    return jsonify({"ok": True})


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
    """手册操作：?mode=preview 浏览器直接打开；默认下载（自动重命名为 物料名称_型号）"""
    rows = db.query("""SELECT d.file_path, m.name AS mat_name, m.model AS mat_model, m.code AS mat_code
                       FROM material_documents d JOIN materials m ON d.material_id = m.id
                       WHERE d.id=?""", (did,))
    if not rows or not os.path.exists(rows[0]["file_path"]):
        return jsonify({"error": "文件不存在"}), 404
    path = rows[0]["file_path"]

    # 下载文件名：物料名称_型号_数据手册.pdf（非法字符替换）
    base = "_".join(x for x in (rows[0]["mat_name"], rows[0]["mat_model"]) if x) or rows[0]["mat_code"] or "手册"
    safe_base = re.sub(r'[\\/:*?"<>|]', "_", base).strip() or "手册"
    ext = os.path.splitext(path)[1] or ".pdf"

    if request.args.get("mode") == "preview":
        return send_file(path, as_attachment=False)  # 浏览器内直接打开
    return send_file(path, as_attachment=True, download_name=f"{safe_base}{ext}")


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
