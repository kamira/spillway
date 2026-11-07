"""behave 情境用的閘道驅動(CHG-20261030-02 T15;決策 11;CHG-20261107-03 T4 改為黑箱)。

做法(CHG-20261107-03 決策 8、11):每個情境一個 `tests.blackbox.harness.Serve`——以 `SPILLWAY_CMD serve`
啟動的子行程,資料目錄為情境的暫存 `SPILLWAY_HOME`,測試控制介面開啟(`SPILLWAY_TEST_CONTROL=1`)。
步驟只經真實 HTTP(`/v1/*`、`/api/*`)、測試控制介面(`/__test__/*`,spec 13)與唯讀資料庫
(`tests.blackbox.db.read_db`)操作它;本檔不 import spillway。serve 在第一次需要時才啟動(`ensure_started`),
所以「一個空的閘道資料目錄」仍可檢查資料目錄是空的。

- 時間:可控時鐘,啟動時刻 `START_TIME`(2026-10-30T12:00:00Z,與原 `FakeClock` 預設相同);
  `gw.clock` 的 `now()`、`set()`、`advance()` 對應 `GET/POST /__test__/clock`。
- 亂數:`SPILLWAY_TEST_RNG_SEED=FIXED_SEED`;`gw.seed = None` 後重新啟動即為「未給 seed」(spec 13.4.2)。
- 假上游:`tests.blackbox.fakeapi.shared_fake_api()`(行程內,environment.py 在 before_all 取得)、每個情境
  reset。假上游依 model 名稱設定模式,所以每個 api 成員配一個不同的模型(`API_MODELS`)。
- 成員:api 成員為 openai provider、`credential_ref = env:SPILLWAY_TEST_KEY`(serve 的環境變數值
  `sk-cred-behave-7777`,與 AC-15 prompt 的 `sk-test-0000` 不同);local 成員為 provider `hosted_vllm`
  (決策 10);兩者 `api_base` 指向假上游 `/v1`。
- 「重新啟動」(AC-19、無 seed、改環境):`start(extra_paths, **opts)` 經 `Serve.restart`(同一資料目錄、
  同一埠、時鐘延續);`opts` 為 `Serve` 的選項(例如 `graceful_timeout`、`allowed_unavailable_adapters`)。
- 假 CLI:`set_fake_bin(bin_dir)` 把假執行檔目錄放到 serve 的 PATH 最前面(已啟動時重新啟動);
  `serve_env()` 為 serve 啟動(或下次啟動)時使用的環境。
- 存取 token:serve 以 `SPILLWAY_TEST_NO_TOKEN=1` 啟動(與原行程內 app 相同:啟動時不簽發);需要 token 的
  步驟經 `POST /api/settings/token/rotate`(`gw.serve.rotate_token()`)。
- 保存清理:背景輪詢 3600 秒(`SPILLWAY_TEST_RETENTION_POLL_SECONDS`),情境中的清理一律由步驟明確呼叫
  `run_cleanup()`(`POST /__test__/retention/run`),結果不受輪詢時機影響。
- 停止:`close(failed)`——情境通過時 `Serve.stop()`(結束碼、Traceback、ERROR、逾時的斷言),失敗時 `kill()`。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from tests.blackbox.control import Control
from tests.blackbox.db import read_db
from tests.blackbox.harness import Serve

START_TIME = "2026-10-30T12:00:00Z"
FIXED_SEED = 20261030
RETENTION_POLL_SECONDS = 3600
# 憑證值刻意與 AC-15 prompt 裡的 `sk-test-0000` 不同(末端審查 M28):
# 否則 Router 的 extra_secrets 也會遮掉 prompt,分不出是 `sk-` 樣式規則遮罩的。
TEST_KEY = "sk-cred-behave-7777"
TEST_KEY_ENV = "SPILLWAY_TEST_KEY"
LOCAL_API_BASE_ENV = "SPILLWAY_LOCAL_API_BASE"
DEFAULT_MESSAGES = [{"role": "user", "content": "hello"}]
# 每個 api 成員依建立順序配一個價格表內、彼此不同的模型(假上游依 model 名稱設定模式)
API_MODELS = ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4.1")
LOCAL_MODEL = "local-model"


class _Clock:
    """serve 的可控時鐘(spec 13.4.1):`now()`、`set(t)`、`advance(**timedelta 參數)`(整數秒)。"""

    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def now(self) -> datetime:
        return self._gw.control.now()

    def set(self, t: datetime | str) -> None:
        self._gw.control.set_clock(t)

    def advance(self, **delta: float) -> datetime:
        seconds = timedelta(**delta).total_seconds()
        if seconds != int(seconds) or seconds < 0:
            raise ValueError(f"時鐘只能前進非負整數秒:{delta}")
        self._gw.control.advance(int(seconds))
        return self.now()


class Gateway:
    """一個情境的閘道狀態:serve 子行程、client、時鐘、成員設定與最後一次回應。"""

    def __init__(self, home: Path, fake: Any, *, fake_bin: Path | None = None) -> None:
        self.home = home
        self.fake = fake
        self.fake_bin = fake_bin
        self.clock = _Clock(self)
        self.seed: int | None = FIXED_SEED
        self.db_path = home / "spillway.db"
        self.serve: Serve | None = None
        self.client: httpx.Client | None = None
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
    def _serve_object(self) -> Serve:
        """尚未建立時以本情境的選項建立 `Serve`(不啟動)。"""
        if self.serve is None:
            self.serve = Serve(
                self.home, start_time=START_TIME, rng_seed=self.seed, adapter_paths=self.extra_paths,
                retention_poll_seconds=RETENTION_POLL_SECONDS, no_token=True, fake_bin=self.fake_bin,
                extra_env={TEST_KEY_ENV: TEST_KEY, LOCAL_API_BASE_ENV: self.fake.base_url + "/v1"})
        return self.serve

    @property
    def started(self) -> bool:
        return self.serve is not None and self.serve.proc is not None and self.serve.returncode is None

    def start(self, extra_paths: Sequence[Path] = (), **opts: Any) -> None:
        """啟動 serve;已啟動過時經 `Serve.restart`(= 重新啟動,時鐘延續、`rng_seed` 依 `self.seed`)。"""
        self.extra_paths = list(extra_paths)
        self._close_client()
        if self.serve is None or self.serve.proc is None:
            s = self._serve_object()
            s.extra_env.update(opts.pop("extra_env", None) or {})
            s.adapter_paths = list(self.extra_paths)
            s.rng_seed = self.seed
            for k, v in opts.items():
                setattr(s, k, v)
            s.start()
        else:
            self.serve.restart(adapter_paths=self.extra_paths, rng_seed=self.seed, **opts)
        self.client = self.serve.http(timeout=60.0)

    def ensure_started(self) -> None:
        if not self.started:
            self.start(self.extra_paths)

    def set_fake_bin(self, bin_dir: Path) -> None:
        """把假 CLI 目錄放到 serve 的 PATH 最前面;serve 已啟動時重新啟動(啟動後改環境一律重啟)。"""
        self.fake_bin = bin_dir
        if self.started:
            self.start(self.extra_paths, fake_bin=bin_dir)
        else:
            self._serve_object().fake_bin = bin_dir

    def serve_env(self) -> dict[str, str]:
        return self._serve_object().env()

    @property
    def control(self) -> Control:
        self.ensure_started()
        assert self.serve is not None
        return self.serve.control

    def _close_client(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def stop(self) -> None:
        self._close_client()
        if self.serve is not None:
            self.serve.stop()

    def close(self, failed: bool = False) -> None:
        self._close_client()
        if self.serve is not None:
            if failed:
                self.serve.kill()
            else:
                self.serve.stop()

    # ------------------------------------------------------------------ HTTP
    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.ensure_started()
        assert self.client is not None
        resp = self.client.request(method, path, **kw)
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
        """唯讀連線(`mode=ro`;資料庫 schema 為跨語言契約,寫入一律經 `/api` 或 `/__test__/*`)。
        serve 從未啟動時先啟動(建出資料庫);已結束的 serve(停機情境)不重新啟動。"""
        if self.serve is None or self.serve.proc is None:
            self.ensure_started()
        with read_db(self.home) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

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
        """`POST /__test__/retention/run`:立即清理一次,回傳刪除的內容列數(失敗為 None)。"""
        return self.control.retention_run()

    def fake_requests(self, path: str = "/v1/chat/completions") -> list[Any]:
        return [r for r in self.fake.requests if r.path == path]
