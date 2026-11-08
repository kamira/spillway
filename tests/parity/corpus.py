"""parity 語料(CHG-20261108-01 T3;spec 15.11;AM-10)。不 import spillway。

每個 `Scenario` 是一組循序請求,宣告預期產生內容的比對面(`surfaces`,runner 斷言每一面非空)與所屬模組(`module`,供差距清單以
測試 id 管理)。名稱等於 15.2「對應 parity 情境」的情境,每個比對面一個;其餘情境補各路徑規則的涵蓋。

語料範圍(15.11):每個 slot 至多一個雲端候選、只含循序請求、不設 `budget_precheck` 與 `usd` 上限、不含 `POST /api/estimate`、
`POST /api/routes/{name}/dry-run`、`GET /api/dashboard`、`GET /api/adapters`;不可達上游固定為 `UNREACHABLE_BASE`。
fakeapi 的 `echo_auth` 模式不得進語料(15.10.2)。金鑰只用 `sk-test-`、`sk-fake` 開頭的假值。

`FAKE_BASE` 在請求 body 中由 runner 換成本次 fakeapi 的 `base_url`。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tests.fakes.api_upstream.server import RecordedRequest
from tests.parity.normalize import (
    DB_ROWS, ERRORS, HEADERS, JSON_BODY, MODE_MALFORMED, MODE_UNKNOWN_PATH, STATUS, UPSTREAM, Capture, Exchange,
)

__all__ = ["CORPUS_KEYS", "CORPUS_KEY_ENVS", "FAKE_BASE", "SCENARIOS", "START_TIME", "UNREACHABLE_BASE", "FakeMode", "Scenario", "Step",
           "sample_capture"]

START_TIME = "2026-11-08T00:00:00Z"
FAKE_BASE = "$FAKE_BASE"
UNREACHABLE_BASE = "http://127.0.0.1:9/v1"
KEY_OPENAI_ENV, KEY_ANTHROPIC_ENV = "SPILLWAY_PARITY_KEY_OPENAI", "SPILLWAY_PARITY_KEY_ANTHROPIC"
KEY_OPENAI, KEY_ANTHROPIC = "sk-test-parity-corpus-0001", "sk-fake-parity-corpus-0002"
CORPUS_KEYS = (KEY_OPENAI, KEY_ANTHROPIC)
CORPUS_KEY_ENVS = {KEY_OPENAI_ENV: KEY_OPENAI, KEY_ANTHROPIC_ENV: KEY_ANTHROPIC}
SEEDED_PARAMS_KEY = "sk-fake-parity-seeded-0003"

CAPS = {"ctx": 128000, "tools": True, "vision": False, "efforts": []}
HELLO = [{"role": "user", "content": "hello"}]


@dataclass(frozen=True)
class Step:
    method: str
    path: str
    json: Any = None
    content: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()
    error_mode: str = ""


@dataclass(frozen=True)
class FakeMode:
    """設定 fakeapi 對某個 model 的模式(`echo_auth` 不得使用)。"""

    model: str
    mode: str
    options: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    module: str
    surfaces: tuple[str, ...]
    steps: tuple[Any, ...] = field(default_factory=tuple)


def api_member(dep_id: str, model: str, *, provider: str = "openai", credential: str | None = KEY_OPENAI_ENV,
               base: str = FAKE_BASE + "/v1", tags: tuple[str, ...] = ()) -> dict[str, Any]:
    body: dict[str, Any] = {"id": dep_id, "model_name": dep_id, "kind": "api", "provider": provider, "model": model,
                            "params": {"api_base": base}, "capabilities": CAPS, "tags": [dep_id, *tags]}
    if credential is not None:
        body["credential_ref"] = f"env:{credential}"
    return body


def local_member(dep_id: str, model: str = "local-model", *, base: str = FAKE_BASE + "/v1") -> dict[str, Any]:
    return {"id": dep_id, "model_name": dep_id, "kind": "local", "provider": "hosted_vllm", "model": model,
            "params": {"api_base": base}, "capabilities": {**CAPS, "tools": False}, "tags": [dep_id]}


def create(body: dict[str, Any]) -> Step:
    return Step("POST", "/api/deployments", json=body)


def route(name: str, slots: list[list[str]], **opts: Any) -> tuple[Step, Step]:
    ids = [d for slot in slots for d in slot]
    order = [[{"deployment": d, "weight": "medium"} for d in slot] for slot in slots]
    return (Step("POST", "/api/routes", json={"name": name, "target": {"tags": {"all": [], "any": ids, "none": []}}, **opts}),
            Step("PUT", f"/api/routes/{name}/order", json={"slots": order}))


def chat(model: str, messages: list[dict[str, Any]] | None = None, **extra: Any) -> Step:
    return Step("POST", "/v1/chat/completions", json={"model": model, "messages": messages or HELLO, **extra})


_ONE_MEMBER = (create(api_member("alpha", "gpt-4o-mini")), *route("main", [["alpha"]]))

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("status", "api", (STATUS,), (
        *_ONE_MEMBER,
        Step("GET", "/v1/models"),
        Step("GET", "/api/deployments/alpha"),
        create(api_member("alpha", "gpt-4o-mini")),    # 409
        Step("GET", "/api/deployments/missing"),
        Step("DELETE", "/v1/models"),
        Step("GET", "/does-not-exist", error_mode=MODE_UNKNOWN_PATH),
        chat("main"),
    )),
    Scenario("headers", "api", (STATUS, HEADERS), (
        *_ONE_MEMBER,
        chat("main"),
        Step("GET", "/api/settings/context"),
        Step("GET", "/does-not-exist", error_mode=MODE_UNKNOWN_PATH),
    )),
    Scenario("json_body", "admin", (STATUS, JSON_BODY), (
        *_ONE_MEMBER,
        Step("POST", "/api/groups", json={"name": "team", "selector": {"all": [], "any": ["alpha"], "none": []}}),
        Step("GET", "/api/groups"),
        Step("GET", "/api/groups/team"),
        Step("POST", "/api/groups/preview", json={"selector": {"all": [], "any": ["alpha"], "none": []}}),
        Step("GET", "/api/deployments"),
        Step("GET", "/api/deployments/alpha"),
        Step("PUT", "/api/deployments/alpha", json=api_member("alpha", "gpt-4o-mini", tags=("extra",))),
        Step("GET", "/api/routes"),
        Step("GET", "/api/routes/main"),
        Step("GET", "/api/routes/main/effective-order"),
        Step("GET", "/v1/models"),
        chat("main"),
        chat("main", temperature=0.5, max_tokens=64),
        Step("GET", "/api/settings"),
        Step("GET", "/api/settings/context"),
        Step("GET", "/api/mcp/catalog"),
        Step("GET", "/api/mcp/catalog?group=team"),
        Step("GET", "/api/config/export"),
    )),
    Scenario("errors", "api", (STATUS, ERRORS), (
        create(api_member("down", "gpt-4o-mini", base=UNREACHABLE_BASE)),
        *route("broken", [["down"]]),
        Step("GET", "/does-not-exist", error_mode=MODE_UNKNOWN_PATH),
        Step("POST", "/v1/chat/completions", json={"model": "broken"}),      # 422:缺 messages
        Step("POST", "/v1/chat/completions", content=b"{not json", headers=(("content-type", "application/json"),),
             error_mode=MODE_MALFORMED),
        chat("broken", stream=True),                                         # streaming_unsupported
        chat("broken"),                                                      # no_candidate
        chat("no-such-route"),
        Step("GET", "/api/deployments/missing"),
        Step("DELETE", "/v1/models"),
        Step("POST", "/__test__/clock", json={}),                            # 422:沒有任何欄位對應
        Step("POST", "/__test__/cooldowns", json={"deployment": "ghost", "until": START_TIME, "reason": "rate_limited"}),
    )),
    Scenario("db_rows", "store", (STATUS, DB_ROWS), (
        create({**api_member("alpha", "gpt-4o-mini", tags=("gold", "paid")),
                "budget": {"type": "credit_pool", "limit_amount": 100.0, "window": "monthly", "reset_anchor": 3}}),
        create(local_member("mini")),
        *route("main", [["alpha"], ["mini"]], tags=["t1"]),
        Step("POST", "/api/groups", json={"name": "team", "selector": {"all": ["gold"], "any": [], "none": []}}),
        Step("PUT", "/api/settings/context", json={"record_context_default": True, "context_retention_days": 3}),
        chat("main"),
        Step("POST", "/__test__/clock", json={"advance_seconds": 90}),
        chat("main", [{"role": "user", "content": "second"}]),
        Step("POST", "/__test__/cooldowns", json={"deployment": "alpha", "until": "2026-11-08T01:00:00Z", "reason": "rate_limited"}),
    )),
    Scenario("upstream_chat", "adapters.builtin", (STATUS, UPSTREAM), (
        create(api_member("alpha", "gpt-4o-mini")),
        create(api_member("beta", "claude-sonnet-4-5", provider="anthropic", credential=KEY_ANTHROPIC_ENV, base=FAKE_BASE)),
        create(local_member("mini")),
        chat("alpha", temperature=0.2, max_tokens=32),
        chat("beta", max_tokens=32),
        chat("local/mini"),
    )),
    Scenario("fallback", "routing", (STATUS, HEADERS, JSON_BODY, DB_ROWS, UPSTREAM), (
        FakeMode("gpt-4o-mini", "http_429"),
        create(api_member("first", "gpt-4o-mini")),
        create(api_member("second", "gpt-4.1-mini")),
        *route("main", [["first"], ["second"]]),
        chat("main"),
        Step("GET", "/api/deployments/first"),
        Step("GET", "/api/ledger"),
    )),
    Scenario("upstream_failures", "routing", (STATUS, ERRORS, DB_ROWS, UPSTREAM), (
        FakeMode("gpt-4o-mini", "http_500"),
        create(api_member("alpha", "gpt-4o-mini")),
        *route("main", [["alpha"]]),
        chat("main"),
        Step("GET", "/api/ledger"),
        Step("GET", "/api/ledger/1/payload"),
    )),
    Scenario("secret_in_prompt", "context", (STATUS, JSON_BODY, DB_ROWS, UPSTREAM), (
        *_ONE_MEMBER,
        chat("main", [{"role": "user", "content": f"my key is {KEY_OPENAI}"}]),
        Step("GET", "/api/ledger"),
        Step("GET", "/api/ledger/1/payload"),
    )),
    Scenario("truncated_payload", "context", (STATUS, JSON_BODY, DB_ROWS), (
        *_ONE_MEMBER,
        Step("PUT", "/api/settings/context", json={"context_max_bytes": 1024}),
        chat("main", [{"role": "user", "content": "x" * 4096}]),
        Step("GET", "/api/ledger/1/payload"),
    )),
    Scenario("ledger_query", "ledger", (STATUS, JSON_BODY), (
        *_ONE_MEMBER,
        chat("main"),
        chat("main"),
        Step("GET", "/api/ledger?page=1"),
        Step("GET", "/api/ledger?status=ok&min_depth=0"),
        Step("GET", "/api/ledger?min_depth=999"),      # 422
        Step("GET", "/api/ledger/1/payload"),
        Step("GET", "/api/ledger/99/payload"),
    )),
    Scenario("deployment_test", "admin", (STATUS, JSON_BODY, DB_ROWS, UPSTREAM), (
        create(api_member("alpha", "gpt-4o-mini")),
        create(api_member("down", "gpt-4.1-mini", base=UNREACHABLE_BASE)),
        Step("POST", "/api/deployments/alpha/test"),
        Step("POST", "/api/deployments/down/test"),
    )),
    Scenario("token_rotate", "settings", (STATUS, JSON_BODY), (
        Step("GET", "/api/settings"),
        Step("POST", "/api/settings/token/rotate"),
        Step("GET", "/api/settings"),
    )),
    Scenario("seeded_params", "test_control", (STATUS, JSON_BODY, DB_ROWS), (
        create(api_member("alpha", "gpt-4o-mini")),
        Step("POST", "/__test__/raw/deployment-params", json={"id": "alpha", "params": {"api_key": SEEDED_PARAMS_KEY}}),
        Step("GET", "/api/deployments/alpha"),
        Step("GET", "/api/deployments"),
    )),
)


# ---------------------------------------------------------------- comparator 自測的記憶體夾具(不是語料情境)

SAMPLE_TOKEN = "A" * 43
_JSON = ("content-type", "application/json")


def _ex(method: str, path: str, status: int, body: Any, *headers: tuple[str, str], mode: str = "") -> Exchange:
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    return Exchange(method, path, status, tuple(headers) or (_JSON,), raw, mode)


def sample_capture(side: str = "a") -> Capture:
    """一份涵蓋全部比對面的原始擷取;兩側只有環境衍生值與不定值不同(依 spec 15 應判相等)。供 comparator 單元測試與突變矩陣使用。"""
    n = 0 if side == "a" else 1
    port, fake = 51000 + n, f"http://127.0.0.1:{61000 + n}"
    home = f"/tmp/parity-sample-{side}/home"
    chat_id = "chatcmpl-" + ("a" if side == "a" else "b") * 32
    chat_headers = (_JSON, ("X-Spillway-Served-By", "alpha"), ("X-Spillway-Slot", "1"), ("X-Spillway-Est-Cost", "0.00000123"),
                    ("cache-control", "no-store"), ("x-content-type-options", "nosniff"))
    chat = {"id": chat_id, "object": "chat.completion", "created": 1794096000, "model": "main",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
    exchanges = [
        _ex("POST", "/v1/chat/completions", 200, chat, *chat_headers),
        _ex("POST", "/v1/chat/completions", 422, {"error": {"code": "validation_error", "message": "body.messages: Field required"}}),
        _ex("POST", "/v1/chat/completions", 503, {"error": {"code": "no_candidate", "message": "沒有可用的候選成員;嘗試:a:error"}}),
        _ex("GET", "/does-not-exist", 404, {"error": {"code": "not_found", "message": "Not Found"}}, mode=MODE_UNKNOWN_PATH),
        _ex("POST", "/v1/chat/completions", 422, {"error": {"code": "validation_error", "message": f"body.{n}: decode"}}, mode=MODE_MALFORMED),
        _ex("POST", "/v1/chat/completions", 500, {"error": {"code": "internal_error", "message": f"upstream said {n}"}}),
        _ex("GET", "/api/deployments/missing", 404, {"error": {"code": "not_found", "message": f"訊息 {n}"}}),
        _ex("GET", "/api/ledger/export.csv", 200, f"a,b\n{n},2\n".encode(), ("content-type", "text/csv")),
        _ex("GET", "/api/settings/context", 200, {"record_context_default": True}, _JSON, ("cache-control", "no-store")),
        _ex("POST", "/api/settings/token/rotate", 200, {"token": SAMPLE_TOKEN}),
    ]
    base = {"method": "POST", "has_auth": True, "auth_header": "authorization", "query_names": (), "has_json": True, "body_length": 10}
    upstream = [
        RecordedRequest(path="/v1/chat/completions", model="gpt-4o-mini", body={"model": "gpt-4o-mini", "messages": HELLO},
                        header_names=("accept", "authorization", "content-type", "host", "x-stainless-arch"),
                        auth_headers=("authorization",), json_body={"model": "gpt-4o-mini", "messages": HELLO}, **base),
        RecordedRequest(path="/v1/messages", model="claude", body={"model": "claude", "messages": HELLO},
                        header_names=("anthropic-version", "content-type", "x-api-key"), auth_headers=("x-api-key",),
                        json_body={"model": "claude", "messages": HELLO}, **base),
        RecordedRequest(path="/v1/chat/completions", model="local-model", body={"model": "local-model", "messages": HELLO},
                        header_names=("content-type", "authorization") if n else ("content-type",), auth_headers=(),
                        json_body={"model": "local-model", "messages": HELLO}, **base),
        RecordedRequest("GET", "/v1/models", None, False, None, header_names=("accept",), body_length=0),
    ]
    db = {
        "deployments": [{"id": ("text", "alpha"), "params": ("text", json.dumps({"api_base": fake + "/v1", "x": 1}, sort_keys=True)),
                         "capabilities": ("text", '{"ctx": 1}')}],
        "settings": [{"key": ("text", "access_token_sha256"), "value": ("text", ("a" if side == "a" else "b") * 64)},
                     {"key": ("text", "context_retention_days"), "value": ("text", "7")}],
        "usage_ledger": [{"id": ("integer", 1), "ts": ("text", START_TIME), "status": ("text", "ok"), "tokens_in": ("integer", 11),
                          "latency_ms": ("integer", 5 + n), "error": ("text", f"upstream text {n}"),
                          "cost_usd": ("real", 0.5 + n), "est_tokens_in": ("integer", 3 + n),
                          "est_tokens_out_p50": ("null", None), "est_tokens_out_p90": ("null", None), "est_cost_usd_p90": ("null", None)}],
        "usage_payloads": [{"ledger_id": ("integer", 1), "ts": ("text", START_TIME),
                            "request_json": ("text", '{"a": 1, "b": 2}' if side == "a" else '{"b":2,"a":1}'),
                            "response_text": ("text", "ok"), "bytes": ("integer", 30), "truncated": ("integer", 0), "redacted": ("integer", 0)}],
    }
    return Capture(scenario="sample", port=port, home=home, fake_base_url=fake, corpus_keys=CORPUS_KEYS, tokens=(SAMPLE_TOKEN,),
                   exchanges=exchanges, upstream=upstream, db=db, user_version=2,
                   log_lines=["SPILLWAY-LOG INFO spillway.router request ok", "SPILLWAY-LOG-CONT more"],
                   model_credentials={"gpt-4o-mini": True, "claude": True, "local-model": False})
