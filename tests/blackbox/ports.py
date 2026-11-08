"""本機埠與位址的小工具(CHG-20261107-03 T2;決策 8 ⑦;AM-15)。

由 `tests/acceptance/procs.py` 與 `tests/helpers/__init__.py` 的同名函式複製(原檔不動,留給 unit 與
integration 使用);本模組不 import spillway。

- `free_port()`:綁 127.0.0.1:0 後立即關閉,回傳當下空著的埠;一律不回 serve 的預設埠 `DEFAULT_SERVE_PORT`
  (8787,擁有者自己的 serve 可能在用;作業系統剛好配到時重抽,施工審議 T4 F6)。
- `dead_base()`:一個當下沒有服務在聽的 127.0.0.1 埠的 `api_base`(`.../v1`)。
- `lan_ip()`:本機的非 loopback IPv4(不送出封包:UDP connect 只做路由選擇);沒有時回 None。
"""
from __future__ import annotations

import socket

__all__ = ["dead_base", "free_port", "lan_ip"]

HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8787


def _os_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def free_port() -> int:
    while True:
        port = _os_free_port()
        if port != DEFAULT_SERVE_PORT:
            return port


def dead_base() -> str:
    return f"http://{HOST}:{free_port()}/v1"


def lan_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
