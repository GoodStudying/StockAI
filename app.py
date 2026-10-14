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
import time
import hmac
import base64
import hashlib
import secrets
import threading
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, send_file, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from config import UPLOAD_DIR, MAX_WAREHOUSE_LEVEL, BASE_DIR, SQLITE_PATH, DB_TYPE
import db
import lcsc

app = Flask(__name__, static_folder="static", static_url_path="")


def _load_or_create_secret():
    """
    密钥优先级：环境变量 SECRET_KEY > data/.secret_key 文件 > 随机生成并落盘。
    必须固定：gunicorn 多 worker 各自 os.urandom() 会导致 session 跨进程验签失败
    （登录后立刻被踢回登录页）；落盘在 data/ 下，在线更新不会覆盖。
    """
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    p = os.path.join(BASE_DIR, "data", ".secret_key")
    try:
        with open(p, "r", encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(k)
    except OSError:
        pass
    return k


app.secret_key = _load_or_create_secret()
# 请求体上限：手册 PDF 全量读入内存存 BLOB，限制 50MB 防止超大文件把 NAS 内存打爆
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

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


def _parse_int(v, minimum=0):
    """解析整数入参；非法输入返回 None（避免 int('abc') 抛 ValueError 变成 500）"""
    try:
        q = int(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return q if q >= minimum else None


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


# ---------------- 密码哈希（werkzeug PBKDF2 加盐，兼容旧无盐 SHA-256 自动升级） ----------------
def _hash_password(pw: str) -> str:
    # 显式指定 pbkdf2:sha256：新 Werkzeug 默认 scrypt，但 Cloudflare Workers 的
    # Web Crypto 不支持 scrypt——保持 pbkdf2 是为了将来上云可无缝校验存量密码
    return generate_password_hash(pw, method="pbkdf2:sha256:260000")


def _verify_password(stored: str, pw: str):
    """返回 (是否匹配, 是否为旧版哈希)。旧版无盐 sha256 校验通过后由调用方原地升级哈希。"""
    if stored and len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower()):
        return hashlib.sha256(pw.encode()).hexdigest() == stored.lower(), True
    try:
        return check_password_hash(stored, pw), False
    except Exception:
        return False, False


# 登录限流：同一 IP 5 分钟内最多 10 次尝试，防暴力破解（内存计数，重启清零）
_LOGIN_ATTEMPTS = {}


def _login_rate_check(ip):
    now = time.time()
    lst = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < 300]
    if len(lst) >= 10:
        _LOGIN_ATTEMPTS[ip] = lst
        return False
    lst.append(now)
    _LOGIN_ATTEMPTS[ip] = lst
    return True


@app.route("/api/login", methods=["POST"])
def login():
    if not _login_rate_check(request.remote_addr or "unknown"):
        return jsonify({"error": "尝试过于频繁，请 5 分钟后再试"}), 429
    d = request.get_json(force=True)
    uname = (d.get("username") or "").strip()
    # 先按用户名查，再验哈希（加盐哈希无法参与 WHERE 匹配）
    rows = db.query("SELECT id, username, role, password_hash FROM users WHERE username=?", (uname,))
    if rows:
        ok, legacy = _verify_password(rows[0]["password_hash"], d.get("password") or "")
        if ok:
            if legacy:
                # 旧无盐 SHA-256 登录成功，立即原地升级为加盐 PBKDF2
                db.execute_write("UPDATE users SET password_hash=? WHERE id=?",
                                 (_hash_password(d["password"]), rows[0]["id"]))
            user = {"id": rows[0]["id"], "username": rows[0]["username"], "role": rows[0]["role"]}
            session["user"] = user
            return jsonify({"ok": True, "user": user})
    return jsonify({"error": "用户名或密码错误"}), 401


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
    rows = db.query("SELECT password_hash FROM users WHERE username='admin'")
    is_default = bool(rows) and _verify_password(rows[0]["password_hash"], "admin123")[0]
    return jsonify({"default_exists": is_default})


# ---------------- API Token 认证（预留给 Cherry Studio / MCP 等程序化调用的 /api/v1 接口） ----------------
# .env 配置 API_TOKEN=xxx（多个用英文逗号分隔）；未配置则 /api/v1/* 一律返回 503
API_TOKENS = {t.strip() for t in os.environ.get("API_TOKEN", "").split(",") if t.strip()}


def token_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not API_TOKENS:
            return jsonify({"error": "服务端未配置 API_TOKEN（在 .env 中添加后重启生效）"}), 503
        auth = request.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not tok or not any(hmac.compare_digest(tok, t) for t in API_TOKENS):
            return jsonify({"error": "API Token 无效或缺失（请求头 Authorization: Bearer <token>）"}), 401
        # 流水操作人留痕：只记 token 前 4 位，不落完整 token
        g.api_operator = f"api:{tok[:4]}***"
        return f(*args, **kwargs)
    return wrapper


@app.errorhandler(413)
def request_too_large(_e):
    """上传超过 MAX_CONTENT_LENGTH 时返回 JSON 而不是默认 HTML 错误页"""
    return jsonify({"error": "文件过大，上限 50MB"}), 413


# ---------------- 版本与在线更新（GitHub） ----------------
# GITHUB_REPO：GitHub 仓库（格式 用户名/仓库名）；GITHUB_PROXY：可选加速前缀（如 https://gh-proxy.com）
# GITHUB_TOKEN：私有仓库必填（fine-grained PAT，只给 Contents:Read-only 权限即可）
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip("/")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "master")
GITHUB_PROXY = os.environ.get("GITHUB_PROXY", "").rstrip("/")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
VERSION_FILE = os.path.join(BASE_DIR, ".version")
# 更新时绝不覆盖的本地目录/文件（数据/环境/部署配置——NAS 上改过密码Token的文件永不被覆盖）
_UPD_KEEP = {".env", "data", "uploads", "__pycache__", ".git", ".version",
             "docker-compose.yml", "Dockerfile", "start.sh", ".dockerignore"}
# 后台更新进度（跨请求共享；gunicorn 多 worker 间通过文件落盘同步）
_UPDATE = {"state": "idle", "msg": "", "done": 0, "total": 0, "at": 0.0}
_UPDATE_LOCK = threading.Lock()
PROGRESS_FILE = os.path.join(BASE_DIR, ".update_progress")


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
        # 403 常见原因：Token 未勾 Contents:Read-only / 限流；404：仓库不存在或无权限
        try:
            detail = r.json().get("message", "")
        except Exception:
            detail = ""
        hint = {403: "（Token 缺少 Contents:Read-only 权限或触发限流）",
                404: "（仓库不存在，或 Token 无权访问）"}.get(r.status_code, "")
        raise RuntimeError(f"GitHub 接口返回 HTTP {r.status_code}{hint} {detail}")
    return r.json().get("sha", "")


@app.route("/api/update/status")
@admin_required
def update_status():
    """检查 GitHub 是否有新版本；同时返回后台更新进度（progress 字段，前端轮询展示）"""
    if not GITHUB_REPO:
        return jsonify({"error": "未配置 GITHUB_REPO 环境变量（格式：用户名/仓库名）"}), 400
    local = _local_version()
    resp = {"repo": GITHUB_REPO, "branch": GITHUB_BRANCH, "local": local,
            "progress": dict(_UPDATE)}
    try:
        remote = _remote_version()
        resp.update(remote=remote[:7], update_available=local != remote)
    except Exception as e:
        hint = "。私有仓库需配置 GITHUB_TOKEN" if not GITHUB_TOKEN else ""
        resp.update(remote="", update_available=False, check_error=f"{e}{hint}")
    return jsonify(resp)


def _progress_write(state, msg, done=0, total=0):
    """写更新进度（内存 + 落盘，gunicorn 另一 worker 读盘同步）"""
    import time, json
    with _UPDATE_LOCK:
        _UPDATE.update(state=state, msg=msg, done=done, total=total, at=time.time())
        try:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(_UPDATE, f)
        except Exception:
            pass


def _progress_load():
    """启动时/查询时从盘读进度（跨 worker 可见）"""
    import time, json
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        # 30 分钟前的进度视为过期
        if time.time() - d.get("at", 0) < 1800:
            _UPDATE.update(d)
    except Exception:
        pass


_progress_load()


def _do_update():
    """后台更新线程：文件树 → 逐文件下载 → 落版本号 → 平滑重启（全程走 api.github.com，国内可达）"""
    import requests, signal, time
    try:
        _progress_write("running", "正在获取文件树...")
        url = _gh(f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1")
        r = requests.get(url, timeout=30, headers=_gh_headers())
        if not r.ok:
            raise RuntimeError(f"获取文件树失败 HTTP {r.status_code}（检查 GITHUB_TOKEN 权限）")
        files = [t for t in r.json().get("tree", []) if t.get("type") == "blob"]
        if not files:
            raise RuntimeError("仓库文件树为空")
        # 过滤：跳过数据/环境/部署配置文件，防目录穿越
        files = [t for t in files if t["path"].split("/")[0] not in _UPD_KEEP
                 and ".." not in t["path"].split("/")]
        total = len(files)
        done, errs = 0, []
        for t in files:
            rel = t["path"]
            _progress_write("running", f"下载 {rel}", done, total)
            url = _gh(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{rel}?ref={GITHUB_BRANCH}")
            try:
                r = requests.get(url, timeout=30, headers=_gh_headers())
                if not r.ok:
                    errs.append(f"{rel}: HTTP {r.status_code}")
                    continue
                dest = os.path.join(BASE_DIR, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(base64.b64decode(r.json().get("content", "")))
                done += 1
            except Exception as e:
                errs.append(f"{rel}: {e}")
        if done == 0:
            raise RuntimeError("全部文件下载失败：" + "; ".join(errs[:3]))
        if errs:
            # 部分失败：不写版本号、不重启，保持“可重试更新”状态，
            # 避免残缺文件被误认为已是最新版而再也触发不了更新
            _progress_write("error",
                            f"{len(errs)}/{total} 个文件下载失败（如 {errs[0]}），版本号未更新，请重试更新",
                            done, total)
            return
        # 全部成功才记录新版本号
        try:
            sha = _remote_version()
        except Exception:
            sha = ""
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(sha)
        _progress_write("restarting", f"更新完成（{done}/{total} 个文件），2 秒后平滑重启", done, total)
        time.sleep(2)
        # 平滑重启：容器内向 gunicorn 主进程(PID1)发 SIGHUP；本地开发直接退出
        try:
            if os.getppid() == 1:
                os.kill(1, signal.SIGHUP)
            else:
                os._exit(0)
        except Exception:
            os._exit(0)
    except Exception as e:
        _progress_write("error", f"更新失败：{e}")


@app.route("/api/update/apply", methods=["POST"])
@admin_required
def update_apply():
    """启动后台更新线程并立即返回（前端轮询 /api/update/status 看进度，避开网关超时）"""
    if not GITHUB_REPO:
        return jsonify({"error": "未配置 GITHUB_REPO 环境变量（格式：用户名/仓库名）"}), 400
    with _UPDATE_LOCK:
        if _UPDATE.get("state") == "running":
            return jsonify({"error": "更新正在进行中，请稍候"}), 409
    _progress_write("running", "准备更新...")
    threading.Thread(target=_do_update, daemon=True).start()
    return jsonify({"ok": True, "msg": "更新已启动，请通过检查更新查看进度"})


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
    # 防御：库（含子库）内还有物料时拒绝删除。
    # materials.warehouse_id 外键无 ON DELETE 行为，直接删会触发 IntegrityError 变 500
    ids = _descendant_ids(wid)
    ph = ",".join("?" * len(ids))
    cnt = db.query(f"SELECT COUNT(*) AS c FROM materials WHERE warehouse_id IN ({ph})",
                   tuple(ids))[0]["c"]
    if cnt:
        return jsonify({"error": f"该库及其子库下还有 {cnt} 件物料，请先移出或删除物料"}), 400
    _, n = db.execute_write("DELETE FROM warehouses WHERE id=?", (wid,))
    if n == 0:
        return jsonify({"error": "库不存在"}), 404
    return jsonify({"ok": True})


# ---------------- 物料管理 ----------------
@app.route("/api/materials", methods=["GET"])
@login_required
def list_materials():
    kw = (request.args.get("q") or "").strip()
    wid_raw = request.args.get("warehouse_id")
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
    if wid_raw:
        wid = _parse_int(wid_raw, 1)
        if wid is None:
            return jsonify({"error": "warehouse_id 不合法"}), 400
        if include_children:
            # 仓库层级包含关系：查该库及其全部子孙库的物料
            ids = _descendant_ids(wid)
            ph = ",".join("?" * len(ids))
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
    qty = _parse_int(d.get("quantity") or 0, 0)
    if qty is None:
        return jsonify({"error": "数量必须是不小于0的整数"}), 400
    last_id, _ = db.execute_write(
        """INSERT INTO materials(name, code, model, category, package, brand,
                                 description, quantity, unit, warehouse_id, lcsc_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (name, d.get("code", ""), d.get("model", ""), d.get("category", ""),
         d.get("package", ""), d.get("brand", ""), d.get("description", ""),
         qty, d.get("unit", "个"),
         d.get("warehouse_id") or None, d.get("lcsc_url", "")))

    # 创建物料时自动下载物料图片（失败不影响建物料）；图片内容直接存数据库
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
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "物料名称不能为空"}), 400
    qty = _parse_int(d.get("quantity") or 0, 0)
    if qty is None:
        return jsonify({"error": "数量必须是不小于0的整数"}), 400
    _, n = db.execute_write(
        """UPDATE materials SET name=?, code=?, model=?, category=?, package=?,
           brand=?, description=?, quantity=?, unit=?, warehouse_id=?, lcsc_url=?,
           updated_at=? WHERE id=?""",
        (name, d.get("code", ""), d.get("model", ""), d.get("category", ""),
         d.get("package", ""), d.get("brand", ""), d.get("description", ""),
         qty, d.get("unit", "个"),
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
def _apply_stock(op, mid, qty, operator, remark):
    """
    原子出入库核心逻辑（网页端与 /api/v1 共用）。
    - 出库用「UPDATE ... WHERE quantity>=?」单语句扣减，并发下也不会扣成负数（防超卖）
    - 库存更新与流水写入同一事务，同成败
    返回 (新库存, None)；失败返回 (None, (错误消息, HTTP状态码))
    """
    rows = db.query("SELECT id, quantity FROM materials WHERE id=?", (mid,))
    if not rows:
        return None, ("物料不存在", 404)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if op == "out":
        with db.transaction() as cur:
            cur.execute(db.convert_sql(
                "UPDATE materials SET quantity=quantity-?, updated_at=? WHERE id=? AND quantity>=?"),
                (qty, ts, mid, qty))
            if cur.rowcount == 0:
                # 用同一 cursor 回读真实库存（避免事务持锁期间再开新连接查询）
                cur.execute(db.convert_sql("SELECT quantity FROM materials WHERE id=?"), (mid,))
                r = cur.fetchone()
                cur_qty = r["quantity"] if r is not None else 0
                return None, (f"库存不足，当前库存 {cur_qty}", 400)
            cur.execute(db.convert_sql(
                "INSERT INTO stock_records(material_id, type, quantity, operator, remark) VALUES (?,?,?,?,?)"),
                (mid, op, qty, operator, remark))
    else:
        with db.transaction() as cur:
            cur.execute(db.convert_sql(
                "UPDATE materials SET quantity=quantity+?, updated_at=? WHERE id=?"), (qty, ts, mid))
            cur.execute(db.convert_sql(
                "INSERT INTO stock_records(material_id, type, quantity, operator, remark) VALUES (?,?,?,?,?)"),
                (mid, op, qty, operator, remark))
    # 事务提交后回读真实库存返回（并发场景下不用事务前的旧值）
    new_qty = db.query("SELECT quantity FROM materials WHERE id=?", (mid,))[0]["quantity"]
    return new_qty, None


@app.route("/api/stock/<op>", methods=["POST"])
@login_required
def stock_inout(op):
    """op: in=入库 out=出库。body: {material_id, quantity, remark}"""
    if op not in ("in", "out"):
        return jsonify({"error": "操作类型不合法"}), 400
    d = request.get_json(force=True)
    mid = _parse_int(d.get("material_id"), 1)
    qty = _parse_int(d.get("quantity"), 1)
    if mid is None:
        return jsonify({"error": "material_id 不合法"}), 400
    if qty is None:
        return jsonify({"error": "数量必须是大于0的整数"}), 400
    new_qty, err = _apply_stock(op, mid, qty, session["user"]["username"], d.get("remark", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify({"ok": True, "quantity": new_qty})


def _query_stock_records():
    """库存流水查询（网页端与 /api/v1 共用）"""
    mid = request.args.get("material_id")
    sql = """SELECT r.*, m.name AS material_name, m.code AS material_code
             FROM stock_records r JOIN materials m ON r.material_id = m.id"""
    params = ()
    if mid:
        sql += " WHERE r.material_id = ?"
        params = (mid,)
    sql += " ORDER BY r.id DESC LIMIT 500"
    return db.query(sql, params)


@app.route("/api/stock/records", methods=["GET"])
@login_required
def stock_records():
    """库存流水，支持按物料过滤"""
    return jsonify(_query_stock_records())


# ---------------- 开放接口 /api/v1（API Token 认证，供 Cherry Studio / MCP 等程序化调用） ----------------

@app.route("/api/v1/materials", methods=["GET"])
@token_required
def v1_search_materials():
    """物料搜索：q 模糊匹配 名称/编码/型号；code 精确匹配编码"""
    code = (request.args.get("code") or "").strip()
    kw = (request.args.get("q") or "").strip()
    cols = "id, name, code, model, category, package, brand, quantity, unit, warehouse_id"
    if code:
        rows = db.query(f"SELECT {cols} FROM materials WHERE code=? LIMIT 50", (code,))
    elif kw:
        like = f"%{kw}%"
        rows = db.query(
            f"SELECT {cols} FROM materials WHERE name LIKE ? OR code LIKE ? OR model LIKE ? "
            f"ORDER BY id DESC LIMIT 50", (like, like, like))
    else:
        return jsonify({"error": "请提供 q 或 code 参数"}), 400
    return jsonify({"count": len(rows), "materials": rows})


@app.route("/api/v1/stock/<op>", methods=["POST"])
@token_required
def v1_stock_inout(op):
    """程序化出入库（与网页端同一原子逻辑）。body: {material_id, quantity, remark}"""
    if op not in ("in", "out"):
        return jsonify({"error": "操作类型不合法"}), 400
    d = request.get_json(force=True)
    mid = _parse_int(d.get("material_id"), 1)
    qty = _parse_int(d.get("quantity"), 1)
    if mid is None:
        return jsonify({"error": "material_id 不合法"}), 400
    if qty is None:
        return jsonify({"error": "数量必须是大于0的整数"}), 400
    new_qty, err = _apply_stock(op, mid, qty, getattr(g, "api_operator", "api"), d.get("remark", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify({"ok": True, "material_id": mid, "quantity": new_qty})


@app.route("/api/v1/stock/records", methods=["GET"])
@token_required
def v1_stock_records():
    """程序化查询库存流水"""
    return jsonify({"records": _query_stock_records()})


def _match_material(item):
    """
    BOM 条目匹配物料，优先级：编码精确 > 型号精确 > 名称精确 > 名称模糊。
    返回 (material或None, 匹配方式, 候选列表)。匹配方式: code/model/name/name_fuzzy/ambiguous/not_found
    """
    code = (item.get("code") or "").strip() if isinstance(item.get("code"), str) else ""
    model = (item.get("model") or "").strip() if isinstance(item.get("model"), str) else ""
    name = (item.get("name") or "").strip() if isinstance(item.get("name"), str) else ""
    cols = "id, name, code, model, quantity, unit, warehouse_id"
    if code:
        rows = db.query(f"SELECT {cols} FROM materials WHERE code=?", (code,))
        if rows:
            return rows[0], "code", []
    if model:
        rows = db.query(f"SELECT {cols} FROM materials WHERE model=?", (model,))
        if len(rows) == 1:
            return rows[0], "model", []
        if len(rows) > 1:
            return None, "ambiguous", rows
    if name:
        rows = db.query(f"SELECT {cols} FROM materials WHERE name=?", (name,))
        if len(rows) == 1:
            return rows[0], "name", []
        if len(rows) > 1:
            return None, "ambiguous", rows
        rows = db.query(f"SELECT {cols} FROM materials WHERE name LIKE ? LIMIT 10", (f"%{name}%",))
        if len(rows) == 1:
            return rows[0], "name_fuzzy", []
        if len(rows) > 1:
            return None, "ambiguous", rows
    return None, "not_found", []


@app.route("/api/v1/bom/check", methods=["POST"])
@token_required
def v1_bom_check():
    """
    BOM 核对。body: {"items": [{"code"/"model"/"name", "quantity"}, ...]}
    逐项匹配物料并对比库存，返回每行状态与汇总：
    ok=库存充足 shortage=缺料(附缺额) not_found=未找到 ambiguous=多个匹配 invalid=入参错误
    """
    d = request.get_json(force=True)
    items = d.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items 必须是非空数组"}), 400
    if len(items) > 500:
        return jsonify({"error": "单次最多核对 500 行"}), 400
    results = []
    summary = {"total": 0, "ok": 0, "shortage": 0, "not_found": 0, "ambiguous": 0, "invalid": 0}
    for it in items:
        if not isinstance(it, dict):
            results.append({"input": it, "status": "invalid", "message": "条目格式错误（应为对象）"})
            summary["invalid"] += 1
            summary["total"] += 1
            continue
        qty = _parse_int(it.get("quantity", 1), 1)
        if qty is None:
            results.append({"input": it, "status": "invalid", "message": "quantity 必须是大于0的整数"})
            summary["invalid"] += 1
            summary["total"] += 1
            continue
        mat, how, cands = _match_material(it)
        row = {"input": {"code": it.get("code"), "model": it.get("model"), "name": it.get("name")},
               "need": qty}
        if how == "ambiguous":
            row.update(status="ambiguous", message="匹配到多个物料，请改用编码(code)精确定位",
                       candidates=cands)
            summary["ambiguous"] += 1
        elif mat is None:
            row.update(status="not_found", message="未找到匹配物料")
            summary["not_found"] += 1
        else:
            avail = mat["quantity"] or 0
            row.update(material=mat, matched_by=how, available=avail)
            if avail >= qty:
                row["status"] = "ok"
                summary["ok"] += 1
            else:
                row.update(status="shortage", shortage=qty - avail)
                summary["shortage"] += 1
        summary["total"] += 1
        results.append(row)
    return jsonify({"summary": summary, "results": results})


@app.route("/api/v1/duplicates", methods=["GET"])
@token_required
def v1_duplicates():
    """重复物料核对：按 编码 / 型号 / 名称 三个维度分组找出重复项"""
    groups = []
    for field, label in (("code", "编码"), ("model", "型号"), ("name", "名称")):
        dup = db.query(
            f"SELECT {field} AS v, COUNT(*) AS c FROM materials "
            f"WHERE {field} IS NOT NULL AND TRIM({field}) <> '' "
            f"GROUP BY {field} HAVING COUNT(*) > 1 ORDER BY c DESC")
        for r in dup:
            mats = db.query(
                f"SELECT id, name, code, model, quantity, unit, warehouse_id "
                f"FROM materials WHERE {field}=? ORDER BY id", (r["v"],))
            groups.append({"field": field, "field_label": label, "value": r["v"],
                           "count": r["c"], "materials": mats})
    return jsonify({"total_groups": len(groups), "duplicates": groups})


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
    # 按魔数判断图片类型（PNG/JPEG/GIF/WebP，未知按二进制流）
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:6] in (b"GIF89a", b"GIF87a"):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "application/octet-stream"
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
    last_id, _ = db.execute_write(
        "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
        (username, _hash_password(password), role))
    return jsonify({"id": last_id}), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def update_user(uid):
    """重置密码 / 修改角色"""
    d = request.get_json(force=True)
    rows = db.query("SELECT id, role FROM users WHERE id=?", (uid,))
    if not rows:
        return jsonify({"error": "用户不存在"}), 404
    old_role = rows[0]["role"]
    new_role = d.get("role")
    # 先校验角色变更，失败则整体不改（密码也不动）
    if new_role in ("admin", "user") and new_role != old_role:
        if uid == session["user"]["id"]:
            return jsonify({"error": "不能修改自己的角色（防止误降级失去管理权限）"}), 400
        if old_role == "admin":
            admins = db.query("SELECT COUNT(*) AS c FROM users WHERE role='admin'")[0]["c"]
            if admins <= 1:
                return jsonify({"error": "至少保留一名管理员"}), 400
    if d.get("password"):
        db.execute_write("UPDATE users SET password_hash=? WHERE id=?",
                         (_hash_password(d["password"]), uid))
    if new_role in ("admin", "user") and new_role != old_role:
        db.execute_write("UPDATE users SET role=? WHERE id=?", (new_role, uid))
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def delete_user(uid):
    if session["user"]["id"] == uid:
        return jsonify({"error": "不能删除当前登录账户"}), 400
    rows = db.query("SELECT role FROM users WHERE id=?", (uid,))
    if rows and rows[0]["role"] == "admin":
        admins = db.query("SELECT COUNT(*) AS c FROM users WHERE role='admin'")[0]["c"]
        if admins <= 1:
            return jsonify({"error": "不能删除最后一名管理员"}), 400
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
    # 用 SQLite 在线备份 API 生成一致性快照；
    # 直接 copyfile 可能复制到写入中途的脏页，得到损坏的备份文件
    import sqlite3
    src = sqlite3.connect(SQLITE_PATH)
    try:
        dst = sqlite3.connect(bak)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return send_file(bak, as_attachment=True, download_name="erp_backup.db")


if __name__ == "__main__":
    # 0.0.0.0：Docker 容器内必须监听全部网卡，宿主机端口映射才能进得来
    app.run(host="0.0.0.0", port=5000, debug=False)
