# 羊毛机器人。目标是「在 NAS 上扔着不用管」，所以镜像尽量小、启动即用。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 不装系统包：zoneinfo 需要的那份时区数据由 requirements 里的 tzdata（PyPI）提供，
# HTTPS 用 requests 自带的 certifi 证书。少一步 apt-get 就少一个网络失败点。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY tools ./tools
COPY tests ./tests
COPY main.py ./

# 不用 root 跑；data/ 是唯一需要写的地方，挂成卷。
RUN useradd --create-home --uid 1000 woolbot \
 && mkdir -p /app/data \
 && chown -R woolbot:woolbot /app
USER woolbot

VOLUME ["/app/data"]
# 只是文档作用（真正对外暴露多少由 docker-compose.yml 的映射决定），两边保持一致。
EXPOSE 16886

# 健康检查的端口**从 config.json 现读**（部署脚本会把真实端口写在里面），读不到就用
# 兜底的 16886。这里以前写死一个 `http://127.0.0.1:8080/api/health`：哪天改了端口而
# 漏改这一行，容器会一直 unhealthy，而页面其实是好的 —— 排查起来格外费劲。
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import json,os,sys,urllib.request; p=16886; \
      p=int(((json.load(open('/app/config.json')).get('web') or {}).get('port') or p)) if os.path.exists('/app/config.json') else p; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%d/api/health' % p, timeout=3).status == 200 else 1)"

CMD ["python", "main.py", "all"]
