"""`spillway mcp-stdio`(CHG-20261102-01 T4;決策 15,決策審議修訂 AM-1;AC-11)。

閘道一律以 `live_gateway`(背景 thread 的真實 uvicorn,127.0.0.1 隨機埠)啟動;bridge 一律以子行程
`sys.executable -m spillway mcp-stdio` 啟動(Global Constraints)。兩種用戶端:

- 官方 SDK 的 stdio 用戶端(`mcp.client.stdio.stdio_client`):initialize、`tools/list`、`ask`,以及
  被拒絕時 `initialize` 丟 `McpError` 且 `error.data.code` 為對應錯誤碼。
- 原生子行程(逐行寫 JSON-RPC 到 stdin、讀 stdout):斷言結束碼、stderr 與 stdout 的內容。

L1:成功路徑;token 未設定 / 空白 / 含 CR-LF → 2 且未連線;serve 未啟動 → 1;`SPILLWAY_DEPTH=1` →
`recursion_blocked`、結束碼 3;錯誤 token → `unauthorized`、結束碼 3;另含 404 `not_found`、503
`shutting_down`。
L2:`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 指向本機計數 listener,斷言 0 個連線;stderr 不含 token、prompt 與
回應文字;stdout 只有 JSON-RPC;不建立資料目錄;SIGINT → 130、無 traceback。

CHG-20261102-02 T2(決策 12,AM-13;允許清單 ③):以本機假 HTTP 端點回各種非 2xx——閘道 9.1.2 body 且狀態碼
與錯誤碼成對 → `data.code`、結束碼 3;SDK 的 503「Too many open sessions」、404「Session not found」、
整數錯誤碼的 JSON-RPC body、非 JSON、不成對、超過 64 KiB → `data` 只有 `http_status`、結束碼 1、不轉印
`message`;2xx 不讀 body(回應 hook 的單元測試)。L2:子行程 serve 設 `SPILLWAY_TEST_MCP_MAX_SESSIONS=1`,
第二個 mcp-stdio 回 `data.http_status` 503、結束碼 1。
"""
from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, McpError
from mcp.client.stdio import StdioServerParameters, stdio_client

from spillway.clock import SystemRng
from tests.helpers.live_gateway import LiveGateway, start_live_gateway
from tests.integration.mcp.conftest import seed_basic

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PROMPT = "stdio-bridge-secret-prompt-7d1f"
ANSWER = "fake upstream ok"   # tests/fakes 假上游的回應文字
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "spillway-stdio-test", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


# ---------------------------------------------------------------------------- fixtures 與工具

@pytest.fixture
def gateway(fake_key_env: str, fake_api: Any, clock: Any):
    from spillway import settings

    db_path = settings.db_path()
    gw = start_live_gateway(db_path, clock=clock, rng=SystemRng(seed=0))
    seed_basic(db_path, fake_api.base_url)
    try:
        yield gw
    finally:
        gw.stop()


class CountingListener:
    """本機 TCP listener,只計算收到的連線數(當作代理伺服器)。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.count += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


@pytest.fixture
def listener():
    lst = CountingListener()
    try:
        yield lst
    finally:
        lst.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(tmp_path: Path, token: str | None = None, depth: str | None = None,
         **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("SPILLWAY_TOKEN", "SPILLWAY_DEPTH") and not k.lower().endswith("_proxy")}
    env["PYTHONPATH"] = str(SRC)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["SPILLWAY_HOME"] = str(tmp_path / "bridge-home")   # 不應被建立
    if token is not None:
        env["SPILLWAY_TOKEN"] = token
    if depth is not None:
        env["SPILLWAY_DEPTH"] = depth
    env.update(extra)
    return env


def _args(group: str, port: int) -> list[str]:
    return ["-m", "spillway", "mcp-stdio", "--group", group, "--port", str(port)]


class Bridge:
    """原生子行程:逐行寫 JSON-RPC、讀 stdout 行(真實時間逾時)。"""

    def __init__(self, env: dict[str, str], group: str, port: int) -> None:
        self.proc = subprocess.Popen([sys.executable, *_args(group, port)], env=env, cwd=str(REPO_ROOT),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._buf = b""
        self.lines: list[bytes] = []

    def send(self, *objs: dict[str, Any]) -> None:
        """一次寫出(多個物件在同一次 write 內)。"""
        assert self.proc.stdin is not None
        self.proc.stdin.write("".join(json.dumps(o) + "\n" for o in objs).encode())
        self.proc.stdin.flush()

    def recv(self, timeout: float = 20.0) -> dict[str, Any]:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("bridge 沒有在時限內回應")
            ready, _, _ = select.select([fd], [], [], left)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError("bridge stdout 已關閉")
                self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        self.lines.append(line)
        return json.loads(line)

    def recv_id(self, rid: Any, timeout: float = 20.0) -> dict[str, Any]:
        while True:
            msg = self.recv(timeout)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg

    def finish(self, timeout: float = 15.0, close_stdin: bool = True) -> tuple[int, str, list[bytes]]:
        """(必要時)關閉 stdin、等待結束;回傳 (結束碼, stderr, 全部 stdout 行)。"""
        if close_stdin and self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise
        assert self.proc.stdout is not None and self.proc.stderr is not None
        rest = self._buf + self.proc.stdout.read()
        self.lines.extend(x for x in rest.split(b"\n") if x)
        self._buf = b""
        return rc, self.proc.stderr.read().decode("utf-8", "replace"), self.lines

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def bridges():
    started: list[Bridge] = []

    def _start(env: dict[str, str], group: str, port: int) -> Bridge:
        b = Bridge(env, group, port)
        started.append(b)
        return b

    yield _start
    for b in started:
        b.kill()


def _assert_clean_stderr(stderr: str, *secrets: str) -> None:
    """失敗路徑的 stderr:沒有 traceback、沒有 pydantic 的 input_value(可能含回應文字)、沒有秘密。"""
    assert "Traceback" not in stderr and "input_value" not in stderr
    for secret in secrets:
        assert secret not in stderr


def _assert_jsonrpc_only(lines: list[bytes]) -> None:
    for line in lines:
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0", line


# ---------------------------------------------------------------------------- L1:SDK stdio 用戶端

@pytest.mark.anyio
async def test_sdk_stdio_client_initialize_list_and_ask(gateway: LiveGateway, tmp_path: Path):
    errlog = tmp_path / "bridge-stderr.txt"
    params = StdioServerParameters(command=sys.executable, args=_args("coding", gateway.port),
                                   env=_env(tmp_path, token=gateway.token), cwd=str(REPO_ROOT))
    with errlog.open("w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "spillway"
                assert init.capabilities.tools is not None and init.capabilities.tools.listChanged is True
                tools = (await session.list_tools()).tools
                assert [t.name for t in tools] == ["ask", "list_routes", "quota_status"]
                ask = next(t for t in tools if t.name == "ask")
                assert ask.inputSchema["properties"]["route"]["enum"] == ["review", "review-b"]
                result = await session.call_tool("ask", {"route": "review", "prompt": PROMPT})
                assert result.isError is False, result.content
                assert result.structuredContent["text"] == ANSWER
                assert result.structuredContent["served_by"] in {"api-a", "api-ab"}
    stderr = errlog.read_text()
    assert gateway.token not in stderr and PROMPT not in stderr and ANSWER not in stderr
    assert not (tmp_path / "bridge-home").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("case,code", [("depth", "recursion_blocked"), ("bad_token", "unauthorized")])
async def test_sdk_stdio_client_initialize_rejected(gateway: LiveGateway, tmp_path: Path, case, code):
    env = (_env(tmp_path, token=gateway.token, depth="1") if case == "depth"
           else _env(tmp_path, token="spw_not-the-real-token"))
    errlog = tmp_path / "bridge-stderr.txt"
    params = StdioServerParameters(command=sys.executable, args=_args("coding", gateway.port), env=env,
                                   cwd=str(REPO_ROOT))
    with errlog.open("w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as session:
                with pytest.raises(McpError) as exc_info:
                    await session.initialize()
    error = exc_info.value.error
    assert error.code == -32000
    assert error.data == {"code": code, "http_status": 403 if code == "recursion_blocked" else 401}
    assert code in errlog.read_text()


# ---------------------------------------------------------------------------- L1:結束碼

@pytest.mark.parametrize("case,status,code", [
    ("depth", 403, "recursion_blocked"),
    ("bad_token", 401, "unauthorized"),
    ("unknown_group", 404, "not_found"),
    ("shutting_down", 503, "shutting_down"),
])
def test_rejected_by_gateway_exit_3(gateway: LiveGateway, tmp_path: Path, bridges, case, status, code):
    group, token, depth = "coding", gateway.token, None
    if case == "depth":
        depth = "1"
    elif case == "bad_token":
        token = "spw_not-the-real-token"
    elif case == "unknown_group":
        group = "no such/group"
    elif case == "shutting_down":
        gateway.call_in_loop(setattr, gateway.app.state, "shutting_down", True)
    b = bridges(_env(tmp_path, token=token, depth=depth), group, gateway.port)
    b.send(INIT)
    msg = b.recv_id(1)
    assert msg["error"] == {"code": -32000, "message": f"{code}: rejected by gateway (HTTP {status})",
                            "data": {"code": code, "http_status": status}}
    rc, stderr, lines = b.finish(close_stdin=False)   # 被拒絕後自行結束,不必等 stdin EOF
    assert rc == 3
    assert code in stderr
    _assert_clean_stderr(stderr, gateway.token)
    _assert_jsonrpc_only(lines)
    if case == "shutting_down":
        gateway.call_in_loop(setattr, gateway.app.state, "shutting_down", False)


@pytest.mark.parametrize("token", [None, "", "   ", "abc\r\nX-Evil: 1", "abc\ndef", "abc\rdef"])
def test_bad_token_exit_2_without_connecting(tmp_path: Path, listener: CountingListener, bridges, token):
    b = bridges(_env(tmp_path, token=token), "coding", listener.port)
    rc, stderr, lines = b.finish(timeout=15)
    assert rc == 2
    assert "SPILLWAY_TOKEN" in stderr
    _assert_clean_stderr(stderr)
    assert lines == []
    time.sleep(0.2)
    assert listener.count == 0


@pytest.mark.parametrize("depth", ["1\r\nX-Evil: 1", "1\n"])
def test_depth_with_crlf_exit_2_without_connecting(tmp_path: Path, listener: CountingListener, bridges,
                                                   depth):
    b = bridges(_env(tmp_path, token="spw_x", depth=depth), "coding", listener.port)
    rc, stderr, lines = b.finish(timeout=15)
    assert rc == 2
    assert "SPILLWAY_DEPTH" in stderr
    _assert_clean_stderr(stderr, "spw_x")
    assert lines == [] and listener.count == 0


def test_gateway_not_running_exit_1(tmp_path: Path, bridges):
    b = bridges(_env(tmp_path, token="spw_x"), "coding", _free_port())
    b.send(INIT)
    msg = b.recv_id(1)
    assert msg["error"]["code"] == -32000
    assert "data" not in msg["error"]
    rc, stderr, lines = b.finish(close_stdin=False)
    assert rc == 1
    _assert_clean_stderr(stderr, "spw_x")
    _assert_jsonrpc_only(lines)


# ---------------------------------------------------------------------------- L2

def test_proxy_env_ignored_stdout_jsonrpc_only_and_no_secrets(gateway: LiveGateway, tmp_path: Path,
                                                              listener: CountingListener, bridges):
    env = _env(tmp_path, token=gateway.token, HTTP_PROXY=listener.url, HTTPS_PROXY=listener.url,
               ALL_PROXY=listener.url, http_proxy=listener.url, all_proxy=listener.url, NO_PROXY="")
    b = bridges(env, "coding", gateway.port)
    b.send(INIT)
    init = b.recv_id(1)
    assert init["result"]["serverInfo"]["name"] == "spillway"
    b.send(INITIALIZED)
    b.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in b.recv_id(2)["result"]["tools"]] == ["ask", "list_routes", "quota_status"]
    b.send({"jsonrpc": "2.0", "id": "call-3", "method": "tools/call",
            "params": {"name": "ask", "arguments": {"route": "review", "prompt": PROMPT}}})
    called = b.recv_id("call-3")
    assert called["result"]["structuredContent"]["text"] == ANSWER
    # 解析失敗(method 不是字串):只在 stderr 記類型,不轉送、也不當成 pending
    b.send({"jsonrpc": "2.0", "id": 99, "method": 5})
    rc, stderr, lines = b.finish()
    assert rc == 0
    assert "stdin 訊息無法解析,已略過(ValidationError)" in stderr
    assert all(json.loads(x).get("id") != 99 for x in lines)
    assert listener.count == 0
    _assert_jsonrpc_only(lines)
    for secret in (gateway.token, PROMPT, ANSWER):
        assert secret not in stderr
    assert "Traceback" not in stderr
    assert not (tmp_path / "bridge-home").exists()


def test_invalid_tools_call_prompt_not_logged(gateway: LiveGateway, tmp_path: Path, bridges, caplog):
    """CHG-20261102-01 終審修正 A:`arguments` 為字串的 `tools/call` 經 bridge 轉送 → 閘道回 -32602;
    bridge 的 stderr 與閘道的日誌(SDK 寫 root logger 的驗證失敗紀錄)都沒有 prompt。"""
    import logging

    caplog.set_level(logging.DEBUG)
    b = bridges(_env(tmp_path, token=gateway.token), "coding", gateway.port)
    _handshake(b)
    b.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "ask", "arguments": f"route=review prompt={PROMPT}"}})
    msg = b.recv_id(4)
    assert msg["error"]["code"] == -32602
    rc, stderr, lines = b.finish()
    assert rc == 0
    _assert_clean_stderr(stderr, gateway.token, PROMPT)
    _assert_jsonrpc_only(lines)
    assert all(PROMPT.encode() not in x for x in lines)
    assert PROMPT not in caplog.text and "Failed to validate request" not in caplog.text


def test_sigint_exit_130_without_traceback(gateway: LiveGateway, tmp_path: Path, bridges):
    b = bridges(_env(tmp_path, token=gateway.token), "coding", gateway.port)
    b.send(INIT)
    assert "result" in b.recv_id(1)
    b.send(INITIALIZED)
    time.sleep(0.3)
    b.proc.send_signal(signal.SIGINT)
    rc, stderr, lines = b.finish(close_stdin=False)
    assert rc == 130
    assert "Traceback" not in stderr and "KeyboardInterrupt" not in stderr
    _assert_jsonrpc_only(lines)


# ---------------------------------------------------------------------------- 審議修正(T4 review)

def _handshake(b: Bridge) -> None:
    b.send(INIT)
    assert "result" in b.recv_id(1)
    b.send(INITIALIZED)


def test_rejected_notification_stops_bridge_and_errors_later_request(gateway: LiveGateway,
                                                                     tmp_path: Path, bridges):
    """通知 POST 被拒絕(SDK 吞掉例外、關閉 read stream)→ bridge 不等 stdin EOF,5 秒內結束碼 3;
    緊接著送出的 request 收到錯誤而不是沒有回應。"""
    b = bridges(_env(tmp_path, token=gateway.token), "coding", gateway.port)
    _handshake(b)
    time.sleep(0.3)
    gateway.call_in_loop(setattr, gateway.app.state, "shutting_down", True)
    try:
        t0 = time.monotonic()
        b.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
               {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        msg = b.recv_id(7, timeout=5)
        assert msg["error"]["data"] == {"code": "shutting_down", "http_status": 503}
        rc, stderr, lines = b.finish(timeout=5, close_stdin=False)
        assert time.monotonic() - t0 < 5
    finally:
        gateway.call_in_loop(setattr, gateway.app.state, "shutting_down", False)
    assert rc == 3
    assert "shutting_down" in stderr
    _assert_clean_stderr(stderr, gateway.token)
    _assert_jsonrpc_only(lines)


def test_stdin_eof_waits_for_inflight_request(gateway: LiveGateway, tmp_path: Path, bridges):
    b = bridges(_env(tmp_path, token=gateway.token), "coding", gateway.port)
    _handshake(b)
    b.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "ask", "arguments": {"route": "review", "prompt": PROMPT}}})
    assert b.proc.stdin is not None
    b.proc.stdin.close()   # 進行中的 request 仍要拿到回應
    msg = b.recv_id(5)
    assert msg["result"]["structuredContent"]["text"] == ANSWER
    rc, stderr, lines = b.finish()
    assert rc == 0
    _assert_jsonrpc_only(lines)


def test_stdin_eof_drain_timeout_writes_errors(gateway: LiveGateway, fake_api: Any, tmp_path: Path,
                                               bridges):
    from spillway.api.mcp.bridge import EOF_DRAIN_TIMEOUT

    fake_api.set_mode("*", "delay", seconds=EOF_DRAIN_TIMEOUT + 2)
    b = bridges(_env(tmp_path, token=gateway.token), "coding", gateway.port)
    _handshake(b)
    b.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "ask", "arguments": {"route": "review", "prompt": PROMPT}}})
    time.sleep(0.3)
    t0 = time.monotonic()
    assert b.proc.stdin is not None
    b.proc.stdin.close()
    msg = b.recv_id(6, timeout=EOF_DRAIN_TIMEOUT + 5)
    assert msg["error"] == {"code": -32000, "message": "stdin closed before the gateway responded"}
    rc, stderr, lines = b.finish()
    assert EOF_DRAIN_TIMEOUT - 0.5 <= time.monotonic() - t0 < EOF_DRAIN_TIMEOUT + 3
    assert rc == 1
    _assert_clean_stderr(stderr, gateway.token, PROMPT)
    _assert_jsonrpc_only(lines)


# ---------------------------------------------------------------------------- CHG-20261102-02 T2:錯誤對應(決策 12)

REMOTE_MESSAGE = "remote-message-must-not-be-relayed-5e2a"


class FakeHttpEndpoint:
    """本機假 HTTP 端點:每個 POST 都回設定好的 (狀態碼, Content-Type, body);其他方法回 405。"""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        endpoint = self
        self.status = 500
        self.content_type: str | None = "application/json"
        self.body = b""

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(endpoint.status)
                if endpoint.content_type is not None:
                    self.send_header("Content-Type", endpoint.content_type)
                self.send_header("Content-Length", str(len(endpoint.body)))
                self.end_headers()
                self.wfile.write(endpoint.body)

            def _not_allowed(self) -> None:
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_DELETE = _not_allowed

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def respond(self, status: int, body: Any, content_type: str | None = "application/json") -> None:
        if isinstance(body, str):
            raw = body.encode()
        else:
            raw = json.dumps(body).encode()
        self.status, self.content_type, self.body = status, content_type, raw

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_http():
    ep = FakeHttpEndpoint()
    try:
        yield ep
    finally:
        ep.close()


def _spec_body(code: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": REMOTE_MESSAGE}}


def _sdk_body(message: str, code: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "server-error", "error": {"code": code, "message": message}}


@pytest.mark.parametrize("status,code", [
    (401, "unauthorized"),
    (403, "recursion_blocked"),
    (403, "host_not_allowed"),
    (403, "origin_not_allowed"),
    (404, "not_found"),
    (503, "shutting_down"),
])
def test_paired_spec_body_exit_3(fake_http: FakeHttpEndpoint, tmp_path: Path, bridges, status, code):
    fake_http.respond(status, _spec_body(code), "application/json; charset=utf-8")
    b = bridges(_env(tmp_path, token="spw_x"), "coding", fake_http.port)
    b.send(INIT)
    msg = b.recv_id(1)
    assert msg["error"] == {"code": -32000, "message": f"{code}: rejected by gateway (HTTP {status})",
                            "data": {"code": code, "http_status": status}}
    rc, stderr, lines = b.finish(close_stdin=False)
    assert rc == 3
    assert code in stderr
    _assert_clean_stderr(stderr, "spw_x", REMOTE_MESSAGE)
    _assert_jsonrpc_only(lines)
    assert all(REMOTE_MESSAGE.encode() not in x for x in lines)


_BIG = {"error": {"code": "shutting_down", "message": "x" * (64 * 1024)}}


@pytest.mark.parametrize("case,status,body,content_type", [
    ("sdk_too_many_sessions", 503, _sdk_body(f"Too many open sessions {REMOTE_MESSAGE}", -32603),
     "application/json"),
    ("sdk_session_not_found", 404, _sdk_body(f"Session not found {REMOTE_MESSAGE}", -32600),
     "application/json"),
    ("integer_code", 401, _spec_body(-32000), "application/json"),
    ("not_json", 401, f"unauthorized {REMOTE_MESSAGE}", "application/json"),
    ("not_json_content_type", 401, _spec_body("unauthorized"), "text/plain"),
    ("no_content_type", 401, _spec_body("unauthorized"), None),
    ("unpaired", 503, _spec_body("unauthorized"), "application/json"),
    ("unpaired_403", 403, _spec_body("not_found"), "application/json"),
    ("over_64k", 503, _BIG, "application/json"),
    ("other_status", 500, _spec_body("internal_error"), "application/json"),
    ("not_a_dict", 404, ["not_found"], "application/json"),
    ("deep_nesting_503", 503, "[" * 30000, "application/json"),   # json.loads → RecursionError
    ("deep_nesting_401", 401, "[" * 30000, "application/json"),
])
def test_non_gateway_rejection_exit_1_with_http_status_only(fake_http: FakeHttpEndpoint, tmp_path: Path,
                                                             bridges, case, status, body, content_type):
    fake_http.respond(status, body, content_type)
    b = bridges(_env(tmp_path, token="spw_x"), "coding", fake_http.port)
    b.send(INIT)
    msg = b.recv_id(1)
    assert msg["error"] == {"code": -32000, "message": f"gateway error (HTTP {status})",
                            "data": {"http_status": status}}
    rc, stderr, lines = b.finish(close_stdin=False)
    assert rc == 1
    _assert_clean_stderr(stderr, "spw_x", REMOTE_MESSAGE)
    _assert_jsonrpc_only(lines)
    assert all(REMOTE_MESSAGE.encode() not in x for x in lines)


def test_gateway_code_pairs():
    from spillway.api.mcp.bridge import gateway_code

    ok = json.dumps(_spec_body("unauthorized")).encode()
    assert gateway_code(401, "application/json", ok) == "unauthorized"
    assert gateway_code(401, "Application/JSON; charset=utf-8", ok) == "unauthorized"
    assert gateway_code(401, "application/json", None) is None          # 超過上限
    assert gateway_code(401, "application/jsonx", ok) is None
    assert gateway_code(401, None, ok) is None
    assert gateway_code(503, "application/json", ok) is None            # 不成對
    assert gateway_code(500, "application/json", ok) is None
    assert gateway_code(401, "application/json", b"\xff\xfe") is None
    assert gateway_code(401, "application/json", b'{"error": "unauthorized"}') is None
    assert gateway_code(401, "application/json", b"[" * 30000) is None    # RecursionError 不外洩


def _tracking_stream(chunks: list[bytes]) -> Any:
    """httpx 的 async 串流:`read` 記錄讀了幾個 chunk。"""
    import httpx

    class _TrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.read = 0

        async def __aiter__(self):
            for chunk in chunks:
                self.read += 1
                yield chunk

    return _TrackingStream()


def _response(status: int, chunks: list[bytes], headers: dict[str, str] | None = None,
              method: str = "POST") -> tuple[Any, Any]:
    import httpx

    stream = _tracking_stream(chunks)
    req = httpx.Request(method, "http://127.0.0.1:1/mcp/coding")
    return httpx.Response(status, headers=headers or {}, stream=stream, request=req), stream


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 202, 204, 307])
async def test_hook_never_reads_2xx_or_redirect_body(status):
    from spillway.api.mcp.bridge import _inspect_response, _State

    state = _State()
    resp, stream = _response(status, [json.dumps(_spec_body("unauthorized")).encode()],
                             {"content-type": "application/json"})
    await _inspect_response(resp, state)
    assert stream.read == 0
    assert state.rejected_status is None


@pytest.mark.anyio
async def test_hook_records_paired_code_and_raises():
    import httpx

    from spillway.api.mcp.bridge import _inspect_response, _State

    state = _State()
    resp, _ = _response(401, [json.dumps(_spec_body("unauthorized")).encode()],
                        {"content-type": "application/json"})
    with pytest.raises(httpx.HTTPStatusError):
        await _inspect_response(resp, state)
    assert (state.rejected_status, state.rejected_code) == (401, "unauthorized")


@pytest.mark.anyio
async def test_hook_stops_reading_past_limit_without_content_length():
    import httpx

    from spillway.api.mcp.bridge import ERROR_BODY_LIMIT, _inspect_response, _State

    assert ERROR_BODY_LIMIT == 64 * 1024
    chunks = [b'{"error": {"code": "shutting_down", "message": "'] + [b"x" * 16384] * 8 + [b'"}}']
    state = _State()
    resp, stream = _response(503, chunks, {"content-type": "application/json"})
    with pytest.raises(httpx.HTTPStatusError):
        await _inspect_response(resp, state)
    assert (state.rejected_status, state.rejected_code) == (503, None)
    assert stream.read < len(chunks)   # 超過上限即停止讀取


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_hook_ignores_get_and_delete(method):
    from spillway.api.mcp.bridge import _inspect_response, _State

    state = _State()
    resp, stream = _response(401, [b"{}"], {"content-type": "application/json"}, method=method)
    await _inspect_response(resp, state)
    assert stream.read == 0 and state.rejected_status is None


# ---------------------------------------------------------------------------- CHG-20261102-02 T2:max_sessions(L2)

def test_test_mcp_max_sessions_env_parsing(monkeypatch):
    from spillway import cli

    assert cli.TEST_MCP_MAX_SESSIONS_ENV == "SPILLWAY_TEST_MCP_MAX_SESSIONS"
    for raw, expected in [(None, None), ("", None), ("x", None), ("0", None), ("-1", None), ("1", 1),
                          ("7", 7)]:
        if raw is None:
            monkeypatch.delenv(cli.TEST_MCP_MAX_SESSIONS_ENV, raising=False)
        else:
            monkeypatch.setenv(cli.TEST_MCP_MAX_SESSIONS_ENV, raw)
        assert cli._test_mcp_max_sessions() == expected


def test_create_app_default_max_sessions():
    import inspect

    from spillway.api.mcp.server import MAX_SESSIONS
    from spillway.app import create_app

    assert inspect.signature(create_app).parameters["mcp_max_sessions"].default == MAX_SESSIONS


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"serve 提前結束 rc={proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail("serve 未在時限內開始監聽")


def test_serve_max_sessions_1_second_bridge_gets_503_exit_1(tmp_path: Path, bridges):
    from spillway import settings
    from spillway.store.db import connect

    home = tmp_path / "serve-home"
    home.mkdir()
    conn = connect(home / "spillway.db")   # migration + token(serve 看到已有雜湊就不印)
    try:
        token = settings.rotate_access_token(conn)
    finally:
        conn.close()
    port = _free_port()
    env = _env(tmp_path, SPILLWAY_TEST_MCP_MAX_SESSIONS="1")
    env["SPILLWAY_HOME"] = str(home)
    with (tmp_path / "serve.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "spillway", "serve", "--port", str(port)], env=env,
                                cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT)
        try:
            _wait_port(port, proc)
            first = bridges(_env(tmp_path, token=token), "all", port)
            _handshake(first)   # 第一個 session 保持開啟
            second = bridges(_env(tmp_path, token=token), "all", port)
            second.send(INIT)
            msg = second.recv_id(1)
            assert msg["error"] == {"code": -32000, "message": "gateway error (HTTP 503)",
                                    "data": {"http_status": 503}}
            rc, stderr, lines = second.finish(close_stdin=False)
            assert rc == 1
            _assert_clean_stderr(stderr, token, "Too many open sessions")
            _assert_jsonrpc_only(lines)
            rc_first, _, _ = first.finish()
            assert rc_first == 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
