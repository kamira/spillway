"""假 API 上游的 Starlette app 與背景執行緒伺服器(CHG-20261030-02 T5)。

模式說明與 LiteLLM 對接實測結果見套件 `__init__.py` 的 docstring。
"""
from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

HOST = "127.0.0.1"  # 只綁本機;不提供其他位址的選項

MODES = (
    "ok",
    "http_402",
    "insufficient_quota",
    "http_429",
    "http_500",
    "delay",
    "echo_auth",
)

DEFAULT_MODELS = ("gpt-4o-mini", "claude-sonnet-4-5", "local-model")

OK_TEXT = "fake upstream ok"
PROMPT_TOKENS = 11
COMPLETION_TOKENS = 7


@dataclass(frozen=True)
class ModeSpec:
    mode: str = "ok"
    seconds: float = 0.0  # delay 模式的延遲秒數
    status: int = 400  # echo_auth 模式回傳的 HTTP 狀態碼

    @classmethod
    def build(cls, mode: str, **kw: Any) -> "ModeSpec":
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        unknown = set(kw) - {"seconds", "status"}
        if unknown:
            raise ValueError(f"unknown mode options: {sorted(unknown)}")
        if mode == "delay" and "seconds" not in kw:
            raise ValueError("delay mode requires seconds=")
        return cls(mode=mode, seconds=float(kw.get("seconds", 0.0)), status=int(kw.get("status", 400)))


@dataclass
class RecordedRequest:
    """收到的請求。不保存 Authorization / x-api-key 的值,只記錄有無與來源 header 名稱。"""

    method: str
    path: str
    model: str | None
    has_auth: bool
    auth_header: str | None  # "authorization" | "x-api-key" | None
    body: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "has_auth": self.has_auth,
            "auth_header": self.auth_header,
            "body": self.body,
        }


@dataclass
class FakeState:
    modes: dict[str, ModeSpec] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_mode(self, model: str, mode: str, **kw: Any) -> None:
        spec = ModeSpec.build(mode, **kw)
        with self.lock:
            self.modes[model] = spec

    def mode_for(self, model: str | None) -> ModeSpec:
        with self.lock:
            if model is not None and model in self.modes:
                return self.modes[model]
            return self.modes.get("*", ModeSpec())

    def record(self, rec: RecordedRequest) -> None:
        with self.lock:
            self.requests.append(rec)

    def snapshot(self) -> list[RecordedRequest]:
        with self.lock:
            return list(self.requests)

    def reset(self) -> None:
        with self.lock:
            self.modes.clear()
            self.requests.clear()


# ---------------------------------------------------------------- 錯誤 body


def _openai_error(status: int, message: str, err_type: str, code: str | None) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "param": None, "code": code}},
        status_code=status,
    )


def _anthropic_error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": err_type, "message": message}},
        status_code=status,
    )


# (status, message, openai type, openai code, anthropic type)
_ERRORS: dict[str, tuple[int, str, str, str | None, str]] = {
    "http_402": (402, "Payment required: credit balance exhausted", "billing_error", "payment_required", "billing_error"),
    # OpenAI 實際行為:額度用完回 HTTP 429,error.type 與 error.code 皆為 insufficient_quota。
    "insufficient_quota": (
        429,
        "You exceeded your current quota, please check your plan and billing details.",
        "insufficient_quota",
        "insufficient_quota",
        "insufficient_quota",
    ),
    "http_429": (429, "Rate limit reached, please retry later", "rate_limit_error", "rate_limit_exceeded", "rate_limit_error"),
    "http_500": (500, "The server had an error while processing your request", "server_error", None, "api_error"),
}


def _auth_value(request: Request) -> tuple[str | None, str | None]:
    if "authorization" in request.headers:
        return "authorization", request.headers["authorization"]
    if "x-api-key" in request.headers:
        return "x-api-key", request.headers["x-api-key"]
    return None, None


async def _read_json(request: Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def _handle(request: Request, protocol: str) -> JSONResponse:
    state: FakeState = request.app.state.fake
    body = await _read_json(request)
    model = body.get("model") if body else None
    header_name, auth = _auth_value(request)
    state.record(
        RecordedRequest(
            method=request.method,
            path=request.url.path,
            model=model,
            has_auth=auth is not None,
            auth_header=header_name,
            body=body,
        )
    )
    if body is None or not isinstance(model, str):
        msg = "request body must be JSON with a 'model' field"
        if protocol == "anthropic":
            return _anthropic_error(400, msg, "invalid_request_error")
        return _openai_error(400, msg, "invalid_request_error", None)

    spec = state.mode_for(model)
    if spec.mode == "delay":
        await asyncio.sleep(spec.seconds)
    elif spec.mode == "echo_auth":
        msg = f"invalid credential: {auth}"
        if protocol == "anthropic":
            return _anthropic_error(spec.status, msg, "authentication_error")
        return _openai_error(spec.status, msg, "invalid_request_error", "invalid_api_key")
    elif spec.mode in _ERRORS:
        status, msg, o_type, o_code, a_type = _ERRORS[spec.mode]
        if protocol == "anthropic":
            return _anthropic_error(status, msg, a_type)
        return _openai_error(status, msg, o_type, o_code)

    if protocol == "anthropic":
        return JSONResponse(
            {
                "id": "msg_fake_0001",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": OK_TEXT}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": COMPLETION_TOKENS},
            }
        )
    return JSONResponse(
        {
            "id": "chatcmpl-fake-0001",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": OK_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": PROMPT_TOKENS,
                "completion_tokens": COMPLETION_TOKENS,
                "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            },
        }
    )


# ---------------------------------------------------------------- 路由


async def chat_completions(request: Request) -> JSONResponse:
    return await _handle(request, "openai")


async def messages(request: Request) -> JSONResponse:
    return await _handle(request, "anthropic")


async def list_models(request: Request) -> JSONResponse:
    state: FakeState = request.app.state.fake
    state.record(RecordedRequest("GET", request.url.path, None, _auth_value(request)[1] is not None, _auth_value(request)[0]))
    with state.lock:
        names = list(DEFAULT_MODELS) + [m for m in state.modes if m != "*" and m not in DEFAULT_MODELS]
    return JSONResponse(
        {"object": "list", "data": [{"id": n, "object": "model", "created": 0, "owned_by": "fake"} for n in names]}
    )


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# 控制端點:供子行程(驗收操作)從外部設定模式;pytest 內請直接用 fixture。
async def control_mode(request: Request) -> JSONResponse:
    state: FakeState = request.app.state.fake
    body = await _read_json(request) or {}
    model, mode = body.pop("model", None), body.pop("mode", None)
    if not isinstance(model, str) or not isinstance(mode, str):
        return JSONResponse({"error": "model and mode are required"}, status_code=400)
    try:
        state.set_mode(model, mode, **body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


async def control_reset(request: Request) -> JSONResponse:
    request.app.state.fake.reset()
    return JSONResponse({"ok": True})


async def control_requests(request: Request) -> JSONResponse:
    return JSONResponse([r.as_dict() for r in request.app.state.fake.snapshot()])


def create_app(state: FakeState | None = None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/models", list_models, methods=["GET"]),
            Route("/v1/messages", messages, methods=["POST"]),
            Route("/_fake/mode", control_mode, methods=["POST"]),
            Route("/_fake/reset", control_reset, methods=["POST"]),
            Route("/_fake/requests", control_requests, methods=["GET"]),
        ]
    )
    app.state.fake = state or FakeState()
    return app


# ---------------------------------------------------------------- 背景伺服器


class FakeApiServer:
    """在背景執行緒以 uvicorn 跑假上游,綁 127.0.0.1 的隨機埠(或指定埠)。"""

    def __init__(self, port: int = 0) -> None:
        self.state = FakeState()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((HOST, port))
        self.port: int = self._sock.getsockname()[1]
        config = uvicorn.Config(
            create_app(self.state),
            host=HOST,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, name="fake-api-upstream", daemon=True)

    def _run(self) -> None:
        self._server.run(sockets=[self._sock])

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def start(self, timeout: float = 10.0) -> "FakeApiServer":
        self._thread.start()
        tick = threading.Event()
        for _ in range(int(timeout / 0.01)):
            if self._server.started or not self._thread.is_alive():
                break
            tick.wait(0.01)
        if not self._server.started:
            raise RuntimeError("fake API upstream failed to start")
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._sock.close()

    # 便利方法(fixture 介面)
    def set_mode(self, model: str, mode: str, **kw: Any) -> None:
        self.state.set_mode(model, mode, **kw)

    def reset(self) -> None:
        self.state.reset()

    @property
    def requests(self) -> list[RecordedRequest]:
        return self.state.snapshot()
