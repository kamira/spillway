"""Messages 轉為 CLI prompt(spec 7.5.1;CHG-20261101-03 T4 審議統一規格)。

CLI 模組(`claude_cli`、`codex_cli`)共用的唯一實作:

1. 角色表:`system`、`developer` → `[System]`;`user` → `[User]`;`assistant` → `[Assistant]`。
   保持輸入順序,不合併、不重排。角色比對前 `.strip().lower()`。
2. 角色缺漏(鍵不存在、None、空字串、全空白)視為 `user`。
3. 其他角色(`tool`、`function`、未知值)整則略過,不產生其他標頭(7.5.2 保證含 tools 的請求不到 CLI)。
4. `content_text(content)`:None → `""`;str → 原樣;list → 逐項取文字(str 項原樣;dict 且 `type`
   為 `text`、`input_text` 或缺漏、且 `text` 為 str → 取 `text`;其他部件如圖片靜默略過),以 `"\\n"`
   連接;其他型別 → `""`(不用 `str()`,避免洩出 repr)。
5. 抽取後 `strip()` 為空的訊息整則略過(例如 assistant 只有 tool_calls)。
6. 每區塊 `f"{header}\\n{text}"`(text 不 strip),區塊間以 `"\\n\\n"` 連接;無區塊時回 `""`。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["ROLE_HEADERS", "content_text", "messages_to_prompt"]

ROLE_HEADERS = {"system": "[System]", "developer": "[System]", "user": "[User]",
                "assistant": "[Assistant]"}
_TEXT_PART_TYPES = ("text", "input_text")


def content_text(content: Any) -> str:
    """一則訊息 `content` 的純文字(規則 4)。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, Mapping):
                ptype = p.get("type")
                text = p.get("text")
                if (ptype is None or ptype in _TEXT_PART_TYPES) and isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _header(role: Any) -> str | None:
    if role is None or (isinstance(role, str) and not role.strip()):
        return ROLE_HEADERS["user"]
    if not isinstance(role, str):
        return None
    return ROLE_HEADERS.get(role.strip().lower())


def messages_to_prompt(messages: Iterable[Mapping[str, Any]]) -> str:
    """spec 7.5.1:依序串成純文字,每段以 `[System]`、`[User]`、`[Assistant]` 標頭開頭。"""
    blocks: list[str] = []
    for m in messages:
        if not isinstance(m, Mapping):
            continue
        header = _header(m.get("role"))
        if header is None:
            continue
        text = content_text(m.get("content"))
        if not text.strip():
            continue
        blocks.append(f"{header}\n{text}")
    return "\n\n".join(blocks)
