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

    def __getattr__(self, name):
        return getattr(self.original, name)

# Apply stdout protection in MCP mode VERY EARLY, before any imports
if os.getenv('MCP_MODE') == '1':
    sys.stdout = StdoutProtector(sys.stdout)

# 5. Redirect stderr to null in MCP mode to prevent any accidental output
_stderr_redirected = False
_stderr_backup = None
if os.getenv('MCP_MODE') == '1':
    # Save original stderr for emergency use
    _stderr_backup = sys.stderr
    # Redirect stderr to devnull
    devnull = open(os.devnull, 'w')
    sys.stderr = devnull
    _stderr_redirected = True

# Now we can safely import other modules
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Dict, Any, List, Optional
try:
    from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket, SecurityFirm, RET_OK
except ImportError as e:
    # In MCP mode, we should avoid printing to stdout/stderr
    # Log to file only
    logger.error(f"Failed to import futu: {e}")
    sys.exit(1)
import json
import asyncio
from loguru import logger
from dotenv import load_dotenv
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
    global quote_ctx
    
    try:
        # Check if OpenD is running by attempting to get global state
        try:
            temp_ctx = OpenQuoteContext(
                host=os.getenv('FUTU_HOST', '127.0.0.1'),
                port=int(os.getenv('FUTU_PORT', '11111'))
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
            host=os.getenv('FUTU_HOST', '127.0.0.1'),
            port=int(os.getenv('FUTU_PORT', '11111'))
        )
        logger.info("Successfully connected to Futu Quote API")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize quote connection: {str(e)}")
        cleanup_connections()
        return False

def init_trade_connection():
    """Initialize trade connection only"""
    global trade_ctx, _is_trade_initialized
    
    if _is_trade_initialized and trade_ctx:
        return True
        
    try:
        # Initialize trade context with proper market access
        trade_env = os.getenv('FUTU_TRADE_ENV', 'SIMULATE')
        security_firm = getattr(SecurityFirm, os.getenv('FUTU_SECURITY_FIRM', 'FUTUSECURITIES'))
        
        # 只支持港股和美股
        market_map = {
            'HK': 1,  # TrdMarket.HK
            'US': 2   # TrdMarket.US
        }
        trd_market = market_map.get(os.getenv('FUTU_TRD_MARKET', 'HK'), 1)
        
        # 创建交易上下文
        trade_ctx = OpenSecTradeContext(
            filter_trdmarket=trd_market,
            host=os.getenv('FUTU_HOST', '127.0.0.1'),
            port=int(os.getenv('FUTU_PORT', '11111')),
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
            cleanup_connections()
            return False
            
        if data is None or len(data) == 0:
            logger.warning("No trading accounts available")
            cleanup_connections()
            return False
            
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
                trd_market = acc.get('trd_market', 'Unknown')
            else:
                acc_id = getattr(acc, 'acc_id', 'Unknown')
                acc_type = getattr(acc, 'acc_type', 'Unknown')
                acc_state = getattr(acc, 'acc_state', 'Unknown')
                trd_env = getattr(acc, 'trd_env', 'Unknown')
                trd_market = getattr(acc, 'trd_market', 'Unknown')
                
            logger.info(f"Account: {acc_id}, Type: {acc_type}, State: {acc_state}, Environment: {trd_env}, Market: {trd_market}")
        
        _is_trade_initialized = True
        logger.info(f"Successfully initialized trade connection (Trade Environment: {trade_env}, Security Firm: {security_firm}, Market: {trd_market})")
        return True
            
    except Exception as e:
        logger.error(f"Failed to initialize trade connection: {str(e)}")
        cleanup_connections()
        _is_trade_initialized = False
        return False

def init_futu_connection() -> bool:
    """
    Initialize connection to Futu OpenD.
    Returns True if successful, False otherwise.
    """
    global quote_ctx, trade_ctx, _is_trade_initialized

    try:
        # Get connection parameters from environment
        host = os.getenv('FUTU_HOST', '127.0.0.1')
        port = int(os.getenv('FUTU_PORT', '11111'))

        # Log to file only
        logger.info(f"Initializing Futu connection to {host}:{port}")

        # Temporarily redirect stderr to capture any futu library output
        original_stderr = None
        futu_log_file = None
        futu_log_fd = None

        if os.getenv('MCP_MODE') == '1' and _stderr_redirected:
            try:
                # Create a special log file for futu connection logs
                home_dir = os.path.expanduser("~")
                log_dir = os.path.join(home_dir, "logs")
                os.makedirs(log_dir, exist_ok=True)
                futu_log_file = os.path.join(log_dir, "futu_connection.log")

                # Save current stderr (should be devnull)
                original_stderr = sys.stderr

                # Redirect stderr to futu log file temporarily
                futu_log_fd = open(futu_log_file, 'a')
                sys.stderr = futu_log_fd
            except Exception as e:
                logger.debug(f"Could not redirect stderr for futu connection: {e}")

        try:
            # Initialize quote context
            quote_ctx = OpenQuoteContext(host=host, port=port)

            # Initialize trade context if needed
            if os.getenv('FUTU_ENABLE_TRADING', '0') == '1':
                # Get trading parameters
                trade_env = os.getenv('FUTU_TRADE_ENV', 'SIMULATE')
                security_firm = os.getenv('FUTU_SECURITY_FIRM', 'FUTUSECURITIES')
                trd_market = os.getenv('FUTU_TRD_MARKET', 'HK')

                # Map environment strings to Futu enums
                trade_env_enum = {
                    'REAL': TrdMarket.REAL,
                    'SIMULATE': TrdMarket.SIMULATE
                }.get(trade_env, TrdMarket.SIMULATE)

                security_firm_enum = {
                    'FUTUSECURITIES': SecurityFirm.FUTUSECURITIES,
                    'FUTUINC': SecurityFirm.FUTUINC
                }.get(security_firm, SecurityFirm.FUTUSECURITIES)

                trd_market_enum = {
                    'HK': TrdMarket.HK,
                    'US': TrdMarket.US,
                    'CN': TrdMarket.CN,
                    'HKCC': TrdMarket.HKCC,
                    'AU': TrdMarket.AU
                }.get(trd_market, TrdMarket.HK)

                # Initialize trade context
                trade_ctx = OpenSecTradeContext(
                    host=host,
                    port=port,
                    security_firm=security_firm_enum
                )
                _is_trade_initialized = True
                logger.info("Trade context initialized successfully")

            logger.info("Futu connection initialized successfully")
            return True

        finally:
            # Restore stderr redirection
            if futu_log_fd:
                try:
                    futu_log_fd.close()
                except:
                    pass
                # Restore original stderr (should be devnull)
                if original_stderr:
                    sys.stderr = original_stderr

    except Exception as e:
        error_msg = f"Failed to initialize Futu connection: {str(e)}"
        logger.error(error_msg)
        # Make sure we restore stderr even in case of errors
        if _stderr_redirected and _stderr_backup:
            sys.stderr = _stderr_backup
        return False

@asynccontextmanager
async def lifespan(server: Server):
    # Startup - only initialize quote connection
    if not init_quote_connection():
        logger.error("Failed to initialize quote connection")
        raise Exception("Quote connection failed")
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
    ret, data = quote_ctx.get_ticker(symbol)
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
    for symbol in symbols:
        for sub_type in sub_types:
            ret, data = quote_ctx.subscribe(symbol, sub_type)
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
    for symbol in symbols:
        for sub_type in sub_types:
            ret, data = quote_ctx.unsubscribe(symbol, sub_type)
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
    ret, data = quote_ctx.get_option_chain(symbol, start, end)
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
    """
    ret, data = quote_ctx.get_option_condor(symbol, expiry, strike_price)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_option_butterfly(symbol: str, expiry: str, strike_price: float) -> Dict[str, Any]:
    """Get option butterfly strategy data
    
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
    """
    ret, data = quote_ctx.get_option_butterfly(symbol, expiry, strike_price)
    return data.to_dict() if ret == RET_OK else {'error': data}

# Account Query Tools
@mcp.tool()
async def get_account_list(ctx: Context[ServerSession, None] = None) -> Dict[str, Any]:
    """Get account list"""
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
    """Get account funds information"""
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
    """Get account positions"""
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.position_list_query()
    return handle_return_data(ret, data)

@mcp.tool()
async def get_max_power() -> Dict[str, Any]:
    """Get maximum trading power for the account"""
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.get_max_power()
    return handle_return_data(ret, data)

@mcp.tool()
async def get_margin_ratio(symbol: str) -> Dict[str, Any]:
    """Get margin ratio for a security"""
    if not init_trade_connection():
        return {'error': 'Failed to initialize trade connection'}
    ret, data = trade_ctx.get_margin_ratio(symbol)
    return handle_return_data(ret, data)

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
async def get_security_info(market: str, code: str) -> Dict[str, Any]:
    """Get security information
    
    Args:
        market: Market code, options:
            - "HK": Hong Kong market
            - "US": US market
            - "SH": Shanghai market
            - "SZ": Shenzhen market
        code: Stock code without market prefix, e.g. "00700" for "HK.00700"
    
    Returns:
        Dict containing security information including:
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
    ret, data = quote_ctx.get_security_info(market, code)
    return data.to_dict() if ret == RET_OK else {'error': data}

@mcp.tool()
async def get_security_list(market: str) -> Dict[str, Any]:
    """Get security list
    
    Args:
        market: Market code, options:
            - "HK": Hong Kong market
            - "US": US market
            - "SH": Shanghai market
            - "SZ": Shenzhen market
            
    Returns:
        Dict containing list of securities:
        - security_list: List of securities, each containing:
            - code: Security code
            - name: Security name
            - lot_size: Lot size
            - stock_type: Security type
            - list_time: Listing date
            - stock_id: Security ID
            - delisting: Whether delisted
            - main_contract: Whether it's the main contract (futures)
            - last_trade_time: Last trade time (futures/options)
            
    Raises:
        - INVALID_PARAM: Invalid parameter
        - INVALID_MARKET: Invalid market code
        - GET_SECURITY_LIST_FAILED: Failed to get security list
        
    Note:
        - Returns all securities in the specified market
        - Includes stocks, ETFs, warrants, etc.
        - Updated daily
        - Useful for market analysis and monitoring
        - Consider caching results for better performance
    """
    ret, data = quote_ctx.get_security_list(market)
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
  futu-mcp-server                    # Start the MCP server
  futu-mcp-server --help            # Show this help message

Environment Variables:
  FUTU_HOST                         # Futu OpenD host (default: 127.0.0.1)
  FUTU_PORT                         # Futu OpenD port (default: 11111)
  FUTU_ENABLE_TRADING               # Enable trading features (default: 0)
  FUTU_TRADE_ENV                    # Trading environment: SIMULATE or REAL (default: SIMULATE)
  FUTU_SECURITY_FIRM                # Security firm: FUTUSECURITIES or FUTUINC (default: FUTUSECURITIES)
  FUTU_TRD_MARKET                   # Trading market: HK or US (default: HK)
  FUTU_DEBUG_MODE                   # Enable debug logging (default: 0)
        """
    )
    
    parser.add_argument(
        '--version', 
        action='version', 
        version='futu-stock-mcp-server 0.1.3'
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
        if init_futu_connection():
            logger.info("Successfully initialized Futu connection")
            logger.info("Starting MCP server in stdio mode - stdout reserved for JSON communication")

            try:
                # Run MCP server - stdout will be used for JSON communication only
                mcp.run(transport='stdio')
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

