"""CHG-20261101-02 T11:測試輔助整併(決策 19)。

- L1:`tests/helpers.py` 的成員建構器產生的成員經 `repo.save_deployment` / `repo.load_deployment`
  往返後欄位相同。
- L2:`anyio_backend` 在 tests/ 只定義一次(根 conftest)。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from spillway.store import db as store_db
from spillway.store import repo
from tests.helpers import TEST_KEY, TEST_KEY_ENV, api_member, dead_base, local_member

TESTS_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("build", [api_member, local_member], ids=["api_member", "local_member"])
def test_builder_round_trips_through_repo(tmp_path, build):
    dep = build("d-1", "http://127.0.0.1:9", tags=("chat", "x"))
    conn = store_db.connect(tmp_path / "h.db")
    try:
        repo.save_deployment(conn, dep)
        loaded = repo.load_deployment(conn, "d-1")
    finally:
        conn.close()
    assert loaded == dep


def test_api_member_uses_fake_key_env():
    dep = api_member("a", "http://127.0.0.1:9")
    assert dep.credential_ref == f"env:{TEST_KEY_ENV}"
    assert dep.params == {"api_base": "http://127.0.0.1:9/v1"}


def test_fake_key_env_fixture(fake_key_env):
    assert fake_key_env == TEST_KEY == os.environ[TEST_KEY_ENV]


def test_dead_base_is_loopback_v1():
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/v1", dead_base())


def test_anyio_backend_defined_once_in_tests():
    pattern = re.compile(r"^\s*def anyio_backend\b", re.M)
    hits = [p.relative_to(TESTS_DIR).as_posix() for p in sorted(TESTS_DIR.rglob("*.py"))
            if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == ["conftest.py"]


def test_redact_tokens_masks_all_and_keeps_other_text():
    from tests.helpers import redact_tokens
    text = "boot tok-AAA ok\nERR tok-BBB and tok-AAA again"
    out = redact_tokens(text, ["tok-AAA", "tok-BBB", "", None])
    assert out == "boot <token> ok\nERR <token> and <token> again"
    assert redact_tokens("plain", None) == "plain" and redact_tokens("plain", []) == "plain"


def test_failure_message_path_hides_injected_token():
    """L2(單元層模擬):m3a/m3b 操作驗收的失敗訊息 `assert cond, redact_tokens(stderr, tokens)` 不含 token。"""
    from tests.helpers import redact_tokens
    token = "spw_FAKE_TOKEN_123"
    stderr = f"serve: Authorization: Bearer {token}\nTraceback ..."
    with pytest.raises(AssertionError) as ei:
        assert False, redact_tokens(stderr, [token])
    assert token not in str(ei.value) and "<token>" in str(ei.value)


def test_redact_tokens_overlap_and_bare_str():
    from tests.helpers import redact_tokens
    assert redact_tokens("x abcdef y", ["abc", "abcdef"]) == "x <token> y"
    assert redact_tokens("x abcdef y", ["abcdef", "abc"]) == "x <token> y"
    assert redact_tokens("a tok b tok", "tok") == "a <token> b <token>"
