# Changelog

## [0.1.3] - 2025-01-02

### Enhanced
- **MCP-Compatible Logging System**: Completely refactored logging to follow MCP official best practices
  - Implemented file-only logging to prevent stdout pollution
  - Added MCP Context integration for operational logging during tool execution
  - Created `safe_log()` function that uses MCP Context when available, falls back to file logging
  - Added comprehensive third-party library log suppression
  - Introduced `FUTU_DEBUG_MODE` environment variable for development debugging
  - All logs now go to `logs/futu_server.log` with automatic rotation (500MB) and retention (10 days)
  - stdout is now exclusively reserved for MCP JSON communication

### Added
- **Documentation**: Created comprehensive logging documentation in `docs/LOGGING.md`
- **Environment Variables**: Added `FUTU_DEBUG_MODE` for controlled debug output
- **Context Support**: Updated key tool functions to accept and use MCP Context for logging
- **Best Practices**: Implemented all MCP Python SDK recommended logging patterns

### Technical Improvements
- Enhanced error handling with proper MCP Context logging
- Thread-safe logging with queue mode enabled
- Improved log format with module information
- Better separation of concerns between file logging and MCP communication

### Breaking Changes
- Removed `LOG_LEVEL` environment variable (replaced with `FUTU_DEBUG_MODE`)
- Console logging now only available in debug mode and when not in MCP mode

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
