# Persona Replay · 运行镜像
#
# 单阶段即可：核心链路没有编译型依赖。
# 构建时不联网拉模型；运行时通过 DEEPSEEK_* 环境变量接远程模型，
# 没有 key 会自动退回离线 heuristic，容器仍然可用。
#
#   docker build -t persona-replay .
#   docker run --rm -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx persona-replay
#
# 注意：本文件在本机**未验证**（开发环境没装 Docker），只做了语法自查。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CHECKPOINT_PATH=/app/runtime/api.sqlite3 \
    GRAPH_ENGINE=auto

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt requirements-optional.txt ./
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements-optional.txt

COPY . .

# 非 root 运行；runtime 目录需要可写（checkpoint）
RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/runtime && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# --no-open：容器里没有浏览器；--skip-build：镜像构建时已做过编译检查
CMD ["python", "manage.py", "run", "--no-open", "--skip-build", "--port", "8000"]
