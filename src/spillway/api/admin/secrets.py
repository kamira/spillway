"""`params` 與 `capabilities` 額外鍵的憑證掃描:鍵名與值樣式(CHG-20261030-02 決策 22、U9;
CHG-20261101-02 決策 9)。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spillway.api import redact_echo

from .common import _REDACTOR


# U9(使用者決定 2026-10-30):params 與 capabilities 額外鍵的鍵名(不分大小寫、完全相等)
CREDENTIAL_KEY_NAMES = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "secret", "password", "authorization",
})


def _secret_paths(value: Any, path: str) -> list[str]:
    """`value` 內疑似憑證的位置,遞迴檢查巢狀 dict 與 list:

    - 字串值命中 Redactor 樣式(`sk-…`、`Bearer …`);
    - 鍵名本身命中 Redactor 樣式(以 `<key>` 代替,不回顯);
    - 鍵名不分大小寫、**完全等於** `CREDENTIAL_KEY_NAMES` 之一(U9;`max_tokens` 不算)。
    回傳的位置只含鍵名,不含值。
    """
    hits: list[str] = []
    if isinstance(value, str):
        if _REDACTOR.matches(value):
            hits.append(path)
    elif isinstance(value, Mapping):
        for k, v in value.items():
            ks = str(k)
            if _REDACTOR.matches(ks):
                hits.append(f"{path}.<key>")
                continue
            if ks.lower() in CREDENTIAL_KEY_NAMES:
                hits.append(f"{path}.{ks}")
                continue
            hits += _secret_paths(v, f"{path}.{ks}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hits += _secret_paths(v, f"{path}.{i}")
    return hits


def _secret_message(hits: list[str]) -> str:
    return redact_echo(f"疑似金鑰,請改用 credential_ref(位置:{', '.join(hits)})")
