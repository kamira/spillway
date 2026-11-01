"""API 層的連線取得(CHG-20261101-02 決策 5)。

`request_conn(request)`:context manager(**不是** FastAPI `Depends`,handler 簽名維持只收
`request`)。每次呼叫讀 `request.app.state.conn_factory`(建 app 後替換工廠,下一次呼叫即生效),
呼叫一次開一條新連線,離開 `with` 時關閉(含例外)——與原 admin `_db(request)` 的開關時機相同,
在回應建立前關閉。在 async handler 內使用時於事件迴圈執行緒開啟與關閉(連線維持預設的
`check_same_thread=True`)。

只需要 `request.app.state.conn_factory`,所以測試可以 `SimpleNamespace(app=app)` 直接呼叫 handler。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["request_conn"]


@contextmanager
def request_conn(request: Any) -> Iterator[sqlite3.Connection]:
    conn = request.app.state.conn_factory()
    try:
        yield conn
    finally:
        conn.close()
