"""`credential_ref` 的格式驗證與呼叫當下解析(spec 2.1.3、7.2.2;CHG-20261030-02 T3)。

支援兩種參照:

- ``env:VAR``:讀環境變數 ``VAR``(``[A-Za-z_][A-Za-z0-9_]*``)。
- ``keyring:服務:帳號``:以 ``keyring`` 套件讀取;服務名不可含 ``:``,帳號取第二個
  ``:`` 之後的全部(可含 ``:``),兩者皆不可為空。

秘密處理:``CredentialError`` 的訊息與 ``args`` 一律不含憑證值,也不回顯呼叫者傳入的
ref 字串(格式錯誤的 ref 可能就是誤貼的金鑰本體)。底層例外以 ``from None`` 切斷,
避免 traceback 帶出後端訊息。

解析出的值一律 ``strip()``:前後空白或結尾換行(例如檔案讀入的值)會讓 HTTP 層送出、
上游回顯的是修剪後的形式,原樣回傳會讓 ``extra_secrets`` 逐字遮罩對不上。strip 後為空
視為未設定。
"""
from __future__ import annotations

import os
import re

__all__ = ["CredentialError", "resolve", "validate_ref"]

_ENV_RE = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]*)")
_KEYRING_RE = re.compile(r"keyring:([^:]+):(.+)", re.DOTALL)


class CredentialError(Exception):
    """憑證參照無效或無法解析。訊息只描述原因,不含值與 ref 本體。"""


def _parse(ref: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(ref, str):
        raise CredentialError("credential_ref 必須是字串")
    m = _ENV_RE.fullmatch(ref)
    if m:
        return "env", (m.group(1),)
    m = _KEYRING_RE.fullmatch(ref)
    if m:
        return "keyring", (m.group(1), m.group(2))
    raise CredentialError("credential_ref 格式無效:必須為 env:變數名 或 keyring:服務:帳號")


def validate_ref(ref: str) -> None:
    """只驗證格式,不讀取環境變數或 keyring。格式錯誤丟 ``CredentialError``。"""
    _parse(ref)


def resolve(ref: str) -> str:
    """依 ``credential_ref`` 解析出憑證值;值會 ``strip()``;找不到或 strip 後為空時丟 ``CredentialError``。"""
    scheme, parts = _parse(ref)
    if scheme == "env":
        value = (os.environ.get(parts[0]) or "").strip()
        if not value:
            raise CredentialError("env 憑證的環境變數未設定或為空")
        return value

    service, account = parts
    try:
        import keyring

        value = keyring.get_password(service, account)
    except Exception:  # noqa: BLE001 — 後端錯誤訊息可能含敏感內容,一律改寫
        raise CredentialError("keyring 憑證讀取失敗") from None
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise CredentialError("keyring 中找不到該憑證或值為空")
    return value
