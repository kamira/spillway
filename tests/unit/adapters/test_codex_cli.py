"""T5:`codex_cli` 模組(CHG-20261101-03 決策 9、11、12、18;spec 7.4、5.2.3、5.3.1、6.3、7.5)。

argv、環境、`-o` 暫存檔與錯誤對應以 spy runner 檢查;四種上游輸出、逾時、非 JSONL、找不到執行檔
以真實 `CliRunner` 對假 `codex`(`tests/fakes/cli_upstream/`,fixture `fake_cli`)檢查。不執行真實 codex。
"""
from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway.adapters.base import (
    AdapterContext,
    AdapterRegistry,
    ChatRequest,
    CliOutput,
    CliRunner,
    Deployment,
    available_models,
)
from spillway.adapters.codex_cli import MANIFEST, create
from spillway.adapters.codex_cli.adapter import ENV_REMOVE, messages_to_prompt
from spillway.clock import FakeClock, SeqRng

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hi"}]
OK_STDOUT = (
    '{"type":"thread.started","thread_id":"t"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":4}}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":11,"cached_input_tokens":2,"output_tokens":7}}\n'
)


class SpyRunner:
    """記錄 `run()` 的引數;呼叫當下檢查 `-o` 檔(存在、權限),可寫入回應、丟例外或回傳指定輸出。"""

    def __init__(self, output: CliOutput | None = None, *, write: str | None = "answer",
                 exc: BaseException | None = None) -> None:
        self.output = output or CliOutput(exit_code=0, stdout=OK_STDOUT, stderr="")
        self.write = write
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    async def run(self, argv, *, stdin=None, env_remove=(), env_set=None, cwd=None, timeout=None,
                  **kw):
        argv = list(argv)
        out_file = Path(argv[argv.index("-o") + 1])
        self.calls.append({
            "argv": argv, "stdin": stdin, "env_remove": tuple(env_remove),
            "env_set": dict(env_set or {}), "cwd": cwd, "timeout": timeout, "out_file": out_file,
            "out_exists": out_file.is_file(), "out_mode": stat.S_IMODE(out_file.stat().st_mode),
        })
        if self.write is not None:
            out_file.write_text(self.write, encoding="utf-8")
        if self.exc is not None:
            raise self.exc
        return self.output


def ctx(runner: Any = None, data_dir: Path | None = None) -> AdapterContext:
    return AdapterContext(credentials=lambda ref: "unused", clock=FakeClock(), cli_runner=runner,
                          data_dir=data_dir)


def dep(**params: Any) -> Deployment:
    return Deployment(id="codex-sub", model_name="codex-sub", kind="cli", provider="codex-cli",
                      model="gpt-5.5", params=params)


def req(messages=None) -> ChatRequest:
    return ChatRequest(model="codex-sub", messages=messages or MSGS)


@pytest.fixture
def home() -> Path:
    return Path(os.environ["SPILLWAY_HOME"])


async def spy_call(home: Path, *, effort=None, messages=None, runner=None, **params):
    runner = runner or SpyRunner()
    result = await create(ctx(runner, home)).call(dep(**params), req(messages), effort)
    return result, runner


# ---- argv(決策 11)------------------------------------------------------------------------

async def test_argv_exact_without_effort(home):
    result, spy = await spy_call(home)
    assert result.status == "ok", result.error
    c = spy.calls[0]
    assert c["argv"] == ["codex", "exec", "--json", "--skip-git-repo-check", "--ignore-user-config",
                         "-m", "gpt-5.5",
                         "-o", str(c["out_file"]), "-c", "mcp_servers={}", "--", "[User]\nhi"]
    assert not any(a.startswith("model_reasoning_effort") for a in c["argv"])
    assert c["stdin"] is None


@pytest.mark.parametrize(("level", "upstream"), [
    ("none", "minimal"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "xhigh"),
])
async def test_effort_mapping(home, level, upstream):
    _, spy = await spy_call(home, effort=level)
    argv = spy.calls[0]["argv"]
    i = argv.index(f"model_reasoning_effort={upstream}")
    assert argv[i - 1] == "-c"
    assert argv[i - 2] == str(spy.calls[0]["out_file"])          # 緊接在 -o <暫存檔> 之後
    assert argv[i + 1:i + 3] == ["-c", "mcp_servers={}"]


async def test_ignore_user_config_always_present(home):
    # 擁有者冒煙(2026-11-02):`-c mcp_servers={}` 與 config.toml 合併而非取代,使用者設定的 MCP 仍載入;
    # 決策 11 補記:固定加 `--ignore-user-config`(不讀 $CODEX_HOME/config.toml,登入仍用 CODEX_HOME)
    _, spy = await spy_call(home, extra_args=["--full-auto"])
    argv = spy.calls[0]["argv"]
    assert argv.count("--ignore-user-config") == 1
    assert argv.index("--ignore-user-config") < argv.index("--")


@pytest.mark.parametrize("extra", [
    ["-c", "mcp_servers.gw.command=x"],
    ["--config", "mcp_servers={gw={command='x'}}"],
    ["--config=mcp_servers.gw.command=x"],
    ["-cmcp_servers.gw.command=x"],
    ["-c", ' "mcp_servers" .gw.command=x'],
    ["-p", "work"],
    ["--profile", "work"],
    ["--profile=work"],
    ["-c=mcp_servers.gw.command=x"],
    ["-pwork"],
    ["-p=work"],
    ["-c", "\tmcp_servers.x=1"],
    ["-c", "profile=work"],
    ["--enable", "apps"],
    ["--enable=apps"],
    ["-c", "features.apps=true"],
    ["-c", "features={apps=true}"],
], ids=["c", "config", "config-eq", "c-joined", "quoted-key", "p", "profile", "profile-eq",
        "c-eq", "p-joined", "p-eq", "tab-key", "c-profile", "enable", "enable-eq", "c-feature",
        "c-features-table"])
async def test_extra_args_cannot_add_mcp_or_profile(home, extra):
    spy = SpyRunner()
    result = await create(ctx(spy, home)).call(dep(extra_args=extra), req(), None)
    assert result.status == "error" and "extra_args" in result.error
    assert spy.calls == []


@pytest.mark.parametrize("extra", [["--", "x"], ["resume"], ["--sandbox=read-only", "x"], "--x"],
                         ids=["separator", "bare-resume", "after-eq-option", "string"])
async def test_bad_extra_args_rejected_without_prompt(home, extra):
    spy = SpyRunner()
    result = await create(ctx(spy, home)).call(dep(extra_args=extra),
                                               req([{"role": "user", "content": "secret-prompt"}]), None)
    assert result.status == "error" and not result.ok and "extra_args" in result.error
    assert "secret-prompt" not in result.error
    assert spy.calls == []


@pytest.mark.parametrize("extra", [["-c", "mcp_servers_timeout=5"], ["-c", "profile_note=x"],
                                   ["--disable", "apps"]])
async def test_similar_config_keys_allowed(home, extra):
    result, spy = await spy_call(home, extra_args=extra)
    assert result.ok, result.error
    assert spy.calls


async def test_option_values_allowed_in_extra_args(home):
    extra = ["--sandbox", "read-only", "-c", "x=1", "--full-auto"]
    result, spy = await spy_call(home, extra_args=extra)
    assert result.ok, result.error
    argv = spy.calls[0]["argv"]
    i = argv.index("mcp_servers={}")
    assert argv[i - 1 - len(extra):i - 1] == extra


@pytest.mark.parametrize("text", ["--help", "-rf /", "resume --last", "resume"])
async def test_prompt_after_separator_verbatim(home, text):
    messages = [{"role": "user", "content": text}]
    _, spy = await spy_call(home, messages=messages)
    argv = spy.calls[0]["argv"]
    assert argv[-2] == "--"
    assert argv[-1] == messages_to_prompt(messages) == f"[User]\n{text}"


def test_messages_to_prompt_7_5_1():
    msgs = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "part a"}, {"type": "text", "text": "part b"}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "go"},
    ]
    assert messages_to_prompt(msgs) == (
        "[System]\nbe brief\n\n[User]\npart a\npart b\n\n[Assistant]\nok\n\n[User]\ngo")


def test_uses_shared_prompt_converter():
    import spillway.adapters.base as base
    import spillway.adapters.codex_cli.adapter as mod
    assert mod.messages_to_prompt is base.messages_to_prompt      # 決策 12a
    assert not hasattr(mod, "_content_text") and not hasattr(mod, "_HEADERS")


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "   "}],
    [{"role": "tool", "content": "x"}],
    [{"role": "assistant", "content": None}],
], ids=["blank", "tool-only", "no-text"])
async def test_empty_prompt_is_error_without_process(home, messages):
    spy = SpyRunner()
    result = await create(ctx(spy, home)).call(dep(), req(messages), None)
    assert result.status == "error" and not result.ok
    assert spy.calls == []
    assert not (home / "work" / "codex").exists()


# ---- 環境與工作目錄(決策 9、11)--------------------------------------------------------------

async def test_env_remove_and_depth(home):
    _, spy = await spy_call(home)
    c = spy.calls[0]
    assert set(c["env_remove"]) == {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"}
    assert set(ENV_REMOVE) == set(c["env_remove"])
    assert c["env_set"] == {"SPILLWAY_DEPTH": "1"}
    assert c["timeout"] == 600


async def test_default_cwd_is_data_dir_work_codex(home):
    _, spy = await spy_call(home)
    cwd = Path(spy.calls[0]["cwd"])
    assert cwd == home / "work" / "codex" and cwd.is_dir()


async def test_params_cwd_overrides(home, tmp_path):
    _, spy = await spy_call(home, cwd=str(tmp_path))
    assert Path(spy.calls[0]["cwd"]) == tmp_path
    assert not (home / "work" / "codex").exists()


async def test_no_data_dir_and_no_cwd_is_error():
    spy = SpyRunner()
    result = await create(ctx(spy, None)).call(dep(), req(), None)
    assert result.status == "error" and not result.ok and "cwd" in result.error
    assert spy.calls == []


async def test_cli_runner_none_create_ok_call_error(home):
    adapter = create(ctx(None, home))
    result = await adapter.call(dep(), req(), None)
    assert result.status == "error" and not result.ok and "cli_runner" in result.error
    assert await adapter.health(dep()) is True


# ---- -o 暫存檔(0600、finally 刪除)、回應與 token ----------------------------------------

async def test_ok_maps_output_file_and_last_usage(home):
    result, spy = await spy_call(home)
    assert (result.ok, result.status, result.text) == (True, "ok", "answer")
    assert (result.tokens_in, result.tokens_out) == (11, 7)        # 最後一個含 usage 的事件
    assert result.cost_usd is None                                 # spec 5.2.3
    c = spy.calls[0]
    assert c["out_exists"] and c["out_mode"] == 0o600
    assert not c["out_file"].exists()


async def test_negative_token_fields_become_none(home):
    """決策 20:與 claude_cli 一致,負數 token 欄位視為 None。"""
    out = CliOutput(exit_code=0, stderr="",
                    stdout='{"type":"turn.completed","usage":{"input_tokens":-3,"output_tokens":-1}}\n')
    result, _ = await spy_call(home, runner=SpyRunner(out))
    assert result.ok and (result.tokens_in, result.tokens_out) == (None, None)


@pytest.mark.parametrize("output", [
    CliOutput(exit_code=1, stdout="", stderr="ERROR: boom"),
    CliOutput(exit_code=None, stdout="", stderr="", timed_out=True, killed_reason="timeout"),
    CliOutput(exit_code=-15, stdout="", stderr="", killed_reason="shutdown"),
], ids=["exit1", "timeout", "shutdown"])
async def test_output_file_deleted_on_failure(home, output):
    result, spy = await spy_call(home, runner=SpyRunner(output))
    assert result.status == "error" and not result.ok
    c = spy.calls[0]
    assert c["out_exists"] and c["out_mode"] == 0o600
    assert not c["out_file"].exists()


@pytest.mark.parametrize("exc", [OSError(errno.E2BIG, "Argument list too long"),
                                 FileNotFoundError(errno.ENOENT, "No such file or directory", "codex")],
                         ids=["E2BIG", "missing-binary"])
async def test_oserror_is_error_and_file_deleted(home, exc):
    result, spy = await spy_call(home, runner=SpyRunner(exc=exc))
    assert result.status == "error" and not result.ok
    assert f"errno {exc.errno}" in result.error
    assert "[User]" not in result.error                            # 不含 prompt
    assert not spy.calls[0]["out_file"].exists()


async def test_cancel_deletes_output_file(home):
    spy = SpyRunner(exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await create(ctx(spy, home)).call(dep(), req(), None)
    assert not spy.calls[0]["out_file"].exists()


async def test_shutdown_reason_in_error(home):
    out = CliOutput(exit_code=-15, stdout="", stderr="", killed_reason="shutdown")
    result, _ = await spy_call(home, runner=SpyRunner(out))
    assert result.status == "error" and "shutdown" in result.error


@pytest.mark.parametrize(("message", "status"), [
    ("You've hit your usage limit. Try again later.", "quota_exhausted"),
    ("exceeded retry limit, last status: 429 Too Many Requests", "rate_limited"),
])
async def test_failure_only_in_stdout_jsonl_is_classified(home, message, status):
    import json
    stdout = (json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"
              + json.dumps({"type": "error", "message": message}) + "\n")
    out = CliOutput(exit_code=1, stdout=stdout, stderr="")
    result, _ = await spy_call(home, runner=SpyRunner(out))
    assert (result.status, result.ok) == (status, False)
    assert message in result.error


async def test_empty_output_file_is_error(home):
    result, _ = await spy_call(home, runner=SpyRunner(write=None))
    assert result.status == "error"


# ---- 模組清單不起子行程 ---------------------------------------------------------------------

async def test_list_models_does_not_spawn(home):
    spy = SpyRunner()
    c = ctx(spy, home)
    r = AdapterRegistry(c)
    r.discover()
    info = next(m for m in r.modules() if m.name == "codex_cli")
    assert info.available, info.reason
    assert info.manifest == MANIFEST
    assert (info.manifest.cardinality, info.manifest.access) == ("single", ("subscription-cli",))
    listing = await available_models(info, c)
    assert {m.id for m in listing.models} >= {"gpt-5.5"}
    assert spy.calls == []


# ---- 真實子行程 × 假 codex(決策 18)---------------------------------------------------------

@pytest.fixture
def real(fake_cli, home):
    return create(ctx(CliRunner(term_grace=0.5, drain_timeout=0.5), home)), fake_cli


@pytest.mark.parametrize(("scenario", "status"), [
    ("ok", "ok"), ("quota", "quota_exhausted"), ("rate_limit", "rate_limited"), ("error", "error"),
])
async def test_four_upstream_outputs(real, scenario, status):
    adapter, fake = real
    fake.set("codex", scenario)
    result = await adapter.call(dep(), req(), None)
    assert (result.status, result.ok) == (status, status == "ok"), result.error
    assert result.cost_usd is None
    if status == "ok":
        assert result.text == "fake codex reply"
        assert result.tokens_in == max(1, len("[User]\nhi") // 4) and result.tokens_out == 9
    else:
        assert result.error


async def test_fake_record_env_flags_and_cleanup(real, home, monkeypatch):
    adapter, fake = real
    for var in ENV_REMOVE:
        monkeypatch.setenv(var, "fake-value-not-a-secret")
    result = await adapter.call(dep(), req([{"role": "user", "content": "resume --last"}]), "max")
    assert result.ok, result.error
    rec = fake.records("codex")[-1]
    assert rec["spillway_depth"] == "1"
    assert not any(rec["env_present"][v] for v in ENV_REMOVE)
    argv = rec["argv"]
    assert argv[:5] == ["exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "-m"]
    assert ["-c", "model_reasoning_effort=xhigh"] == argv[argv.index("model_reasoning_effort=xhigh") - 1:][:2]
    assert "mcp_servers={}" in argv and argv[-2:] == ["--", "[User]\nresume --last"]
    assert Path(rec["cwd"]).resolve() == (home / "work" / "codex").resolve()
    assert not Path(argv[argv.index("-o") + 1]).exists()


async def test_non_json_is_error(real):
    adapter, fake = real
    fake.set("codex", "non_json")
    result = await adapter.call(dep(), req(), None)
    assert result.status == "error" and not result.ok


async def test_timeout_is_error_and_file_deleted(real):
    adapter, fake = real
    fake.set("codex", "timeout")
    # 逾時要長於假 codex 啟動並寫出開始紀錄所需時間(0.5 秒在負載下會在寫紀錄前被殺,測試偶發失敗)
    result = await adapter.call(dep(timeout=2.0), req(), None)
    assert result.status == "error" and "逾時" in result.error
    rec = fake.records("codex")[-1]
    assert not Path(rec["argv"][rec["argv"].index("-o") + 1]).exists()


async def test_nul_in_prompt_is_error(real):
    adapter, fake = real
    result = await adapter.call(dep(), req([{"role": "user", "content": "a\x00b"}]), None)
    assert result.status == "error" and not result.ok and "NUL" in result.error
    assert "a\x00b" not in result.error
    assert fake.records("codex") == []


async def test_missing_binary_is_error(home, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    adapter = create(ctx(CliRunner(term_grace=0.5), home))
    result = await adapter.call(dep(), req(), None)
    assert result.status == "error" and not result.ok


# ---- cardinality(AC-12、spec 7.6.5)---------------------------------------------------------

async def test_second_codex_member_409_provider_single(home):
    from spillway.app import create_app

    app = create_app(clock=FakeClock(), rng=SeqRng([0.0]), db_path=home / "spillway.db")
    body = {"model_name": "codex", "kind": "cli", "provider": "codex-cli", "model": "gpt-5.5"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r1 = await c.post("/api/deployments", json={"id": "codex-a", **body})
            assert r1.status_code == 201, r1.text
            r2 = await c.post("/api/deployments", json={"id": "codex-b", **body})
            assert r2.status_code == 409, r2.text
            assert r2.json()["error"]["code"] == "provider_single"
            mods = {m["module"]: m for m in (await c.get("/api/adapters")).json()["modules"]}
    assert mods["codex_cli"]["cardinality"] == "single"
    assert app.state.cli_runner.active_pids == ()


# ---- 終審 I1a:params.timeout 驗證與 claude_cli 一致 ------------------------------------------

@pytest.mark.parametrize("timeout", [0, -1, True, False, float("nan"), float("inf"), "abc", [1]])
async def test_bad_timeout_rejected_without_process(home, timeout):
    result, spy = await spy_call(home, timeout=timeout)
    assert result.status == "error" and "params.timeout 必須是正數(秒)" in result.error
    assert spy.calls == []


@pytest.mark.parametrize("timeout, expected", [(None, None), (12, 12.0), ("2.5", 2.5)])
async def test_good_timeout_passed_to_runner(home, timeout, expected):
    result, spy = await spy_call(home, timeout=timeout)
    assert result.status == "ok"
    assert spy.calls[0]["timeout"] == expected
