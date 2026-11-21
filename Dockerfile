# 单镜像：python slim + ffmpeg + 内置 musicdl（在线音源）
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py fnmusic_client.py ./
COPY gwlib/ ./gwlib/
COPY bridge/ ./bridge/

# 内置在线音源（musicdl）与 MCP 桥接器依赖
RUN pip install --no-cache-dir "musicdl==2.14.2" "httpx>=0.27" "curl_cffi>=0.16" "websockets>=13" "mcp>=2.3,<3"

ENV PYTHONUNBUFFERED=1 \
    GW_DATA=/data \
    GW_PORT=8766

VOLUME ["/data"]
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GW_PORT','8766')+'/healthz',timeout=3)"

CMD ["python", "-u", "app.py"]
