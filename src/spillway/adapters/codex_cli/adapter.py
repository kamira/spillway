"""codex_cli 模組的 adapter:以 `codex exec` 子行程處理請求(spec 7.4、5.2.3、5.3.1、6.3;
CHG-20261101-03 決策 9、11、12)。

命令(決策 11,argv 陣列、不經 shell;子行程一律經 `AdapterContext.cli_runner.run()` 啟動):

    codex exec --json --skip-git-repo-check -m <model> -o <暫存檔>
          [-c model_reasoning_effort=<值>]      # effort 有對應值時(none→minimal、max→xhigh)
          -c mcp_servers={}                     # 固定附加:停用 codex 全部 MCP,extra_args 無法移除
          <params.extra_args...>
          -- <prompt>                           # prompt 以 `--` 隔開,開頭為 `-` 或 `resume` 也不會被當成旗標或子命令

- prompt:messages 依 spec 7.5.1 串成 `[System]`/`[User]`/`[Assistant]` 段落;stdin 不給(DEVNULL)。
- `-o` 暫存檔:`tempfile.mkstemp`(0600)建立,`finally` 刪除(含錯誤、逾時、取消)。
- 環境(決策 9):以目前環境為底,移除 `ENV_REMOVE`(`OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`),
  設 `SPILLWAY_DEPTH=1`。其他成員 `env:` 憑證變數由 Router 經 `CliRunner.extra_env_remove` 移除(T6),
  本模組不處理;官方登入狀態照常繼承,閘道不讀取任何憑證。
- 工作目錄:`params.cwd`;未設定時為 `AdapterContext.data_dir / "work" / "codex"`(不存在就建立);
  兩者皆無時回 `error`,不起子行程。
- 結果:結束碼 0 → 回應取 `-o` 檔內容、token 取 stdout JSONL 中最後一個含 `usage` 的事件、`cost_usd`
  為 None(spec 5.2.3)。結束碼 ≠ 0 → raw `{exit_code, stderr, message}` 交 `classify()`(spec 5.3.1)。
  逾時、停機終止、找不到執行檔、`OSError`(含 argv 過長 E2BIG)、stdout 不是 JSONL、`-o` 檔為空 → `error`。
- `health()` 不起子行程,回 True(可用性只依上游訊號,spec 5.2.3)。

模組只可 import `spillway.adapters.base`、標準庫與 pyyaml(spec 7.6.3)。
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
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
    effort_to_upstream,
    resolve_signals,
)

__all__ = ["CodexCliAdapter", "ENV_REMOVE", "ENV_SET", "EXECUTABLE", "MCP_OFF",
           "build_argv", "messages_to_prompt", "last_usage"]

EXECUTABLE = "codex"
ENV_REMOVE = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")   # 決策 9(codex 固定清單)
ENV_SET = {"SPILLWAY_DEPTH": "1"}
MCP_OFF = ("-c", "mcp_servers={}")                                    # 決策 11:固定停用全部 MCP
WORK_SUBDIR = ("work", "codex")
_ERROR_TAIL = 1000   # 錯誤訊息只留最後這麼多字元

_HEADERS = {"system": "[System]", "developer": "[System]", "user": "[User]",
            "assistant": "[Assistant]"}


def _error(message: str) -> CallResult:
    return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                      status="error", error=message)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content
                 if isinstance(p, Mapping) and p.get("type") in ("text", "input_text")]
        return "\n".join(p for p in parts if isinstance(p, str))
    return str(content)


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """spec 7.5.1:依序串成純文字,每段以 `[System]`、`[User]`、`[Assistant]` 標頭開頭。

    `developer` 視同 `system`;其他角色(tool 等)不送 CLI(含 tools 的請求已由 4.2.2 篩除)。
    """
    out = []
    for m in messages:
        header = _HEADERS.get(str(m.get("role", "")))
        if header is not None:
            out.append(f"{header}\n{_content_text(m.get('content'))}")
    return "\n\n".join(out)


def build_argv(model: str, output_file: str, upstream_effort: str | None,
               extra_args: list[str], prompt: str) -> list[str]:
    argv = [EXECUTABLE, "exec", "--json", "--skip-git-repo-check", "-m", model, "-o", output_file]
    if upstream_effort is not None:
        argv += ["-c", f"model_reasoning_effort={upstream_effort}"]
    argv += [*MCP_OFF, *extra_args, "--", prompt]
    return argv


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
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


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _ERROR_TAIL else "…" + text[-_ERROR_TAIL:]


def _failure_message(out: CliOutput, events: list[dict[str, Any]]) -> str:
    """結束碼 ≠ 0 時的訊息:JSONL 最後一個 error 事件的 message,其次 stderr。"""
    for e in reversed(events):
        if e.get("type") in ("error", "turn.failed"):
            msg = e.get("message")
            if msg is None and isinstance(e.get("error"), Mapping):
                msg = e["error"].get("message")
            if isinstance(msg, str) and msg.strip():
                return msg
    return out.stderr


class CodexCliAdapter:
    """`codex exec` 子行程 adapter;`create(context)` 建立,`cli_runner` 為 None 時 `call` 回 `error`。"""

    def __init__(self, context: AdapterContext, manifest: AdapterManifest) -> None:
        self.context = context
        self.manifest = manifest

    def _params(self, dep: Deployment) -> dict[str, Any]:
        return {**self.manifest.default_params, **(dep.params or {})}

    def _cwd(self, params: Mapping[str, Any]) -> Path | str:
        """回傳工作目錄;無法決定時回錯誤訊息字串。"""
        raw = params.get("cwd")
        if raw:
            return Path(str(raw)).expanduser()
        if self.context.data_dir is None:
            return "未設定 params.cwd,且 AdapterContext.data_dir 未注入:無法決定 codex 的工作目錄"
        cwd = Path(self.context.data_dir).joinpath(*WORK_SUBDIR)
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"無法建立工作目錄 {cwd}:{type(exc).__name__}: {exc.strerror or exc}"
        return cwd

    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult:
        if dep.kind not in self.manifest.kinds:
            return _error(f"此模組不支援 kind {dep.kind!r}")
        runner = self.context.cli_runner
        if runner is None:
            return _error("cli_runner 未注入:無法啟動 codex 子行程")
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
            timeout = float(params["timeout"]) if params.get("timeout") is not None else None
        except (TypeError, ValueError):
            return _error("params.timeout 必須是數字(秒)")
        extra = params.get("extra_args") or []
        if not isinstance(extra, list | tuple) or not all(isinstance(a, str) for a in extra):
            return _error("params.extra_args 必須是字串清單")
        cwd = self._cwd(params)
        if isinstance(cwd, str):
            return _error(cwd)
        prompt = messages_to_prompt(req.messages)

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
                return _error(f"無法啟動 codex 子行程:{type(exc).__name__}"
                              f"(errno {exc.errno}):{exc.strerror or '未知原因'}")
            try:
                text = Path(out_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        finally:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass
        return self._result(out, text, signals, timeout)

    @staticmethod
    def _result(out: CliOutput, text: str, signals: Mapping[str, Any],
                timeout: float | None) -> CallResult:
        if out.killed_reason == "shutdown":
            return _error("codex 子行程因閘道停機被終止(shutdown)")
        if out.timed_out:
            return _error(f"codex 逾時(超過 {timeout:g} 秒)" if timeout is not None else "codex 逾時")
        events = _events(out.stdout)
        if out.exit_code != 0:
            message = _failure_message(out, events)
            raw = {"exit_code": out.exit_code, "stderr": out.stderr, "message": message}
            return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                              status=classify(raw, signals),
                              error=_tail(message) or f"codex 結束碼 {out.exit_code}")
        if not events:
            return _error("codex 輸出不是 JSONL(stdout 沒有可解析的事件)")
        if not text.strip():
            return _error("codex 沒有寫出回應(-o 檔為空)")
        usage = last_usage(events) or {}
        return CallResult(ok=True, text=text, tokens_in=_int_or_none(usage.get("input_tokens")),
                          tokens_out=_int_or_none(usage.get("output_tokens")), cost_usd=None,
                          status="ok", error=None, finish_reason="stop")

    async def health(self, dep: Deployment) -> bool:
        """不起子行程、回 True:codex 成員的可用性只依上游訊號判斷(spec 5.2.3)。"""
        return True
