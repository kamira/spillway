r"""疑似金鑰字串的遮罩(spec 2.4.4;CHG-20261030-02 T3)。

遮罩規則(依序套用,命中處以固定字串 ``MASK`` 取代):

1. ``extra_secrets``:呼叫端傳入的明確秘密(例如 ``credential_ref`` 當次解析出的值),
   逐字取代所有出現處,長者先取代。**空字串與 strip 後少於 4 個字元的值會被忽略**——
   這麼短的值極可能出現在一般文字中,遮掉只會破壞內容,也不像真正的金鑰。
   每個值同時遮罩其 ``strip()`` 後的形式(上游與 HTTP 層常回顯修剪後的值)。
2. ``sk-`` 規則:``sk-`` 後接至少 4 個 ``[A-Za-z0-9_.+/=-]``(含 base64 常見的
   ``.``、``+``、``/``、``=``,避免只遮半截)。前一個字元若是英數字(如 ``task-1234``)
   則不算,避免把一般單字切開;但下列跳脫/編碼前綴雖以英數字結尾,仍視為邊界而遮罩:
   JSON 跳脫 ``\n``、``\r``、``\t``、``\b``、``\f``、``\"``、``\/``、``\uXXXX``,
   以及 URL 編碼 ``%XX``(如 ``%3D``、``%22``)。payload 以 JSON 文字儲存,換行後的
   金鑰在文字中就是 ``\nsk-…``。
3. ``Bearer`` 規則(不分大小寫):``Bearer`` 加分隔(任意空白含換行與 NBSP、``:``、
   ``=``)之後的值,到空白、引號(``"``、``'``)或換行為止;只取代值,保留 ``Bearer``
   與分隔字樣。已是 ``MASK`` 的值不重複計為命中,因此 ``redact`` 對自己的輸出是冪等的(第二次 ``hit=False``)。

已知限制(CHG「安全影響」):非 ``sk-`` 格式的金鑰只能靠 ``extra_secrets`` 遮罩;
長度 < ``MIN_SECRET_LEN`` 的 extra_secrets 不遮罩;金鑰本身若被 URL 編碼(如內含
``%2F``),只遮到第一個 ``%`` 之前。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["MASK", "MIN_SECRET_LEN", "Redactor"]

MASK = "[REDACTED]"
MIN_SECRET_LEN = 4

_SK_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])"  # 一般邊界:前一字元不是英數字
    r"|(?<=\\[nrtbf\"/])"  # JSON 跳脫 \n \r \t \b \f \" \/
    r"|(?<=\\u[0-9A-Fa-f]{4})"  # JSON \uXXXX
    r"|(?<=%[0-9A-Fa-f]{2}))"  # URL 編碼 %XX
    r"sk-[A-Za-z0-9_.+/=-]{4,}"
)
_BEARER_RE = re.compile(
    r"(\bbearer[\s:=]+)(?!" + re.escape(MASK) + r"(?=[\s\"']|$))[^\s\"']+",
    re.IGNORECASE,
)


class Redactor:
    """以固定規則遮罩文字中的疑似金鑰。無狀態,可共用同一實例。"""

    def redact(self, text: str, extra_secrets: Iterable[str] = ()) -> tuple[str, bool]:
        """回傳 ``(遮罩後文字, 是否有命中)``;沒有命中時原文不變。

        ``extra_secrets`` 中的每個值連同其 ``strip()`` 形式都會遮罩;``strip()`` 後長度 <
        ``MIN_SECRET_LEN``(4)的值(含空字串)整個忽略。
        """
        hit = False
        # 長度門檻以 strip 後為準:「 ab 」這種值實質只有 2 個字元,不遮
        secrets = sorted(
            {
                v
                for s in extra_secrets
                if isinstance(s, str) and len(s.strip()) >= MIN_SECRET_LEN
                for v in (s, s.strip())
            },
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
