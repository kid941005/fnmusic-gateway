# fnmusic-gateway

[![docker](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml/badge.svg)](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-fnmusic--gateway-blue)](https://github.com/kid941005/fnmusic-gateway/pkgs/container/fnmusic-gateway)
[![release](https://img.shields.io/github/v/release/kid941005/fnmusic-gateway)](https://github.com/kid941005/fnmusic-gateway/releases)

> 当前版本 **2.5.0**（2026-11-20） · [CHANGELOG](CHANGELOG.md)

飞牛音乐优先、在线音源兜底的播放网关。一个进程，一个端口，WebUI 同端口；飞牛地址与账号在网页里填，不用改环境变量：

| 入口 | 地址 | 谁用 | 用途 |
|---|---|---|---|
| MCP（streamable-http） | `POST /mcp` | 小智服务端 / 云端 LLM | 搜索飞牛/在线音源、拿直链、下载到本地 |
| 签名直链 | `GET /play/<guid>.mp3?exp=&sig=` | 设备 `self.music.play_url` | 设备直拉流，不经过小智服务端 |

另有 REST 面（`/api/*`）给小智插件或脚本用，避免它们去讲 MCP 协议。
歌词直链使用同一套签名：`GET /lyrics/<guid>.lrc?exp=&sig=`。

## 能做什么

- **飞牛音乐优先**：在网页「网关设置」填飞牛地址与账号，搜索/歌单/收藏/播放优先走飞牛；没配飞牛时自动用在线音源兜底。
- **设备可播**：音源里的 FLAC/无损自动转 MP3，签发短时直链，支持 Range、HEAD 和预转码。
- **屏显信息**：播放时把歌名、歌手、歌词（LRC）一起交给设备，不再显示 `unknown`。
- **听得更准**：中文同音字/错字纠正 + 相关度排序（青花磁 → 青花瓷）。
- **按心情选曲**：内置情绪关键词 + 在线音源搜索组合出候选歌单。
- **安全外网**：只发布一个 8766；公网反代只转发 `/play/` 与 `/lyrics/`，管理面与 WebUI 不暴露。
- **多种接入**：12 个 MCP 工具、REST 插件、MCP 接入点（wss）桥接三种方式任选。
- **音质可选**：`standard / high / exhigh / lossless` 四档，设备始终拿 MP3，下载保留原始音质。
- **下载与边播边存**：在线歌曲可在播放时同步落盘（`GW_TEE_SAVE`），也可显式 `music_download` 下载到本地下载目录。
- **模块解耦**：`gwlib/` 拆出 errors / quality / storage / sources / playback / config / settings_page / mcp_tools / mcp_server，新增音源或音质策略不改播放层与 MCP 层。
- **同端口 WebUI**：音源管理界面直接挂在 8766 根路径（`http://NAS_IP:8766/`），浏览器不需要记路径、也不需要访问单独的 WebUI 端口，不额外暴露宿主机端口。
- **官方 MCP 2.3**：`/mcp` 由官方 `mcp>=2.3` 的 `MCPServer`（原 FastMCP）提供工具注册、参数校验与错误包装，旧 JSON-RPC 仅在依赖缺失时兜底。

## 为什么需要这一层

1. **设备只解 MP3**，音源多为 FLAC/无损 ⇒ 必须转码。
2. **音源地址与密钥不能进 URL**（URL 会进设备日志、进 LLM 上下文）⇒ 直链用 HMAC 短时签名，密钥留在网关里。
3. **设备要求 URL ≤ 2048 字节、仅 http/https、禁空白与 `@`** ⇒ 不能把签名参数塞一堆。
4. 首次点播就转码并落盘缓存，`/api/play` 返回前预热，设备连上就能满速读。
5. **凭据只留在网关本地**：飞牛账号只写进 `/data/config.json`（0600）并换成本地 token，不出现在 URL、日志和 LLM 上下文里。

## 2.x 的分层结构

代码按职责拆成独立模块，互不越界；`app.py` 只做 HTTP/MCP 路由与装配：

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `gwlib/errors.py` | 统一错误码与 HTTP 状态映射 | `GatewayError` / `as_payload` |
| `gwlib/quality.py` | 音质档位与转码参数 | `normalize_quality` / `get_profile` |
| `gwlib/storage.py` | MP3 转码缓存 + 本地下载目录落盘 | `MediaCache.ensure` / `LibraryStore.save_from_provider` |
| `gwlib/config.py` | 网页设置持久化（`/data/config.json`，0600 原子写） | `ConfigStore.update/section` |
| `gwlib/settings_page.py` | `/settings` 网关设置页（纯静态 HTML） | `SETTINGS_HTML` |
| `gwlib/sources.py` | 在线音源适配、探活、多源并行检索 | `ExtProvider` / `FallbackProvider` / `source_status` |
| `gwlib/playback.py` | 选源→音质→缓存→直链→边播边存 | `PlaybackService.prepare` |
| `gwlib/mcp_tools.py` | 12 个 MCP 工具定义 | `build_tools` / `QUALITY_ENUM` |
| `gwlib/mcp_server.py` | 官方 MCPServer 2.3 工具注册 + 回环 HTTP 桥 | `McpService` / `McpHttpBridge` |

新增音源只需实现 `search/stream/lyrics` 并注册到 `sources.py`；新增音质只改 `quality.py` 的 `PROFILES`，播放层与 MCP 层无需改动。

## 部署：单端口 + WebUI

整个服务只发布一个宿主机端口 **8766**。WebUI、MCP、REST、播放直链全部同端口：

| 入口 | 地址 | 谁用 |
|---|---|---|
| WebUI | `http://NAS_IP:8766/` | 浏览器（音源管理，根路径直达） |
| MCP | `http://NAS_IP:8766/mcp` | 小智服务端 / 桥接器 |
| REST / 播放 | `http://NAS_IP:8766/api/...`、`/play/...` | 插件、设备直链 |

三个容器（网关、桥接器、音源）同处内部 bridge 网络 `fnmusic`；音源容器不映射任何宿主机端口，WebUI 由网关反代。

### 首次部署（可直接复制）

**步骤 1：准备目录和配置**

~~~bash
cd /path/to/fnmusic-gateway
mkdir -p data sources-data sources-repo music
chown -R 1000:1000 sources-data sources-repo music   # 容器内以 uid 1000 运行
cp .env.example .env
chmod 600 .env
~~~

**步骤 2：设置管理令牌（必填）**

~~~bash
TOKEN=$(openssl rand -hex 24)
sed -i "s/^GW_ADMIN_TOKEN=.*/GW_ADMIN_TOKEN=$TOKEN/" .env
# 没有 openssl 时可用：python3 -c 'import secrets;print(secrets.token_hex(24))'
~~~

**步骤 3：启动**

~~~bash
docker compose pull
docker compose up -d
~~~

**步骤 4：验证**

~~~bash
ss -ltnp | grep 8766                  # 对外只应看到 8766；127.0.0.1 的随机端口是 MCP 内部回环
curl -s 127.0.0.1:8766/healthz
curl -s -H "X-Admin-Token: $TOKEN" 127.0.0.1:8766/api/quality
~~~

**步骤 5：打开 WebUI**

浏览器访问：

~~~text
http://NAS_IP:8766/?token=<GW_ADMIN_TOKEN>
~~~

网关校验 token 后写入 HttpOnly Cookie，并 302 回到 `/`（URL 里的 token 被去掉）；之后直接访问
`http://NAS_IP:8766/` 即可。WebUI 里的管理 API 由网关注入管理员身份，不需要在浏览器里手工加 Header。
（上游管理台前端固化了 `/app/fnmusic-ext/static|api` 前缀，网关仍保留该前缀的反代供其资源与 API 使用，但浏览地址就是根路径。）

### 网关设置（飞牛音乐）

WebUI 首页右下角有「**网关设置 · 飞牛音乐**」入口，也可以直接访问 `http://NAS_IP:8766/settings`（首次带 `?token=<GW_ADMIN_TOKEN>` 换 Cookie）。页面上：

- 填 **飞牛音乐地址**（如 `http://192.168.1.10:5666`）、**账号**、**密码**、设备 ID（可留空）；
- 点「**测试连接**」会真实登录并试取 1 首，返回耗时和曲目数或错误原因；
- 点「保存」写入 `/data/config.json`（权限 0600），并立即生效，无需重启；
- 点「清除登录令牌」删掉缓存 token（换账号或密码后建议清一次）。

配置优先级：网页保存的值 > 环境变量 `GW_FNOS_*`。密码只保存在本地，接口只回 `password_set`，保存时留空表示不修改。
没配飞牛时，网关自动用下面的「在线音源」兜底。

### 端口与安全边界

- **只发布 8766**：网关容器 `ports: 8766:8766`；音源容器零端口。
- **内部回环**：官方 MCPServer 2.3 在容器内 `127.0.0.1` 的随机端口启动，只给网关自己转发 `/mcp`，不映射到宿主机。
- **鉴权**：`GW_ADMIN_TOKEN` 保护 `/api/*`、`/mcp`、WebUI；`/play/`、`/lyrics/`、`/healthz` 匿名。
- **公网**：Lucky/nginx 只反代 8766 的 `/play/` 与 `/lyrics/`；不要反代 `/`、`/api/`、`/mcp`（`/` 就是 WebUI）。
- **WebUI 镜像**：默认 `:musicdl-webui`（musicdl + WebUI，约 906 MB）。需要网易/洛雪时把
  `FNMUSIC_SOURCES_IMAGE` 改成 `:latest` 全量镜像；内部端口仍是 8001/8004，不需要增加宿主机端口。
- **本地下载目录**：下载与边播边存写到容器内 `/music`，compose 已挂到宿主机 `./music`；
  改 `GW_MUSIC_DIR` 即可换目录。
- **桥接器**：只有使用小智“MCP 接入点（wss）”时才需要；不填 `MCP_ENDPOINT` 会一直重连，不影响网关。

### 升级

~~~bash
docker compose pull
docker compose up -d
~~~

镜像标签、`.env` 与数据卷不变；升级不会删除 `data/`、`sources-data/`、`sources-repo/`、`music/`。

## 环境变量

### 网关与缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_HOST` | 0.0.0.0 | 监听地址 |
| `GW_PORT` | 8766 | 唯一端口（REST + MCP + WebUI，**只留内网**） |
| `GW_PUBLIC_BASE` | 空=按请求 Host 推导 | **设备能访问的地址**，如 `https://fnmusic.example.com`；单端口版不需要另开端口 |
| `GW_URL_TTL` | 900 | 直链有效期（秒） |
| `GW_SIGN_SECRET` | 空=自动生成 `/data/sign.key` | 签名密钥，多副本部署要一致 |
| `GW_DATA` | `./data` | 状态/密钥/token/歌词落盘目录 |
| `GW_CACHE_DIR` | `$GW_DATA/cache` | MP3 缓存目录 |
| `GW_CACHE_MAX_BYTES` | 8 GiB | 缓存上限，超了按最久未用淘汰 |
| `GW_MP3_BITRATE` | 128k | 转码码率 |
| `GW_MP3_SR` | 44100 | 采样率 |
| `GW_MP3_CHANNELS` | 2 | 声道数 |
| `GW_PREWARM` | 1 | `/api/play` 返回前先转码，设备连上即可读 |
| `GW_MAX_LIMIT` | 50 | 单次查询返回条数上限（LLM 传再大也会被收敛） |
| `GW_TRANSCODE_CONCURRENCY` | 2 | 同时转码数上限，防止多设备点播把宿主机 CPU 打满 |
| `GW_LOG_LEVEL` | INFO | 日志级别 |
| `GW_MCP_TIMEOUT` | 300 | 网关转发到官方 MCPServer 内部回环服务的超时（秒） |
| `GW_ADMIN_TOKEN` | 空=不鉴权 | 管理令牌；设置后除 `/play/`、`/lyrics/`、`/healthz` 外的接口都要带 `X-Admin-Token` 或 `Authorization: Bearer`。单端口 compose 强制设置；WebUI 首次访问用 `?token=` 换取 Cookie |
| `GW_WEBUI_URL` | 空=关闭 | WebUI 上游地址；compose 默认 `http://fnmusic-sources:8004` |
| `GW_WEBUI_TIMEOUT` | 30 | WebUI 反代超时（秒） |
| `GW_WEBUI_BODY_CAP` | 32 MiB | WebUI 反代请求体上限（字节） |

### 飞牛音乐（可选，优先级最高）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_FNOS_BASE` | 空 | 飞牛音乐地址，如 `http://192.168.1.10:5666`；也可在网页「网关设置」里填 |
| `GW_FNOS_USER` | 空 | 登录账号 |
| `GW_FNOS_PASS` | 空 | 登录密码（仅本地保存） |
| `GW_FNOS_DEVICE_ID` | 空 | 设备 ID，可留空 |
| `GW_FNOS_TIMEOUT` | 12 | 飞牛接口超时（秒） |
| `GW_FNOS_INSECURE_TLS` | 0 | 1=内网自签 HTTPS 不校验证书 |

网页保存的值（`/data/config.json`）优先于以上环境变量；这些变量只作首次启动默认值。

### 音质与本地存储

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_QUALITY_DEFAULT` | `standard` | 默认音质：`standard`(128k) / `high`(192k) / `exhigh`(320k) / `lossless`(下载原始音质，播放仍 320k MP3) |
| `GW_TEE_SAVE` | 1 | 在线歌曲播放时是否边播边存到 `GW_DOWNLOAD_DIR`；0=关闭 |
| `GW_LIBRARY_MAX_BYTES` | 0 | 本地下载目录容量上限（字节，0=不限）；超出后下载报错 |
| `GW_SOURCES_SEARCH_PARALLEL` | 1 | 多音源检索是否并行；0=串行 |

### 歌词（设备屏显）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_LYRICS` | 1 | 是否查歌词；0=关闭，`lyric_url` 一律留空 |
| `GW_LRCLIB_URL` | `https://lrclib.net/api/get` | 歌词源（LRCLIB 免费无 key，可自建） |
| `GW_LRCLIB_TIMEOUT` | 6 | 查歌词超时（秒） |
| `GW_LYRICS_DIR` | `$GW_DATA/lyrics` | LRC 缓存目录 |
| `GW_LYRICS_RETRY` | 86400 | 查不到歌词的重试间隔（秒） |

### 在线音源

飞牛音乐之外的兜底音源。配了飞牛时先查飞牛、本地不足再并行问这些源；都没配时请求会明确报「未配置任何音源」。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_FALLBACK_URLS` | 空 | 在线音源，逗号分隔；默认 compose 已设为 `musicdl@http://fnmusic-sources:8001`，也可只写 URL 按端口推断类型 |
| `GW_FALLBACK_TIMEOUT` | 8 | 在线音源请求超时（秒） |
| `GW_FALLBACK_SEARCH_LIMIT` | 10 | 每个在线音源最多取几条 |
| `GW_FALLBACK_TOTAL_LIMIT` | 50 | 所有音源合并后的总结果上限 |
| `GW_FALLBACK_TOTAL_TIMEOUT` | 15 | 多源检索总超时预算（秒），到点返回已有结果 |
| `GW_DOWNLOAD_DIR` | 空 | 下载与边播边存的落盘目录；不配则下载/边播边存关闭 |
| `GW_PREVIEW_MIN_SECONDS` | 35 | 转码后时长低于此值判为试听片段（很多在线源未登录只给 30 秒） |

### 搜索与心情

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_SMART_SEARCH` | 1 | 搜索是否做别名纠错 + 相关度排序；0=直接用音源返回顺序 |
| `GW_ALIASES_FILE` | `$GW_DATA/music_aliases.json` | ASR 同音字/错字纠正表（示例见 `data/music_aliases.json.example`，改完自动重载） |
| `GW_MOODS_FILE` | `$GW_DATA/moods.json` | 心情映射表 |

### 离线自测

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_MOCK` | 0 | 1=用内置假曲库自测，不连任何音源 |

## 接到小智

| 你的情况 | 建议方式 |
|---|---|
| 小智服务端支持全局 MCP 配置，想最小改动 | **方式 A：服务端 MCP** |
| 想一次工具调用完成“搜歌 + 播放”，且能改插件 | **方式 B：插件调 REST（推荐）** |
| 已有“小智 MCP 接入点”（wss 地址），不想改服务端配置 | **方式 C：MCP 接入点 + 桥接器** |

### 方式 A：服务端 MCP（全局，零 Java 改动）

把 `examples/mcp_server_settings.snippet.json` 的内容并进
`main/xiaozhi-server/data/.mcp_server_settings.json`，重启服务端。
`mcp_manager.py` 启动时读这个文件，把网关的 12 个 `music_*` 工具挂到所有智能体上。

### 方式 B：插件调 REST（推荐，能一次调用完成“查歌 + 播放”）

自建 xiaozhi-server **一轮只执行一个工具**（工具返回后走 `Action.REQLLM` → 纯文本，
不再进 function_call），所以“LLM 先调 MCP 拿直链、再调设备工具”走不通。
把 `examples/xiaozhi_plugin_fnmusic_gw.py` 放到
`main/xiaozhi-server/plugins_func/functions/`，插件内部直接：

~~~python
GET /api/play?query=晴天
-> {"url": "...", "title": "晴天", "artist": "周杰伦", "lyric_url": "..."}
call_mcp_tool(conn, conn.mcp_client, "self.music.play_url", {
    "url": url,
    "title": data["title"],
    "artist": data["artist"] or "unknown",
    "lyric_url": data.get("lyric_url") or "",
})
~~~

插件读环境变量 `FNMUSIC_GW_BASE`（默认 `http://NAS_IP:8766`），可在 xiaozhi 容器里设；网关设置了 `GW_ADMIN_TOKEN` 时插件也要带 `X-Admin-Token`。

### 方式 C：小智 MCP 接入点（wss，已有接入点时用这个）

如果你的小智已经有「MCP 接入点」地址，形如
`wss://xxx:59826/mcp_endpoint/mcp/?token=...`，那它就是一个**消息转发中心**：
xiaozhi-server 连上去要工具，工具端也要连上去供工具。工具端必须主动外连，
所以不需要公网 IP，也不用改 xiaozhi-server 配置。

~~~
xiaozhi-server --WS--> 接入点 <--WS-- fnmusic-bridge --HTTP--> fnmusic-gateway
~~~

bridge 已经打进同一个镜像（`bridge/mcp_pipe_http.py`，只依赖 `websockets`），
compose 里已经包含 `fnmusic-bridge` 服务（填 `MCP_ENDPOINT` 就会启动）：

~~~bash
export MCP_ENDPOINT='wss://xxx:59826/mcp_endpoint/mcp/?token=...'
docker compose up -d            # fnmusic-gateway + fnmusic-bridge
docker logs -f fnmusic-bridge   # 看到 "接入点已连接" 和 "tools/list -> 12 个工具" 即成功
~~~

不填 `MCP_ENDPOINT` 时桥接器会一直重连、不影响网关本体。

接入点会把同一 token 下所有工具端的工具**合并**成一张表，所以：

- 一个 token 只跑一个 fnmusic-bridge，重复连会看到重复工具；
- 想换工具集，直接重启 bridge（接入点会重新拉 tools/list）。

本地自测（不用真接入点，用假 broker 跑一遍 initialize/tools/list/ping/tools/call）：

~~~bash
python tests/bridge_selftest.py    # 需网关已在 127.0.0.1:8799 以 mock 模式运行
~~~

### 两个必须注意的挂载

1. **插件文件不会自动进容器**：小智官方 compose 只挂了 `./data` 和
   `models/SenseVoiceSmall/model.pt`，`plugins_func/` 在镜像里。方式 B 要加一条挂载：

   ~~~yaml
   volumes:
     - ./data:/opt/xiaozhi-server/data
     - ./plugins_func:/opt/xiaozhi-server/plugins_func   # 新增
   ~~~

   并把 `examples/xiaozhi_plugin_fnmusic_gw.py` 放到宿主机 `main/xiaozhi-server/plugins_func/functions/`。
2. **方式 A 的 MCP 配置**在容器内是 `/opt/xiaozhi-server/data/.mcp_server_settings.json`，
   对应宿主机 `main/xiaozhi-server/data/.mcp_server_settings.json`（已挂载，直接改宿主机那份即可）。

## 播放时显示歌名 / 歌手 / 歌词

固件 `self.music.play_url` 不只接受 `url`，还接受 `title` / `artist` / `lyric_url`
（见 `xiaozhi-esp32-zhj` 的 `main/mcp_server.cc`）。只传 `url` 时屏幕上是 `unknown`、没有歌词。

网关 1.3.0 起：`/api/play` 的 JSON 多了 `lyric_url` 与 `device_args`，MCP `music_play` 也会把
歌名、歌手、歌词直链一起吐给 LLM，要求它调 `self.music.play_url` 时四个参数都带上。

歌词来源与格式：

- 源是 [LRCLIB](https://lrclib.net)（免费、无 key、无需登录），按 `歌名 + 歌手` 查。
- 优先用 `syncedLyrics`（带 `[mm:ss.xx]` 时间轴）。只有 `plainLyrics` 时，网关按 4 秒一行
  自动补时间戳——**固件只认带时间戳的行，纯文本会被整行丢掉**。
- LRC 缓存在 `$GW_DATA/lyrics/<guid>.lrc`；查不到会写一个空文件，`GW_LYRICS_RETRY`（默认 1 天）
  后才会重试，避免每次播放都卡在查歌词上。
- 纯音乐 / 查不到 → `lyric_url` 为空串，插件就不传该参数，屏幕只显示歌名与歌手。
- 歌词直链与音频直链同一套 HMAC 签名，同样受 `GW_URL_TTL` 限制；公网反代同样放行 `/lyrics/`。

示例插件（`examples/xiaozhi_plugin_fnmusic_gw.py`）已经把这些参数带上：

~~~python
args = {"url": url, "title": data["title"], "artist": data["artist"] or "unknown"}
if data.get("lyric_url"):
    args["lyric_url"] = data["lyric_url"]
_device(conn, DEVICE_PLAY, args)   # self.music.play_url
~~~

自测：

~~~bash
curl -s "http://127.0.0.1:8766/api/play?query=%E6%99%B4%E5%A4%A9" \
  | python -c "import json,sys;d=json.load(sys.stdin);print(d['lyric_url'])"
curl -s "<上一步的 lyric_url>" | head -3   # 期望 [00:29.36] 故事的小黃花 ...
~~~

## 在线音源

网关只做「搜索 → 转码 → 签名直链」；飞牛音乐优先，其余由 [fnos_music_ext](https://github.com/12hgl/fnos_music_ext)（或自建 fork）的容器提供。默认 `docker-compose.yml` 已经在内部网络里接好 musicdl 单源，不需要改：

~~~bash
# 单源：type@url；type 省略时按端口推断
GW_FALLBACK_URLS="musicdl@http://fnmusic-sources:8001"
# 多个源用逗号分隔，每项写成 type@url（需要 :latest 全量镜像时再配）
~~~

默认 compose 中音源容器只在内部网络 `fnmusic` 里监听，宿主不映射端口；容器内端口与类型对应：
musicdl `8001`、musicbox `8002`、lxmusic `8003`、neteasefree `8005`。
`/healthz` 的 `fallback` 字段、`/api/sources`、MCP `music_sources` 都会列出当前配置的源与健康状态。

行为：

- **搜索**：并行问所有在线音源，按「歌名+歌手」跨源去重。
  在线结果的 `guid` 形如 `ext:<svc>:<原始 id>`，`source` 标明来自哪个源。
- **播放**：`/api/play` / MCP `music_play` 对在线歌曲同样出签名直链，`source` 为在线源名；
  网关按需拉流 → ffmpeg 转 MP3 → 落缓存，设备端无感。
- **试听识别**：转码后时长 < `GW_PREVIEW_MIN_SECONDS`（默认 35 秒）判为试听片段，
  `preview=true`、`duration=N`，`music_play` 会提示「这是试听片段，想听完整版就说收藏」。
- **收藏即下载**：`music_favorite(action=add)` / `music_download` / `POST /api/download` 会把在线歌曲按源给的最高音质原样落到
  `GW_DOWNLOAD_DIR`，文件名 `歌名 - 歌手.<flac|mp3>`。
- **账号类接口不可用**：歌单、收藏夹、本地曲库浏览属于账号数据，网关不提供；
  `/api/playlists`、`/api/favorites`、`music_browse` 等在未配飞牛音乐时返回明确提示（400 / 工具错误），不会静默给空列表。
- **歌词**：在线源自带歌词优先（musicdl `/info`、lx `/api/v1/track/lyric`、其余 `/api/v1/song/{id}/lyric`），
  没有再回退 LRCLIB，仍然走同一套签名 `/lyrics/` 直链。

注意：在线源的 `id` 有 TTL（进程内存缓存），解析失败时网关会用「歌名+歌手」重搜一次；
单源同一时刻通常只能跑一个（supervisor 互斥），多源配置时靠 `/healthz` 探测选活的。

## 公网：只反代 8766 的 /play/ 与 /lyrics/

整个服务只有一个监听端口 **8766**。公网只让 Lucky/nginx 转发两个路径前缀：

| 请求 | 是否反代到公网 |
|---|---|
| `GET/HEAD /play/<guid>.mp3?exp=&sig=` | 是（支持 Range） |
| `GET/HEAD /lyrics/<guid>.lrc?exp=&sig=` | 是 |
| `/`、`/api/*`、`/mcp`、`/app/fnmusic-ext/*`、`/healthz`、`/readyz` | **否**（`/` 即 WebUI） |

Lucky 配置要点：

- 前端地址：`https://fnmusic.example.com:59826`
- 后端地址：`http://192.168.50.100:8766`
- 路径规则：只放行 `/play/*` 与 `/lyrics/*`；其余路径直接拒绝
- **不要**把 `/`（WebUI）反代到公网；WebUI 走内网或 VPN
- `GW_PUBLIC_BASE` 可留空（按请求 Host 推导），也可显式设成公网域名，设备直链会用它

验证（应全部符合上表）：

~~~bash
curl -s -o /dev/null -w '%{http_code}' https://fnmusic.example.com/healthz; echo
curl -s -o /dev/null -w '%{http_code}' https://fnmusic.example.com/api/playlists; echo
curl -s -o /dev/null -w '%{http_code}' https://fnmusic.example.com/; echo   # WebUI，应 401/403
curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://fnmusic.example.com/mcp; echo
~~~

> 即使反代规则配错，管理接口仍需 `GW_ADMIN_TOKEN`；但正确做法仍是只转发 `/play/` 与 `/lyrics/`。

### hairpin：家里设备拉公网域名会失败怎么办

`GW_PUBLIC_BASE` 是公网域名后，**在家的设备也会去拉这个公网域名**。如果路由器不支持
NAT 回环（hairpin），表现是“设备说正在播放，但没有声音”。两种解法任选：

1. 在 Lucky 或内网 DNS 里把该域名解析到 `192.168.50.100`（内外都通，推荐）；
2. 或让路由器/防火墙放行内网访问公网 IP 的回环。

> 固件对 `self.music.play_url` 的限制：**URL ≤ 2048 字节、仅 http/https、不能含空格和 `@`**。

## 接口速查

### REST

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针：只证明进程活着，不探音源；含 `mode`（mock/fnos/fnos+online/online）与 `fnos` 配置状态 |
| GET | `/readyz` | 就绪探针：优先真连飞牛取 1 首，失败再看在线音源；都不可用返回 503 |
| GET | `/api/search?q=&limit=` | 搜歌（含别名纠正与排序） |
| GET | `/api/playlists` | 列出飞牛音乐歌单（未配飞牛返回 400） |
| GET | `/api/playlist?name=&limit=` | 按歌单名列出飞牛曲目（未配飞牛返回 400） |
| GET | `/api/favorites?limit=` | 列出飞牛收藏（未配飞牛返回 400） |
| GET | `/api/mood?mood=&limit=` | 按心情/场景选曲 |
| GET | `/api/play?query=\|mood=\|playlist=&index=&quality=` | 取签名直链；主要字段 `url/title/artist/lyric_url/quality/bitrate/device_args/source/preview/duration` |
| GET | `/api/status` | 上一次播放的是什么 |
| GET | `/api/sources` | 各在线音源探活结果 |
| GET | `/api/storage` | 缓存与本地下载目录用量 |
| GET | `/api/quality` | 可用音质档位 |
| GET | `/api/settings/fnos` | 读取飞牛配置（密码只回 `password_set`）与当前模式 |
| PUT | `/api/settings/fnos` | 保存飞牛配置（空密码=不修改，base 必须 http(s)） |
| POST | `/api/settings/fnos/test` | 用当前/提交的配置真实登录并试取 1 首，返回 `{ok,ms,tracks|error}` |
| POST | `/api/settings/fnos/logout` | 删除缓存的飞牛 token |
| POST | `/api/favorite` | body `{"action":"add\|remove","query":"歌名"}`（也可传 `guid`）；在线歌曲 add 时按最高音质下载到本地下载目录 |
| POST | `/api/warm` | body `{"guid":"...","quality":"high"}`，预转码到缓存 |
| POST | `/api/download` | body `{"guid":"..."}` 或 `{"query":"歌名","quality":"lossless"}`，下载到本地下载目录 |
| POST | `/mcp` | MCP streamable-http（JSON-RPC） |

### MCP 工具（12 个）

| 工具 | 用途 |
|---|---|
| `music_search` | 搜歌：飞牛优先，本地不足再用在线音源，跨源去重排序 |
| `music_browse` | 浏览飞牛曲库：全部歌曲/专辑/歌手/歌单（未配飞牛返回提示） |
| `music_playlists` | 列出飞牛音乐歌单（未配飞牛不可用） |
| `music_playlist_tracks` | 按歌单名列出飞牛曲目（未配飞牛不可用） |
| `music_favorites` | 列出飞牛收藏（未配飞牛不可用） |
| `music_mood` | 按心情/场景选曲 |
| `music_play` | 拿直链 + 歌名/歌手/歌词直链 + 音质，提示调用设备 `self.music.play_url`；可选 `quality` |
| `music_favorite` | 配了飞牛时直接写飞牛收藏；在线曲目 add 时按最高音质下载到本地下载目录 |
| `music_status` | 上一次播放的是什么（含音质） |
| `music_download` | 显式下载在线歌曲到本地下载目录，可指定音质 |
| `music_sources` | 查看在线兜底音源可用性 |
| `music_storage` | 查看播放缓存与本地下载目录用量 |

## 自检与验收

### 探针

| 探针 | 语义 | 失败时 |
|---|---|---|
| `GET /healthz` | 存活：只证明网关进程活着，不探音源（音源掉线时缓存仍能服务） | 进程挂了 |
| `GET /readyz` | 就绪：优先真连飞牛，失败再看在线音源 | 返回 503 + 原因（未配置音源 / 飞牛连不上 / 源不可用） |

### 直链自测

~~~bash
# 1) 拿直链
curl -s "http://127.0.0.1:8766/api/play?query=%E6%99%B4%E5%A4%A9" | python -m json.tool
# 2) 验证可播 + 参数正确
curl -s -o /tmp/t.mp3 "<上一步的 url>"
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,bit_rate -of default=nw=1 /tmp/t.mp3
# 期望 codec_name=mp3 sample_rate=44100 channels=2 bit_rate=128000
# 3) 过期/篡改应 403
curl -s -o /dev/null -w '%{http_code}\n' "<把 sig 改一位的 url>"
~~~

### 测试

~~~bash
python -m py_compile app.py
python tests/test_gateway.py -v   # 84 个用例：签名/直链/Range/MCP/REST/参数校验/缓存/歌词/搜索排序与别名/在线音源/飞牛设置持久化与测试连接/流式落盘，全离线（mock）
python tests/bridge_selftest.py   # 桥接器：假接入点跑 initialize/tools/list/ping/tools/call
~~~

`test_gateway.py` 进程内起服务，不需要 NAS 也不需要 Docker；有 ffmpeg 时还会验转码缓存不留残片。

分级验收记录见 [docs/acceptance-2.0.md](docs/acceptance-2.0.md)（P0→P2 逐项自测与结论）；2026-11-19 全项目审计与修复记录见 [docs/audit-2026-11-19.md](docs/audit-2026-11-19.md)；2026-11-20 MCP 服务层迁移到官方 MCPServer 2.3 见 [docs/migration-mcp-2.3.md](docs/migration-mcp-2.3.md)。

### 已验证（2026-11-19，mock + 真实 LRCLIB）

| 项 | 结果 |
|---|---|
| 官方 MCP SDK 3.12 客户端（streamable-http） | initialize 成功（协商 2025-11-25）、tools/list 12 个工具、call_tool 正常 |
| 2.1.0 官方 MCPServer 2.3（mock） | 内部 uvicorn 随机回环端口启动成功；initialize / tools-list / tools-call / notification / 坏 JSON 全通过；66 用例全绿 |
| `/api/play?query=晴天` | 返回签名直链 + 预热 65244 字节 |
| 直链下载 | 200，`ffprobe`：mp3 / 44100 / 2ch / 128000 |
| 篡改 sig / 过期 | 403 |
| `Range: bytes=0-99` | 206，100 字节 |
| `DELETE /mcp`（会话关闭） | 204，无告警 |
| 同端口 WebUI（mock 上游） | `/` 直接返回管理台 HTML（无 token 401）；`/?token=` 302 回 `/` + HttpOnly Cookie；`/app/fnmusic-ext/static|api` 反代成功，上游收到 `X-Trim-IsAdmin: true`；POST/PUT 正常 |
| 歌词链路（真实 LRCLIB） | `/api/play?query=晴天` → `lyric_url`；拉取 200 `text/plain; charset=utf-8`，首行 `[00:29.36] 故事的小黃花`（带 LRC 时间戳，固件才认） |
| 多音源兜底（mock 网关 + 假 musicdl 上游） | `/api/search?q=七里香` → `ext:musicdl:s7`；`/api/play` → source/preview=true/duration=4.0；直链 200 |
| 2.0 音质 `high`（mock） | `/api/play?query=晴天&quality=high` → `bitrate=192k`，直链 `q=high`，ffprobe `bit_rate=192000` |
| 2.0 下载与存储（mock） | `POST /api/download` 落盘成功；`GET /api/storage` 返回 cache/library 用量 |
| `tests/test_gateway.py`（84 用例，含官方 MCPServer 2.3、管理令牌、飞牛设置页与纯在线回退） | 全绿：签名/过期/篡改、Range 206、MCP initialize+12 工具+call_tool、参数校验、转码不留残片、歌词、搜索别名与排序、在线音源、飞牛配置读写/测试连接/登出、音质档位与缓存分键、新 REST |

真实音源容器与真实飞牛音乐仍需在 NAS 同网段按上面的「直链自测」验收一次（默认 compose 里的 `fnmusic-sources` 只在内网监听，开发机直连不到；飞牛可在网页「网关设置」里点测试连接确认）。

## 已知限制

- **播歌时设备断 WS**：`/api/play` 这条路服务端看不到播放状态，也发不出 stop。
  要“定时停 / 语音打断”请继续用 `fnmusic.py` 的 TTS FILE 通道（`POST /api/play` 只是拿到 URL，播放控制权在设备）。
- **只支持 MP3**：固件 `self.music.play_url` 的播放任务硬编码 `ESP_AUDIO_SIMPLE_DEC_TYPE_MP3`
  （`main/application.cc`），Ogg/Opus 直链放不了，所以网关不提供 `?fmt=opus`。
- **无封面与进度条**：固件只显示歌名、歌手和滚动歌词。
- **单实例**：缓存与签名密钥都在本地，多副本要共享 `/data`。
- **在线音源依赖上游**：未登录时常只给试听片段，且单源同一时刻通常只能跑一个；用多源时靠 `/healthz` 选活。
- **管理面默认无鉴权**：不设 `GW_ADMIN_TOKEN` 时 `/api/*`、`/mcp`、WebUI 全部裸奔；单端口 compose 强制要求设置。公网只反代 `/play/` 与 `/lyrics/`，WebUI 走内网/VPN。

## 仓库结构

~~~
app.py                    网关主体（HTTP/MCP 路由 + 依赖装配）
fnmusic_client.py         飞牛音乐 REST 客户端（token 缓存、直链取流）
gwlib/                    分层模块：errors/quality/storage/sources/playback/config/settings_page/mcp_tools/mcp_server
bridge/mcp_pipe_http.py   小智 MCP 接入点桥接器
examples/                 MCP 配置片段与 xiaozhi 插件
docs/refactor-2.0.md      2.0 改造方案（问题清单/架构/MCP/缓存/部署/验收/回滚）
docs/acceptance-2.0.md    2.0 分级落地验收报告（P0→P2 逐项自测）
tests/                    test_gateway.py（84 用例）、bridge_selftest.py
docker-compose.yml        单端口：网关 + 桥接器 + 音源 + 同端口 WebUI（只发布 8766）
~~~
