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
import io
import base64
import hashlib
import shutil
import threading
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, send_file, session
from config import UPLOAD_DIR, MAX_WAREHOUSE_LEVEL, BASE_DIR, SQLITE_PATH, DB_TYPE
import db
import lcsc

app = Flask(__name__, static_folder="static", static_url_path="")
# 固定密钥：gunicorn 多 worker 各自 os.urandom(24) 会导致 session 跨进程验签失败（登录后立刻被踢回登录页）
# 可通过环境变量 SECRET_KEY 覆盖
app.secret_key = os.environ.get("SECRET_KEY") or "personal-erp-fixed-secret-9f4c2a8e7b1d"

db.init_schema()
db.ensure_default_admin()
db.run_migrations()


# ---------------- 文件存储辅助（BLOB 优先，兼容旧磁盘文件） ----------------
def _read_material_file(path_col, blob_col, rid):
    """按 id 读取文件内容：优先数据库 BLOB，其次旧磁盘路径。返回 bytes 或 None"""
    rows = db.query(f"SELECT {path_col} AS p, {blob_col} AS b FROM "
                    f"{'materials' if path_col == 'image_path' else 'material_documents'} WHERE id=?", (rid,))
    if not rows:
        return None
    r = rows[0]
    if r["b"] is not None:
        return bytes(r["b"])
    if r["p"] and os.path.exists(r["p"]):
        with open(r["p"], "rb") as f:
            return f.read()
    return None


def _serve_bytes(data: bytes, download_name: str, inline: bool):
    """从内存返回文件（预览 inline / 下载 attachment）"""
    ext = os.path.splitext(download_name)[1].lower()
    mime = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return send_file(io.BytesIO(data), as_attachment=not inline,
                     download_name=download_name, mimetype=mime)


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


@app.route("/api/default_admin_exists")
def default_admin_exists():
    """登录页探测：admin 是否仍为默认密码（是则前端显示修改提醒，改密后自动消失）"""
    import hashlib
    pw = hashlib.sha256("admin123".encode()).hexdigest()
    rows = db.query("SELECT id FROM users WHERE username='admin' AND password_hash=?", (pw,))
    return jsonify({"default_exists": bool(rows)})


# ---------------- 版本与在线更新（GitHub） ----------------
# GITHUB_REPO：GitHub 仓库（格式 用户名/仓库名）；GITHUB_PROXY：可选加速前缀（如 https://gh-proxy.com）
# GITHUB_TOKEN：私有仓库必填（fine-grained PAT，只给 Contents:Read-only 权限即可）
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip("/")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "master")
GITHUB_PROXY = os.environ.get("GITHUB_PROXY", "").rstrip("/")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
VERSION_FILE = os.path.join(BASE_DIR, ".version")
# 更新时绝不覆盖的本地目录/文件（数据与环境配置）
_UPD_KEEP = {".env", "data", "uploads", "__pycache__", ".git", ".version",
             "docker-compose.yml", "Dockerfile", "start.sh", ".dockerignore"}


def _gh(path):
    """拼接 GitHub 地址（可选走加速代理）"""
    return GITHUB_PROXY + path if GITHUB_PROXY else path


def _gh_headers():
    """带 Token 的请求头（私有仓库需要）"""
    h = {"User-Agent": "erp-updater"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _local_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except Exception:
        return "unknown"


def _remote_version():
    """查询 GitHub 远程分支最新 commit sha"""
    import requests
    url = _gh(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}")
    r = requests.get(url, timeout=15, headers=_gh_headers())
    if not r.ok:
        raise RuntimeError(f"GitHub 接口返回 HTTP {r.status_code}")
    return r.json().get("sha", "")


@app.route("/api/update/status")
@admin_required
def update_status():
    """检查 GitHub 是否有新版本"""
    if not GITHUB_REPO:
        return jsonify({"error": "未配置 GITHUB_REPO 环境变量（格式：用户名/仓库名）"}), 400
    try:
        remote = _remote_version()
    except Exception as e:
        hint = "。私有仓库需配置 GITHUB_TOKEN" if not GITHUB_TOKEN else ""
        return jsonify({"error": f"无法访问 GitHub：{e}{hint}。NAS 若无法直连，可配置 GITHUB_PROXY 加速前缀"}), 502
    local = _local_version()
    return jsonify({"repo": GITHUB_REPO, "branch": GITHUB_BRANCH, "local": local,
                    "remote": remote[:7], "update_available": local != remote})


@app.route("/api/update/apply", methods=["POST"])
@admin_required
def update_apply():
    """从 GitHub 下载最新代码覆盖到 /app 并平滑重启（数据库与部署配置不受影响）"""
    import requests
    if not GITHUB_REPO:
        return jsonify({"error": "未配置 GITHUB_REPO 环境变量（格式：用户名/仓库名）"}), 400

    def _fetch_tree():
        """经 api.github.com 拉取分支完整文件树（国内 NAS 通常可直连该域名）"""
        url = _gh(f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1")
        r = requests.get(url, timeout=30, headers=_gh_headers())
        if not r.ok:
            raise RuntimeError(f"获取文件树失败 HTTP {r.status_code}")
        tree = r.json().get("tree", [])
        files = [t for t in tree if t.get("type") == "blob"]
        if not files:
            raise RuntimeError("仓库文件树为空")
        return files

    def _write_base64(content_b64, rel):
        """base64 内容写入目标文件（自动建目录）"""
        dest = os.path.join(BASE_DIR, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(base64.b64decode(content_b64))

    # 1. 拉文件树
    try:
        files = _fetch_tree()
    except Exception as e:
        return jsonify({"error": f"{e}。请检查 GITHUB_TOKEN 权限（需 Contents:Read-only）或配置 GITHUB_PROXY"}), 502
    # 过滤：跳过数据/环境/部署配置文件
    files = [t for t in files if t["path"].split("/")[0] not in _UPD_KEEP
             and ".." not in t["path"].split("/")]
    # 2. 逐文件下载（经 api.github.com 的 contents 接口，走与查版本相同的可达域名）
    done, errs = 0, []
    for t in files:
        rel = t["path"]
        url = _gh(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{rel}?ref={GITHUB_BRANCH}")
        try:
            r = requests.get(url, timeout=30, headers=_gh_headers())
            if not r.ok:
                errs.append(f"{rel}: HTTP {r.status_code}")
                continue
            _write_base64(r.json().get("content", ""), rel)
            done += 1
        except Exception as e:
            errs.append(f"{rel}: {e}")
    if errs and done == 0:
        return jsonify({"error": "全部文件下载失败：" + "; ".join(errs[:3])}), 502
    # 3. 记录新版本号
    try:
        sha = _remote_version()
    except Exception:
        sha = ""
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(sha)
    # 4. 延迟 2 秒平滑重启：容器内向 gunicorn 主进程(PID1)发 SIGHUP；本地开发则直接退出进程
    def _reload():
        import signal, time
        time.sleep(2)
        try:
            if os.getppid() == 1:
                os.kill(1, signal.SIGHUP)   # gunicorn 平滑重启 worker，加载新代码
            else:
                os._exit(0)                 # 本地 python app.py 场景，退出后手动重启
        except Exception:
            os._exit(0)
    threading.Thread(target=_reload, daemon=True).start()
    msg = f"更新完成（{done} 个文件），服务正在平滑重启，约 5 秒后请刷新页面"
    if errs:
        msg += f"。{len(errs)} 个文件失败：{errs[0]}"
    return jsonify({"ok": True, "version": sha[:7], "msg": msg})


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
    sql = """SELECT m.id, m.name, m.code, m.model, m.category, m.package, m.brand,
                    m.description, m.quantity, m.unit, m.warehouse_id, m.lcsc_url,
                    m.image_path, (m.image_blob IS NOT NULL) AS has_image,
                    m.created_at, m.updated_at, w.name AS warehouse_name
             FROM materials m
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

    # 立创建库时自动下载物料图片（失败不影响建物料）；图片内容直接存数据库
    image_url = (d.get("image_url") or "").strip()
    if image_url and (d.get("code") or "").strip():
        code = d["code"].strip()
        img_bytes = lcsc.fetch_image_bytes(image_url)
        if img_bytes:
            db.execute_write("UPDATE materials SET image_blob=? WHERE id=?", (img_bytes, last_id))
    return jsonify({"id": last_id}), 201


@app.route("/api/materials/<int:mid>", methods=["PUT"])
@login_required
def update_material(mid):
    d = request.get_json(force=True)
    _, n = db.execute_write(
        """UPDATE materials SET name=?, code=?, model=?, category=?, package=?,
           brand=?, description=?, quantity=?, unit=?, warehouse_id=?, lcsc_url=?,
           updated_at=? WHERE id=?""",
        (d.get("name", ""), d.get("code", ""), d.get("model", ""), d.get("category", ""),
         d.get("package", ""), d.get("brand", ""), d.get("description", ""),
         int(d.get("quantity") or 0), d.get("unit", "个"),
         d.get("warehouse_id") or None, d.get("lcsc_url", ""),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid))
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
    db.execute_write("UPDATE materials SET quantity=?, updated_at=? WHERE id=?",
                     (new_qty, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid))
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

    pdf_bytes, fname = lcsc.fetch_datasheet_bytes(datasheet_url)
    if not pdf_bytes:
        return jsonify({"error": "手册PDF下载失败（网络受限或超时），请稍后重试"}), 502

    # 避免重复登记同一手册
    exists = db.query("SELECT id FROM material_documents WHERE material_id=? AND file_name=?",
                      (mid, fname))
    if exists:
        return jsonify({"ok": True, "file_name": fname, "note": "手册已存在"})

    db.execute_write(
        "INSERT INTO material_documents(material_id, doc_type, file_path, file_name, file_blob) VALUES (?,?,?,?,?)",
        (mid, "datasheet", "", fname, pdf_bytes))
    return jsonify({"ok": True, "file_name": fname})


@app.route("/api/materials/<int:mid>/image", methods=["GET"])
@login_required
def material_image(mid):
    """物料图片（优先数据库 BLOB，其次旧磁盘文件）"""
    rows = db.query("SELECT image_path, image_blob FROM materials WHERE id=?", (mid,))
    if not rows:
        return jsonify({"error": "物料不存在"}), 404
    data = _read_material_file("image_path", "image_blob", mid)
    if data is None:
        return jsonify({"error": "无图片"}), 404
    # 按内容判断图片类型
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return send_file(io.BytesIO(data), mimetype=mime)


@app.route("/api/materials/<int:mid>/fetch_image", methods=["POST"])
@login_required
def fetch_image(mid):
    """按编号从立创补取物料图片（存入数据库）"""
    rows = db.query("SELECT code, image_path, image_blob FROM materials WHERE id=?", (mid,))
    if not rows:
        return jsonify({"error": "物料不存在"}), 404
    code = (rows[0]["code"] or "").strip()
    if not code:
        return jsonify({"error": "该物料没有立创C编号，无法获取图片"}), 400
    if rows[0]["image_blob"] is not None or (rows[0]["image_path"] and os.path.exists(rows[0]["image_path"])):
        return jsonify({"ok": True, "note": "图片已存在"})
    try:
        detail = lcsc.fetch_detail(code)
    except Exception:
        detail = []
    if not detail or not detail[0].get("image"):
        return jsonify({"error": "立创未提供图片或获取失败"}), 404
    img_bytes = lcsc.fetch_image_bytes(detail[0]["image"])
    if not img_bytes:
        return jsonify({"error": "图片下载失败"}), 502
    db.execute_write("UPDATE materials SET image_blob=? WHERE id=?", (img_bytes, mid))
    return jsonify({"ok": True})


# ---------------- 手册上传 / 下载 ----------------
ALLOWED_DOC_TYPES = {"datasheet", "user_manual", "other"}


@app.route("/api/materials/<int:mid>/documents", methods=["POST"])
@login_required
def upload_document(mid):
    """上传手册：文件内容直接存入数据库 BLOB"""
    if not db.query("SELECT id FROM materials WHERE id=?", (mid,)):
        return jsonify({"error": "物料不存在"}), 404
    f = request.files.get("file")
    doc_type = request.form.get("doc_type", "datasheet")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    if doc_type not in ALLOWED_DOC_TYPES:
        return jsonify({"error": "doc_type 不合法"}), 400
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", f.filename)
    blob = f.read()  # 读入内存，存数据库
    last_id, _ = db.execute_write(
        "INSERT INTO material_documents(material_id, doc_type, file_path, file_name, file_blob) VALUES (?,?,?,?,?)",
        (mid, doc_type, "", safe_name, blob))
    return jsonify({"id": last_id, "file_name": safe_name}), 201


@app.route("/api/documents/<int:did>", methods=["GET"])
@login_required
def download_document(did):
    """手册操作：?mode=preview 浏览器直接打开；默认下载（自动重命名为 物料名称_型号）"""
    rows = db.query("""SELECT d.file_path, d.file_name, m.name AS mat_name, m.model AS mat_model, m.code AS mat_code
                       FROM material_documents d JOIN materials m ON d.material_id = m.id
                       WHERE d.id=?""", (did,))
    if not rows:
        return jsonify({"error": "记录不存在"}), 404
    data = _read_material_file("file_path", "file_blob", did)
    if data is None:
        return jsonify({"error": "文件不存在"}), 404

    # 下载文件名：物料名称_型号_数据手册.pdf（非法字符替换）
    base = "_".join(x for x in (rows[0]["mat_name"], rows[0]["mat_model"]) if x) or rows[0]["mat_code"] or "手册"
    safe_base = re.sub(r'[\\/:*?"<>|]', "_", base).strip() or "手册"
    ext = os.path.splitext(rows[0]["file_name"])[1] or ".pdf"

    if request.args.get("mode") == "preview":
        return _serve_bytes(data, f"{safe_base}{ext}", inline=True)   # 浏览器内直接打开
    return _serve_bytes(data, f"{safe_base}{ext}", inline=False)      # 下载，友好文件名


@app.route("/api/documents/<int:did>", methods=["DELETE"])
@login_required
def delete_document(did):
    rows = db.query("SELECT file_path FROM material_documents WHERE id=?", (did,))
    if not rows:
        return jsonify({"error": "记录不存在"}), 404
    db.execute_write("DELETE FROM material_documents WHERE id=?", (did,))
    # 旧磁盘文件一并清理（新数据 BLOB 随记录删除）
    if rows[0]["file_path"] and os.path.exists(rows[0]["file_path"]):
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
    if DB_TYPE == "postgres":
        # postgres 库大小（pg_database_size 返回字节）
        try:
            info["size_kb"] = round(db.query(
                "SELECT pg_database_size(current_database()) AS s")[0]["s"] / 1024, 1)
        except Exception:
            pass
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
    # 0.0.0.0：Docker 容器内必须监听全部网卡，宿主机端口映射才能进得来
    app.run(host="0.0.0.0", port=5000, debug=False)
