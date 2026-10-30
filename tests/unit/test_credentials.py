"""credentials:`credential_ref` 解析與格式驗證(CHG-20261030-02 T3;spec 2.1.3、7.2.2)。"""
from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from spillway.credentials import CredentialError, resolve, validate_ref

FAKE_KEY = "sk-test-0000"


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


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"backend exploded while holding {FAKE_KEY}")


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


def _assert_no_secret(exc: CredentialError, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in str(exc)
        assert secret not in repr(exc)
        assert all(secret not in str(arg) for arg in exc.args)


# ---- resolve: env ----

def test_resolve_env_reads_variable(monkeypatch):
    monkeypatch.setenv("SPILLWAY_TEST_KEY", FAKE_KEY)
    assert resolve("env:SPILLWAY_TEST_KEY") == FAKE_KEY


def test_resolve_env_missing_variable_raises_without_value(monkeypatch):
    monkeypatch.delenv("SPILLWAY_TEST_KEY", raising=False)
    with pytest.raises(CredentialError) as info:
        resolve("env:SPILLWAY_TEST_KEY")
    _assert_no_secret(info.value, FAKE_KEY)


def test_resolve_env_empty_value_raises(monkeypatch):
    monkeypatch.setenv("SPILLWAY_TEST_KEY", "")
    with pytest.raises(CredentialError):
        resolve("env:SPILLWAY_TEST_KEY")


# ---- resolve: keyring ----

def test_resolve_keyring_reads_from_backend(memory_keyring):
    memory_keyring.set_password("spillway-test", "acct", FAKE_KEY)
    assert resolve("keyring:spillway-test:acct") == FAKE_KEY


def test_resolve_keyring_account_may_contain_colon(memory_keyring):
    memory_keyring.set_password("spillway-test", "team:acct", FAKE_KEY)
    assert resolve("keyring:spillway-test:team:acct") == FAKE_KEY


def test_resolve_keyring_missing_entry_raises(memory_keyring):
    with pytest.raises(CredentialError):
        resolve("keyring:spillway-test:nobody")


def test_resolve_keyring_backend_error_is_wrapped_without_leaking(monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    try:
        with pytest.raises(CredentialError) as info:
            resolve("keyring:spillway-test:acct")
    finally:
        keyring.set_keyring(previous)
    _assert_no_secret(info.value, FAKE_KEY)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_resolve_invalid_ref_raises_without_echo():
    with pytest.raises(CredentialError) as info:
        resolve(FAKE_KEY)
    _assert_no_secret(info.value, FAKE_KEY)


# ---- validate_ref ----

@pytest.mark.parametrize(
    "ref",
    [
        "env:SPILLWAY_TEST_KEY",
        "env:_X1",
        "keyring:spillway:acct",
        "keyring:spillway:team:acct",
    ],
)
def test_validate_ref_accepts_valid_formats(ref, monkeypatch):
    monkeypatch.delenv("SPILLWAY_TEST_KEY", raising=False)
    # 只驗格式:變數不存在也不丟例外
    validate_ref(ref)


@pytest.mark.parametrize(
    "ref",
    [
        "sk-abc",
        FAKE_KEY,
        "",
        "env:",
        "env:1BAD",
        "env:HAS-DASH",
        "env:A B",
        "keyring:",
        "keyring:svc",
        "keyring::acct",
        "keyring:svc:",
        "file:/tmp/key",
        "ENV:SPILLWAY_TEST_KEY",
        " env:SPILLWAY_TEST_KEY",
        "Bearer abcdef",
    ],
)
def test_validate_ref_rejects_bad_formats_without_echo(ref):
    with pytest.raises(CredentialError) as info:
        validate_ref(ref)
    # 只有前綴(`env:`、`keyring:`)或空字串時沒有可回顯的內容,格式說明本身含前綴
    if ref.strip() not in ("", "env:", "keyring:"):
        _assert_no_secret(info.value, ref.strip())


def test_validate_ref_rejects_non_string():
    with pytest.raises(CredentialError):
        validate_ref(None)  # type: ignore[arg-type]


def test_validate_ref_does_not_resolve(memory_keyring, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(memory_keyring, "get_password", lambda s, u: calls.append((s, u)))
    validate_ref("keyring:spillway-test:acct")
    assert calls == []
