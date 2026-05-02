import asyncio
import atexit
import importlib
import sys
import types


class _IdentityMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        return lambda func: func

    def prompt(self):
        return lambda func: func

    def run(self):
        pass


class _SubscriptableContext:
    def __class_getitem__(cls, item):
        return cls


class _FakeEnum:
    HK = "HK"
    FUTUSECURITIES = "FUTUSECURITIES"


class _FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient=None):
        if orient == "records":
            return self.rows
        result = {}
        for row in self.rows:
            for key, value in row.items():
                result.setdefault(key, []).append(value)
        return result


class _HistoryQuoteContext:
    def __init__(self):
        self.calls = []

    def request_history_kline(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("page_req_key") is None:
            return 0, _FakeFrame([{"time_key": "2024-01-01", "close": 1}]), b"next"
        return 0, _FakeFrame([{"time_key": "2024-01-02", "close": 2}]), None


class _CurQuoteContext:
    def __init__(self):
        self.calls = []

    def get_cur_kline(self, **kwargs):
        self.calls.append(kwargs)
        return 0, _FakeFrame([{"time_key": "2024-01-01", "close": 1}])


def _install_test_modules():
    futu = types.ModuleType("futu")
    futu.OpenQuoteContext = object
    futu.OpenSecTradeContext = object
    futu.TrdMarket = _FakeEnum
    futu.SecurityFirm = _FakeEnum
    futu.RET_OK = 0
    futu.TrdEnv = _FakeEnum
    futu.TrdSide = _FakeEnum
    futu.OrderType = _FakeEnum
    futu.ModifyOrderOp = _FakeEnum
    futu.Session = _FakeEnum
    futu.TrailType = _FakeEnum
    futu.TimeInForce = _FakeEnum
    futu.OrderStatus = _FakeEnum
    futu.CashFlowDirection = _FakeEnum
    sys.modules["futu"] = futu

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _IdentityMCP
    fastmcp.Context = _SubscriptableContext
    sys.modules["mcp.server.fastmcp"] = fastmcp

    mcp_types = types.ModuleType("mcp.types")
    mcp_types.TextContent = object
    mcp_types.PromptMessage = object
    sys.modules["mcp.types"] = mcp_types

    mcp_server = types.ModuleType("mcp.server")
    mcp_server.Server = object
    sys.modules["mcp.server"] = mcp_server

    mcp_session = types.ModuleType("mcp.server.session")
    mcp_session.ServerSession = object
    sys.modules["mcp.server.session"] = mcp_session


def _load_server():
    _install_test_modules()
    sys.modules.pop("futu_stock_mcp_server.server", None)
    server = importlib.import_module("futu_stock_mcp_server.server")
    try:
        atexit.unregister(server.cleanup_all)
    except ValueError:
        pass
    return server


def test_history_count_doc_describes_page_size():
    server = _load_server()

    doc = server.get_history_kline.__doc__

    assert "per API page" in doc
    assert "returns all available K-lines" in doc


def test_history_count_must_be_valid_page_size():
    server = _load_server()
    server.quote_ctx = _HistoryQuoteContext()

    result = asyncio.run(
        server.get_history_kline("HK.00700", "K_DAY", "2024-01-01", "2024-01-02", count=0)
    )

    assert result == {"error": "count must be between 1 and 1000"}
    assert server.quote_ctx.calls == []


def test_cur_count_must_be_valid_api_limit():
    server = _load_server()
    server.quote_ctx = _CurQuoteContext()

    result = asyncio.run(server.get_cur_kline("HK.00700", "K_DAY", count=1001))

    assert result == {"error": "count must be between 1 and 1000"}
    assert server.quote_ctx.calls == []


def test_history_count_is_page_size_and_fetches_all_pages():
    server = _load_server()
    server.quote_ctx = _HistoryQuoteContext()

    result = asyncio.run(
        server.get_history_kline("HK.00700", "K_DAY", "2024-01-01", "2024-01-02", count=2)
    )

    assert result == {"time_key": ["2024-01-01", "2024-01-02"], "close": [1, 2]}
    assert [call["max_count"] for call in server.quote_ctx.calls] == [2, 2]
    assert server.quote_ctx.calls[1]["page_req_key"] == b"next"
