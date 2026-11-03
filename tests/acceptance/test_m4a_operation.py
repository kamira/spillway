"""M4a 操作驗收的自動部分(CHG-20261102-02「### Acceptance operation」;T6;macOS)。

拆成 2 個測試函式:
1. `test_m4a_operation_config`:設定匯出入、帳本 CSV、設定總覽、群組名稱規則與 NFR-4 搜尋。
   - 子行程假 API 上游(`python -m tests.fakes.api_upstream`)與假 CLI(PATH 只含假 claude/codex);
   - 每個暫存資料目錄先以子行程 `python -m spillway init` 產生 token(serve 看到已有雜湊就不印;
     明文只存在測試行程記憶體),再起子行程 `python -m spillway serve --port <隨機>`;
   - serve 1:以管理 API 建立 api-a(`credential_ref: env:SPILLWAY_TEST_KEY`,子行程環境帶假值)、
     api-b(無憑證)、claude-sub(claude-cli,假 CLI)、群組 `coding`、task route `review`(手動順序)
     與 chat route `chat-x`;送 3 次請求產生帳本(2 次 task 到 review、1 次 chat 到 chat-x)。
     「其中一列 `error` 以 `=1+1` 開頭」:假上游與假 CLI 的錯誤訊息是固定文字,無法由請求帶出,所以
     由測試行程以 SQL 把 review 的一列 `error` 改為 `=1+1 …`(只改該欄;其餘列不動)。
   - 匯出 A → serve 2(空資料目錄)以 replace 匯入 A → 匯出 B;黃金檔(決策 8)serve 3 → serve 4 往返
     (DB 比對為設定表快照:只比 `CONFIG_TABLES` 與 token 雜湊以外的 settings,不含帳本、payload、冷卻);
     serve 1 上 merge、dry_run、replace(縮小)、不合法檔案、兩個 claude-cli、CSV、總覽、`..` 群組。
2. `test_m4a_operation_mcp_stdio_max_sessions`:`SPILLWAY_TEST_MCP_MAX_SESSIONS=1` 的 serve 上,第一個
   MCP session(官方 SDK Streamable HTTP)保持開啟,第二個以子行程 `mcp-stdio`(官方 SDK stdio 用戶端)
   initialize → 錯誤 `data == {"http_status": 503}`、結束碼 1。

產物(`artifacts/m4a/`,不進版控):
- `http-log.jsonl`:測試行程對閘道的每一筆 HTTP 請求一行,只有 `method`、`path`(不含 query)、`status`、
  `elapsed_ms` 四鍵(httpx event hook);不記 header 與 body。
- `operation-results.json`:各函式的觀察值(不含任何秘密)。
模組結束時以假金鑰與全部 token 明文搜尋 `artifacts/m4a/` 每個檔案,命中須為 0。
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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from spillway import settings
from tests.acceptance import procs
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install
from tests.features.support.mcp_proc import (
    ServeProc,
    child_env,
    group_alive,
    mcp_http_session,
    stdio_session,
)
from tests.helpers import redact_tokens
from tests.helpers.config_golden import GOLDEN, seed_golden

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
            from spillway.store.db import connect
            conn = connect(self.home / "spillway.db")
            try:
                seed(conn)
            finally:
                conn.close()
        else:
            self._init_token()
        self.serve = ServeProc(self.home, fake_bin=env.fake_bin, graceful=GRACE,
                               log_path=env.tmp / f"serve-{name}.log",
                               extra_env={**env.extra_env, **(extra_env or {})}, secrets=TOKENS)
        env.serves.append(self.serve)
        self.serve.wait_ready()
        self.http = httpx.Client(base_url=self.serve.url, trust_env=False, timeout=60,
                                 event_hooks=HttpLog.sync_hooks())
        env.clients.append(self.http)

    def _init_token(self) -> None:
        envv = child_env(self.home, fake_bin=self.env.fake_bin, extra=self.env.extra_env)
        r = subprocess.run([sys.executable, "-m", "spillway", "init"], env=envv, cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=120)
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

    # -- DB
    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.home / "spillway.db"), timeout=30)
        c.execute("PRAGMA busy_timeout = 30000")
        return c

    def dump(self) -> list[str]:
        c = self.conn()
        try:
            return list(c.iterdump())
        finally:
            c.close()

    def config_rows(self) -> dict[str, list[tuple]]:
        c = self.conn()
        try:
            out = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in CONFIG_TABLES}
            out["settings"] = [tuple(r) for r in c.execute(
                "SELECT * FROM settings WHERE key != ? ORDER BY key", (settings.ACCESS_TOKEN_KEY,))]
            return out
        finally:
            c.close()

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
        self.serves: list[ServeProc] = []
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
        for s in self.serves:
            s.stop()
        procs.stop(self.fake_proc)
        for fake in self.fakes:
            for rec in fake.records():
                if not rec.get("finished") and rec.get("started_at", 0) >= self.started \
                        and group_alive(rec["pid"]):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(rec["pid"], signal.SIGKILL)
            shutil.rmtree(fake.control_dir, ignore_errors=True)

    def assert_all_stopped(self) -> None:
        assert all(s.proc.poll() is not None for s in self.serves) and self.fake_proc.poll() is not None


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
    c = s1.conn()
    try:
        with c:
            c.execute("UPDATE usage_ledger SET error = ? WHERE id = ?", (FORMULA_ERROR, review_ids[0]))
    finally:
        c.close()

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
    s3 = env.node("home-3", seed=seed_golden)
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
    obs["listening"] = {n.name: procs.listening(n.serve.proc.pid) for n in (s1, s2, s3, s4)}
    assert all(v == [f"127.0.0.1:{n.serve.port}"] for n, v in zip((s1, s2, s3, s4), obs["listening"].values()))
    record("config", obs)


# ---------------------------------------------------------------- 2. mcp-stdio 對 SDK 503


async def test_m4a_operation_mcp_stdio_max_sessions(env: Env):
    s = env.node("home-mcp", extra_env={"SPILLWAY_TEST_MCP_MAX_SESSIONS": "1"})
    await asyncio.to_thread(_seed, s, env.fake_base)
    async with mcp_http_session(f"{s.serve.url}/mcp/{GROUP}", s.token,
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
    log = s.serve.log_text()
    token_in_log = any(t in log for t in TOKENS)
    key_in_log = procs.TEST_KEY in log
    del log
    assert not token_in_log and not key_in_log, \
        f"serve 日誌含 token 明文={token_in_log}、假金鑰={key_in_log}"
    record("mcp_stdio_max_sessions", obs)
