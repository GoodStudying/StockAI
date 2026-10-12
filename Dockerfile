# 个人ERP Docker 镜像（适配飞牛 fnOS / 群晖等 x86_64 NAS）
# 基础镜像走国内加速源（绕过 fnOS 自带的 docker.fnnas.com，无需改系统设置）
# 多源备份：哪个可用用哪个（保持 /library/python:3.12-slim 后缀不变）
# 可选源：docker.1ms.run / hub.rat.dev / docker.xuanyuan.me / dockerproxy.net
FROM docker.1ms.run/library/python:3.12-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，改代码不用重装）；pip 走清华源加速
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    && python -c "import gunicorn, flask, requests, psycopg2; print('依赖安装完整')"

# 再拷贝代码与启动脚本
COPY app.py db.py lcsc.py config.py ./
COPY static ./static
COPY start.sh .
RUN chmod +x start.sh

# 数据目录（挂载点：SQLite 数据库；文件已存 BLOB 入库，无需挂 uploads）
RUN mkdir -p /app/data

EXPOSE 5000

# 启动脚本：等数据库就绪 → gunicorn 绑定 0.0.0.0（不依赖 app.py 的 __main__ 启动行）
CMD ["./start.sh"]
