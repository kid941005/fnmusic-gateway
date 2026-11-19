# fnmusic-gateway

[![docker](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml/badge.svg)](https://github.com/kid941005/fnmusic-gateway/actions/workflows/docker.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-fnmusic--gateway-blue)](https://github.com/kid941005/fnmusic-gateway/pkgs/container/fnmusic-gateway)
[![release](https://img.shields.io/github/v/release/kid941005/fnmusic-gateway)](https://github.com/kid941005/fnmusic-gateway/releases)

> 当前版本 **1.5.0**（2026-11-19） · [CHANGELOG](CHANGELOG.md)

飞牛音乐（fnOS Music）→ 小智 的播放网关。**一个进程，两个出口**：

| 出口 | 地址 | 谁用 | 用途 |
|---|---|---|---|
| MCP（streamable-http） | `POST /mcp` | 小智服务端 / 云端 LLM | 搜索、歌单、收藏、按心情选曲、拿直链 |
| 签名直链 | `GET /play/<guid>.mp3?exp=&sig=` | 设备 `self.music.play_url` | 设备直拉流，不经过小智服务端 |

另有 REST 面（`/api/*`）给小智插件或脚本用，避免它们去讲 MCP 协议。
歌词直链使用同一套签名：`GET /lyrics/<guid>.lrc?exp=&sig=`。

## 能做什么

- **本地曲库**：搜索、浏览歌曲/专辑/歌手/歌单、看收藏、按心情选曲。
- **设备可播**：飞牛曲库里的 FLAC/其他格式自动转 MP3，签发短时直链，支持 Range、HEAD 和预转码。
- **屏显信息**：播放时把歌名、歌手、歌词（LRC）一起交给设备，不再显示 `unknown`。
- **在线兜底**：本地没有的歌，按配置走在线音源（musicdl / 网易 / 洛雪），支持试听识别和“收藏即下载”。
- **听得更准**：中文同音字/错字纠正 + 相关度排序（青花磁 → 青花瓷）。
- **安全外网**：单独开一个只服务 `/play/` 与 `/lyrics/` 的公网端口，管理面不暴露。
- **多种接入**：9 个 MCP 工具、REST 插件、MCP 接入点（wss）桥接三种方式任选。

## 为什么需要这一层

1. **设备只解 MP3**，飞牛曲库多为 FLAC ⇒ 必须转码。
2. **飞牛 token 不能进 URL**（URL 会进设备日志、进 LLM 上下文）⇒ 直链用 HMAC 短时签名，token 留在网关里。
3. **设备要求 URL ≤ 2048 字节、仅 http/https、禁空白与 `@`** ⇒ 不能把签名参数塞一堆。
4. 首次点播就转码并落盘缓存，`/api/play` 返回前预热，设备连上就能满速读。

## 部署形态怎么选

| 形态 | compose 文件 | 容器 / 镜像 | 宿主机端口 | 音源 WebUI |
|---|---|---|---|---|
| **单网关**（只播本地曲库） | `docker-compose.yml` | 网关 + 桥接器，同一镜像 | 8766 / 8767（host 网络） | 无 |
| **全量多源** | `docker-compose.full.yml` | 网关 + 桥接器 + `fnos_music_ext:latest`（1.96 GB） | 8766 / 8767 / 8768 / 8770 / 8772 / 8774（后四个只绑 127.0.0.1） | 有 |
| **轻量多源** | `docker-compose.slim.yml` | 网关 + 桥接器 + `fnos_music_ext:musicdl`（827 MB） | **仅 8766 / 8767** | 无 |
| **轻量多源 + WebUI** | `docker-compose.slim.yml` + `docker-compose.webui.yml` | 音源换 `fnos_music_ext:musicdl-webui`（906 MB） | 仅 8766 / 8767 + 127.0.0.1:8774 | 有（只能管理 musicdl） |

- 桥接器只有在你使用“小智 MCP 接入点（wss）”时才需要；不填 `MCP_ENDPOINT` 它会一直重连，不影响网关本体。
- `full` 与 `slim` 二选一，不要同时起；WebUI 叠加文件只用于 `slim`。
- 只需要本地曲库时用最上面的默认 `docker-compose.yml`，最省资源。

### 最小可用：只播本地曲库

~~~bash
cd fnmusic-gateway
cp data/moods.json.example data/moods.json   # 可选，改心情映射
docker compose up -d
curl -s http://127.0.0.1:8766/healthz | python -m json.tool   # 存活
curl -s http://127.0.0.1:8766/readyz  | python -m json.tool   # 就绪（真访问曲库）
~~~

裸跑（本机调试 / 没有 Docker）：

~~~bash
FNMUSIC_MOCK=1 python app.py        # 离线模式，不需要 NAS，音频是本地 4 秒正弦波
python app.py                       # 真实模式，需要 .env 里的飞牛地址和账号
~~~

### 镜像与版本

每次推 `main` 或打 `v*` tag，GitHub Actions 会自动构建 `linux/amd64` + `linux/arm64` 并推到 GHCR。
以 `v1.5.0` 为例，会生成 `1.5.0`、`1.5`、`latest`、`sha-xxxxxxx` 四个标签。

~~~bash
docker pull ghcr.io/kid941005/fnmusic-gateway:1.5.0   # 生产建议锁定版本
docker pull ghcr.io/kid941005/fnmusic-gateway:latest  # 跟主线
~~~

两份 compose 默认跟 `latest`；想锁定版本或换本地镜像时用 `GW_IMAGE` 覆盖：

~~~bash
GW_IMAGE=ghcr.io/kid941005/fnmusic-gateway:1.5.0 docker compose up -d
GW_IMAGE=fnmusic-gateway:dev docker compose up -d   # 配合本地 build
~~~

镜像包默认 private：要么在 GitHub 包设置里改成 public，要么在 NAS 上
`docker login ghcr.io -u kid941005` 用 PAT（read:packages）登录。

> 需要把密码写进文件时，在 compose 同目录手工建 `.env`（键名见下面的环境变量表），
> compose 里已用 `${VAR:-默认值}` 形式读取，不建也能跑（密码留空则首次登录失败）。

## 多音源部署

### 全量版：musicdl / 网易 / 洛雪 + WebUI

~~~bash
mkdir -p data sources-data sources-repo
chown -R 1000:1000 sources-data sources-repo      # 容器内以 uid 1000 运行
docker compose -f docker-compose.full.yml pull
docker compose -f docker-compose.full.yml up -d
curl -s 127.0.0.1:8766/healthz      # 网关，fallback 应含 musicdl
curl -s 127.0.0.1:8768/healthz      # 音源（musicdl）
curl -s 127.0.0.1:8774/healthz      # 音源管理 WebUI
~~~

音源切换推荐用 WebUI（`http://127.0.0.1:8774`，SSH 隧道访问），保存后秒级生效；
WebUI 会把配置写进 `./sources-repo/.env`（.env 优先级高于 compose 里的 environment）。

### 轻量版：只开 2 个端口

音源容器不再映射任何宿主机端口，三个容器同处内部 bridge 网络 `fnmusic`，
网关用服务名 `http://fnmusic-sources:8001` 访问音源，只有网关把 8766/8767 发布出来。

~~~bash
mkdir -p data sources-data
chown -R 1000:1000 sources-data
docker compose -f docker-compose.slim.yml pull
docker compose -f docker-compose.slim.yml up -d
ss -ltnp | grep -E '876[0-9]'      # 只应看到 8766 / 8767
~~~

### 轻量版 + WebUI

~~~bash
mkdir -p sources-repo
printf 'FNMUSIC_MUSICDL_ENABLED=true\nFNMUSIC_WEBUI_ENABLED=true\nMUSICDL_SOURCES=KuwoMusicClient,MiguMusicClient\n' > sources-repo/.env
chown -R 1000:1000 sources-repo sources-data
docker compose -f docker-compose.slim.yml -f docker-compose.webui.yml up -d
# 访问 http://127.0.0.1:8774 （只绑本机回环，不对外）
~~~

限制：`:musicdl-webui` 镜像里没有 Node / 洛雪 / 网易运行时，WebUI 只能管理 musicdl；
网易云、洛雪相关按钮会显示不可用。需要三源全开就改用完整镜像 `docker-compose.full.yml`。

两个轻量镜像由本仓库 `.github/workflows/publish-musicdl.yml` 构建（多架构），
产出 `:musicdl`（无 WebUI）与 `:musicdl-webui`（带 WebUI），源码只含
`musicdl-service/`、`webui-service/` 与 WebUI 需要的 `proxy/` 模块，去掉 Node、ffmpeg 与另外三个音源。

> 两个轻量镜像已同步上游 3.1.1（musicdl 双引擎 `search_engines.py`、WebUI 官方聚合开关与歌单拖动排序）。
> 3.1.1 起 musicdl 新增实验开关 `FNMUSIC_MUSICDL_OFFICIAL_SEARCH`：本项目两份 compose 默认设为 `false`，
> 继续用带熔断、自适应超时、逐首探活的旧版聚合，避免兜底链路行为变化；想试上游官方聚合，
> 把它设为 `true`（或在 WebUI 里打开对应开关）即可。

## 环境变量

### 网关与缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_HOST` | 0.0.0.0 | 监听地址 |
| `GW_PORT` | 8766 | 管理端口（REST + MCP，**只留内网**） |
| `GW_PUBLIC_PORT` | 0=关闭（compose 设 8767） | 公网专用端口：**只开放 `/play/` 与 `/lyrics/`**，其余路径 404 |
| `GW_PUBLIC_BASE` | 空=按请求 Host 推导 | **设备能访问的地址**，如 `http://192.168.50.100:8766` |
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
| `GW_TRANSCODE_CONCURRENCY` | 2 | 同时转码数上限，防止多设备点播把 NAS CPU 打满 |
| `GW_LOG_LEVEL` | INFO | 日志级别 |

### 歌词（设备屏显）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_LYRICS` | 1 | 是否查歌词；0=关闭，`lyric_url` 一律留空 |
| `GW_LRCLIB_URL` | `https://lrclib.net/api/get` | 歌词源（LRCLIB 免费无 key，可自建） |
| `GW_LRCLIB_TIMEOUT` | 6 | 查歌词超时（秒） |
| `GW_LYRICS_DIR` | `$GW_DATA/lyrics` | LRC 缓存目录 |
| `GW_LYRICS_RETRY` | 86400 | 查不到歌词的重试间隔（秒） |

### 多音源兜底

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_FALLBACK_URLS` | 空=关闭 | 在线音源，逗号分隔，如 `musicdl@http://127.0.0.1:8768`；也可只写 URL，按端口推断类型 |
| `GW_FALLBACK_TIMEOUT` | 8 | 在线音源请求超时（秒） |
| `GW_FALLBACK_SEARCH_LIMIT` | 10 | 每个在线音源最多取几条 |
| `GW_DOWNLOAD_DIR` | 空 | 收藏在线歌曲的落盘目录（指向飞牛曲库目录）；不配则“收藏即下载”会报错 |
| `GW_LIBRARY_SCAN_CMD` | 空 | 下载完成后触发的曲库扫描命令（如 `fnmusic-ext scan`），可选 |
| `GW_PREVIEW_MIN_SECONDS` | 35 | 转码后时长低于此值判为试听片段（很多在线源未登录只给 30 秒） |

### 搜索与心情

| 变量 | 默认 | 说明 |
|---|---|---|
| `GW_SMART_SEARCH` | 1 | 搜索是否做别名纠错 + 相关度排序；0=沿用飞牛原始顺序 |
| `GW_ALIASES_FILE` | `$GW_DATA/music_aliases.json` | ASR 同音字/错字纠正表（示例见 `data/music_aliases.json.example`，改完自动重载） |
| `GW_MOODS_FILE` | `$GW_DATA/moods.json` | 心情映射表 |

### 飞牛账号

| 变量 | 默认 | 说明 |
|---|---|---|
| `FNMUSIC_BASE` | `http://192.168.50.100:59820` | 飞牛音乐地址 |
| `FNMUSIC_USER` | KID | 必须是 KID |
| `FNMUSIC_PASS` | 空 | 飞牛密码；登录后 token 落盘，之后可留空 |
| `FNMUSIC_DEVICE_ID` | 内置 | 换设备号会强制重新登录 |
| `FNMUSIC_TOKEN_FILE` | `$GW_DATA/fnmusic_token.json` | 登录 token 落盘位置 |
| `FNMUSIC_MOCK` | 0 | 1=离线自测 |

## 接到小智

| 你的情况 | 建议方式 |
|---|---|
| 小智服务端支持全局 MCP 配置，想最小改动 | **方式 A：服务端 MCP** |
| 想一次工具调用完成“搜歌 + 播放”，且能改插件 | **方式 B：插件调 REST（推荐）** |
| 已有“小智 MCP 接入点”（wss 地址），不想改服务端配置 | **方式 C：MCP 接入点 + 桥接器** |

### 方式 A：服务端 MCP（全局，零 Java 改动）

把 `examples/mcp_server_settings.snippet.json` 的内容并进
`main/xiaozhi-server/data/.mcp_server_settings.json`，重启服务端。
`mcp_manager.py` 启动时读这个文件，把网关的 9 个 `music_*` 工具挂到所有智能体上。

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

插件读环境变量 `FNMUSIC_GW_BASE`（默认 `http://192.168.50.100:8766`），可在 xiaozhi 容器里设。

### 方式 C：小智 MCP 接入点（wss，已有接入点时用这个）

如果你的小智已经有「MCP 接入点」地址，形如
`wss://xxx:59826/mcp_endpoint/mcp/?token=...`，那它就是一个**消息转发中心**：
xiaozhi-server 连上去要工具，工具端也要连上去供工具。工具端必须主动外连，
所以不需要公网 IP，也不用改 xiaozhi-server 配置。

~~~
xiaozhi-server --WS--> 接入点 <--WS-- fnmusic-bridge --HTTP--> fnmusic-gateway
~~~

bridge 已经打进同一个镜像（`bridge/mcp_pipe_http.py`，只依赖 `websockets`），
在 compose 里加一个服务即可（两份 compose 都已写好，填 `MCP_ENDPOINT` 就会启动）：

~~~bash
export MCP_ENDPOINT='wss://xxx:59826/mcp_endpoint/mcp/?token=...'
docker compose up -d            # fnmusic-gateway + fnmusic-bridge
docker logs -f fnmusic-bridge   # 看到 "接入点已连接" 和 "tools/list -> 9 个工具" 即成功
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
- 歌词直链与音频直链同一套 HMAC 签名，同样受 `GW_URL_TTL` 限制；公网面 `GW_PUBLIC_PORT` 同样放行。

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

## 多音源兜底（本地优先，在线兜底）

飞牛库里没有的歌，可以接到在线音源。默认**关闭**，设置 `GW_FALLBACK_URLS` 才启用：

~~~bash
# 单源：type@url；type 省略时按端口推断
GW_FALLBACK_URLS="musicdl@http://127.0.0.1:8768"
# 多源逗号分隔，按顺序探测（/healthz 不通的自动跳过）
GW_FALLBACK_URLS="musicdl@http://127.0.0.1:8768,musicbox@http://127.0.0.1:8770"
~~~

上游就是 [fnos_music_ext](https://github.com/12hgl/fnos_music_ext)（或自建 fork）的**音源容器**，
端口与类型对应：musicdl `8768→8001`、musicbox `8770→8002`、lxmusic `8772→8003`、neteasefree `8776→8005`。
`/healthz` 的 `fallback` 字段会列出当前配置的源。

行为：

- **搜索**：本地曲库结果优先；条数不够才追加在线结果，并按「歌名+歌手」跨源去重。
  在线结果的 `guid` 形如 `ext:<svc>:<原始 id>`，`source` 标明来自哪个源。
- **播放**：`/api/play` / MCP `music_play` 对在线歌曲同样出签名直链，`source` 为在线源名；
  网关按需拉流 → ffmpeg 转 MP3 → 落缓存，设备端无感。
- **试听识别**：转码后时长 < `GW_PREVIEW_MIN_SECONDS`（默认 35 秒）判为试听片段，
  `preview=true`、`duration=N`，`music_play` 会提示「这是试听片段，想听完整版就说收藏」。
- **收藏即下载**：对在线歌曲 `music_favorite(action=add)` 会按源给的最高音质原样落到
  `GW_DOWNLOAD_DIR`（指向飞牛曲库目录），文件名 `歌名 - 歌手.<flac|mp3>`；
  配了 `GW_LIBRARY_SCAN_CMD` 就顺手触发曲库扫描，之后这首歌就走本地库了。
- **歌词**：在线源自带歌词优先（musicdl `/info`、lx `/api/v1/track/lyric`、其余 `/api/v1/song/{id}/lyric`），
  没有再回退 LRCLIB，仍然走同一套签名 `/lyrics/` 直链。

注意：在线源的 `id` 有 TTL（进程内存缓存），解析失败时网关会用「歌名+歌手」重搜一次；
单源同一时刻通常只能跑一个（supervisor 互斥），多源配置时靠 `/healthz` 探测选活的。

## 公网播放：只反代 8767，绝不要反代 8766

`/api/*`、`/mcp`、`/healthz` **没有鉴权**。要设备在外网也能播，正确做法是开一个
**只服务 `/play/` 与 `/lyrics/` 的公网端口**，反代只指向它：

~~~yaml
  fnmusic-gateway:
    network_mode: host
    environment:
      GW_PORT: "8766"                                        # 管理面：REST + MCP，只留内网
      GW_PUBLIC_PORT: "8767"                                 # 公网面：只放行 /play/ 与 /lyrics/
      GW_PUBLIC_BASE: "https://fnmusic.example.com"          # 设备拿到的直链走这个地址
~~~

反代（Lucky / nginx / caddy）把整个域名转发到 `192.168.50.100:8767`，**不需要任何路径规则**：

| 请求 | 8767（公网面） | 8766（管理面） |
|---|---|---|
| `GET/HEAD /play/<guid>.mp3?exp=&sig=` | 200 / 206（支持 Range） | 200 / 206 |
| `GET/HEAD /lyrics/<guid>.lrc?exp=&sig=` | 200 `text/plain` | 200 `text/plain` |
| `/api/*`、`/mcp`、`/healthz`、`/readyz` | **404** | 200 |
| `POST` / `DELETE /mcp` | **404** | 200 / 204 |

验证（应全部符合上表）：

~~~bash
curl -s -o /dev/null -w '%{http_code}\n' https://fnmusic.example.com/healthz        # 404
curl -s -o /dev/null -w '%{http_code}\n' https://fnmusic.example.com/api/playlists  # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://fnmusic.example.com/mcp  # 404
~~~

> `GW_PUBLIC_PORT` 是进程内的路径判断，不依赖反代软件的子规则，配错也不会漏。

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
| GET | `/healthz` | 存活探针：只证明进程活着，不碰 NAS |
| GET | `/readyz` | 就绪探针：真访问一次曲库，异常返回 503 |
| GET | `/api/search?q=&limit=` | 搜歌（含别名纠正与排序） |
| GET | `/api/playlists` | 列出歌单 |
| GET | `/api/playlist?name=&limit=` | 按歌单名列出曲目 |
| GET | `/api/favorites?limit=` | 列出收藏 |
| GET | `/api/mood?mood=&limit=` | 按心情/场景选曲 |
| GET | `/api/play?query=\|mood=\|playlist=&index=` | 取签名直链；主要字段 `url/title/artist/lyric_url/device_args/source/preview/duration` |
| GET | `/api/status` | 上一次播放的是什么 |
| POST | `/api/favorite` | body `{"action":"add\|remove","query":"歌名"}`（也可传 `guid`） |
| POST | `/api/warm` | body `{"guid":"..."}`，预转码到缓存 |
| POST | `/mcp` | MCP streamable-http（JSON-RPC） |

### MCP 工具（9 个）

| 工具 | 用途 |
|---|---|
| `music_search` | 本地优先搜歌，自动纠错排序，可在线兜底 |
| `music_browse` | 浏览 track / album / artist / playlist |
| `music_playlists` | 列出我的自建歌单 |
| `music_playlist_tracks` | 按歌单名列出曲目 |
| `music_favorites` | 列出收藏 |
| `music_mood` | 按心情/场景选曲 |
| `music_play` | 拿直链 + 歌名/歌手/歌词直链，提示调用设备 `self.music.play_url` |
| `music_favorite` | 收藏/取消收藏；在线歌曲 add 时按最高音质下载 |
| `music_status` | 上一次播放的是什么 |

## 自检与验收

### 探针

| 探针 | 语义 | 失败时 |
|---|---|---|
| `GET /healthz` | 存活：只证明网关进程活着，不碰 NAS（NAS 掉线时缓存仍能服务） | 进程挂了 |
| `GET /readyz` | 就绪：真访问一次曲库 | 返回 503 + 原因（NAS 不通 / 账号错 / token 失效） |

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
python tests/test_gateway.py -v   # 44 个用例：签名/直链/Range/MCP/REST/参数校验/缓存/歌词/搜索排序与别名/多音源兜底，全离线（mock）
python tests/bridge_selftest.py   # 桥接器：假接入点跑 initialize/tools/list/ping/tools/call
~~~

`test_gateway.py` 进程内起服务，不需要 NAS 也不需要 Docker；有 ffmpeg 时还会验转码缓存不留残片。

### 已验证（2026-11-19，mock + 真实 LRCLIB）

| 项 | 结果 |
|---|---|
| 官方 MCP SDK 3.12 客户端（streamable-http） | initialize 成功（协商 2025-11-25）、tools/list 9 个工具、call_tool 正常 |
| `/api/play?query=晴天` | 返回签名直链 + 预热 65244 字节 |
| 直链下载 | 200，`ffprobe`：mp3 / 44100 / 2ch / 128000 |
| 篡改 sig / 过期 | 403 |
| `Range: bytes=0-99` | 206，100 字节 |
| `DELETE /mcp`（会话关闭） | 204，无告警 |
| 公网面 `GW_PUBLIC_PORT` | `/play/` 200 / Range 206 / HEAD 200、`/lyrics/` 200 `text/plain`；`/healthz`、`/readyz`、`/api/*`、`POST /mcp`、`DELETE /mcp` 全部 404 |
| 歌词链路（真实 LRCLIB） | `/api/play?query=晴天` → `lyric_url`；拉取 200 `text/plain; charset=utf-8`，首行 `[00:29.36] 故事的小黃花`（带 LRC 时间戳，固件才认） |
| 多音源兜底（mock 网关 + 假 musicdl 上游） | `/api/search?q=七里香` → `ext:musicdl:s7`；`/api/play` → source/preview=true/duration=4.0；直链 200 |
| `tests/test_gateway.py`（44 用例） | 全绿：签名/过期/篡改、Range 206、MCP initialize+9 工具+call_tool、参数校验、转码不留残片、歌词、搜索别名与排序、多音源兜底 |

真实飞牛曲库仍需在 NAS 同网段按上面的「直链自测」验收一次（开发机到 `192.168.50.100:59820` 不通）。

## 已知限制

- **播歌时设备断 WS**：`/api/play` 这条路服务端看不到播放状态，也发不出 stop。
  要“定时停 / 语音打断”请继续用 `fnmusic.py` 的 TTS FILE 通道（`POST /api/play` 只是拿到 URL，播放控制权在设备）。
- **只支持 MP3**：固件 `self.music.play_url` 的播放任务硬编码 `ESP_AUDIO_SIMPLE_DEC_TYPE_MP3`
  （`main/application.cc`），Ogg/Opus 直链放不了，所以网关不提供 `?fmt=opus`。
- **无封面与进度条**：固件只显示歌名、歌手和滚动歌词。
- **单实例**：缓存与签名密钥都在本地，多副本要共享 `/data`。
- **在线音源依赖上游**：未登录时常只给试听片段，且单源同一时刻通常只能跑一个；用多源时靠 `/healthz` 选活。
- **管理面无鉴权**：`8766` 只能留在内网；公网只反代 `8767`。

## 仓库结构

~~~
app.py                    网关主体（MCP + REST + 签名直链 + 歌词 + 多源兜底）
fnmusic_client.py         飞牛音乐客户端
bridge/mcp_pipe_http.py   小智 MCP 接入点桥接器
examples/                 MCP 配置片段与 xiaozhi 插件
tests/                    test_gateway.py（44 用例）、bridge_selftest.py
docker-compose.yml        单网关（host 网络）
docker-compose.full.yml   网关 + 全量音源 + WebUI
docker-compose.slim.yml   网关 + 轻量音源（只开 2 个端口）
docker-compose.webui.yml  给轻量音源叠加 WebUI
~~~
