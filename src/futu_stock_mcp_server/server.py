import os
import sys
import warnings
import logging
import argparse

# CRITICAL: Check if this is a help command before setting MCP mode
_is_help_command = any(arg in ['--help', '-h', '--version', '-v'] for arg in sys.argv)

# CRITICAL: Set MCP mode BEFORE any logging to ensure clean stdout
# But not if this is a help command - in that case, we want normal stdout
if not _is_help_command:
    os.environ['MCP_MODE'] = os.environ.get('MCP_MODE', '1')

# CRITICAL: Completely disable all potential stdout pollution sources
# This must be done BEFORE any other imports or operations

# 1. Disable all warnings that might go to stdout/stderr
warnings.filterwarnings("ignore")
warnings.simplefilter("ignore")

# 2. Completely disable the standard logging system
logging.disable(logging.CRITICAL)

# 3. Set environment variables to prevent ANSI escape sequences
os.environ['NO_COLOR'] = '1'
os.environ['TERM'] = 'dumb'
os.environ['FORCE_COLOR'] = '0'
os.environ['COLORTERM'] = ''
os.environ['ANSI_COLORS_DISABLED'] = '1'
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'

# 4. Create a custom stdout wrapper to catch any accidental writes
class StdoutProtector:
    """Protects stdout from any non-MCP content"""
    def __init__(self, original_stdout):
        self.original = original_stdout
        self.buffer = ""

    def write(self, text):
        # Only allow JSON-like content or empty strings
        if not text or text.isspace():
            self.original.write(text)
        elif text.strip().startswith(('{', '[', '"')) or text.strip() == '':
            self.original.write(text)
        else:
            # Silently drop non-JSON content
            pass

    def flush(self):
        self.original.flush()

    def readable(self):
        """Delegate readable() method to original stdout"""
        return getattr(self.original, 'readable', lambda: False)()

    def writable(self):
        """Delegate writable() method to original stdout"""
        return getattr(self.original, 'writable', lambda: True)()

    def seekable(self):
        """Delegate seekable() method to original stdout"""
        return getattr(self.original, 'seekable', lambda: False)()

    def __getattr__(self, name):
        return getattr(self.original, name)

# Apply stdout protection in MCP mode VERY EARLY, before any imports
# if os.getenv('MCP_MODE') == '1':
    # sys.stdout = StdoutProtector(sys.stdout)

# 5. Don't redirect stderr in MCP mode - let it work normally
# MCP servers can use stderr for logging, only stdout needs protection
_stderr_redirected = False
_stderr_backup = None

# Now we can safely import other modules
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Dict, Any, List, Optional
try:
    from futu import (
        OpenQuoteContext,
        OpenSecTradeContext,
        TrdMarket,
        SecurityFirm,
        RET_OK,
        TrdEnv,
        TrdSide,
        OrderType,
        ModifyOrderOp,
        Session,
        TrailType,
        TimeInForce,
        OrderStatus,
        CashFlowDirection,
    )
except ImportError as e:
    # In MCP mode, we should avoid printing to stdout/stderr
    # Log to file only
    logger.error(f"Failed to import futu: {e}")
    sys.exit(1)
import json
import asyncio
from loguru import logger
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import TextContent, PromptMessage
from mcp.server import Server
from mcp.server.session import ServerSession
import atexit
import signal
import fcntl
import psutil
import time
from datetime import datetime

# Get the user home directory and create logs directory there
home_dir = os.path.expanduser("~")
log_dir = os.path.join(home_dir, "logs")
os.makedirs(log_dir, exist_ok=True)

# CRITICAL: Configure logging to be MCP-compatible
# According to MCP best practices, we should:
# 1. Never write to stdout (reserved for MCP JSON communication)
# 2. Use file logging for debugging
# 3. Use MCP Context for operational logging when available
# 4. Suppress third-party library logs that might pollute output

# Completely silence warnings and third-party logs
warnings.filterwarnings("ignore")

# Configure loguru for file-only logging
logger.remove()  # Remove all default handlers

# CRITICAL: In MCP mode, ensure NO stderr output at all
if os.getenv('MCP_MODE') == '1':
    # Remove any remaining handlers that might output to stderr
    logger.remove()
    # Add only file handler - NO console output
    logger.add(
        os.path.join(log_dir, "futu_mcp_server.log"),
        rotation="500 MB",
        retention="10 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}",
        enqueue=True,  # Thread-safe logging
        backtrace=True,
        diagnose=True
    )
else:
    # Non-MCP mode: add file handler
    logger.add(
        os.path.join(log_dir, "futu_mcp_server.log"),
        rotation="500 MB",
        retention="10 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}",
        enqueue=True,  # Thread-safe logging
        backtrace=True,
        diagnose=True
    )

# Only add stderr logging if explicitly in debug mode and not in MCP mode
if os.getenv('FUTU_DEBUG_MODE') == '1' and not os.getenv('MCP_MODE') == '1':
    logger.add(
        sys.stderr,
        level="INFO",
        format="{time:HH:mm:ss} | {level} | {message}",
        colorize=False,
        filter=lambda record: record["level"].name in ["INFO", "WARNING", "ERROR", "CRITICAL"]
    )

# Suppress all third-party library logging to prevent stdout pollution
logging.disable(logging.CRITICAL)

# Set up null handlers for problematic loggers
class NullHandler(logging.Handler):
    def emit(self, record):
        pass

null_handler = NullHandler()
root_logger = logging.getLogger()
root_logger.addHandler(null_handler)
root_logger.setLevel(logging.CRITICAL + 1)

# Specifically silence known problematic loggers
for logger_name in [
    'mcp', 'fastmcp', 'futu', 'uvicorn', 'asyncio',
    'websockets', 'aiohttp', 'urllib3', 'requests'
]:
    lib_logger = logging.getLogger(logger_name)
    lib_logger.disabled = True
    lib_logger.addHandler(null_handler)
    lib_logger.setLevel(logging.CRITICAL + 1)
    lib_logger.propagate = False

# Even more aggressive suppression for futu library
# This is critical because futu library may output logs during connection
try:
    # Suppress futu library logging completely
    futu_logger = logging.getLogger('futu')
    futu_logger.disabled = True
    futu_logger.setLevel(logging.CRITICAL + 1)
    futu_logger.propagate = False

    # Suppress specific futu sub-modules that are known to output logs
    for sub_logger_name in [
        'futu', 'futu.common', 'futu.quote', 'futu.trade',
        'futu.common.constant', 'futu.common.sys_utils',
        'futu.quote.open_quote_context', 'futu.quote.quote_response_handler',
        'futu.trade.open_trade_context', 'futu.trade.trade_response_handler',
        'futu.common.open_context_base', 'futu.common.network_manager'
    ]:
        sub_logger = logging.getLogger(sub_logger_name)
        sub_logger.disabled = True
        sub_logger.setLevel(logging.CRITICAL + 1)
        sub_logger.propagate = False

    # Also redirect any direct print statements from futu to a file
    if os.getenv('MCP_MODE') == '1':
        # Create a special log file for futu connection logs
        home_dir = os.path.expanduser("~")
        log_dir = os.path.join(home_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        futu_conn_log_file = os.path.join(log_dir, "futu_connection.log")
        futu_conn_log = open(futu_conn_log_file, 'a')

        # This is a last resort to catch any print statements from futu
        # We'll redirect stderr to this file temporarily during connection
        # and then restore it to devnull
except Exception as e:
    # If we can't set up additional logging, continue anyway
    pass

# MCP-compatible logging helper functions
async def log_to_mcp(ctx: Context, level: str, message: str):
    """Send log message through MCP Context when available"""
    try:
        if level.upper() == "DEBUG":
            await ctx.debug(message)
        elif level.upper() == "INFO":
            await ctx.info(message)
        elif level.upper() == "WARNING":
            await ctx.warning(message)
        elif level.upper() == "ERROR":
            await ctx.error(message)
        else:
            await ctx.info(f"[{level}] {message}")
    except Exception:
        # Fallback to file logging if MCP context fails
        logger.log(level.upper(), message)

def safe_log(level: str, message: str, ctx: Context = None):
    """Safe logging that uses MCP context when available, file logging otherwise"""
    # Always log to file
    logger.log(level.upper(), message)

    # Also send to MCP if context is available
    if ctx and os.getenv('MCP_MODE') == '1' and not _stderr_redirected:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(log_to_mcp(ctx, level, message))
        except Exception:
            pass  # Ignore MCP logging errors

# Only log to file, never to stdout/stderr in MCP mode
# These logs will only be written to file, not to stdout/stderr

# Get project root directory
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# PID file path
PID_FILE = os.path.join(project_root, '.futu_mcp.pid')
LOCK_FILE = os.path.join(project_root, '.futu_mcp.lock')

# Global variables
quote_ctx = None
trade_ctx = None
lock_fd = None
_is_shutting_down = False
_is_trade_initialized = False
_futu_host = '127.0.0.1'
_futu_port = 11111

def is_process_running(pid):
    """Check if a process with given PID is running"""
    try:
        return psutil.pid_exists(pid)
    except:
        return False

def cleanup_stale_processes():
    """Clean up any stale Futu processes"""
    global _is_shutting_down
    if _is_shutting_down:
        return
        
    try:
        # 只检查 PID 文件中的进程
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                    if old_pid != os.getpid():
                        try:
                            old_proc = psutil.Process(old_pid)
                            if any('futu_stock_mcp_server' in cmd for cmd in old_proc.cmdline()):
                                logger.info(f"Found stale process {old_pid}")
                                old_proc.terminate()
                                try:
                                    old_proc.wait(timeout=3)
                                except psutil.TimeoutExpired:
                                    old_proc.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except (IOError, ValueError):
                pass
            
            # 清理 PID 文件
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass
                
        # 清理锁文件
        if os.path.exists(LOCK_FILE):
            try:
                os.unlink(LOCK_FILE)
            except OSError:
                pass
                
    except Exception as e:
        logger.error(f"Error cleaning up stale processes: {str(e)}")

def cleanup_connections():
    """Clean up Futu connections"""
    global quote_ctx, trade_ctx
    try:
        if quote_ctx:
            try:
                quote_ctx.close()
                logger.info("Successfully closed quote context")
            except Exception as e:
                logger.error(f"Error closing quote context: {str(e)}")
            quote_ctx = None
        
        if trade_ctx:
            try:
                trade_ctx.close()
                logger.info("Successfully closed trade context")
            except Exception as e:
                logger.error(f"Error closing trade context: {str(e)}")
            trade_ctx = None
            
        # 等待连接完全关闭
        time.sleep(1)
    except Exception as e:
        logger.error(f"Error during connection cleanup: {str(e)}")

def release_lock():
    """Release the process lock"""
    global lock_fd
    try:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            lock_fd = None
        if os.path.exists(LOCK_FILE):
            os.unlink(LOCK_FILE)
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
    except Exception as e:
        logger.error(f"Error releasing lock: {str(e)}")

def cleanup_all():
    """Clean up all resources on exit"""
    global _is_shutting_down
    if _is_shutting_down:
        return
    _is_shutting_down = True
    
    cleanup_connections()
    release_lock()
    cleanup_stale_processes()

def signal_handler(signum, frame):
    """Handle process signals"""
    global _is_shutting_down
    if _is_shutting_down:
        logger.info("Already shutting down, forcing exit...")
        os._exit(1)
        
    # 只处理 SIGINT 和 SIGTERM
    if signum not in (signal.SIGINT, signal.SIGTERM):
        return
        
    logger.info(f"Received signal {signum}, cleaning up...")
    _is_shutting_down = True

    try:
        cleanup_all()
        logger.info("Cleanup completed, exiting...")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    finally:
        # 强制退出，确保进程能够终止
        os._exit(0)

# Register cleanup functions
atexit.register(cleanup_all)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def acquire_lock():
    """Try to acquire the process lock"""
    try:
        # 先检查 PID 文件
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                    if old_pid != os.getpid() and psutil.pid_exists(old_pid):
                        try:
                            old_proc = psutil.Process(old_pid)
                            if any('futu_stock_mcp_server' in cmd for cmd in old_proc.cmdline()):
                                logger.error(f"Another instance is already running (PID: {old_pid})")
                                return None
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except (IOError, ValueError):
                pass
        
        # 创建锁文件
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            os.close(lock_fd)
            return None
            
        # 写入 PID 文件
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
            
        return lock_fd
    except Exception as e:
        logger.error(f"Failed to acquire lock: {str(e)}")
        if 'lock_fd' in locals():
            try:
                os.close(lock_fd)
            except:
                pass
        return None

def init_quote_connection():
    """Initialize quote connection only"""
    global quote_ctx, _futu_host, _futu_port
    
    try:
        # Check if OpenD is running by attempting to get global state
        try:
            temp_ctx = OpenQuoteContext(
                host=_futu_host,
                port=_futu_port
            )
            ret, _ = temp_ctx.get_global_state()
            temp_ctx.close()
            if ret != RET_OK:
                logger.error("OpenD is not running or not accessible")
                return False
        except Exception as e:
            logger.error(f"Failed to connect to OpenD: {str(e)}")
            return False

        # Initialize Futu connection
        quote_ctx = OpenQuoteContext(
            host=_futu_host,
            port=_futu_port
        )
        logger.info("Successfully connected to Futu Quote API")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize quote connection: {str(e)}")
        cleanup_connections()
        return False

def init_trade_connection():
    """Initialize trade connection only"""
    global trade_ctx, _is_trade_initialized, _futu_host, _futu_port
    
    if _is_trade_initialized and trade_ctx:
        return True
        
    try:
        # Initialize trade context with proper market access
        trade_env = os.getenv('FUTU_TRADE_ENV', 'SIMULATE')
        security_firm = getattr(SecurityFirm, os.getenv('FUTU_SECURITY_FIRM', 'FUTUSECURITIES'))
        
        trd_market_str = os.getenv('FUTU_TRD_MARKET', 'HK')
        # TrdMarket enum value must be used, not a raw string
        trd_market = getattr(TrdMarket, trd_market_str, TrdMarket.HK)
        
        # 创建交易上下文
        # 不传 filter_trdmarket（使用默认值），避免因市场过滤导致账户列表为空
        trade_ctx = OpenSecTradeContext(
            host=_futu_host,
            port=_futu_port,
            security_firm=security_firm
        )
            
        # 等待连接就绪
        time.sleep(1)
            
        # 验证连接状态
        if not trade_ctx:
            raise Exception("Failed to create trade context")
            
        # Set trade environment
        if hasattr(trade_ctx, 'set_trade_env'):
            ret, data = trade_ctx.set_trade_env(trade_env)
            if ret != RET_OK:
                logger.warning(f"Failed to set trade environment: {data}")
                
        # Verify account access and permissions
        ret, data = trade_ctx.get_acc_list()
        if ret != RET_OK:
            logger.warning(f"Failed to get account list: {data}")
            # 即使获取账户列表失败，交易上下文本身可能仍然可用，不做 cleanup
            # 继续初始化，让后续工具调用自行处理错误
        
        if data is None or len(data) == 0:
            logger.warning("No trading accounts available for the current filter; trade context still initialized")
        else:
            # Convert DataFrame to records if necessary
            if hasattr(data, 'to_dict'):
                accounts = data.to_dict('records')
            else:
                accounts = data
                
            logger.info(f"Found {len(accounts)} trading account(s)")
            
            # 检查账户状态
            for acc in accounts:
                if isinstance(acc, dict):
                    acc_id = acc.get('acc_id', 'Unknown')
                    acc_type = acc.get('acc_type', 'Unknown')
                    acc_state = acc.get('acc_state', 'Unknown')
                    trd_env = acc.get('trd_env', 'Unknown')
                    trd_market_acc = acc.get('trd_market', 'Unknown')
                else:
                    acc_id = getattr(acc, 'acc_id', 'Unknown')
                    acc_type = getattr(acc, 'acc_type', 'Unknown')
                    acc_state = getattr(acc, 'acc_state', 'Unknown')
                    trd_env = getattr(acc, 'trd_env', 'Unknown')
                    trd_market_acc = getattr(acc, 'trd_market', 'Unknown')
                    
                logger.info(f"Account: {acc_id}, Type: {acc_type}, State: {acc_state}, Environment: {trd_env}, Market: {trd_market_acc}")
        
        _is_trade_initialized = True
        logger.info(f"Successfully initialized trade connection (Trade Environment: {trade_env}, Security Firm: {security_firm}, Market: {trd_market_str})")
        return True
            
    except Exception as e:
        logger.error(f"Failed to initialize trade connection: {str(e)}")
        cleanup_connections()
        _is_trade_initialized = False
        return False

def init_futu_connection(host: str = '127.0.0.1', port: int = 11111) -> bool:
    """
    Initialize connection to Futu OpenD.

    Args:
        host: Futu OpenD host address
        port: Futu OpenD port number

    Returns True if successful, False otherwise.
    """
    global quote_ctx, trade_ctx, _is_trade_initialized, _futu_host, _futu_port

    try:
        # Set global connection parameters
        _futu_host = host
        _futu_port = port

        # Log to file only
        logger.info(f"Initializing Futu connection to {host}:{port}")

        # Initialize quote context
        quote_ctx = OpenQuoteContext(host=host, port=port)

        # Trade context stays lazy-initialized by trading/position tools.
        # This avoids startup failure when trade permissions are not available.
        trade_ctx = None
        _is_trade_initialized = False
        if trading_feature_enabled() or position_feature_enabled():
            logger.info("Trade-related features enabled; trade context will initialize lazily on first use")

        logger.info("Futu connection initialized successfully")
        return True

    except Exception as e:
        error_msg = f"Failed to initialize Futu connection: {str(e)}"
        logger.error(error_msg)
        return False

@asynccontextmanager
async def lifespan(server: Server):
    # Startup - connections are already initialized in main()
    # No need to initialize here as it's done before mcp.run()
    try:
        yield
    finally:
        # Shutdown - ensure connections are closed
        cleanup_all()

# Create MCP server instance
mcp = FastMCP("futu-stock-server", lifespan=lifespan)

def handle_return_data(ret: int, data: Any) -> Dict[str, Any]:
    """Helper function to handle return data from Futu API
    
    Args:
        ret: Return code from Futu API
        data: Data returned from Futu API
    
    Returns:
        Dict containing either the data or error message
    """
    if ret != RET_OK:
        return {'error': str(data)}
    
    # If data is already a dict, return it directly
    if isinstance(data, dict):
        return data
    
    # If data has to_dict method, call it
    if hasattr(data, 'to_dict'):
        return data.to_dict()
    
    # If data is a pandas DataFrame, convert to dict
    if hasattr(data, 'to_dict') and callable(getattr(data, 'to_dict')):
        return data.to_dict('records')
    
    # For other types, try to convert to dict or return as is
    try:
        return dict(data)
    except (TypeError, ValueError):
        return {'data': data}


def is_env_flag_enabled(env_key: str, default: str = "0") -> bool:
    """Parse env feature flags in a tolerant way."""
    value = os.getenv(env_key, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def trading_feature_enabled() -> bool:
    """Whether active trading tools are enabled."""
    return is_env_flag_enabled("FUTU_ENABLE_TRADING", "0")


def position_feature_enabled() -> bool:
    """Whether position/holding tools are enabled."""
    return is_env_flag_enabled("FUTU_ENABLE_POSITIONS", "1")


def feature_disabled_error(feature: str, env_key: str) -> Dict[str, Any]:
    """Standardized feature-disabled response."""
    return {"error": f"{feature} is disabled. Set {env_key}=1 to enable."}


def parse_enum_value(enum_cls: Any, raw_value: Any, field_name: str) -> Any:
    """Convert user input into futu enum values, keeping protocol simple."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return raw_value
    enum_name = raw_value.strip().upper()
    if hasattr(enum_cls, enum_name):
        return getattr(enum_cls, enum_name)
    raise ValueError(
        f"Invalid {field_name}: {raw_value}. "
        f"Expected one of {[k for k in dir(enum_cls) if k.isupper() and not k.startswith('_')]}"
    )


def parse_enum_list(enum_cls: Any, values: Optional[List[str]], field_name: str) -> List[Any]:
    """Convert a list of enum names into futu enum values."""
    if not values:
        return []
    return [parse_enum_value(enum_cls, item, field_name) for item in values]


def get_trade_env_value(trd_env: Optional[str]) -> Any:
    """Resolve trade environment from parameter or env variable."""
    resolved = trd_env or os.getenv("FUTU_TRADE_ENV", "SIMULATE")
    return parse_enum_value(TrdEnv, resolved, "trd_env")


def dataframe_to_records(data: Any, wrapper_key: str) -> Dict[str, Any]:
    """Normalize pandas DataFrame-like responses to record list payload."""
    if hasattr(data, "to_dict"):
        return {wrapper_key: data.to_dict("records")}
    return {wrapper_key: data}

# Market Data Tools
@mcp.tool()
async def get_stock_quote(symbols: List[str], ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """Get stock quote data for given symbols
    
    Args:
        symbols: List of stock codes, e.g. ["HK.00700", "US.AAPL", "SH.600519"]
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing quote data including:
        - quote_list: List of quote data entries, each containing:
            - code: Stock code
            - update_time: Update time (YYYY-MM-DD HH:mm:ss)
            - last_price: Latest price
            - open_price: Opening price
            - high_price: Highest price
            - low_price: Lowest price
            - prev_close_price: Previous closing price
            - volume: Trading volume
            - turnover: Trading amount
            - turnover_rate: Turnover rate
            - amplitude: Price amplitude
            - dark_status: Dark pool status (0: Normal)
            - list_time: Listing date
            - price_spread: Price spread
            - stock_owner: Stock owner
            - lot_size: Lot size
            - sec_status: Security status
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_STOCK_QUOTE_FAILED: Failed to get stock quote
        
    Note:
        - Stock quote contains latest market data
        - Can request multiple stocks at once
        - Does not include historical data
        - Consider actual needs when selecting stocks
        - Handle exceptions properly
    """
    safe_log("info", f"Getting stock quotes for symbols: {symbols}", ctx)
    
    try:
        ret, data = quote_ctx.get_stock_quote(symbols)
        if ret != RET_OK:
            error_msg = f"Failed to get stock quote: {str(data)}"
            safe_log("error", error_msg, ctx)
            return {'error': error_msg}
    
        # Convert DataFrame to dict if necessary
        if hasattr(data, 'to_dict'):
            result = {
                'quote_list': data.to_dict('records')
            }
        else:
            result = {
                'quote_list': data
            }

        safe_log("info", f"Successfully retrieved quotes for {len(symbols)} symbols", ctx)
        return result

    except Exception as e:
        error_msg = f"Exception in get_stock_quote: {str(e)}"
        safe_log("error", error_msg, ctx)
        return {'error': error_msg}

@mcp.tool()
async def get_market_snapshot(symbols: List[str]) -> Dict[str, Any]:
    """Get market snapshot for given symbols
    
    Args:
        symbols: List of stock codes, e.g. ["HK.00700", "US.AAPL", "SH.600519"]
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing snapshot data including:
        - snapshot_list: List of snapshot data entries, each containing:
            - code: Stock code
            - update_time: Update time (YYYY-MM-DD HH:mm:ss)
            - last_price: Latest price
            - open_price: Opening price
            - high_price: Highest price
            - low_price: Lowest price
            - prev_close_price: Previous closing price
            - volume: Trading volume
            - turnover: Trading amount
            - turnover_rate: Turnover rate
            - amplitude: Price amplitude
            - dark_status: Dark pool status (0: Normal)
            - list_time: Listing date
            - price_spread: Price spread
            - stock_owner: Stock owner
            - lot_size: Lot size
            - sec_status: Security status
            - bid_price: List of bid prices
            - bid_volume: List of bid volumes
            - ask_price: List of ask prices
            - ask_volume: List of ask volumes
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_MARKET_SNAPSHOT_FAILED: Failed to get market snapshot
        
    Note:
        - Market snapshot contains latest market data
        - Can request multiple stocks at once
        - Does not include historical data
        - Consider actual needs when selecting stocks
        - Handle exceptions properly
    """
    ret, data = quote_ctx.get_market_snapshot(symbols)
    if ret != RET_OK:
        return {'error': str(data)}
    
    # Convert DataFrame to dict if necessary
    if hasattr(data, 'to_dict'):
        result = {
            'snapshot_list': data.to_dict('records')
        }
    else:
        result = {
            'snapshot_list': data
        }
    
    return result

@mcp.tool()
async def get_cur_kline(symbol: str, ktype: str, count: int = 100) -> Dict[str, Any]:
    """Get current K-line data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
        ktype: K-line type, options:
            - "K_1M": 1 minute
            - "K_5M": 5 minutes
            - "K_15M": 15 minutes
            - "K_30M": 30 minutes
            - "K_60M": 60 minutes
            - "K_DAY": Daily
            - "K_WEEK": Weekly
            - "K_MON": Monthly
            - "K_QUARTER": Quarterly
            - "K_YEAR": Yearly
        count: Number of K-lines to return (default: 100)
            Range: 1-1000
    
    Returns:
        Dict containing K-line data including:
        - kline_list: List of K-line data entries, each containing:
            - code: Stock code
            - kline_type: K-line type
            - update_time: Update time (YYYY-MM-DD HH:mm:ss)
            - open_price: Opening price
            - high_price: Highest price
            - low_price: Lowest price
            - close_price: Closing price
            - volume: Trading volume
            - turnover: Trading amount
            - pe_ratio: Price-to-earnings ratio
            - turnover_rate: Turnover rate
            - timestamp: K-line time
            - kline_status: K-line status (0: Normal)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - INVALID_SUBTYPE: Invalid K-line type
        - GET_CUR_KLINE_FAILED: Failed to get K-line data
        
    Note:
        - IMPORTANT: Must subscribe to the K-line data first using subscribe() with the corresponding K-line type
        - K-line data contains latest market data
        - Can request multiple stocks at once
        - Different periods have different update frequencies
        - Consider actual needs when selecting stocks and K-line types
        - Handle exceptions properly
    """
    ret, data = quote_ctx.get_cur_kline(
        code=symbol,
        ktype=ktype,
        num=count
    )
    if ret != RET_OK:
        return {'error': str(data)}
    
    # Convert DataFrame to dict if necessary
    if hasattr(data, 'to_dict'):
        result = {
            'kline_list': data.to_dict('records')
        }
    else:
        result = {
            'kline_list': data
        }
    
    return result

@mcp.tool()
async def get_history_kline(symbol: str, ktype: str, start: str, end: str, count: int = 100) -> Dict[str, Any]:
    """Get historical K-line data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
        ktype: K-line type, options:
            - "K_1M": 1 minute
            - "K_3M": 3 minutes
            - "K_5M": 5 minutes
            - "K_15M": 15 minutes
            - "K_30M": 30 minutes
            - "K_60M": 60 minutes
            - "K_DAY": Daily
            - "K_WEEK": Weekly
            - "K_MON": Monthly
        start: Start date in format "YYYY-MM-DD"
        end: End date in format "YYYY-MM-DD"
        count: Number of K-lines to return (default: 100)
            Range: 1-1000
    
    Note:
        - Limited to 30 stocks per 30 days
        - Used quota will be automatically released after 30 days
        - Different K-line types have different update frequencies
        - Historical data availability varies by market and stock
    
    Returns:
        Dict containing K-line data including:
        - code: Stock code
        - kline_type: K-line type
        - time_key: K-line time (YYYY-MM-DD HH:mm:ss)
        - open: Opening price
        - close: Closing price
        - high: Highest price
        - low: Lowest price
        - volume: Trading volume
        - turnover: Trading amount
        - pe_ratio: Price-to-earnings ratio
        - turnover_rate: Turnover rate
        - change_rate: Price change rate
        - last_close: Last closing price
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - INVALID_SUBTYPE: Invalid K-line type
        - GET_HISTORY_KLINE_FAILED: Failed to get historical K-line data
    """
    ret, data, page_req_key = quote_ctx.request_history_kline(
        code=symbol,
        start=start,
        end=end,
        ktype=ktype,
        max_count=count
    )
    
    if ret != RET_OK:
        return {'error': data}
    
    result = data.to_dict()
    
    # If there are more pages, continue fetching
    while page_req_key is not None:
        ret, data, page_req_key = quote_ctx.request_history_kline(
            code=symbol,
            start=start,
            end=end,
            ktype=ktype,
            max_count=count,
            page_req_key=page_req_key
        )
        if ret != RET_OK:
            return {'error': data}
        # Append new data to result
        new_data = data.to_dict()
        for key in result:
            if isinstance(result[key], list):
                result[key].extend(new_data[key])
    
    return result

@mcp.tool()
async def get_rt_data(symbol: str) -> Dict[str, Any]:
    """Get real-time data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing real-time data including:
        - rt_data_list: List of real-time data entries, each containing:
            - code: Stock code
            - time: Time (HH:mm:ss)
            - price: Latest price
            - volume: Trading volume
            - turnover: Trading amount
            - avg_price: Average price
            - timestamp: Update time (YYYY-MM-DD HH:mm:ss)
            - rt_data_status: Real-time data status (0: Normal)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_RT_DATA_FAILED: Failed to get real-time data
        
    Note:
        - IMPORTANT: Must subscribe to RT_DATA first using subscribe()
        - Real-time data is updated frequently
        - Contains latest data only, not historical data
        - Update frequency varies by market and stock
        - Consider using callbacks for real-time processing
    """
    ret, data = quote_ctx.get_rt_data(symbol)
    if ret != RET_OK:
        return {'error': str(data)}
    
    # Convert DataFrame to dict if necessary
    if hasattr(data, 'to_dict'):
        result = {
            'rt_data_list': data.to_dict('records')
        }
    else:
        result = {
            'rt_data_list': data
        }
    
    return result

@mcp.tool()
async def get_ticker(symbol: str) -> Dict[str, Any]:
    """Get ticker data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing ticker data including:
        - code: Stock code
        - sequence: Sequence number
        - price: Deal price
        - volume: Deal volume
        - turnover: Deal amount
        - ticker_direction: Ticker direction
            1: Bid order
            2: Ask order
            3: Neutral order
        - ticker_type: Ticker type
            1: Regular trade
            2: Cancel trade
            3: Trading at closing price
            4: Off-exchange trade
            5: After-hours trade
        - timestamp: Deal time (YYYY-MM-DD HH:mm:ss)
        - ticker_status: Ticker status (0: Normal)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_RT_TICKER_FAILED: Failed to get ticker data
        
    Note:
        - IMPORTANT: Must subscribe to TICKER first using subscribe()
        - Ticker data is updated in real-time
        - High update frequency, large data volume
        - Update frequency varies by market and stock
        - Consider using callbacks for real-time processing
    """
    ret, data = quote_ctx.get_rt_ticker(symbol)
    return handle_return_data(ret, data)

@mcp.tool()
async def get_order_book(symbol: str) -> Dict[str, Any]:
    """Get order book data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing order book data including:
        - code: Stock code
        - update_time: Update time (YYYY-MM-DD HH:mm:ss)
        - bid_price: List of bid prices (up to 10 levels)
        - bid_volume: List of bid volumes (up to 10 levels)
        - ask_price: List of ask prices (up to 10 levels)
        - ask_volume: List of ask volumes (up to 10 levels)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_ORDER_BOOK_FAILED: Failed to get order book data
        
    Note:
        - IMPORTANT: Must subscribe to ORDER_BOOK first using subscribe()
        - Order book data is updated in real-time
        - Contains latest bid/ask information only
        - Number of price levels may vary by market
        - Update frequency varies by market and stock
    """
    ret, data = quote_ctx.get_order_book(symbol)
    return handle_return_data(ret, data)

@mcp.tool()
async def get_broker_queue(symbol: str) -> Dict[str, Any]:
    """Get broker queue data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing broker queue data including:
        - code: Stock code
        - update_time: Update time (YYYY-MM-DD HH:mm:ss)
        - bid_broker_id: List of bid broker IDs
        - bid_broker_name: List of bid broker names
        - bid_broker_pos: List of bid broker positions
        - ask_broker_id: List of ask broker IDs
        - ask_broker_name: List of ask broker names
        - ask_broker_pos: List of ask broker positions
        - timestamp: Update timestamp
        - broker_status: Broker queue status (0: Normal)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_BROKER_QUEUE_FAILED: Failed to get broker queue data
        
    Note:
        - IMPORTANT: Must subscribe to BROKER first using subscribe()
        - Broker queue data is updated in real-time
        - Shows broker information for both bid and ask sides
        - Number of brokers may vary by market
        - Update frequency varies by market and stock
        - Mainly used for displaying broker trading activities
    """
    ret, data = quote_ctx.get_broker_queue(symbol)
    return handle_return_data(ret, data)

@mcp.tool()
async def subscribe(symbols: List[str], sub_types: List[str]) -> Dict[str, Any]:
    """Subscribe to real-time data
    
    Args:
        symbols: List of stock codes, e.g. ["HK.00700", "US.AAPL"]
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
        sub_types: List of subscription types, options:
            - "QUOTE": Basic quote (price, volume, etc.)
            - "ORDER_BOOK": Order book (bid/ask)
            - "TICKER": Ticker (trades)
            - "RT_DATA": Real-time data
            - "BROKER": Broker queue
            - "K_1M": 1-minute K-line
            - "K_3M": 3-minute K-line
            - "K_5M": 5-minute K-line
            - "K_15M": 15-minute K-line
            - "K_30M": 30-minute K-line
            - "K_60M": 60-minute K-line
            - "K_DAY": Daily K-line
            - "K_WEEK": Weekly K-line
            - "K_MON": Monthly K-line
            - "K_QUARTER": Quarterly K-line
            - "K_YEAR": Yearly K-line
    
    Note:
        - Maximum 100 symbols per request
        - Maximum 5 subscription types per request
        - Each socket can subscribe up to 500 symbols
        - Data will be pushed through callbacks
        - Consider unsubscribing when data is no longer needed
    
    Returns:
        Dict containing subscription result:
        - status: "success" or error message
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - INVALID_SUBTYPE: Invalid subscription type
        - SUBSCRIBE_FAILED: Failed to subscribe
    """
    ret, data = quote_ctx.subscribe(symbols, sub_types)
    if ret != RET_OK:
        return {'error': data}
    return {"status": "success"}

@mcp.tool()
async def unsubscribe(symbols: List[str], sub_types: List[str]) -> Dict[str, Any]:
    """Unsubscribe from real-time data
    
    Args:
        symbols: List of stock codes, e.g. ["HK.00700", "US.AAPL"]
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
        sub_types: List of subscription types, options:
            - "QUOTE": Basic quote (price, volume, etc.)
            - "ORDER_BOOK": Order book (bid/ask)
            - "TICKER": Ticker (trades)
            - "RT_DATA": Real-time data
            - "BROKER": Broker queue
            - "K_1M": 1-minute K-line
            - "K_5M": 5-minute K-line
            - "K_15M": 15-minute K-line
            - "K_30M": 30-minute K-line
            - "K_60M": 60-minute K-line
            - "K_DAY": Daily K-line
            - "K_WEEK": Weekly K-line
            - "K_MON": Monthly K-line
            
    Returns:
        Dict containing unsubscription result:
        - status: "success" or error message
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - INVALID_SUBTYPE: Invalid subscription type
        - UNSUBSCRIBE_FAILED: Failed to unsubscribe
    """
    ret, data = quote_ctx.unsubscribe(symbols, sub_types)
    if ret != RET_OK:
        return {'error': data}
    return {"status": "success"}

# Derivatives Tools
@mcp.tool()
async def get_option_chain(symbol: str, start: str, end: str) -> Dict[str, Any]:
    """Get option chain data
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
        start: Start date in format "YYYY-MM-DD"
        end: End date in format "YYYY-MM-DD"
    
    Returns:
        Dict containing option chain data including:
        - stock_code: Underlying stock code
        - stock_name: Underlying stock name
        - option_list: List of option contracts, each containing:
            - option_code: Option code
            - option_name: Option name
            - option_type: Option type (CALL/PUT)
            - strike_price: Strike price
            - expiry_date: Expiry date
            - last_price: Latest price
            - volume: Trading volume
            - open_interest: Open interest
            - implied_volatility: Implied volatility
            - delta: Delta value
            - gamma: Gamma value
            - theta: Theta value
            - vega: Vega value
            - update_time: Update time
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - INVALID_STOCKCODE: Invalid stock code
        - INVALID_EXPIRYDATE: Invalid expiry date
        - GET_OPTION_CHAIN_FAILED: Failed to get option chain
        
    Note:
        - Option chain data is essential for options trading
        - Contains both call and put options
        - Includes Greeks for risk management
        - Data is updated during trading hours
        - Consider using with option expiration dates API
    """
    ret, data = quote_ctx.get_option_chain(code=symbol, start=start, end=end)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_option_expiration_date(symbol: str) -> Dict[str, Any]:
    """Get option expiration dates
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
    
    Returns:
        Dict containing expiration dates:
        - strike_time: List of expiration dates in format "YYYY-MM-DD"
        - option_expiry_info: Additional expiry information
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - INVALID_STOCKCODE: Invalid stock code
        - GET_OPTION_EXPIRATION_FAILED: Failed to get expiration dates
        
    Note:
        - Use this API before querying option chain
        - Different stocks may have different expiry dates
        - Expiry dates are typically on monthly/weekly cycles
        - Not all stocks have listed options
    """
    ret, data = quote_ctx.get_option_expiration_date(symbol)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_option_condor(symbol: str, expiry: str, strike_price: float) -> Dict[str, Any]:
    """Get option condor strategy data
    
    WARNING: This interface may be deprecated in the latest API documentation (v9.6).
    Please use get_option_chain for option data instead.
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
        expiry: Option expiration date in format "YYYY-MM-DD"
        strike_price: Strike price of the option
        
    Returns:
        Dict containing condor strategy data including:
        - strategy_name: Strategy name
        - option_list: List of options in the strategy
        - risk_metrics: Risk metrics for the strategy
        - profit_loss: Profit/loss analysis
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - INVALID_STOCKCODE: Invalid stock code
        - INVALID_EXPIRYDATE: Invalid expiry date
        - INVALID_STRIKEPRICE: Invalid strike price
        - GET_OPTION_CONDOR_FAILED: Failed to get condor data
        
    Note:
        - Condor is a neutral options trading strategy
        - Involves four different strike prices
        - Limited risk and limited profit potential
        - Best used in low volatility environments
        - This interface may not be available in the latest API version
    """
    ret, data = quote_ctx.get_option_condor(symbol, expiry, strike_price)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_option_butterfly(symbol: str, expiry: str, strike_price: float) -> Dict[str, Any]:
    """Get option butterfly strategy data
    
    WARNING: This interface may be deprecated in the latest API documentation (v9.6).
    Please use get_option_chain for option data instead.
    
    Args:
        symbol: Stock code, e.g. "HK.00700", "US.AAPL"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
        expiry: Option expiration date in format "YYYY-MM-DD"
        strike_price: Strike price of the option
        
    Returns:
        Dict containing butterfly strategy data including:
        - strategy_name: Strategy name
        - option_list: List of options in the strategy
        - risk_metrics: Risk metrics for the strategy
        - profit_loss: Profit/loss analysis
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - INVALID_STOCKCODE: Invalid stock code
        - INVALID_EXPIRYDATE: Invalid expiry date
        - INVALID_STRIKEPRICE: Invalid strike price
        - GET_OPTION_BUTTERFLY_FAILED: Failed to get butterfly data
        
    Note:
        - Butterfly is a neutral options trading strategy
        - Involves three different strike prices
        - Limited risk and limited profit potential
        - Maximum profit at middle strike price
        - Best used when expecting low volatility
        - This interface may not be available in the latest API version
    """
    ret, data = quote_ctx.get_option_butterfly(symbol, expiry, strike_price)
    return data.to_dict() if ret == RET_OK else {'error': data}

# Account Query Tools
@mcp.tool()
async def get_account_list(ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """Get trading account list
    
    Returns:
        Dict containing account list information including:
        - acc_list: List of accounts, each containing:
            - acc_id: Account ID
            - trd_env: Trading environment (SIMULATE/REAL)
            - acc_type: Account type
            - card_num: Card number
            - security_firm: Security firm
            - trd_market: Trading market (HK/US/CN)
            - acc_state: Account state
            - acc_name: Account name
            
    Raises:
        - Failed to initialize trade connection
        - Failed to get account list
        
    Note:
        - Requires trade connection to be initialized
        - Returns all trading accounts available for the user
        - Different accounts may have different trading permissions
        - Account list includes both simulated and real trading accounts
    """
    safe_log("info", "Attempting to get account list", ctx)

    if not init_trade_connection():
        error_msg = 'Failed to initialize trade connection'
        safe_log("error", error_msg, ctx)
        return {'error': error_msg}

    try:
        ret, data = trade_ctx.get_acc_list()
        result = handle_return_data(ret, data)

        if 'error' not in result:
            safe_log("info", "Successfully retrieved account list", ctx)
        else:
            safe_log("error", f"Failed to get account list: {result['error']}", ctx)

        return result
    except Exception as e:
        error_msg = f"Exception in get_account_list: {str(e)}"
        safe_log("error", error_msg, ctx)
        return {'error': error_msg}

@mcp.tool()
async def get_funds() -> Dict[str, Any]:
    """Get account funds information
    
    Returns:
        Dict containing account funds information including:
        - total_assets: Total assets
        - cash: Available cash
        - market_val: Market value of positions
        - power: Trading power
        - max_power_short: Maximum short trading power
        - net_cash_power: Net cash power
        - long_mv: Long position market value
        - short_mv: Short position market value
        - pending_asset: Pending assets
        - currency: Currency code
        
    Raises:
        - Failed to initialize trade connection
        - Failed to get account funds
        
    Note:
        - Requires trade connection to be initialized
        - Returns real-time account information
        - Data format may vary by market and account type
    """
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    try:
        ret, data = trade_ctx.accinfo_query()
        if ret != RET_OK:
            return {'error': str(data)}
        
        if data is None or data.empty:
            return {'error': 'No account information available'}
            
        return handle_return_data(ret, data)
    except Exception as e:
        return {'error': f'Failed to get account funds: {str(e)}'}

@mcp.tool()
async def get_positions() -> Dict[str, Any]:
    """Get account positions list
    
    Returns:
        Dict containing position list information including:
        - code: Stock code
        - stock_name: Stock name
        - qty: Position quantity
        - can_sell_qty: Sellable quantity
        - cost_price: Average cost price
        - cost_price_valid: Whether cost price is valid
        - market_val: Market value
        - nominal_price: Nominal price
        - pl_ratio: Profit/loss ratio
        - pl_ratio_valid: Whether P/L ratio is valid
        - pl_val: Profit/loss value
        - pl_val_valid: Whether P/L value is valid
        - today_buy_val: Today's buy value
        - today_buy_qty: Today's buy quantity
        - today_sell_val: Today's sell value
        - today_sell_qty: Today's sell quantity
        
    Raises:
        - Failed to initialize trade connection
        - Failed to get positions
        
    Note:
        - Requires trade connection to be initialized
        - Returns all positions in the account
        - Includes both long and short positions
    """
    """Get account positions"""
    if not position_feature_enabled():
        return feature_disabled_error("Position feature", "FUTU_ENABLE_POSITIONS")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.position_list_query()
    return handle_return_data(ret, data)

@mcp.tool()
async def get_max_power() -> Dict[str, Any]:
    """Get maximum tradable quantity for the account
    
    Args:
        symbol: Optional stock code. If provided, returns max tradable quantity for that stock.
            If not provided, returns general trading power information.
            Format: "market.code", e.g. "HK.00700", "US.AAPL"
    
    Returns:
        Dict containing maximum trading power information including:
        - max_cash_buy: Maximum cash buy quantity
        - max_cash_and_margin_buy: Maximum cash + margin buy quantity
        - max_position_sell: Maximum position sell quantity
        - max_sell_short: Maximum short sell quantity
        - max_buy_back: Maximum buy back quantity
        - net_cash_power: Net cash power
        - long_mv: Long position market value
        - short_mv: Short position market value
        
    Raises:
        - Failed to initialize trade connection
        - Failed to get maximum trading power
        
    Note:
        - Requires trade connection to be initialized
        - If symbol is provided, calculates max tradable quantity for that specific stock
        - Takes into account margin requirements, available cash, and position limits
    """
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.get_max_power()
    return handle_return_data(ret, data)

@mcp.tool()
async def get_margin_ratio(symbol: str) -> Dict[str, Any]:
    """Get margin trading data for a security
    
    Args:
        symbol: Stock code in format "market.code", e.g. "HK.00700", "US.AAPL"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
    
    Returns:
        Dict containing margin trading data including:
        - financing_ratio: Financing ratio
        - margin_ratio: Margin ratio
        - financing_cash: Available financing cash
        - collateral_ratio: Collateral ratio
        - available_margin: Available margin
        - margin_call_price: Margin call price
        - forced_liquidation_price: Forced liquidation price
        
    Raises:
        - Failed to initialize trade connection
        - INVALID_PARAM: Invalid parameter
        - INVALID_CODE: Invalid stock code format
        - GET_MARGIN_RATIO_FAILED: Failed to get margin ratio
        
    Note:
        - Requires trade connection to be initialized
        - Only available for margin trading enabled accounts
        - Data may vary by market and security type
    """
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.get_margin_ratio(symbol)
    return handle_return_data(ret, data)


# Trading Tools
@mcp.tool()
async def unlock_trade(
    password: Optional[str] = None,
    password_md5: Optional[str] = None,
    is_unlock: bool = True,
) -> Dict[str, Any]:
    """Unlock trading operations for real/simulated accounts."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.unlock_trade(password=password, password_md5=password_md5, is_unlock=is_unlock)
    return handle_return_data(ret, data)


@mcp.tool()
async def place_order(
    code: str,
    price: float,
    qty: float,
    trd_side: str,
    order_type: str = "NORMAL",
    adjust_limit: float = 0,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    remark: Optional[str] = None,
    time_in_force: str = "DAY",
    fill_outside_rth: bool = False,
    aux_price: Optional[float] = None,
    trail_type: Optional[str] = None,
    trail_value: Optional[float] = None,
    trail_spread: Optional[float] = None,
    session: str = "N/A",
) -> Dict[str, Any]:
    """Place a trading order."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        side_value = parse_enum_value(TrdSide, trd_side, "trd_side")
        order_type_value = parse_enum_value(OrderType, order_type, "order_type")
        trd_env_value = get_trade_env_value(trd_env)
        time_in_force_value = parse_enum_value(TimeInForce, time_in_force, "time_in_force")
        session_value = (
            parse_enum_value(Session, session, "session")
            if session and session != "N/A"
            else session
        )
        trail_type_value = (
            parse_enum_value(TrailType, trail_type, "trail_type")
            if trail_type
            else None
        )

        ret, data = trade_ctx.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=side_value,
            order_type=order_type_value,
            adjust_limit=adjust_limit,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            remark=remark,
            time_in_force=time_in_force_value,
            fill_outside_rth=fill_outside_rth,
            aux_price=aux_price,
            trail_type=trail_type_value,
            trail_value=trail_value,
            trail_spread=trail_spread,
            session=session_value,
        )
        return handle_return_data(ret, data)
    except Exception as e:
        return {'error': f'Failed to place order: {str(e)}'}


@mcp.tool()
async def modify_order(
    modify_order_op: str,
    order_id: str,
    qty: float,
    price: float,
    adjust_limit: float = 0,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    aux_price: Optional[float] = None,
    trail_type: Optional[str] = None,
    trail_value: Optional[float] = None,
    trail_spread: Optional[float] = None,
) -> Dict[str, Any]:
    """Modify or cancel an existing order."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        modify_op_value = parse_enum_value(ModifyOrderOp, modify_order_op, "modify_order_op")
        trd_env_value = get_trade_env_value(trd_env)
        trail_type_value = (
            parse_enum_value(TrailType, trail_type, "trail_type")
            if trail_type
            else None
        )

        ret, data = trade_ctx.modify_order(
            modify_order_op=modify_op_value,
            order_id=order_id,
            qty=qty,
            price=price,
            adjust_limit=adjust_limit,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            aux_price=aux_price,
            trail_type=trail_type_value,
            trail_value=trail_value,
            trail_spread=trail_spread,
        )
        return handle_return_data(ret, data)
    except Exception as e:
        return {'error': f'Failed to modify order: {str(e)}'}


@mcp.tool()
async def cancel_order(
    order_id: str,
    qty: float = 0,
    price: float = 0,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
) -> Dict[str, Any]:
    """Cancel an existing order by order_id."""
    return await modify_order(
        modify_order_op="CANCEL",
        order_id=order_id,
        qty=qty,
        price=price,
        trd_env=trd_env,
        acc_id=acc_id,
        acc_index=acc_index,
    )


@mcp.tool()
async def cancel_all_orders(
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    trdmarket: str = "N/A",
) -> Dict[str, Any]:
    """Cancel all cancellable orders for a trading account."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    try:
        trd_env_value = get_trade_env_value(trd_env)
        trd_market_value = (
            parse_enum_value(TrdMarket, trdmarket, "trdmarket")
            if trdmarket and trdmarket != "N/A"
            else trdmarket
        )
        ret, data = trade_ctx.cancel_all_order(
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            trdmarket=trd_market_value,
        )
        return handle_return_data(ret, data)
    except Exception as e:
        return {'error': f'Failed to cancel all orders: {str(e)}'}


@mcp.tool()
async def get_order_list(
    order_id: str = "",
    status_filter_list: Optional[List[str]] = None,
    code: str = "",
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    refresh_cache: bool = False,
    order_market: str = "N/A",
) -> Dict[str, Any]:
    """Get current order list."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        status_values = parse_enum_list(OrderStatus, status_filter_list, "status_filter_list")
        trd_env_value = get_trade_env_value(trd_env)
        order_market_value = (
            parse_enum_value(TrdMarket, order_market, "order_market")
            if order_market and order_market != "N/A"
            else order_market
        )
        ret, data = trade_ctx.order_list_query(
            order_id=order_id,
            status_filter_list=status_values,
            code=code,
            start=start,
            end=end,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            refresh_cache=refresh_cache,
            order_market=order_market_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "order_list")
    except Exception as e:
        return {'error': f'Failed to get order list: {str(e)}'}


@mcp.tool()
async def get_history_order_list(
    status_filter_list: Optional[List[str]] = None,
    code: str = "",
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    order_market: str = "N/A",
) -> Dict[str, Any]:
    """Get historical order list."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        status_values = parse_enum_list(OrderStatus, status_filter_list, "status_filter_list")
        trd_env_value = get_trade_env_value(trd_env)
        order_market_value = (
            parse_enum_value(TrdMarket, order_market, "order_market")
            if order_market and order_market != "N/A"
            else order_market
        )
        ret, data = trade_ctx.history_order_list_query(
            status_filter_list=status_values,
            code=code,
            start=start,
            end=end,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            order_market=order_market_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "history_order_list")
    except Exception as e:
        return {'error': f'Failed to get history order list: {str(e)}'}


@mcp.tool()
async def get_deal_list(
    code: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    refresh_cache: bool = False,
    deal_market: str = "N/A",
) -> Dict[str, Any]:
    """Get current deal list."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        trd_env_value = get_trade_env_value(trd_env)
        deal_market_value = (
            parse_enum_value(TrdMarket, deal_market, "deal_market")
            if deal_market and deal_market != "N/A"
            else deal_market
        )
        ret, data = trade_ctx.deal_list_query(
            code=code,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            refresh_cache=refresh_cache,
            deal_market=deal_market_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "deal_list")
    except Exception as e:
        return {'error': f'Failed to get deal list: {str(e)}'}


@mcp.tool()
async def get_history_deal_list(
    code: str = "",
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    deal_market: str = "N/A",
) -> Dict[str, Any]:
    """Get historical deal list."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        trd_env_value = get_trade_env_value(trd_env)
        deal_market_value = (
            parse_enum_value(TrdMarket, deal_market, "deal_market")
            if deal_market and deal_market != "N/A"
            else deal_market
        )
        ret, data = trade_ctx.history_deal_list_query(
            code=code,
            start=start,
            end=end,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            deal_market=deal_market_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "history_deal_list")
    except Exception as e:
        return {'error': f'Failed to get history deal list: {str(e)}'}


@mcp.tool()
async def get_position_list(
    code: str = "",
    pl_ratio_min: Optional[float] = None,
    pl_ratio_max: Optional[float] = None,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    refresh_cache: bool = False,
    position_market: str = "N/A",
) -> Dict[str, Any]:
    """Get position list with optional filtering."""
    if not position_feature_enabled():
        return feature_disabled_error("Position feature", "FUTU_ENABLE_POSITIONS")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}

    try:
        trd_env_value = get_trade_env_value(trd_env)
        position_market_value = (
            parse_enum_value(TrdMarket, position_market, "position_market")
            if position_market and position_market != "N/A"
            else position_market
        )
        ret, data = trade_ctx.position_list_query(
            code=code,
            pl_ratio_min=pl_ratio_min,
            pl_ratio_max=pl_ratio_max,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            refresh_cache=refresh_cache,
            position_market=position_market_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "position_list")
    except Exception as e:
        return {'error': f'Failed to get position list: {str(e)}'}


@mcp.tool()
async def get_order_fee(
    order_id_list: Optional[List[str]] = None,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
) -> Dict[str, Any]:
    """Get order fee details."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    try:
        trd_env_value = get_trade_env_value(trd_env)
        ret, data = trade_ctx.order_fee_query(
            order_id_list=order_id_list or [],
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "order_fee_list")
    except Exception as e:
        return {'error': f'Failed to get order fee: {str(e)}'}


@mcp.tool()
async def get_acc_cash_flow(
    clearing_date: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    cashflow_direction: str = "N/A",
) -> Dict[str, Any]:
    """Get account cash-flow details."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    try:
        trd_env_value = get_trade_env_value(trd_env)
        cashflow_direction_value = (
            parse_enum_value(CashFlowDirection, cashflow_direction, "cashflow_direction")
            if cashflow_direction and cashflow_direction != "N/A"
            else cashflow_direction
        )
        ret, data = trade_ctx.get_acc_cash_flow(
            clearing_date=clearing_date,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            cashflow_direction=cashflow_direction_value,
        )
        if ret != RET_OK:
            return {'error': str(data)}
        return dataframe_to_records(data, "cash_flow_list")
    except Exception as e:
        return {'error': f'Failed to get account cash flow: {str(e)}'}


@mcp.tool()
async def get_acc_trading_info(
    order_type: str,
    code: str,
    price: float,
    order_id: Optional[str] = None,
    adjust_limit: float = 0,
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
    session: str = "N/A",
) -> Dict[str, Any]:
    """Get account trading capability before placing/modifying an order."""
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    try:
        order_type_value = parse_enum_value(OrderType, order_type, "order_type")
        trd_env_value = get_trade_env_value(trd_env)
        session_value = (
            parse_enum_value(Session, session, "session")
            if session and session != "N/A"
            else session
        )
        ret, data = trade_ctx.acctradinginfo_query(
            order_type=order_type_value,
            code=code,
            price=price,
            order_id=order_id,
            adjust_limit=adjust_limit,
            trd_env=trd_env_value,
            acc_id=acc_id,
            acc_index=acc_index,
            session=session_value,
        )
        return handle_return_data(ret, data)
    except Exception as e:
        return {'error': f'Failed to get account trading info: {str(e)}'}


@mcp.tool()
async def get_acc_list(ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """Official-name alias of get_account_list."""
    return await get_account_list(ctx=ctx)


@mcp.tool()
async def get_fund_list() -> Dict[str, Any]:
    """Official-name alias of get_funds."""
    return await get_funds()


@mcp.tool()
async def get_asset_list() -> Dict[str, Any]:
    """Asset list view based on current accinfo query."""
    return await get_funds()


@mcp.tool()
async def get_history_position_list(
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
) -> Dict[str, Any]:
    """Compatibility tool for official trade overview; not supported by current futu Python SDK."""
    _ = (start, end, trd_env, acc_id, acc_index)
    if not position_feature_enabled():
        return feature_disabled_error("Position feature", "FUTU_ENABLE_POSITIONS")
    return {
        "error": (
            "get_history_position_list is not supported by the installed futu-api SDK "
            "(OpenSecTradeContext has no history_position_list_query method)."
        )
    }


@mcp.tool()
async def get_history_asset_list(
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
) -> Dict[str, Any]:
    """Compatibility tool for official trade overview; not supported by current futu Python SDK."""
    _ = (start, end, trd_env, acc_id, acc_index)
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    return {
        "error": (
            "get_history_asset_list is not supported by the installed futu-api SDK "
            "(OpenSecTradeContext has no history_asset_list_query method)."
        )
    }


@mcp.tool()
async def get_history_fund_list(
    start: str = "",
    end: str = "",
    trd_env: Optional[str] = None,
    acc_id: int = 0,
    acc_index: int = 0,
) -> Dict[str, Any]:
    """Compatibility tool for official trade overview; not supported by current futu Python SDK."""
    _ = (start, end, trd_env, acc_id, acc_index)
    if not trading_feature_enabled():
        return feature_disabled_error("Trading feature", "FUTU_ENABLE_TRADING")
    return {
        "error": (
            "get_history_fund_list is not supported by the installed futu-api SDK "
            "(OpenSecTradeContext has no history_fund_list_query method)."
        )
    }

# Market Information Tools
@mcp.tool()
async def get_market_state(market: str) -> Dict[str, Any]:
    """Get market state
    
    Args:
        market: Market code, options:
            - "HK": Hong Kong market (includes pre-market, continuous trading, afternoon, closing auction)
            - "US": US market (includes pre-market, continuous trading, after-hours)
            - "SH": Shanghai market (includes pre-opening, morning, afternoon, closing auction)
            - "SZ": Shenzhen market (includes pre-opening, morning, afternoon, closing auction)
    
    Returns:
        Dict containing market state information including:
        - market: Market code
        - market_state: Market state code
            - NONE: Market not available
            - AUCTION: Auction period
            - WAITING_OPEN: Waiting for market open
            - MORNING: Morning session
            - REST: Lunch break
            - AFTERNOON: Afternoon session
            - CLOSED: Market closed
            - PRE_MARKET_BEGIN: Pre-market begin
            - PRE_MARKET_END: Pre-market end
            - AFTER_HOURS_BEGIN: After-hours begin
            - AFTER_HOURS_END: After-hours end
        - market_state_desc: Description of market state
        - update_time: Update time (YYYY-MM-DD HH:mm:ss)
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - GET_MARKET_STATE_FAILED: Failed to get market state
        
    Note:
        - Market state is updated in real-time
        - Different markets have different trading hours
        - Consider timezone differences
        - Market state affects trading operations
        - Recommended to check state before trading
    """
    ret, data = quote_ctx.get_market_state(market)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_stock_basicinfo(stock_code: str, market: str = None) -> Dict[str, Any]:
    """Get stock basic information
    
    Args:
        stock_code: Stock code in format "market.code", e.g. "HK.00700", "US.AAPL", "SH.600519"
            Format: {market}.{code}
            - HK: Hong Kong stocks
            - US: US stocks
            - SH: Shanghai stocks
            - SZ: Shenzhen stocks
        market: Optional market code. If not provided, will be parsed from stock_code
            Options:
            - "HK": Hong Kong market
            - "US": US market
            - "SH": Shanghai market
            - "SZ": Shenzhen market
    
    Returns:
        Dict containing stock basic information including:
        - stock_code: Stock code
        - stock_name: Stock name
        - market: Market code
        - stock_type: Stock type (e.g., "STOCK", "ETF", "WARRANT")
        - stock_child_type: Stock subtype (e.g., "MAIN_BOARD", "GEM")
        - list_time: Listing date
        - delist_time: Delisting date (if applicable)
        - lot_size: Lot size
        - stock_owner: Company name
        - issue_price: IPO price
        - issue_size: IPO size
        - net_profit: Net profit
        - net_profit_growth: Net profit growth rate
        - revenue: Revenue
        - revenue_growth: Revenue growth rate
        - eps: Earnings per share
        - pe_ratio: Price-to-earnings ratio
        - pb_ratio: Price-to-book ratio
        - dividend_ratio: Dividend ratio
        - stock_derivatives: List of related derivatives
        
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - INVALID_STOCKCODE: Invalid stock code
        - GET_STOCK_BASICINFO_FAILED: Failed to get stock information
        
    Note:
        - Contains static information about the security
        - Financial data may be delayed
        - Some fields may be empty for certain security types
        - Important for fundamental analysis
    """
    if market:
        ret, data = quote_ctx.get_stock_basicinfo(stock_code=stock_code, market=market)
    else:
        ret, data = quote_ctx.get_stock_basicinfo(stock_code=stock_code)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_stock_list(market: str) -> Dict[str, Any]:
    """Get stock list
    
    Args:
        market: Market code, options:
            - "HK": Hong Kong market
            - "US": US market
            - "SH": Shanghai market
            - "SZ": Shenzhen market
            
    Returns:
        Dict containing list of stocks:
        - stock_list: List of stocks, each containing:
            - code: Stock code
            - name: Stock name
            - market: Market code
            - lot_size: Lot size
            - stock_type: Stock type
            - list_time: Listing date
            - delist_time: Delisting date (if applicable)
            - status: Stock status (1: Listed, 0: Delisted)
            
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - GET_STOCK_LIST_FAILED: Failed to get stock list
        
    Note:
        - Returns all stocks in the specified market
        - Includes stocks, ETFs, warrants, etc.
        - Updated daily
        - Useful for market analysis and monitoring
        - Consider caching results for better performance
    """
    ret, data = quote_ctx.get_stock_list(market)
    return data.to_dict() if ret == RET_OK else {'error': data}

# Prompts
@mcp.prompt()
async def market_analysis(symbol: str) -> str:
    """Create a market analysis prompt"""
    return f"Please analyze the market data for {symbol}"

@mcp.prompt()
async def option_strategy(symbol: str, expiry: str) -> str:
    """Create an option strategy analysis prompt"""
    return f"Please analyze option strategies for {symbol} expiring on {expiry}"

@mcp.tool()
async def get_stock_filter(base_filters: List[Dict[str, Any]] = None, 
                         accumulate_filters: List[Dict[str, Any]] = None,
                         financial_filters: List[Dict[str, Any]] = None,
                         market: str = None,
                         page: int = 1,
                         page_size: int = 200) -> Dict[str, Any]:
    """Get filtered stock list based on conditions
    
    Args:
        base_filters: List of base filters with structure:
            {
                "field_name": int,  # StockField enum value
                "filter_min": float,  # Optional minimum value
                "filter_max": float,  # Optional maximum value
                "is_no_filter": bool,  # Optional, whether to skip filtering
                "sort_dir": int  # Optional, sort direction (0: No sort, 1: Ascending, 2: Descending)
            }
        accumulate_filters: List of accumulate filters with structure:
            {
                "field_name": int,  # AccumulateField enum value
                "filter_min": float,
                "filter_max": float,
                "is_no_filter": bool,
                "sort_dir": int,  # 0: No sort, 1: Ascending, 2: Descending
                "days": int  # Required, number of days to accumulate
            }
        financial_filters: List of financial filters with structure:
            {
                "field_name": int,  # FinancialField enum value
                "filter_min": float,
                "filter_max": float,
                "is_no_filter": bool,
                "sort_dir": int,  # 0: No sort, 1: Ascending, 2: Descending
                "quarter": int  # Required, financial quarter
            }
        market: Market code, options:
            - "HK.Motherboard": Hong Kong Main Board
            - "HK.GEM": Hong Kong GEM
            - "HK.BK1911": H-Share Main Board
            - "HK.BK1912": H-Share GEM
            - "US.NYSE": NYSE
            - "US.AMEX": AMEX
            - "US.NASDAQ": NASDAQ
            - "SH.3000000": Shanghai Main Board
            - "SZ.3000001": Shenzhen Main Board
            - "SZ.3000004": Shenzhen ChiNext
        page: Page number, starting from 1 (default: 1)
        page_size: Number of results per page, max 200 (default: 200)
    """
    # Create filter request
    req = {
        "begin": (page - 1) * page_size,
        "num": page_size
    }
    
    # Add market filter if specified
    if market:
        req["plate"] = {"plate_code": market}
    
    # Add base filters
    if base_filters:
        req["baseFilterList"] = []
        for f in base_filters:
            filter_item = {"fieldName": f["field_name"]}
            if "filter_min" in f:
                filter_item["filterMin"] = f["filter_min"]
            if "filter_max" in f:
                filter_item["filterMax"] = f["filter_max"]
            if "is_no_filter" in f:
                filter_item["isNoFilter"] = f["is_no_filter"]
            if "sort_dir" in f:
                filter_item["sortDir"] = f["sort_dir"]
            req["baseFilterList"].append(filter_item)
    
    # Add accumulate filters
    if accumulate_filters:
        req["accumulateFilterList"] = []
        for f in accumulate_filters:
            filter_item = {
                "fieldName": f["field_name"],
                "days": f["days"]
            }
            if "filter_min" in f:
                filter_item["filterMin"] = f["filter_min"]
            if "filter_max" in f:
                filter_item["filterMax"] = f["filter_max"]
            if "is_no_filter" in f:
                filter_item["isNoFilter"] = f["is_no_filter"]
            if "sort_dir" in f:
                filter_item["sortDir"] = f["sort_dir"]
            req["accumulateFilterList"].append(filter_item)
    
    # Add financial filters
    if financial_filters:
        req["financialFilterList"] = []
        for f in financial_filters:
            filter_item = {
                "fieldName": f["field_name"],
                "quarter": f["quarter"]
            }
            if "filter_min" in f:
                filter_item["filterMin"] = f["filter_min"]
            if "filter_max" in f:
                filter_item["filterMax"] = f["filter_max"]
            if "is_no_filter" in f:
                filter_item["isNoFilter"] = f["is_no_filter"]
            if "sort_dir" in f:
                filter_item["sortDir"] = f["sort_dir"]
            req["financialFilterList"].append(filter_item)

    ret, data = quote_ctx.get_stock_filter(req)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_warrant(
    stock_owner: str = '',
    warrant_types: Optional[List[str]] = None,
    issuers: Optional[List[str]] = None,
    sort_field: str = 'CUR_PRICE',
    ascend: bool = True,
    begin: int = 0,
    num: int = 200,
    maturity_time_min: Optional[str] = None,
    maturity_time_max: Optional[str] = None,
    price_type: Optional[str] = None,
    status: Optional[str] = None,
    cur_price_min: Optional[float] = None,
    cur_price_max: Optional[float] = None,
    strike_price_min: Optional[float] = None,
    strike_price_max: Optional[float] = None,
    recovery_price_min: Optional[float] = None,
    recovery_price_max: Optional[float] = None,
    price_recovery_ratio_min: Optional[float] = None,
    price_recovery_ratio_max: Optional[float] = None,
    leverage_ratio_min: Optional[float] = None,
    leverage_ratio_max: Optional[float] = None,
    premium_min: Optional[float] = None,
    premium_max: Optional[float] = None,
    delta_min: Optional[float] = None,
    delta_max: Optional[float] = None,
    implied_min: Optional[float] = None,
    implied_max: Optional[float] = None,
) -> Dict[str, Any]:
    """Get warrant/CBBC (Callable Bull/Bear Contracts) list with filters

    Args:
        stock_owner: Underlying stock code, e.g. "HK.00700". Empty means all warrants.
        warrant_types: List of warrant types to filter. Options:
            - "CALL": Call warrant (认购证)
            - "PUT": Put warrant (认沽证)
            - "BULL": Bull contract (牛证)
            - "BEAR": Bear contract (熊证)
            - "INLINE": Inline warrant (界内证)
        issuers: List of issuer codes to filter, e.g. ["GS", "JP", "MS"].
            Common issuers: GS(Goldman Sachs), JP(JPMorgan), MS(Morgan Stanley),
            HS(HSBC), CS(Credit Suisse), CT(Citi), MB(Macquarie), DB(Deutsche Bank)
        sort_field: Field to sort by (default: "CUR_PRICE"). Options:
            CODE, CUR_PRICE, CHANGE_RATE, VOLUME, TURNOVER, PREMIUM,
            EFFECTIVE_LEVERAGE, DELTA, IMPLIED_VOLATILITY, STRIKE_PRICE,
            MATURITY_TIME, LEVERAGE, RECOVERY_PRICE, STREET_RATE, AMPLITUDE
        ascend: Sort ascending (True) or descending (False). Default: True
        begin: Data start index for pagination (default: 0)
        num: Number of results to return, max 200 (default: 200)
        maturity_time_min: Maturity date range start, format "YYYY-MM-DD"
        maturity_time_max: Maturity date range end, format "YYYY-MM-DD"
        price_type: Price type filter. Options:
            - "WITH_IN": In the money (价内)
            - "OUTSIDE": Out of the money (价外)
        status: Warrant status filter. Options:
            - "NORMAL": Normal trading (正常)
            - "SUSPEND": Suspended (停牌)
            - "STOP_TRADE": Stop trade (停止交易)
            - "PENDING_LISTING": Pending listing (待上市)
        cur_price_min: Current price minimum filter
        cur_price_max: Current price maximum filter
        strike_price_min: Strike price minimum filter
        strike_price_max: Strike price maximum filter
        recovery_price_min: Recovery price minimum filter (CBBC only, 牛熊证)
        recovery_price_max: Recovery price maximum filter (CBBC only, 牛熊证)
        price_recovery_ratio_min: Distance from recovery price % minimum (CBBC only)
        price_recovery_ratio_max: Distance from recovery price % maximum (CBBC only)
        leverage_ratio_min: Leverage ratio minimum filter
        leverage_ratio_max: Leverage ratio maximum filter
        premium_min: Premium % minimum filter
        premium_max: Premium % maximum filter
        delta_min: Delta minimum filter (call/put warrants only)
        delta_max: Delta maximum filter (call/put warrants only)
        implied_min: Implied volatility minimum filter (call/put warrants only)
        implied_max: Implied volatility maximum filter (call/put warrants only)

    Returns:
        Dict containing warrant list with fields:
        - stock_code: Warrant code
        - stock_name: Warrant name
        - wrt_type: Warrant type (CALL/PUT/BULL/BEAR/INLINE)
        - issuer_code: Issuer code
        - maturity_time: Maturity date
        - list_time: Listing date
        - last_trade_time: Last trading date
        - recovery_price: Recovery price (CBBC only)
        - conversion_ratio: Conversion ratio
        - strike_price: Strike price
        - cur_price: Current price
        - price_change_val: Price change value
        - change_rate: Change rate %
        - volume: Trading volume
        - turnover: Turnover
        - premium: Premium %
        - leverage: Leverage ratio
        - effective_leverage: Effective leverage
        - delta: Delta (call/put only)
        - implied_volatility: Implied volatility (call/put only)
        - street_rate: Street rate %
        - break_even_point: Break even point
        - in_out_money: In/out of money status

    Note:
        - recovery_price_min/max and price_recovery_ratio_min/max only work for BULL/BEAR types
        - delta_min/max and implied_min/max only work for CALL/PUT types
        - Use stock_owner to find all warrants linked to a specific underlying stock
        - CBBC (牛熊证) includes BULL and BEAR types; warrants (窝轮) include CALL and PUT types
    """
    from futu.quote.quote_get_warrant import Request as WarrantRequest
    from futu.common.utils import WrtType, Issuer, SortField, PriceType, WarrantStatus

    req = WarrantRequest()
    req.begin = begin
    req.num = min(num, 200)
    req.ascend = ascend

    if stock_owner:
        req.stock_owner = stock_owner

    # Sort field
    r, v = SortField.to_number(sort_field)
    if r:
        req.sort_field = sort_field

    # Warrant types
    if warrant_types:
        req.type_list = warrant_types

    # Issuers
    if issuers:
        req.issuer_list = issuers

    # Date range
    if maturity_time_min:
        req.maturity_time_min = maturity_time_min
    if maturity_time_max:
        req.maturity_time_max = maturity_time_max

    # Price type
    if price_type:
        req.price_type = price_type

    # Status
    if status:
        req.status = status

    # Numeric range filters
    if cur_price_min is not None:
        req.cur_price_min = cur_price_min
    if cur_price_max is not None:
        req.cur_price_max = cur_price_max
    if strike_price_min is not None:
        req.strike_price_min = strike_price_min
    if strike_price_max is not None:
        req.strike_price_max = strike_price_max
    if recovery_price_min is not None:
        req.recovery_price_min = recovery_price_min
    if recovery_price_max is not None:
        req.recovery_price_max = recovery_price_max
    if price_recovery_ratio_min is not None:
        req.price_recovery_ratio_min = price_recovery_ratio_min
    if price_recovery_ratio_max is not None:
        req.price_recovery_ratio_max = price_recovery_ratio_max
    if leverage_ratio_min is not None:
        req.leverage_ratio_min = leverage_ratio_min
    if leverage_ratio_max is not None:
        req.leverage_ratio_max = leverage_ratio_max
    if premium_min is not None:
        req.premium_min = premium_min
    if premium_max is not None:
        req.premium_max = premium_max
    if delta_min is not None:
        req.delta_min = delta_min
    if delta_max is not None:
        req.delta_max = delta_max
    if implied_min is not None:
        req.implied_min = implied_min
    if implied_max is not None:
        req.implied_max = implied_max

    ret, data = quote_ctx.get_warrant(stock_owner=stock_owner, req=req)
    return dataframe_to_records(data, 'warrant_list') if ret == RET_OK else {'error': data}


@mcp.tool()
async def get_current_time() -> Dict[str, Any]:
    """Get current time information
    
    Returns:
        Dict containing time information including:
        - timestamp: Unix timestamp in seconds
        - datetime: Formatted datetime string (YYYY-MM-DD HH:mm:ss)
        - date: Date string (YYYY-MM-DD)
        - time: Time string (HH:mm:ss)
        - timezone: Local timezone name
    """
    now = datetime.now()
    return {
        'timestamp': int(now.timestamp()),
        'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'timezone': datetime.now().astimezone().tzname()
    }

def main():
    """Main entry point for the futu-mcp-server command."""
    # Parse command line arguments first
    parser = argparse.ArgumentParser(
        description="Futu Stock MCP Server - A Model Context Protocol server for accessing Futu OpenAPI functionality",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  futu-mcp-server                                    # Start the MCP server with default settings
  futu-mcp-server --host 192.168.1.100 --port 11111 # Connect to remote OpenD
  futu-mcp-server --help                            # Show this help message

Arguments:
  --host                            # Futu OpenD host (default: 127.0.0.1)
  --port                            # Futu OpenD port (default: 11111)

Environment Variables:
  FUTU_ENABLE_TRADING               # Enable active trading tools (default: 0)
  FUTU_ENABLE_POSITIONS             # Enable position tools (default: 1)
  FUTU_TRADE_ENV                    # Trading environment: SIMULATE or REAL (default: SIMULATE)
  FUTU_SECURITY_FIRM                # Security firm: FUTUSECURITIES or FUTUINC (default: FUTUSECURITIES)
  FUTU_TRD_MARKET                   # Trading market: HK or US (default: HK)
  FUTU_DEBUG_MODE                   # Enable debug logging (default: 0)
        """
    )
    
    parser.add_argument(
        '--host',
        default=os.getenv('FUTU_HOST', '127.0.0.1'),
        help='Futu OpenD host address (default: env FUTU_HOST or 127.0.0.1)'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=int(os.getenv('FUTU_PORT', '11111')),
        help='Futu OpenD port number (default: env FUTU_PORT or 11111)'
    )

    parser.add_argument(
        '--version', 
        action='version', 
        version='futu-stock-mcp-server 1.0.6'
    )
    
    args = parser.parse_args()
    
    try:
        # CRITICAL: Set MCP mode BEFORE any logging to ensure clean stdout
        os.environ['MCP_MODE'] = '1'

        # Ensure no color output or ANSI escape sequences in MCP mode
        os.environ['NO_COLOR'] = '1'
        os.environ['TERM'] = 'dumb'
        os.environ['FORCE_COLOR'] = '0'
        os.environ['COLORTERM'] = ''
        os.environ['ANSI_COLORS_DISABLED'] = '1'
        os.environ['PYTHONUNBUFFERED'] = '1'
        os.environ['PYTHONIOENCODING'] = 'utf-8'

        # Disable Python buffering to ensure clean MCP JSON communication

        # Clean up stale processes and acquire lock
        cleanup_stale_processes()

        lock_fd = acquire_lock()
        if lock_fd is None:
            # Use file logging only - no stderr output in MCP mode
            logger.error("Failed to acquire lock. Another instance may be running.")
            sys.exit(1)
            
        # Set up signal handlers
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
            
        # Initialize Futu connection with file logging only
        logger.info("Initializing Futu connection for MCP server...")
        if init_futu_connection(args.host, args.port):
            logger.info("Successfully initialized Futu connection")
            logger.info("Starting MCP server in stdio mode - stdout reserved for JSON communication")

            try:
                # Run MCP server - stdout will be used for JSON communication only
                logger.info("About to call mcp.run() without transport parameter")
                mcp.run()
                logger.info("mcp.run() completed successfully")
            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, shutting down gracefully...")
                cleanup_all()
                os._exit(0)
            except Exception as e:
                logger.error(f"Error running MCP server: {str(e)}")
                cleanup_all()
                os._exit(1)
        else:
            logger.error("Failed to initialize Futu connection. MCP server will not start.")
            os._exit(1)

    except Exception as e:
        # In MCP mode, we should avoid printing to stdout
        # Log to file only
        logger.error(f"Error starting MCP server: {str(e)}")
        sys.exit(1)
    finally:
        cleanup_all() 

if __name__ == "__main__":
    main()
