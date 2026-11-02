"""設定匯出黃金檔與對應的資料庫內容(CHG-20261102-02 T3;決策 8)。

- `GOLDEN`:`tests/unit/data/config_golden.yaml`(每欄都有一組非預設值;同 slot 兩成員皆帶非預設
  weight、內嵌 tag target、`type: none` 的 budget)。
- `seed_golden(conn)`:種入黃金檔對應的資料庫(另含不該出現在匯出檔的 token 雜湊、冷卻、帳本、
  payload);slot 以 1 起算、position 以 0 起算(與 `PUT /api/routes/{name}/order` 的寫法一致)。
  T3 的匯出測試與 T4 的往返測試(黃金檔 replace 匯入空 DB 後的 DB 全表快照對照)共用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from spillway.domain import Budget, Deployment
from spillway.store import db as store_db
from spillway.store import repo

__all__ = ["GOLDEN", "TOKEN_HASH", "NOW", "seed_golden"]

GOLDEN = Path(__file__).resolve().parents[1] / "unit" / "data" / "config_golden.yaml"
TOKEN_HASH = "f" * 64
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def seed_golden(conn) -> None:
    """黃金檔對應的資料庫內容(另含不該出現在匯出檔的 token 雜湊、冷卻、帳本、payload)。"""
    with store_db.transaction(conn):
        repo.set_setting(conn, "record_context_default", False)
        repo.set_setting(conn, "context_retention_days", 30)
        repo.set_setting(conn, "context_max_bytes", 1048576)
        repo.set_setting(conn, "access_token_sha256", TOKEN_HASH)
        repo.save_deployment(conn, Deployment(
            id="api-a", model_name="gpt-4o-mini", kind="api", provider="openai", model="gpt-4o-mini",
            params={"api_base": "http://127.0.0.1:9/v1", "timeout": 30},
            credential_ref="env:SPILLWAY_TEST_KEY",
            capabilities={"ctx": 128000, "efforts": ["low", "medium", "high"], "tools": True,
                          "vision": False},
            enabled=False, tags=("coding", "review"),
            budget=Budget(type="usd", limit_amount=30.0, window="monthly", reset_anchor=1)))
        repo.save_deployment(conn, Deployment(
            id="api-b", model_name="gpt-4.1-mini", kind="api", provider="openai",
            model="gpt-4.1-mini", credential_ref="keyring:spillway:api-b", tags=("cheap", "coding"),
            budget=Budget(type="credit_pool", limit_amount=100.0, window="weekly")))
        repo.save_deployment(conn, Deployment(
            id="bare", model_name="bare", kind="local", provider="hosted_vllm", model="local-model"))
        repo.save_deployment(conn, Deployment(
            id="claude-sub", model_name="sonnet", kind="cli", provider="claude-cli",
            model="claude-sonnet-4-5",
            params={"cwd": "/tmp/work", "extra_args": ["--verbose"], "nested": {"empty": {}}},
            tags=("coding",), budget=Budget(type="none", limit_amount=None, window="none")))
        repo.save_deployment(conn, Deployment(
            id="qwen-local", model_name="qwen-coder", kind="local", provider="ollama",
            model="qwen3-coder:30b", params={"api_base": "http://127.0.0.1:11434"},
            capabilities={"ctx": 32768, "efforts": []}, tags=("coding", "private-ok")))
        repo.save_group(conn, repo.Group("coding", {"all": ["coding"], "any": [], "none": []}))
        repo.save_group(conn, repo.Group("mixed", {"all": ["coding"], "any": ["review", "cheap"],
                                                   "none": ["private-ok"]}))
        repo.save_group(conn, repo.Group("中文群組", {"all": [], "any": [], "none": []}))
        repo.save_route(conn, repo.Route(
            name="review", target={"group": "coding"}, mode="task", tags=("daily", "alpha", "daily"),
            record_context=True, budget_precheck=True))
        repo.replace_route_targets(conn, "review", [
            repo.RouteTarget("claude-sub", slot=1, position=0, effort="high"),
            repo.RouteTarget("api-a", slot=2, position=0, effort="medium", weight="low"),
            repo.RouteTarget("api-b", slot=2, position=1, effort="none", weight="high"),
            repo.RouteTarget("qwen-local", slot=3, position=0, weight="low"),
        ])
        repo.save_route(conn, repo.Route(
            name="quick", target={"tags": {"all": ["coding"], "any": [], "none": ["review"]}}))
        repo.save_route(conn, repo.Route(
            name="silent", target={"tags": {"all": [], "any": [], "none": []}},
            record_context=False))
        # 以下不可出現在匯出檔
        repo.mark_cooldown(conn, "api-a", NOW, "rate_limited")
        lid = repo.insert_ledger(conn, repo.LedgerRow(ts=NOW, status="ok", deployment_id="api-a",
                                                      error="ledger-only-text"))
        repo.insert_payload(conn, repo.Payload(ledger_id=lid, ts=NOW,
                                               request_json='"payload-only-text"', bytes=19))
