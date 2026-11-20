# CHANGELOG

## 2.4.1 (2026-11-20)

### 变更：WebUI 改为根路径直达
- WebUI 不再需要 `/app/fnmusic-ext/` 前缀：`GET /` 直接反代音源管理台首页，首次访问 `http://NAS_IP:8766/?token=<GW_ADMIN_TOKEN>` 校验后写 HttpOnly Cookie 并 302 回 `/`。
- 上游管理台前端固化了 `/app/fnmusic-ext/static|api` 绝对路径，网关继续保留该前缀的反代供其静态资源与 API 使用；正常浏览地址就是根路径。
- `/` 归入受 `GW_ADMIN_TOKEN` 保护的入口（`/play/`、`/lyrics/`、`/healthz` 仍匿名）；公网反代规则里不要放行 `/`。
- 版本 2.4.0 → **2.4.1**；测试仍 77 个用例（WebUI 用例改为根路径挂载断言）。

## 2.4.0 (2026-11-20)

### 破坏性变更：去掉飞牛账号接管，改为纯在线音源模式
- 移除飞牛音乐账号登录：删除 `FnProvider`、`fnmusic_client.py`、`FNMUSIC_BASE` / `FNMUSIC_USER` / `FNMUSIC_PASS` / `FNMUSIC_DEVICE_ID` / `FNMUSIC_INSECURE_TLS` 与 `GW_LIBRARY_SCAN_CMD`；网关不再保存任何账号、密码或 token。
- `GW_FALLBACK_URLS` 成为唯一数据来源：配置后走在线音源（默认 compose 已接好 `musicdl@http://fnmusic-sources:8001`），未配置时请求明确报「未配置在线音源：请设置 GW_FALLBACK_URLS」，不再静默失败。
- `FallbackProvider(base=None)` 支持纯在线模式：搜索只并行问在线音源；歌单、收藏夹、本地曲库浏览等账号类接口抛 `BadRequest`（HTTP 400 / MCP 工具错误），不再返回空列表。
- 收藏/下载语义统一为「落本地下载目录」：`music_favorite(action=add)`、`music_download`、`POST /api/download`、`POST /api/favorite` 都会把在线歌曲按源给的最高音质存到 `GW_DOWNLOAD_DIR`（容器内默认 `/music`），文件名 `歌名 - 歌手.<flac|mp3>`。
- `/readyz` 语义改为探在线音源健康（有健康源即 200，否则 503 + 各源状态）；`/healthz` 新增 `mode` 字段（`online` / `mock`）。
- 本地下载目录从「飞牛曲库目录」改为普通目录：`GW_DOWNLOAD_DIR` 默认 `/music`，compose 卷改为 `${GW_MUSIC_DIR:-./music}:/music`。
- 离线自测变量 `FNMUSIC_MOCK` 改名为 `GW_MOCK`。
- `gwlib/storage.py` 的 `LibraryStore` 去掉 `scan_cmd_getter`（不再触发曲库扫描）。
- 版本 2.3.0 → **2.4.0**；测试 72 → **77 个用例**（新增纯在线模式 5 例）。

## 2.3.0 (2026-11-20)

### 破坏性变更：只保留单端口 + 同端口 WebUI
- 移除 `GW_PUBLIC_PORT`、`PublicHandler` 与第二个监听端口，网关只监听一个端口（默认 8766）；REST、MCP、播放直链、WebUI 全部同端口。
- WebUI 通过 `/app/fnmusic-ext/` 反代到音源容器（默认 `http://fnmusic-sources:8004`）：根路径 302；`?token=` 校验后写 HttpOnly Cookie 并 302 去掉 token；向上游注入 `X-Trim-IsAdmin: true`，浏览器不再需要手工加 Header。
- 新增 `GW_WEBUI_URL`（空=关闭）、`GW_WEBUI_TIMEOUT`（默认 30 秒）、`GW_WEBUI_BODY_CAP`（默认 32 MiB）。
- `docker-compose.yml` 合并为唯一部署文件：网关 + 桥接器 + 音源，只发布 8766；音源容器零宿主端口，默认镜像 `:musicdl-webui`。删除 `docker-compose.bridge.yml` / `full.yml` / `slim.yml` / `webui.yml` / `oneport.yml`。
- `GW_ADMIN_TOKEN` 在默认 compose 中为必填；`/play/`、`/lyrics/`、`/healthz` 保持匿名。
- 版本 2.2.0 → **2.3.0**；测试 70 → **72 个用例**（新增 WebUI 反代/Cookie/鉴权/POST/PUT，删除双端口用例）。

## 2.2.0 (2026-11-20)

### 新增：单端口部署模式（参考 Songloft / 柠檬音乐）
- 新增 `docker-compose.oneport.yml`：网关只发布 **8766** 一个宿主端口，管理接口与播放接口共用；音源容器不映射任何端口。
- 新增环境变量 `GW_ADMIN_TOKEN`：设置后除 `/play/`、`/lyrics/`、`/healthz` 外的接口都要求 `X-Admin-Token` 或 `Authorization: Bearer`；未设置时保持旧的无鉴权行为。
- 桥接器 `bridge/mcp_pipe_http.py` 支持 `GW_ADMIN_TOKEN`，转发 MCP 请求时自动带 `X-Admin-Token`。
- `/healthz` 增加 `auth_required` 字段；公网入口仍只建议反代 `/play/` 与 `/lyrics/`。
- README 新增「单端口版：只发布 1 个端口」章节与部署形态对照行；新增 `.env.example` 的 `GW_ADMIN_TOKEN` 示例。
- 版本 2.1.0 → **2.2.0**；测试 66 → **70 个用例**（新增管理令牌的公开路径/401/两种令牌头/播放匿名）。

## 2.1.0 (2026-11-20)

### 升级：MCP 服务层迁移到官方 MCPServer 2.3
- 新增 `gwlib/mcp_server.py`：用官方 `mcp>=2.3` 的 `MCPServer`（原 FastMCP）注册 12 个工具，工具参数 Schema 继续复用 `gwlib/mcp_tools.build_tools()`，保留 enum / anyOf / 中文描述。
- `/mcp` 改为反向代理官方 streamable-http 应用：官方应用只监听 `127.0.0.1` 随机端口，不新增对外端口，部署仍只有 8766/8767。
- 保留内置 JSON-RPC 作为兜底：官方依赖缺失或内部服务启动失败时自动回退，`/healthz` 的 `mcp_server` 字段会显示 `available/engine/error`。
- 新增环境变量 `GW_MCP_TIMEOUT`（默认 300 秒）；Dockerfile 与 Dockerfile.debian 增加 `mcp>=2.3,<3` 依赖。
- 版本 2.0.1 → **2.1.0**；测试保持 66 个用例全绿，bridge_selftest 4 项 PASS。

## 2.0.1 (2026-11-20)

### 优化：流式落盘、锁回收、单版本常量与检索预算
- 在线音频改为流式落盘（`ExtProvider.stream_to_path` / `_call_stream_to_path`），`MediaCache` 与 `LibraryStore` 不再把整首 FLAC/MP3 读进内存。
- `MediaCache._locks` 改用 `weakref.WeakValueDictionary`，长期运行时 guid 增多不再累积锁对象。
- 版本收敛到 `gwlib.__version__` 单一常量，`app.py`、`sources.py`、镜像标签与文档从同一处取值。
- `FallbackProvider` 新增总结果上限 `GW_FALLBACK_TOTAL_LIMIT`（默认 50）与总超时预算 `GW_FALLBACK_TOTAL_TIMEOUT`（默认 15 秒），并行/串行检索都受预算约束。
- 测试 60 → **66 个用例**（新增流式落盘、弱引用锁、单版本常量、总上限/总超时回归）。

### 修复：全项目审计发现的功能、安全与健壮性问题
- 管理类 MCP 工具不再强依赖飞牛可用：`call_tool` 使用惰性 provider，`music_sources` / `music_storage` / `music_status` 在 NAS 掉线时仍可用。
- `music_sources` 改用 `source_status` 实际返回的 `source` / `healthy` 字段，修复永远显示“不可用”的字段错配。
- `FallbackProvider` 内部的 `LibraryStore` 现在继承 `GW_LIBRARY_MAX_BYTES`，修复收藏下载绕过本地曲库容量上限。
- `_env_bool` 将空字符串视为未设置（沿用默认值），避免 `GW_TEE_SAVE=""` 意外关闭边播边存。
- `lyrics_for` 在入口统一受 `GW_LYRICS` 控制，关闭歌词后不再请求在线音源歌词。
- `ExtProvider.health` 使用真值判断，修复 `ok=0` 被误报为健康。
- `MediaCache.evict` 在 `max<=0`（不限容量）时直接返回，避免误删全部缓存。
- `PlaybackService` 歌词直链生成失败时降级为空歌词继续播放，不再让整次播放失败。
- `LibraryStore` / `MediaCache` 改用 `inspect.signature` 判断 `provider.stream` 是否接受 `quality`，避免用 `TypeError` 探测签名误吞真实异常。
- `save_bytes` 写入失败时清理 `.part` 残留；`_jobs` 完成后移除；容量统计只计音频扩展名。
- `parse_spec` 先识别 `http(s)://`，修复 URL 含 `@` 时被误当 `type@url` 拆分。
- `fnmusic_client` 的协议常量支持环境变量覆盖；TLS 默认校验证书，仅 `FNMUSIC_INSECURE_TLS=1` 时关闭。
- `music_play` / `music_download` 的 JSON Schema 增加 `anyOf` 必填约束；清理未使用 import、死 `except BadRequest` 与无文档 `q_quality` 别名。
- 文档同步：`music_*` 工具数 9→12、新增 `.env.example`、compose 的 `GW_PUBLIC_BASE` 默认改 8767（公网播放口）。
- 测试 53 → **60 个用例**（新增审计回归：空环境变量、source/healthy、NAS 掉线下的 status/storage、URL 含 @、缓存 0 不限、曲库上限、FallbackProvider 上限传递）。
- 2.0.1 再补 6 个用例至 **66 个**。

## 2.0.0 (2026-11-19)

### 重构：分层解耦 + 音质 / 下载 / 边播边存
- 新增 `gwlib/` 包：`errors`（统一错误码）、`quality`（音质档位）、`storage`（MediaCache + LibraryStore）、
  `sources`（ExtProvider / FallbackProvider / source_status）、`playback`（PlaybackService）、`mcp_tools`（12 工具）。
- `app.py` 只保留 HTTP/MCP 路由与依赖装配；播放链路统一走 `PlaybackService.prepare`。
- 新增音质四档 `standard/high/exhigh/lossless`：设备仍拿 MP3，`lossless` 仅下载保留原始音质；
  直链新增 `q` 参数，转码缓存按 `guid|quality` 分键。
- 新增 `music_download` / `music_sources` / `music_storage` 三个 MCP 工具与
  `GET /api/sources` / `GET /api/storage` / `GET /api/quality` / `POST /api/download`。
- 在线播放支持边播边存（`GW_TEE_SAVE`），本地曲库容量上限 `GW_LIBRARY_MAX_BYTES`。
- 新增环境变量 `GW_QUALITY_DEFAULT`、`GW_TEE_SAVE`、`GW_LIBRARY_MAX_BYTES`、`GW_SOURCES_SEARCH_PARALLEL`。
- 测试 44 → **53 个用例**（含换源重试、边播边存专项）；Dockerfile 两份均复制 `gwlib/`；四份 compose 增加新环境变量。
- README 增加「2.0 的分层结构」与音质/存储配置表，接口速查更新为 12 个 MCP 工具。

## 1.5.0 (2026-11-19)

### 新增：多音源兜底（本地曲库优先，在线音源补齐）
- 新增 `ExtProvider`：直连 fnos_music_ext 音源容器的 HTTP 契约（musicdl /search+/stream?proxy=true+/info、
  lxmusic /api/v1/search+track/url+track/lyric、musicbox 与 neteasefree /api/v1/search+song/{id}/url+lyric），
  按端口推断类型（8768 musicdl / 8770 musicbox / 8772 lxmusic / 8776 neteasefree），/healthz 探活。
- 新增 `FallbackProvider`：本地库优先，结果不足才追加在线结果并按「歌名+歌手」跨源去重；
  在线 guid 统一 `ext:<svc>:<id>`，`stream()` 按前缀分流，歌词先取上游、空则回退 LRCLIB。
- 试听识别：转码后用 ffprobe 取时长，< `GW_PREVIEW_MIN_SECONDS`（默认 35 秒）判为试听片段，
  `/api/play` 返回 `source/preview/duration`，MCP `music_play` 文本提示「想听完整版就说收藏」。
- 收藏即下载：`music_favorite(action=add)` 对在线歌曲按最高音质原样落 `GW_DOWNLOAD_DIR`，
  文件名 `歌名 - 歌手.<flac|mp3>`，可选 `GW_LIBRARY_SCAN_CMD` 触发曲库扫描。
- 新增环境变量 `GW_FALLBACK_URLS`（默认空=关闭，支持 `type@url`）、`GW_FALLBACK_TIMEOUT`、`GW_FALLBACK_SEARCH_LIMIT`、
  `GW_DOWNLOAD_DIR`、`GW_LIBRARY_SCAN_CMD`、`GW_PREVIEW_MIN_SECONDS`；`/healthz` 增 `fallback` 字段。
- 测试 35 → **44 个用例**：ext guid 解析、端口推断、本地优先与去重、ext 流路由、试听时长、收藏下载、
  假上游契约解析（search/stream/lyric）。
- README 新增「多音源兜底（本地优先，在线兜底）」一节，环境变量表与已验证表同步。

## 1.4.0 (2026-11-18)

### 新增：搜索别名纠错 + 相关度排序
- 新增文本归一化 `_norm_text`（NFKC 全角转半角、大小写折叠、去空格/标点/书名号）与
  别名表 `apply_aliases`：`$GW_DATA/music_aliases.json`（示例 `data/music_aliases.json.example`），
  按 mtime/size 自动重载，最长匹配替换，解决中文 ASR 把歌名/歌手听成同音字（青花磁→青花瓷、周杰轮→周杰伦）。
- 新增 `score_track` / `rank_tracks` / `search_tracks`：精确 > 前缀 > 包含 > difflib 相似度，
  歌手命中 +12，未提版本词时「伴奏/纯音乐/钢琴版/翻唱/remix…」候选 −15，标题长度接近 +3；
  别名生效时同时用纠正前后两个词搜索再合并排序。
- `pick_tracks` 的 query 分支、MCP `music_search`、REST `/api/search` 都改走 `search_tracks`；
  `/api/search` 多返回 `corrected` 字段，MCP 文本注明「已按别名纠正」，`music_search` 工具描述同步。
- 新增环境变量 `GW_SMART_SEARCH`（默认 1，0=回退原始顺序）、`GW_ALIASES_FILE`（默认 `$GW_DATA/music_aliases.json`）。
- 测试 26 → **35 个用例**：归一化、别名应用与重载、精确版压过版本词、歌手命中、排序、`search_tracks` 别名纠正、
  `/api/search` 排序与 `corrected`、`/api/play` 优先录音室版。
- README 新增「搜索更聪明」一节，环境变量表与已知限制（只支持 MP3）同步更新。

## 1.3.0 (2026-11-17)

### 新增：播放时显示歌名 / 歌手 / 歌词
- 网关按 `歌名 + 歌手` 从 LRCLIB 取歌词，优先 `syncedLyrics`；只有 `plainLyrics` 时按 4 秒一行补
  `[mm:ss.xx]` 时间戳（固件只认带时间戳的行）。LRC 缓存到 `$GW_DATA/lyrics/<guid>.lrc`，
  查不到写空文件并在 `GW_LYRICS_RETRY`（默认 86400 秒）后才重试。
- 新增签名歌词直链 `GET /lyrics/<guid>.lrc?exp=&sig=`（与音频同一套 HMAC，`text/plain`，支持 HEAD，
  坏签名/过期 403，无歌词 404）。`GW_PUBLIC_PORT` 公网面同时放行 `/lyrics/`，管理面路径仍全部 404。
- `/api/play` 返回新增 `lyric_url` 与 `device_args`（url/title/artist/lyric_url 一次给全）；
  MCP `music_play` 的返回文本与工具描述都要求 LLM 调 `self.music.play_url` 时带齐 title/artist/lyric_url。
- 示例插件 `examples/xiaozhi_plugin_fnmusic_gw.py` 改为传 `url + title + artist + lyric_url`
  （`lyric_url` 为空或超 2048 字节时自动省略），修复设备屏显 `unknown`、无歌词的问题。
- 新增环境变量 `GW_LYRICS`（默认 1）、`GW_LRCLIB_URL`、`GW_LYRICS_DIR`、`GW_LYRICS_RETRY`。
- 测试 21 → 26 个用例：`plain_to_lrc` 时间戳、`fetch_lyrics` 优先级与离线降级、`/lyrics/` 签名/403/404、
  `/api/play` 元数据、公网面 `/lyrics/` 放行。

## 1.2.2 (2026-11-17)

### 文档
- README「部署到飞牛 NAS」新增：
  - **公网播放：只反代 8767，绝不要反代 8766**——给出 compose 片段、8767/8766 行为对照表与三条 `curl` 验证命令。
  - **hairpin 排查**：`GW_PUBLIC_BASE` 用公网域名后，家里设备依赖 NAT 回环；不通时用内网 DNS 覆盖该域名到 `192.168.50.100`。
  - 记录固件 `self.music.play_url` 的 URL 限制（≤2048 字节、仅 http/https、禁空格与 `@`）。
- 版本号同步 1.2.2（无功能变更，仅文档）。

## 1.2.1 (2026-11-17)

### 新增
- **`GW_PUBLIC_PORT`（默认 0=关闭）**：额外监听一个「只开放 `/play/`」的公网端口，
  其余路径（`/api/*`、`/mcp`、`/healthz`、`/readyz`）一律 404。
  用于整域名反代场景，避免把无鉴权的管理面暴露到公网；纯进程内路径判断，不依赖反代软件特性。
- 两份 compose 增加 `GW_PUBLIC_PORT: "8767"`；bridge 版同时映射 `8767:8767`；镜像 `EXPOSE 8766 8767`。

### 测试
- `tests/test_gateway.py` 增至 **21 个用例**：新增 `TestPublicPort`，覆盖"公网面只放行 /play/"与
  "`/play` 的 GET / HEAD / Range 206 / 坏签名 403"。

## 1.2.0 (2026-11-17)

### 修复（会影响实际可用性）
- **示例插件注册错误**（`examples/xiaozhi_plugin_fnmusic_gw.py`）：给 `register_function` 多传了参数会直接 TypeError，
  且 `__module__` 改在注册之后（`module_func_map` 在注册时按 `__module__` 记账）；描述必须是完整 OpenAI 工具字典，
  否则 LLM 看不到 `mode`/`value`。三处已一并修正。
- **`Dockerfile.debian` 缺桥接器**：补 `COPY bridge/` 与 `websockets`，否则 debian 镜像跑 `fnmusic-bridge` 必崩。
- **`/healthz` 假健康**：真实模式下它从不访问后端，NAS 掉线仍 200。现在拆成
  `/healthz`（存活，不碰 NAS）与 `/readyz`（真探曲库，异常 503）。
- **转码残片**：失败时留 `*.tmp.mp3`，且以 `.mp3` 结尾会被当成缓存条目统计/淘汰。临时文件改 `.part`，
  统一在 `finally` 清理，启动时顺带清历史残片。
- **参数校验**：`limit` 收敛到 `GW_MAX_LIMIT`；`int` 解析失败、空 `q`、坏 `action`、空 `guid` 一律 400（原先 500）；
  MCP 请求体上限 4 MiB（413）。
- **配置鲁棒性**：`GW_LOG_LEVEL` 写错不再让进程起不来；`Host` 头校验后才拼进给设备的直链；`/play` 改精确匹配；
  `music_status` 时间缺失不再显示 1970。
- **桥接器**：网关超时/无响应时回一条 JSON-RPC 错误，避免接入点一直等到超时。

### 新增
- `GW_MAX_LIMIT`（默认 50）、`GW_TRANSCODE_CONCURRENCY`（默认 2，防止并发转码打满 NAS CPU）。
- `tests/test_gateway.py`：19 个离线用例（签名/直链/Range/MCP/REST/参数校验/缓存），进程内起服务，不需要 NAS。
- `.github/workflows/tests.yml`：push / PR 自动跑上面的用例。
- 监听队列 `request_queue_size` 5 → 64，多设备同时拉流不再被拒。

### 变更 / 清理
- provider 接口补 `albums()` / `artists()`，`music_browse` 不再直接摸 `p.client`。
- 删除 `fnmusic_client.client_from_env()` 死代码（引用仓库外 `.secrets/fnos.env`）；签名 nonce 改用 `secrets`。

## 1.1.0 (2026-11-17)

- 新增：小智「MCP 接入点」桥接器 `bridge/mcp_pipe_http.py`（WS ⇄ 网关 `/mcp`，主动外连、免公网 IP）。
- 新增：两份 compose 都带 `fnmusic-bridge` 服务，填 `MCP_ENDPOINT` 即启用；桥接器关闭继承的 healthcheck。
- 新增：`tests/bridge_selftest.py` 假 broker 自测（initialize / tools/list / ping / tools/call）。
- 变更：compose 默认直接拉 GHCR 镜像（`ghcr.io/kid941005/fnmusic-gateway:1.1.0`），可用 `GW_IMAGE` 覆盖。
- 变更：镜像内置 `websockets`，镜像体积约 263 MB。

## 1.0.0 (2026-11-16)

- 首个版本：飞牛音乐网关，9 个 MCP 工具 + REST API + 签名直链 MP3（HMAC，默认 15 分钟，Range 支持）。
- ffmpeg 实时转码为 128k / 44.1kHz / 立体声 MP3，按 atime LRU 缓存（默认上限 8 GiB）。
- Dockerfile（alpine，261 MB）与 Dockerfile.debian（805 MB）两版；GitHub Actions 双架构构建推送 GHCR。
