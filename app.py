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
import json
import time
import hmac
import base64
import hashlib
import secrets
import threading
from datetime import datetime
from functools import wraps
import requests as http_req
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
    # 迭代次数 100000：Cloudflare Workers 的 WebCrypto 硬性上限就是 10 万次
    # （超限抛 "iteration counts above 100000 are not supported"），云端本地两端统一 10 万；
    # werkzeug 格式自带迭代数，历史 26 万次的旧哈希在本地 Flask 端仍可正常校验登录
    return generate_password_hash(pw, method="pbkdf2:sha256:100000")


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


def _auto_fetch_material(mid):
    """
    物料自动补全（新建/编辑/AI建料卡片确认 共用）：
    编码为立创C编号（形如 C621395）时抓取详情——
      1) 名称/型号/品牌/封装/分类/描述/商城链接 仅在当前为空时填充（绝不覆盖用户已填内容）
      2) 无图片时下载入库（BLOB）
      3) 无 datasheet 记录时下载登记
    同步执行（个人系统等 2~5s 可接受）；任何失败静默跳过（不阻塞物料创建/编辑）。
    返回 None=无需抓取或全部成功；字符串=失败原因说明（调用方可放进响应 note 提示）。
    """
    rows = db.query(
        "SELECT code, name, model, brand, package, category, description, lcsc_url, "
        "image_path, image_blob FROM materials WHERE id=?", (mid,))
    if not rows:
        return "物料不存在"
    r = rows[0]
    code = (r["code"] or "").strip()
    if not re.fullmatch(r"C\d+", code, re.I):
        return None                      # 不是立创C编号，无需抓取（正常情况）

    # 快速路径：参数全填 + 已有图片 + 已有手册 + 已有商城链接 → 无可补全，跳过网络请求
    has_image = r["image_blob"] is not None or (r["image_path"] and os.path.exists(r["image_path"]))
    has_doc = db.query(
        "SELECT id FROM material_documents WHERE material_id=? AND doc_type='datasheet'", (mid,))
    fields_full = all((r[c] or "").strip() for c in
                      ("name", "model", "brand", "package", "category", "lcsc_url"))
    if fields_full and has_image and has_doc:
        return None

    try:
        detail = lcsc.fetch_detail(code)
    except Exception:
        return "访问立创失败（网络受限），参数未自动补全"
    if not detail:
        return "未获取到立创详情（编号可能无效），参数未自动补全"
    d = detail[0]

    # 1) 参数字段：仅填空不覆盖（★lcsc_url 列对应立创详情里的 "url" 键，做映射）
    _map = {"name": "name", "model": "model", "brand": "brand", "package": "package",
            "category": "category", "description": "description", "lcsc_url": "url"}
    updates, params = [], []
    for col, key in _map.items():
        cur = (r[col] or "").strip() if r[col] else ""
        new = (d.get(key) or "").strip()
        if not cur and new:
            updates.append(f"{col}=?")
            params.append(new)

    # 2) 图片：无 BLOB 且无有效磁盘文件时下载（失败仅记提示，不影响建料）
    notes = []
    if not has_image and d.get("image"):
        img = d["image"]
        if img.startswith("//"):
            img = "https:" + img        # 协议相对地址补全
        img_bytes = lcsc.fetch_image_bytes(img)
        if img_bytes:
            updates.append("image_blob=?")
            params.append(img_bytes)
        else:
            notes.append("图片下载失败")

    if updates:
        params.append(mid)
        db.execute_write(f"UPDATE materials SET {', '.join(updates)} WHERE id=?", tuple(params))

    # 3) 手册：无 datasheet 记录时下载登记（file_blob 存内容）
    if not has_doc and d.get("datasheet_url"):
        pdf_bytes, fname = lcsc.fetch_datasheet_bytes(d["datasheet_url"])
        if pdf_bytes:
            db.execute_write(
                "INSERT INTO material_documents(material_id, doc_type, file_path, file_name, file_blob)"
                " VALUES (?,?,?,?,?)", (mid, "datasheet", "", fname, pdf_bytes))
        else:
            notes.append("手册下载失败")
    return "；".join(notes) if notes else None


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

    # 立创C编号自动补全：图片/手册/参数（仅填空不覆盖；失败静默不阻塞建料）
    note = _auto_fetch_material(last_id)
    resp = {"id": last_id}
    if note:
        resp["note"] = note
    return jsonify(resp), 201


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
    # 后补立创编号/尚有空字段时自动补全（幂等：已全填则快速路径直接跳过）
    note = _auto_fetch_material(mid)
    resp = {"ok": True}
    if note:
        resp["note"] = note
    return jsonify(resp)


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

def _search_core(q, code, limit=20):
    """物料搜索核心：code 精确 或 q 模糊（名称/编码/型号），返回精简字段行列表。
    被 /api/v1（外部Token）、AI 工具 search_materials 共用，避免三处复制查询逻辑。"""
    q = (q or "").strip()
    code = (code or "").strip()
    cols = "id, name, code, model, category, package, brand, quantity, unit, warehouse_id"
    if code:
        return db.query(f"SELECT {cols} FROM materials WHERE code=? LIMIT ?", (code, limit))
    if q:
        like = f"%{q}%"
        return db.query(
            f"SELECT {cols} FROM materials WHERE name LIKE ? OR code LIKE ? OR model LIKE ? "
            f"ORDER BY id DESC LIMIT ?", (like, like, like, limit))
    return None   # 两个参数都为空


@app.route("/api/v1/materials", methods=["GET"])
@token_required
def v1_search_materials():
    """物料搜索：q 模糊匹配 名称/编码/型号；code 精确匹配编码"""
    rows = _search_core(request.args.get("q"), request.args.get("code"), limit=50)
    if rows is None:
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


def _bom_check_core(items):
    """BOM 核对核心：items=[{code/model/name, quantity}]，返回 (summary, results) 或 (None, err_msg)。
    被 /api/v1/bom/check（外部Token）、网页 POST /api/bom/check（会话）、AI 工具 check_bom 共用。"""
    if not isinstance(items, list) or not items:
        return None, "items 必须是非空数组"
    if len(items) > 500:
        return None, "单次最多核对 500 行"
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
    return summary, results


@app.route("/api/v1/bom/check", methods=["POST"])
@token_required
def v1_bom_check():
    """
    BOM 核对。body: {"items": [{"code"/"model"/"name", "quantity"}, ...]}
    逐项匹配物料并对比库存，返回每行状态与汇总：
    ok=库存充足 shortage=缺料(附缺额) not_found=未找到 ambiguous=多个匹配 invalid=入参错误
    """
    summary, results = _bom_check_core(request.get_json(force=True).get("items"))
    if summary is None:
        return jsonify({"error": results}), 400
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
    for t in ("warehouses", "materials", "material_documents", "stock_records", "users",
              "app_settings", "chat_messages", "purchase_items", "purchase_batches"):
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


# =====================================================================
# AI 对话 + 采购清单 模块（2026-10）
# 与云端版 cloudflare/src/routes.ts、ai.ts 1:1 镜像，共用 static/ 前端
# =====================================================================

# ---------------- 系统设置（app_settings key-value） ----------------

def _get_setting(key):
    """读单个设置项，不存在返回空串"""
    rows = db.query("SELECT value FROM app_settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else ""


def _set_setting(key, value):
    """写设置项（UPSERT：SQLite 与 PostgreSQL 均支持 ON CONFLICT 语法）"""
    db.execute_write(
        "INSERT INTO app_settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _mask_key(k):
    """API Key 打码回显：保头 3 位尾 4 位（太短只保头 2 位），防配置页回显泄露"""
    if not k:
        return ""
    if len(k) <= 8:
        return k[:2] + "***"
    return k[:3] + "***" + k[-4:]


def _ai_cfg():
    """读取 AI 接口配置（OpenAI 兼容三件套：base_url / api_key / model）"""
    return {"base_url": _get_setting("ai_base_url").strip(),
            "api_key": _get_setting("ai_api_key").strip(),
            "model": _get_setting("ai_model").strip()}


# ---------------- AI 接口配置（管理员） ----------------

@app.route("/api/ai/config", methods=["GET"])
@admin_required
def ai_config_get():
    """读取 AI 配置：api_key 打码回显（前端显示用），has_key 供判断是否已配置"""
    cfg = _ai_cfg()
    return jsonify({"base_url": cfg["base_url"], "model": cfg["model"],
                    "api_key_masked": _mask_key(cfg["api_key"]),
                    "has_key": bool(cfg["api_key"])})


@app.route("/api/ai/config", methods=["POST"])
@admin_required
def ai_config_set():
    """保存 AI 配置。★ api_key 传空 = 保留原值（防打码回显覆盖清空）；显式传 "!" 清空"""
    d = request.get_json(force=True)
    base_url = (d.get("base_url") or "").strip()
    model = (d.get("model") or "").strip()
    api_key = (d.get("api_key") or "").strip()
    if not base_url:
        return jsonify({"error": "接口地址（base_url）不能为空"}), 400
    if not model:
        return jsonify({"error": "模型名（model）不能为空"}), 400
    _set_setting("ai_base_url", base_url)
    _set_setting("ai_model", model)
    if api_key == "!":
        _set_setting("ai_api_key", "")
    elif api_key:                        # 空 = 保留原值
        _set_setting("ai_api_key", api_key)
    return jsonify({"ok": True})


@app.route("/api/ai/test", methods=["POST"])
@admin_required
def ai_test():
    """测试 AI 连通性：向 {base_url}/chat/completions 发一条最小请求，返回耗时。
    body 里缺的字段回退用已存配置（配置页"测试连接"按钮直接传表单当前值）"""
    d = request.get_json(force=True)
    saved = _ai_cfg()
    base_url = (d.get("base_url") or "").strip() or saved["base_url"]
    api_key = (d.get("api_key") or "").strip() or saved["api_key"]
    model = (d.get("model") or "").strip() or saved["model"]
    if not (base_url and api_key and model):
        return jsonify({"error": "请先填写完整的 接口地址/API Key/模型名"}), 400
    t0 = time.time()
    try:
        r = http_req.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 5},
            timeout=(10, 30))
    except Exception as e:
        return jsonify({"error": f"连接失败：{type(e).__name__}（检查地址/网络/代理）"}), 502
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        # 常见错误透传：401=key错 404=地址错(缺/v1?) 429=限流
        return jsonify({"error": f"HTTP {r.status_code}：{r.text[:200]}"}), 502
    return jsonify({"ok": True, "ms": ms})


@app.route("/api/ai/status", methods=["GET"])
@login_required
def ai_status():
    """聊天页判断 AI 是否已配置可用（不打码也不泄露 key 本体）"""
    cfg = _ai_cfg()
    ok = bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])
    return jsonify({"configured": ok, "model": cfg["model"] if ok else ""})


# ---------------- 采购清单 ----------------

def _purchase_bulk_core(project, items, operator):
    """
    采购批量导入核心（BOM 一键导入 & AI add_purchase_items 卡片共用）。
    合并规则：同项目下命中已有条目（material_id 相同，或 code 非空且相同）→ need_qty 累加，
    否则新建。返回 (created, merged, err)，err 非 None 时前两个值无效。
    """
    project = (project or "").strip()
    if not project:
        return 0, 0, "项目名不能为空"
    if not isinstance(items, list) or not items:
        return 0, 0, "items 必须是非空数组"
    if len(items) > 200:
        return 0, 0, "单次最多导入 200 行"
    created = merged = 0
    for it in items:
        if not isinstance(it, dict):
            return 0, 0, "items 内每行必须是对象"
        name = (it.get("name") or "").strip()
        if not name:
            return 0, 0, "每行的 name 不能为空"
        qty = _parse_int(it.get("need_qty"), 1)
        if qty is None:
            return 0, 0, f"「{name}」的 need_qty 必须是大于0的整数"
        mid = _parse_int(it.get("material_id") or 0, 1)
        code = (it.get("code") or "").strip()
        # 合并查找：material_id 优先，其次 code（code 非空才参与合并，防把不同无码物料合并）
        hit = None
        if mid:
            rows = db.query("SELECT id FROM purchase_items WHERE project=? AND material_id=?",
                            (project, mid))
            hit = rows[0]["id"] if rows else None
        elif code:
            rows = db.query("SELECT id FROM purchase_items WHERE project=? AND code=?",
                            (project, code))
            hit = rows[0]["id"] if rows else None
        if hit:
            db.execute_write("UPDATE purchase_items SET need_qty=need_qty+? WHERE id=?", (qty, hit))
            merged += 1
        else:
            db.execute_write(
                "INSERT INTO purchase_items(project, material_id, name, code, model, "
                "need_qty, unit, remark, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (project, mid or None, name, code, (it.get("model") or "").strip(),
                 qty, (it.get("unit") or "个").strip() or "个",
                 (it.get("remark") or "").strip(), operator))
            created += 1
    return created, merged, None


def _purchase_list_core(project=None, status=None):
    """
    采购条目查询核心：LEFT JOIN 批次 SUM 派生 purchased（已购）/status（open/partial/done）。
    ★ 不存 status 列——删批次后状态自动回退，永不与批次表脱节。
    """
    sql = """SELECT p.*, COALESCE(b.purchased, 0) AS purchased,
                    CASE WHEN COALESCE(b.purchased, 0) >= p.need_qty THEN 'done'
                         WHEN COALESCE(b.purchased, 0) > 0 THEN 'partial'
                         ELSE 'open' END AS status
             FROM purchase_items p
             LEFT JOIN (SELECT purchase_item_id, SUM(qty) AS purchased
                        FROM purchase_batches GROUP BY purchase_item_id) b
               ON b.purchase_item_id = p.id"""
    params = []
    if project:
        sql += " WHERE p.project=?"
        params.append(project)
    sql += " ORDER BY p.id DESC LIMIT 500"
    rows = db.query(sql, tuple(params))
    if status:                           # 派生列的过滤放 Python 层（SQL 兼容两端）
        rows = [r for r in rows if r["status"] == status]
    return rows


@app.route("/api/purchase/items", methods=["GET"])
@login_required
def purchase_items_list():
    """采购清单：?project= 按项目过滤，?status=open|partial|done 按状态过滤"""
    return jsonify(_purchase_list_core((request.args.get("project") or "").strip() or None,
                                       (request.args.get("status") or "").strip() or None))


@app.route("/api/purchase/items", methods=["POST"])
@login_required
def purchase_items_add():
    """新增采购条目（支持批量，BOM 一键导入同用此接口）。
    body: {project, items:[{material_id?, name, code?, model?, need_qty, unit?, remark?}]}"""
    d = request.get_json(force=True)
    created, merged, err = _purchase_bulk_core(
        d.get("project"), d.get("items"), session["user"]["username"])
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"created": created, "merged": merged}), 201


@app.route("/api/purchase/items/<int:pid>", methods=["PUT"])
@login_required
def purchase_item_update(pid):
    """编辑采购条目（项目/数量/名称等；改 code 可能影响后续合并归属，属预期行为）"""
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "名称不能为空"}), 400
    qty = _parse_int(d.get("need_qty"), 1)
    if qty is None:
        return jsonify({"error": "需求量必须是大于0的整数"}), 400
    _, n = db.execute_write(
        "UPDATE purchase_items SET project=?, name=?, code=?, model=?, need_qty=?, unit=?, "
        "remark=? WHERE id=?",
        ((d.get("project") or "").strip(), name, (d.get("code") or "").strip(),
         (d.get("model") or "").strip(), qty, (d.get("unit") or "个").strip() or "个",
         (d.get("remark") or "").strip(), pid))
    if n == 0:
        return jsonify({"error": "条目不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/purchase/items/<int:pid>", methods=["DELETE"])
@login_required
def purchase_item_delete(pid):
    """删除采购条目（级联删除其全部批次）"""
    _, n = db.execute_write("DELETE FROM purchase_items WHERE id=?", (pid,))
    if n == 0:
        return jsonify({"error": "条目不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/purchase/items/<int:pid>/batches", methods=["POST"])
@login_required
def purchase_batch_add(pid):
    """登记一次采购（同一物料分多次采购，每笔一条）：body {qty, remark}"""
    if not db.query("SELECT id FROM purchase_items WHERE id=?", (pid,)):
        return jsonify({"error": "采购条目不存在"}), 404
    d = request.get_json(force=True)
    qty = _parse_int(d.get("qty"), 1)
    if qty is None:
        return jsonify({"error": "数量必须是大于0的整数"}), 400
    last_id, _ = db.execute_write(
        "INSERT INTO purchase_batches(purchase_item_id, qty, remark, created_by) VALUES (?,?,?,?)",
        (pid, qty, (d.get("remark") or "").strip(), session["user"]["username"]))
    return jsonify({"id": last_id}), 201


@app.route("/api/purchase/items/<int:pid>/batches", methods=["GET"])
@login_required
def purchase_batches_list(pid):
    """查某采购条目的全部采购批次（前端行展开用）"""
    if not db.query("SELECT id FROM purchase_items WHERE id=?", (pid,)):
        return jsonify({"error": "采购条目不存在"}), 404
    return jsonify(db.query(
        "SELECT * FROM purchase_batches WHERE purchase_item_id=? ORDER BY id DESC", (pid,)))


@app.route("/api/purchase/batches/<int:bid>", methods=["DELETE"])
@login_required
def purchase_batch_delete(bid):
    """删除一笔采购批次（录错时用；已购数量自动回退，状态随之重算）"""
    _, n = db.execute_write("DELETE FROM purchase_batches WHERE id=?", (bid,))
    if n == 0:
        return jsonify({"error": "批次不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/purchase/projects", methods=["GET"])
@login_required
def purchase_projects():
    """项目聚合视图：条数/已完成数/需求与已购总量（采购页左侧项目列表）"""
    rows = db.query(
        """SELECT p.project,
                  COUNT(*) AS item_count,
                  SUM(CASE WHEN COALESCE((SELECT SUM(qty) FROM purchase_batches
                            WHERE purchase_item_id = p.id), 0) >= p.need_qty THEN 1 ELSE 0 END)
                    AS done_count,
                  SUM(p.need_qty) AS need_total,
                  COALESCE(SUM(COALESCE((SELECT SUM(qty) FROM purchase_batches
                            WHERE purchase_item_id = p.id), 0)), 0) AS purchased_total,
                  MAX(p.id) AS last_id
           FROM purchase_items p GROUP BY p.project ORDER BY last_id DESC""")
    return jsonify(rows)


# ---------------- 网页端 BOM 核对（session 认证；与 /api/v1 版同核心） ----------------

@app.route("/api/bom/check", methods=["POST"])
@login_required
def bom_check_web():
    """BOM 核对网页入口：body {items:[{code|model|name, quantity}]}"""
    summary, results = _bom_check_core(request.get_json(force=True).get("items"))
    if summary is None:
        return jsonify({"error": results}), 400
    return jsonify({"summary": summary, "results": results})


# ---------------- AI 对话（OpenAI 兼容协议：Kimi / DeepSeek / GLM 等） ----------------

# tool loop 双保险上限：LLM 轮次 与 工具调用总数 都封顶，防模型死循环刷调用
MAX_LLM_ROUNDS = 8
MAX_TOOL_CALLS = 12


def _llm_chat(cfg, messages, tools=None):
    """
    OpenAI 兼容 /chat/completions 单一收敛调用点（Kimi/DeepSeek/GLM 等）。
    返回 (content, tool_calls)。所有 LLM 交互都走这里——冒烟测试 monkeypatch 本函数
    即可脚本化整个对话流程，无需真实 API key。
    """
    payload = {"model": cfg["model"], "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    r = http_req.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
        json=payload, timeout=(10, 90))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    msg = r.json()["choices"][0]["message"]
    return msg.get("content") or "", msg.get("tool_calls") or []


def _system_prompt(username):
    """系统提示词：角色 + 工具规范 + 安全模式说明 + 电子领域常识 + 回复风格"""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""你是「个人ERP」的库存管理 AI 助手。当前用户：{username}；今天：{today}。

【职责】帮助用户查询物料库存、办理出入库、核对 BOM、管理采购清单。

【工具使用规范】
1. 出入库前必须先用 search_materials 找到物料，material_id 只能来自搜索结果，禁止编造。
2. stock_in / stock_out / create_material / add_purchase_items 是写操作：调用后只会生成"待确认卡片"，并未真正执行。你必须向用户清晰复述操作内容（物料、数量、仓库），并提醒用户点击卡片上的「确认执行」按钮；用户没确认就当作未执行。
3. 查仓库列表用 list_warehouses；信息不足（数量/仓库/编码）时先问清再动手，禁止猜测执行。
4. 库存中搜不到的物料：用 lcsc_search 搜立创商城（结果含立创C编号），把候选列成简表让用户选；用户确认编号后用 create_material 建料（填了 C 编号会自动抓取图片/手册/参数，无需用户手填），再按需办理入库。
5. BOM 核对用 check_bom；用户想采购缺料时用 add_purchase_items（问清项目名，BOM 缺料可批量导入）。

【电子领域常识】
- 电容容值换算：1µF = 1000nF（0.1µF = 100nF；三位数标注 104 = 100nF、105 = 1µF）。用户给出的不同写法若等值，主动指出并确认是否同一种物料。
- 电阻：10k = 10kΩ = 10000Ω；贴片三位数标注 103 = 10kΩ。
- 封装 0402/0603/0805/1206 是尺寸规格；同容值不同封装/耐压/精度是不同物料，用户未说明时要问清。

【回复风格】简体中文；简洁分点，必要时用小表格；涉及写操作的回复末尾必须提醒用户确认卡片。"""


# 9 个工具的 OpenAI function calling 声明（只读 5 个真执行 + 写 4 个转卡片）
AI_TOOLS = [
    {"type": "function", "function": {
        "name": "search_materials",
        "description": "搜索库存中的物料：code 精确匹配编码，或 q 模糊匹配名称/编码/型号（至少提供一个）。出入库/BOM 核对前先用它拿到 material_id。",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "模糊关键词（匹配名称/编码/型号）"},
            "code": {"type": "string", "description": "物料编码精确匹配（如立创C编号 C621395）"}}}}},
    {"type": "function", "function": {
        "name": "list_warehouses",
        "description": "列出全部仓库（id/名称/层级/父库），入库前用它确认仓库 id。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "lcsc_search",
        "description": "搜索立创商城（库存没有的物料来这里找候选），返回立创C编号/型号/品牌/封装/库存/价格。用户选定 C 编号后可用 create_material 建料并自动补全参数。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词，如 '100nF 0603 电容'"}},
            "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "check_bom",
        "description": "BOM 核对：逐项匹配库存并比对数量，返回每行状态（ok充足/shortage缺料/not_found未找到/ambiguous多个匹配）。",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "description": "BOM 行数组", "items": {"type": "object", "properties": {
                "code": {"type": "string"}, "model": {"type": "string"},
                "name": {"type": "string"},
                "quantity": {"type": "integer", "description": "需求数量，默认1"}}}}}}}},
    {"type": "function", "function": {
        "name": "query_purchase",
        "description": "查询采购清单（可按项目过滤），返回每条的需求量/已购量/状态（open未采/partial部分/done完成）。",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string", "description": "项目名（空=查全部）"}}}}},
    {"type": "function", "function": {
        "name": "create_material",
        "description": "新建物料（写操作，生成待确认卡片）。填了立创C编号会自动抓取图片/数据手册/参数。",
        "parameters": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string", "description": "物料名称"},
            "code": {"type": "string", "description": "立创C编号（强烈建议填，自动补全参数）"},
            "model": {"type": "string", "description": "型号"},
            "category": {"type": "string"}, "package": {"type": "string"},
            "brand": {"type": "string"}, "unit": {"type": "string"},
            "warehouse_id": {"type": "integer", "description": "仓库 id（list_warehouses 查）"},
            "quantity": {"type": "integer", "description": "初始库存，默认0"},
            "description": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "stock_in",
        "description": "入库（写操作，生成待确认卡片）。material_id 必须来自 search_materials 结果。",
        "parameters": {"type": "object", "required": ["material_id", "quantity"], "properties": {
            "material_id": {"type": "integer"}, "quantity": {"type": "integer", "description": "数量>0"},
            "remark": {"type": "string", "description": "备注（可选）"}}}}},
    {"type": "function", "function": {
        "name": "stock_out",
        "description": "出库（写操作，生成待确认卡片）。material_id 必须来自 search_materials 结果；出库前确认库存充足。",
        "parameters": {"type": "object", "required": ["material_id", "quantity"], "properties": {
            "material_id": {"type": "integer"}, "quantity": {"type": "integer", "description": "数量>0"},
            "remark": {"type": "string", "description": "备注（可选）"}}}}},
    {"type": "function", "function": {
        "name": "add_purchase_items",
        "description": "向采购清单批量添加条目（写操作，生成待确认卡片）。同项目同物料自动合并累加需求量。BOM 缺料批量采购用这个工具。",
        "parameters": {"type": "object", "required": ["project", "items"], "properties": {
            "project": {"type": "string", "description": "项目名（归档用）"},
            "items": {"type": "array", "items": {"type": "object", "required": ["name", "need_qty"], "properties": {
                "material_id": {"type": "integer", "description": "库存物料 id（有则填）"},
                "name": {"type": "string"}, "code": {"type": "string"},
                "model": {"type": "string"},
                "need_qty": {"type": "integer", "description": "需求数量>0"},
                "unit": {"type": "string"}, "remark": {"type": "string"}}}}}}}},
]

# 写工具的固定回话：驱动模型产出面向用户的确认文案，而不是继续调工具
_ACTION_NOTE = ("已生成待确认操作卡片（用户点击「确认执行」前不会执行任何写操作）。"
                "请向用户复述操作内容（对象/数量/仓库），并提醒用户在前端卡片上点击确认。不要重复调用本工具。")


def _build_action(name, args):
    """
    写工具 → 待确认操作卡片。卡片 title 由服务端从参数生成（不信任 AI 的展示文案）。
    返回 (action, err)：action 为 None 时 err 是给模型自纠的参数错误说明。
    """
    base = {"id": secrets.token_hex(4), "type": name, "params": args,
            "status": "pending", "result": None, "executed_at": None}
    if name == "create_material":
        mname = (args.get("name") or "").strip()
        if not mname:
            return None, "name 不能为空"
        qty = _parse_int(args.get("quantity") or 0, 0)
        if qty is None:
            return None, "quantity 必须是不小于0的整数"
        title = f"新建物料：{mname}"
        code = (args.get("code") or "").strip()
        if code:
            title += f"（{code}）"
        if qty > 0:
            title += f"，初始库存 ×{qty}"
        base["title"] = title
        return base, None
    if name in ("stock_in", "stock_out"):
        mid = _parse_int(args.get("material_id"), 1)
        qty = _parse_int(args.get("quantity"), 1)
        if mid is None or qty is None:
            return None, "material_id / quantity 必须是大于0的整数"
        mat = db.query("SELECT name FROM materials WHERE id=?", (mid,))
        if not mat:
            return None, f"物料 #{mid} 不存在，请先通过 search_materials 获取真实 material_id"
        base["title"] = f"{'入库' if name == 'stock_in' else '出库'}：{mat[0]['name']}（#{mid}）×{qty}"
        return base, None
    if name == "add_purchase_items":
        project = (args.get("project") or "").strip()
        items = args.get("items")
        if not project:
            return None, "project（项目名）不能为空"
        if not isinstance(items, list) or not items:
            return None, "items 必须是非空数组"
        base["title"] = f"导入采购清单：项目「{project}」{len(items)} 项"
        return base, None
    return None, f"未知工具 {name}"


def _run_tool(name, args, username):
    """
    执行一次 AI 工具调用。返回 (给模型的结果 dict, 待确认卡片 dict 或 None)。
    只读工具（搜索/列表/BOM/采购查询）真执行返回数据；
    写工具一律拦截转卡片，绝不直接落库。参数问题返回 {"error":...} 供模型自纠。
    """
    if name == "search_materials":
        rows = _search_core(args.get("q"), args.get("code"), limit=20)
        if rows is None:
            return {"error": "q 与 code 至少提供一个"}, None
        return {"count": len(rows), "materials": rows}, None
    if name == "list_warehouses":
        rows = db.query("SELECT id, name, parent_id, level FROM warehouses ORDER BY level, id")
        return {"count": len(rows), "warehouses": rows}, None
    if name == "lcsc_search":
        kw = (args.get("keyword") or "").strip()
        if not kw:
            return {"error": "keyword 不能为空"}, None
        try:
            rows = lcsc.search_lcsc(kw)
        except Exception:
            rows = []
        slim = [{k: r.get(k, "") for k in ("code", "model", "name", "brand",
                                           "package", "stock", "price")} for r in rows]
        if not slim:
            return {"count": 0, "items": [],
                    "note": "立创搜索无结果（可能被风控或网络受限），建议用户提供立创C编号"}, None
        return {"count": len(slim), "items": slim}, None
    if name == "check_bom":
        summary, results = _bom_check_core(args.get("items"))
        if summary is None:
            return {"error": results}, None
        if summary["total"] > 30:        # token 经济：超 30 行只回汇总 + 非 ok 行
            bad = [r for r in results if r.get("status") != "ok"]
            return {"summary": summary, "results": bad,
                    "note": f"共 {summary['total']} 行，超出 30 行仅返回非 ok 行（{len(bad)} 条）"}, None
        return {"summary": summary, "results": results}, None
    if name == "query_purchase":
        rows = _purchase_list_core((args.get("project") or "").strip() or None, None)
        slim = [{k: r.get(k) for k in ("id", "project", "name", "code", "model",
                                       "need_qty", "purchased", "unit", "status")} for r in rows]
        return {"count": len(slim), "items": slim}, None
    # ---------- 写操作：拦截转"待确认卡片" ----------
    action, err = _build_action(name, args)
    if err:
        return {"error": err}, None
    return {"ok": True, "action_id": action["id"], "note": _ACTION_NOTE}, action


def _tool_msg(tc_id, result):
    """构造 tool 角色消息（OpenAI 协议）：结果序列化为 JSON 文本回给模型"""
    return {"role": "tool", "tool_call_id": tc_id,
            "content": json.dumps(result, ensure_ascii=False, default=str)}


@app.route("/api/chat", methods=["POST"])
@login_required
def chat_send():
    """
    AI 对话主入口。流程：存用户消息 → 组装上下文（system + 最近20条历史降级为纯文本）
    → tool loop（只读工具真执行；写工具转卡片）→ 存 assistant 消息与卡片 → 返回。
    """
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()
    if not message:
        return jsonify({"error": "消息不能为空"}), 400
    cfg = _ai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return jsonify({"error": "AI 接口未配置，请联系管理员在「系统管理」页配置"}), 400
    u = session["user"]
    uid = u["id"]

    # 1) 落库用户消息（先落库再查历史，本轮 user 消息自然包含在上下文里）
    db.execute_write("INSERT INTO chat_messages(user_id, role, content) VALUES (?,?,?)",
                     (uid, "user", message))

    # 2) 组装上下文。★历史重放必须降级：数据库里没有 tool_calls 结构，
    #   assistant 消息的卡片以文字摘要拼进 content，避免发给模型缺 tool 消息导致 400
    history = db.query(
        "SELECT role, content, actions_json FROM chat_messages "
        "WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))[::-1]
    msgs = [{"role": "system", "content": _system_prompt(u["username"])}]
    for h in history:
        content = h["content"] or ""
        if h["actions_json"]:
            try:
                for a in json.loads(h["actions_json"]):
                    res = a.get("result") or {}
                    tail = f"（结果：{res.get('error') or res.get('quantity') or res.get('material_id') or '完成'}）"
                    content += f"\n[操作卡片 {a.get('status')}: {a.get('title')}]{tail}"
            except (ValueError, TypeError):
                pass
        msgs.append({"role": h["role"], "content": content})

    # 3) tool loop：模型给纯文本即结束；给 tool_calls 则逐个执行后继续
    actions = []
    tool_count = 0
    content = ""
    for _ in range(MAX_LLM_ROUNDS):
        try:
            content, tool_calls = _llm_chat(cfg, msgs, AI_TOOLS)
        except Exception as e:
            # LLM 失败也落一条 assistant 消息，保证历史连续可追溯
            db.execute_write("INSERT INTO chat_messages(user_id, role, content) VALUES (?,?,?)",
                             (uid, "assistant", f"[AI调用失败] {type(e).__name__}: {e}"))
            return jsonify({"error": f"AI 调用失败：{type(e).__name__}: {e}"}), 502
        if not tool_calls:
            break                       # 模型给出最终文本，正常出口
        msgs.append({"role": "assistant", "content": content or None, "tool_calls": tool_calls})
        for tc in tool_calls:
            tool_count += 1
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except (ValueError, TypeError):
                msgs.append(_tool_msg(tc.get("id"), {"error": "arguments 必须是合法的 JSON 对象"}))
                continue
            if tool_count > MAX_TOOL_CALLS:
                msgs.append(_tool_msg(tc.get("id"),
                                      {"error": "已达本轮工具调用上限，请直接用现有信息回答用户"}))
                continue
            result, action = _run_tool(name, args, u["username"])
            if action:
                actions.append(action)
            msgs.append(_tool_msg(tc.get("id"), result))
    else:
        # 8 轮用尽模型仍在要工具：追加一次不带 tools 的请求逼出文本总结
        try:
            content, _ = _llm_chat(cfg, msgs, None)
        except Exception:
            content = content or "（本轮工具调用已达上限，未生成总结。请换种问法或缩小范围后重试。）"

    # 4) 落库 assistant 消息 + 待确认卡片
    actions_json = json.dumps(actions, ensure_ascii=False, default=str) if actions else None
    aid, _ = db.execute_write(
        "INSERT INTO chat_messages(user_id, role, content, actions_json) VALUES (?,?,?,?)",
        (uid, "assistant", content or "", actions_json))
    return jsonify({"message_id": aid, "content": content or "", "actions": actions})


@app.route("/api/chat/history", methods=["GET"])
@login_required
def chat_history():
    """游标分页加载历史：?before_id= 取更早消息，?limit= 默认30（旧→新顺序返回）"""
    uid = session["user"]["id"]
    before = _parse_int(request.args.get("before_id") or 0, 1)
    limit = _parse_int(request.args.get("limit") or 30, 1) or 30
    limit = min(limit, 100)
    if before:
        rows = db.query("SELECT id, role, content, actions_json, created_at FROM chat_messages "
                        "WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?", (uid, before, limit))
    else:
        rows = db.query("SELECT id, role, content, actions_json, created_at FROM chat_messages "
                        "WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))
    out = []
    for r in reversed(rows):            # 反转成 旧→新，前端按序渲染
        item = {"id": r["id"], "role": r["role"], "content": r["content"],
                "created_at": r["created_at"]}
        try:
            item["actions"] = json.loads(r["actions_json"]) if r["actions_json"] else []
        except (ValueError, TypeError):
            item["actions"] = []
        out.append(item)
    return jsonify({"messages": out, "has_more": len(rows) == limit})


@app.route("/api/chat/history", methods=["DELETE"])
@login_required
def chat_history_clear():
    """清空当前用户的对话记录（只删自己的，管理员也不能删别人的）"""
    db.execute_write("DELETE FROM chat_messages WHERE user_id=?", (session["user"]["id"],))
    return jsonify({"ok": True})


def _execute_action(act, username):
    """
    执行一张已确认的操作卡片（operator 留痕为 ai:用户名）。
    返回结果 dict；出错时带 error 字段，keep_pending=True（库存不足）表示保持待确认可重试。
    """
    t = act.get("type")
    p = act.get("params") or {}
    operator = f"ai:{username}"
    if t in ("stock_in", "stock_out"):
        mid = _parse_int(p.get("material_id"), 1)
        qty = _parse_int(p.get("quantity"), 1)
        if mid is None or qty is None:
            return {"error": "参数无效（material_id / quantity）"}
        new_qty, err = _apply_stock("in" if t == "stock_in" else "out", mid, qty, operator,
                                    (p.get("remark") or "").strip() or "AI 对话确认执行")
        if err:
            # 库存不足(400)：保持 pending，补货后可重试；物料不存在(404)等永久失败置 failed
            return {"error": err[0], "keep_pending": err[1] == 400}
        return {"material_id": mid, "quantity": new_qty}
    if t == "create_material":
        mname = (p.get("name") or "").strip()
        if not mname:
            return {"error": "name 缺失"}
        qty = _parse_int(p.get("quantity") or 0, 0)
        if qty is None:
            qty = 0
        wid = _parse_int(p.get("warehouse_id") or 0, 1)
        new_id, _ = db.execute_write(
            """INSERT INTO materials(name, code, model, category, package, brand,
                                     description, quantity, unit, warehouse_id, lcsc_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mname, (p.get("code") or "").strip(), (p.get("model") or "").strip(),
             (p.get("category") or "").strip(), (p.get("package") or "").strip(),
             (p.get("brand") or "").strip(), (p.get("description") or "").strip(),
             qty, (p.get("unit") or "个").strip() or "个", wid or None, ""))
        note = _auto_fetch_material(new_id)       # 立创C编号自动抓图/手册/参数（失败静默）
        res = {"material_id": new_id}
        if note:
            res["note"] = note
        return res
    if t == "add_purchase_items":
        created, merged, err = _purchase_bulk_core(p.get("project"), p.get("items"), operator)
        if err:
            return {"error": err}
        return {"created": created, "merged": merged}
    return {"error": f"未知操作类型 {t}"}


@app.route("/api/chat/execute", methods=["POST"])
@login_required
def chat_execute():
    """
    确认/取消 AI 生成的操作卡片。body: {message_id, action_id, cancel?}
    幂等状态机：pending → executed / cancelled / failed；非 pending 一律 409（防双击重放）。
    """
    d = request.get_json(force=True)
    msg_id = _parse_int(d.get("message_id"), 1)
    action_id = (d.get("action_id") or "").strip()
    if not msg_id or not action_id:
        return jsonify({"error": "message_id / action_id 不能为空"}), 400
    uid = session["user"]["id"]
    rows = db.query("SELECT user_id, actions_json FROM chat_messages WHERE id=?", (msg_id,))
    if not rows or rows[0]["user_id"] != uid:
        return jsonify({"error": "消息不存在或不属于当前用户"}), 403
    try:
        acts = json.loads(rows[0]["actions_json"] or "[]")
    except (ValueError, TypeError):
        acts = []
    act = next((a for a in acts if a.get("id") == action_id), None)
    if not act:
        return jsonify({"error": "操作卡片不存在"}), 404
    if act.get("status") != "pending":
        return jsonify({"error": f"该卡片已处理（状态：{act.get('status')}），不可重复操作"}), 409
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if d.get("cancel"):
        act.update(status="cancelled", executed_at=now)
    else:
        result = _execute_action(act, session["user"]["username"])
        act["result"] = result
        if result.get("error") and result.get("keep_pending"):
            # 库存不足：保持 pending（补货后可再点确认），不推进状态机
            db.execute_write("UPDATE chat_messages SET actions_json=? WHERE id=?",
                             (json.dumps(acts, ensure_ascii=False, default=str), msg_id))
            return jsonify({"error": result["error"], "action": act}), 400
        act["status"] = "failed" if result.get("error") else "executed"
        act["executed_at"] = now
    db.execute_write("UPDATE chat_messages SET actions_json=? WHERE id=?",
                     (json.dumps(acts, ensure_ascii=False, default=str), msg_id))
    return jsonify({"ok": True, "action": act})


if __name__ == "__main__":
    # 0.0.0.0：Docker 容器内必须监听全部网卡，宿主机端口映射才能进得来
    app.run(host="0.0.0.0", port=5000, debug=False)
