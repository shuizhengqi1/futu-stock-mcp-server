# Changelog

## [0.1.2] - 2025-10-02

### Fixed
- **Critical MCP Communication Fix**: Fixed JSON parsing errors during MCP connection startup
  - Redirected all log output from stdout to stderr to prevent pollution of MCP JSON communication
  - Disabled ANSI color codes in log output to avoid escape sequence contamination
  - Suppressed verbose logging from third-party libraries (mcp, futu) that could interfere with protocol
  - Added environment variables (`NO_COLOR=1`, `TERM=dumb`) to ensure clean output
  - This resolves the "Unexpected non-whitespace character after JSON" and "Unexpected token" errors

### Technical Details
The issue was caused by:
1. Console logger using `print()` which outputs to stdout instead of stderr
2. ANSI color escape sequences (`\u001b[0;30m`) being mixed into JSON responses
3. Third-party library logs interfering with MCP protocol communication

The fix ensures:
- All application logs go to stderr only
- JSON responses on stdout remain clean and parseable
- No color codes or escape sequences in MCP communication
- Proper separation of logging and protocol data streams

### Testing
- Added test script `test_mcp_output.py` to verify clean JSON output
- Verified MCP connection works without parsing errors
- Confirmed all 24 tools load successfully after fix
