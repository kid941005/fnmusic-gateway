# CHANGELOG

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
