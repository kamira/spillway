"""claude_cli:以 Claude Code 訂閱登入的 `claude -p` 子行程處理請求(spec 7.3、5.2.2、7.6.9;
CHG-20261101-03 決策 9、10、12)。

子行程一律經 `AdapterContext.cli_runner`(`adapters.base.CliRunner`)啟動;本模組只組 argv、stdin、
cwd、環境清理清單,解析 JSON 輸出並歸類。併發名額、成員憑證變數的移除與 `extra_secrets` 遮罩由
Router 處理(決策 7、9)。沒有 `list_models()`:可用模型只來自 `models.yaml`,列出模型不起子行程。
"""
from __future__ import annotations

import copy

from spillway.adapters.base import AdapterContext, AdapterManifest

from .adapter import CLAUDE_DEFAULT_SIGNALS, ClaudeCliAdapter

MANIFEST = AdapterManifest(
    providers=("claude-cli",),
    kinds=("cli",),
    access=("subscription-cli",),
    cardinality="single",
    # 上游值經 params.effort_args 的 `{effort}` 模板傳給 `claude --effort`(spec 6.3.2;決策 10)
    effort_map={"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
    default_signals=copy.deepcopy(CLAUDE_DEFAULT_SIGNALS),
    default_capabilities={"ctx": 1000000, "tools": False, "vision": False,
                          "efforts": ["low", "medium", "high", "max"]},
    default_params={"timeout": 600, "effort_args": ["--effort", "{effort}"], "extra_args": []},
    requires=("claude",),
    contract_version=1,
)


def create(context: AdapterContext) -> ClaudeCliAdapter:
    """`context.cli_runner` 為 None 時照樣建立;此時 `call()` 一律回 `error`。"""
    return ClaudeCliAdapter(context, MANIFEST)
