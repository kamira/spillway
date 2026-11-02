"""claude_cli 模組(CHG-20261101-03 T4;spec 7.3、7.5、5.2.2、5.3.1;決策 9、10、12)。

子行程一律是 `tests/fakes/cli_upstream/` 的假 `claude`(`fake_cli` fixture),經真的 `CliRunner` 執行;
只有 CLI 實際上不會產生的輸出(事件陣列、被停機終止)以替身 runner 驗證。
"""
from __future__ import annotations

import json
import os
import sys
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
    load_models_yaml,
    validate_manifest,
)
from spillway.adapters.claude_cli import MANIFEST, create
from spillway.adapters.claude_cli.adapter import ENV_REMOVE, MCP_OFF, build_argv, messages_to_prompt
from spillway.clock import FakeClock, SeqRng

pytestmark = pytest.mark.anyio

SRC = Path(__file__).resolve().parents[3] / "src" / "spillway"
MSGS = [{"role": "user", "content": "hi"}]
BASE_ARGV = ["-p", "--output-format", "json", "--model", "claude-sonnet-5-5"]


def ctx(runner: Any = "default", data_dir: Path | None = None) -> AdapterContext:
    return AdapterContext(credentials=lambda ref: "unused", clock=FakeClock(),
                          cli_runner=CliRunner() if runner == "default" else runner,
                          data_dir=data_dir)


def dep(**params: Any) -> Deployment:
    return Deployment(id="claude-sub", model_name="claude-sub", kind="cli", provider="claude-cli",
                      model="claude-sonnet-5-5", params=params)


def req(messages: list[dict[str, Any]] | None = None) -> ChatRequest:
    return ChatRequest(model="review", messages=messages or MSGS)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def adapter(data_dir: Path):
    return create(ctx(data_dir=data_dir))


async def _one(fake_cli, adapter, d: Deployment | None = None, effort: str | None = None,
               messages=None):
    result = await adapter.call(d or dep(), req(messages), effort)
    recs = fake_cli.records("claude")
    return result, (recs[-1] if recs else None)


class StubRunner:
    """替身 runner:回固定 CliOutput 並記下參數(不起子行程)。"""

    def __init__(self, out: CliOutput | None = None, exc: BaseException | None = None) -> None:
        self.out, self.exc, self.calls = out, exc, []

    async def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc is not None:
            raise self.exc
        return self.out


# ---- manifest ------------------------------------------------------------------------------

def test_manifest_values():
    m = validate_manifest(MANIFEST)
    assert m.providers == ("claude-cli",) and m.kinds == ("cli",)
    assert m.access == ("subscription-cli",) and m.cardinality == "single"
    assert m.effort_map == {"none": None, "low": "low", "medium": "medium", "high": "high",
                            "max": "max"}
    assert m.default_params == {"timeout": 600, "effort_args": ["--effort", "{effort}"],
                                "extra_args": []}
    assert m.default_capabilities["tools"] is False and m.default_capabilities["vision"] is False
    assert m.default_signals == {
        "quota": [{"equals": {"is_error": True},
                   "match": {"message": "credit|usage limit|extra usage"}}],
        "rate_limit": [{"match": {"message": "rate limit|overloaded"}}],
    }


def test_oauth_variable_name_not_in_src():
    """決策 9:閘道不讀取官方登入變數,src 中不得出現其名稱。"""
    name = "CLAUDE_CODE_" + "OAUTH_TOKEN"
    hits = [p for p in SRC.rglob("*") if p.is_file() and p.suffix in (".py", ".yaml", ".md")
            and name in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == []


# ---- prompt(spec 7.5.1)--------------------------------------------------------------------

def test_messages_to_prompt():
    prompt = messages_to_prompt([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "part 1"},
                                     {"type": "text", "text": "part 2"}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "again"},
    ])
    assert prompt == ("[System]\nbe brief\n\n[User]\npart 1\npart 2\n\n[Assistant]\nok\n\n"
                      "[User]\nagain")


# ---- argv、stdin、cwd、環境 ---------------------------------------------------------------

async def test_argv_stdin_cwd_env(fake_cli, adapter, data_dir, monkeypatch):
    for name in ENV_REMOVE:
        monkeypatch.setenv(name, "fake-value")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")          # codex 的清單,claude 不移除
    monkeypatch.setenv("CLAUDE_CODE_" + "OAUTH_TOKEN", "fake")   # 官方登入變數照常繼承
    monkeypatch.setenv("SPILLWAY_DEPTH", "0")
    result, rec = await _one(fake_cli, adapter, effort="high",
                             messages=[{"role": "system", "content": "s"},
                                       {"role": "user", "content": "u"}])
    assert result.status == "ok", result.error
    assert rec["argv"] == BASE_ARGV + ["--effort", "high", "--strict-mcp-config"]
    assert rec["stdin"] == "[System]\ns\n\n[User]\nu"
    work = data_dir / "work" / "claude"
    assert work.is_dir()
    assert Path(rec["cwd"]).resolve() == work.resolve()
    assert rec["spillway_depth"] == "1"
    present = rec["env_present"]
    assert not any(present[n] for n in ENV_REMOVE)
    assert present["OPENAI_API_KEY"] is True
    assert present["CLAUDE_CODE_" + "OAUTH_TOKEN"] is True


def test_mcp_off_constant():
    assert MCP_OFF == ("--strict-mcp-config",)


def test_env_remove_list_is_decision_9():
    assert ENV_REMOVE == (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    )


@pytest.mark.parametrize("effort", [None, "none"])
async def test_no_effort_args_when_none(fake_cli, adapter, effort):
    _, rec = await _one(fake_cli, adapter, effort=effort)
    assert rec["argv"] == BASE_ARGV + ["--strict-mcp-config"]


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
async def test_effort_levels(fake_cli, adapter, level):
    _, rec = await _one(fake_cli, adapter, effort=level)
    assert rec["argv"] == BASE_ARGV + ["--effort", level, "--strict-mcp-config"]


async def test_effort_args_empty_or_custom_template(fake_cli, adapter):
    _, rec = await _one(fake_cli, adapter, dep(effort_args=[]), effort="high")
    assert rec["argv"] == BASE_ARGV + ["--strict-mcp-config"]
    _, rec = await _one(fake_cli, adapter, dep(effort_args=["--thinking={effort}"]), effort="low")
    assert rec["argv"] == BASE_ARGV + ["--thinking=low", "--strict-mcp-config"]


async def test_strict_mcp_flag_kept_with_custom_extra_args(fake_cli, adapter):
    _, rec = await _one(fake_cli, adapter, dep(extra_args=["--verbose"]), effort="medium")
    assert rec["argv"] == BASE_ARGV + ["--effort", "medium", "--strict-mcp-config", "--verbose"]
    _, rec = await _one(fake_cli, adapter, dep(extra_args=["--mcp-config", "x.json"]))
    assert "--strict-mcp-config" in rec["argv"]
    assert rec["argv"].index("--strict-mcp-config") < rec["argv"].index("--mcp-config")


async def test_params_cwd_used(fake_cli, adapter, tmp_path):
    own = tmp_path / "own-cwd"
    own.mkdir()
    result, rec = await _one(fake_cli, adapter, dep(cwd=str(own)))
    assert result.status == "ok"
    assert Path(rec["cwd"]).resolve() == own.resolve()


async def test_bad_params_return_error_without_spawn(fake_cli, adapter):
    for d in (dep(extra_args="--verbose"), dep(effort_args=[1]), dep(timeout="soon"),
              dep(signals={"quota": "nope"})):
        result = await adapter.call(d, req(), "high")
        assert result.status == "error" and result.ok is False and result.error
    assert fake_cli.records("claude") == []


# ---- 回應對應 ------------------------------------------------------------------------------

async def test_ok_maps_result_usage_cost(fake_cli, adapter):
    fake_cli.set("claude", "ok", result="hello there", input_tokens=321, output_tokens=45,
                 total_cost_usd=0.0456)
    result, _ = await _one(fake_cli, adapter)
    assert result.ok is True and result.status == "ok" and result.error is None
    assert result.text == "hello there"
    assert (result.tokens_in, result.tokens_out) == (321, 45)
    assert result.cost_usd == pytest.approx(0.0456)


@pytest.mark.parametrize(("scenario", "expected"), [
    ("quota", "quota_exhausted"), ("rate_limit", "rate_limited"), ("error", "error"),
    ("non_json", "error"),
])
async def test_upstream_outputs_classified(fake_cli, adapter, scenario, expected):
    fake_cli.set("claude", scenario)
    result, _ = await _one(fake_cli, adapter)
    assert (result.ok, result.status) == (False, expected)
    assert result.error and result.cost_usd is None and result.text == ""


async def test_signals_override(fake_cli, adapter):
    fake_cli.set("claude", "error", result="custom budget gone")
    d = dep(signals={"quota": [{"equals": {"is_error": True}, "match": {"message": "budget gone"}}]})
    result, _ = await _one(fake_cli, adapter, d)
    assert result.status == "quota_exhausted"


async def test_nonzero_exit_without_is_error_is_failure(fake_cli, adapter):
    fake_cli.set("claude", "ok", exit_code=2)
    result, _ = await _one(fake_cli, adapter)
    assert (result.ok, result.status) == (False, "error")


async def test_timeout_is_error(fake_cli, adapter):
    fake_cli.set("claude", "timeout")
    result, _ = await _one(fake_cli, adapter, dep(timeout=0.5))
    assert (result.ok, result.status) == (False, "error")
    assert "逾時" in result.error


async def test_missing_executable_is_error(adapter, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = await adapter.call(dep(), req(), None)
    assert (result.ok, result.status) == (False, "error")
    assert "找不到" in result.error


async def test_verbose_event_array_takes_last_result():
    body = ('[{"type":"system","subtype":"init"},'
            '{"type":"result","is_error":false,"result":"done","usage":{"input_tokens":3,'
            '"output_tokens":4},"total_cost_usd":0.5}]')
    runner = StubRunner(CliOutput(exit_code=0, stdout=body, stderr=""))
    a = create(ctx(runner=runner, data_dir=Path(os.environ["SPILLWAY_HOME"])))
    result = await a.call(dep(), req(), None)
    assert (result.status, result.text, result.tokens_in, result.tokens_out, result.cost_usd) == \
        ("ok", "done", 3, 4, 0.5)


async def test_shutdown_and_truncated_are_error():
    home = Path(os.environ["SPILLWAY_HOME"])
    killed = StubRunner(CliOutput(exit_code=-15, stdout="", stderr="", killed_reason="shutdown"))
    result = await create(ctx(runner=killed, data_dir=home)).call(dep(), req(), None)
    assert result.status == "error" and "(shutdown)" in result.error
    trunc = StubRunner(CliOutput(exit_code=0, stdout='{"result": "x', stderr="",
                                 stdout_truncated=True))
    result = await create(ctx(runner=trunc, data_dir=home)).call(dep(), req(), None)
    assert result.status == "error" and "截斷" in result.error


async def test_oserror_is_error():
    runner = StubRunner(exc=OSError(7, "Argument list too long"))
    result = await create(ctx(runner=runner, data_dir=Path(os.environ["SPILLWAY_HOME"]))).call(
        dep(), req(), None)
    assert result.status == "error" and "Argument list too long" in result.error


async def test_runner_receives_env_contract():
    runner = StubRunner(CliOutput(exit_code=0, stdout='{"result":"x","is_error":false}', stderr=""))
    await create(ctx(runner=runner, data_dir=Path(os.environ["SPILLWAY_HOME"]))).call(
        dep(timeout=12), req(), None)
    (argv, kw), = runner.calls
    assert argv[0] == "claude"
    assert tuple(kw["env_remove"]) == ENV_REMOVE
    assert kw["env_set"] == {"SPILLWAY_DEPTH": "1"}
    assert kw["timeout"] == 12.0 and kw["stdin"] == "[User]\nhi"


# ---- 注入缺漏與其他 kind ------------------------------------------------------------------

async def test_no_cli_runner_create_ok_call_error(fake_cli, data_dir):
    a = create(ctx(runner=None, data_dir=data_dir))
    result = await a.call(dep(), req(), None)
    assert (result.ok, result.status) == (False, "error") and "cli_runner" in result.error
    assert await a.health(dep()) is True
    assert fake_cli.records("claude") == []


async def test_no_data_dir_and_no_cwd_is_error(fake_cli):
    a = create(ctx(data_dir=None))
    result = await a.call(dep(), req(), None)
    assert result.status == "error" and "data_dir" in result.error
    assert fake_cli.records("claude") == []


async def test_no_data_dir_with_params_cwd_ok(fake_cli, tmp_path):
    a = create(ctx(data_dir=None))
    result = await a.call(dep(cwd=str(tmp_path)), req(), None)
    assert result.status == "ok", result.error


async def test_wrong_kind_is_error(fake_cli, adapter):
    d = Deployment(id="x", model_name="x", kind="api", provider="claude-cli", model="m")
    assert (await adapter.call(d, req(), None)).status == "error"
    assert fake_cli.records("claude") == []


# ---- 可用模型與管理 API(L2)------------------------------------------------------------

async def test_available_models_does_not_spawn(fake_cli, data_dir):
    c = ctx(data_dir=data_dir)
    r = AdapterRegistry(c)
    r.discover()
    info = next(m for m in r.modules() if m.name == "claude_cli")
    assert info.available and not info.has_list_models
    listing = await available_models(info, c)
    assert [m.id for m in listing.models] == ["claude-opus-5-5", "claude-sonnet-5-5",
                                              "claude-haiku-5-5"]
    assert all(m.capabilities["tools"] is False for m in listing.models)
    assert fake_cli.records() == []


def _cli_member(dep_id: str) -> dict[str, Any]:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-5-5", "tags": ["r"]}


async def test_admin_lists_module_and_rejects_second_member(fake_cli, tmp_path, clock):
    from spillway.app import create_app

    app = create_app(clock=clock, rng=SeqRng([0.0]), db_path=tmp_path / "t4.db")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
            r = await c.post("/api/deployments", json=_cli_member("claude-sub"))
            assert r.status_code == 201, r.text
            r = await c.get("/api/adapters")
            assert r.status_code == 200, r.text
            mod = next(m for m in r.json()["modules"] if m["module"] == "claude_cli")
            assert mod["providers"] == ["claude-cli"] and mod["access"] == ["subscription-cli"]
            assert mod["cardinality"] == "single" and mod["available"] is True
            assert mod["existing_members"] == 1 and mod["models_error"] is None
            assert [m["id"] for m in mod["models"]][0] == "claude-opus-5-5"
            r = await c.post("/api/deployments", json=_cli_member("claude-sub-2"))
            assert r.status_code == 409, r.text
            assert r.json()["error"]["code"] == "provider_single"
    assert fake_cli.records() == []


# ---- 施工審議 T4 回歸(arbiter fix list 3–9)-----------------------------------------------

def _stub_adapter(stdout: str, exit_code: int = 0, stderr: str = "") -> tuple[Any, StubRunner]:
    runner = StubRunner(CliOutput(exit_code=exit_code, stdout=stdout, stderr=stderr))
    return create(ctx(runner=runner, data_dir=Path(os.environ["SPILLWAY_HOME"]))), runner


async def test_is_error_true_with_exit_zero_is_classified(fake_cli, adapter):
    fake_cli.set("claude", "quota", exit_code=0)
    result, _ = await _one(fake_cli, adapter)
    assert (result.ok, result.status) == (False, "quota_exhausted")


async def test_model_answer_not_used_for_classification(fake_cli, adapter):
    """D3:結束碼 ≠ 0 但 is_error 不是 true 時,模型回答(含 rate limit 字樣)不進判定。"""
    fake_cli.set("claude", "ok", result="Here is how to handle a rate limit or overloaded API",
                 exit_code=2)
    result, _ = await _one(fake_cli, adapter)
    assert (result.ok, result.status) == (False, "error")
    assert "rate limit" not in (result.error or "")


async def test_nonzero_exit_uses_stderr_for_classification():
    a, _ = _stub_adapter('{"type":"result","is_error":false,"result":"fine answer"}', exit_code=1,
                         stderr="API Error: rate limit exceeded")
    result = await a.call(dep(), req(), None)
    assert result.status == "rate_limited" and "rate limit" in result.error


async def test_quota_wins_when_both_signals_match(fake_cli, adapter):
    fake_cli.set("claude", "quota", result="usage limit reached; rate limit also hit")
    result, _ = await _one(fake_cli, adapter)
    assert result.status == "quota_exhausted"


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-Infinity", "-0.5", "true", '"0.1"'])
async def test_bad_cost_is_none(cost):
    a, _ = _stub_adapter('{"is_error":false,"result":"x","total_cost_usd":%s}' % cost)
    result = await a.call(dep(), req(), None)
    assert result.status == "ok" and result.cost_usd is None


@pytest.mark.parametrize(("usage", "expected"), [
    ({"input_tokens": 10}, 10),
    ({"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000}, 3210),
    ({"cache_read_input_tokens": 7}, 7),
    ({"input_tokens": 10, "cache_creation_input_tokens": -5, "cache_read_input_tokens": "9"}, 10),
    ({"input_tokens": True, "cache_read_input_tokens": 1.5}, None),
    ({}, None),
])
async def test_tokens_in_sums_cache_fields(usage, expected):
    a, _ = _stub_adapter(json.dumps({"is_error": False, "result": "x",
                                     "usage": {**usage, "output_tokens": 4}}))
    result = await a.call(dep(), req(), None)
    assert (result.tokens_in, result.tokens_out) == (expected, 4)


async def test_ok_sets_finish_reason_stop(fake_cli, adapter):
    result, _ = await _one(fake_cli, adapter)
    assert result.status == "ok" and result.finish_reason == "stop"


async def test_model_starting_with_dash_rejected(fake_cli, adapter):
    d = Deployment(id="c", model_name="c", kind="cli", provider="claude-cli", model="--dangerous")
    result = await adapter.call(d, req(), None)
    assert result.status == "error" and "'-'" in result.error
    assert fake_cli.records("claude") == []
    with pytest.raises(ValueError):
        build_argv("-x", None, [], [])


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "abc"])
async def test_bad_timeout_rejected(fake_cli, adapter, timeout):
    result = await adapter.call(dep(timeout=timeout), req(), None)
    assert result.status == "error" and "timeout" in result.error
    assert fake_cli.records("claude") == []


async def test_timeout_none_means_no_limit():
    a, runner = _stub_adapter('{"is_error":false,"result":"x"}')
    assert (await a.call(dep(timeout=None), req(), None)).status == "ok"
    assert runner.calls[0][1]["timeout"] is None


async def test_verbose_array_without_result_has_own_message():
    a, _ = _stub_adapter('[{"type":"system","subtype":"init"}]')
    result = await a.call(dep(), req(), None)
    assert result.status == "error" and "缺少 result 事件" in result.error
    assert "不是 JSON" not in result.error


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "content": "tool output"}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}],
    [{"role": "user", "content": "   "}],
])
async def test_empty_prompt_is_error_without_spawn(fake_cli, adapter, messages):
    result = await adapter.call(dep(), req(messages), None)
    assert result.status == "error" and "沒有可送出的文字訊息" in result.error
    assert fake_cli.records("claude") == []


def test_messages_to_prompt_is_base_implementation():
    from spillway.adapters import base

    assert messages_to_prompt is base.messages_to_prompt


def test_manifest_ctx_matches_models_yaml():
    models = load_models_yaml(SRC / "adapters" / "claude_cli" / "models.yaml")
    assert {m.capabilities["ctx"] for m in models} == {MANIFEST.default_capabilities["ctx"]}


def test_harness_fails_loudly_without_registry_instance(monkeypatch):
    from tests.fakes import claude_cli as harness

    monkeypatch.setitem(sys.modules, harness.CONTRACT_TEST_MODULE, None)
    with pytest.raises(AssertionError):
        harness.call_status_harness(None)
