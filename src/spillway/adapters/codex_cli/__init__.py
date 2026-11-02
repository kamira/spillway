"""codex_cli:以訂閱登入的 Codex CLI(`codex exec`)子行程處理請求(spec 7.4、7.6.9;
CHG-20261101-03 決策 9、11、12)。行為說明見 `adapter.py` 與 README.md。"""
from __future__ import annotations

from spillway.adapters.base import AdapterContext, AdapterManifest

from .adapter import CodexCliAdapter

MANIFEST = AdapterManifest(
    providers=("codex-cli",),
    kinds=("cli",),
    access=("subscription-cli",),
    cardinality="single",
    # spec 6.3:codex-cli 以 `-c model_reasoning_effort=<值>` 傳遞
    effort_map={"none": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "xhigh"},
    # spec 5.3.1 codex-cli 列
    default_signals={
        "quota": [{"exit_code_not": 0, "match": {"stderr": "usage limit|quota"}}],
        "rate_limit": [{"match": {"stderr": "rate limit|429"}}],
    },
    # spec 7.5.2:含 tools 或圖片的請求不送 CLI 成員
    default_capabilities={"ctx": 272000, "tools": False, "vision": False,
                          "efforts": ["low", "medium", "high", "max"]},
    # cwd 未設定時為 AdapterContext.data_dir / "work" / "codex"(決策 11)
    default_params={"timeout": 600, "extra_args": []},
    requires=("codex",),
    contract_version=1,
)


def create(context: AdapterContext) -> CodexCliAdapter:
    """`cli_runner` 為 None 時照樣建立;此時 `call` 回 `error`(決策 11)。"""
    return CodexCliAdapter(context, MANIFEST)
