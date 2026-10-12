# -*- coding: utf-8 -*-
"""全局配置：读取 .env"""
import os

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

DB_TYPE = os.environ.get("DB_TYPE", "sqlite").lower()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, os.environ.get("SQLITE_PATH", "data/erp.db"))
POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_WAREHOUSE_LEVEL = 4
