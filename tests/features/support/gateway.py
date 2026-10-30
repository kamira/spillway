"""behave 情境用的閘道驅動(CHG-20261030-02 T15;決策 11)。

做法(與 `tests/integration/conftest.py` 的 `open_spillway` 同一方式):以
`spillway.app.create_app(clock=FakeClock, rng=SystemRng(seed=FIXED_SEED), extra_adapter_paths=..., db_path=...)`
組 app,手動進出 `app.router.lifespan_context(app)`(migration、adapter 發現、RetentionJob 排程),
再以 httpx `AsyncClient(ASGITransport)` 經真實 HTTP 介面(ASGI)打 `/v1/*` 與 `/api/*`。
behave 的步驟是同步函式,所以每個情境用一個 `asyncio.Runner` 承載事件迴圈:lifespan、client 與
每次請求都在同一個迴圈上執行(`Runner.run()`)。

- 時間:`FakeClock`(預設 2026-10-30T12:00:00Z);「系統時間推進」對應 `clock.advance()` / `clock.set()`。
- 亂數:`SystemRng(seed=FIXED_SEED)`,固定 seed。
- 假上游:`FakeApiServer`(背景執行緒,127.0.0.1 隨機埠),由 environment.py 在 before_all 啟動、
  每個情境 reset。假上游依 model 名稱設定模式,所以每個 api 成員配一個不同的模型(`API_MODELS`)。
- 成員:api 成員為 openai provider、`credential_ref = env:SPILLWAY_TEST_KEY`(值 `sk-cred-behave-7777`,與 AC-15 prompt 的 `sk-test-0000` 不同);
  local 成員為 provider `hosted_vllm`(決策 10);兩者 `api_base` 指向假上游 `/v1`。
- 「重新啟動」(AC-19):`start(extra_paths)` 以新的 `create_app(extra_adapter_paths=[tmp])` 建 app。
- RetentionJob 的背景輪詢設為 3600 秒(真實時間),情境中的清理一律由步驟明確呼叫
  `app.state.retention.run_once()`,結果不受輪詢時機影響。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from spillway.app import create_app
from spillway.clock import FakeClock, SystemRng
from spillway.store.db import connect

BASE_URL = "http://spillway.test"
FIXED_SEED = 20261030
# 憑證值刻意與 AC-15 prompt 裡的 `sk-test-0000` 不同(末端審查 M28):
# 否則 Router 的 extra_secrets 也會遮掉 prompt,分不出是 `sk-` 樣式規則遮罩的。
TEST_KEY = "sk-cred-behave-7777"
TEST_KEY_ENV = "SPILLWAY_TEST_KEY"
DEFAULT_MESSAGES = [{"role": "user", "content": "hello"}]
# 每個 api 成員依建立順序配一個價格表內、彼此不同的模型(假上游依 model 名稱設定模式)
API_MODELS = ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4.1")
LOCAL_MODEL = "local-model"


class Gateway:
    """一個情境的閘道狀態:app、client、時鐘、亂數、成員設定與最後一次回應。"""

    def __init__(self, home: Path, fake: Any) -> None:
        self.home = home
        self.fake = fake
        self.clock = FakeClock()
        self.seed = FIXED_SEED
        self.rng = SystemRng(seed=self.seed)
        self.db_path = home / "spillway.db"
        self.runner = asyncio.Runner()
        self.app: Any = None
        self.client: httpx.AsyncClient | None = None
        self._lifespan: Any = None
        self.extra_paths: list[Path] = []
        self.start_error: BaseException | None = None
        # 情境資料
        self.members: dict[str, dict[str, Any]] = {}     # id → {"body": POST body, "weight": str}
        self.member_model: dict[str, str] = {}           # api 成員 id → 假上游 model 名稱
        self.routes: dict[str, dict[str, Any]] = {}      # name → POST body
        self.last: httpx.Response | None = None
        self.responses: list[httpx.Response] = []
        self.last_ledger_id: int | None = None
        self.plans: list[dict[str, Any]] = []
        self.ledger_count_before: int | None = None
        self.created: list[httpx.Response] = []

    # ------------------------------------------------------------------ 生命週期
    @property
    def started(self) -> bool:
        return self.app is not None

    def start(self, extra_paths: Sequence[Path] = ()) -> None:
        """建 app 並進入 lifespan。已啟動時先停止(= 重新啟動)。"""
        if self.started:
            self.stop()
        self.extra_paths = list(extra_paths)
        app = create_app(clock=self.clock, rng=self.rng, extra_adapter_paths=tuple(self.extra_paths),
                         db_path=self.db_path, retention_poll_seconds=3600)
        lifespan = app.router.lifespan_context(app)
        self.runner.run(lifespan.__aenter__())
        self.app = app
        self._lifespan = lifespan
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)

    def ensure_started(self) -> None:
        if not self.started:
            self.start()

    def stop(self) -> None:
        if self.client is not None:
            self.runner.run(self.client.aclose())
            self.client = None
        if self._lifespan is not None:
            self.runner.run(self._lifespan.__aexit__(None, None, None))
            self._lifespan = None
        self.app = None

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self.runner.close()

    # ------------------------------------------------------------------ HTTP
    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.ensure_started()
        assert self.client is not None
        resp = self.runner.run(self.client.request(method, path, **kw))
        return resp

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("PUT", path, **kw)

    def ok(self, resp: httpx.Response, *statuses: int) -> dict[str, Any]:
        allowed = statuses or (200, 201, 204)
        assert resp.status_code in allowed, f"{resp.request.method} {resp.request.url.path} → {resp.status_code} {resp.text}"
        return resp.json() if resp.content else {}

    def chat(self, model: str, *, messages: list[dict[str, Any]] | None = None,
             **extra: Any) -> httpx.Response:
        body = {"model": model, "messages": messages or DEFAULT_MESSAGES, **extra}
        resp = self.post("/v1/chat/completions", json=body)
        self.last = resp
        self.responses.append(resp)
        return resp

    # ------------------------------------------------------------------ 成員與 route
    def api_body(self, dep_id: str, *, provider: str = "openai") -> dict[str, Any]:
        model = self.member_model.get(dep_id)
        if model is None:
            used = set(self.member_model.values())
            model = next(m for m in API_MODELS if m not in used)
            self.member_model[dep_id] = model
        return {
            "id": dep_id, "model_name": dep_id, "kind": "api", "provider": provider, "model": model,
            "params": {"api_base": self.fake.base_url + "/v1"},
            "credential_ref": f"env:{TEST_KEY_ENV}",
            "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []},
            "tags": [dep_id],
        }

    def local_body(self, dep_id: str, provider: str) -> dict[str, Any]:
        return {
            "id": dep_id, "model_name": dep_id, "kind": "local", "provider": provider,
            "model": LOCAL_MODEL, "params": {"api_base": self.fake.base_url + "/v1"},
            "capabilities": {"ctx": 32768, "tools": False, "vision": False, "efforts": []},
            "tags": [dep_id],
        }

    def plain_body(self, dep_id: str, provider: str) -> dict[str, Any]:
        """測試模組(acme、solo、broken)的 api 成員:不需要憑證與 api_base。"""
        return {
            "id": dep_id, "model_name": dep_id, "kind": "api", "provider": provider,
            "model": "fake-small",
            "capabilities": {"ctx": 8000, "tools": True, "vision": False, "efforts": []},
            "tags": [dep_id],
        }

    def create_member(self, body: dict[str, Any], weight: str = "medium") -> httpx.Response:
        resp = self.post("/api/deployments", json=body)
        self.created.append(resp)
        if resp.status_code == 201:
            self.members[body["id"]] = {"body": body, "weight": weight}
        return resp

    def update_member(self, dep_id: str, **changes: Any) -> dict[str, Any]:
        body = {**self.members[dep_id]["body"], **changes}
        out = self.ok(self.put(f"/api/deployments/{dep_id}", json=body), 200)
        self.members[dep_id]["body"] = body
        return out

    def create_route(self, name: str, slots: list[list[str]], **opts: Any) -> None:
        ids = [d for slot in slots for d in slot]
        body = {"name": name, "target": {"tags": {"all": [], "any": ids, "none": []}}, **opts}
        self.ok(self.post("/api/routes", json=body), 201)
        self.routes[name] = body
        order = [[{"deployment": d, "weight": self.members.get(d, {}).get("weight", "medium")}
                  for d in slot] for slot in slots]
        self.ok(self.put(f"/api/routes/{name}/order", json={"slots": order}), 200)

    def update_route(self, name: str, **changes: Any) -> None:
        body = {**self.routes[name], **changes}
        self.ok(self.put(f"/api/routes/{name}", json=body), 200)
        self.routes[name] = body

    def model_of(self, dep_id: str) -> str:
        return self.member_model[dep_id]

    # ------------------------------------------------------------------ 帳本與資料庫
    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def ledger(self, **params: Any) -> dict[str, Any]:
        """`GET /api/ledger`(分頁格式)。"""
        return self.ok(self.get("/api/ledger", params={"page_size": 500, **params}), 200)

    def ledger_total(self) -> int:
        return int(self.ledger()["total"])

    def latest_ledger(self, **params: Any) -> dict[str, Any]:
        items = self.ledger(**params)["items"]
        assert items, f"帳本沒有符合 {params} 的列"
        return items[0]

    def dry_run(self, route: str, **extra: Any) -> dict[str, Any]:
        body = {"messages": DEFAULT_MESSAGES, **extra}
        plan = self.ok(self.post(f"/api/routes/{route}/dry-run", json=body), 200)
        self.plans.append(plan)
        return plan

    def plan_entry(self, plan: dict[str, Any], dep_id: str) -> dict[str, Any]:
        for s in plan["slots"]:
            for e in s["entries"]:
                if e["deployment"] == dep_id:
                    return e
        for x in plan["excluded"]:
            if x["deployment"] == dep_id:
                return x
        raise AssertionError(f"dry-run 結果沒有成員 {dep_id}:{json.dumps(plan, ensure_ascii=False)}")

    def deployment(self, dep_id: str) -> dict[str, Any]:
        return self.ok(self.get(f"/api/deployments/{dep_id}"), 200)

    def dashboard_row(self, dep_id: str) -> dict[str, Any]:
        rows = self.ok(self.get("/api/dashboard"), 200)["deployments"]
        return next(r for r in rows if r["id"] == dep_id)

    def run_cleanup(self) -> int | None:
        self.ensure_started()
        return self.app.state.retention.run_once()

    def fake_requests(self, path: str = "/v1/chat/completions") -> list[Any]:
        return [r for r in self.fake.requests if r.path == path]
