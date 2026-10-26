# -*- coding: utf-8 -*-
"""
个人ERP MCP 服务器（供 Cherry Studio 等 MCP 客户端以对话方式管理库存）

作用：把本项目的 /api/v1 开放接口包装成 MCP 工具，AI 客户端连上后即可
     用自然语言完成：查物料 / 出入库（可挂仓库）/ 查仓库 / 建仓库 /
     删除物料 / BOM 核对 / 重复物料核对。

配置完成后在对话里启用该 MCP 服务器，即可直接说：
  「帮我查一下 0603 100nF 电容的库存」
  「把 C621395 出库 5 个，备注：发样板」
  「入库 100 个 C621395，挂到仓库 元器件盒1」
  「建三个仓库：元器件盒1、元器件盒2、样品管盒」
  「把 RS1ML 这个物料删掉」（会先复述库存征得确认）
  「核对这份 BOM：STM32F103C8T6 x2、0603 10K 电阻 x20」

前置条件：
  1. ERP 服务已启动（本地 python app.py，或 NAS Docker 部署）
  2. ERP 的 .env 中已配置 API_TOKEN=xxx 并重启生效
  3. 本机安装 MCP SDK：pip install "mcp[cli]"  （requests 本项目已带）

Cherry Studio 配置（设置 → MCP 服务器 → 新建）：
  类型/协议：标准输入输出（stdio）
  命令：python
  参数：<本项目路径>/mcp_server.py
  环境变量：
    ERP_API_BASE=http://127.0.0.1:5000    （NAS 部署则填 http://NAS_IP:端口；上云后填 https://你的域名）
    ERP_API_TOKEN=<与 ERP 服务端 .env 中 API_TOKEN 完全一致>
"""
import json
import os

import requests
from mcp.server.fastmcp import FastMCP

# ERP 服务地址与令牌（从环境变量读取，避免硬编码泄露）
BASE = os.environ.get("ERP_API_BASE", "http://127.0.0.1:5000").rstrip("/")
TOKEN = os.environ.get("ERP_API_TOKEN", "")

mcp = FastMCP("personal-erp")


def _call(method: str, path: str, params: dict = None, body: dict = None):
    """
    统一请求 /api/v1 接口。
    任何错误（服务未启动 / token 无效 / 业务失败）都以 RuntimeError 抛出，
    MCP 框架会把异常文本回给 AI，AI 再用自然语言向用户解释原因。
    """
    if not TOKEN:
        raise RuntimeError("未配置 ERP_API_TOKEN 环境变量（请在 Cherry Studio 的 MCP 配置中填写）")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        r = requests.request(method, BASE + path, params=params, json=body,
                             headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"无法连接 ERP 服务（{BASE}）：{e}。请确认服务已启动、地址正确")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"ERP 服务返回异常（HTTP {r.status_code}，非 JSON 响应）")
    if r.status_code >= 400:
        raise RuntimeError(data.get("error") or f"HTTP {r.status_code}")
    return data


def _d(obj) -> str:
    """dict/list 序列化为紧凑中文 JSON，作为工具返回文本"""
    return json.dumps(obj, ensure_ascii=False)


@mcp.tool()
def search_materials(keyword: str = "", code: str = "") -> str:
    """搜索库存物料。keyword=按名称/编码/型号模糊搜索；code=按物料编码精确查询（二选一）。
    返回匹配列表（含 id、名称、编码、型号、库存、单位）。后续出入库操作需要用返回中的 id。"""
    if not keyword and not code:
        raise RuntimeError("请提供 keyword（模糊搜索）或 code（精确编码）至少一个参数")
    params = {"code": code} if code else {"q": keyword}
    return _d(_call("GET", "/api/v1/materials", params=params))


@mcp.tool()
def stock_in(material_id: int, quantity: int, remark: str = "",
             warehouse_id: int = 0, warehouse_name: str = "") -> str:
    """入库：给指定物料增加库存。material_id 来自 search_materials 的查询结果；
    quantity 必须为正整数；remark 为操作备注（如采购单号）。
    挂库（warehouse_id 与 warehouse_name 二选一，仅入库支持）：
      warehouse_id=已知仓库 id；warehouse_name=仓库名（如"纸盒1"，一级库不存在则自动创建，
      已存在则直接复用不会重复建）。用户说"入到/挂到 某盒子/货架"时用 warehouse_name 最省事。
    ⚠ 写操作：执行前应先向用户复述物料名称与数量，征得确认后再调用。返回入库后的最新库存。"""
    body = {"material_id": material_id, "quantity": quantity,
            "remark": remark or "Cherry Studio 对话入库"}
    if warehouse_id:
        body["warehouse_id"] = warehouse_id
    elif warehouse_name:
        body["warehouse_name"] = warehouse_name
    return _d(_call("POST", "/api/v1/stock/in", body=body))


@mcp.tool()
def stock_out(material_id: int, quantity: int, remark: str = "") -> str:
    """出库：从指定物料扣减库存（库存不足会被拒绝，不会扣成负数）。
    material_id 来自 search_materials；quantity 为正整数；remark 为备注（如用途、领用人）。
    ⚠ 写操作：执行前应先向用户复述物料名称与数量，征得确认后再调用。"""
    return _d(_call("POST", "/api/v1/stock/out",
                    body={"material_id": material_id, "quantity": quantity,
                          "remark": remark or "Cherry Studio 对话出库"}))


@mcp.tool()
def get_stock_records(material_id: int = 0) -> str:
    """查询最近 500 条出入库流水。material_id 可选（0 或不传=全部物料）。
    每条含 时间/类型(in入库 out出库)/数量/操作人/备注。"""
    params = {"material_id": material_id} if material_id else None
    return _d(_call("GET", "/api/v1/stock/records", params=params))


@mcp.tool()
def check_bom(items_json: str) -> str:
    """BOM 缺料核对：传入 JSON 数组字符串，每个元素 {"code"/"model"/"name", "quantity"}。
    匹配优先级：编码精确 > 型号精确 > 名称精确 > 名称模糊；逐行返回
    ok(库存充足)/shortage(缺料,附缺额)/not_found(未入库)/ambiguous(多个匹配需改用编码)，
    以及汇总统计。示例：'[{"code":"C621395","quantity":2},{"name":"10K电阻","quantity":20}]'"""
    try:
        items = json.loads(items_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"items_json 不是合法 JSON：{e}")
    return _d(_call("POST", "/api/v1/bom/check", body={"items": items}))


@mcp.tool()
def find_duplicates() -> str:
    """核对重复录入的物料：按 编码/型号/名称 三个维度分组列出所有出现多次的物料，
    供人工确认后合并或删除。无重复时 total_groups=0。"""
    return _d(_call("GET", "/api/v1/duplicates"))


@mcp.tool()
def list_warehouses() -> str:
    """列出全部仓库（id/名称/层级/父库）。入库挂库(create_warehouse/stock_in 的 warehouse_id)、
    建子库前先用它确认仓库 id。返回 count + warehouses 数组。"""
    return _d(_call("GET", "/api/v1/warehouses"))


@mcp.tool()
def create_warehouse(name: str, parent_id: int = 0) -> str:
    """新建仓库节点。name=仓库名（必填）；parent_id=父仓库 id（0 或不传=建一级库）。
    仓库树最深 4 级。建子库前先确认父级 id（可通过对话让用户从仓库列表中选择）。
    ⚠ 写操作：执行前向用户复述将创建的仓库名与层级，征得确认后再调用。"""
    body = {"name": name}
    if parent_id:
        body["parent_id"] = parent_id
    return _d(_call("POST", "/api/v1/warehouses", body=body))


@mcp.tool()
def delete_material(material_id: int) -> str:
    """删除指定物料。material_id 来自 search_materials 的查询结果。
    ⚠️ 危险写操作：物料的现有库存会随删除一并清空（返回里的 discarded_stock 为清掉的数量），
    执行前必须把物料名称和当前库存复述给用户，明确征得"确认删除"后再调用。
    用户要清空全部物料时：先 search_materials 逐个列出，逐一经确认后删除。"""
    return _d(_call("DELETE", f"/api/v1/materials/{material_id}"))


if __name__ == "__main__":
    # stdio 模式运行（Cherry Studio 等客户端通过标准输入输出与之通信）
    if not TOKEN:
        print("警告：未设置 ERP_API_TOKEN 环境变量，所有工具调用都会被拒绝", flush=True)
    mcp.run()
