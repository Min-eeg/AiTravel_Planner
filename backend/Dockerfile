# ===== 途策智游后端镜像 =====
# 用 3.13-slim：与本地开发版本一致（pydantic-core 等二进制包在新版本可能没有预编译 wheel）
FROM python:3.13-slim

# PyPI 源做成构建参数：国内构建可传 --build-arg PIP_INDEX_URL=<镜像源>，默认官方源
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只拷依赖清单再安装：这一层不常变，改代码不会触发依赖重装
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 应用代码 + RAG 知识库（data/kb 是检索语料，运行时必需）
# tests/ scripts/ eval/ 不进镜像（见 .dockerignore）
COPY app ./app
COPY data ./data

# 不用 root 跑服务
# 运行时数据单独放 dbdata/，不要把卷挂在 data/ 上：
# data/ 是镜像内静态资源（RAG 知识库），被卷遮蔽后新构建的语料不会生效
RUN useradd -m appuser \
    && mkdir -p /app/dbdata \
    && chown -R appuser:appuser /app/dbdata
USER appuser

EXPOSE 8030
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8030"]
