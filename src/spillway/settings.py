"""資料目錄、埠號與綁定位址(design D8、D9;CHG-20261030-02 決策 4、21)。

M2 只有常數與環境變數,不讀資料庫的 settings 表。
"""
from __future__ import annotations

import os
from pathlib import Path

import platformdirs

ENV_HOME = "SPILLWAY_HOME"
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
# 只接受字面 127.0.0.1;其他位址需要 M3 的存取 token(決策 4)
ALLOWED_HOSTS = frozenset({"127.0.0.1"})


def data_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    if home:
        return Path(home)
    return Path(platformdirs.user_data_dir("spillway"))


def db_path() -> Path:
    return data_dir() / "spillway.db"


def is_allowed_host(host: str) -> bool:
    return host in ALLOWED_HOSTS
