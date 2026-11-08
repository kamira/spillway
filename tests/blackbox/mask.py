"""測試端的遮罩函式與假金鑰常數(CHG-20261107-03 T2;決策 8 ⑦、11;AM-9、AM-15)。

預期值不取自產品程式(AM-9):本模組自帶遮罩規則,不 import `spillway.redact`。規則刻意比產品寬鬆或相同
即可——它只用於「測試自己寫出的檔案與失敗訊息不帶秘密」,不是產品遮罩的 oracle;驗證產品遮罩的情境
一律走真實請求並以字面值斷言(spec 2.4.4;種入不經遮罩,13.4.3)。

- `TEST_KEY`、`TEST_KEY_ENV`:測試假金鑰 `sk-test-0000` 與其環境變數名 `SPILLWAY_TEST_KEY`
  (與 `tests/helpers` 相同的值)。
- `MASK`:遮罩後的固定字串 `[REDACTED]`。
- `mask_secrets(text, extra=()) -> str`:依序把 `extra` 中的每個值(長者先,空字串與少於 4 字元者略過)、
  `sk-` 開頭的金鑰樣字串、`Bearer <值>` 的值換成 `MASK`。
- `redact_tokens(text, tokens) -> str`:把每個 token 明文換成 `<token>`(失敗訊息帶出 serve stderr 或回應
  本文時用;同 `tests/helpers.redact_tokens`)。
- `redact(text, secrets)`:`redact_tokens` 的別名(MCP 驅動沿用的名稱)。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["MASK", "TEST_KEY", "TEST_KEY_ENV", "mask_secrets", "redact", "redact_tokens"]

TEST_KEY = "sk-test-0000"
TEST_KEY_ENV = "SPILLWAY_TEST_KEY"
MASK = "[REDACTED]"
_MIN_LEN = 4

_SK_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_.+/=-]{4,}")
_BEARER_RE = re.compile(r"(\bbearer[\s:=]+)[^\s\"']+", re.IGNORECASE)


def mask_secrets(text: str, extra: Iterable[str | None] = ()) -> str:
    for secret in sorted({s for s in extra if s and len(s.strip()) >= _MIN_LEN}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    text = _SK_RE.sub(MASK, text)
    return _BEARER_RE.sub(lambda m: m.group(1) + MASK, text)


def redact_tokens(text: str, tokens: str | Iterable[str | None] | None) -> str:
    if isinstance(tokens, str):
        tokens = [tokens]
    for tok in sorted({t for t in tokens or () if t}, key=len, reverse=True):   # 長的先換,避免殘留尾巴
        text = text.replace(tok, "<token>")
    return text


redact = redact_tokens
