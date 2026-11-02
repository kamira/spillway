"""假 `claude` / `codex` 執行檔本體(CHG-20261101-03 T3;決策 18)。

由 `install()` 產生的 wrapper 以 `<sys.executable> -I fake.py <claude|codex> <args...>` 執行;只用標準庫。

- 控制檔:`$SPILLWAY_HOME/fake_cli/<name>.json`(每次呼叫重新讀取,長駐的 serve 也能切換);
  沒有控制檔時為 `scenario=ok`、`delay_seconds=0`。欄位見 `tests/fakes/cli_upstream/__init__.py`。
- 記錄檔:每次呼叫一個 `$SPILLWAY_HOME/fake_cli/records/<name>-<ns>-<pid>-<rand>.json`;開始時寫一次
  (`finished=false`,可作為「已開始」的訊號),結束前以 rename 原子覆寫(`finished=true`、`exit_code`)。
  只記指定變數「是否存在」,不記值。
- 等待(延遲、`timeout`、`hold_until_release`、孫行程)期間控制目錄消失即結束;`hold_until_release` 最多等
  `HOLD_MAX_SECONDS`(120 秒)。
"""
from __future__ import annotations

import json
import os
import secrets
import select
import subprocess
import sys
import time
from pathlib import Path

PRESENCE_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SPILLWAY_TEST_KEY",   # 成員 env 憑證(決策 9 由 Router 逐次移除);只記是否存在
)

# spec 5.3.1 預設訊號:claude-cli 額度 `credit|usage limit|extra usage`、限流 `rate limit|overloaded`;
# codex-cli 額度 `usage limit|quota`(結束碼 ≠ 0)、限流 `rate limit|429`。各訊息只符合自己那一類。
CLAUDE_MESSAGES = {
    "quota": "Claude AI usage limit reached. Your limit will reset at 5pm (UTC).",
    "rate_limit": "API Error: rate limit exceeded, please retry later.",
    "error": "fake claude internal failure",
}
CODEX_MESSAGES = {
    "quota": "ERROR: You've hit your usage limit. Try again later.",
    "rate_limit": "ERROR: exceeded retry limit, last status: 429 Too Many Requests",
    "error": "ERROR: fake codex internal failure",
}

POLL_SECONDS = 0.02
HOLD_MAX_SECONDS = 120.0


def _control_dir() -> Path:
    home = os.environ.get("SPILLWAY_HOME")
    if not home:
        sys.stderr.write("fake cli: SPILLWAY_HOME 未設定\n")
        sys.exit(97)
    return Path(home) / "fake_cli"


def _read_control(cdir: Path, name: str) -> dict:
    try:
        data = json.loads((cdir / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_stdin(wait: bool) -> str:
    """讀完 stdin;tty 不讀。`wait=False`(codex,prompt 在 argv)時 0.2 秒內不可讀即視為空,避免卡住;
    claude 的 prompt 經 stdin(spec 7.3.1),一律讀到 EOF。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        if not wait:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not ready:
                return ""
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".tmp-{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _gone(cdir: Path) -> None:
    """控制目錄消失(測試已結束、暫存目錄已刪)時立即結束,不留孤兒行程。"""
    if not cdir.is_dir():
        sys.exit(96)


def _sleep(seconds: float, cdir: Path) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds or 0))
    while (left := deadline - time.monotonic()) > 0:
        _gone(cdir)
        time.sleep(min(POLL_SECONDS * 10, left))


def _hold(cdir: Path, name: str) -> None:
    deadline = time.monotonic() + HOLD_MAX_SECONDS
    while not _read_control(cdir, name).get("released"):
        _gone(cdir)
        if time.monotonic() > deadline:
            sys.stderr.write("fake cli: hold_until_release 超過上限仍未放行\n")
            sys.exit(95)
        time.sleep(POLL_SECONDS)


_GRANDCHILD = """\
import os, sys, time
cdir, deadline = sys.argv[1], time.monotonic() + float(sys.argv[2])
while time.monotonic() < deadline and os.path.isdir(cdir):
    time.sleep(0.2)
"""


def _spawn_grandchild(seconds: float, cdir: Path) -> int:
    # 繼承 stdout/stderr 的孫行程:父行程結束後 stdout 仍未 EOF;控制目錄消失即結束。
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _GRANDCHILD, str(cdir), repr(float(seconds))],
        stdin=subprocess.DEVNULL,
    )
    return proc.pid


def _codex_output_file(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in ("-o", "--output-last-message") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--output-last-message="):
            return a.split("=", 1)[1]
    return None


def _codex_prompt(args: list[str]) -> str:
    return args[-1] if args else ""


def _claude_ok(ctl: dict, stdin: str) -> tuple[str, str, int]:
    text = ctl.get("result", "fake claude reply")
    body = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": text,
        "usage": {
            "input_tokens": int(ctl.get("input_tokens", max(1, len(stdin) // 4))),
            "output_tokens": int(ctl.get("output_tokens", 12)),
        },
        "total_cost_usd": float(ctl.get("total_cost_usd", 0.0123)),
        "session_id": "fake-session",
    }
    return json.dumps(body), "", 0


def _claude_error(ctl: dict, scenario: str) -> tuple[str, str, int]:
    body = {
        "type": "result", "subtype": "success", "is_error": True,
        "result": ctl.get("result", CLAUDE_MESSAGES[scenario]),
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "total_cost_usd": 0.0,
        "session_id": "fake-session",
    }
    return json.dumps(body), "", 1


def _codex_ok(ctl: dict, prompt: str, out_file: str | None) -> tuple[str, str, int]:
    text = ctl.get("result", "fake codex reply")
    if out_file:
        Path(out_file).write_text(text, encoding="utf-8")
    events = [
        {"type": "thread.started", "thread_id": "fake-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {
            "input_tokens": int(ctl.get("input_tokens", max(1, len(prompt) // 4))),
            "cached_input_tokens": 0,
            "output_tokens": int(ctl.get("output_tokens", 9)),
        }},
    ]
    return "".join(json.dumps(e) + "\n" for e in events), "", 0


def _codex_error(ctl: dict, scenario: str) -> tuple[str, str, int]:
    msg = ctl.get("result", CODEX_MESSAGES[scenario])
    events = [{"type": "thread.started", "thread_id": "fake-thread"},
              {"type": "error", "message": msg}]
    return "".join(json.dumps(e) + "\n" for e in events), msg + "\n", 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("claude", "codex"):
        sys.stderr.write("fake cli: 第一個引數必須是 claude 或 codex\n")
        return 98
    name, args = argv[0], argv[1:]
    cdir = _control_dir()
    rdir = cdir / "records"
    rdir.mkdir(parents=True, exist_ok=True)
    ctl = _read_control(cdir, name)
    scenario = ctl.get("scenario", "ok")
    stdin = _read_stdin(wait=(name == "claude"))

    record = {
        "name": name, "argv": args, "stdin": stdin, "cwd": os.getcwd(), "pid": os.getpid(),
        "scenario": scenario, "started_at": time.time(),
        "spillway_depth": os.environ.get("SPILLWAY_DEPTH"),
        "env_present": {v: v in os.environ for v in PRESENCE_VARS},
        "finished": False, "exit_code": None,
    }
    rpath = rdir / f"{name}-{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.json"
    _write_json_atomic(rpath, record)

    _sleep(float(ctl.get("delay_seconds", 0) or 0), cdir)

    if scenario == "timeout":
        _sleep(float(ctl.get("timeout_sleep_seconds", 3600)), cdir)
        return 0
    if scenario == "hold_until_release":
        _hold(cdir, name)
    if scenario == "grandchild_holds_stdout":
        record["grandchild_pid"] = _spawn_grandchild(float(ctl.get("grandchild_sleep_seconds", 3600)), cdir)

    if scenario == "non_json":
        out, err, code = "this is not json {{{\n", "", 0
    elif scenario in ("quota", "rate_limit", "error"):
        out, err, code = (_claude_error(ctl, scenario) if name == "claude"
                          else _codex_error(ctl, scenario))
    elif scenario in ("ok", "hold_until_release", "grandchild_holds_stdout"):
        out, err, code = (_claude_ok(ctl, stdin) if name == "claude"
                          else _codex_ok(ctl, _codex_prompt(args), _codex_output_file(args)))
    else:
        out, err, code = "", f"fake cli: 未知情境 {scenario!r}\n", 99
    if "exit_code" in ctl:
        code = int(ctl["exit_code"])

    record.update(finished=True, exit_code=code)
    _write_json_atomic(rpath, record)
    sys.stdout.write(out)
    sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
