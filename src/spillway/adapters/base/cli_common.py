"""CLI 模組(claude_cli、codex_cli)共用的輔助函式(CHG-20261102-01 決策 20)。

- `cli_cwd`:工作目錄決定規則(`params.cwd`,否則 `data_dir/work/<模組>`),失敗丟 `ValueError`。
- `tail` / `head`:錯誤訊息截斷(各模組自帶長度常數)。
- `nonneg_int`:token 欄位只計非負整數(bool 不算)。
- `CLI_WORK_SUBDIRS`:內建 CLI 模組的工作子目錄名;`cli.py` 的 `WORK_SUBDIRS` 由此取得。
  取捨(決策 20):這裡寫死具體模組名,與 Registry(D11)方向相反;本筆照做,不改為 manifest。

只用標準庫(另依 AdapterContext 型別)。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from spillway.adapters.base.protocol import AdapterContext

__all__ = ["CLI_WORK_SUBDIRS", "cli_cwd", "head", "nonneg_int", "tail"]

CLI_WORK_SUBDIRS = ("claude", "codex")


def cli_cwd(params: Mapping[str, Any], context: AdapterContext, subdir: str) -> Path:
    """`params.cwd`(展開 `~`);未設定時為 `context.data_dir / "work" / subdir`(不存在就建立)。

    `data_dir` 為 None 或無法建立目錄 → `ValueError`(呼叫端轉成該次 `error`)。
    """
    raw = params.get("cwd")
    if raw:
        return Path(str(raw)).expanduser()
    if context.data_dir is None:
        raise ValueError(f"未設定 params.cwd,且閘道沒有注入資料目錄(data_dir),無法決定 {subdir} 的工作目錄")
    work = Path(context.data_dir) / "work" / subdir
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"無法建立工作目錄 {work}:{type(exc).__name__}: {exc.strerror or exc}") from exc
    return work


def head(text: str, limit: int) -> str:
    """去頭尾空白後取前 `limit` 字元;超出時尾端加 `…`。"""
    text = text.strip()
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def tail(text: str, limit: int) -> str:
    """去頭尾空白後取最後 `limit` 字元;超出時開頭加 `…`。"""
    text = text.strip()
    if limit <= 0:
        return ""
    return text if len(text) <= limit else "…" + text[-limit:]


def nonneg_int(v: Any) -> int | None:
    """非負整數(排除 bool)回原值,其餘 None。"""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
