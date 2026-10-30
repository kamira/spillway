"""上游訊號判定:`classify(raw, signals)`(spec 5.3.1;決策 12)。

## raw:描述一次失敗的上游輸出(dict,欄位皆可省略)

| 欄位 | 型別 | 來源 |
|------|------|------|
| `http_status` | int \\| None | HTTP 狀態碼(`litellm_call` 由例外的 `status_code` 取得) |
| `error_code` | str \\| None | 上游錯誤碼,例如 `insufficient_quota` |
| `message` | str | 錯誤訊息;CLI 為 JSON `result`/錯誤文字 |
| `exit_code` | int \\| None | 子行程結束碼(CLI) |
| `stderr` | str \\| None | 子行程 stderr(CLI) |
| `is_error` | bool \\| None | claude-cli JSON 的 `is_error` |
| `timeout` | bool | 是否逾時 |
| `exception` | str | 例外類別名稱 |

模組可另加欄位,規則以欄位名稱引用即可。

## signals:判定規則(可存成 JSON;manifest 的 `default_signals`、成員的 `params.signals`)

```
{"quota":      [rule, ...],     # 命中任一條 → "quota_exhausted"
 "rate_limit": [rule, ...]}     # 命中任一條 → "rate_limited"
```

兩個鍵皆可省略。先比 `quota` 再比 `rate_limit`,都沒命中回 `"error"`。
一條 rule 是非空 dict,所列條件**全部**成立才算命中(AND;清單之間是 OR):

| 條件 | 值 | 成立條件 |
|------|----|----------|
| `http_status` | int 或 int 清單 | `raw.http_status` 在其中(數字字串視同數字) |
| `error_code` | str 或 str 清單 | `raw.error_code` 等於其一(不分大小寫) |
| `exit_code` | int 或 int 清單 | `raw.exit_code` 在其中 |
| `exit_code_not` | int 或 int 清單 | `raw.exit_code` 不是 None 且不在其中 |
| `match` | {欄位: 正規式} | 每個欄位存在且 `re.search(正規式, str(值), re.IGNORECASE)` 命中 |
| `equals` | {欄位: 值} | 每個欄位存在且 `raw[欄位] == 值` |

spec 5.3.1 三列的寫法:

```
api:        {"quota": [{"http_status": 402}, {"error_code": "insufficient_quota"}],
             "rate_limit": [{"http_status": 429}]}
claude-cli: {"quota": [{"equals": {"is_error": true},
                        "match": {"message": "credit|usage limit|extra usage"}}],
             "rate_limit": [{"match": {"message": "rate limit|overloaded"}}]}
codex-cli:  {"quota": [{"exit_code_not": 0, "match": {"stderr": "usage limit|quota"}}],
             "rate_limit": [{"match": {"stderr": "rate limit|429"}}]}
```

覆寫:`resolve_signals(default_signals, params)` 以 `params["signals"]` 逐鍵取代預設
(有給 `quota` 就整個取代預設的 `quota`,沒給的鍵沿用預設)。

規則不合法時 `validate_signals`、`classify`、`resolve_signals` 丟 `SignalsError`;
管理 API 寫入 `params.signals` 前應先 `validate_signals`。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

STATUS_FOR_KEY = {"quota": "quota_exhausted", "rate_limit": "rate_limited"}
RULE_CONDITIONS = ("http_status", "error_code", "exit_code", "exit_code_not", "match", "equals")

API_DEFAULT_SIGNALS: dict[str, list[dict[str, Any]]] = {
    "quota": [{"http_status": 402}, {"error_code": "insufficient_quota"}],
    "rate_limit": [{"http_status": 429}],
}
"""spec 5.3.1 的 api 列;anthropic_api、openai_api、litellm 模組的 `default_signals`。"""


class SignalsError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_list(v: Any, field: str) -> list[int]:
    items = v if isinstance(v, list | tuple) else [v]
    if not items or not all(_is_int(i) for i in items):
        raise SignalsError(field, "必須是整數或整數清單")
    return list(items)


def _str_list(v: Any, field: str) -> list[str]:
    items = v if isinstance(v, list | tuple) else [v]
    if not items or not all(isinstance(i, str) and i for i in items):
        raise SignalsError(field, "必須是字串或字串清單")
    return list(items)


def _validate_rule(rule: Any, field: str) -> None:
    if not isinstance(rule, Mapping) or not rule:
        raise SignalsError(field, "規則必須是非空 dict")
    for cond, value in rule.items():
        f = f"{field}.{cond}"
        if cond in ("http_status", "exit_code", "exit_code_not"):
            _int_list(value, f)
        elif cond == "error_code":
            _str_list(value, f)
        elif cond == "match":
            if not isinstance(value, Mapping) or not value:
                raise SignalsError(f, "必須是 {欄位: 正規式}")
            for k, pattern in value.items():
                if not isinstance(k, str) or not isinstance(pattern, str):
                    raise SignalsError(f"{f}.{k}", "正規式必須是字串")
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise SignalsError(f"{f}.{k}", f"正規式不合法:{e}") from None
        elif cond == "equals":
            if not isinstance(value, Mapping) or not value:
                raise SignalsError(f, "必須是 {欄位: 值}")
        else:
            raise SignalsError(f, f"未知的條件,可用:{', '.join(RULE_CONDITIONS)}")


def validate_signals(signals: Any, field: str = "signals") -> None:
    if not isinstance(signals, Mapping):
        raise SignalsError(field, "必須是 {quota: [...], rate_limit: [...]}")
    for key, rules in signals.items():
        f = f"{field}.{key}"
        if key not in STATUS_FOR_KEY:
            raise SignalsError(f, "未知的鍵,只可為 quota、rate_limit")
        if not isinstance(rules, list | tuple):
            raise SignalsError(f, "必須是規則清單")
        for i, rule in enumerate(rules):
            _validate_rule(rule, f"{f}[{i}]")


def resolve_signals(default_signals: Mapping[str, Any], params: Mapping[str, Any] | None) -> dict[str, Any]:
    """成員 `params.signals` 逐鍵覆寫模組的 `default_signals`(spec 5.3.1)。"""
    merged = dict(default_signals)
    override = (params or {}).get("signals")
    if override is None:
        return merged
    validate_signals(override, "params.signals")
    merged.update(override)
    return merged


def _as_int(v: Any) -> int | None:
    if _is_int(v):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _rule_hits(rule: Mapping[str, Any], raw: Mapping[str, Any]) -> bool:
    for cond, value in rule.items():
        if cond == "http_status":
            if _as_int(raw.get("http_status")) not in _int_list(value, cond):
                return False
        elif cond == "error_code":
            code = raw.get("error_code")
            if not isinstance(code, str) or code.lower() not in {c.lower() for c in _str_list(value, cond)}:
                return False
        elif cond == "exit_code":
            if _as_int(raw.get("exit_code")) not in _int_list(value, cond):
                return False
        elif cond == "exit_code_not":
            ec = _as_int(raw.get("exit_code"))
            if ec is None or ec in _int_list(value, cond):
                return False
        elif cond == "match":
            for k, pattern in value.items():
                v = raw.get(k)
                if v is None or not re.search(pattern, str(v), re.IGNORECASE):
                    return False
        elif cond == "equals":
            for k, expected in value.items():
                if k not in raw or raw[k] != expected:
                    return False
    return True


def classify(raw: Mapping[str, Any], signals: Mapping[str, Any]) -> str:
    """回傳 `quota_exhausted`、`rate_limited` 或 `error`。只用於已判定失敗的上游輸出。"""
    validate_signals(signals)
    for key in ("quota", "rate_limit"):
        for rule in signals.get(key, ()):
            if _rule_hits(rule, raw):
                return STATUS_FOR_KEY[key]
    return "error"
