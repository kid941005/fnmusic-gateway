# MCP 服务层迁移：内置 JSON-RPC → 官方 MCPServer 2.3

## 结论

网关对外仍是 `POST /mcp`（streamable-http），但工具注册、参数校验、错误包装已交给官方 `mcp>=2.3` 的 `MCPServer`（原 FastMCP）。
官方应用只监听 `127.0.0.1` 的随机回环端口，由 `app.py` 在 8766 上反向代理，因此**不新增任何对外端口**，也不改变小智接入点/桥接器的用法。

## 结构

```
小智 / 桥接器
      │ POST /mcp（8766，内网）
      ▼
app.py Handler._handle_mcp
      │ 读取 body，补 X-GW-Base / Mcp-Session-Id 等头
      ▼
gwlib/mcp_server.py  McpHttpBridge
      │ http.client → 127.0.0.1:<随机端口>/mcp
      ▼
官方 mcp>=2.3 MCPServer.streamable_http_app(json_response=True, stateless_http=True)
      │ Tool.from_function → call_tool
      ▼
gwlib/mcp_tools.build_tools() 的 12 个工具定义 / 既有播放链路
```

## 关键实现

- `McpService`：为每个工具生成 async 函数，参数用 `inspect.Signature` 保留原名/类型；`tool.parameters` 覆盖回原始 JSON Schema（保留 `enum`、`anyOf`、中文说明）。工具执行放到 `asyncio.to_thread`，避免 ffmpeg/HTTP 阻塞事件循环。
- `McpHttpBridge`：后台 daemon 线程用 uvicorn 启动官方 ASGI 应用，`port=0` 由系统分配，启动后从 `server.servers[0].sockets[0].getsockname()[1]` 取端口；`request()` 用 `http.client` 转发 POST /mcp。
- 兼容：官方 stateless 模式不返回 `Mcp-Session-Id`，网关在 initialize 响应里补一个；官方 `DELETE /mcp` 为 405，网关仍返回 204。
- 兜底：官方依赖缺失或启动异常时，`_handle_mcp` 回退到原有 `_handle_mcp_legacy` JSON-RPC；`/healthz` 返回 `mcp_server.available/engine/error`。
- 安全：`X-GW-Base` 仅由网关根据当前请求生成，官方工具据此产出与原有逻辑一致的直链；不接受客户端传入。

## 验证

- `PYTHONPATH=<mcp-2.3-site> python tests/test_gateway.py`：**66 tests OK**。
- 官方日志确认：`官方 MCPServer 2.3 已启动：POST http://127.0.0.1:<随机端口>/mcp`。
- `python tests/bridge_selftest.py`：initialize / tools/list（12）/ ping / music_status 4 项 PASS。
- `/healthz` 新增：
  ```json
  {"mcp_server": {"available": true, "engine": "mcp>=2.3", "error": null}}
  ```

## 回滚

1. 将镜像标签切回 `2.0.1` 或关闭官方 MCP 分支（旧 `_handle_mcp_legacy` 仍在，可直接工作）。
2. 新版仅在官方依赖可用时启用；若构建环境无法安装 `mcp>=2.3`，`/mcp` 自动走旧 JSON-RPC，不阻塞网关启动。
3. 不改变数据目录、签名密钥与端口映射，回滚无需迁移数据。
