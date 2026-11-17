# CHANGELOG

## Unreleased

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
