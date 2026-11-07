"""測試控制介面 `/__test__/*` 的同步 client(CHG-20261107-03 T2;決策 7、8 ③;spec 13)。

只經 HTTP;不 import spillway。時間參數可傳 `datetime`(轉成 UTC `YYYY-MM-DDTHH:MM:SSZ`)或該格式字串。
便利方法在非 200 時 `AssertionError`(訊息含狀態碼與錯誤碼),並斷言回應 body 的鍵集合**完全等於** spec 13.4
的定義(清單內每一項也一樣;施工審議 T2 F12);要斷言錯誤回應時用 `request()`。`ENDPOINTS` 列出 13.4 的全部
(方法, 路徑)。

- `Control(base_url, *, timeout=30.0)`;`close()`;可當 context manager。
- `request(method, path, json=None) -> httpx.Response`:原樣送出(`path` 為 `/__test__` 之後的部分,
  例如 `"/clock"`;以 `/__test__` 開頭時照用)。
- 時鐘:`clock() -> {"controllable", "now"}`、`now() -> datetime`、`set_clock(t)`、`advance(seconds)`
  (後兩者回傳新的 `now` 字串)。
- 亂數:`rng() -> {"seeded", "seed"}`。
- 種入:`ledger(rows) -> ids`、`payloads(rows) -> ids`(列中的 datetime 值自動轉字串)、
  `cooldown(deployment, until, reason)`、`raw_deployment_params(id, params)`(測試值一律 `sk-fake…`)。
- 保存清理:`retention_run() -> deleted`、`retention() -> {"runs", "next_due"}`、
  `tick(advance_seconds=0) -> {"ran", "deleted", "runs", "now"}`(STEPPED 模式)。
- `health_cache() -> entries`;`cli_slots() -> slots`、`acquire(dep, n=1)`、`release(dep, n=1)`。
- MCP:`stream_open(session_id) -> bool`;`wait_stream_open(session_id, timeout=5.0)`(真實時間輪詢)。
- `iso(t)`:模組函式,datetime → `YYYY-MM-DDTHH:MM:SSZ`(字串原樣回傳)。
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

__all__ = ["ENDPOINTS", "PREFIX", "Control", "iso", "parse_iso"]

PREFIX = "/__test__"
# spec 13.4 的端點(方法, 路徑);`{id}` 以任意值代入
ENDPOINTS = (
    ("GET", "/clock"), ("POST", "/clock"), ("GET", "/rng"), ("POST", "/ledger"), ("POST", "/payloads"),
    ("POST", "/cooldowns"), ("POST", "/raw/deployment-params"), ("POST", "/retention/run"), ("GET", "/retention"),
    ("POST", "/retention/tick"), ("GET", "/health-cache"), ("GET", "/cli-slots"), ("POST", "/cli-slots/acquire"),
    ("POST", "/cli-slots/release"), ("GET", "/mcp/sessions/x"),
)
_SLOT_KEYS = {"deployment", "in_flight", "limit"}
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def iso(t: datetime | str) -> str:
    if isinstance(t, str):
        return t
    if t.tzinfo is None:
        raise ValueError("datetime 必須帶時區")
    return t.astimezone(timezone.utc).strftime(_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def _jsonable_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: iso(v) if isinstance(v, datetime) else v for k, v in row.items()}


class Control:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Control:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 原樣請求
    def request(self, method: str, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        full = path if path.startswith(PREFIX) else PREFIX + path
        if json is not None:
            kw["json"] = json
        return self._client.request(method, full, **kw)

    def _ok(self, method: str, path: str, json: Any = None, *, keys: set[str],
            items: tuple[str, set[str]] | None = None) -> Any:
        r = self.request(method, path, json)
        if r.status_code != 200:
            try:
                code = r.json()["error"]["code"]
            except Exception:  # noqa: BLE001 — 只用於錯誤訊息
                code = r.text[:200]
            raise AssertionError(f"{method} {PREFIX}{path} → {r.status_code} {code}")
        body = r.json()
        assert isinstance(body, dict) and set(body) == keys, f"{method} {PREFIX}{path} 回應的鍵 {sorted(body)} ≠ {sorted(keys)}"
        if items is not None:
            field, item_keys = items
            for it in body[field]:
                assert set(it) == item_keys, f"{method} {PREFIX}{path} 的 {field} 項目鍵 {sorted(it)} ≠ {sorted(item_keys)}"
        return body

    # ------------------------------------------------------------------ 時鐘(13.4.1)
    def clock(self) -> dict[str, Any]:
        return self._ok("GET", "/clock", keys={"controllable", "now"})

    def now(self) -> datetime:
        return parse_iso(self.clock()["now"])

    def set_clock(self, t: datetime | str) -> str:
        return self._ok("POST", "/clock", {"set": iso(t)}, keys={"now"})["now"]

    def advance(self, seconds: int) -> str:
        return self._ok("POST", "/clock", {"advance_seconds": int(seconds)}, keys={"now"})["now"]

    # ------------------------------------------------------------------ 亂數(13.4.2)
    def rng(self) -> dict[str, Any]:
        return self._ok("GET", "/rng", keys={"seeded", "seed"})

    # ------------------------------------------------------------------ 種入(13.4.3–13.4.5)
    def ledger(self, rows: Iterable[Mapping[str, Any]]) -> list[int]:
        return self._ok("POST", "/ledger", {"rows": [_jsonable_row(r) for r in rows]}, keys={"ids"})["ids"]

    def payloads(self, rows: Iterable[Mapping[str, Any]]) -> list[int]:
        return self._ok("POST", "/payloads", {"rows": [_jsonable_row(r) for r in rows]}, keys={"ids"})["ids"]

    def cooldown(self, deployment: str, until: datetime | str, reason: str) -> dict[str, Any]:
        return self._ok("POST", "/cooldowns", {"deployment": deployment, "until": iso(until), "reason": reason},
                        keys={"deployment", "until", "reason"})

    def raw_deployment_params(self, dep_id: str, params: Any) -> dict[str, Any]:
        return self._ok("POST", "/raw/deployment-params", {"id": dep_id, "params": params}, keys={"id"})

    # ------------------------------------------------------------------ 保存清理(13.4.6)
    def retention_run(self) -> int | None:
        return self._ok("POST", "/retention/run", keys={"deleted"})["deleted"]

    def retention(self) -> dict[str, Any]:
        return self._ok("GET", "/retention", keys={"runs", "next_due"})

    def tick(self, advance_seconds: int = 0) -> dict[str, Any]:
        return self._ok("POST", "/retention/tick", {"advance_seconds": int(advance_seconds)},
                        keys={"ran", "deleted", "runs", "now"})

    # ------------------------------------------------------------------ 健康快取、CLI 名額(13.4.7–8)
    def health_cache(self) -> list[dict[str, Any]]:
        return self._ok("GET", "/health-cache", keys={"entries"}, items=("entries", {"deployment", "healthy"}))["entries"]

    def cli_slots(self) -> list[dict[str, Any]]:
        return self._ok("GET", "/cli-slots", keys={"slots"}, items=("slots", _SLOT_KEYS))["slots"]

    def acquire(self, deployment: str, n: int = 1) -> dict[str, Any]:
        return self._ok("POST", "/cli-slots/acquire", {"deployment": deployment, "n": n}, keys=_SLOT_KEYS)

    def release(self, deployment: str, n: int = 1) -> dict[str, Any]:
        return self._ok("POST", "/cli-slots/release", {"deployment": deployment, "n": n}, keys=_SLOT_KEYS)

    # ------------------------------------------------------------------ MCP(13.4.9)
    def stream_open(self, session_id: str) -> bool:
        return bool(self._ok("GET", f"/mcp/sessions/{quote(session_id, safe='')}", keys={"stream_open"})["stream_open"])

    def wait_stream_open(self, session_id: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.stream_open(session_id):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"MCP session 的通知串流未在 {timeout} 秒內就緒")
            time.sleep(0.02)
