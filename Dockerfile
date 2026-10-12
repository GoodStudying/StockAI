# 个人ERP Docker 镜像（适配飞牛 fnOS / 群晖等 x86_64 NAS）
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，改代码不用重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝代码
COPY app.py db.py lcsc.py config.py ./
COPY static ./static

# 数据目录（挂载点：SQLite 数据库；文件已存 BLOB 入库，无需挂 uploads）
RUN mkdir -p /app/data

EXPOSE 5000

# 生产环境用 gunicorn 多 worker（需在 requirements 之外单独确认已安装）
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:5000 app:app --timeout 120 || python app.py"]
