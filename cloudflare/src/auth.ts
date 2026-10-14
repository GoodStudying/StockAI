/**
 * 认证模块：登录 / 登出 / 会话探测 / 密码哈希
 *
 * 密码兼容设计（与 Flask 版互通）：
 * - 存量格式一：werkzeug "pbkdf2:sha256:260000$salt$hexhash" —— Web Crypto 原生可复算
 * - 存量格式二：旧版无盐 SHA-256（64位hex）—— 仅校验（建议登录后改密焕新为 pbkdf2）
 * 会话：JWT(HS256) 存 HttpOnly Cookie，密钥为环境变量 WORKER_SECRET。
 */
import { Hono } from "hono";
import type { Context, MiddlewareHandler } from "hono";
import type { AppEnv } from "./index";
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import { sign, verify } from "hono/jwt";

const COOKIE = "erp_session";      // 会话 Cookie 名（前端不感知具体名字）
const SESSION_HOURS = 7 * 3600;    // 会话有效期 7 小时（秒）

const enc = new TextEncoder();

/** 字节数组转小写 hex 字符串 */
function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 校验密码（兼容 werkzeug pbkdf2 与旧版无盐 sha256 两种存量格式） */
export async function verifyPassword(stored: string, pw: string): Promise<boolean> {
  if (!stored) return false;
  // 旧版无盐 SHA-256：64 位十六进制串
  if (/^[0-9a-f]{64}$/i.test(stored)) {
    return toHex(await crypto.subtle.digest("SHA-256", enc.encode(pw))) === stored.toLowerCase();
  }
  // werkzeug pbkdf2 格式：pbkdf2:sha256:260000$salt$hexhash
  const m = stored.match(/^pbkdf2:sha256:(\d+)\$(.+)\$([0-9a-fA-F]+)$/);
  if (!m) return false;
  const iterations = Number(m[1]);
  if (!Number.isInteger(iterations) || iterations <= 0 || iterations > 2_000_000) return false;
  try {
    const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt: enc.encode(m[2]), iterations }, key, 256);
    return toHex(bits) === m[3].toLowerCase();
  } catch {
    return false;
  }
}

/** 生成密码哈希：与 Flask 版同格式（pbkdf2:sha256:260000$...），两端数据可互迁 */
export async function hashPassword(pw: string): Promise<string> {
  // 16 位安全随机可见 ASCII 盐（字母数字，去掉易混淆字符）
  const ABC = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  const rnd = crypto.getRandomValues(new Uint8Array(16));
  const salt = [...rnd].map((r) => ABC[r % ABC.length]).join("");
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: 260000 }, key, 256);
  return `pbkdf2:sha256:260000$${salt}$${toHex(bits)}`;
}

/** 当前登录用户类型 */
export type SessionUser = { id: number; username: string; role: string };

/** 从 Cookie 解析当前登录用户；未登录返回 null */
export async function currentUser(c: Context<AppEnv>): Promise<SessionUser | null> {
  const token = getCookie(c, COOKIE);
  if (!token) return null;
  try {
    const payload = await verify(token, c.env.WORKER_SECRET);
    if (payload && typeof payload === "object" && typeof (payload as any).id === "number") {
      const { id, username, role } = payload as any;
      return { id, username, role };
    }
  } catch { /* 签名无效/过期视为未登录 */ }
  return null;
}

/** 登录校验中间件（对应 Flask 版 login_required） */
export const requireLogin: MiddlewareHandler<AppEnv> = async (c, next) => {
  const u = await currentUser(c);
  if (!u) return c.json({ error: "未登录" }, 401);
  c.set("user", u);
  await next();
};

/** 管理员校验中间件（对应 Flask 版 admin_required） */
export const requireAdmin: MiddlewareHandler<AppEnv> = async (c, next) => {
  const u = await currentUser(c);
  if (!u) return c.json({ error: "未登录" }, 401);
  if (u.role !== "admin") return c.json({ error: "需要管理员权限" }, 403);
  c.set("user", u);
  await next();
};

/** 统一解析 JSON 请求体（对应 Flask get_json(force=True)，坏 JSON 返回空对象） */
export async function getJson(c: Context<AppEnv>): Promise<Record<string, any>> {
  try { return (await c.req.json()) ?? {}; } catch { return {}; }
}

/** 解析整数入参；非法返回 null（与 Flask 版 _parse_int 一致，避免 NaN 落库） */
export function parseIntSafe(v: unknown, min: number): number | null {
  const n = Number(v);
  if (!Number.isInteger(n) || n < min) return null;
  return n;
}

/** UTC 时间字符串 "YYYY-MM-DD HH:MM:SS"（与 D1 的 CURRENT_TIMESTAMP 同一时区口径） */
export function nowStr(): string {
  return new Date().toISOString().slice(0, 19).replace("T", " ");
}

const auth = new Hono<AppEnv>();

// ---------------- 登录 ----------------
auth.post("/api/login", async (c) => {
  if (!c.env.WORKER_SECRET) {
    return c.json({ error: "服务端未配置 WORKER_SECRET（Worker Settings → Variables and Secrets 添加后重试）" }, 500);
  }
  const d = await getJson(c);
  const uname = String(d.username ?? "").trim();
  const row = await c.env.DB.prepare(
    "SELECT id, username, role, password_hash FROM users WHERE username=?").bind(uname).first<any>();
  if (row && await verifyPassword(row.password_hash, String(d.password ?? ""))) {
    const user = { id: row.id as number, username: row.username as string, role: row.role as string };
    const jwt = await sign({ ...user, exp: Math.floor(Date.now() / 1000) + SESSION_HOURS }, c.env.WORKER_SECRET);
    setCookie(c, COOKIE, jwt, {
      httpOnly: true, secure: true, sameSite: "Lax", path: "/", maxAge: SESSION_HOURS,
    });
    return c.json({ ok: true, user });
  }
  return c.json({ error: "用户名或密码错误" }, 401);
});

// ---------------- 登出 ----------------
auth.post("/api/logout", (c) => {
  deleteCookie(c, COOKIE, { path: "/" });
  return c.json({ ok: true });
});

// ---------------- 当前用户 ----------------
auth.get("/api/me", async (c) => {
  return c.json({ user: await currentUser(c) });
});

// ---------------- 默认密码探测（登录页提醒用） ----------------
auth.get("/api/default_admin_exists", async (c) => {
  const row = await c.env.DB.prepare(
    "SELECT password_hash FROM users WHERE username='admin'").first<any>();
  const isDefault = !!row && await verifyPassword(row.password_hash, "admin123");
  return c.json({ default_exists: isDefault });
});

export default auth;
