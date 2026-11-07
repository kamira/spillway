"""雜湊 requirements lock 的解析(CHG-20261107-01 決策 6 ③;合約測試與漂移偵測共用)。

`parse_lock(text)` 先合併 `\\` 續行、去掉 `#` 註解與空行;以 `-` 開頭的選項行(`-e`、`--index-url` 等)
一律判失敗;其餘每個邏輯行必須是 `name==version [; marker] --hash=sha256:<64 hex>…`。
回傳 `{canon(name): {(version, marker)}}`,marker 不帶分號、未求值(空字串表示沒有 marker)。
"""
from __future__ import annotations

import re

__all__ = ["BLOCKED_LITELLM", "LockError", "canon", "parse_lock"]

# 2026 年 PyPI 被植入的 litellm 版本(CHG-20261107-01 決策 5;來源見 sdlc_docs/knowledge/rust-evaluation.md 第 6 節)
BLOCKED_LITELLM = frozenset({"1.82.7", "1.82.8", "1.107.0"})

_LINE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*(?:;\s*([^-]*?))?\s*((?:--hash=sha256:[0-9a-f]{64}\s*)+)$")


class LockError(ValueError):
    """lock 格式錯誤(依 README 升版流程重產 lock)。"""


def canon(name: str) -> str:
    """PEP 503 正規化名稱。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for raw in text.replace("\\\n", " ").splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        if line.startswith("-"):
            raise LockError(f"lock 不可含選項行:{line[:60]}(依 README 升版流程重產 lock)")
        m = _LINE.match(line)
        if m is None:
            raise LockError(f"lock 行格式不符(需 `==` 確切版本與 sha256 雜湊):{line[:60]}"
                            "(依 README 升版流程重產 lock)")
        name, version, marker, _hashes = m.groups()
        out.setdefault(canon(name), set()).add((version, (marker or "").strip()))
    return out
