# Futu Stock MCP Server - 日志配置

## 概述

本项目已根据 [MCP 官方文档](https://github.com/modelcontextprotocol/python-sdk) 的最佳实践重新配置了日志系统，确保与 MCP 协议完全兼容。

## MCP 兼容性原则

根据 MCP 规范，服务器必须遵循以下原则：

1. **stdout 专用于 MCP JSON 通信** - 绝不能向 stdout 输出任何非 JSON 内容
2. **使用文件日志进行调试** - 所有调试信息应写入日志文件
3. **使用 MCP Context 进行操作日志** - 在工具执行期间使用 Context 对象发送日志
4. **抑制第三方库日志** - 防止其他库污染 stdout

## 日志配置

### 文件日志

所有日志都会写入 `logs/futu_server.log` 文件：

- **轮转**: 500 MB 后自动轮转
- **保留**: 保留 10 天的日志
- **级别**: DEBUG 及以上
- **格式**: `{时间} | {级别} | {模块} | {消息}`
- **线程安全**: 启用队列模式确保线程安全

### MCP Context 日志

在工具执行期间，日志会通过 MCP Context 发送给客户端：

```python
@mcp.tool()
async def my_tool(param: str, ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """示例工具函数"""
    safe_log("info", f"Processing parameter: {param}", ctx)

    try:
        # 执行操作
        result = do_something(param)
        safe_log("info", "Operation completed successfully", ctx)
        return {"result": result}
    except Exception as e:
        safe_log("error", f"Operation failed: {str(e)}", ctx)
        return {"error": str(e)}
```

### 调试模式

仅在开发期间使用，通过环境变量控制：

```bash
# 启用调试模式（会向 stderr 输出日志）
export FUTU_DEBUG_MODE=1

# 运行服务器（非 MCP 模式）
python src/futu_stock_mcp_server/server.py
```

**注意**: 调试模式不应在 MCP 客户端中使用，因为它会向 stderr 输出日志。

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `FUTU_DEBUG_MODE` | `0` | 设置为 `1` 启用 stderr 日志输出（仅用于开发） |
| `MCP_MODE` | 自动设置 | 由服务器自动设置，表示运行在 MCP 模式下 |

## 日志级别

- **DEBUG**: 详细的调试信息
- **INFO**: 一般信息，如操作成功
- **WARNING**: 警告信息
- **ERROR**: 错误信息
- **CRITICAL**: 严重错误

## 最佳实践

### 1. 使用 safe_log 函数

```python
# 推荐：使用 safe_log 函数
safe_log("info", "Operation started", ctx)

# 避免：直接使用 logger（不会发送到 MCP 客户端）
logger.info("Operation started")
```

### 2. 在工具函数中添加 Context 参数

```python
@mcp.tool()
async def my_tool(param: str, ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """工具函数应该接受 Context 参数"""
    safe_log("info", f"Tool called with param: {param}", ctx)
    # ... 实现逻辑
```

### 3. 错误处理和日志记录

```python
try:
    result = risky_operation()
    safe_log("info", "Operation successful", ctx)
    return {"result": result}
except Exception as e:
    error_msg = f"Operation failed: {str(e)}"
    safe_log("error", error_msg, ctx)
    return {"error": error_msg}
```

## 故障排除

### 问题：MCP 客户端收不到响应

**可能原因**: stdout 被污染

**解决方案**:
1. 检查是否有代码直接使用 `print()` 函数
2. 确保第三方库没有向 stdout 输出内容
3. 检查日志文件中的错误信息

### 问题：日志文件过大

**解决方案**:
1. 日志会自动轮转（500 MB）
2. 旧日志会自动删除（10 天后）
3. 可以手动清理 `logs/` 目录

### 问题：看不到实时日志

**解决方案**:
1. 在 MCP 模式下，使用支持 MCP 日志的客户端查看
2. 在开发模式下，设置 `FUTU_DEBUG_MODE=1`
3. 查看日志文件：`tail -f logs/futu_server.log`

## 与官方文档的对应关系

本实现遵循了 MCP Python SDK 官方文档的以下最佳实践：

1. **Context 日志记录**: 使用 `ctx.info()`, `ctx.error()` 等方法
2. **文件日志**: 所有调试信息写入文件
3. **stdout 保护**: 确保 stdout 仅用于 MCP JSON 通信
4. **第三方库抑制**: 防止其他库污染输出

参考: [MCP Python SDK 官方文档](https://github.com/modelcontextprotocol/python-sdk)
