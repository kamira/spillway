"""NFR-3:預設只綁 127.0.0.1(量測方式:啟動後檢查監聽位址)。

以子行程 `python -m spillway serve --port <隨機埠>`(不帶 --host,使用預設值)啟動,
`lsof -nP -iTCP -sTCP:LISTEN` 只見 `127.0.0.1:<埠>`;經 loopback 可連線,經本機的非 loopback
位址連同一埠被拒;`--host 0.0.0.0` 以非 0 退出且沒有任何監聽。驗收平台 macOS。
"""
from __future__ import annotations

import socket

import httpx
import pytest

from tests.acceptance import procs

pytestmark = pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)")


def test_nfr3_default_bind_is_loopback_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    port = procs.free_port()
    p = procs.start_serve(home, port)
    try:
        addrs = procs.wait_listening(p)
        assert addrs == [f"127.0.0.1:{port}"], addrs
        r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=10)
        assert r.status_code == 200, r.text
        ip = procs.lan_ip()
        if ip is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                assert s.connect_ex((ip, port)) != 0, f"{ip}:{port} 可連線"
    finally:
        procs.stop(p)


def test_nfr3_non_loopback_host_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    port = procs.free_port()
    p = procs.start_serve(home, port, "--host", "0.0.0.0", log_path=tmp_path / "refused.log")
    try:
        rc = p.wait(timeout=60)
    finally:
        procs.stop(p)
    assert rc != 0
    assert "127.0.0.1" in (tmp_path / "refused.log").read_text(encoding="utf-8")
