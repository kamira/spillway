"""HTTP 存活探測(決策 10;T6 litellm 模組 `health()` 對 local `api_base` 使用)。

adapter 模組不可直接 import `httpx`(Global Constraints「分層」),需要探測端點時經這裡。
"""
from __future__ import annotations

import httpx

PROBE_TIMEOUT = 2.0


async def probe_http(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """對 `url` 發一次 `GET`;`timeout` 秒內收到任何 HTTP 回應(含 4xx/5xx)回 True。

    連線失敗、逾時、URL 不合法等一律回 False,不外拋(`asyncio.CancelledError` 除外,照常傳遞)。
    不跟隨轉址、不讀環境的 proxy 設定(`trust_env=False`),避免把 local 探測送往他處。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            await client.get(url)
    except Exception:  # noqa: BLE001 — 任何失敗都只代表「不健康」
        return False
    return True
