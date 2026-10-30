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
    # 「task-1234」不是金鑰;只有在非英數字元(或跳脫/編碼前綴)之後的 sk- 才算
    assert r.redact("task-1234 done") == ("task-1234 done", False)
    assert r.redact("ask-question") == ("ask-question", False)


# ---- M4:JSON 跳脫與 URL 編碼的上下文 ----

@pytest.mark.parametrize(
    "prompt",
    [
        f"line1\n{FAKE_KEY}",
        f"col\t{FAKE_KEY}",
        f"cr\r{FAKE_KEY}",
        f"x\b{FAKE_KEY}",
        f"x\f{FAKE_KEY}",
        f"say \"{FAKE_KEY}\"",
        f"path/{FAKE_KEY}",
        f"space {FAKE_KEY}",
        f"\u00e9{FAKE_KEY}",  # ensure_ascii 時變成 \u00e9sk-…
        f"\u4e2d{FAKE_KEY}",
        f"\x01{FAKE_KEY}",  # 控制字元 → \u0001sk-…
    ],
)
def test_sk_after_json_escape_is_masked(r, prompt):
    raw = json.dumps({"messages": [{"role": "user", "content": prompt}]})
    out, hit = r.redact(raw)
    assert hit is True
    assert FAKE_KEY not in out
    json.loads(out)  # 遮罩後仍是合法 JSON


@pytest.mark.parametrize(
    "text",
    [
        "key%3Dsk-abcdef123",
        "%22sk-proj-abcdef%22",
        "q=a%20sk-abcdef123&b=1",
        "%0Ask-abcdef123",
        "%0ask-abcdef123",
    ],
)
def test_sk_after_url_encoding_is_masked(r, text):
    out, hit = r.redact(text)
    assert hit is True
    assert "sk-abcdef" not in out and "sk-proj" not in out


def test_sk_url_encoded_keeps_surrounding_text(r):
    assert r.redact("key%3Dsk-abcdef123&x=1") == (f"key%3D{MASK}&x=1", True)
    assert r.redact("%22sk-proj-abcdef%22") == (f"%22{MASK}%22", True)


def test_sk_full_charset_is_masked_whole(r):
    # 含 .、+、/、= 的金鑰要整串遮掉,不能只遮到第一個標點前
    key = "sk-ant-api03-ab.cd/ef+gh=="
    out, hit = r.redact(f"key {key} end")
    assert hit is True
    assert out == f"key {MASK} end"
    for frag in ("ab.cd", "ef+gh", "gh=="):
        assert frag not in out
    raw = json.dumps({"k": key})
    out, _ = r.redact(raw)
    assert json.loads(out) == {"k": MASK}


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
        "Bearer\nxyz",
        "Bearer:xyz",
        "Bearer: xyz",
        "bearer=xyz",
        "Bearer\u00a0xyz",  # NBSP
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


@pytest.mark.parametrize(
    "secret", [" AIzaFAKE ", "AIzaFAKE\n", "\tAIzaFAKE\r\n", "AIzaFAKE"]
)
def test_extra_secret_with_whitespace_masks_stripped_form(r, secret):
    # M5:上游/HTTP 層回顯的是修剪後的值
    out, hit = r.redact("invalid credential: AIzaFAKE end", extra_secrets=(secret,))
    assert hit is True
    assert "AIzaFAKE" not in out
    assert out.startswith("invalid credential:") and out.endswith("end")
    assert MASK in out


def test_extra_secret_raw_form_with_whitespace_also_masked(r):
    out, hit = r.redact("x=AIzaFAKE\n;y", extra_secrets=("AIzaFAKE\n",))
    assert hit is True and "AIzaFAKE" not in out


@pytest.mark.parametrize("secret", ["    ", " ab ", "\n\n\n\n", "  abc\n"])
def test_extra_secret_short_after_strip_is_ignored(r, secret):
    # 長度門檻以 strip 後為準:純空白或修剪後 < 4 的值不遮(含原樣形式)
    text = "x ab y    z\n\n\n\n  abc\n"
    assert r.redact(text, extra_secrets=(secret,)) == (text, False)


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
