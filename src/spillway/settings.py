"""資料目錄、埠號、綁定位址與存取 token(design D8、D9;CHG-20261030-02 決策 4、21;
CHG-20261101-03 決策 14、17)。

- 資料目錄、埠號、綁定位址只有常數與環境變數(另有 MCP 端點 `mcp_url`、保留群組名 `MCP_ALL_GROUP`、
  SIGINT 結束碼 `EXIT_SIGINT` 的單一定義,CHG-20261102-01 終審;
  `TOKEN_ENV`、`DEPTH_ENV`、`DEPTH_HEADER`,CHG-20261102-02 決策 15);settings 表的 `bind_host`、`port` 鍵仍不讀(D16 不變)。
- settings 表只讀寫 `access_token_sha256`(`ACCESS_TOKEN_KEY`):存取 token 由
  `secrets.token_urlsafe(32)` 產生,資料庫只存 SHA-256 十六進位;明文只回給呼叫端一次
  (`spillway init`、serve 首次啟動的 stderr 一行、`POST /api/settings/token/rotate`)。
  本模組不記錄、不印出 token;M3a 不以 token 做任何驗證(M3b 的 `/mcp/*` 才比對雜湊)。
- 匯入方向:本模組為了 token 輔助函式匯入 `spillway.store.*`(`repo.get_setting`/`set_setting`、
  `transaction`;SQL 仍只在 store);`spillway.store` 永遠不可匯入本模組,否則形成循環匯入。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import quote

import platformdirs

from spillway.store import repo
from spillway.store.db import transaction

ENV_HOME = "SPILLWAY_HOME"
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
# 只接受字面 127.0.0.1:M3 仍只允許 127.0.0.1(CHG-20261101-03 決策 2、17;D16 不放寬)
ALLOWED_HOSTS = frozenset({"127.0.0.1"})

# CHG-20261102-01 決策 22:保留的群組名稱(`/mcp/all` 暴露全部 route;管理 API 拒絕建立同名群組)
MCP_ALL_GROUP = "all"
# CHG-20261102-01 決策 15、19:serve 與 mcp-stdio 被 Ctrl-C(SIGINT)結束時的結束碼
EXIT_SIGINT = 130
# CHG-20261102-02 決策 15:存取 token 與遞迴深度的環境變數名、遞迴深度 header 名(單一定義;
# api.mcp、mcp_config、catalog 引用;adapters 依分層規則保留字面值,由 tests/contract/test_env_names.py 釘住)
TOKEN_ENV = "SPILLWAY_TOKEN"
DEPTH_ENV = "SPILLWAY_DEPTH"
DEPTH_HEADER = "X-Spillway-Depth"

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


def mcp_url(port: int, group: str) -> str:
    """本機 MCP 端點 `http://127.0.0.1:<port>/mcp/<group>`(群組名以 `quote(g, safe="")` 編碼;
    catalog、`/api/mcp/config` 與 mcp-stdio 共用)。"""
    return f"http://{DEFAULT_HOST}:{port}/mcp/{quote(group, safe='')}"


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
