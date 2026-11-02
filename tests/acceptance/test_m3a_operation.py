"""L4 操作驗收的自動部分(CHG-20261101-03「### Acceptance operation」;T9;macOS)。

operate:
- 暫存 `SPILLWAY_HOME`,放入一份 v1 測試資料庫(DDL 取自 `git show d89118c:src/spillway/store/schema.sql`,
  取不到時用套件內 `schema.sql`,兩者逐字相同由測試斷言);內含一個冷卻中的 api 成員(帳本最近一筆
  `rate_limited`)與一個決策 59 暫行列(`type=none, window=none, limit_amount=NULL`)。
- 每個子行程的環境一律經 `tests.fakes.cli_upstream.safe_subprocess_env`(PATH 清理、假 CLI 目錄在最前面、
  斷言找不到真實 claude/codex);serve 的環境另放入決策 9 清單內全部變數的假值。
- `python -m spillway init`(stdout 只印 token)→ `python -m spillway serve` + 假 API 上游子行程。
- 管理 API 建 claude-sub(claude-cli,credit_pool 10 美元每月,`max_concurrency` 1)、codex-sub(codex-cli,
  budget none)、deepseek-api(api)與 task route `review`;10 次 task 請求(預設順序);把 deepseek-api
  排到 slot 1 再 10 次;建第二個 claude-cli 成員;控制檔讓假 claude 停住並送 2 個 task 請求;假 claude
  回額度用完;codex route;chat route;dry-run;rotate token;`lsof`;最後在假 codex 停住時對 serve 送 SIGTERM
  並計時(停機上限以測試用變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT` 設為 `GRACEFUL` 秒,縮短測試時間;
  預設 30 秒由 `tests/integration/test_graceful_shutdown.py` L1 驗證)。

observe:見 CHG;逐項斷言,結果(不含任何秘密)寫入 `artifacts/m3a/operation-results.json`。
產物:`artifacts/m3a/http-log.jsonl`——每行一筆對閘道的 HTTP 請求,只含 method、path(不含 query)、
status、elapsed_ms;不記 body。測試結束(含失敗)終止全部子行程並刪除假 CLI 控制目錄。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway import cli
from spillway.store import db as store_db
from tests.acceptance import procs
from tests.helpers import redact_tokens
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install, safe_subprocess_env

pytestmark = pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)")

REPO_ROOT = procs.REPO_ROOT
ARTIFACTS = REPO_ROOT / "artifacts" / "m3a"
HTTP_LOG = ARTIFACTS / "http-log.jsonl"
RESULTS = ARTIFACTS / "operation-results.json"
V1_COMMIT = "d89118c"
GRACEFUL = 5.0          # 測試用停機上限(秒)
TERM_GRACE = 5.0        # CliRunner SIGTERM→SIGKILL
SLACK = 5.0
CLAUDE_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
CODEX_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")
CLAUDE, CODEX, API = "claude-sub", "codex-sub", "deepseek-api"
LEGACY_COOL, LEGACY_PLACEHOLDER = "legacy-cool", "legacy-placeholder"
MESSAGES = [{"role": "user", "content": "請檢查這段程式碼"}]


# ---------------------------------------------------------------- 輔助


class LoggedClient:
    """對閘道的 httpx client;每次請求只記 method、path、status、elapsed_ms(不記 body)。"""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=60)
        self.lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        t0 = time.perf_counter()
        r = self.client.request(method, path, **kw)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        with self._lock:
            self.lines.append({"method": method, "path": r.request.url.path, "status": r.status_code,
                               "elapsed_ms": ms})
        return r

    def chat(self, model: str, *, task: bool = True) -> httpx.Response:
        body: dict[str, Any] = {"model": model, "messages": MESSAGES}
        if task:
            body["metadata"] = {"mode": "task"}
        return self.request("POST", "/v1/chat/completions", json=body)

    def ok(self, r: httpx.Response, status: int = 200) -> Any:
        assert r.status_code == status, f"{r.request.method} {r.request.url.path} → {r.status_code} {r.text}"
        return r.json() if r.content else None

    def ledger(self, **params: Any) -> list[dict[str, Any]]:
        return self.ok(self.request("GET", "/api/ledger", params={"page_size": 500, **params}))["items"]

    def write_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(ln, ensure_ascii=False) + "\n" for ln in self.lines),
                        encoding="utf-8")

    def close(self) -> None:
        self.client.close()


def _v1_schema() -> tuple[str, str]:
    """v1 DDL:`git show d89118c:src/spillway/store/schema.sql`(與套件內 schema.sql 必須相同)。"""
    packaged = store_db.MIGRATIONS[0][1]
    r = subprocess.run(["git", "show", f"{V1_COMMIT}:src/spillway/store/schema.sql"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    if r.returncode == 0:
        assert r.stdout == packaged, "d89118c 的 schema.sql 與套件內 v1 DDL 不同"
        return r.stdout, f"git show {V1_COMMIT}"
    return packaged, "package schema.sql(git 不可用)"


def _make_v1_db(path: Path) -> str:
    sql, source = _v1_schema()
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        c.execute("BEGIN")
        for stmt in store_db._split_statements(sql):
            c.execute(stmt)
        c.execute("PRAGMA user_version = 1")
        for dep in (LEGACY_COOL, LEGACY_PLACEHOLDER):
            c.execute("INSERT INTO deployments(id, model_name, kind, provider, model) "
                      "VALUES (?, ?, 'api', 'openai', 'gpt-4o-mini')", (dep, dep))
            c.execute("INSERT INTO deployment_tags(deployment_id, tag) VALUES (?, 'legacy')", (dep,))
        c.executemany(
            "INSERT INTO budgets(deployment_id, type, limit_amount, window, reset_anchor, exhausted_until) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(LEGACY_COOL, "usd", 5.0, "daily", None, "2099-01-01T00:00:00Z"),
             (LEGACY_PLACEHOLDER, "none", None, "none", None, "2026-10-30T13:00:00Z")])   # 決策 59 暫行列
        c.executemany(
            "INSERT INTO usage_ledger(ts, deployment_id, status) VALUES (?, ?, ?)",
            [("2026-10-30T11:00:00Z", LEGACY_COOL, "quota_exhausted"),
             ("2026-10-30T12:00:00Z", LEGACY_COOL, "rate_limited")])   # 最近一筆 → rate_limited
        c.execute("COMMIT")
    finally:
        c.close()
    return source


def _q(path: Path, sql: str, *args: Any) -> list[tuple]:
    c = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _cli_member(dep_id: str, provider: str, model: str, budget: dict[str, Any]) -> dict[str, Any]:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": provider, "model": model,
            "params": {"max_concurrency": 1, "timeout": 60}, "tags": ["m3a", dep_id],
            "capabilities": {"ctx": 272000, "tools": False, "vision": False,
                             "efforts": ["low", "medium", "high"]},
            "budget": budget}


def _api_member(fake_base: str) -> dict[str, Any]:
    return {"id": API, "model_name": API, "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
            "params": {"api_base": fake_base + "/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
            "tags": ["m3a", API],
            "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}}


def _order(gw: LoggedClient, route: str, ids: list[str]) -> None:
    gw.ok(gw.request("PUT", f"/api/routes/{route}/order",
                     json={"slots": [[{"deployment": d}] for d in ids]}))


def _route(gw: LoggedClient, name: str, ids: list[str], mode: str) -> None:
    gw.ok(gw.request("POST", "/api/routes", json={
        "name": name, "mode": mode, "target": {"tags": {"all": [], "any": ids, "none": []}}}), 201)


def _served(resps: list[httpx.Response]) -> list[str | None]:
    assert all(r.status_code == 200 for r in resps), [(r.status_code, r.text) for r in resps]
    return [r.headers.get("X-Spillway-Served-By") for r in resps]


def _start_request(gw: LoggedClient, model: str) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["resp"] = gw.chat(model)
        except httpx.HTTPError as exc:
            box["exc"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    box["thread"] = t
    return box


def _group_alive(pgid: int) -> bool:
    r = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def _next_monthly_reset(now: datetime) -> str:
    """credit_pool 每月、reset_anchor 1 的下次重置(serve 用真實 UTC 時鐘;spec 4.2.6)。"""
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"{y:04d}-{m:02d}-01T00:00:00Z"


def _unfinished(fake: FakeCli, since: float) -> list[dict[str, Any]]:
    """本次測試啟動(`started_at >= since`)且尚未寫入結束記錄的假 CLI 呼叫。

    已結束的記錄其 pid 可能已被系統重用,一律不檢查、不送訊號。"""
    return [r for r in fake.records() if not r.get("finished") and r.get("started_at", 0) >= since]


def _files_containing(root: Path, needle: str) -> list[str]:
    data = needle.encode()
    return [str(p) for p in root.rglob("*") if p.is_file() and data in p.read_bytes()]


# ---------------------------------------------------------------- 驗收


def test_seed_has_two_cli_members(tmp_path):
    """NFR-1/2 種子(T9):100 個成員中有 2 個 cli 成員(claude-cli、codex-cli)。"""
    from datetime import datetime, timezone

    from spillway.store import repo
    from spillway.store.db import connect
    from tests.acceptance.seed import seed_nfr_dataset

    conn = connect(tmp_path / "seed.db")
    try:
        ds = seed_nfr_dataset(conn, datetime(2026, 11, 15, tzinfo=timezone.utc), "http://127.0.0.1:9")
        deps = repo.list_deployments(conn)
        n_codex_cost = conn.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE deployment_id = ? AND cost_usd IS NOT NULL",
            (ds.cli_ids[1],)).fetchone()[0]
    finally:
        conn.close()
    assert len(deps) == 100 and len(ds.local_ids) == 10
    cli = sorted((d.id, d.provider) for d in deps if d.kind == "cli")
    assert cli == [("m000", "claude-cli"), ("m001", "codex-cli")]
    assert ds.cli_ids == ("m000", "m001")
    assert n_codex_cost == 0


def test_m3a_acceptance_operation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    obs: dict[str, Any] = {"platform": "macOS(Linux、Windows 未涵蓋)"}
    db_path = home / "spillway.db"
    obs["v1_schema_source"] = _make_v1_db(db_path)
    assert _q(db_path, "PRAGMA user_version") == [(1,)]

    fake = FakeCli(home, install(tmp_path / "fake-cli-bin"))
    run_started = time.time()
    base = procs.child_env(home)
    for var in (*CLAUDE_VARS, *CODEX_VARS):
        base[var] = f"fake-{var.lower()}-m3a"           # 決策 9 清單的假值:CLI 子行程不可看到
    env = safe_subprocess_env(base, fake_bin=fake.bin_dir)
    env[cli.TEST_GRACEFUL_TIMEOUT_ENV] = str(GRACEFUL)
    assert env["SPILLWAY_TEST_KEY"]                     # deepseek-api 的 env:SPILLWAY_TEST_KEY;CLI 子行程不可看到
    resolved = assert_only_fakes(env["PATH"])
    assert resolved == {"claude": str(fake.path("claude")), "codex": str(fake.path("codex"))}

    # ---- init(子行程):migration + 備份 + token(stdout 只印 token)
    r = subprocess.run([sys.executable, "-m", "spillway", "init"], env=env, cwd=str(REPO_ROOT),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, redact_tokens(r.stderr, [r.stdout.strip()])
    token = r.stdout.strip()
    assert token and r.stdout.count(token) == 1 and len(r.stdout.splitlines()) == 1
    assert token not in r.stderr
    backups = sorted(p.name for p in home.glob("spillway.db.v1.bak*"))
    cooldowns = dict((d, (u, why)) for d, u, why in _q(
        db_path, "SELECT deployment_id, exhausted_until, reason FROM cooldowns"))
    budget_cols = [row[1] for row in _q(db_path, "PRAGMA table_info(budgets)")]
    placeholder = _q(db_path, "SELECT type, limit_amount, window, reset_anchor FROM budgets "
                              "WHERE deployment_id = ?", LEGACY_PLACEHOLDER)
    obs["migration"] = {"backups": backups, "user_version": _q(db_path, "PRAGMA user_version")[0][0],
                        "backup_user_version": _q(home / "spillway.db.v1.bak", "PRAGMA user_version")[0][0],
                        "cooldowns": cooldowns, "placeholder_row": placeholder,
                        "budgets_has_exhausted_until": "exhausted_until" in budget_cols}
    assert backups == ["spillway.db.v1.bak"]
    assert obs["migration"]["user_version"] == 2 and obs["migration"]["backup_user_version"] == 1
    assert cooldowns == {LEGACY_COOL: ("2099-01-01T00:00:00Z", "rate_limited"),
                         LEGACY_PLACEHOLDER: ("2026-10-30T13:00:00Z", "quota_exhausted")}
    assert placeholder == [("none", None, "none", None)]
    assert "exhausted_until" not in budget_cols
    assert _q(db_path, "PRAGMA foreign_key_check") == []
    assert (home / "work" / "claude").is_dir() and (home / "work" / "codex").is_dir()
    stored_hash = _q(db_path, "SELECT value FROM settings WHERE key = 'access_token_sha256'")[0][0]
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()
    obs["init"] = {"stdout_lines": len(r.stdout.splitlines()), "token_printed_once": True}

    port = procs.free_port()
    fake_proc, fake_base = procs.start_fake_upstream(tmp_path)
    serve_log = tmp_path / "serve.log"
    serve: subprocess.Popen | None = None
    gw: LoggedClient | None = None
    try:
        with open(serve_log, "w", encoding="utf-8") as log:
            serve = subprocess.Popen(
                [sys.executable, "-m", "spillway", "serve", "--port", str(port)],
                env=env, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, text=True,
                start_new_session=True)
        procs.wait_listening(serve)
        gw = LoggedClient(f"http://127.0.0.1:{port}")
        deps = gw.ok(gw.request("GET", "/api/deployments"))["deployments"]
        legacy = {d["id"]: d for d in deps}
        assert legacy[LEGACY_COOL]["cooling_until"] == "2099-01-01T00:00:00Z"
        assert legacy[LEGACY_COOL]["cooling_reason"] == "rate_limited"
        assert legacy[LEGACY_PLACEHOLDER]["cooling_until"] is None     # 已到期

        # ---- 成員與 route
        gw.ok(gw.request("POST", "/api/deployments", json=_cli_member(
            CLAUDE, "claude-cli", "claude-sonnet-5-5",
            {"type": "credit_pool", "limit_amount": 10.0, "window": "monthly", "reset_anchor": 1})), 201)
        gw.ok(gw.request("POST", "/api/deployments", json=_cli_member(
            CODEX, "codex-cli", "gpt-5.5", {"type": "none", "limit_amount": None, "window": "none"})), 201)
        gw.ok(gw.request("POST", "/api/deployments", json=_api_member(fake_base)), 201)
        _route(gw, "review", [CLAUDE, API], "task")

        # ---- AC-1:預設順序 10 次 → claude-sub
        ac1 = _served([gw.chat("review") for _ in range(10)])
        obs["ac1_served_by"] = ac1
        assert ac1 == [CLAUDE] * 10

        # ---- AC-2:deepseek-api 拖到 slot 1 → 10 次 deepseek-api
        _order(gw, "review", [API, CLAUDE])
        ac2 = _served([gw.chat("review") for _ in range(10)])
        obs["ac2_served_by"] = ac2
        assert ac2 == [API] * 10

        # ---- AC-12:第二個 claude-cli 成員
        r = gw.request("POST", "/api/deployments", json=_cli_member(
            "claude-sub-2", "claude-cli", "claude-sonnet-5-5", None))
        obs["ac12"] = {"status": r.status_code, "code": r.json()["error"]["code"]}
        assert (r.status_code, r.json()["error"]["code"]) == (409, "provider_single")

        # ---- NFR-6:假 claude 停住,第二個請求由 deepseek-api 處理
        _order(gw, "review", [CLAUDE, API])
        n_claude_before = len(gw.ledger(deployment=CLAUDE))
        n_records_before = len(fake.records("claude"))
        fake.set("claude", "hold_until_release")
        box = _start_request(gw, "review")
        try:
            fake.wait_for_records("claude", n_records_before + 1, timeout=30)
            second = gw.chat("review")
            plan = gw.ok(gw.request("POST", "/api/routes/review/dry-run",
                                    json={"messages": MESSAGES, "metadata": {"mode": "task"}}))
            excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
            claude_rows_during = len(gw.ledger(deployment=CLAUDE))
        finally:
            fake.release("claude")
        box["thread"].join(60)
        first = box["resp"]
        obs["nfr6"] = {"first": _served([first])[0], "second": _served([second])[0],
                       "dry_run_reason": excluded.get(CLAUDE),
                       "claude_rows_added_during_hold": claude_rows_during - n_claude_before}
        assert obs["nfr6"]["first"] == CLAUDE and obs["nfr6"]["second"] == API
        assert excluded.get(CLAUDE) == "concurrency_full"
        assert claude_rows_during == n_claude_before                      # 被篩除不寫帳本
        assert len(gw.ledger(deployment=CLAUDE)) == n_claude_before + 1   # 放行後只多 1 列
        assert len(fake.records("claude")) == n_records_before + 1

        # ---- AC-17(CLI):額度用完 → 冷卻
        fake.set("claude", "quota")
        resets = {_next_monthly_reset(datetime.now(timezone.utc))}
        r = gw.chat("review")
        resets.add(_next_monthly_reset(datetime.now(timezone.utc)))   # 跨月邊界時兩者之一
        assert _served([r]) == [API]
        assert gw.ledger(deployment=CLAUDE)[0]["status"] == "quota_exhausted"
        dep = gw.ok(gw.request("GET", f"/api/deployments/{CLAUDE}"))
        listed = {d["id"]: d for d in gw.ok(gw.request("GET", "/api/deployments"))["deployments"]}
        dash = {d["id"]: d for d in gw.ok(gw.request("GET", "/api/dashboard"))["deployments"]}
        obs["cooldown"] = {"deployment": dep["cooling_until"], "list": listed[CLAUDE]["cooling_until"],
                           "dashboard": dash[CLAUDE]["cooling_until"], "reason": dep["cooling_reason"]}
        assert dep["cooling_until"] in resets and dep["cooling_reason"] == "quota_exhausted", (dep, resets)
        assert listed[CLAUDE]["cooling_until"] == dep["cooling_until"]
        assert dash[CLAUDE]["cooling"] is True and dash[CLAUDE]["cooling_until"] == dep["cooling_until"]
        fake.set("claude", "ok")

        # ---- codex:花費 NULL
        _route(gw, "review-codex", [CODEX], "task")
        _order(gw, "review-codex", [CODEX])
        r = gw.chat("review-codex")
        assert _served([r]) == [CODEX]
        row = gw.ledger(deployment=CODEX)[0]
        obs["codex"] = {"status": row["status"], "cost_usd": row["cost_usd"], "tokens_out": row["tokens_out"]}
        assert row["status"] == "ok" and row["cost_usd"] is None and row["tokens_out"]
        assert _q(db_path, "SELECT COUNT(*) FROM usage_ledger WHERE deployment_id = ? "
                           "AND cost_usd IS NOT NULL", CODEX) == [(0,)]

        # ---- chat route 不選 CLI
        _route(gw, "chat-route", [CLAUDE, CODEX, API], "chat")
        n_cli_records = len(fake.records())
        r = gw.chat("chat-route", task=False)
        plan = gw.ok(gw.request("POST", "/api/routes/chat-route/dry-run", json={"messages": MESSAGES}))
        excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
        obs["chat_route"] = {"served_by": _served([r])[0], "excluded": excluded}
        assert obs["chat_route"]["served_by"] == API
        assert excluded.get(CODEX) == "mode_chat_excludes_cli" and CLAUDE in excluded
        assert len(fake.records()) == n_cli_records

        # ---- dry-run 不寫帳本
        before = gw.ok(gw.request("GET", "/api/ledger", params={"page_size": 1}))["total"]
        plan = gw.ok(gw.request("POST", "/api/routes/review/dry-run",
                                json={"messages": MESSAGES, "metadata": {"mode": "task"}}))
        after = gw.ok(gw.request("GET", "/api/ledger", params={"page_size": 1}))["total"]
        obs["dry_run"] = {"ledger_before": before, "ledger_after": after, "first": plan["first"],
                          "excluded": {x["deployment"]: x["reason"] for x in plan["excluded"]}}
        assert before == after and plan["excluded"] and plan["first"] == API

        # ---- 假 CLI 記錄:環境、深度、隔離旗標
        claude_recs, codex_recs = fake.records("claude"), fake.records("codex")
        assert claude_recs and codex_recs
        for rec in claude_recs:
            assert not any(rec["env_present"][v] for v in CLAUDE_VARS), rec["env_present"]
            assert rec["env_present"]["SPILLWAY_TEST_KEY"] is False    # 決策 9:成員 env 憑證
            assert rec["spillway_depth"] == "1"
            assert rec["argv"][0] == "-p" and "--strict-mcp-config" in rec["argv"]
            assert MESSAGES[0]["content"] in rec["stdin"]
            assert not any(MESSAGES[0]["content"] in a for a in rec["argv"])
        for rec in codex_recs:
            argv = rec["argv"]
            assert not any(rec["env_present"][v] for v in CODEX_VARS), rec["env_present"]
            assert rec["env_present"]["SPILLWAY_TEST_KEY"] is False
            assert rec["spillway_depth"] == "1"
            assert ("-c", "mcp_servers={}") in list(zip(argv, argv[1:])) and argv[-2] == "--", argv
            assert "--ignore-user-config" in argv, argv
        obs["fake_cli_records"] = {
            "claude": len(claude_recs), "codex": len(codex_recs),
            "claude_env_present": sorted({v for r in claude_recs for v in CLAUDE_VARS if r["env_present"][v]}),
            "codex_env_present": sorted({v for r in codex_recs for v in CODEX_VARS if r["env_present"][v]}),
            "member_env_credential_present": sorted({
                r["name"] for r in claude_recs + codex_recs if r["env_present"]["SPILLWAY_TEST_KEY"]}),
            "depth": sorted({r["spillway_depth"] for r in claude_recs + codex_recs}),
            "claude_strict_mcp_config": all("--strict-mcp-config" in r["argv"] for r in claude_recs),
            "codex_mcp_servers_empty_then_dashdash": all(
                r["argv"][-2] == "--" and "mcp_servers={}" in r["argv"] for r in codex_recs),
            "codex_ignore_user_config": all("--ignore-user-config" in r["argv"] for r in codex_recs),
        }

        # ---- rotate:回應帶新 token,資料庫只存雜湊
        rot = gw.ok(gw.request("POST", "/api/settings/token/rotate"))
        token2 = rot["token"]
        assert set(rot) == {"token"} and token2 and token2 != token
        assert _q(db_path, "SELECT value FROM settings WHERE key = 'access_token_sha256'")[0][0] == \
            hashlib.sha256(token2.encode()).hexdigest()
        obs["rotate"] = {"new_token_returned": True, "hash_updated": True}

        # ---- 監聽位址
        addrs = procs.listening(serve.pid)
        obs["listening"] = addrs
        assert addrs == [f"127.0.0.1:{port}"], addrs

        # ---- SIGTERM:假 codex 停住時送訊號並計時
        n_codex = len(fake.records("codex"))
        fake.set("codex", "hold_until_release")
        box = _start_request(gw, "review-codex")
        recs = fake.wait_for_records("codex", n_codex + 1, timeout=30)
        held = recs[-1]
        assert _pid_alive(held["pid"])
        t0 = time.monotonic()
        serve.send_signal(signal.SIGTERM)
        limit = GRACEFUL + TERM_GRACE + SLACK
        try:
            serve.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            pytest.fail(f"serve 未在 {limit} 秒內結束")
        elapsed = round(time.monotonic() - t0, 2)
        box["thread"].join(15)
        unfinished = _unfinished(fake, run_started)
        assert [r["pid"] for r in unfinished] == [held["pid"]], unfinished   # 只有被停機中斷的那次
        leftovers = [r["pid"] for r in unfinished if _group_alive(r["pid"]) or _pid_alive(r["pid"])]
        shutdown_rows = _q(db_path, "SELECT status, error FROM usage_ledger WHERE deployment_id = ? "
                                    "ORDER BY id DESC LIMIT 1", CODEX)
        obs["sigterm"] = {"elapsed_s": elapsed, "limit_s": limit, "graceful_s": GRACEFUL,
                          "returncode": serve.returncode, "leftover_fake_pids": leftovers,
                          "last_codex_ledger": shutdown_rows,
                          "in_flight_status": box["resp"].status_code if "resp" in box else "connection closed"}
        assert elapsed >= cli.drain_expire_delay(GRACEFUL) - 0.5, elapsed   # 先等進行中請求
        assert leftovers == []
        assert shutdown_rows == [("error", "shutdown")]
        assert len(fake.records("codex")) == n_codex + 1     # 停機期間沒有再啟動假 codex
    finally:
        if gw is not None:
            gw.write_log(HTTP_LOG)
            gw.close()
        if serve is not None and serve.poll() is None:
            try:
                os.killpg(serve.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            serve.wait(timeout=15)
        procs.stop(fake_proc)
        for rec in _unfinished(fake, run_started):   # 只處理本次啟動、尚未結束的假 CLI
            if _group_alive(rec["pid"]):
                try:
                    os.killpg(rec["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # 控制目錄消失 → 仍在等待的假 CLI 自行結束
        shutil.rmtree(fake.control_dir, ignore_errors=True)

    assert serve is not None and serve.poll() is not None and fake_proc.poll() is not None

    # ---- 產物與 token 明文
    log_lines = HTTP_LOG.read_text(encoding="utf-8").splitlines()
    for line in log_lines:
        assert set(json.loads(line)) == {"method", "path", "status", "elapsed_ms"}
    obs["http_log_lines"] = len(log_lines)
    RESULTS.write_text(json.dumps(obs, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
    serve_text = serve_log.read_text(encoding="utf-8")
    leaks = {}
    for name, secret in (("init_token", token), ("rotated_token", token2)):
        leaks[name] = (_files_containing(ARTIFACTS, secret) + _files_containing(home, secret)
                       + ([str(serve_log)] if secret in serve_text else []))
    assert leaks == {"init_token": [], "rotated_token": []}, leaks
    ledger_text = json.dumps(_q(db_path, "SELECT * FROM usage_ledger"), ensure_ascii=False)
    assert token not in ledger_text and token2 not in ledger_text

