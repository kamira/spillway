"""codex_cli 模組的 adapter:以 `codex exec` 子行程處理請求(spec 7.4、5.2.3、5.3.1、6.3;
CHG-20261101-03 決策 9、11、12)。完整說明見 README.md。

    codex exec --json --skip-git-repo-check --ignore-user-config -m <model> -o <暫存檔>
          [-c model_reasoning_effort=<值>] <params.extra_args...> -c mcp_servers={} -- <prompt>

- `--ignore-user-config` 與 `-c mcp_servers={}` 固定附加(決策 11 與擁有者冒煙補記:`-c mcp_servers={}`
  與 config.toml 合併而非取代,故另不讀 config.toml);`extra_args` 不可含 `--`、裸位置引數、
  設定 `mcp_servers` 的 `-c`/`--config` 或 `-p`/`--profile`(`_extra_args_problem`)。子行程只經 `AdapterContext.cli_runner.run()` 啟動。
- `-o` 暫存檔以 `tempfile.mkstemp`(0600)建立,`finally` 刪除。環境移除 `ENV_REMOVE`、設 `SPILLWAY_DEPTH=1`
  (其他成員 `env:` 憑證變數由 Router 移除)。
- prompt 以 `adapters.base.messages_to_prompt` 串成(決策 12a);為空時回 `error`、不起子行程。
- 結束碼 ≠ 0 時 stderr 與 stdout JSONL 的錯誤訊息一併交 `classify()`(spec 5.3.1)。
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spillway.adapters.base import (
    AdapterContext,
    AdapterManifest,
    CallResult,
    ChatRequest,
    CliOutput,
    Deployment,
    SignalsError,
    classify,
    cli_cwd,
    cli_timeout,
    effort_to_upstream,
    messages_to_prompt,
    nonneg_int,
    resolve_signals,
    tail,
)

__all__ = ["CodexCliAdapter", "ENV_REMOVE", "ENV_SET", "IGNORE_USER_CONFIG", "MCP_OFF", "messages_to_prompt"]

EXECUTABLE = "codex"
ENV_REMOVE = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")   # 決策 9(codex 固定清單)
ENV_SET = {"SPILLWAY_DEPTH": "1"}
MCP_OFF = ("-c", "mcp_servers={}")                                    # 決策 11:固定停用全部 MCP
IGNORE_USER_CONFIG = "--ignore-user-config"   # 決策 11 補記:不讀 $CODEX_HOME/config.toml(其中的 MCP)
_ERROR_TAIL = 1000   # 錯誤訊息只留最後這麼多字元

def _error(message: str) -> CallResult:
    return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                      status="error", error=message)


# 會加回 MCP、選用 profile 設定檔或開啟功能(如提供 codex_apps MCP 的功能;擁有者冒煙 2026-11-02)的 `-c` 鍵
_BLOCKED_CONFIG_KEYS = ("mcp_servers", "profile", "features")


def _config_key(value: str) -> str:
    """`-c` 值的鍵(`=` 前),去掉所有空白與引號。"""
    return "".join(value.split("=", 1)[0].split()).replace('"', "").replace("'", "")


def _blocked_config(value: str) -> bool:
    key = _config_key(value)
    return any(key == k or key.startswith(k + ".") for k in _BLOCKED_CONFIG_KEYS)


def _extra_args_problem(extra: Any) -> str | None:
    """`extra_args` 不合法時回原因:非字串清單、含 `--`、含裸位置引數(不以 `-` 開頭,且前一個元素
    不是不帶 `=` 的選項;位置引數會被 codex 當成 prompt 或子命令如 `resume`)、以 `-c`/`--config`
    設定 `mcp_servers`(會與 `mcp_servers={}` 合併而加回 MCP)、`profile` 或 `features`,`--enable`(開啟功能,
    可能加回 codex_apps 等 MCP),或 `-p`/`--profile`(含 `-pNAME`、`-p=NAME`;疊加 profile 設定檔)。`-c` 的鍵去掉空白與引號後以整段比對(`mcp_servers_timeout` 不擋)。"""
    if not isinstance(extra, Sequence) or isinstance(extra, str) \
            or not all(isinstance(a, str) for a in extra):
        return "params.extra_args 必須是字串清單"
    for i, a in enumerate(extra):
        if a == "--":
            return "params.extra_args 不可含 `--`"
        if a in ("-p", "--profile") or a.startswith(("-p", "--profile=")):
            return "params.extra_args 不可含 `-p`/`--profile`"
        if a == "--enable" or a.startswith("--enable="):
            return "params.extra_args 不可含 `--enable`"
        if a.startswith("--config="):
            value = a[len("--config="):]
        elif a.startswith("-c") and a != "-c":
            value = a[2:].removeprefix("=")   # `-cKEY=V` 與 `-c=KEY=V`
        elif i > 0 and extra[i - 1] in ("-c", "--config"):
            value = a
        else:
            value = None
        if value is not None and _blocked_config(value):
            return "params.extra_args 不可以 -c 設定 mcp_servers、profile 或 features"
        if not a.startswith("-"):
            prev = extra[i - 1] if i > 0 else ""
            if not (prev.startswith("-") and "=" not in prev):
                return f"params.extra_args 第 {i} 個元素是位置引數,只可為選項或選項的值"
    return None


def build_argv(model: str, output_file: str, upstream_effort: str | None,
               extra_args: Sequence[str], prompt: str) -> list[str]:
    argv = [EXECUTABLE, "exec", "--json", "--skip-git-repo-check", IGNORE_USER_CONFIG,
            "-m", model, "-o", output_file]
    if upstream_effort is not None:
        argv += ["-c", f"model_reasoning_effort={upstream_effort}"]
    argv += [*extra_args, *MCP_OFF, "--", prompt]   # MCP_OFF 為 `--` 前最後一個 -c
    return argv


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def last_usage(events: list[dict[str, Any]]) -> Mapping[str, Any] | None:
    """JSONL 事件中最後一個含 `usage`(dict)的事件的 usage;沒有時 None(spec 7.4.2)。"""
    for e in reversed(events):
        usage = e.get("usage")
        if isinstance(usage, Mapping):
            return usage
    return None


def _jsonl_failure(events: list[dict[str, Any]]) -> str:
    """stdout JSONL 中最後一個 error/turn.failed 事件的訊息;沒有時空字串。"""
    for e in reversed(events):
        if e.get("type") in ("error", "turn.failed"):
            msg = e.get("message")
            if msg is None and isinstance(e.get("error"), Mapping):
                msg = e["error"].get("message")
            if isinstance(msg, str) and msg.strip():
                return msg
    return ""


class CodexCliAdapter:
    """`codex exec` 子行程 adapter;`create(context)` 建立,`cli_runner` 為 None 時 `call` 回 `error`。"""

    def __init__(self, context: AdapterContext, manifest: AdapterManifest) -> None:
        self.context = context
        self.manifest = manifest

    def _params(self, dep: Deployment) -> dict[str, Any]:
        return {**self.manifest.default_params, **(dep.params or {})}

    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult:
        if dep.kind not in self.manifest.kinds:
            return _error(f"此模組不支援 kind {dep.kind!r}")
        runner = self.context.cli_runner
        if runner is None:
            return _error(f"cli_runner 未注入:無法啟動 {EXECUTABLE} 子行程")
        params = self._params(dep)
        try:
            signals = resolve_signals(self.manifest.default_signals, dep.params)
        except SignalsError as exc:
            return _error(f"params.signals 不合法:{exc}")
        try:
            upstream_effort = effort_to_upstream(self.manifest, effort)
        except ValueError as exc:
            return _error(str(exc))
        try:
            timeout = cli_timeout(params.get("timeout"))   # 與 claude_cli 同一驗證(終審 I1a)
        except ValueError as exc:
            return _error(str(exc))
        extra = params.get("extra_args") or []
        problem = _extra_args_problem(extra)
        if problem is not None:
            return _error(problem)
        prompt = messages_to_prompt(req.messages)   # adapters.base 共用轉換(spec 7.5.1;決策 12a)
        if not prompt:
            return _error("沒有可送出的文字訊息")
        try:
            cwd = cli_cwd(params, self.context, EXECUTABLE)
        except ValueError as exc:
            return _error(str(exc))

        try:
            fd, out_path = tempfile.mkstemp(prefix="spillway-codex-", suffix=".txt")   # 0600
        except OSError as exc:
            return _error(f"無法建立 -o 暫存檔:{type(exc).__name__}: {exc.strerror or exc}")
        try:
            os.close(fd)
            argv = build_argv(dep.model, out_path, upstream_effort, list(extra), prompt)
            try:
                out = await runner.run(argv, env_remove=ENV_REMOVE, env_set=ENV_SET,
                                       cwd=cwd, timeout=timeout)
            except OSError as exc:
                # 找不到執行檔、argv 過長(E2BIG)、cwd 不存在等;訊息不含 argv(prompt)
                return _error(f"無法啟動 {EXECUTABLE} 子行程:{type(exc).__name__}"
                              f"(errno {exc.errno}):{exc.strerror or '未知原因'}")
            except ValueError:
                # argv 含 NUL 字元(execve 不接受);訊息不含 argv
                return _error(f"無法啟動 {EXECUTABLE} 子行程:引數含 NUL 字元")
            try:
                text = Path(out_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        return self._result(out, text, signals, timeout)

    @staticmethod
    def _result(out: CliOutput, text: str, signals: Mapping[str, Any],
                timeout: float | None) -> CallResult:
        if out.killed_reason == "shutdown":
            return _error(f"{EXECUTABLE} 子行程因閘道停機被終止(shutdown)")
        if out.timed_out:
            if timeout is None:
                return _error(f"{EXECUTABLE} 逾時")
            return _error(f"{EXECUTABLE} 逾時(超過 {timeout:g} 秒)")
        events = _events(out.stdout)
        if out.exit_code != 0:
            jsonl_msg = _jsonl_failure(events)
            message = jsonl_msg or out.stderr
            # stdout JSONL 的錯誤訊息也納入比對:額度/限流只出現在 JSONL 時仍能歸類
            matched = "\n".join(s for s in (out.stderr, jsonl_msg) if s)
            raw = {"exit_code": out.exit_code, "stderr": matched, "message": message}
            return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                              status=classify(raw, signals),
                              error=tail(message, _ERROR_TAIL) or f"{EXECUTABLE} 結束碼 {out.exit_code}")
        if not events:
            return _error(f"{EXECUTABLE} 輸出不是 JSONL(stdout 沒有可解析的事件)")
        if not text.strip():
            return _error(f"{EXECUTABLE} 沒有寫出回應(-o 檔為空)")
        usage = last_usage(events) or {}
        return CallResult(ok=True, text=text, tokens_in=nonneg_int(usage.get("input_tokens")),
                          tokens_out=nonneg_int(usage.get("output_tokens")), cost_usd=None,
                          status="ok", error=None, finish_reason="stop")

    async def health(self, dep: Deployment) -> bool:
        """不起子行程、回 True:codex 成員的可用性只依上游訊號判斷(spec 5.2.3)。"""
        return True
