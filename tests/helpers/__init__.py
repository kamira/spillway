"""全測試共用的輔助(CHG-20261101-02 T11;決策 19)。

只放跨目錄重複的測試輔助;各目錄專屬的輔助仍留在原處(`tests/unit/routing/helpers.py`、
`tests/unit/adapters/cases.py`、`tests/integration/admin/helpers.py`)。

- `TEST_KEY`、`TEST_KEY_ENV`:測試假金鑰與其環境變數名(Global Constraints:`sk-test-0000`)。
- `fake_key_env`:fixture,把 `TEST_KEY_ENV` 設為 `TEST_KEY`。根 conftest 已註冊(非 autouse);
  測試模組以 `pytest.mark.usefixtures("fake_key_env")` 套用。
- `api_member(dep_id, base_url, ...)`、`local_member(dep_id, base_url, ...)`:`Deployment` 建構器,
  `api_base` 指向假上游 `{base_url}/v1`。
- `MemoryKeyring`、`memory_keyring`:記憶體 keyring 後端與換上它的 fixture(不碰系統 Keychain);
  測試模組 `from tests.helpers import memory_keyring` 即可使用。
- `dead_base()`:一個當下沒有服務在聽的 127.0.0.1 埠的 `api_base`(`.../v1`)。
"""
from __future__ import annotations

import socket
from collections.abc import Iterable, Iterator
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from spillway.domain import Deployment

__all__ = ["TEST_KEY", "TEST_KEY_ENV", "fake_key_env", "api_member", "local_member",
           "MemoryKeyring", "memory_keyring", "dead_base", "redact_tokens"]

TEST_KEY = "sk-test-0000"
TEST_KEY_ENV = "SPILLWAY_TEST_KEY"


@pytest.fixture
def fake_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    return TEST_KEY


# ---- 成員建構器 ----

def api_member(dep_id: str, base_url: str, *, model: str = "gpt-4o-mini",
               model_name: str | None = None, tags: tuple[str, ...] = ("chat",),
               **kw: Any) -> Deployment:
    """openai provider 的 api 成員,`api_base` 指向假上游(`{base_url}/v1`),憑證 `env:SPILLWAY_TEST_KEY`。"""
    return Deployment(
        id=dep_id, model_name=model_name or model, kind="api", provider="openai", model=model,
        params={"api_base": f"{base_url}/v1"}, credential_ref=f"env:{TEST_KEY_ENV}",
        capabilities={"ctx": 128000, "tools": True, "vision": False, "efforts": []},
        tags=tags, **kw,
    )


def local_member(dep_id: str, base_url: str, *, model: str = "local-model",
                 model_name: str | None = None, tags: tuple[str, ...] = ("chat",),
                 **kw: Any) -> Deployment:
    """provider `hosted_vllm` 的 local 成員(CHG-20261030-02 決策 10),`api_base` 指向假上游。"""
    return Deployment(
        id=dep_id, model_name=model_name or model, kind="local", provider="hosted_vllm",
        model=model, params={"api_base": f"{base_url}/v1"}, credential_ref=None,
        capabilities={"ctx": 32768, "tools": False, "vision": False, "efforts": []},
        tags=tags, **kw,
    )


# ---- keyring ----

class MemoryKeyring(KeyringBackend):
    """測試用記憶體 keyring 後端,不碰系統 Keychain。"""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError(username) from exc


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


# ---- 網路 ----

def dead_base() -> str:
    """一個當下沒有服務在聽的 127.0.0.1 埠(綁定後立即關閉)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def redact_tokens(text: str, tokens: Iterable[str | None] | None) -> str:
    """把 `text` 中每個 token 明文換成 `<token>`(CHG-20261102-02 決策 14;失敗訊息帶出 serve stderr
    或回應本文時一律經它,避免測試輸出洩漏 token)。空字串與 None 略過;其餘文字不變。"""
    for tok in tokens or ():
        if tok:
            text = text.replace(tok, "<token>")
    return text
