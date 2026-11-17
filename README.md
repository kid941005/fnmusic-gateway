# fnmusic-gateway

[![docker](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml/badge.svg)](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-fnmusic--gateway-blue)](https://github.com/kid941005/fnmusic-gateway/pkgs/container/fnmusic-gateway)
[![release](https://img.shields.io/github/v/release/kid941005/fnmusic-gateway)](https://github.com/kid941005/fnmusic-gateway/releases)

> 当前版本 **1.1.0**（见 [`CHANGELOG.md`](CHANGELOG.md)）

飞牛音乐（fnOS Music）→ 小智 的一层网关。**一个进程，两个出口**：

| 出口 | 地址 | 谁用 | 用途 |
|---|---|---|---|
| MCP（streamable-http） | `POST /mcp` | 服务端 / 云端 LLM | 查歌单、收藏、按心情选曲、拿直链 |
| 签名直链 | `GET /play/<guid>.mp3?exp=&sig=` | 设备 `self.music.play_url` | 设备直拉流，不经过小智服务端 |

另有 REST 面（`/api/*`）给小智插件用，避免插件去讲 MCP 协议。

## 为什么要有这一层

1. **设备只解 MP3**，飞牛曲库多为 FLAC ⇒ 必须转码。
2. **飞牛 token 不能进 URL**（URL 会进设备日志、进 LLM 上下文）⇒ 直链用 HMAC 短时签名，token 留在网关里。
3. **设备要求 URL ≤2048 字节、仅 http/https、禁空白与 `@`** ⇒ 不能把签名参数塞一堆。
4. 首次点播就转码并落盘缓存，`/api/play` 返回前预热，设备连上就能满速读。

## 快速开始

```bash
cd fnmusic-gateway
cp data/moods.json.example data/moods.json   # 可选，改心情映射
docker compose up -d --build
curl -s http://127.0.0.1:8766/healthz | python -m json.tool
```

裸跑（本机调试 / 没有 Docker）：

```bash
FNMUSIC_MOCK=1 python app.py        # 离线模式，不需要 NAS，音频是本地 4 秒正弦波
python app.py                       # 真实模式，需要 .env 里的飞牛地址和账号
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_PORT` | 8766 | 监听端口 |
| `GW_PUBLIC_BASE` | 空=按请求 Host 推导 | **设备能访问的地址**，如 `http://192.168.50.100:8766` |
| `GW_URL_TTL` | 900 | 直链有效期（秒） |
| `GW_SIGN_SECRET` | 空=自动生成 `/data/sign.key` | 签名密钥，多副本部署要一致 |
| `GW_DATA` | `./data` | 状态/密钥/token 落盘目录 |
| `GW_CACHE_DIR` | `$GW_DATA/cache` | MP3 缓存目录 |
| `GW_CACHE_MAX_BYTES` | 8 GiB | 缓存上限，超了按最久未用淘汰 |
| `GW_MP3_BITRATE` | 128k | 转码码率 |
| `GW_MP3_SR` | 44100 | 采样率 |
| `GW_PREWARM` | 1 | `/api/play` 返回前先转码，设备连上即可读 |
| `GW_MOODS_FILE` | `$GW_DATA/moods.json` | 心情映射表 |
| `FNMUSIC_MOCK` | 0 | 1=离线自测 |
| `FNMUSIC_BASE` | `http://192.168.50.100:59820` | 飞牛音乐地址 |
| `FNMUSIC_USER` | KID | 必须是 KID |
| `FNMUSIC_PASS` | 空 | 飞牛密码；登录后 token 落盘，之后可留空 |
| `FNMUSIC_DEVICE_ID` | 内置 | 换设备号会强制重新登录 |

## 部署到飞牛 NAS（与小智服务端同宿主机）

### 直接用 CI 构建好的镜像（不用自己 build）

每次推 `main` 或打 `v*` tag，GitHub Actions 会自动构建 `linux/amd64` + `linux/arm64`
并推到 GHCR（`v1.1.0` 这类 tag 会生成 `1.1.0`、`1.1`、`latest`、`sha-xxxxxxx` 四个标签）：

```bash
docker pull ghcr.io/kid941005/fnmusic-gateway:1.1.0   # 生产建议锁定版本
docker pull ghcr.io/kid941005/fnmusic-gateway:latest  # 跟主线
```

**两份 compose 默认已经指向 `1.1.0` 镜像**，不用改；想换版本或本地镜像时用环境变量覆盖：

```bash
GW_IMAGE=ghcr.io/kid941005/fnmusic-gateway:latest docker compose up -d
GW_IMAGE=fnmusic-gateway:dev docker compose up -d   # 配合 docker compose build
```

镜像包默认 private：要么在 GitHub 包设置里改成 public，要么在 NAS 上
`docker login ghcr.io -u kid941005` 用 PAT（read:packages）登录。


> 需要把密码写进文件时，在 compose 同目录手工建 `.env`（本工具链会拒绝代写 `.env*` 类文件），
> 键名见上面环境变量表；compose 里已用 `${VAR:-默认值}` 形式读取，不建也能跑（密码留空则首次登录失败）。


是 Docker 镜像：`Dockerfile`（alpine + ffmpeg 8，**261 MB**）已构建并跑通；
`Dockerfile.debian`（glibc + ffmpeg 7，805 MB）作备选，
`docker-compose.yml` 用 `network_mode: host` 让设备直接访问 8766。

```bash
# NAS 上（项目目录内）
docker compose up -d --build
docker compose logs -f --tail=50
curl -s http://127.0.0.1:8766/healthz
```

不在 NAS 上构建（本地构建后搬运）：

```bash
docker build -t fnmusic-gateway:1.1.0 .
docker save fnmusic-gateway:1.1.0 | gzip > fnmusic-gateway-1.1.0.tgz
# 传到 NAS 后
GW_IMAGE=fnmusic-gateway:1.1.0 docker load -i fnmusic-gateway-1.1.0.tgz && GW_IMAGE=fnmusic-gateway:1.1.0 docker compose up -d
```

### 和小智服务端怎么组网

小智官方 compose（`main/xiaozhi-server/docker-compose.yml`）是 **bridge + 映射 8000/8003**，
不是 host 网络。网关用 host 网络时三者关系：

| 谁 | 访问网关用 | 说明 |
|---|---|---|
| ESP32 设备 | `http://192.168.50.100:8766` | 必须和 NAS 同网段 |
| xiaozhi-server 容器（bridge） | `http://192.168.50.100:8766` | 容器到宿主机 LAN IP 可达 |
| 宿主机自己 | `http://127.0.0.1:8766` | 调试用 |

不想占用 host 网络就改成 bridge，效果等价：

```yaml
    # network_mode: host      # 删掉这行
    ports:
      - "8766:8766"
```

端口冲突先查：`ss -ltnp | grep 8766`。

### 两个必须注意的挂载

1. **插件文件不会自动进容器**：小智官方 compose 只挂了 `./data` 和
   `models/SenseVoiceSmall/model.pt`，`plugins_func/` 在镜像里。方式 B 要加一条挂载：

   ```yaml
   volumes:
     - ./data:/opt/xiaozhi-server/data
     - ./plugins_func:/opt/xiaozhi-server/plugins_func   # 新增
   ```

   并把 `examples/xiaozhi_plugin_fnmusic_gw.py` 放到宿主机 `main/xiaozhi-server/plugins_func/functions/`。
   插件读环境变量 `FNMUSIC_GW_BASE`（默认 `http://192.168.50.100:8766`），可在 xiaozhi 容器里设。
2. **方式 A 的 MCP 配置**在容器内是 `/opt/xiaozhi-server/data/.mcp_server_settings.json`，
   对应宿主机 `main/xiaozhi-server/data/.mcp_server_settings.json`（已挂载，直接改宿主机那份即可）。

## 接到小智

### 方式 A：服务端 MCP（全局，零 Java 改动）

把 `examples/mcp_server_settings.snippet.json` 的内容并进
`main/xiaozhi-server/data/.mcp_server_settings.json`，重启服务端。
`mcp_manager.py` 启动时读这个文件，把网关的 9 个 `music_*` 工具挂到所有智能体上。

### 方式 B：插件调 REST（推荐，能一次调用完成"查歌 + 播放"）

自建 xiaozhi-server **一轮只执行一个工具**（工具返回后走 `Action.REQLLM` → 纯文本，
不再进 function_call），所以"LLM 先调 MCP 拿直链、再调设备工具"走不通。
把 `examples/xiaozhi_plugin_fnmusic_gw.py` 放到
`main/xiaozhi-server/plugins_func/functions/`，插件内部直接：

```python
GET /api/play?query=晴天  ->  {"url": "http://nas:8766/play/xxx.mp3?exp=&sig="}
call_mcp_tool(conn, conn.mcp_client, "self.music.play_url", {"url": url})
```

### 方式 C：小智 MCP 接入点（wss，已有接入点时用这个）

如果你的小智已经有「MCP 接入点」地址，形如
`wss://xxx:59826/mcp_endpoint/mcp/?token=...`，那它就是一个**消息转发中心**：
xiaozhi-server 连上去要工具，工具端也要连上去供工具。工具端必须主动外连，
所以不需要公网 IP，也不用改 xiaozhi-server 配置。

```
xiaozhi-server --WS--> 接入点 <--WS-- fnmusic-bridge --HTTP--> fnmusic-gateway
```

bridge 已经打进同一个镜像（`bridge/mcp_pipe_http.py`，只依赖 `websockets`），
在 compose 里加一个服务即可（两份 compose 都已写好，填 `MCP_ENDPOINT` 就会启动）：

```bash
export MCP_ENDPOINT='wss://xxx:59826/mcp_endpoint/mcp/?token=...'
docker compose up -d            # fnmusic-gateway + fnmusic-bridge
docker logs -f fnmusic-bridge   # 看到 "接入点已连接" 和 "tools/list -> 9 个工具" 即成功
```

不填 `MCP_ENDPOINT` 时桥接器会一直重连、不影响网关本体。

接入点会把同一 token 下所有工具端的工具**合并**成一张表，所以：
- 一个 token 只跑一个 fnmusic-bridge，重复连会看到重复工具；
- 想换工具集，直接重启 bridge（接入点会重新拉 tools/list）。

本地自测（不用真接入点，用假 broker 跑一遍 initialize/tools/list/ping/tools/call）：

```bash
python tests/bridge_selftest.py    # 需网关已在 127.0.0.1:8799 以 mock 模式运行
```

## 直链自测

```bash
# 1) 拿直链
curl -s "http://127.0.0.1:8766/api/play?query=%E6%99%B4%E5%A4%A9" | python -m json.tool
# 2) 验证可播 + 参数正确
curl -s -o /tmp/t.mp3 "<上一步的 url>"
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,bit_rate -of default=nw=1 /tmp/t.mp3
# 期望 codec_name=mp3 sample_rate=44100 channels=2 bit_rate=128000
# 3) 过期/篡改应 403
curl -s -o /dev/null -w '%{http_code}\n' "<把 sig 改一位的 url>"
```

## 心情映射

飞牛 API 没有 genre/mood 维度，只有 `/search/track?q=`。所以心情靠 `data/moods.json`
把 `心情 → 关键词` 映射到搜索词，命中后去重打散。默认 8 个场景，按自己曲库改。

## 已验证（2026-11-16，mock 模式）

| 项 | 结果 |
|---|---|
| 官方 MCP SDK 3.12 客户端（streamable-http） | initialize 成功（协商 2025-11-25）、tools/list 9 个工具、call_tool 正常 |
| `/api/play?query=晴天` | 返回签名直链 + 预热 65244 字节 |
| 直链下载 | 200，`ffprobe`：mp3 / 44100 / 2ch / 128000 |
| 篡改 sig / 过期 | 403 |
| `Range: bytes=0-99` | 206，100 字节 |
| `DELETE /mcp`（会话关闭） | 204，无告警 |
| 真实模式缺账号 | `/healthz` 200 且 `provider_error` 说明原因，`/api/play` 500 带同一错误，不崩 |
| Docker 镜像构建 | 成功：alpine 261 MB / debian 805 MB，均带 HEALTHCHECK |
| 容器内跑通（bridge + 端口映射） | `/healthz` healthy；容器内 ffmpeg 8.1.3 含 libmp3lame，直链 200 / 65245 字节；缓存落到 `/data/cache` |

真实飞牛未验证：开发机到 `192.168.50.100:59820` 不通，需在 NAS 同网段跑一次。

## 已知限制

- **播歌时设备断 WS**：`/api/play` 这条路服务端看不到播放状态，也发不出 stop。
  要"定时停/语音打断"请继续用 `fnmusic.py` 的 TTS FILE 通道（`POST /api/play` 只是拿到 URL，
  播放控制权在设备）。
- 签名直链到 `self.music.play_url` 的端到端验收在固件 v2.6.0 上**还没做过**
  （见 `docs/merge_v2.6.0_assessment.md:97`），先用上面"直链自测"验通再上线。
- 无进度条/封面/歌词：固件只认 URL。
- 网关单实例：缓存与签名密钥都在本地，多副本要共享 `/data`。
