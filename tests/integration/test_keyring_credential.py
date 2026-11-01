"""keyring 憑證端到端(CHG-20261101-02 T10 L2)。

以記憶體 keyring 後端(不碰系統 Keychain;真實 macOS Keychain 路徑不在自動測試中,見 T10 L4)存入
假金鑰,成員 `credential_ref = keyring:spillway-test:acct`,經 `/v1/chat/completions` 打假上游:
回 200、假上游收到的 API key 為該值、帳本與 payload 不含該值。

假上游 `RecordedRequest` 刻意不保存金鑰值,所以本測試以 monkeypatch 包住伺服器模組的
`_auth_value`(同一行程內的背景執行緒),只在測試內記下收到的 header 值。
"""
from __future__ import annotations

import keyring
import pytest

from spillway.domain import Deployment
from spillway.store import repo
from tests.fakes.api_upstream import server as fake_server
from tests.helpers import memory_keyring  # noqa: F401 — fixture 註冊

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000"
SERVICE = "spillway-test"
ACCOUNT = "acct"
MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def seen_auth(monkeypatch):
    seen: list[tuple[str | None, str | None]] = []
    original = fake_server._auth_value

    def spy(request):
        out = original(request)
        seen.append(out)
        return out

    monkeypatch.setattr(fake_server, "_auth_value", spy)
    return seen


async def test_keyring_credential_reaches_upstream_and_stays_out_of_storage(
        open_spillway, fake_api, memory_keyring, seen_auth, monkeypatch):
    # 移除 env 憑證來源,確保請求帶到上游的金鑰只可能來自 keyring
    monkeypatch.delenv("SPILLWAY_TEST_KEY", raising=False)
    keyring.set_password(SERVICE, ACCOUNT, KEY)
    async with open_spillway() as h:
        h.add_deployment(Deployment(
            id="kr-1", model_name="kr-1", kind="api", provider="openai", model="gpt-4o-mini",
            params={"api_base": f"{fake_api.base_url}/v1"},
            credential_ref=f"keyring:{SERVICE}:{ACCOUNT}",
            capabilities={"ctx": 128000, "tools": True, "vision": False, "efforts": []},
            tags=("kr",),
        ))
        h.add_route(repo.Route(name="kr", target={"tags": {"all": ["kr"]}}, record_context=True))

        r = await h.client.post("/v1/chat/completions", json={"model": "kr", "messages": MSGS})
        assert r.status_code == 200, r.text
        assert r.headers["X-Spillway-Served-By"] == "kr-1"
        assert KEY not in r.text

        chat = [x for x in fake_api.requests if x.path == "/v1/chat/completions"]
        assert len(chat) == 1 and chat[0].has_auth
        assert seen_auth == [("authorization", f"Bearer {KEY}")]

        with h.db() as conn:
            ledger = [tuple(row) for row in conn.execute("SELECT * FROM usage_ledger")]
            payloads = [tuple(row) for row in conn.execute("SELECT * FROM usage_payloads")]
        assert len(ledger) == 1 and len(payloads) == 1
        assert [x.status for x in h.ledger()] == ["ok"]
        for row in ledger + payloads:
            assert all(KEY not in str(v) for v in row), row
