# 默认镜像：alpine + ffmpeg（约 261 MB，ffmpeg 8.x 自带 libmp3lame）
# 想要 glibc 版本用 Dockerfile.debian（约 805 MB，ffmpeg 7.x）
FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY app.py fnmusic_client.py ./
COPY gwlib/ ./gwlib/
COPY bridge/ ./bridge/

# 接入小智 MCP 接入点（WebSocket）用的桥接器依赖
RUN pip install --no-cache-dir "websockets>=13" "mcp>=2.3,<3"

ENV PYTHONUNBUFFERED=1 \
    GW_DATA=/data \
    GW_PORT=8766

VOLUME ["/data"]
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GW_PORT','8766')+'/healthz',timeout=3)"

CMD ["python", "-u", "app.py"]
