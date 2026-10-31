"""probe_http():local 端點存活探測(決策 10;審查 M8)。

只打 127.0.0.1:假上游的 `GET /`,以及一個剛關閉、沒有人監聽的埠。
"""
from __future__ import annotations

import socket
import time

import pytest

from spillway.adapters.base import PROBE_TIMEOUT, probe_http

pytestmark = pytest.mark.anyio


def _closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_default_timeout_is_two_seconds():
    assert PROBE_TIMEOUT == 2.0


async def test_any_http_response_is_healthy(fake_api):
    assert await probe_http(fake_api.base_url + "/") is True


async def test_http_error_status_still_healthy(fake_api):
    # 404 也是「收到 HTTP 回應」
    assert await probe_http(fake_api.base_url + "/no-such-path") is True


async def test_closed_port_is_unhealthy():
    start = time.monotonic()
    assert await probe_http(f"http://127.0.0.1:{_closed_port()}/") is False
    assert time.monotonic() - start < PROBE_TIMEOUT + 1


async def test_timeout_is_unhealthy():
    # 只 listen 不 accept/回應:連線建立但等不到 HTTP 回應 → 逾時回 False
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        start = time.monotonic()
        assert await probe_http(f"http://127.0.0.1:{port}/", timeout=0.3) is False
        assert time.monotonic() - start < 2
    finally:
        srv.close()


@pytest.mark.parametrize("url", ["", "not a url", "ftp://127.0.0.1/", "http://"])
async def test_invalid_url_returns_false_without_raising(url):
    assert await probe_http(url) is False
