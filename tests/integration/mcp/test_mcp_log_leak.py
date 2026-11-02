"""SDK 直接寫 root logger 的紀錄不可帶出 prompt(CHG-20261102-01 終審修正 A)。

`mcp.shared.session.BaseSession._receive_loop` 對驗證失敗的 request 呼叫模組層級的
`logging.warning(f"Failed to validate request: {e}")`(與 `logging.debug(...)`):紀錄寫在 **root**
logger,pydantic 的 `input_value` 含 request 的片段(例如 `tools/call` 的 `arguments` 為字串時整段
prompt)。`spillway.api.mcp.sdk_logging.install_sdk_root_log_filter()` 在 root logger 物件上掛 filter,
丟掉 `name == "root"` 且來源檔在 `mcp` 套件內的紀錄;`create_app` 與 `bridge.run` 都安裝。

L1:filter 本身;行程內 app 收到 `arguments` 為字串的 `tools/call` → 仍回 JSON-RPC -32602,caplog 與
stderr 都沒有 prompt;`bridge.run` 安裝 filter。子行程 serve 與子行程 bridge 的案例分別在
`tests/integration/test_serve.py`、`tests/integration/test_mcp_stdio.py`。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import mcp
import pytest

from spillway.api.mcp import bridge
from spillway.api.mcp.sdk_logging import SdkRootRecordFilter, install_sdk_root_log_filter
from tests.integration.mcp.conftest import INIT_BODY, MCP_ACCEPT

MARKER = "root-log-leak-prompt-5c2e"
MCP_DIR = Path(mcp.__file__).resolve().parent


def _record(name: str, pathname: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, pathname, 1, f"Failed to validate request: {MARKER}",
                             None, None)


def _filters() -> list[logging.Filter]:
    return [f for f in logging.getLogger().filters if isinstance(f, SdkRootRecordFilter)]


def test_filter_drops_only_root_records_from_the_mcp_package():
    f = SdkRootRecordFilter()
    assert f.filter(_record("root", str(MCP_DIR / "shared" / "session.py"))) is False
    assert f.filter(_record("root", str(MCP_DIR / "__init__.py"))) is False
    assert f.filter(_record("root", __file__)) is True                       # 其他來源的 root 紀錄照常
    assert f.filter(_record("root", str(MCP_DIR) + "-other/x.py")) is True   # 前綴相同的別的目錄
    assert f.filter(_record("mcp.server.lowlevel", str(MCP_DIR / "server" / "x.py"))) is True   # 具名 logger 不在此管


def test_install_is_idempotent_and_logging_warning_is_dropped(caplog):
    install_sdk_root_log_filter()
    install_sdk_root_log_filter()
    assert len(_filters()) == 1
    caplog.set_level(logging.DEBUG)
    record = _record("root", str(MCP_DIR / "shared" / "session.py"))
    logging.getLogger().handle(record)
    assert MARKER not in caplog.text


def test_bridge_run_installs_filter(monkeypatch):
    root = logging.getLogger()
    for f in _filters():
        root.removeFilter(f)

    def fake_bridge(url, headers):   # noqa: ANN001 — 不連線
        async def _done() -> int:
            return 0
        return _done()

    monkeypatch.setattr(bridge, "_bridge", fake_bridge)
    try:
        assert bridge.run("g", 1, {"SPILLWAY_TOKEN": "x" * 43}) == 0
        assert len(_filters()) == 1
    finally:
        for name in ("mcp", "httpx", "httpcore"):   # `_quiet_loggers` 的副作用還原
            lg = logging.getLogger(name)
            lg.propagate = True
            lg.handlers[:] = []
            lg.setLevel(logging.NOTSET)


def _sse_json(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


@pytest.mark.anyio
async def test_invalid_tools_call_does_not_log_prompt(asgi_app, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    c, tok = asgi_app.client, asgi_app.token
    h = {"Accept": MCP_ACCEPT, "Content-Type": "application/json", "Authorization": f"Bearer {tok}"}
    r = await c.post("/mcp/all", json=INIT_BODY, headers=h)
    assert r.status_code == 200, r.text
    h = {**h, "Mcp-Session-Id": r.headers["mcp-session-id"], "Mcp-Protocol-Version": "2025-06-18"}
    r = await c.post("/mcp/all", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    assert r.status_code == 202, r.text
    bad = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
           "params": {"name": "ask", "arguments": f"route=review prompt={MARKER}"}}
    r = await c.post("/mcp/all", json=bad, headers=h)
    assert r.status_code == 200, r.text
    msg = _sse_json(r.text)
    assert msg["id"] == 5 and msg["error"]["code"] == -32602
    await asyncio.sleep(0)
    assert MARKER not in caplog.text
    assert "Failed to validate request" not in caplog.text
    captured = capsys.readouterr()
    assert MARKER not in captured.err and MARKER not in captured.out
