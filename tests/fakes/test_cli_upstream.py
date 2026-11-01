"""T3(CHG-20261101-03)L1/L2:假 CLI 執行檔的協定與真實 CLI 護欄。

全程只執行 `tests/fakes/cli_upstream/` 產生的假執行檔;「真實」執行檔一律是暫存目錄中的模擬檔,不會被執行。
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.fakes import cli_upstream
from tests.fakes.cli_upstream import (
    CLI_NAMES, FakeCli, RealCliFound, assert_only_fakes, install, is_fake, real_cli_dirs,
    safe_subprocess_env, sanitize_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# spec 5.3.1 預設訊號
CLAUDE_QUOTA = re.compile(r"credit|usage limit|extra usage", re.I)
CLAUDE_RATE = re.compile(r"rate limit|overloaded", re.I)
CODEX_QUOTA = re.compile(r"usage limit|quota", re.I)
CODEX_RATE = re.compile(r"rate limit|429", re.I)

FAKE_SECRET = "sk-fake-presence-only-31337"


def _run(fake_cli: FakeCli, name: str, args: list[str], *, stdin: str = "",
         timeout: float = 20, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([name, *args], input=stdin, capture_output=True, text=True,
                          timeout=timeout, **kw)


def _claude_args() -> list[str]:
    return ["-p", "--output-format", "json", "--model", "sonnet", "--strict-mcp-config"]


def _codex_args(out: Path, prompt: str = "[User]\nhello codex") -> list[str]:
    return ["exec", "--json", "--skip-git-repo-check", "-m", "gpt-5-codex", "-o", str(out),
            "-c", "mcp_servers={}", "--", prompt]


def _jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _last_usage(events: list[dict]) -> dict:
    return [e["usage"] for e in events if "usage" in e][-1]


def _simulated_real(dir_: Path, names=CLI_NAMES) -> Path:
    """模擬「真實」執行檔:可執行、但不是假執行檔。測試不會執行它。"""
    dir_.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = dir_ / n
        p.write_text("#!/bin/sh\necho SIMULATED-REAL-CLI-MUST-NOT-RUN\nexit 42\n", encoding="utf-8")
        p.chmod(0o755)
    return dir_


# ---- 安裝 ----

def test_wrappers_use_current_interpreter_and_marker(fake_cli):
    for name in CLI_NAMES:
        text = fake_cli.path(name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert cli_upstream.FAKE_MARKER in text
        assert sys.executable in text
        assert "/usr/bin/python3" not in text
        assert os.access(fake_cli.path(name), os.X_OK)
        assert is_fake(fake_cli.path(name))


def test_presence_vars_identical_in_fake_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_fake_cli_script", cli_upstream.FAKE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PRESENCE_VARS == cli_upstream.PRESENCE_VARS
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cli_upstream.PRESENCE_VARS


# ---- claude ----

def test_claude_ok_json_matches_spec_735_and_record(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_DEPTH", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", FAKE_SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    work = tmp_path / "work-claude"
    work.mkdir()
    fake_cli.set("claude", "ok", result="hello from fake", input_tokens=33, output_tokens=7,
                 total_cost_usd=0.5)
    cp = _run(fake_cli, "claude", _claude_args(), stdin="[User]\nhi there", cwd=work)
    assert cp.returncode == 0, cp.stderr
    data = json.loads(cp.stdout)
    assert data["is_error"] is False
    assert data["result"] == "hello from fake"
    assert data["usage"]["input_tokens"] == 33
    assert data["usage"]["output_tokens"] == 7
    assert data["total_cost_usd"] == 0.5

    (rec,) = fake_cli.records("claude")
    assert rec["argv"] == _claude_args()
    assert rec["stdin"] == "[User]\nhi there"
    assert Path(rec["cwd"]).resolve() == work.resolve()
    assert rec["spillway_depth"] == "1"
    assert rec["finished"] is True and rec["exit_code"] == 0
    assert set(rec["env_present"]) == set(cli_upstream.PRESENCE_VARS)
    assert rec["env_present"]["ANTHROPIC_BASE_URL"] is True
    assert rec["env_present"]["ANTHROPIC_API_KEY"] is False
    # 只記是否存在,不記值
    for p in fake_cli.records_dir.iterdir():
        assert FAKE_SECRET not in p.read_text(encoding="utf-8")


def test_claude_defaults_without_control_file(fake_cli, monkeypatch):
    monkeypatch.delenv("SPILLWAY_DEPTH", raising=False)
    cp = _run(fake_cli, "claude", _claude_args(), stdin="x" * 400)
    data = json.loads(cp.stdout)
    assert cp.returncode == 0
    assert data["result"] and data["is_error"] is False
    assert data["usage"]["input_tokens"] > 0 and data["usage"]["output_tokens"] > 0
    assert data["total_cost_usd"] > 0
    assert fake_cli.records("claude")[0]["spillway_depth"] is None


@pytest.mark.parametrize("scenario,quota,rate", [
    ("quota", True, False), ("rate_limit", False, True), ("error", False, False)])
def test_claude_error_scenarios_match_spec_531(fake_cli, scenario, quota, rate):
    fake_cli.set("claude", scenario)
    cp = _run(fake_cli, "claude", _claude_args(), stdin="hi")
    assert cp.returncode != 0
    data = json.loads(cp.stdout)
    assert data["is_error"] is True
    assert bool(CLAUDE_QUOTA.search(data["result"])) is quota
    assert bool(CLAUDE_RATE.search(data["result"])) is rate


def test_claude_non_json(fake_cli):
    fake_cli.set("claude", "non_json")
    cp = _run(fake_cli, "claude", _claude_args(), stdin="hi")
    with pytest.raises(ValueError):
        json.loads(cp.stdout)


# ---- codex ----

def test_codex_ok_jsonl_and_output_file(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_DEPTH", "1")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_SECRET)
    out = tmp_path / "last.txt"
    out.write_text("", encoding="utf-8")
    fake_cli.set("codex", "ok", result="codex says hi", input_tokens=21, output_tokens=5)
    args = _codex_args(out)
    cp = _run(fake_cli, "codex", args, stdin="")
    assert cp.returncode == 0, cp.stderr
    events = _jsonl(cp.stdout)
    assert _last_usage(events) == {"input_tokens": 21, "cached_input_tokens": 0, "output_tokens": 5}
    msgs = [e["item"]["text"] for e in events if e.get("type") == "item.completed"]
    assert msgs == ["codex says hi"]
    assert out.read_text(encoding="utf-8") == "codex says hi"

    (rec,) = fake_cli.records("codex")
    assert rec["argv"] == args
    assert rec["argv"][-2] == "--"
    assert rec["spillway_depth"] == "1"
    assert rec["env_present"]["OPENAI_API_KEY"] is True
    assert rec["env_present"]["CODEX_API_KEY"] is False
    for p in fake_cli.records_dir.iterdir():
        assert FAKE_SECRET not in p.read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario,quota,rate", [
    ("quota", True, False), ("rate_limit", False, True), ("error", False, False)])
def test_codex_error_scenarios_match_spec_531(fake_cli, tmp_path, scenario, quota, rate):
    out = tmp_path / "last.txt"
    out.write_text("", encoding="utf-8")
    fake_cli.set("codex", scenario)
    cp = _run(fake_cli, "codex", _codex_args(out))
    assert cp.returncode != 0
    assert bool(CODEX_QUOTA.search(cp.stderr)) is quota
    assert bool(CODEX_RATE.search(cp.stderr)) is rate
    assert out.read_text(encoding="utf-8") == ""


def test_codex_non_json(fake_cli, tmp_path):
    fake_cli.set("codex", "non_json")
    cp = _run(fake_cli, "codex", _codex_args(tmp_path / "o.txt"))
    with pytest.raises(ValueError):
        _jsonl(cp.stdout)


def test_codex_does_not_hang_on_open_stdin(fake_cli, tmp_path):
    proc = subprocess.Popen(["codex", *_codex_args(tmp_path / "o.txt")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, _ = proc.communicate(timeout=10)  # communicate 會關 stdin;另測不關的情況
    finally:
        proc.kill()
    assert proc.returncode == 0
    proc = subprocess.Popen(["codex", *_codex_args(tmp_path / "o2.txt")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.wait(timeout=10) == 0  # stdin 未關也會結束
    finally:
        proc.kill()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


# ---- 延遲、逾時、放行、孫行程 ----

@pytest.mark.parametrize("name", CLI_NAMES)
def test_delay_seconds(fake_cli, tmp_path, name):
    fake_cli.set(name, "ok", delay_seconds=0.6)
    args = _claude_args() if name == "claude" else _codex_args(tmp_path / "o.txt")
    t0 = time.monotonic()
    cp = _run(fake_cli, name, args, stdin="hi")
    assert cp.returncode == 0
    assert time.monotonic() - t0 >= 0.6


@pytest.mark.parametrize("name", CLI_NAMES)
def test_timeout_never_answers(fake_cli, tmp_path, name):
    fake_cli.set(name, "timeout")
    args = _claude_args() if name == "claude" else _codex_args(tmp_path / "o.txt")
    proc = subprocess.Popen([name, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(b"hi", timeout=1.0)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
    rec = fake_cli.records(name)[0]
    assert rec["finished"] is False


@pytest.mark.parametrize("name", CLI_NAMES)
def test_hold_until_release(fake_cli, tmp_path, name):
    fake_cli.set(name, "hold_until_release")
    args = _claude_args() if name == "claude" else _codex_args(tmp_path / "o.txt")
    proc = subprocess.Popen([name, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        proc.stdin.write("held prompt")
        proc.stdin.close()
        fake_cli.wait_for_records(name, 1)  # 已開始
        time.sleep(0.4)
        assert proc.poll() is None  # 尚未放行
        fake_cli.release(name)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    assert proc.returncode == 0
    if name == "claude":
        assert json.loads(out)["is_error"] is False
    else:
        assert _last_usage(_jsonl(out))
    # 重新設定情境會清掉放行
    fake_cli.set(name, "hold_until_release")
    assert "released" not in fake_cli.control(name)


@pytest.mark.parametrize("name", CLI_NAMES)
def test_grandchild_holds_stdout(fake_cli, tmp_path, name):
    fake_cli.set(name, "grandchild_holds_stdout", grandchild_sleep_seconds=30)
    args = _claude_args() if name == "claude" else _codex_args(tmp_path / "o.txt")
    proc = subprocess.Popen([name, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    gpid = None
    try:
        proc.stdin.write(b"hi")
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0  # 子行程本身已結束
        (rec,) = fake_cli.wait_for_records(name, 1, finished=True)
        gpid = rec["grandchild_pid"]
        os.kill(gpid, 0)  # 孫行程還活著
        fd = proc.stdout.fileno()
        buf = b""
        deadline = time.monotonic() + 0.8
        eof = False
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    eof = True
                    break
                buf += chunk
        assert not eof  # 孫行程握著 stdout,讀不到 EOF
        assert buf  # 父行程的回應已寫出
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if gpid is not None:
            try:
                os.kill(gpid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.stdout.close()


def test_concurrent_calls_have_separate_records(fake_cli):
    fake_cli.set("claude", "ok", delay_seconds=0.3)
    procs = [subprocess.Popen(["claude", *_claude_args()], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    outs = [p.communicate(f"prompt-{i}", timeout=20) for i, p in enumerate(procs)]
    assert all(p.returncode == 0 for p in procs), outs
    recs = fake_cli.records("claude")
    assert len(recs) == 2
    assert sorted(r["stdin"] for r in recs) == ["prompt-0", "prompt-1"]
    assert len({r["pid"] for r in recs}) == 2
    assert len(list(fake_cli.records_dir.glob("claude-*.json"))) == 2


def test_unknown_scenario_rejected_by_helper(fake_cli):
    with pytest.raises(ValueError):
        fake_cli.set("claude", "nope")
    with pytest.raises(ValueError):
        fake_cli.set("gemini", "ok")


# ---- 護欄(L2)----

def test_fake_cli_fixture_puts_only_fakes_on_path(fake_cli):
    for name in CLI_NAMES:
        assert shutil.which(name) == str(fake_cli.path(name))
    assert real_cli_dirs() == []
    assert os.environ["PATH"].split(os.pathsep)[0] == str(fake_cli.bin_dir)


def test_without_fake_cli_nothing_is_found():
    for name in CLI_NAMES:
        assert shutil.which(name) is None
    assert real_cli_dirs() == []


def test_guard_functions_detect_and_remove_simulated_real(tmp_path):
    real = _simulated_real(tmp_path / "real-bin")
    only_claude = _simulated_real(tmp_path / "real-claude-only", names=("claude",))
    fake_bin = install(tmp_path / "fake-bin")
    other = tmp_path / "other"
    other.mkdir()
    raw = os.pathsep.join([str(real), str(other), str(only_claude), os.environ["PATH"]])
    with pytest.raises(RealCliFound):
        assert_only_fakes(raw)
    assert set(real_cli_dirs(raw)) == {str(real), str(only_claude)}
    clean = sanitize_path(raw)
    assert str(real) not in clean.split(os.pathsep)
    assert str(only_claude) not in clean.split(os.pathsep)
    assert str(other) in clean.split(os.pathsep)
    assert assert_only_fakes(clean) == {"claude": None, "codex": None}
    # 假執行檔排在真實的後面也一樣會被擋下(真實目錄仍在 PATH 上)
    with pytest.raises(RealCliFound):
        assert_only_fakes(os.pathsep.join([str(fake_bin), str(real)]))


def test_safe_subprocess_env(tmp_path):
    real = _simulated_real(tmp_path / "real-bin")
    fake_bin = install(tmp_path / "fake-bin")
    base = {**os.environ, "PATH": os.pathsep.join([str(real), os.environ["PATH"]])}
    env = safe_subprocess_env(base, fake_bin=fake_bin)
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(fake_bin) and str(real) not in parts
    for name in CLI_NAMES:
        assert shutil.which(name, path=env["PATH"]) == str(fake_bin / name)
    env2 = safe_subprocess_env(base)
    assert all(shutil.which(n, path=env2["PATH"]) is None for n in CLI_NAMES)


def _inner_env(tmp_path: Path, real: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(real), env["PATH"]])
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["SIM_REAL_DIR"] = str(real)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def test_pytest_guard_fails_test_that_puts_real_cli_on_path(tmp_path):
    real = _simulated_real(tmp_path / "real-bin")
    proj = tmp_path / "inner"
    proj.mkdir()
    (proj / "conftest.py").write_text(
        "from tests.conftest import _spillway_home, _real_cli_guard, fake_cli  # noqa: F401\n",
        encoding="utf-8")
    (proj / "test_inner.py").write_text(textwrap.dedent("""
        import os, shutil

        def test_sanitized_at_start():
            # 外層 PATH 最前面放了模擬的真實執行檔;護欄在測試開始前已移除
            assert os.environ["SIM_REAL_DIR"] not in os.environ["PATH"].split(os.pathsep)
            assert shutil.which("claude") is None and shutil.which("codex") is None

        def test_fake_cli_fixture(fake_cli):
            assert shutil.which("claude") == str(fake_cli.path("claude"))

        def test_puts_real_back(monkeypatch):
            monkeypatch.setenv("PATH", os.environ["SIM_REAL_DIR"] + os.pathsep + os.environ["PATH"])
    """), encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(proj),
         str(proj / "test_inner.py")],
        cwd=proj, env=_inner_env(tmp_path, real), capture_output=True, text=True, timeout=120)
    out = cp.stdout + cp.stderr
    assert cp.returncode != 0, out
    assert "3 passed" in out and "1 error" in out, out  # 測試本體通過,護欄在 teardown 讓它失敗
    assert "RealCliFound" in out
    assert "ERROR test_inner.py::test_puts_real_back" in out
    assert "SIMULATED-REAL-CLI-MUST-NOT-RUN" not in out


def test_behave_guard_fails_scenario_that_puts_real_cli_on_path(tmp_path):
    real = _simulated_real(tmp_path / "real-bin")
    feats = tmp_path / "features"
    (feats / "steps").mkdir(parents=True)
    (feats / "environment.py").write_text(
        "from tests.features.environment import *  # noqa: F401,F403\n", encoding="utf-8")
    (feats / "steps" / "guard_steps.py").write_text(textwrap.dedent("""
        import os, shutil
        from behave import step

        @step("PATH 已清理")
        def _clean(context):
            assert os.environ["SIM_REAL_DIR"] not in os.environ["PATH"].split(os.pathsep)
            assert shutil.which("claude") is None and shutil.which("codex") is None

        @step("把假 CLI 放到 PATH 最前面")
        def _fake(context):
            os.environ["PATH"] = str(context.fake_cli_bin) + os.pathsep + os.environ["PATH"]
            assert shutil.which("claude") == str(context.fake_cli_bin / "claude")

        @step("把模擬的真實 CLI 放回 PATH")
        def _real(context):
            os.environ["PATH"] = os.environ["SIM_REAL_DIR"] + os.pathsep + os.environ["PATH"]
    """), encoding="utf-8")

    def run(feature_text: str) -> subprocess.CompletedProcess:
        (feats / "g.feature").write_text(feature_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "behave", "--no-capture", str(feats)],
            cwd=REPO_ROOT, env=_inner_env(tmp_path, real), capture_output=True, text=True,
            timeout=120)

    ok = run("Feature: g\n  Scenario: clean\n    Given PATH 已清理\n    And 把假 CLI 放到 PATH 最前面\n"
             "  Scenario: still clean\n    Given PATH 已清理\n")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = run("Feature: g\n  Scenario: bad\n    Given 把模擬的真實 CLI 放回 PATH\n")
    out = bad.stdout + bad.stderr
    assert bad.returncode != 0, out
    assert "RealCliFound" in out
    assert "SIMULATED-REAL-CLI-MUST-NOT-RUN" not in out
