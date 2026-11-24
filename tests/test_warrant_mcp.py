import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


WARRANT_ROWS = [
    {
        "stock": "HK.61234",
        "stock_owner": "HK.00700",
        "type": "BULL",
        "cur_price": 0.125,
        "recovery_price": 350.0,
        "volume": 12_000,
        "maturity_time": "2026-11-20",
        "maturity_timestamp": 1795104000.0,
    },
    {
        "stock": "HK.62345",
        "stock_owner": "HK.00700",
        "type": "BEAR",
        "cur_price": 0.247,
        "recovery_price": 470.0,
        "volume": 16_000,
        "maturity_time": "2026-11-20",
        "maturity_timestamp": 1795104000.0,
    },
]


def run_mock_server(case):
    from datetime import date

    import numpy as np
    import pandas as pd
    from futu import RET_ERROR, RET_OK
    from futu_stock_mcp_server import server

    def get_warrant(stock_owner, req):
        assert stock_owner == req.stock_owner == "HK.00700"
        assert req.type_list == ["BULL", "BEAR"]
        assert req.begin == 2 and req.num == 2
        if case == "error":
            return RET_ERROR, "模拟 OpenD 查询失败"
        if case == "empty":
            return RET_OK, (pd.DataFrame(columns=WARRANT_ROWS[0]), True, 2)
        if case == "special":
            row = {
                "list_time": date(2026, 9, 1),
                "maturity_time": pd.Timestamp("2026-11-20T13:30:15.123456789+08:00"),
                "last_trade_time": pd.NaT,
                "maturity_timestamp": np.datetime64("2026-11-20T05:30:15"),
                "last_trade_timestamp": np.datetime64("NaT"),
                "volume": np.int64(2**53 + 1),
                "cur_price": np.float32(0.125),
                "delta": float("nan"),
                "effective_leverage": np.float32("inf"),
                "implied_volatility": pd.NA,
                "is_suspended": np.bool_(False),
            }
            # 保留 object 列内的原始标量类型，避免 pandas 提前自动转换。
            rows = pd.DataFrame([row], dtype=object)
            return RET_OK, (rows, np.bool_(True), np.int64(3))
        return RET_OK, (pd.DataFrame(WARRANT_ROWS), False, 7)

    # 使用独立临时 HOME，运行 MCP stdio，跳过真实 OpenD 连接和服务进程登记。
    server.PID_FILE = str(Path.home() / ".futu_mcp.pid")
    server.LOCK_FILE = str(Path.home() / ".futu_mcp.lock")
    server.quote_ctx = SimpleNamespace(get_warrant=get_warrant, close=lambda: None)
    server.mcp.run()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("normal", {"warrant_list": WARRANT_ROWS, "last_page": False, "all_count": 7}),
        ("empty", {"warrant_list": [], "last_page": True, "all_count": 2}),
        (
            "special",
            {
                "warrant_list": [{
                    "list_time": "2026-09-01",
                    "maturity_time": "2026-11-20T13:30:15.123456789+08:00",
                    "last_trade_time": None,
                    "maturity_timestamp": "2026-11-20T05:30:15",
                    "last_trade_timestamp": None,
                    "volume": 2**53 + 1,
                    "cur_price": 0.125,
                    "delta": None,
                    "effective_leverage": None,
                    "implied_volatility": None,
                    "is_suspended": False,
                }],
                "last_page": True,
                "all_count": 3,
            },
        ),
        ("error", {"error": "模拟 OpenD 查询失败"}),
    ],
)
def test_get_warrant_over_mcp(tmp_path, case, expected):
    package = importlib.util.find_spec("futu_stock_mcp_server")
    package_parent = Path(package.origin).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), case],
        env={"HOME": str(tmp_path), "PYTHONPATH": str(package_parent)},
    )

    async def call_tool():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                return await client.call_tool(
                    "get_warrant",
                    {
                        "stock_owner": "HK.00700",
                        "warrant_types": ["BULL", "BEAR"],
                        "begin": 2,
                        "num": 2,
                    },
                )

    result = asyncio.run(asyncio.wait_for(call_tool(), timeout=20))
    assert not result.isError, result.content
    expected_json = json.dumps(expected, sort_keys=True, allow_nan=False)
    text_payload = json.loads(result.content[0].text)
    assert json.dumps(text_payload, sort_keys=True, allow_nan=False) == expected_json
    if "structuredContent" in type(result).model_fields:
        structured_payload = result.structuredContent["result"]
        assert json.dumps(structured_payload, sort_keys=True, allow_nan=False) == expected_json


if __name__ == "__main__":
    run_mock_server(sys.argv[1])
