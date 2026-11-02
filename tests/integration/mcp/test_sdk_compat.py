"""`api/mcp/sdk_compat.py` 的絆線測試(CHG-20261102-01 決策 14;AM-5)。

每個依賴的 SDK 私有屬性各一條斷言;SDK 升版改動內部結構時在這裡失敗,而不是在停機或通知時靜默失效。
另以 AST 檢查:`src/spillway` 下只有 `sdk_compat.py` 存取這些私有屬性。
"""
from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path

import pytest
from mcp.server.streamable_http import GET_STREAM_KEY, StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

import spillway
from spillway.api.mcp import sdk_compat
from spillway.api.mcp.server import SpillwayServer

pytestmark = pytest.mark.anyio

PRIVATE = {"_server_instances", "_request_streams"}


def test_sdk_version_in_supported_range():
    major, minor, *_ = (int(x) for x in importlib.metadata.version("mcp").split(".")[:2])
    assert (major, minor) >= (1, 30) and major < 2


def test_manager_has_server_instances_dict():
    mgr = StreamableHTTPSessionManager(app=SpillwayServer(None), security_settings=None)
    assert isinstance(mgr._server_instances, dict)


def test_transport_has_request_streams_and_get_key():
    t = StreamableHTTPServerTransport(mcp_session_id="x", is_json_response_enabled=False,
                                      event_store=None)
    assert isinstance(t._request_streams, dict)
    assert isinstance(GET_STREAM_KEY, str) and GET_STREAM_KEY


async def test_get_stream_open_live(live_gateway):
    gw = live_gateway
    assert gw.stream_open("missing") is False
    async with gw.mcp_session("all") as c:
        await gw.wait_stream_open(c.session_id)
        assert sdk_compat.get_stream_open(gw.app, c.session_id) is True


def test_get_stream_open_without_manager():
    class _State:
        mcp_manager = None

    class _App:
        state = _State()

    assert sdk_compat.get_stream_open(_App(), "x") is False


def test_private_access_only_in_sdk_compat():
    src = Path(spillway.__file__).parent
    offenders = []
    for path in src.rglob("*.py"):
        if path.name == "sdk_compat.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in PRIVATE:
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []
