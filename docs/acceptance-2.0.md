# fnmusic-gateway 2.0 分级落地验收报告

- 版本：2.0.0
- 验收日期：2026-11-19
- 代码基线：`934d21b`（2.0 分层解耦 + 音质/下载/边播边存）
- 验收范围：P0 阻断项 → P1 核心能力 → P2 观测与文档
- 测试方式：逐优先级执行针对性用例；最后跑全量 53 用例与 4 份 compose 校验

## 总结论

全部 P0 / P1 项通过；P2 观测与文档通过。P2「WebUI 跳转」不做源码硬改，保留部署侧处置，不影响网关和小智播放链路。

| 优先级 | 验收项 | 结果 |
|---|---|---|
| P0 | 分层解耦与模块边界 | PASS |
| P0 | 播放链路统一 | PASS |
| P0 | 音质档位与缓存分键 | PASS |
| P0 | 端口与公网边界 | PASS |
| P0 | MCP 兼容与 12 工具 | PASS |
| P1 | 多源聚合兜底与换源重试 | PASS |
| P1 | 下载落盘与容量上限 | PASS |
| P1 | 在线播放边播边存 | PASS |
| P2 | 观测接口 | PASS |
| P2 | 文档与发布一致性 | PASS |
| P2 | WebUI 跳转部署侧处置 | PASS（不改源码） |

## P0-1 分层解耦与模块边界

- 落地内容：新增 `gwlib/errors.py`、`quality.py`、`storage.py`、`sources.py`、`playback.py`、`mcp_tools.py`；`app.py` 只保留路由、配置和装配。
- 自测命令：`python -m py_compile app.py gwlib/*.py`；`python -m unittest -q tests.test_gateway.TestPure tests.test_gateway.TestTools`
- 结果：编译通过；12 个用例 OK。
- 判定：PASS。

## P0-2 播放链路统一

- 落地内容：`call_tool` 与 `/api/play` 统一调用 `PlaybackService.prepare()`；返回 `device_args{url,title,artist,lyric_url}`、`quality`、`bitrate`。
- 自测命令：`python -m unittest -q tests.test_gateway.TestHttp.test_api_play_has_metadata tests.test_gateway.TestV2Api.test_api_play_quality`
- 结果：2 个用例 OK；`quality=high` 时 `bitrate=192k`，直链含 `q=high`。
- 判定：PASS。

## P0-3 音质档位与缓存分键

- 落地内容：`standard/high/exhigh/lossless` 四档；缓存键 `sha1(guid|quality)`；不同音质互不覆盖。
- 自测命令：`python -m unittest -q tests.test_gateway.TestCache tests.test_gateway.TestV2Quality.test_cache_key_separates_quality`
- 结果：2 个用例 OK；`flac` 与 `lossless` 等价，缓存键稳定。
- 判定：PASS。

## P0-4 端口与公网边界

- 落地内容：8766 管理口内网；8767 只放行 `/play/`、`/lyrics/`；full 版音源仅 `127.0.0.1:8768/8770/8772/8774`；slim 版音源零端口。
- 自测命令：`python -m unittest -q tests.test_gateway.TestPublicPort`
- 结果：3 个用例 OK；非播放路径 404，歌词放行，播放正常。
- 判定：PASS。

## P0-5 MCP 兼容与 12 工具

- 落地内容：原 9 个工具名称和参数保留，新增 `music_download` / `music_sources` / `music_storage`；`music_play` 追加可选 `quality`。
- 自测命令：`python -m unittest -q tests.test_gateway.TestHttp.test_mcp tests.test_gateway.TestV2Quality.test_tool_catalog`
- 结果：2 个用例 OK；tools/list 返回 12 个工具。
- 判定：PASS。

## P1-1 多源聚合兜底与换源重试

- 落地内容：`FallbackProvider` 本地优先、多源并行、去重；播放失败时 `PlaybackService._alternate()` 按歌名/歌手换源重试一次。
- 自测命令：`python -m unittest -q tests.test_gateway.TestFallback tests.test_gateway.TestExtProviderHttp`
- 结果：9 个用例 OK；新增 `TestV2Playback.test_failover_reselects_source` 验证 `ext:musicdl:bad` 失败后切到 `ext:musicdl:good`。
- 判定：PASS。

## P1-2 下载落盘与容量上限

- 落地内容：`music_download` / `POST /api/download`；保存原始文件；`GW_LIBRARY_MAX_BYTES` 超限返回 `StorageError`。
- 自测命令：`python -m unittest -q tests.test_gateway.TestFallback.test_ext_cache_and_download tests.test_gateway.TestFallback.test_download_requires_dir tests.test_gateway.TestV2Api.test_api_download_and_tool`
- 结果：3 个用例 OK；下载文件存在，未配置目录时报错。
- 判定：PASS。

## P1-3 在线播放边播边存

- 落地内容：`PlaybackService` 增加 `tee_save`；仅在线源 `ext:`、开关开启且本地曲库已配置时异步保存原始流。
- 自测命令：`python -m unittest -v tests.test_gateway.TestV2Playback`
- 结果：2 个用例 OK；在线曲 `saving=True` 且触发保存，本地曲不触发。
- 判定：PASS。

## P2-1 观测接口

- 落地内容：`GET /api/sources`、`GET /api/storage`、`GET /api/quality`；对应 MCP 工具 `music_sources` / `music_storage`。
- 自测命令：`python -m unittest -q tests.test_gateway.TestV2Api.test_quality_and_storage_endpoints`
- 结果：1 个用例 OK，返回 cache/library/sources/quality。
- 判定：PASS。

## P2-2 文档与发布一致性

- 落地内容：`README.md` 版本 2.0.0、MCP 12 工具、53 用例；`docs/refactor-2.0.md` 八段齐全；4 份 compose 有效。
- 自测命令：`for f in docker-compose.yml docker-compose.full.yml docker-compose.slim.yml docker-compose.webui.yml; do docker compose -f $f config --quiet || exit 1; done`
- 结果：COMPOSE_OK；文档标记一致性检查通过。
- 判定：PASS。

## P2-3 WebUI 跳转部署侧处置

- 现状：上游 `webui-service/static/app.js` 在端口 8774 时强制跳转飞牛桌面 5666；不改上游源码。
- 处置：`docker-compose.webui.yml` 保持 `127.0.0.1:8774:8004` 只绑回环；需要直连时改回环端口或用飞牛桌面入口。
- 自测：compose 配置校验通过，端口仅回环。
- 判定：PASS（按部署侧方案验收，非代码缺陷）。

## 全量回归

- `python tests/test_gateway.py -q`：Ran 53 tests，OK（约 4 秒）。
- `docker compose config --quiet`：4 份全部通过。
- 运行时元数据：VERSION=2.0.0，TOOLS=12，QUALITY_DEFAULT=standard。

## 验收结论

2.0 已按 P0 → P1 → P2 顺序完成落地并通过分级自测；可进入 NAS 部署与真机联调阶段。
