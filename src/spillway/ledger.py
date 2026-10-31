"""帳本寫入、context 記錄與保存清理(CHG-20261030-02 T10;spec 2.1.5、2.4、4.5.1;決策 17、19)。

- `Attempt`:Router 交給 `LedgerWriter` 的單次嘗試記錄(呼叫後與結果一次寫入,D24)。
- `LedgerWriter.record(attempt)`:同一交易內寫 `usage_ledger` 一列,必要時加 `usage_payloads` 一列。
- `RetentionJob.run_once(now)`:刪除 `ts` 早於保存期限的 payload(2.4.5)。
- `payload_status(conn, ledger_id, now)`:`ok` | `expired` | `never_recorded`(決策 17)。

Payload 規則:
- 是否寫入(2.4.1–2.4.2):`route_record_context` 為 None 時沿用全域 `record_context_default`,
  0/1 時以 route 為準;保存天數 ≤ 0 時一律不寫(2.4.5)。`request_text` 為 None(沒有送出任何
  內容,例如 `skipped_precheck`)時也不寫。
- 先遮罩再截斷(安全影響 (d)):請求與回應各自經 `Redactor.redact(text, extra_secrets)`,
  之後才截斷,因此截斷點落在金鑰中間時,被切開的只會是 `[REDACTED]` 字樣,不會留下金鑰前綴。
- 上限 `context_max_bytes` 是**單筆 payload**(請求 + 回應)的 UTF-8 位元組總數(2.4.3)。
  超過時分配:較短的一方若不超過一半就整段保留,餘額給另一方;兩者都超過一半時各取一半。
  截斷只在 UTF-8 字元邊界上切,因此含多位元組字元時 `bytes` 可能比上限少最多 3 bytes(每段)。
- `bytes` 欄 = 實際寫入的請求與回應的 UTF-8 位元組數總和;`redacted` = 請求或回應有遮罩命中;
  `truncated` = 任一段被截斷。

`usage_ledger.error` 一律先遮罩(含 `extra_secrets`)再寫入;帳本列不含任何請求或回應內容(2.1.5)。
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from spillway.clock import Clock
from spillway.redact import Redactor
from spillway.store import repo
from spillway.store.db import transaction

_log = logging.getLogger(__name__)

__all__ = [
    "LEDGER_STATUSES",
    "Attempt",
    "LedgerNotFound",
    "LedgerWriter",
    "PayloadStatus",
    "RetentionJob",
    "payload_status",
]

# 契約清單 `usage_ledger.status`
LEDGER_STATUSES = frozenset({"ok", "error", "quota_exhausted", "rate_limited", "skipped_precheck"})

PayloadStatus = Literal["ok", "expired", "never_recorded"]


class LedgerNotFound(LookupError):
    """`payload_status()` 查詢的 ledger_id 不存在(呼叫端應回 404 `not_found`)。"""


@dataclass(frozen=True)
class Attempt:
    """單次嘗試(含 fallback 中失敗者、`skipped_precheck`)的完整記錄。

    帳本欄位(對應 `usage_ledger`,spec 2.3):
        ts: 嘗試時間(帶時區;存為 UTC 秒精度)。
        status: `ok` | `error` | `quota_exhausted` | `rate_limited` | `skipped_precheck`。
        route_name: route 名;`local/` 直呼為 None,隱含 route 為 model_name(決策 18)。
        deployment_id、effort(實際送出的 effort)、tokens_in、tokens_out、cost_usd、
        slot(`local/` 直呼為 None)、fallback_depth、latency_ms、
        est_tokens_in、est_tokens_out_p50、est_tokens_out_p90、est_cost_usd_p90(呼叫前預估)。
        error: 錯誤訊息原文;寫入前會遮罩。

    Context 內容(只寫入 `usage_payloads`):
        request_text: 實際送出的內容(已序列化的 messages JSON;CLI 為轉換後的 prompt)。
            None 表示沒有送出任何內容,不寫 payload。
        response_text: 回應文字;失敗時可為 None。
        extra_secrets: 當次解析出的憑證值等明確秘密,遮罩 request/response/error 時使用。
        route_record_context: 該 route 的 `record_context`(None = 沿用全域,0 關、1 開);
            `local/` 直呼與隱含 route 一律傳 None(2.4.2)。
    """

    ts: datetime
    status: str
    route_name: str | None = None
    deployment_id: str | None = None
    effort: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    slot: int | None = None
    fallback_depth: int = 0
    latency_ms: int | None = None
    est_tokens_in: int | None = None
    est_tokens_out_p50: int | None = None
    est_tokens_out_p90: int | None = None
    est_cost_usd_p90: float | None = None
    error: str | None = None
    request_text: str | None = None
    response_text: str | None = None
    extra_secrets: Sequence[str] = ()
    route_record_context: int | bool | None = None


def _utf8(text: str) -> bytes:
    # 單獨的 surrogate 無法以 UTF-8 存入 SQLite,以 replace 轉成 "?"
    return text.encode("utf-8", errors="replace")


def _truncate_utf8(data: bytes, limit: int) -> bytes:
    """截到至多 `limit` bytes,且不切壞多位元組字元。"""
    if len(data) <= limit:
        return data
    # data 本身是合法 UTF-8,只有尾端被切斷的那個字元會失敗;ignore 只丟掉它
    return data[: max(limit, 0)].decode("utf-8", errors="ignore").encode("utf-8")


def _split_budget(req_len: int, resp_len: int, limit: int) -> tuple[int, int]:
    """請求與回應各自可用的位元組數(兩者合計不超過 `limit`)。"""
    if req_len + resp_len <= limit:
        return req_len, resp_len
    half = limit // 2
    if req_len <= half:
        return req_len, limit - req_len
    if resp_len <= limit - half:
        return limit - resp_len, resp_len
    return half, limit - half


class LedgerWriter:
    """每次嘗試一列帳本,依設定附帶一列 payload(同一交易)。"""

    def __init__(self, conn: sqlite3.Connection, redactor: Redactor | None = None) -> None:
        self._conn = conn
        self._redactor = redactor or Redactor()

    def record(self, attempt: Attempt) -> int:
        """寫入並回傳 `usage_ledger.id`。status 不在五個合法值內時丟 `ValueError`、不寫任何列。"""
        if attempt.status not in LEDGER_STATUSES:
            raise ValueError(f"未知的 ledger status: {attempt.status!r}")
        secrets = tuple(attempt.extra_secrets)
        error = attempt.error
        if error is not None:
            error, _ = self._redactor.redact(error, secrets)
        row = repo.LedgerRow(
            ts=attempt.ts,
            status=attempt.status,
            route_name=attempt.route_name,
            deployment_id=attempt.deployment_id,
            effort=attempt.effort,
            tokens_in=attempt.tokens_in,
            tokens_out=attempt.tokens_out,
            cost_usd=attempt.cost_usd,
            slot=attempt.slot,
            fallback_depth=attempt.fallback_depth,
            latency_ms=attempt.latency_ms,
            est_tokens_in=attempt.est_tokens_in,
            est_tokens_out_p50=attempt.est_tokens_out_p50,
            est_tokens_out_p90=attempt.est_tokens_out_p90,
            est_cost_usd_p90=attempt.est_cost_usd_p90,
            error=error,
        )
        settings = repo.get_context_settings(self._conn)
        # 遮罩與截斷在交易外先完成;失敗時只記錄例外型別(不含內容或秘密),略過 payload、帳本列照寫
        draft = None
        if attempt.request_text is not None and self._should_record(attempt, settings):
            try:
                draft = self._build_payload(attempt, secrets, settings.max_bytes)
            except Exception as exc:  # noqa: BLE001 - 帳本列不可因 payload 失敗而遺失
                _log.error("組 payload 失敗,略過 payload(%s)", type(exc).__name__)
        with transaction(self._conn):
            ledger_id = repo.insert_ledger(self._conn, row)
            if draft is not None:
                repo.insert_payload(self._conn, dataclasses.replace(draft, ledger_id=ledger_id))
        return ledger_id

    def payload_status(self, ledger_id: int, now: datetime) -> PayloadStatus:
        return payload_status(self._conn, ledger_id, now)

    @staticmethod
    def _should_record(attempt: Attempt, settings: repo.ContextSettings) -> bool:
        if settings.retention_days <= 0:
            return False
        if attempt.route_record_context is None:
            return settings.record_default
        return bool(attempt.route_record_context)

    def _build_payload(
        self, attempt: Attempt, secrets: tuple[str, ...], max_bytes: int
    ) -> repo.Payload:
        assert attempt.request_text is not None
        req, req_hit = self._redactor.redact(attempt.request_text, secrets)
        resp_hit = False
        resp: str | None = None
        if attempt.response_text is not None:
            resp, resp_hit = self._redactor.redact(attempt.response_text, secrets)

        req_b = _utf8(req)
        resp_b = _utf8(resp) if resp is not None else b""
        req_lim, resp_lim = _split_budget(len(req_b), len(resp_b), max_bytes)
        req_out = _truncate_utf8(req_b, req_lim)
        resp_out = _truncate_utf8(resp_b, resp_lim)
        truncated = len(req_out) < len(req_b) or len(resp_out) < len(resp_b)

        return repo.Payload(
            ledger_id=0,  # 佔位,寫入時換成實際 id
            ts=attempt.ts,
            request_json=req_out.decode("utf-8"),
            response_text=None if resp is None else resp_out.decode("utf-8"),
            bytes=len(req_out) + len(resp_out),
            truncated=truncated,
            redacted=req_hit or resp_hit,
        )


def _cutoff(now: datetime, retention_days: int) -> datetime:
    return now - timedelta(days=retention_days)


def payload_status(conn: sqlite3.Connection, ledger_id: int, now: datetime) -> PayloadStatus:
    """payload 狀態(決策 17;先以時間判斷是否過期,再看 payload 列)。

    - 保存天數 > 0 且 `usage_ledger.ts` 早於 `now − 保存天數` → `expired`(即使清理尚未執行);
    - 保存天數 == 0 且 payload 列仍在(尚未清理)→ `expired`;
    - payload 列存在 → `ok`;
    - 其餘(route 關閉、沒有送出內容、已清理的 0 天設定)→ `never_recorded`。
    - ledger_id 不存在 → 丟 `LedgerNotFound`(與 never_recorded 區分;呼叫端皆可回 404)。

    已知限制:以 ts 判斷,當初未記錄但已超過保存期限的列也會回 `expired`;保存天數改為 0 並清理後,
    曾記錄過的列回 `never_recorded`(刪除後無法區分)。
    """
    led = repo.get_ledger(conn, ledger_id)
    if led is None:
        raise LedgerNotFound(ledger_id)
    days = repo.get_context_settings(conn).retention_days
    has_payload = repo.get_payload(conn, ledger_id) is not None
    if days > 0 and led.ts < _cutoff(now, days):
        return "expired"
    if days <= 0 and has_payload:
        return "expired"
    if has_payload:
        return "ok"
    return "never_recorded"


class RetentionJob:
    """刪除過期 payload(2.4.5)。排程(每小時)由 app 負責,本類只做單次清理。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def run_once(self, now: datetime | None = None) -> int:
        """刪除 `ts` 早於 `now − 保存天數` 的 payload,回傳刪除列數;`now` 省略時取注入的 Clock。

        保存天數為 0 時期限即 `now`,早於 `now` 的 payload 全數刪除。帳本列不動。
        """
        now = self._clock.now() if now is None else now
        days = max(repo.get_context_settings(self._conn).retention_days, 0)
        return repo.delete_payloads_before(self._conn, _cutoff(now, days))
