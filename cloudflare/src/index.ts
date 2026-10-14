/**
 * 个人ERP - Cloudflare Worker 入口
 *
 * 部署形态：Workers + D1（数据库）+ 静态资源（../static 由 wrangler.toml 的 assets 托管）
 * 请求分流：静态文件命中直接由平台返回，未命中（/api/* 等）进入本 Worker。
 * 路由与响应格式与 Flask 版（app.py）保持 1:1 兼容，前端页面零改动。
 */
import { Hono } from "hono";
import authRoutes from "./auth";
import apiRoutes from "./routes";
import v1Routes from "./apiv1";

/** Worker 绑定资源（wrangler.toml / 网页 Settings 配置） */
export interface Env {
  DB: D1Database;            // D1 数据库（binding 名必须是 DB）
  ASSETS: Fetcher;           // 静态资源（自动注入）
  WORKER_SECRET: string;     // 会话 JWT 签名密钥（Secret，必配）
  API_TOKEN?: string;        // /api/v1 开放接口令牌，多个用英文逗号分隔（可选）
}

/** Hono 环境类型：绑定 + 请求级变量（登录用户 / API 操作人） */
export type AppEnv = {
  Bindings: Env;
  Variables: {
    user?: { id: number; username: string; role: string };
    apiOperator?: string;
  };
};

const app = new Hono<AppEnv>();

// 三组路由：认证 / 网页端业务 API / 开放接口 /api/v1（Cherry Studio 等）
app.route("/", authRoutes);
app.route("/", apiRoutes);
app.route("/", v1Routes);

// 全局兜底：任何未捕获异常一律返回 JSON（与 Flask 版错误格式一致），避免默认 HTML 错误页
app.onError((err, c) => {
  console.error("unhandled:", err);
  const msg = err instanceof Error ? err.message : String(err);
  return c.json({ error: `服务器内部错误：${msg}` }, 500);
});
app.notFound((c) => c.json({ error: "接口不存在" }, 404));

export default app;
