"""`params` 與 `capabilities` 額外鍵的憑證掃描:鍵名與值樣式(CHG-20261030-02 決策 22、U9;
CHG-20261101-02 決策 1、4、9)。"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from spillway.api import redact_echo

from .common import _REDACTOR


# CHG-20261101-02 決策 1(使用者決定 2026-11-01,含決策審議 B2):鍵名先正規化(轉小寫、去掉
# `-`、`_`、`.`、空白)再比對;(a) 完全等於 CREDENTIAL_KEY_EXACT 之一,或 (b) 以
# CREDENTIAL_KEY_SUFFIXES 之一結尾,即視為憑證鍵。`key`、`token` 只做完全比對(不擋 `max_tokens`、
# `cache_key`)。原 U9 的 8 個鍵名正規化後皆命中。
CREDENTIAL_KEY_EXACT = frozenset({
    "apikey", "apikeys", "xapikey", "key", "token", "secret", "password", "passwd", "passphrase",
    "authorization", "bearer", "credential", "credentials",
})
CREDENTIAL_KEY_SUFFIXES = (
    "apikey", "apikeys", "secret", "password", "passwd", "passphrase", "secretkey", "privatekey",
    "accesskey", "apitoken", "accesstoken", "authtoken", "sessiontoken", "refreshtoken", "idtoken",
    "bearertoken",
)
_NORMALIZE_RE = re.compile(r"[-_.\s]+")


def normalize_key(name: str) -> str:
    """轉小寫並去掉 `-`、`_`、`.`、空白(決策 1)。"""
    return _NORMALIZE_RE.sub("", name.lower())


def is_credential_key(name: str) -> bool:
    """鍵名是否為憑證鍵(決策 1)。"""
    n = normalize_key(name)
    return n in CREDENTIAL_KEY_EXACT or n.endswith(CREDENTIAL_KEY_SUFFIXES)


def _secret_paths(value: Any, path: str) -> list[str]:
    """`value` 內疑似憑證的位置,遞迴檢查巢狀 dict 與 list:

    - 字串值命中 Redactor 樣式(`sk-…`、`Bearer …`);
    - 鍵名本身命中 Redactor 樣式(以 `<key>` 代替,不回顯);
    - 鍵名經 `is_credential_key` 判為憑證鍵(決策 1;`max_tokens`、`cache_key` 不算)。
    整棵樹一律檢查,`params.signals` 不豁免(決策 4)。
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
            if is_credential_key(ks):
                hits.append(f"{path}.{ks}")
                continue
            hits += _secret_paths(v, f"{path}.{ks}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hits += _secret_paths(v, f"{path}.{i}")
    return hits


def _secret_message(hits: list[str]) -> str:
    return redact_echo(f"疑似金鑰,請改用 credential_ref(位置:{', '.join(hits)})")
