"""疑似金鑰字串的遮罩(spec 2.4.4;CHG-20261030-02 T3)。

遮罩規則(依序套用,命中處以固定字串 ``MASK`` 取代):

1. ``extra_secrets``:呼叫端傳入的明確秘密(例如 ``credential_ref`` 當次解析出的值),
   逐字取代所有出現處,長者先取代。**空字串與少於 4 個字元的值會被忽略**——
   這麼短的值極可能出現在一般文字中,遮掉只會破壞內容,也不像真正的金鑰。
2. ``sk-`` 規則:``sk-`` 後接至少 4 個 ``[A-Za-z0-9_-]``。前一個字元若是英數字
   (如 ``task-1234``)則不算,避免把一般單字切開。
3. ``Bearer`` 規則(不分大小寫):``Bearer`` 加空白或 tab 之後的值,到空白、引號
   (``"``、``'``)或換行為止;只取代值,保留 ``Bearer`` 字樣。已是 ``MASK`` 的值不重複
   計為命中,因此 ``redact`` 對自己的輸出是冪等的(第二次 ``hit=False``)。

已知限制(CHG「安全影響」):非 ``sk-`` 格式的金鑰只能靠 ``extra_secrets`` 遮罩。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["MASK", "MIN_SECRET_LEN", "Redactor"]

MASK = "[REDACTED]"
MIN_SECRET_LEN = 4

_SK_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{4,}")
_BEARER_RE = re.compile(
    r"(\bbearer[ \t]+)(?!" + re.escape(MASK) + r"(?=[\s\"']|$))[^\s\"']+",
    re.IGNORECASE,
)


class Redactor:
    """以固定規則遮罩文字中的疑似金鑰。無狀態,可共用同一實例。"""

    def redact(self, text: str, extra_secrets: Iterable[str] = ()) -> tuple[str, bool]:
        """回傳 ``(遮罩後文字, 是否有命中)``;沒有命中時原文不變。

        ``extra_secrets`` 中的空字串與長度 < ``MIN_SECRET_LEN``(4)的值會被忽略。
        """
        hit = False
        secrets = sorted(
            {s for s in extra_secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, MASK)
                hit = True

        text, n = _SK_RE.subn(MASK, text)
        hit = hit or n > 0
        text, n = _BEARER_RE.subn(lambda m: m.group(1) + MASK, text)
        hit = hit or n > 0
        return text, hit

    def matches(self, text: str) -> bool:
        """文字是否命中 ``sk-`` 或 ``Bearer`` 規則(供寫入驗證,不含 extra_secrets)。"""
        return bool(_SK_RE.search(text) or _BEARER_RE.search(text))
