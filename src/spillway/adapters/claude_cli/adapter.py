"""`ClaudeCliAdapter`:經 `AdapterContext.cli_runner` 執行 `claude -p`(spec 7.3、7.5;決策 9、10、12)。

`call()` 流程:
1. 成員 `kind` 不是 `cli` → `error`;`context.cli_runner` 為 None → `error`(不起子行程)。
2. `params` = manifest `default_params` 加成員 `params`;`params.signals` 覆寫 `default_signals`。
3. 工作目錄:`params.cwd`;未設定時為 `context.data_dir / "work" / "claude"`(不存在就建立);
   `data_dir` 也為 None → `error`(spec 7.3.4)。
4. argv:`claude -p --output-format json --model <model>` + effort 參數 + **固定** `--strict-mcp-config`
   + `params.extra_args`。effort 參數由 `params.effort_args` 模板把 `{effort}` 換成 `effort_map` 的
   上游值;effort 為 None、`none`(對應 null)或 `effort_args` 為空時不傳(spec 6.3.2)。隔離旗標不在
   `extra_args` 內,成員自訂 `extra_args` 不會移除它(spec 7.3.3;決策 10)。
5. prompt 依 spec 7.5.1 由 messages 串成,經 stdin 傳入(spec 7.3.1);環境移除 `CLAUDE_ENV_REMOVE`
   (決策 9 的固定清單)並設 `SPILLWAY_DEPTH=1`。
6. 回應(spec 7.3.5、5.2.2):JSON `result` → `text`、`usage.input_tokens`/`output_tokens` → token、
   `total_cost_usd` → `cost_usd`。`--verbose` 時 CLI 輸出事件陣列,取最後一個 `type == "result"` 的物件。
7. 歸類(決策 12):逾時、被停機終止(`error="shutdown"`)、輸出非 JSON 或被截斷、找不到執行檔與其他
   `OSError` → `error`;`is_error` 為 true 或結束碼 ≠ 0 → `classify()`(raw 欄位 `is_error`、`message`、
   `exit_code`、`stderr`)。

秘密:不讀取、不解析任何 CLI 憑證檔或 OAuth 變數;官方登入變數照常由子行程繼承。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spillway.adapters.base import (
    AdapterContext,
    AdapterManifest,
    CallResult,
    ChatRequest,
    Deployment,
    SignalsError,
    classify,
    effort_to_upstream,
    resolve_signals,
)

__all__ = ["CLAUDE_DEFAULT_SIGNALS", "CLAUDE_ENV_REMOVE", "CLI_NAME", "ClaudeCliAdapter",
           "messages_to_prompt"]

CLI_NAME = "claude"
WORK_SUBDIR = ("work", "claude")
STRICT_MCP_FLAG = "--strict-mcp-config"
EFFORT_PLACEHOLDER = "{effort}"
MESSAGE_LIMIT = 2000   # 錯誤訊息保留的字元上限

# 決策 9:API 金鑰與改道變數會讓計費或連線偏離訂閱登入
CLAUDE_ENV_REMOVE = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
ENV_SET = {"SPILLWAY_DEPTH": "1"}   # spec 7.3.3

# spec 5.3.1 claude-cli 列
CLAUDE_DEFAULT_SIGNALS: dict[str, list[dict[str, Any]]] = {
    "quota": [{"equals": {"is_error": True}, "match": {"message": "credit|usage limit|extra usage"}}],
    "rate_limit": [{"match": {"message": "rate limit|overloaded"}}],
}

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
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, Mapping) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return str(content)


def messages_to_prompt(messages: Sequence[Mapping[str, Any]]) -> str:
    """spec 7.5.1:依序串成純文字,每段以 `[System]`、`[User]`、`[Assistant]` 標頭開頭。"""
    blocks = []
    for m in messages:
        role = str(m.get("role") or "user")
        header = _HEADERS.get(role, f"[{role.capitalize()}]")
        blocks.append(f"{header}\n{_content_text(m.get('content'))}")
    return "\n\n".join(blocks)


def _str_list(v: Any, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list | tuple) or not all(isinstance(a, str) for a in v):
        raise ValueError(f"params.{name} 必須是字串清單")
    return list(v)


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _float_or_none(v: Any) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT] + "…"


def parse_output(stdout: str) -> dict[str, Any] | None:
    """`--output-format json` 的輸出 → 結果物件;非 JSON 或找不到結果物件回 None。"""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):   # --verbose:事件陣列,最後一個 result 事件為結果
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None


class ClaudeCliAdapter:
    def __init__(self, context: AdapterContext, manifest: AdapterManifest) -> None:
        self.context = context
        self.manifest = manifest

    def _params(self, dep: Deployment) -> dict[str, Any]:
        return {**self.manifest.default_params, **(dep.params or {})}

    def _cwd(self, params: Mapping[str, Any]) -> Path:
        cwd = params.get("cwd")
        if cwd:
            return Path(str(cwd)).expanduser()
        data_dir = self.context.data_dir
        if data_dir is None:
            raise ValueError("未設定 params.cwd,且閘道沒有注入資料目錄(data_dir),無法決定工作目錄")
        work = Path(data_dir).joinpath(*WORK_SUBDIR)
        work.mkdir(parents=True, exist_ok=True)
        return work

    def build_argv(self, dep: Deployment, params: Mapping[str, Any], effort: str | None) -> list[str]:
        """argv(spec 7.3.1;決策 10)。參數不合法時丟 `ValueError`。"""
        if not isinstance(dep.model, str) or not dep.model:
            raise ValueError("成員 model 未設定")
        argv = [CLI_NAME, "-p", "--output-format", "json", "--model", dep.model]
        upstream = effort_to_upstream(self.manifest, effort)
        effort_args = _str_list(params.get("effort_args"), "effort_args")
        if upstream is not None:
            argv += [a.replace(EFFORT_PLACEHOLDER, upstream) for a in effort_args]
        argv.append(STRICT_MCP_FLAG)
        argv += _str_list(params.get("extra_args"), "extra_args")
        return argv

    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult:
        if dep.kind not in self.manifest.kinds:
            return _error(f"此模組不支援 kind {dep.kind!r}")
        runner = self.context.cli_runner
        if runner is None:
            return _error("閘道未注入子行程執行器(cli_runner),無法執行 claude")
        params = self._params(dep)
        try:
            signals = resolve_signals(self.manifest.default_signals, dep.params)
        except SignalsError as exc:
            return _error(f"params.signals 不合法:{exc}")
        try:
            timeout = float(params["timeout"]) if params.get("timeout") is not None else None
        except (TypeError, ValueError):
            return _error("params.timeout 必須是數字(秒)")
        try:
            argv = self.build_argv(dep, params, effort)
            cwd = self._cwd(params)
        except (ValueError, OSError) as exc:
            return _error(str(exc))

        try:
            out = await runner.run(
                argv,
                stdin=messages_to_prompt(req.messages),
                env_remove=CLAUDE_ENV_REMOVE,
                env_set=dict(ENV_SET),
                cwd=cwd,
                timeout=timeout,
            )
        except FileNotFoundError:
            return _error(f"找不到 {CLI_NAME} 執行檔(PATH 上沒有 {CLI_NAME},或工作目錄 {cwd} 不存在)")
        except OSError as exc:
            return _error(f"無法啟動 {CLI_NAME}:{type(exc).__name__}: {exc.strerror or exc}")
        return self._result(out, signals, timeout)

    def _result(self, out: Any, signals: Mapping[str, Any], timeout: float | None) -> CallResult:
        if out.killed_reason == "shutdown":
            return _error("shutdown")
        if out.timed_out:
            return _error(f"{CLI_NAME} 逾時({timeout:g} 秒)已終止" if timeout is not None
                          else f"{CLI_NAME} 逾時已終止")
        if out.stdout_truncated:
            return _error(f"{CLI_NAME} 輸出超過上限,已截斷,無法解析")
        body = parse_output(out.stdout)
        if body is None:
            detail = _clip(out.stderr or "")
            return _error(f"{CLI_NAME} 輸出不是 JSON(結束碼 {out.exit_code})"
                          + (f":{detail}" if detail else ""))
        result = body.get("result")
        is_error = body.get("is_error") is True
        if is_error or out.exit_code != 0:
            message = result if isinstance(result, str) and result.strip() else (out.stderr or "")
            raw = {"is_error": is_error, "message": message, "exit_code": out.exit_code,
                   "stderr": out.stderr}
            text = _clip(str(message)) or f"{CLI_NAME} 回報錯誤(結束碼 {out.exit_code})"
            return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                              status=classify(raw, signals), error=text)
        if not isinstance(result, str):
            return _error(f"{CLI_NAME} 回應缺少 result 文字")
        usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
        return CallResult(ok=True, text=result,
                          tokens_in=_int_or_none(usage.get("input_tokens")),
                          tokens_out=_int_or_none(usage.get("output_tokens")),
                          cost_usd=_float_or_none(body.get("total_cost_usd")),
                          status="ok", error=None)

    async def health(self, dep: Deployment) -> bool:
        """不探測、不起子行程,回 True;可用性由 `call()` 的 status 與冷卻處理。"""
        return True
