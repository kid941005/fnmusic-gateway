# CHANGELOG

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
