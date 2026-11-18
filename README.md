# fnmusic-gateway

[![docker](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml/badge.svg)](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-fnmusic--gateway-blue)](https://github.com/kid941005/fnmusic-gateway/pkgs/container/fnmusic-gateway)
[![release](https://img.shields.io/github/v/release/kid941005/fnmusic-gateway)](https://github.com/kid941005/fnmusic-gateway/releases)

> 当前版本 **1.4.0**（见 [`CHANGELOG.md`](CHANGELOG.md)）

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
curl -s http://127.0.0.1:8766/healthz | python -m json.tool   # 存活
curl -s http://127.0.0.1:8766/readyz  | python -m json.tool   # 就绪（真访问曲库）
```

裸跑（本机调试 / 没有 Docker）：

```bash
FNMUSIC_MOCK=1 python app.py        # 离线模式，不需要 NAS，音频是本地 4 秒正弦波
python app.py                       # 真实模式，需要 .env 里的飞牛地址和账号
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_PORT` | 8766 | 管理端口（REST + MCP，**只留内网**） |
| `GW_PUBLIC_PORT` | 0=关闭 | 公网专用端口：**只开放 `/play/` 与 `/lyrics/`**，其余路径 404（见下文"公网只暴露播放端口"） |
| `GW_PUBLIC_BASE` | 空=按请求 Host 推导 | **设备能访问的地址**，如 `http://192.168.50.100:8766` |
| `GW_URL_TTL` | 900 | 直链有效期（秒） |
| `GW_SIGN_SECRET` | 空=自动生成 `/data/sign.key` | 签名密钥，多副本部署要一致 |
| `GW_DATA` | `./data` | 状态/密钥/token 落盘目录 |
| `GW_CACHE_DIR` | `$GW_DATA/cache` | MP3 缓存目录 |
| `GW_CACHE_MAX_BYTES` | 8 GiB | 缓存上限，超了按最久未用淘汰 |
| `GW_MP3_BITRATE` | 128k | 转码码率 |
| `GW_MP3_SR` | 44100 | 采样率 |
| `GW_PREWARM` | 1 | `/api/play` 返回前先转码，设备连上即可读 |
| `GW_MAX_LIMIT` | 50 | 单次查询返回条数上限（LLM 传再大也会被收敛） |
| `GW_TRANSCODE_CONCURRENCY` | 2 | 同时转码数上限，防止多设备点播把 NAS CPU 打满 |
| `GW_LYRICS` | 1 | 是否查歌词（设备屏显用）；0=关闭，`lyric_url` 一律留空 |
| `GW_LRCLIB_URL` | `https://lrclib.net/api/get` | 歌词源（LRCLIB 免费无 key，可自建） |
| `GW_LYRICS_DIR` | `$GW_DATA/lyrics` | LRC 缓存目录 |
| `GW_LYRICS_RETRY` | 86400 | 查不到歌词的重试间隔（秒） |
| `GW_SMART_SEARCH` | 1 | 搜索是否做别名纠错 + 相关度排序；0=沿用飞牛原始顺序 |
| `GW_ALIASES_FILE` | `$GW_DATA/music_aliases.json` | ASR 同音字/错字纠正表（示例见 `data/music_aliases.json.example`，改完自动重载） |
| `GW_MOODS_FILE` | `$GW_DATA/moods.json` | 心情映射表 |
| `FNMUSIC_MOCK` | 0 | 1=离线自测 |
| `FNMUSIC_BASE` | `http://192.168.50.100:59820` | 飞牛音乐地址 |
| `FNMUSIC_USER` | KID | 必须是 KID |
| `FNMUSIC_PASS` | 空 | 飞牛密码；登录后 token 落盘，之后可留空 |
| `FNMUSIC_DEVICE_ID` | 内置 | 换设备号会强制重新登录 |

## 部署到飞牛 NAS（与小智服务端同宿主机）

### 直接用 CI 构建好的镜像（不用自己 build）

每次推 `main` 或打 `v*` tag，GitHub Actions 会自动构建 `linux/amd64` + `linux/arm64`
并推到 GHCR（`v1.4.0` 这类 tag 会生成 `1.4.0`、`1.2`、`latest`、`sha-xxxxxxx` 四个标签）：

```bash
docker pull ghcr.io/kid941005/fnmusic-gateway:1.4.0   # 生产建议锁定版本（compose 默认用 latest）
docker pull ghcr.io/kid941005/fnmusic-gateway:latest  # 跟主线
```

**两份 compose 默认跟 `latest`**（CI 每次推 main / tag 都会更新它），不用改；想锁定版本或换本地镜像时用环境变量覆盖：

```bash
# 生产建议显式锁定版本，避免 latest 漂移
GW_IMAGE=ghcr.io/kid941005/fnmusic-gateway:1.4.0 docker compose up -d
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
docker build -t fnmusic-gateway:1.4.0 .
docker save fnmusic-gateway:1.4.0 | gzip > fnmusic-gateway-1.4.0.tgz
# 传到 NAS 后
GW_IMAGE=fnmusic-gateway:1.4.0 docker load -i fnmusic-gateway-1.4.0.tgz && GW_IMAGE=fnmusic-gateway:1.4.0 docker compose up -d
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

### 公网播放：只反代 8767，绝不要反代 8766

`/api/*`、`/mcp`、`/healthz` **没有鉴权**。要设备在外网也能播，正确做法是开一个
**只服务 `/play/` 的公网端口**，反代只指向它：

```yaml
  fnmusic-gateway:
    network_mode: host
    environment:
      GW_PORT: "8766"                                        # 管理面：REST + MCP，只留内网
      GW_PUBLIC_PORT: "8767"                                 # 公网面：只放行 /play/
      GW_PUBLIC_BASE: "https://fnmusic.example.com"          # 设备拿到的直链走这个地址
```

反代（Lucky / nginx / caddy）把整个域名转发到 `192.168.50.100:8767`，**不需要任何路径规则**：

| 请求 | 8767（公网面） | 8766（管理面） |
|---|---|---|
| `GET/HEAD /play/<guid>.mp3?exp=&sig=` | 200 / 206（支持 Range） | 200 / 206 |
| `/api/*`、`/mcp`、`/healthz`、`/readyz` | **404** | 200 |
| `POST` / `DELETE /mcp` | **404** | 200 / 204 |

验证（应全部符合上表）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://fnmusic.example.com/healthz        # 404
curl -s -o /dev/null -w '%{http_code}\n' https://fnmusic.example.com/api/playlists  # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://fnmusic.example.com/mcp  # 404
```

> `GW_PUBLIC_PORT` 是进程内的路径判断，不依赖反代软件的子规则，配错也不会漏。

### hairpin：家里设备拉公网域名会失败怎么办

`GW_PUBLIC_BASE` 是公网域名后，**在家的设备也会去拉这个公网域名**。如果路由器不支持
NAT 回环（hairpin），表现是“设备说正在播放，但没有声音”。两种解法任选：

1. 在 Lucky 或内网 DNS 里把该域名解析到 `192.168.50.100`（内外都通，推荐）；
2. 或让路由器/防火墙放行内网访问公网 IP 的回环。

固件对 `self.music.play_url` 的限制：**URL ≤ 2048 字节、仅 http/https、不能含空格和 `@`**。


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

## 自检与测试

两个探针分工不同，别混用：

| 探针 | 语义 | 失败时 |
|---|---|---|
| `GET /healthz` | 存活：只证明网关进程活着，不碰 NAS（NAS 掉线时缓存仍能服务） | 进程挂了 |
| `GET /readyz` | 就绪：真访问一次曲库 | 返回 503 + 原因（NAS 不通 / 账号错 / token 失效） |

```bash
python tests/test_gateway.py -v   # 35 个用例：签名/直链/Range/MCP/REST/参数校验/缓存/歌词/搜索排序与别名，全离线（mock）
python tests/bridge_selftest.py   # 桥接器：假接入点跑 initialize/tools/list/ping/tools/call
```

`test_gateway.py` 进程内起服务，不需要 NAS 也不需要 Docker；有 ffmpeg 时还会验转码缓存不留残片。

## 播放时显示歌名 / 歌手 / 歌词

固件 `self.music.play_url` 不只接受 `url`，还接受 `title` / `artist` / `lyric_url`
（见 `xiaozhi-esp32-zhj` 的 `main/mcp_server.cc`）。只传 `url` 时屏幕上是 `unknown`、没有歌词。

网关 1.4.0 起：`/api/play` 的 JSON 多了 `lyric_url` 与 `device_args`，MCP `music_play` 也会把
歌名、歌手、歌词直链一起吐给 LLM，要求它调 `self.music.play_url` 时四个参数都带上。

歌词来源与格式：

- 源是 [LRCLIB](https://lrclib.net)（免费、无 key、无需登录），按 `歌名 + 歌手` 查。
- 优先用 `syncedLyrics`（带 `[mm:ss.xx]` 时间轴）。只有 `plainLyrics` 时，网关按 4 秒一行
  自动补时间戳——**固件只认带时间戳的行，纯文本会被整行丢掉**。
- LRC 缓存在 `$GW_DATA/lyrics/<guid>.lrc`；查不到会写一个空文件，`GW_LYRICS_RETRY`（默认 1 天）
  后才会重试，避免每次播放都卡在查歌词上。
- 纯音乐 / 查不到 → `lyric_url` 为空串，插件就不传该参数，屏幕只显示歌名与歌手。
- 歌词直链与音频直链同一套 HMAC 签名，同样受 `GW_URL_TTL` 限制；公网面 `GW_PUBLIC_PORT` 同样放行。

示例插件（`examples/xiaozhi_plugin_fnmusic_gw.py`）已经把这些参数带上：

```python
args = {"url": url, "title": data["title"], "artist": data["artist"] or "unknown"}
if data.get("lyric_url"):
    args["lyric_url"] = data["lyric_url"]
_device(conn, DEVICE_PLAY, args)   # self.music.play_url
```

自测：

```bash
curl -s "http://127.0.0.1:8766/api/play?query=%E6%99%B4%E5%A4%A9" \
  | python -c "import json,sys;d=json.load(sys.stdin);print(d['lyric_url'])"
curl -s "<上一步的 lyric_url>" | head -3   # 期望 [00:29.36] 故事的小黃花 ...
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
| `tests/test_gateway.py`（26 用例） | 全绿：签名/过期/篡改、直链下载与 Range 206、MCP initialize+9 工具+call_tool、空 q / 坏 limit / 坏 action → 400、`/playlists` → 404、转码不留残片 |
| `/healthz` 与 `/readyz` | 前者只报进程存活，后者真探曲库；NAS 异常时 `/readyz` 返回 503 |
| `GW_PUBLIC_PORT` 公网面 | `/play/...` 200 / Range 206 / HEAD 200、`/lyrics/...` 200 text/plain；`/healthz`、`/readyz`、`/api/*`、`POST /mcp`、`DELETE /mcp` 全部 404；`GW_PORT` 管理面不受影响 |
| 歌词链路（真实 LRCLIB） | `/api/play?query=晴天` → `lyric_url`；拉取 200 `text/plain; charset=utf-8`，首行 `[00:29.36] 故事的小黃花`（带 LRC 时间戳，固件才认） |
| 公网面歌词 | `/lyrics/...` 200、坏签名 403，其余路径仍 404 |

真实飞牛未验证：开发机到 `192.168.50.100:59820` 不通，需在 NAS 同网段跑一次。

## 公网只暴露播放端口（GW_PUBLIC_PORT）

网关的 `/api/*`、`/mcp`、`/healthz` **没有任何鉴权**。如果整域名反代到 `GW_PORT`（8766），
等于把曲库浏览、签名直链（`/api/play` 会直接发出签名 URL）、转码接口全暴露到公网。

正确做法：开一个**只服务 `/play/` 与 `/lyrics/` 的公网端口**，反代只指向它。

```yaml
environment:
  GW_PORT: "8766"          # 管理面，内网
  GW_PUBLIC_PORT: "8767"   # 公网面，只放行 /play/ 与 /lyrics/
```

- 反代（Lucky / nginx / caddy）把 `https://fnmusic.example.com` 整域名转发到 `192.168.50.100:8767`，**不需要任何路径规则**。
- `8767` 上：`GET/HEAD /play/<guid>.mp3?exp=&sig=` 正常（200 / 206，支持 Range）；`GET /lyrics/<guid>.lrc?exp=&sig=` 正常（200 `text/plain`）；其余一律 **404**。
- `8766` 不上公网，插件方式 B、桥接器、探针都走它。
- `GW_PUBLIC_BASE` 填这个公网地址（如 `https://fnmusic.example.com`），设备拿到的直链就走公网。

> 0 = 关闭（默认）。这是纯进程内的路径判断，不依赖反代软件的子规则，配错也不会漏。

## 搜索更聪明：错字/同音字纠正 + 相关度排序

中文语音识别经常把歌名/歌手听成同音字（"青花瓷"→"青花磁"、"周杰伦"→"周杰轮"），飞牛接口本身只做简单匹配，所以网关拿到结果后自己再排一次序：

- **归一化**：NFKC（全角转半角）、大小写折叠、去掉空格/标点/书名号。
- **别名表**：`$GW_DATA/music_aliases.json`（示例 `data/music_aliases.json.example`），键=听到的词、值=正确写法；最长匹配替换，改完立即生效（按 mtime/size 重载，不用重启）。
- **打分**：标题/歌手精确命中 100 > 前缀 88 > 包含 78−长度差 > difflib 相似度×60；歌手整串命中 +12；用户没提版本词时，带「伴奏/纯音乐/钢琴版/翻唱/remix…」的候选 −15；标题长度接近查询 +3。按分数降序返回，同分短标题优先。
- 别名生效时**同时用纠正前后两个词各搜一次**再合并排序（别名表写错也不会搜不到），`/api/search` 响应多一个 `corrected` 字段，MCP 返回文本注明"已按别名纠正"。

```bash
# 精确版应排在钢琴版前面
curl -s 'http://127.0.0.1:8766/api/search?q=晴天' | python -m json.tool | head -20
# 别名纠正（先按示例建表）
cp data/music_aliases.json.example data/music_aliases.json
curl -s 'http://127.0.0.1:8766/api/search?q=青花磁'
# 期望 {"query":"青花磁","corrected":"青花瓷",...}
```

`GW_SMART_SEARCH=0` 可关掉整层（回退成飞牛原始顺序），排查"是不是排序搞的鬼"时用。

## 已知限制

- **播歌时设备断 WS**：`/api/play` 这条路服务端看不到播放状态，也发不出 stop。
  要"定时停/语音打断"请继续用 `fnmusic.py` 的 TTS FILE 通道（`POST /api/play` 只是拿到 URL，
  播放控制权在设备）。
- 签名直链到 `self.music.play_url` 的端到端验收在固件 v2.6.0 上**还没做过**
  （见 `docs/merge_v2.6.0_assessment.md:97`），先用上面"直链自测"验通再上线。
- **只支持 MP3**：固件 `self.music.play_url` 的播放任务硬编码 `ESP_AUDIO_SIMPLE_DEC_TYPE_MP3`（`application.cc:1909`），Ogg/Opus 直链放不了，所以网关不提供 `?fmt=opus`。
- 无进度条/封面：固件只显示歌名、歌手和滚动歌词，没有封面图与进度条。
- 网关单实例：缓存与签名密钥都在本地，多副本要共享 `/data`。
