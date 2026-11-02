"""`ClaudeCliAdapter`:經 `AdapterContext.cli_runner` 執行 `claude -p`(spec 7.3、7.5;決策 9、10、12)。

`call()` 流程:
1. 成員 `kind` 不是 `cli` → `error`;`context.cli_runner` 為 None → `error`(不起子行程)。
2. `params` = manifest `default_params` 加成員 `params`;`params.signals` 覆寫 `default_signals`;
   `params.timeout` 為 bool、非有限或 ≤ 0 → `error`(None 表示擁有者明確取消上限)。
3. 工作目錄:`params.cwd`;未設定時為 `context.data_dir / "work" / "claude"`(不存在就建立);
   `data_dir` 也為 None → `error`(spec 7.3.4)。
4. argv(`build_argv`):`claude -p --output-format json --model <model>` + effort 參數 + **固定**
   `MCP_OFF`(`--strict-mcp-config`)+ `params.extra_args`。effort 參數由 `params.effort_args` 模板把
   `{effort}` 換成 `effort_map` 的上游值;effort 為 None、`none`(對應 null)或 `effort_args` 為空時不傳
   (spec 6.3.2)。隔離旗標不在 `extra_args` 內,成員自訂 `extra_args` 不會移除它(spec 7.3.3)。
   `model` 以 `-` 開頭時拒絕(不可被當成旗標)。
5. prompt 以 `adapters.base.messages_to_prompt`(spec 7.5.1)串成,經 stdin 傳入(spec 7.3.1);
   結果為空字串時回 `error`、不起子行程。環境移除 `ENV_REMOVE`(決策 9 的固定清單)並設
   `SPILLWAY_DEPTH=1`。
6. 回應(spec 7.3.5、5.2.2):JSON `result` → `text`;`tokens_in` = `usage.input_tokens` +
   `cache_creation_input_tokens` + `cache_read_input_tokens`(各項只計非負整數,三者皆缺為 None;
   施工審議 T4 補記);`tokens_out` = `usage.output_tokens`;`total_cost_usd`(有限且 ≥ 0)→ `cost_usd`。
   `--verbose` 時 CLI 輸出事件陣列,取最後一個 `type == "result"` 的物件。
7. 歸類(決策 12):逾時、被停機終止、輸出非 JSON、缺少 result 事件或被截斷、找不到執行檔與其他
   `OSError` → `error`;`is_error` 為 true 或結束碼 ≠ 0 → `classify()`(raw 欄位 `is_error`、`message`、
   `exit_code`、`stderr`)。`message` 只在 `is_error` 為 true 時取 JSON `result`,否則取 stderr
   (模型的回答文字不進判定,避免答案內含 "rate limit" 被誤判)。

秘密:不讀取、不解析任何 CLI 憑證檔或 OAuth 變數;官方登入變數照常由子行程繼承。
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
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
    cli_timeout,
    effort_to_upstream,
    head,
    messages_to_prompt,
    nonneg_int,
    resolve_signals,
)
from spillway.adapters.base.cli_common import cli_cwd

__all__ = ["CLAUDE_DEFAULT_SIGNALS", "ClaudeCliAdapter", "ENV_REMOVE", "ENV_SET", "EXECUTABLE",
           "MCP_OFF", "build_argv", "messages_to_prompt", "parse_output", "tokens_in_of"]

EXECUTABLE = "claude"
# 決策 9:API 金鑰與改道變數會讓計費或連線偏離訂閱登入
ENV_REMOVE = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
ENV_SET = {"SPILLWAY_DEPTH": "1"}           # spec 7.3.3
MCP_OFF = ("--strict-mcp-config",)          # 決策 10:固定附加,不載入任何 MCP
EFFORT_PLACEHOLDER = "{effort}"
MESSAGE_LIMIT = 2000   # 錯誤訊息保留的字元上限
_INPUT_TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# spec 5.3.1 claude-cli 列
CLAUDE_DEFAULT_SIGNALS: dict[str, list[dict[str, Any]]] = {
    "quota": [{"equals": {"is_error": True}, "match": {"message": "credit|usage limit|extra usage"}}],
    "rate_limit": [{"match": {"message": "rate limit|overloaded"}}],
}


def _error(message: str) -> CallResult:
    return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                      status="error", error=message)


def _str_list(v: Any, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list | tuple) or not all(isinstance(a, str) for a in v):
        raise ValueError(f"params.{name} 必須是字串清單")
    return list(v)


def _cost(v: Any) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        f = float(v)
        if math.isfinite(f) and f >= 0:
            return f
    return None


def tokens_in_of(usage: Mapping[str, Any]) -> int | None:
    """input + cache_creation + cache_read(各項只計非負整數);三者皆缺為 None(施工審議 T4 補記)。"""
    values = [n for n in (nonneg_int(usage.get(k)) for k in _INPUT_TOKEN_FIELDS) if n is not None]
    return sum(values) if values else None



def build_argv(model: str, upstream_effort: str | None, effort_args: list[str],
               extra_args: list[str]) -> list[str]:
    """argv(spec 7.3.1;決策 10)。`model` 為空或以 `-` 開頭時丟 `ValueError`。"""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("成員 model 未設定")
    if model.startswith("-"):
        raise ValueError(f"成員 model 不可以 '-' 開頭:{model!r}")
    argv = [EXECUTABLE, "-p", "--output-format", "json", "--model", model]
    if upstream_effort is not None:
        argv += [a.replace(EFFORT_PLACEHOLDER, upstream_effort) for a in effort_args]
    argv += list(MCP_OFF)
    argv += list(extra_args)
    return argv


class _NoResult(ValueError):
    """JSON 合法,但 `--verbose` 事件陣列中沒有 result 事件。"""


def parse_output(stdout: str) -> dict[str, Any] | None:
    """`--output-format json` 的輸出 → 結果物件。非 JSON 回 None;事件陣列沒有 result 事件時丟
    `_NoResult`(ValueError)。"""
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
        raise _NoResult("輸出缺少 result 事件")
    return None


class ClaudeCliAdapter:
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
            return _error("閘道未注入子行程執行器(cli_runner),無法執行 claude")
        params = self._params(dep)
        try:
            signals = resolve_signals(self.manifest.default_signals, dep.params)
        except SignalsError as exc:
            return _error(f"params.signals 不合法:{exc}")
        try:
            timeout = cli_timeout(params.get("timeout"))   # adapters.base 共用驗證
            argv = build_argv(dep.model, effort_to_upstream(self.manifest, effort),
                              _str_list(params.get("effort_args"), "effort_args"),
                              _str_list(params.get("extra_args"), "extra_args"))
            prompt = messages_to_prompt(req.messages)
            if not prompt:
                raise ValueError("沒有可送出的文字訊息")
            cwd = cli_cwd(params, self.context, EXECUTABLE)
        except (ValueError, OSError) as exc:
            return _error(str(exc))

        try:
            out = await runner.run(argv, stdin=prompt, env_remove=ENV_REMOVE,
                                   env_set=dict(ENV_SET), cwd=cwd, timeout=timeout)
        except FileNotFoundError:
            return _error(f"找不到 {EXECUTABLE} 執行檔(PATH 上沒有 {EXECUTABLE},或工作目錄 {cwd} 不存在)")
        except OSError as exc:
            return _error(f"無法啟動 {EXECUTABLE}:{type(exc).__name__}: {exc.strerror or exc}")
        return self._result(out, signals, timeout)

    @staticmethod
    def _result(out: CliOutput, signals: Mapping[str, Any], timeout: float | None) -> CallResult:
        if out.killed_reason == "shutdown":
            return _error(f"{EXECUTABLE} 子行程因閘道停機被終止(shutdown)")
        if out.timed_out:
            return _error(f"{EXECUTABLE} 逾時(超過 {timeout:g} 秒)已終止" if timeout is not None
                          else f"{EXECUTABLE} 逾時已終止")
        if out.stdout_truncated:
            return _error(f"{EXECUTABLE} 輸出超過上限,已截斷,無法解析")
        try:
            body = parse_output(out.stdout)
        except _NoResult as exc:
            return _error(f"{EXECUTABLE} {exc}(結束碼 {out.exit_code})")
        if body is None:
            detail = head(out.stderr or "", MESSAGE_LIMIT)
            return _error(f"{EXECUTABLE} 輸出不是 JSON(結束碼 {out.exit_code})"
                          + (f":{detail}" if detail else ""))
        result = body.get("result")
        is_error = body.get("is_error") is True
        if is_error or out.exit_code != 0:
            if is_error and isinstance(result, str) and result.strip():
                message = result
            else:
                message = out.stderr or ""
            raw = {"is_error": is_error, "message": message, "exit_code": out.exit_code,
                   "stderr": out.stderr}
            text = head(message, MESSAGE_LIMIT) or f"{EXECUTABLE} 回報錯誤(結束碼 {out.exit_code})"
            return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                              status=classify(raw, signals), error=text)
        if not isinstance(result, str):
            return _error(f"{EXECUTABLE} 回應缺少 result 文字")
        usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
        return CallResult(ok=True, text=result, tokens_in=tokens_in_of(usage),
                          tokens_out=nonneg_int(usage.get("output_tokens")),
                          cost_usd=_cost(body.get("total_cost_usd")),
                          status="ok", error=None, finish_reason="stop")

    async def health(self, dep: Deployment) -> bool:
        """不探測、不起子行程,回 True;可用性由 `call()` 的 status 與冷卻處理。"""
        return True
