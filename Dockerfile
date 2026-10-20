# 刷B站 · 独立常驻服务镜像
#
#   docker compose up -d --build
#
# 只跑一个进程：调度器 + HTTP 控制页（8787）。所有状态都在 /data 卷里，
# 容器重建不会丢台账（见 README「数据目录」一节）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    BILIDUTY_DATA=/data

# tzdata 是给 zoneinfo 用的（窗口按北京时间算）；ca-certificates 给 B站 / 模型接口
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin biliduty \
 && mkdir -p /data \
 && chown -R biliduty:biliduty /data /app

USER biliduty
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "biliduty", "serve"]
