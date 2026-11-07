"""M4a 操作驗收的自動部分(CHG-20261102-02「### Acceptance operation」;T6;macOS)。

拆成 2 個測試函式:
1. `test_m4a_operation_config`:設定匯出入、帳本 CSV、設定總覽、群組名稱規則與 NFR-4 搜尋。
   - 子行程假 API 上游(`python -m tests.fakes.api_upstream`)與假 CLI(PATH 只含假 claude/codex);
   - 每個暫存資料目錄先以子行程 `SPILLWAY_CMD init`(`run_cli`)產生 token(serve 看到已有雜湊就不印;
     明文只存在測試行程記憶體),再起子行程 `SPILLWAY_CMD serve --port <隨機>`(`tests.blackbox.harness.Serve`,
     開啟測試控制介面以種入帳本與冷卻);
   - serve 1:以管理 API 建立 api-a(`credential_ref: env:SPILLWAY_TEST_KEY`,子行程環境帶假值)、
     api-b(無憑證)、claude-sub(claude-cli,假 CLI)、群組 `coding`、task route `review`(手動順序)
     與 chat route `chat-x`;送 3 次請求產生帳本(2 次 task 到 review、1 次 chat 到 chat-x)。
     「其中一列 `error` 以 `=1+1` 開頭」:假上游與假 CLI 的錯誤訊息是固定文字,無法由請求帶出,所以
     由測試行程以 `POST /__test__/ledger` upsert 把 review 的一列 `error` 改為 `=1+1 …`(以唯讀連線讀出整列,
     只改該欄後整列取代;其餘列不動;spec 13.4.3)。
   - 匯出 A → serve 2(空資料目錄)以 replace 匯入 A → 匯出 B;黃金檔(決策 8)serve 3 → serve 4 往返
     (DB 比對為設定表快照:只比 `CONFIG_TABLES` 與 token 雜湊以外的 settings,不含帳本、payload、冷卻);
     serve 3 的黃金檔資料庫:設定表在 serve 啟動前以 sqlite3 依凍結的 schema 黃金檔建立(CHG-20261107-03 AM-2),
     token 由 serve 簽發(不再寫固定雜湊),不該出現在匯出檔的冷卻、帳本、payload 經 `/__test__/*` 種入;
     serve 1 上 merge、dry_run、replace(縮小)、不合法檔案、兩個 claude-cli、CSV、總覽、`..` 群組。
2. `test_m4a_operation_mcp_stdio_max_sessions`:`SPILLWAY_TEST_MCP_MAX_SESSIONS=1` 的 serve 上,第一個
   MCP session(官方 SDK Streamable HTTP)保持開啟,第二個以子行程 `SPILLWAY_CMD mcp-stdio`(官方 SDK stdio 用戶端)
   initialize → 錯誤 `data == {"http_status": 503}`、結束碼 1。

產物(`artifacts/m4a/`,不進版控):
- `http-log.jsonl`:測試行程對閘道的每一筆 HTTP 請求一行,只有 `method`、`path`(不含 query)、`status`、
  `elapsed_ms` 四鍵(httpx event hook);不記 header 與 body。
- `operation-results.json`:各函式的觀察值(不含任何秘密)。
模組結束時以假金鑰與全部 token 明文搜尋 `artifacts/m4a/` 每個檔案,命中須為 0。
本檔不 import spillway(邊界守門);資料庫一律唯讀開啟(`tests.blackbox.db.read_db`)。
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import os
import shutil
import signal
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.acceptance import procs
from tests.blackbox.db import ACCESS_TOKEN_KEY, build_db, read_db
from tests.blackbox.harness import Serve, child_env, run_cli
from tests.blackbox.mask import redact_tokens
from tests.blackbox.mcp import group_alive, mcp_http_session, stdio_session
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)"),
]

REPO_ROOT = procs.REPO_ROOT
ARTIFACTS = REPO_ROOT / "artifacts" / "m4a"
HTTP_LOG = ARTIFACTS / "http-log.jsonl"
RESULTS = ARTIFACTS / "operation-results.json"
LOG_KEYS = {"method", "path", "status", "elapsed_ms"}
GRACE = 2.0
YAML_CT = {"Content-Type": "application/yaml"}
EXPORT, IMPORT = "/api/config/export", "/api/config/import"
API_A, API_B, CLAUDE, GROUP = "api-a", "api-b", "claude-sub", "coding"
PROMPT = "m4a-operation-prompt-5151"
FORMULA_ERROR = "=1+1 injected-by-acceptance"
CONFIG_TABLES = ("deployments", "deployment_tags", "budgets", "groups", "routes", "route_targets")
# 設定匯出黃金檔(CHG-20261102-02 決策 8;與 unit 測試共用的資料檔)與資料庫 schema 黃金檔(資料庫為契約,AM-2)
GOLDEN = REPO_ROOT / "tests" / "unit" / "data" / "config_golden.yaml"
SCHEMA_GOLDEN = REPO_ROOT / "tests" / "blackbox" / "golden" / "schema_v2.json"
GOLDEN_NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
# 本模組產生過的全部 token 明文(只在測試行程記憶體);斷言訊息與產物掃描用
TOKENS: list[str] = []


# ---------------------------------------------------------------- 產物


class HttpLog:
    _lock = threading.Lock()

    @staticmethod
    def _line(resp: httpx.Response) -> None:
        t0 = resp.request.extensions.get("m4a_t0")
        ms = round((time.perf_counter() - t0) * 1000, 2) if t0 is not None else None
        line = {"method": resp.request.method, "path": resp.request.url.path, "status": resp.status_code,
                "elapsed_ms": ms}
        with HttpLog._lock:
            HTTP_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(HTTP_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    @staticmethod
    def sync_hooks() -> dict[str, list[Any]]:
        def req(r: httpx.Request) -> None:
            r.extensions["m4a_t0"] = time.perf_counter()

        return {"request": [req], "response": [HttpLog._line]}

    @staticmethod
    def async_hooks() -> dict[str, list[Any]]:
        async def req(r: httpx.Request) -> None:
            r.extensions["m4a_t0"] = time.perf_counter()

        async def resp(r: httpx.Response) -> None:
            HttpLog._line(r)

        return {"request": [req], "response": [resp]}


def record(key: str, value: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    data.update({"platform": "macOS(Linux、Windows 未涵蓋)", key: value})
    RESULTS.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _files_containing(root: Path, needle: str) -> list[str]:
    data = needle.encode()
    return [str(p) for p in root.rglob("*") if p.is_file() and data in p.read_bytes()]


@pytest.fixture(scope="module", autouse=True)
def _fresh_artifacts():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for p in (HTTP_LOG, RESULTS):
        p.unlink(missing_ok=True)
    TOKENS.clear()
    yield
    assert HTTP_LOG.exists(), "沒有產生 http-log.jsonl"
    lines = [json.loads(ln) for ln in HTTP_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "http-log.jsonl 是空的"
    bad = [sorted(ln) for ln in lines if set(ln) != LOG_KEYS]
    assert not bad, bad
    assert all("?" not in str(ln["path"]) for ln in lines)
    assert RESULTS.exists(), "沒有產生 operation-results.json"
    assert TOKENS, "前置條件:至少產生過一個 token"
    leaked = sorted({f for s in [procs.TEST_KEY, *TOKENS] for f in _files_containing(ARTIFACTS, s)})
    assert not leaked, f"含假金鑰或 token 明文的產物:{leaked}"


def _safe(text: str) -> str:
    return redact_tokens(redact_tokens(text, TOKENS), procs.TEST_KEY)


# ---------------------------------------------------------------- 環境


class Node:
    """一個資料目錄 + 一個子行程 serve + 一個 httpx client。"""

    def __init__(self, env: "Env", name: str, *, seed=None, extra_env: dict[str, str] | None = None) -> None:
        self.env = env
        self.name = name
        self.home = env.tmp / name
        self.home.mkdir()
        if seed is not None:
            seed(self.home)               # serve 啟動前以 sqlite3 建庫(AM-2);token 由 serve 簽發
        else:
            self._init_token()
        self.serve = Serve(self.home, fake_bin=env.fake_bin, graceful_timeout=GRACE,
                           log_path=env.tmp / f"serve-{name}.log",
                           extra_env={**env.extra_env, **(extra_env or {})})
        env.serves.append(self.serve)
        self.serve.start()
        if seed is not None:
            assert self.serve.token, "serve 應簽發 token(黃金檔資料庫不含 token 雜湊)"
            self.token = self.serve.token
            TOKENS.append(self.token)
        self.http = self.serve.http(timeout=60, event_hooks=HttpLog.sync_hooks())
        env.clients.append(self.http)

    def _init_token(self) -> None:
        r = run_cli("init", home=self.home, fake_bin=self.env.fake_bin, extra_env=self.env.extra_env, timeout=120)
        self.token = r.stdout.strip()
        if self.token:
            TOKENS.append(self.token)
        rc, n_lines = r.returncode, len(r.stdout.splitlines())
        assert rc == 0, f"init 失敗 rc={rc}"
        assert self.token and n_lines == 1, f"init 的 stdout 應只有一行 token(實際 {n_lines} 行)"

    # -- HTTP
    def ok(self, r: httpx.Response, status: int = 200) -> Any:
        assert r.status_code == status, \
            f"{r.request.method} {r.request.url.path} → {r.status_code} {_safe(r.text)}"
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else r

    def export(self) -> bytes:
        r = self.http.get(EXPORT)
        assert r.status_code == 200, _safe(r.text)
        assert r.headers["content-type"] == "application/yaml; charset=utf-8"
        return r.content

    def import_(self, body: bytes | str | dict, mode: str, *, dry_run: bool = False) -> httpx.Response:
        if isinstance(body, dict):
            body = yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
        if isinstance(body, str):
            body = body.encode("utf-8")
        params = {"mode": mode, **({"dry_run": "true"} if dry_run else {})}
        return self.http.post(IMPORT, params=params, content=body, headers=YAML_CT)

    def dry_run_first(self, route: str) -> str:
        plan = self.ok(self.http.post(f"/api/routes/{route}/dry-run",
                                      json={"messages": [{"role": "user", "content": "x"}]}))
        return plan["slots"][0]["entries"][0]["deployment"]

    # -- DB(唯讀)
    @contextlib.contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with read_db(self.home) as c:
            c.execute("PRAGMA busy_timeout = 30000")
            yield c

    def dump(self) -> list[str]:
        with self.conn() as c:
            return list(c.iterdump())

    def config_rows(self) -> dict[str, list[tuple]]:
        with self.conn() as c:
            out = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in CONFIG_TABLES}
            out["settings"] = [tuple(r) for r in c.execute(
                "SELECT * FROM settings WHERE key != ? ORDER BY key", (ACCESS_TOKEN_KEY,))]
            return out

    def backups(self) -> list[Path]:
        d = self.home / "backups"
        return sorted(d.iterdir()) if d.exists() else []

    def ledger_total(self) -> int:
        return int(self.ok(self.http.get("/api/ledger", params={"page_size": 1}))["total"])


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.fake_bin = install(tmp / "fake-cli-bin")
        self.started = time.time()
        self.extra_env = {"SPILLWAY_TEST_KEY": procs.TEST_KEY}
        self.serves: list[Serve] = []
        self.clients: list[httpx.Client] = []
        self.fakes: list[FakeCli] = []
        envv = child_env(tmp, fake_bin=self.fake_bin)
        assert set(assert_only_fakes(envv["PATH"])) == {"claude", "codex"}
        self.fake_proc, self.fake_base = procs.start_fake_upstream(tmp)

    def node(self, name: str, **kw: Any) -> Node:
        n = Node(self, name, **kw)
        self.fakes.append(FakeCli(n.home, self.fake_bin))
        return n

    def close(self) -> None:
        for c in self.clients:
            c.close()
        errors: list[BaseException] = []
        for s in self.serves:              # stop():結束碼、stderr 沒有 Traceback/ERROR、停機沒有逾時
            try:
                s.stop()
            except BaseException as exc:  # noqa: BLE001 — 全部停完再回報
                s.kill()
                errors.append(exc)
        procs.stop(self.fake_proc)
        for fake in self.fakes:
            for rec in fake.records():
                if not rec.get("finished") and rec.get("started_at", 0) >= self.started \
                        and group_alive(rec["pid"]):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(rec["pid"], signal.SIGKILL)
            shutil.rmtree(fake.control_dir, ignore_errors=True)
        if errors:
            raise errors[0]

    def assert_all_stopped(self) -> None:
        assert all(s.returncode is not None for s in self.serves) and self.fake_proc.poll() is not None


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()
    e.assert_all_stopped()


def _seed(n: Node, fake_base: str) -> None:
    caps_api = {"ctx": 128000, "tools": True, "vision": False, "efforts": []}
    n.ok(n.http.post("/api/deployments", json={
        "id": API_A, "model_name": API_A, "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
        "params": {"api_base": fake_base + "/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
        "tags": [API_A], "capabilities": caps_api}), 201)
    n.ok(n.http.post("/api/deployments", json={
        "id": API_B, "model_name": API_B, "kind": "api", "provider": "openai", "model": "gpt-4.1-mini",
        "params": {"api_base": fake_base + "/v1"}, "tags": [API_B], "capabilities": caps_api}), 201)
    n.ok(n.http.post("/api/deployments", json={
        "id": CLAUDE, "model_name": CLAUDE, "kind": "cli", "provider": "claude-cli",
        "model": "claude-sonnet-5-5", "params": {"timeout": 60}, "tags": [CLAUDE],
        "capabilities": {"ctx": 272000, "tools": False, "vision": False, "efforts": ["low", "medium", "high"]},
        "budget": {"type": "none", "limit_amount": None, "window": "none"}}), 201)
    n.ok(n.http.post("/api/groups", json={
        "name": GROUP, "selector": {"all": [], "any": [API_A, API_B, CLAUDE], "none": []}}), 201)
    n.ok(n.http.post("/api/routes", json={"name": "review", "target": {"group": GROUP}, "mode": "task"}), 201)
    n.ok(n.http.put("/api/routes/review/order", json={"slots": [
        [{"deployment": CLAUDE}], [{"deployment": API_B, "weight": "high"}], [{"deployment": API_A}]]}))
    n.ok(n.http.post("/api/routes", json={
        "name": "chat-x", "mode": "chat", "target": {"tags": {"all": [], "any": [API_A], "none": []}}}), 201)


def _j(value: Any) -> str:
    """設定表的 JSON 欄位(data.md:`json.dumps(..., ensure_ascii=False, sort_keys=True)`)。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _seed_golden(home: Path) -> None:
    """serve 啟動前以 sqlite3 建出黃金檔對應的設定(AM-2 的例外;由凍結的 schema 黃金檔建表)。

    與 `tests/helpers/config_golden.seed_golden` 同一份內容,差別:不寫 token 雜湊(serve 簽發);冷卻、帳本、
    payload 改由 `_seed_golden_runtime` 經 `/__test__/*` 種入。"""
    schema = json.loads(SCHEMA_GOLDEN.read_text(encoding="utf-8"))
    ddl = [f"{sql};" for want in ("table", "index")                       # 先建表再建索引
           for kind, _name, _tbl, sql in schema["sqlite_master"] if sql and kind == want]
    deployments = [
        ("api-a", "gpt-4o-mini", "api", "openai", "gpt-4o-mini", {"api_base": "http://127.0.0.1:9/v1", "timeout": 30},
         "env:SPILLWAY_TEST_KEY", {"ctx": 128000, "efforts": ["low", "medium", "high"], "tools": True, "vision": False},
         0, ("coding", "review"), ("usd", 30.0, "monthly", 1)),
        ("api-b", "gpt-4.1-mini", "api", "openai", "gpt-4.1-mini", {}, "keyring:spillway:api-b", {}, 1,
         ("cheap", "coding"), ("credit_pool", 100.0, "weekly", None)),
        ("bare", "bare", "local", "hosted_vllm", "local-model", {}, None, {}, 1, (), None),
        ("claude-sub", "sonnet", "cli", "claude-cli", "claude-sonnet-4-5",
         {"cwd": "/tmp/work", "extra_args": ["--verbose"], "nested": {"empty": {}}}, None, {}, 1, ("coding",),
         ("none", None, "none", None)),
        ("qwen-local", "qwen-coder", "local", "ollama", "qwen3-coder:30b", {"api_base": "http://127.0.0.1:11434"},
         None, {"ctx": 32768, "efforts": []}, 1, ("coding", "private-ok"), None),
    ]
    groups = [("coding", {"all": ["coding"], "any": [], "none": []}),
              ("mixed", {"all": ["coding"], "any": ["review", "cheap"], "none": ["private-ok"]}),
              ("中文群組", {"all": [], "any": [], "none": []})]
    routes = [("review", {"group": "coding"}, "task", ["daily", "alpha", "daily"], 1, 1),
              ("quick", {"tags": {"all": ["coding"], "any": [], "none": ["review"]}}, "chat", [], None, 0),
              ("silent", {"tags": {"all": [], "any": [], "none": []}}, "chat", [], 0, 0)]
    targets = [("review", "claude-sub", 1, 0, "high", "medium"), ("review", "api-a", 2, 0, "medium", "low"),
               ("review", "api-b", 2, 1, "none", "high"), ("review", "qwen-local", 3, 0, None, "low")]
    path = build_db(home, "\n".join(ddl), user_version=schema["user_version"])
    c = sqlite3.connect(path)   # serve 啟動前(AM-2);之後一律唯讀
    try:
        with c:
            c.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", [
                ("record_context_default", "0"), ("context_retention_days", "30"), ("context_max_bytes", "1048576")])
            for dep_id, model_name, kind, provider, model, params, cred, caps, enabled, tags, budget in deployments:
                c.execute("INSERT INTO deployments (id, model_name, kind, provider, model, params, credential_ref, "
                          "capabilities, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (dep_id, model_name, kind, provider, model, _j(params), cred, _j(caps), enabled))
                c.executemany("INSERT INTO deployment_tags (deployment_id, tag) VALUES (?, ?)",
                              [(dep_id, t) for t in sorted(set(tags))])
                if budget is not None:
                    c.execute("INSERT INTO budgets (deployment_id, type, limit_amount, window, reset_anchor) "
                              "VALUES (?, ?, ?, ?, ?)", (dep_id, *budget))
            c.executemany("INSERT INTO groups (name, selector) VALUES (?, ?)", [(n, _j(sel)) for n, sel in groups])
            c.executemany("INSERT INTO routes (name, target, mode, tags, record_context, budget_precheck) "
                          "VALUES (?, ?, ?, ?, ?, ?)",
                          [(n, _j(t), m, _j(tags), rc, bp) for n, t, m, tags, rc, bp in routes])
            c.executemany("INSERT INTO route_targets (route_name, deployment_id, slot, position, effort, weight) "
                          "VALUES (?, ?, ?, ?, ?, ?)", targets)
    finally:
        c.close()


def _seed_golden_runtime(n: Node) -> None:
    """不可出現在匯出檔的執行期狀態(冷卻、帳本、payload),經 `/__test__/*` 種入(spec 13.4.3、13.4.4)。"""
    ctl = n.serve.control
    ctl.cooldown("api-a", GOLDEN_NOW, "rate_limited")
    (lid,) = ctl.ledger([{"ts": GOLDEN_NOW, "status": "ok", "deployment_id": "api-a", "error": "ledger-only-text"}])
    ctl.payloads([{"ledger_id": lid, "ts": GOLDEN_NOW, "request_json": '"payload-only-text"', "bytes": 19}])


def _chat(n: Node, model: str, *, task: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
    if task:
        body["metadata"] = {"mode": "task"}
    return n.ok(n.http.post("/v1/chat/completions", json=body))


def _doc(text: bytes) -> dict[str, Any]:
    return yaml.safe_load(text.decode("utf-8"))


def _err(r: httpx.Response) -> tuple[int, str, str]:
    e = r.json()["error"]
    return r.status_code, e["code"], e["message"]


# ---------------------------------------------------------------- 1. 設定匯出入、CSV、總覽


def test_m4a_operation_config(env: Env):
    obs: dict[str, Any] = {}
    s1 = env.node("home-1")
    _seed(s1, env.fake_base)

    # ---- 帳本:3 次請求(2 次 task → review、1 次 chat → chat-x),一列 error 改為 =1+1 開頭
    served = [_chat(s1, "review", task=True)["model"] for _ in range(2)]
    _chat(s1, "chat-x", task=False)
    rows = s1.ok(s1.http.get("/api/ledger", params={"page_size": 500}))["items"]
    obs["ledger"] = {"total": len(rows), "by_route": sorted((r["route_name"], r["deployment_id"], r["status"])
                                                           for r in rows)}
    assert len(rows) == 3 and [r["status"] for r in rows] == ["ok"] * 3, obs["ledger"]
    review_ids = sorted(r["id"] for r in rows if r["route_name"] == "review")
    assert len(review_ids) == 2 and any(r["deployment_id"] == API_A for r in rows), (rows, served)
    with s1.conn() as c:                 # 讀出整列,只改 error 後經 `/__test__/ledger` upsert(整列取代)
        c.row_factory = sqlite3.Row
        full = dict(c.execute("SELECT * FROM usage_ledger WHERE id = ?", (review_ids[0],)).fetchone())
    assert s1.serve.control.ledger([{**full, "error": FORMULA_ERROR}]) == [review_ids[0]]
    with s1.conn() as c:
        c.row_factory = sqlite3.Row
        assert dict(c.execute("SELECT * FROM usage_ledger WHERE id = ?", (review_ids[0],)).fetchone()) == \
            {**full, "error": FORMULA_ERROR}

    # ---- 匯出 A;serve 2 以 replace 匯入 A → 匯出 B
    a = s1.export()
    (env.tmp / "A.yaml").write_bytes(a)
    doc_a = _doc(a)
    obs["export_a"] = {"deployments": [d["id"] for d in doc_a["deployments"]], "groups": sorted(doc_a["groups"]),
                       "routes": sorted(doc_a["routes"]),
                       "api_a_credential_ref": next(d for d in doc_a["deployments"]
                                                    if d["id"] == API_A)["credential_ref"]}
    assert obs["export_a"]["api_a_credential_ref"] == "env:SPILLWAY_TEST_KEY"
    s2 = env.node("home-2")
    r = s2.import_(a, "replace")
    obs["import_a_on_serve2"] = {"status": r.status_code, **{k: r.json()[k] for k in ("created", "backup")}}
    assert r.status_code == 200, _safe(r.text)
    b = s2.export()
    (env.tmp / "B.yaml").write_bytes(b)
    obs["a_equals_b"] = a == b
    obs["dry_run_first"] = {"serve1": s1.dry_run_first("review"), "serve2": s2.dry_run_first("review")}
    assert obs["a_equals_b"], "A 與 B 不同"
    assert obs["dry_run_first"]["serve1"] == obs["dry_run_first"]["serve2"] == CLAUDE, obs["dry_run_first"]

    # ---- 黃金檔往返(決策 8):serve 3(種入黃金檔)→ serve 4(空)
    s3 = env.node("home-3", seed=_seed_golden)
    _seed_golden_runtime(s3)
    g = s3.export()
    s4 = env.node("home-4")
    r = s4.import_(g, "replace")
    assert r.status_code == 200, _safe(r.text)
    g2 = s4.export()
    obs["golden_round_trip"] = {"export_equals_golden": g == GOLDEN.read_bytes(), "re_export_equal": g == g2,
                                "config_tables_snapshot_equal": s3.config_rows() == s4.config_rows()}
    assert all(obs["golden_round_trip"].values()), obs["golden_round_trip"]

    # ---- merge:由現有 api-b 匯出後只改 tags
    before = s1.config_rows()
    dep_b = dict(next(d for d in _doc(s1.export())["deployments"] if d["id"] == API_B))
    dep_b["tags"] = [API_B, "cheap"]
    r = s1.import_({"version": 1, "deployments": [dep_b]}, "merge")
    assert r.status_code == 200, _safe(r.text)
    after = s1.config_rows()
    diff = {t for t in after if after[t] != before[t]}
    obs["merge"] = {"status": r.status_code, "updated": r.json()["updated"], "changed_tables": sorted(diff),
                    "api_b_tags": s1.ok(s1.http.get(f"/api/deployments/{API_B}"))["tags"]}
    assert diff == {"deployment_tags"}, diff
    changed = set(after["deployment_tags"]) ^ set(before["deployment_tags"])
    assert changed == {(API_B, "cheap")}, changed

    # ---- dry_run:改 api-b 的 params.api_base
    doc = _doc(s1.export())
    next(d for d in doc["deployments"] if d["id"] == API_B)["params"]["api_base"] = "http://127.0.0.1:9/v1"
    dump0, backups0 = s1.dump(), s1.backups()
    r = s1.import_(doc, "replace", dry_run=True)
    assert r.status_code == 200, _safe(r.text)
    body = r.json()
    obs["dry_run"] = {"dry_run": body["dry_run"], "backup": body["backup"], "privileged": body["privileged"],
                      "db_unchanged": s1.dump() == dump0, "backups_unchanged": s1.backups() == backups0}
    assert body["dry_run"] is True and body["backup"] is None and body["privileged"] == [API_B], body
    assert obs["dry_run"]["db_unchanged"] and obs["dry_run"]["backups_unchanged"], obs["dry_run"]

    # ---- replace:縮小後的檔案(移除 api-b)與備份
    pre = s1.export()
    ledger0 = s1.ledger_total()
    doc = _doc(pre)
    doc["deployments"] = [d for d in doc["deployments"] if d["id"] != API_B]
    doc["routes"]["review"]["targets"] = [[e for e in slot if e["deployment"] != API_B]
                                          for slot in doc["routes"]["review"]["targets"]]
    doc["routes"]["review"]["targets"] = [s for s in doc["routes"]["review"]["targets"] if s]
    r = s1.import_(doc, "replace")
    assert r.status_code == 200, _safe(r.text)
    body = r.json()
    new = [p for p in s1.backups() if p not in backups0]
    obs["replace"] = {"deleted": body["deleted"], "backup": body["backup"],
                      "backup_files": [p.name for p in new],
                      "backup_mode": oct(new[0].stat().st_mode & 0o777) if new else None,
                      "backup_equals_pre_export": bool(new) and new[0].read_bytes() == pre,
                      "ledger_unchanged": s1.ledger_total() == ledger0,
                      "api_b_gone": s1.http.get(f"/api/deployments/{API_B}").status_code == 404}
    assert len(new) == 1 and body["backup"] == new[0].name, obs["replace"]
    assert obs["replace"]["backup_mode"] == "0o600" and obs["replace"]["backup_equals_pre_export"], obs["replace"]
    assert obs["replace"]["ledger_unchanged"] and obs["replace"]["api_b_gone"], obs["replace"]
    assert body["deleted"]["deployments"] == 1, body

    # ---- 不合法檔案:第二個成員的 kind 為 bogus
    dump0 = s1.dump()
    doc = _doc(s1.export())
    doc["deployments"][1]["kind"] = "bogus"
    r = s1.import_(doc, "replace")
    status, code, msg = _err(r)
    obs["invalid"] = {"status": status, "code": code, "has_path": "deployments[1].kind" in msg,
                      "db_unchanged": s1.dump() == dump0}
    assert (status, code) == (422, "validation_error") and obs["invalid"]["has_path"], _safe(msg)
    assert obs["invalid"]["db_unchanged"]

    # ---- 兩個 claude-cli
    two = [{"id": i, "model_name": i, "kind": "cli", "provider": "claude-cli", "model": "claude-sonnet-5-5"}
           for i in ("claude-x", "claude-y")]
    r = s1.import_({"version": 1, "deployments": two}, "merge")
    status, code, _msg = _err(r)
    obs["two_claude_cli"] = {"status": status, "code": code, "db_unchanged": s1.dump() == dump0}
    assert (status, code) == (409, "provider_single") and obs["two_claude_cli"]["db_unchanged"], obs

    # ---- 帳本 CSV(route 篩選)
    r = s1.http.get("/api/ledger/export.csv", params={"route": "review"})
    assert r.status_code == 200, _safe(r.text)
    (env.tmp / "ledger.csv").write_bytes(r.content)
    text = r.content.decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(text)))
    header, data = table[0], table[1:]
    err_col, id_col = header.index("error"), header.index("id")
    formula = next(row for row in data if row[id_col] == str(review_ids[0]))
    listed = s1.ok(s1.http.get("/api/ledger", params={"route": "review", "page_size": 500}))
    obs["csv"] = {"rows": len(data), "ledger_filter_total": listed["total"], "bom": r.content.startswith(b"\xef\xbb\xbf"),
                  "no_prompt": PROMPT not in text, "formula_cell": formula[err_col][:6],
                  "content_type": r.headers["content-type"]}
    assert len(data) == listed["total"] == 2 and obs["csv"]["bom"] and obs["csv"]["no_prompt"], obs["csv"]
    assert formula[err_col] == "'" + FORMULA_ERROR, formula

    # ---- 設定總覽
    r = s1.http.get("/api/settings")
    ov = s1.ok(r)
    obs["overview"] = {k: ov[k] for k in ("port", "token_issued", "first_run", "counts")}
    obs["overview"]["data_dir_matches"] = ov["data_dir"] == str(s1.home)
    obs["overview"]["token_free"] = all(t not in r.text for t in TOKENS)
    assert ov["port"] == s1.serve.port and ov["token_issued"] is True and ov["first_run"] is False, obs
    assert obs["overview"]["data_dir_matches"] and obs["overview"]["token_free"], obs["overview"]

    # ---- 群組名稱 `..`
    r = s1.http.post("/api/groups", json={"name": "..", "selector": {"all": [], "any": [API_A], "none": []}})
    obs["group_dotdot"] = _err(r)[:2]
    assert obs["group_dotdot"] == (422, "validation_error")

    # ---- NFR-4:資料庫、匯出檔、備份、CSV、serve 日誌以假金鑰與 token 搜尋
    roots = [n.home for n in (s1, s2, s3, s4)]
    files = [env.tmp / "A.yaml", env.tmp / "B.yaml", env.tmp / "ledger.csv", *(s.log_path for s in env.serves)]
    for root in roots:
        files += [*root.glob("spillway.db*"), *(root / "backups").glob("*")]
    assert any(p.name == "spillway.db" for p in files) and any(p.parent.name == "backups" for p in files)
    hits = {name: sorted(str(p.relative_to(env.tmp)) for p in files if p.is_file() and s.encode() in p.read_bytes())
            for name, s in [("fake_key", procs.TEST_KEY)] + [(f"token{i}", t) for i, t in enumerate(TOKENS)]}
    obs["nfr4"] = {"files_scanned": len(files), "hits": hits}
    assert all(v == [] for v in hits.values()), hits

    # ---- 只聽 127.0.0.1
    obs["listening"] = {n.name: procs.listening(n.serve.port) for n in (s1, s2, s3, s4)}
    assert all(v == [f"127.0.0.1:{n.serve.port}"] for n, v in zip((s1, s2, s3, s4), obs["listening"].values()))
    record("config", obs)


# ---------------------------------------------------------------- 2. mcp-stdio 對 SDK 503


async def test_m4a_operation_mcp_stdio_max_sessions(env: Env):
    s = env.node("home-mcp", extra_env={"SPILLWAY_TEST_MCP_MAX_SESSIONS": "1"})
    await asyncio.to_thread(_seed, s, env.fake_base)
    async with mcp_http_session(s.serve.mcp_url(GROUP), s.token,
                                event_hooks=HttpLog.async_hooks()) as (sess, _init, _sid):
        tools = [t.name for t in (await sess.list_tools()).tools]
        out = await stdio_session(group=GROUP, port=s.serve.port, token=s.token,
                                  bridge_home=env.tmp / "bridge-home", fake_bin=env.fake_bin)
    obs = {"first_session_tools": tools,
           "second_error_data": out.error.data if out.error else None,
           "second_init": out.init is not None, "returncode": out.returncode,
           "stderr_token_free": all(t not in out.stderr for t in TOKENS),
           "bridge_created_data_dir": (env.tmp / "bridge-home").exists()}
    assert tools == ["ask", "list_routes", "quota_status"], tools
    assert out.init is None and out.error is not None, (out.error, out.returncode)
    assert out.error.data == {"http_status": 503}, out.error
    assert out.returncode == 1 and obs["stderr_token_free"], (out.returncode, _safe(out.stderr))
    log = s.serve.stderr_bytes().decode("utf-8", errors="replace")   # 原始 stderr(未遮罩)
    token_in_log = any(t in log for t in TOKENS)
    key_in_log = procs.TEST_KEY in log
    del log
    assert not token_in_log and not key_in_log, \
        f"serve 日誌含 token 明文={token_in_log}、假金鑰={key_in_log}"
    record("mcp_stdio_max_sessions", obs)
