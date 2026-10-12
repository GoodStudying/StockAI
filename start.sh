#!/bin/sh
# 启动脚本：等待数据库就绪后再启动 gunicorn（gunicorn 自己绑 0.0.0.0，不依赖 app.py 的启动行）
echo "[启动] DB_TYPE=$DB_TYPE"

if [ "$DB_TYPE" = "postgres" ]; then
  echo "[启动] 等待 PostgreSQL 就绪（最多 60 秒）..."
  i=0
  while [ $i -lt 30 ]; do
    if python -c "import os,psycopg2; psycopg2.connect(os.environ['POSTGRES_DSN'])" 2>/dev/null; then
      echo "[启动] 数据库连接成功"
      break
    fi
    i=$((i+1))
    sleep 2
  done
  if [ $i -ge 30 ]; then
    echo "[错误] 数据库连接失败！以下是具体原因："
    python -c "import os,psycopg2; psycopg2.connect(os.environ['POSTGRES_DSN'])"
    echo "[错误] 请检查：postgres 容器是否在跑 / 端口映射是否 54321:5432 / DSN 密码库名是否正确"
    sleep 5
    exit 1
  fi
fi

echo "[启动] 启动 ERP 服务：http://0.0.0.0:5000"
exec gunicorn -w 2 -b 0.0.0.0:5000 --timeout 120 --access-logfile - app:app
