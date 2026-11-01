"""資料目錄、埠號、綁定位址與存取 token(design D8、D9;CHG-20261030-02 決策 4、21;
CHG-20261101-03 決策 14、17)。

- 資料目錄、埠號、綁定位址只有常數與環境變數;settings 表的 `bind_host`、`port` 鍵仍不讀(D16 不變)。
- settings 表只讀寫 `access_token_sha256`(`ACCESS_TOKEN_KEY`):存取 token 由
  `secrets.token_urlsafe(32)` 產生,資料庫只存 SHA-256 十六進位;明文只回給呼叫端一次
  (`spillway init`、serve 首次啟動的 stderr 一行、`POST /api/settings/token/rotate`)。
  本模組不記錄、不印出 token;M3a 不以 token 做任何驗證(M3b 的 `/mcp/*` 才比對雜湊)。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from pathlib import Path

import platformdirs

from spillway.store import repo
from spillway.store.db import transaction

ENV_HOME = "SPILLWAY_HOME"
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
# 只接受字面 127.0.0.1:M3 仍只允許 127.0.0.1(CHG-20261101-03 決策 2、17;D16 不放寬)
ALLOWED_HOSTS = frozenset({"127.0.0.1"})

ACCESS_TOKEN_KEY = "access_token_sha256"
ACCESS_TOKEN_BYTES = 32   # secrets.token_urlsafe(32) → 43 個字元


def data_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    if home:
        return Path(home)
    return Path(platformdirs.user_data_dir("spillway"))


def db_path() -> Path:
    return data_dir() / "spillway.db"


def is_allowed_host(host: str) -> bool:
    return host in ALLOWED_HOSTS


# ---------------------------------------------------------------- 存取 token(決策 14)


def generate_access_token() -> str:
    """新的 token 明文(`secrets.token_urlsafe(32)`)。"""
    return secrets.token_urlsafe(ACCESS_TOKEN_BYTES)


def hash_access_token(token: str) -> str:
    """資料庫存放的形式:SHA-256 十六進位(64 個小寫字元)。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_access_token_hash(conn: sqlite3.Connection) -> str | None:
    """settings 表的 `access_token_sha256`;尚未產生時為 None。"""
    return repo.get_setting(conn, ACCESS_TOKEN_KEY)


def rotate_access_token(conn: sqlite3.Connection) -> str:
    """產生新 token、只把雜湊寫入資料庫(覆寫舊值),回傳明文(呼叫端只顯示這一次)。"""
    token = generate_access_token()
    repo.set_setting(conn, ACCESS_TOKEN_KEY, hash_access_token(token))
    return token


def ensure_access_token(conn: sqlite3.Connection) -> str | None:
    """還沒有雜湊時產生並寫入,回傳明文;已存在時不變更、回 None。

    檢查與寫入在同一個 `BEGIN IMMEDIATE` 交易內,兩個行程同時首次啟動也只會產生一個。"""
    with transaction(conn):
        if get_access_token_hash(conn) is not None:
            return None
        return rotate_access_token(conn)
