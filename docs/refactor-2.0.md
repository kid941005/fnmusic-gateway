# fnmusic-gateway 2.0 改造方案

> **历史文档**：2.3.0 起部署已收敛为单端口 8766，本文中的 8767、多份 compose 与音源宿主机端口只是 2.0 阶段的设计记录；现行部署见 [README](../README.md)。

> 目标：在不改变既有端口、服务名和设备播放方式的前提下，把网关从单文件大而全拆成可独立演进的分层模块，并补齐多音源聚合、音质选择、下载落盘和边播边存。

## 结论先行

- 不重写上游 fnos_music_ext，也不改小智固件；网关继续做协议翻译、媒体转码和直链签名。
- 2.0 已落地：`gwlib/` 六层拆分、12 个 MCP 工具、四档音质、`/api/download`、边播边存、`/api/sources`、`/api/storage`、`/api/quality`。
- 端口约束完全保持：8766 管理面只在内网，8767 公网面只放行 `/play/` 与 `/lyrics/`；full 版音源端口只绑 127.0.0.1，slim 版零端口。
- 设备侧仍只认 `self.music.play_url(url,title,artist,lyric_url)`，2.0 不改设备参数。

## 1. 问题清单

| 分类 | 问题 | 影响 | 优先级 | 2.0 处置 |
|---|---|---|---|---|
| 架构耦合 | `app.py` 1811 行，配置、缓存、音源适配、播放编排、MCP、HTTP 路由混在一起 | 改一处易伤全局，新增音源或音质要动播放代码 | P0 | 拆 `gwlib/errors / quality / storage / sources / playback / mcp_tools`，`app.py` 只做路由与装配 |
| 接口边界 | 播放逻辑同时在 `call_tool` 和 `_handle_api_play`，字段易漂移 | MCP 与 REST 返回不一致 | P0 | 统一 `PlaybackService.prepare()` 产出同一份 `device_args` |
| 播放链路 | 旧 `Mp3Cache` 只按 guid 缓存，无音质概念 | 无法同时缓存不同音质 | P0 | `MediaCache` 以 `guid / quality` 为键，直链带 `q` 参数 |
| 播放链路 | 在线源首次失败不换源 | 单源抽风即播放失败 | P1 | `PlaybackService._alternate()` 按歌名/歌手回退搜索其它源 |
| 音质策略 | 只有 `GW_MP3_BITRATE=128k` 一档 | 无法按设备或网络选音质 | P0 | 新增 `GW_QUALITY_DEFAULT` 与四档 profile；设备仍输出 MP3 |
| 下载落盘 | 收藏即下载只覆盖收藏动作，且无容量上限 | 无法主动下载，磁盘可能写满 | P1 | 新增 `music_download` / `POST /api/download`；`GW_LIBRARY_MAX_BYTES` 超限报错 |
| 下载落盘 | 在线播放不落盘，下次重新拉流 | 弱网重复缓冲 | P1 | `GW_TEE_SAVE` 边播边存源文件 |
| 多源聚合 | 在线源串行探测，慢源拖慢整体 | 搜歌响应随源数量线性变差 | P1 | `FallbackProvider` 线程池并行（最多 4 源），本地优先、跨源去重 |
| 部署端口 | 6 个端口暴露，音源管理面易误暴露 | 公网攻击面大 | P0 | full 版音源端口只绑 127.0.0.1；slim 版零端口；8767 只反代播放/歌词 |
| 部署端口 | WebUI 8774 前端写死跳转飞牛桌面 5666 | SSH 隧道访问会打不开 | P2 | 文档给改映射端口或走桌面入口两种方案，不硬改上游源码 |
| 可观测 | 无音源健康、缓存用量、曲库用量查询 | 排障靠猜 | P1 | `music_sources` / `music_storage` / `GET /api/sources` / `/api/storage` / `/api/quality` |
| 集成兼容 | MCP 工具名和参数变化会破坏小智提示词 | 升级后设备不会调新工具 | P0 | 保留原 9 工具名称和参数，仅追加可选 `quality` 与 3 个新工具 |

## 2. 架构与解耦设计

### 2.1 分层

~~~text
设备 <--/play/-- HTTP 层(Handler: REST + MCP 路由)
小智 <--/mcp/---|
                     | call_tool
               MCP 工具层(mcp_tools: 12 schema)
                     |
               播放编排(playback: 选源/音质/缓存/直链/边播边存)
              /          |          \
       storage(缓存/曲库) sources(本地+多在线源) quality(档位)
              \          |          /
               errors(统一错误类型与 HTTP 映射)
~~~

### 2.2 模块职责与接口

| 模块 | 输入 | 输出 | 错误约定 | 复用/重写 |
|---|---|---|---|---|
| `gwlib/quality.py` | 音质（standard/high/exhigh/lossless/320/flac/无损） | profile{name,label,bitrate,sample_rate,channels,ext_quality,download_original} | 非法值回落 DEFAULT_QUALITY | 新写 |
| `gwlib/storage.py` MediaCache | guid, provider, quality, timeout | MP3 文件路径 | UpstreamError / StorageError | 由旧 Mp3Cache 重写 |
| `gwlib/storage.py` LibraryStore | track, ctype, raw | 落盘路径 | BadRequest（无目录/空流）、StorageError（超容量） | 新写，继承旧下载逻辑 |
| `gwlib/sources.py` ExtProvider | base, svc, timeout | 标准化 track / stream / lyrics | 网络异常转 UpstreamError | 迁移旧 ExtProvider |
| `gwlib/sources.py` FallbackProvider | urls, timeout, search_limit, parallel | 合并 track / stream / lyrics / download | 上游全失败 NoSourceError | 迁移旧 FallbackProvider 并并行化 |
| `gwlib/playback.py` | track, provider, base, quality | 播放信息 dict（含 url/lyric_url/device_args） | 透传 GatewayError | 新写，替代两处重复逻辑 |
| `gwlib/mcp_tools.py` | 无 | 12 个工具定义 | 无 | 由旧 TOOLS 迁移扩展 |
| `app.py` | HTTP/MCP 请求 | JSON / 文件流 | GatewayError -> as_payload() | 路由保留，逻辑下沉 |

### 2.3 关键边界

- `playback` 不 import `app`：缓存、曲库、签名函数、歌词函数、时长探测、状态保存全部通过构造函数注入，便于单测。
- 新增在线音源：在 `sources.py` 实现 search/stream/lyrics，并在 `SERVICE_PORTS` 或 `parse_spec` 注册即可，播放层与 MCP 层零改动。
- 新增音质：只在 `quality.PROFILES` 增加一行，MediaCache、make_url、MCP schema 自动继承。
- 设备契约不变：无论音质档位，设备拿到的始终是 MP3 直链与 url/title/artist/lyric_url 四参数。

## 3. MCP 接口定义

### 3.1 传输与鉴权

- 传输：MCP Streamable HTTP，入口 `POST /mcp`，`DELETE /mcp` 关闭会话，JSON-RPC 2.0。
- 鉴权：MCP 不内置 token，依赖部署边界；8766 只在内网或宿主机回环可达，绝不映射公网；8767 只放行 /play/ 与 /lyrics/。
- 超时：小智桥接器默认 GW_TIMEOUT=120s；上游请求用 GW_FALLBACK_TIMEOUT（8s）与 GW_LRCLIB_TIMEOUT（6s）。
- 失败：工具异常转中文错误文本返回 isError；HTTP 面返回 {error:{code,message}} 与对应状态码。

### 3.2 工具清单（12 个）

| 工具 | 关键参数 | 返回 | 说明 |
|---|---|---|---|
| music_search | query, limit | 曲目列表 | 本地优先，在线兜底 |
| music_browse | kind, id/name, limit | 曲目/专辑/歌手/歌单 | 浏览飞牛曲库 |
| music_playlists | 无 | 歌单列表 | 自建歌单 |
| music_playlist_tracks | name, limit | 曲目列表 | 按歌单名取曲 |
| music_favorites | limit | 曲目列表 | 收藏 |
| music_mood | mood, limit | 曲目列表 | 心情/场景映射 |
| music_play | query/mood/playlist, index, quality? | url,title,artist,lyric_url,quality,bitrate,device_args,spoken | 设备播放主入口 |
| music_favorite | action, query/guid, quality? | 操作结果 | 在线曲 add 即下载，默认 lossless |
| music_status | 无 | 上次播放信息 | 含音质 |
| music_download | guid 或 query, quality | 保存路径/大小 | 显式下载到本地曲库 |
| music_sources | 无 | 各源 {service,source,url,ok} | 在线源探活 |
| music_storage | 无 | cache / library 用量 | 排障与容量检查 |

### 3.3 music_play 示例

~~~json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"music_play","arguments":{"query":"晴天","quality":"high"}}}
~~~

返回文本包含：

~~~text
正在播放：晴天 - 周杰伦（音质 high / 192k）
直链：http://192.168.50.100:8766/play/mock-1.mp3?exp=...&sig=...&q=high
歌词直链：http://192.168.50.100:8766/lyrics/mock-1.lrc?exp=...&sig=...
设备工具：self.music.play_url，参数 url=直链，title=晴天，artist=周杰伦，lyric_url=歌词直链（没有歌词就留空）
~~~

### 3.4 飞牛不可用降级顺序

1. provider() 返回 FallbackProvider（配置了 GW_FALLBACK_URLS 时）。
2. search：先查飞牛本地；异常或结果不足，再并行查在线源，按歌名+歌手去重补足。
3. music_play：PlaybackService.prepare 先转码取流；失败则 _alternate 用同歌名/歌手重新搜索并换源。
4. 在线流按 ext:<svc>:<id> 路由：musicdl 用 /stream?proxy=true，lxmusic 用 /api/v1/track/url，其它用 /api/v1/song/{id}/url。
5. 歌词优先上游歌词接口，空则回退 LRCLIB；仍无则 lyric_url 为空，设备只显示歌名/歌手。

## 4. 播放与缓存策略

### 4.1 选源策略

- 本地库优先：飞牛能搜到且能取流，就绝不出网。
- 在线兜底：只在本地无结果、取流失败、或用户指定在线 guid 时使用。
- 多源顺序：按 GW_FALLBACK_URLS 排列；本地优先 + 并行探测，健康源先返回。
- 换源重试：第一次失败后用标准化歌名+歌手再搜一次，换源重试一次，避免无限循环。

### 4.2 音质选择与降级

| 档位 | 播放（设备） | 下载（本地） |
|---|---|---|
| standard | MP3 128k / 44.1k / 2ch | 源文件原样 |
| high | MP3 192k / 44.1k / 2ch | 源文件原样 |
| exhigh | MP3 320k / 48k / 2ch | 源文件原样 |
| lossless | MP3 320k / 48k / 2ch（设备只解 MP3） | 原始 flac/mp3，不转码 |

- 请求音质非法时回落到 GW_QUALITY_DEFAULT，默认 standard。
- 在线源可能不支持高音质：ExtProvider._resolve_url 会把 lossless 映射到该源最高可用档，实际文件以源返回为准。

### 4.3 缓存

- 目录：GW_CACHE_DIR，默认 $GW_DATA/cache。
- 键：sha1(guid + "|" + quality_name)，同一首歌不同音质互不覆盖。
- 写入：先写 .part，ffmpeg 完成后 os.replace 原子改名；失败清理残片。
- 淘汰：MediaCache.evict() 按 atime 最久未用淘汰到 GW_CACHE_MAX_BYTES 的 90%。
- 预转码：GW_PREWARM=1 时 /api/play 返回前完成转码，设备连上即满速读。

### 4.4 下载与边播边存

- 目录：GW_DOWNLOAD_DIR，指向飞牛曲库目录；不配则下载和边播边存关闭。
- 命名：歌名 - 歌手.<ext>，冲突时追加序号；先写 .part 再原子改名。
- 边播边存：GW_TEE_SAVE=1 且播放的是在线曲时，PlaybackService 异步保存原始流，不阻塞设备首帧。
- 容量：GW_LIBRARY_MAX_BYTES>0 时，保存前检查已用量，超出返回 StorageError。
- 可选：保存后执行 GW_LIBRARY_SCAN_CMD 通知飞牛曲库扫描。

### 4.5 断点与并发

- 设备侧 Range 由 /play/ 的 _serve_file 支持：Range: bytes=0-99 返回 206，非法范围 416。
- 转码并发由 GW_TRANSCODE_CONCURRENCY（默认 2）限制，防止多设备点播打满 NAS CPU。
- 同一 guid|quality 的并发请求共用同一把锁/等待，不会重复转码。

## 5. 部署与端口

### 5.1 镜像构建与发布

- 工作流 .github/workflows/docker.yml：push main 或 tag v* 触发，构建 linux/amd64 + linux/arm64，推 GHCR。
- 版本标签：v2.0.0 生成 2.0.0、2.0、latest、sha-xxxxxxx。
- 本仓库只构建网关镜像；音源镜像由 refs/fnos_music_ext_fork/.github/workflows/publish-musicdl.yml 构建 :musicdl 与 :musicdl-webui。

### 5.2 compose 与端口

| 形态 | 文件 | 发布端口 | 音源网络 |
|---|---|---|---|
| 单网关 | docker-compose.yml | 8766 / 8767（host） | 无 |
| 全量多源 | docker-compose.full.yml | 8766 / 8767 + 127.0.0.1:8768/8770/8772/8774 | 宿主机回环 |
| 轻量多源 | docker-compose.slim.yml | 仅 8766 / 8767 | 内部 bridge fnmusic，音源零端口 |
| 轻量+WebUI | docker-compose.slim.yml + docker-compose.webui.yml | 8766 / 8767 + 127.0.0.1:8774 | 内部 bridge |

- 硬约束：8766 不得公网映射；8767 仅 Lucky 反代 /play/ 与 /lyrics/；其余路径 404。
- 公网 base：GW_PUBLIC_BASE=https://fnmusic-xiaozhi.hqgg.top:59826，Lucky 后端指向 192.168.50.100:8767。
- WebUI：8774 只绑 127.0.0.1；若被前端跳转到 5666，改映射 127.0.0.1:18774:8004 或从飞牛桌面 5666/app/fnmusic-ext/ 进入。

### 5.3 启动示例（slim）

~~~bash
mkdir -p data sources-data
chown -R 1000:1000 sources-data
docker compose -f docker-compose.slim.yml pull
docker compose -f docker-compose.slim.yml up -d
curl -s http://127.0.0.1:8766/healthz | python -m json.tool
~~~

## 6. 代码变更清单

### 6.1 新增文件

| 文件 | 作用 |
|---|---|
| gwlib/__init__.py | 包声明 |
| gwlib/errors.py | 统一错误类型与 HTTP 映射 |
| gwlib/quality.py | 音质档位 |
| gwlib/storage.py | MediaCache + LibraryStore |
| gwlib/sources.py | ExtProvider / FallbackProvider / source_status |
| gwlib/playback.py | 播放编排 |
| gwlib/mcp_tools.py | 12 个 MCP 工具定义 |

### 6.2 修改文件

| 文件 | 改动 |
|---|---|
| app.py | 版本 2.0.0；删除旧 BadRequest/Mp3Cache/ExtProvider/FallbackProvider/TOOLS；改用 gwlib；新增 /api/sources、/api/storage、/api/quality、POST /api/download；/api/play 支持 quality；/play 读取 q |
| tests/test_gateway.py | 44 → 53 用例，新增音质、缓存分键、新 REST、新 MCP 工具测试 |
| Dockerfile / Dockerfile.debian | 增加 COPY gwlib/ ./gwlib/ |
| docker-compose.yml / .full.yml / .slim.yml | 增加 GW_QUALITY_DEFAULT、GW_TEE_SAVE、GW_LIBRARY_MAX_BYTES |
| examples/xiaozhi_plugin_fnmusic_gw.py | 播放参数增加 quality |
| README.md | 版本 2.0.0、分层结构、音质/存储配置、12 工具、REST 速查 |
| CHANGELOG.md | 新增 2.0.0 |

### 6.3 验证命令

~~~bash
cd fnmusic-gateway
python -m py_compile app.py gwlib/*.py examples/xiaozhi_plugin_fnmusic_gw.py
python tests/test_gateway.py -v
python tests/bridge_selftest.py
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.full.yml config --quiet
docker compose -f docker-compose.slim.yml config --quiet
docker compose -f docker-compose.slim.yml -f docker-compose.webui.yml config --quiet
~~~

## 7. 自测与验收

### 7.1 离线 mock

~~~bash
FNMUSIC_MOCK=1 GW_LYRICS=0 python app.py
curl -s http://127.0.0.1:8766/api/quality | python -m json.tool
curl -s 'http://127.0.0.1:8766/api/play?query=晴天&quality=high' | python -m json.tool
curl -s -o /tmp/t.mp3 '<url>' && ffprobe -v error -show_entries stream=bit_rate -of default=nw=1 /tmp/t.mp3
~~~

### 7.2 四项可复现验收

| 验收项 | 复现命令 | 期望 |
|---|---|---|
| 多源聚合检索 | curl -s 'http://127.0.0.1:8766/api/search?q=晴天'（配置 GW_FALLBACK_URLS） | 本地结果在前，在线补齐，source 字段可见 |
| 在线播放 | curl -s 'http://127.0.0.1:8766/api/play?query=晴天' 后拉 url | 200 audio/mpeg，可 Range |
| 下载落盘 | curl -s -X POST http://127.0.0.1:8766/api/download -d '{"query":"晴天","quality":"lossless"}' | 返回保存路径与大小，文件在 GW_DOWNLOAD_DIR |
| 音质选择 | /api/play?query=晴天&quality=high 后 ffprobe | bit_rate=192000；standard 为 128000 |
| 边播边存 | GW_TEE_SAVE=1 播在线曲后查 /api/storage | library.files/bytes 增加 |
| MCP 完整播放 | tests/test_gateway.py 或 MCP 客户端 tools/call music_play | 返回 device_args 四参数与直链 |

### 7.3 真实环境

- 部署后 curl http://127.0.0.1:8766/readyz 应真实访问飞牛成功。
- 公网面：/play/、/lyrics/ 200；/healthz、/api/*、/mcp 全部 404。
- 设备实测：刷 xiaozhi-esp32-zhj 的设备播放时显示歌名/歌手/歌词；纯音乐无歌词属预期。

## 8. 风险与回滚

| 风险 | 触发条件 | 规避 | 回滚 |
|---|---|---|---|
| 上游更新覆盖 | 合并 javycoder/fnos_music_ext 时与 fork 改动冲突 | 改动集中在 Dockerfile 与 publish-musicdl.yml，尽量不碰上游业务文件 | git checkout <tag> 或回退镜像 tag |
| 音源接口变化 | 上游 /search /stream 字段改名 | 契约集中在 gwlib/sources.py，只改一处 | 回退网关版本或关闭 GW_FALLBACK_URLS |
| 缓存目录膨胀 | GW_CACHE_MAX_BYTES 过大或未设 | 默认 8 GiB 且 atime 淘汰 | 设小上限或清 GW_CACHE_DIR |
| 本地曲库写满 | GW_LIBRARY_MAX_BYTES=0 且长期下载 | 设置上限，监控 /api/storage | 删除下载文件 |
| 音质不生效 | 在线源最高只有 128k | lossless 仅下载保留原文件，播放以源为准 | 切回 standard |
| 设备不兼容 | 固件过旧不支持 lyric_url | 2.0 只传设备原生四参数 | 空 lyric_url 仍可播放 |
| WebUI 跳转 | 8774 被 JS 跳到 5666 | 改映射端口或走桌面入口 | 恢复 8774 映射 |
| 管理面误暴露 | 把 8766 映射公网 | 文档与 compose 均不映射，巡检 ss -ltnp | 删除公网映射并换签名密钥 |

## 待确认清单（不阻塞最小可行方案）

1. 目标音质档位是否就是 standard/high/exhigh/lossless 四档？默认 standard。
2. 本地曲库路径与容量上限：GW_DOWNLOAD_DIR 需手工指向飞牛曲库，容量默认 0（不限）。
3. 是否允许把 WebUI 端口从 8774 改为其它回环端口，绕过上游前端写死跳转？
4. 小智 MCP 协议版本是否固定为当前桥接器使用的 streamable-http 会话？
5. 镜像 tag 规则是否沿用 2.0.0 / 2.0 / latest / sha？

以上均有默认假设，不确认也能按本文最小方案部署。