"""redact:遮罩疑似金鑰字串(CHG-20261030-02 T3;spec 2.4.4)。"""
from __future__ import annotations

import json

import pytest

from spillway.redact import MASK, Redactor

FAKE_KEY = "sk-test-0000"


@pytest.fixture
def r() -> Redactor:
    return Redactor()


def test_sk_key_is_masked(r):
    out, hit = r.redact("key sk-test-0000 ok")
    assert FAKE_KEY not in out
    assert hit is True
    assert out == f"key {MASK} ok"


def test_sk_requires_at_least_four_chars(r):
    assert r.redact("sk-abc")[1] is False
    assert r.redact("sk-abcd")[1] is True


def test_sk_inside_word_is_not_masked(r):
    # 「task-1234」不是金鑰;只有在非英數字元之後的 sk- 才算
    assert r.redact("task-1234 done") == ("task-1234 done", False)


def test_sk_after_punctuation_is_masked(r):
    out, hit = r.redact('api_key="sk-test-0000",')
    assert hit and FAKE_KEY not in out


@pytest.mark.parametrize(
    "text",
    [
        "bearer xyz",
        "Bearer xyz",
        "BEARER xyz",
        '"Bearer xyz"',
        "Authorization: Bearer xyz\nnext",
        "line1\nBearer xyz",
        "Bearer\txyz",
        "Bearer   xyz",
    ],
)
def test_bearer_value_is_masked(r, text):
    out, hit = r.redact(text)
    assert hit is True
    assert "xyz" not in out


def test_bearer_value_stops_at_quote_space_newline(r):
    out, _ = r.redact('"Bearer xyz" keep')
    assert out == f'"Bearer {MASK}" keep'
    out, _ = r.redact("Bearer xyz\nkeep")
    assert out == f"Bearer {MASK}\nkeep"
    out, _ = r.redact("bearer xyz keep")
    assert out == f"bearer {MASK} keep"
    out, _ = r.redact("Bearer xyz'keep")
    assert out == f"Bearer {MASK}'keep"


def test_bearer_inside_json_string(r):
    raw = json.dumps({"headers": {"Authorization": "Bearer xyz-123"}, "msg": "hi"})
    out, hit = r.redact(raw)
    assert hit is True
    assert "xyz-123" not in out
    assert json.loads(out)["headers"]["Authorization"] == f"Bearer {MASK}"
    assert json.loads(out)["msg"] == "hi"


def test_extra_secret_is_masked(r):
    out, hit = r.redact("token AIzaFAKE here", extra_secrets=("AIzaFAKE",))
    assert hit is True
    assert "AIzaFAKE" not in out
    assert out == f"token {MASK} here"


def test_extra_secret_all_occurrences(r):
    out, hit = r.redact("AIzaFAKE AIzaFAKE", extra_secrets=["AIzaFAKE"])
    assert hit and "AIzaFAKE" not in out


def test_extra_secrets_longer_first(r):
    out, _ = r.redact("abcdefgh", extra_secrets=("abcd", "abcdefgh"))
    assert out == MASK


@pytest.mark.parametrize("secret", ["", "a", "abc"])
def test_short_extra_secrets_are_ignored(r, secret):
    out, hit = r.redact("abc def", extra_secrets=(secret,))
    assert out == "abc def"
    assert hit is False


def test_no_hit_leaves_text_unchanged(r):
    text = "hello world, nothing secret here\n{\"a\": 1}"
    assert r.redact(text) == (text, False)
    assert r.redact(text, extra_secrets=("zzzzzz",)) == (text, False)


def test_empty_text(r):
    assert r.redact("") == ("", False)


def test_redact_is_idempotent(r):
    once, _ = r.redact("Bearer xyz and sk-test-0000")
    twice, hit = r.redact(once)
    assert twice == once
    assert hit is False


def test_matches(r):
    assert r.matches("key sk-test-0000") is True
    assert r.matches("Authorization: bearer abc") is True
    assert r.matches("plain text") is False
    assert r.matches("") is False
