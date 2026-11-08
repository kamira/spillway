"""NFR-3:預設只綁 127.0.0.1(量測方式:啟動後檢查監聽位址)。

以子行程 `SPILLWAY_CMD serve --port <隨機埠>`(不帶 --host,使用預設值;`tests.blackbox.harness.Serve`)啟動,
`lsof -nP -iTCP:<埠> -sTCP:LISTEN` 只見 `127.0.0.1:<埠>`;經 loopback 可連線,經本機的非 loopback
位址連同一埠被拒;`--host 0.0.0.0` 以結束碼 2(spec 14.1 用法錯誤)退出且沒有任何監聽。驗收平台 macOS。
"""
from __future__ import annotations

import socket
import subprocess

import httpx
import pytest

from tests.acceptance import procs
from tests.blackbox.harness import EXIT_USAGE, spillway_cmd

pytestmark = pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)")


def test_nfr3_default_bind_is_loopback_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    port = procs.free_port()
    p = procs.start_serve(home, port)
    try:
        addrs = procs.wait_listening(p)
        assert addrs == [f"127.0.0.1:{port}"], addrs
        r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=10, trust_env=False)
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
    r = subprocess.run([*spillway_cmd(), "serve", "--port", str(port), "--host", "0.0.0.0"],
                       env=procs.child_env(home), cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    (tmp_path / "refused.log").write_text(r.stderr, encoding="utf-8")
    assert r.returncode != 0
    assert r.returncode == EXIT_USAGE and procs.listening(port) == [], (r.returncode, r.stderr[-2000:])
