# CHANGELOG

## 2.13.12 (2026-11-24)
### 优化：点播本地优先、整库分页与首播耗时日志
- **点播搜索**：`music_play` 按歌名点播与 REST `/api/play` 改为本地优先、在线边搜边回，最多等待约 3 秒（`GW_PLAY_SEARCH_BUDGET`，默认 3）；本地曲库命中时不再等到在线源聚合超时才起播。
- **整库播放**：`library=true` 改为分页拉取全部曲目，上限从 500 提升到 2000（`GW_LIBRARY_PLAY_LIMIT`），`FnProvider.list_tracks` 结果缓存 60 秒，避免重复拉取。
- **耗时日志**：新增点播搜索、`/play` 首块、`/playlist` 首曲准备耗时日志，便于定位起播瓶颈。
- 工具数保持 **16**；测试 181 → **184** 个用例；版本 2.13.11 → **2.13.12**。

## 2.13.11 (2026-11-24)
### 优化：首播流式播放、状态写盘去抖与预取调度
- **流式播放**：/play/<guid> 与 /playlist/<token> 首曲在未缓存时改为边收边播，首块到达即返回 200，不再等待全量下载/转码；standard 且源为 MP3 时原样透传，其他音质走 ffmpeg stdin/stdout 管道；边播边写缓存，客户端断开或失败时清理半成品，旧 provider 自动回退完整缓存。
- **状态轮询**：music_status 不再深拷贝整张列表，续播记录只保留必要字段，并按会话/曲目 30 秒去抖写盘，降低频繁轮询开销。
- **预取调度**：预取改为固定 2 个 worker 的队列，按曲目去重，失败 30 秒 / 转码槽忙 5 秒退避；预取转码给前台播放保留至少一个槽位，避免抢占首播。
- 新增 scripts/bench_streaming.py：模拟慢源对比 CACHE.ensure 全量等待与 CACHE.stream 首块等待，可调块数、延迟与并发。
- 工具数保持 **16**；测试 168 → **181** 个用例；版本 2.13.10 → **2.13.11**。

## 2.13.10 (2026-11-24)
### 优化：连续播放与切歌提前预取下一首
- `/playlist` 连续播放每首开始时后台预取下一首；`music_status` 在等待期间预取当前会话的下一首；`music_next` / `music_previous` 切换目标后预取再下一首，减少设备等待。
- 预取复用现有 `CACHE.prefetch` 与去重机制，不阻塞当前请求；预取失败只记 debug 日志，不影响正常播放。
- 工具数保持 **16**；测试 164 → **168** 个用例；版本 2.13.9 → **2.13.10**。

## 2.13.9 (2026-11-24)
### 新增：循环、整库播放、精确收藏与断点续播
- `music_play` 新增 `library=true` 整库顺序/随机播放、`loop=one/all/off` 单曲循环/列表循环/顺序播放；歌单、收藏、歌手、专辑、整库都支持随机顺序。
- 新增 `music_loop`（切换循环模式）、`music_resume`（断点续播）、`music_stop`（停止并记录进度）；单曲按秒切片继续，歌单恢复到对应曲目与播放位置。
- `music_favorite` 支持 `guid` 精确曲目和 `current=true` 当前正在播放的歌曲收藏/取消。
- 新增 `gwlib/resume.py`：按会话持久化播放进度（7 天 TTL，最多 64 条），容器重启后仍可继续；`/playlist` 流直链支持签名 `start` 秒偏移。
- 工具数 13 → **16**；测试 156 → **164** 个用例；版本 2.13.8 → **2.13.9**。

## 2.13.8 (2026-11-23)

### 修复：桥接直链、会话隔离与反向代理文档
- **设备直链**：新增 `_device_base()` 校验并规范化 `GW_PUBLIC_BASE`（仅接受 http/https）；网关在未设置该变量时启动告警。桥接器新增 `GW_PUBLIC_BASE` 必填校验，并统一转发 `X-GW-Base`，避免设备收到容器内地址。
- **会话隔离**：网关 MCP 会话键优先 `X-GW-Session` / `Mcp-Session-Id`，REST 优先 `?session=` / `?s=`；桥接器为每条接入点连接生成 `bridge-<uuid>` 会话并透传，同时支持从 JSON-RPC 帧提取 `session_id/device_id`。
- **Context 注入修复**：官方 MCPServer 2.3 下显式使用 `Tool.from_function(..., context_kwarg="ctx")`，修复此前 `ctx` 为 None 导致转发头不生效的问题。
- **文档**：README 与部署 README 的公网反代说明补充 `/playlist/*`；`.env.example` 与 compose 补充 `GW_PUBLIC_BASE`；README 工具数/测试数更新为 13/156。
- 版本 2.13.7 → **2.13.8**；测试 **156 个用例**。

## 2.13.7 (2026-11-23)

### 修复：连续播放状态与工具文档
- 连续播放游标改为按客户端/会话保存，单曲播放会清除本会话的连续流状态，避免多设备互相覆盖。
- 歌单/收藏的 index 使用绝对索引，music_status/music_next/music_previous 与剩余数量都基于完整列表；列表容量提升到 500 首，超出明确报错，不再静默截断。
- 桥接器自测工具数 11 → 13；README 工具表补齐 `music_next`/`music_previous`，测试数更新为 151。
- 版本 2.13.6 → **2.13.7**。

## 2.13.6 (2026-11-22)

### 测试：补充 MCP chunked 请求体超限回归
- 新增 `test_mcp_rejects_oversized_chunked_body`：临时调小 `MCP_BODY_CAP` 后发送超限 chunked 请求，断言返回 413 与 `payload too large`，防止 chunked 解析与错误映射退化。

### 文档与部署
- README 更新到 2.13.6，环境变量表补充 `GW_MCP_SEARCH_BUDGET`（默认 8）、`GW_FNOS_SEARCH_TIMEOUT`（默认 5）与桥接器 `GW_CONCURRENCY`（默认 4）。
- `docker-compose.yml` 与部署包 compose 增加上述参数默认值、网关 healthcheck 和可选 `extra_hosts` 示例；部署包 README 补充可选调优说明。
- 版本 2.13.5 → **2.13.6**；测试 **145 个用例**。

## 2.13.5 (2026-11-22)

### 修复与稳定性：播放链路评审问题落地
- **provider 生命周期**：`online_status` 查询状态用的临时 provider 用完即关闭，不再泄漏线程池/worker 子进程；`reset_provider` 不再立刻关闭旧实例，改为退休队列延迟释放，避免保存设置时打断正在播放的音频。
- **缓存与容量**：单曲大于缓存上限时明确报错并清理，不再刚写入就被 evict；evict 保护当前刚写入的文件；本地曲库下载前先检查容量，避免边下边写把磁盘写满。
- **搜索一致性**：MCP 快速搜索与 `/api/search/stream` 都支持「别名结果不足时用原词补搜」；SSE 对重复结果去重并按实际发送数计数。
- **播放识别**：普通 query 只搜一次，只有结果全部来自同一歌手时才升级为按歌手播放，避免每次点歌都拉一遍歌手全表；歌单名优先匹配真实歌单，只有确实不存在同名歌单时才把「收藏」短语当收藏。
- **REST 播放**：`/api/play` 新增 `favorites` / `artist` / `album` / `shuffle` 可选参数，复用连续播放流；修复连续播放列表的 WebUI 地址误生成 `/play/...` 的问题。
- **签名与协议**：直链签名绑定音质 `q`，改 `q` 不能越权取高音质（旧链接在有效期内兼容）；`/mcp` 支持 `Transfer-Encoding: chunked`；桥接器对 `GW_TIMEOUT` / `GW_CONCURRENCY` 非法值回退默认；工具数量文档修正为 11。
- 版本 2.13.4 → **2.13.5**；测试 **144 个用例**（新增 provider 生命周期、缓存保护、容量预检、搜索兜底、列表优先级、REST 播放、签名绑定、chunked 回归）。

## 2.13.4 (2026-11-22)

### 修复：未配置飞牛音乐时按歌手播放检测不影响普通搜索
- `resolve_artist`/`resolve_album` 在本地曲库不可用时安全返回空，不抛异常；在线音源模式下 `music_play` 的普通 query 搜索保持正常。
- 测试仍为 **132 个用例**。

## 2.13.3 (2026-11-22)

### 新增：按歌手/专辑播放飞牛音乐库
- `music_play` 新增 `artist=歌手名`：连续播放飞牛音乐库里该歌手的全部歌曲；`album=专辑名`：连续播放整张专辑。
- 当 `query` 正好是曲库里的歌手名时（如「播放周杰伦」），自动转为按歌手播放，不再只播放搜索到的第一首。
- artist/album 与 playlist/favorites 一样支持 `shuffle=true` 随机播放。
- 飞牛客户端已存在的 `artist_tracks`/`album_tracks` 接口接入网关，并通过 FallbackProvider 透传。
- MCP 工具描述补充按歌手/专辑播放；测试 **132 个用例**。

## 2.13.2 (2026-11-22)

### 新增：随机播放与多歌单精确匹配
- `music_play` 新增 `shuffle=true`：随机播放收藏用 `favorites=true&shuffle=true`，随机播放某歌单用 `playlist=歌单名&shuffle=true`；也识别「随机播放收藏的歌单」「随机播放儿歌歌单」等口语。
- 歌单名改为评分匹配：先精确名/核心名（去掉歌单、播放、随机等噪声），再比包含关系和相似度；多个歌单时「播放儿歌歌单」优先命中「儿歌」，不会误选「儿歌大全」。
- MCP 工具描述补充随机播放与模糊匹配用法；测试 **129 个用例**。

## 2.13.1 (2026-11-22)

### 修复：播放收藏/歌单只播放第一首
- 根因：MCP `music_play` 一直只返回一首歌的 `self.music.play_url`；传入 `playlist` 时也只取 `index` 指向的那一首，设备没有「下一首」接口，所以收藏/歌单永远停在一首。
- 新增连续播放流：`music_play` 传 `playlist=歌单名` 或 `favorites=true` 时，把整个列表登记为签名播放流 `/playlist/<token>.mp3`；该响应按顺序把多首 MP3 拼成一条音频流（每首跳过 ID3 标签，避免中途标签破坏解码），设备的 `self.music.play_url` 会一直读到 EOF，自然实现整张歌单/收藏连播。
- MCP 工具描述、`music_favorites`/`music_playlist_tracks` 说明同步提示「连续播放请用 `music_play` 的 `playlist`/`favorites`」，避免模型先 browse 再 index 只播一首。
- 版本 2.13.0 → **2.13.1**；测试 **125 个用例**（新增歌单/收藏连续播放与 `/playlist/` 流回归）。

## 2.13.0 (2026-11-22)

### 优化：小智点歌全链路提速
- **按 guid 播放**：`music_search` 结果每行带 `guid`，`music_play` 新增 `guid` 参数并优先直取，避免按关键词二次搜索和同名歌曲播错；查询路径仍保留。
- **播放异步化**：`PlaybackService.prepare(wait=False)` 立即返回签名直链，音频在后台 `MediaCache.prefetch` 准备，不再阻塞 MCP/WebUI；旧同步模式保留给内部与既有测试。
- **MP3 直出**：标准音质且上游流本身就是 MP3（Content-Type 或文件头判断）时直接改名入缓存，跳过 ffmpeg 转码。
- **MCP 快速搜索**：`fast_search_tracks` 优先走 `search_stream`，边搜边返回，最多等 `GW_MCP_SEARCH_BUDGET`（默认 8 秒）或凑够条数；返回本地/在线计数与失败原因。
- **常驻搜索池与预热**：`NativeMusicDLProvider` 使用常驻线程池，启动后后台预热各平台 worker，避免每次搜索新建线程/子进程。
- **本地搜索短超时**：`FnProvider.search` 使用 `GW_FNOS_SEARCH_TIMEOUT`（默认 5 秒），飞牛不可达时更快回退在线源。
- **桥接并发**：`bridge/mcp_pipe_http.py` 每帧独立任务，`GW_CONCURRENCY`（默认 4）限制并发，单个慢工具不再阻塞后续请求。
- **部署**：网关 compose 增加 `extra_hosts: fn.hqgg.top:192.168.50.100`，绕过 Docker NAT 回环导致的本地库不可达。
- 版本 2.12.1 → **2.13.0**；测试 **122 个用例**（新增 guid 搜索/播放、快速流式搜索、MP3 直出、异步 prepare）。

## 2.12.1 (2026-11-22)

### 修复：扫码前预热网易云会话
- 生成二维码前先访问一次 `music.163.com` 首页，规避 unikey 接口对冷会话返回 `code=-462`（网络环境风险）的问题；仍保留手动 Cookie 兜底。
- 版本 2.12.0 → **2.12.1**；测试 **117 个用例**。

## 2.12.0 (2026-11-22)

### 新增：网易云扫码登录
- WebUI「设置 → 在线音源」新增「生成登录二维码」：手机网易云音乐 App 扫码并在手机确认后，网关自动保存 `MUSIC_U`（必要时含 `__csrf`）到 `/data/config.json`（0600），并重建在线音源。
- 新增 `POST /api/settings/online/qr` 获取二维码（unikey + SVG data URL），`GET /api/settings/online/qr?key=` 轮询扫码状态；支持等待扫码、已扫码待确认、成功、过期。
- 扫码结果不回传 Cookie 明文，日志不打印；仍保留手动粘贴 Cookie 与「清除 Cookie」选项。Dockerfile 增加 `qrcode` 依赖。
- 版本 2.11.0 → **2.12.0**；测试 **117 个用例**（新增扫码接口启动、轮询成功保存 Cookie、缺少 key 校验）。

## 2.11.0 (2026-11-22)

### 新增：网易云 MUSIC_U Cookie 配置
- WebUI「设置 → 在线音源」新增网易云 Cookie 输入框与「清除已保存的网易云 Cookie」选项；Cookie 只写 `/data/config.json`（0600），接口只返回 `netease_cookie_set`，不回明文，日志不打印。
- Cookie 支持整段 `MUSIC_U=...; __csrf=...` 或纯 `MUSIC_U` 值；仅对 `NeteaseMusicClient` 注入到 musicdl 的 `default_search_cookies`/`default_parse_cookies`/`default_download_cookies`，worker 子进程按任务传递。
- 版本 2.10.2 → **2.11.0**；测试 **115 个用例**（新增 Cookie 解析/仅网易云注入、设置接口脱敏与清除）。

## 2.10.2 (2026-11-21)

### 修复：飞牛 token 失效后本地搜索一直为空
- 飞牛接口在 token 失效时可能返回 HTTP 200，但 body 为 {"code":99999,"msg":"INVALID TOKEN"}；旧客户端只在 HTTP 401 时重新登录，导致本地库搜索永远失败。
- `FnMusicClient.request()` 现在同时识别 HTTP 401 与 body code=99999 / INVALID TOKEN：清空旧 token、重新登录并重试一次。
- `stream_to_path()` / `stream_bytes()` 同样识别 200+JSON 的失效响应，避免把错误 JSON 当音频写盘。
- 版本 2.10.1 → **2.10.2**；测试 **112 个用例**（新增失效 token HTTP 200 重登录、流式播放重登录用例）。

## 2.10.1 (2026-11-21)

### 修复：WebUI 播放按钮无反馈/无法播放
- 首页新增常驻播放器（audio controls），播放按钮点击后把直链填入播放器并显示当前曲目、音质、是否试听。
- 播放请求增加秒级等待提示与错误提示；浏览器拦截自动播放时明确提示点播放器或「打开直链」，不再静默失败。
- /api/play 新增同源 web_url（相对 /play/...），WebUI 用同源地址播放，避免 GW_PUBLIC_BASE 与当前访问主机/协议不一致导致音频加载失败；设备端仍使用绝对 url 和签名直链。
- 版本 2.10.0 → **2.10.1**；测试 **109 个用例**（新增 web_url 与播放器元素断言）。

## 2.10.0 (2026-11-21)

### 新增：搜索结果流式返回
- 新增 `GET /api/search/stream?q=&limit=`（Server-Sent Events）：本地结果先到先出，在线音源每探活出一首就推一首，不必等所有平台/所有结果完成。
- 首页搜索改用 `EventSource`，结果逐条追加；已出现的曲目可立即播放/下载。SSE 响应带 `X-Accel-Buffering: no`，并保留 `/api/search` 旧接口兼容其他调用方。
- musicdl worker 增加 `iter_search()` 逐条产出 entry；`NativeMusicDLProvider.search_stream()` 多平台并发、按完成先后产出；`FallbackProvider.search_stream()` 本地优先后再接在线流。
- 版本 2.9.3 → **2.10.0**；测试 **109 个用例**（新增流式 provider 与 SSE 接口用例）。

## 2.9.3 (2026-11-21)

### 修复：本地库未命中与下载文件名
- 飞牛音乐地址自动归一化：normalize_base() 会去掉末尾 / 及误填的 /music、/music/api/v1，避免再拼成 /music/music/api/v1 而只返回 HTML、导致本地搜索被静默跳过。
- /api/search 新增 local_count/online_count/local_error/online_error 诊断字段，前端显示「本地 X · 在线 Y」；本地搜索异常不再被完全吞掉。
- 下载文件名改为「歌名 - 歌手」：新增 resolve_download_track()，按显式 title/artist → 播放状态同 guid → provider.track_info(guid) → guid 兜底；首页下载与 MCP music_download 均带 title/artist，服务端按 guid 从缓存还原元数据。
- 搜索结果标注来源（本地/平台）。版本 2.9.2 → **2.9.3**；测试 **106 个用例**。

## 2.9.2 (2026-11-21)

### 代码审查修复与内部重构
- 修复 WebUI 点播按标题重搜可能放错歌：`/api/play` 支持 `guid`，首页直接按 guid 点播，并新增回归用例。
- 飞牛音乐流式落盘：新增 `FnMusicClient.stream_to_path`，缓存不再把整首 FLAC/MP3 读入内存；token 文件写盘后设为 0600。
- 高级设置热更新改用可调 `TranscodeSlots`，不再替换正在使用的信号量；ffmpeg 超时/失败统一包装为 `StorageError`。
- 内置 musicdl worker 启动改为每平台独立锁；缓存淘汰先去重，避免误删仍有效的缓存。
- 清理失效配置 `GW_MP3_BITRATE`/`GW_MP3_SR`/`GW_MP3_CHANNELS`/`GW_PREWARM`/`GW_SOURCES_SEARCH_PARALLEL` 及对应文档。
- `call_tool` 拆分为 11 个独立 handler 按名分派；`FallbackProvider.search` 改用常驻线程池；删除死代码 `fallback_services()`/`source_status()`。
- 修复 MCP 桥关闭后端口/线程未重置、未知在线音源错误回退到首个源等问题。
- 版本 2.9.1 → **2.9.2**；测试 **100 个用例**（新增 guid 点播回归）。

## 2.9.1 (2026-11-21)

### 新功能：WebUI 与日志显示版本号
- 首页、设置页、登录页的标题与页头显示当前版本（`v2.9.1`），首页状态栏同时显示。
- 网关日志与内置 musicdl worker 日志每行带 `[v2.9.1]` 前缀，便于对照镜像版本排查。
- 版本 2.9.0 → **2.9.1**；测试 99 个用例（新增 3 处页面版本断言）。

## 2.9.0 (2026-11-21)

### 新功能：WebUI 登录页
- 设置 `GW_ADMIN_TOKEN` 后，未登录访问 `/` 或 `/settings` 会 302 到 `/login?next=...`，输入密码（即令牌）登录；登录成功写 HttpOnly Cookie，支持 `/logout` 退出。
- 兼容旧 `?token=` 引导；`/api/*`、`/mcp` 未授权仍返回 401 JSON，不跳登录页。
- 登录页防开放重定向：`next` 只允许站内路径；登录失败返回 401 并提示「密码错误」，不回显密码。
- 新增 `gwlib/login_page.py`；首页/设置页右上角增加「退出」。
- 版本 2.8.0 → **2.9.0**；测试 92 → **99**（新增登录页/退出/开放重定向用例）。

## 2.8.0 (2026-11-21)

### 破坏性变更：废弃 fnmusic-ext 音源容器，在线音源内置到网关
- 网关镜像直接内置 musicdl 2.14.2：新增 `gwlib/musicdl_core.py`、`gwlib/musicdl_worker.py`、`gwlib/native_source.py`；每个平台独立子进程 worker，搜索/直链缓存与音乐源管理全部在网关内完成。
- 删除独立音源容器与相关配置：compose 不再有 `fnmusic-sources`，移除 `GW_WEBUI_URL` / `GW_FALLBACK_URLS` / `FNMUSIC_SOURCES_IMAGE` / `MUSICDL_*`；只保留 `fnmusic-gateway` + 可选 `fnmusic-bridge`。
- 新增内置在线音源配置（网页「设置 → 在线音源」）：启用开关、平台勾选、附加平台、单平台条数/超时、聚合条数/总超时；新增 `GET/PUT /api/settings/online`、`POST /api/settings/online/test`，保存写入 `/data/config.json` 的 `online` 段并热重建。
- WebUI 改为网关原生页面：`/` 首页（状态/搜索/播放/下载）、`/settings` 设置页；删除对上游管理台 `/app/fnmusic-ext/*` 的反代。
- `/healthz` 的 `fallback`、`webui` 字段替换为 `online:{enabled,platforms,deps,error}`；`/api/sources` 返回内置平台状态。
- `GW_FALLBACK_*` 从高级设置移除，替换为 `GW_ONLINE_*` 系列（默认值写入 compose）。
- Dockerfile / Dockerfile.debian 改为 python:3.12-slim，安装 `musicdl==2.14.2`、`httpx`、`curl_cffi`、`ffmpeg`。
- 版本 2.7.0 → **2.8.0**；测试 93 → **92 个用例**（删除 WebUI 反代/兜底音源用例，新增原生首页与在线音源设置用例）。

## 2.7.0 (2026-11-21)

### 新功能：音乐直链返回总时长与歌词
- `/play/<guid>` 响应新增 `X-Audio-Duration-Ms` 与 `X-Lyric-Url`；设备播放时可直接显示总时长与歌词，不再需要额外请求。
- `/api/play` 的 `device_args` 新增 `duration`（秒，整数）。
- MCP `music_play` 与默认指令增加 `duration` 参数，工具描述同步为 5 个参数。
- 版本 2.6.1 → **2.7.0**；测试 93 个用例。

## 2.6.1 (2026-11-21)

### 修复
- 在线音源搜索的 `limit` 统一夹取到 1–10，避免上游 musicdl `/search` 对 `limit>10` 返回 HTTP 422；新增回归测试 `test_search_limit_capped_at_10`。
- 默认 `GW_FALLBACK_TIMEOUT` 由 8 秒调至 25 秒、`GW_FALLBACK_TOTAL_TIMEOUT` 由 15 秒调至 40 秒，给咪咕等慢源更多响应时间；README 默认值与设置页高级参数同步。
- 版本 2.6.0 → **2.6.1**；测试 92 → **93** 个用例。

## 2.6.0 (2026-11-20)

### 变更：移除心情选曲
- 删除 `GET /api/mood`、MCP 工具 `music_mood`、`mood_tracks()`、`MOODS`/`moods.json` 与 `GW_MOODS_FILE`；`/api/play` 与 `music_play` 只保留 `query` / `playlist` 二选一。这是破坏性变更：仍调用 `/api/mood` 或 `music_mood` 的客户端需改走 `/api/search` / `music_search`。

### 新功能：设置页补齐自签证书与兜底音源
- 设置页新增「使用自签 HTTPS 证书（跳过证书校验）」开关，存 `/data/config.json` 的 `fnos.insecure_tls`，运行期即时生效；`GW_FNOS_INSECURE_TLS` 仅作首次默认。
- 设置页新增兜底音源编辑器：可增删改 `GW_FALLBACK_URLS` 列表、逐条「测试」探活，保存后立即热重建 provider。
- 新增 REST：`GET/PUT /api/settings/fallback`、`POST /api/settings/fallback/test`；配置存 `/data/config.json` 的 `fallback.urls`，环境变量 `GW_FALLBACK_URLS` 仅作兜底。
### 新功能：设置页高级参数（运行期热生效）
- 设置页新增「高级设置」卡片：默认音质、搜索上限、直链有效期、试听阈值、边播边存、播放缓存/本地曲库上限、转码并发、智能搜索、歌词开关/LRCLIB 地址/重试间隔、音源/飞牛/MCP 超时。
- 新增 REST：`GET/PUT /api/settings/advanced`；值存 `/data/config.json` 的 `advanced` 段，保存后同步模块全局与已创建的缓存/播放/桥接对象，无需重启。
- 环境变量仍作首次启动默认值；端口、路径、`GW_ADMIN_TOKEN` 等启动/安全参数不进入网页。
- 版本 2.5.0 → **2.6.0**；测试 84 → **92** 个用例（删除心情用例，新增 TLS 开关、兜底音源增删改/校验/逐条测试、高级参数读写与热生效/非法值校验）。

## 2.5.0 (2026-11-20)

### 新功能：飞牛音乐优先 + 网页设置
- 恢复飞牛音乐作为首选音源：新增 `FnProvider` 与 `fnmusic_client.py`（token 存 `/data/fnmusic_token.json`，绑定 base 地址），搜索/歌单/收藏/播放优先走飞牛，`GW_FALLBACK_URLS` 里的在线音源只作兜底。
- 新增网页「网关设置」页 `/settings`（同样受 `GW_ADMIN_TOKEN` 保护，`?token=` 换 Cookie）：填写飞牛地址、账号、密码、设备 ID，可「测试连接」「保存」「清除登录令牌」，并显示当前在线兜底列表；配置存 `/data/config.json`（0600）。
- 新增 REST：`GET/PUT /api/settings/fnos`（密码只回 `password_set`，空密码表示不修改；base 必须 http(s)）、`POST /api/settings/fnos/test`（真实登录 + 试取 1 首）、`POST /api/settings/fnos/logout`。
- WebUI 首页右下角注入「网关设置 · 飞牛音乐」入口链接。
- `/healthz` 新增 `mode`（`mock` / `fnos` / `fnos+online` / `online`）与 `fnos: {configured, source, token_cached}`；`/readyz` 优先真连飞牛，失败再看在线兜底。
- 凭据来源优先级：网页保存的 `/data/config.json` > 环境变量 `GW_FNOS_BASE` / `GW_FNOS_USER` / `GW_FNOS_PASS` / `GW_FNOS_DEVICE_ID`。
- 版本 2.4.1 → **2.5.0**；测试 77 → **84** 个用例。

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
