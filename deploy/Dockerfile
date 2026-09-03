# TeleOps backend 镜像（自托管 / docker-compose 用）
# 构建上下文为项目根目录：docker compose build 时 context=., dockerfile=deploy/Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 仅安装 FastAPI 真正需要的依赖，避免 gradio/chromadb/pandas 撑大镜像
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 复制整个项目（含 data/ 基线、kb/、tools/、web/ 静态界面）
COPY . /app

# 运行时由 named volume 挂载，保证业务数据 / 知识库 / 生成工具持久化
VOLUME ["/app/data", "/app/kb", "/app/tools", "/app/traces"]

EXPOSE 8000

# 单端口同时提供 API 与静态界面（server.py 已 mount web/ 到 /）
CMD ["sh", "-c", "uvicorn src.api.server:app --host 0.0.0.0 --port 8000"]
