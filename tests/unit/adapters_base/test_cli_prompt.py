"""`adapters.base.messages_to_prompt` / `content_text`(spec 7.5.1;CHG-20261101-03 T4 審議統一規格)。"""
from __future__ import annotations

import pytest

from spillway.adapters.base import content_text, messages_to_prompt


def test_role_table_and_order():
    prompt = messages_to_prompt([
        {"role": "system", "content": "s"},
        {"role": "developer", "content": "d"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "u2"},
    ])
    assert prompt == "[System]\ns\n\n[System]\nd\n\n[User]\nu1\n\n[Assistant]\na\n\n[User]\nu2"


def test_role_case_and_whitespace_insensitive():
    assert messages_to_prompt([{"role": " SYSTEM ", "content": "x"},
                               {"role": "Assistant", "content": "y"}]) == \
        "[System]\nx\n\n[Assistant]\ny"


@pytest.mark.parametrize("msg", [{"content": "x"}, {"role": None, "content": "x"},
                                 {"role": "", "content": "x"}, {"role": "   ", "content": "x"}])
def test_missing_role_is_user(msg):
    assert messages_to_prompt([msg]) == "[User]\nx"


@pytest.mark.parametrize("role", ["tool", "function", "robot", 5])
def test_other_roles_skipped(role):
    prompt = messages_to_prompt([{"role": "user", "content": "q"},
                                 {"role": role, "content": "secret tool output"}])
    assert prompt == "[User]\nq"
    assert "[Tool]" not in prompt


def test_content_text_rules():
    assert content_text(None) == ""
    assert content_text("  keep  ") == "  keep  "
    assert content_text([
        "raw",
        {"type": "text", "text": "a"},
        {"type": "input_text", "text": "b"},
        {"text": "c"},
        {"type": "image_url", "image_url": {"url": "data:x"}},
        {"type": "image", "text": "not text part"},
        {"type": "text"},
        {"type": "text", "text": 5},
        7,
    ]) == "raw\na\nb\nc"
    assert content_text({"type": "text", "text": "dict"}) == ""
    assert content_text(42) == ""


def test_empty_blocks_skipped():
    prompt = messages_to_prompt([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        {"role": "user", "content": "  \n "},
        {"role": "assistant", "content": "a"},
    ])
    assert prompt == "[User]\nq\n\n[Assistant]\na"


def test_text_not_stripped_and_empty_result():
    assert messages_to_prompt([{"role": "user", "content": " x \n"}]) == "[User]\n x \n"
    assert messages_to_prompt([]) == ""
    assert messages_to_prompt([{"role": "tool", "content": "t"}]) == ""
