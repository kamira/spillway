"""Token 與花費預估(spec 5.4.1–5.4.4;CHG-20261030-02 T9)。

`Estimator.estimate(dep, req, route_name)` 回傳 `Estimate`,供 precheck(4.2.5)、帳本、
`X-Spillway-Est-Cost` header 與 dry-run 使用。

輸入 token(5.4.2)
    以 `litellm.token_counter(model=model_string(dep), messages=req.messages)` 計算;
    `model_string` 與 adapter 呼叫共用(`adapters.base`,spec 7.2.1):provider 為 `litellm`
    (直通)時原樣使用 `model`,其他一律 `<provider>/<model>`。
    LiteLLM 經 `adapters.base.load_litellm()` 載入,其中 `disable_hf_tokenizer_download=True`:
    llama 系等模型名不從 HuggingFace 下載 tokenizer,退回 tiktoken(不連網、不阻塞事件迴圈)。
    token_counter 拋出任何例外或回傳非正整數以外的值時,退回「字元數 ÷ 4」:
    `ceil(req.text_chars / 4)`(`text_chars` 為各訊息文字內容的字元總數,見 `ChatRequest`)。
    `kind == "cli"` 的成員另加 `params.cli_overhead_tokens`(未設定或不是非負整數時視為 0)。

輸出 token(5.4.3)
    `req.max_tokens` 有值時 P50 = P90 = `max_tokens`。
    否則取帳本中 `route_name` 相同(None 時以 `route_name IS NULL` 比對,即 `local/` 直呼)、
    `deployment_id` 相同、`status = 'ok'` 且 `tokens_out IS NOT NULL` 的列,依 `ts DESC, id DESC`
    取最近 50 筆;`tokens_out` 為 NULL 的列不計入,也不佔 50 筆的名額。
    樣本數 n < 5 時 P50 = 500、P90 = 2000。
    百分位採 nearest-rank 定義:樣本由小到大排序為 x[1..n],
    第 p 百分位 = x[ceil(p / 100 × n)](1 起算;結果必為樣本中的實際值)。
    例:n = 50 時 P50 = x[25]、P90 = x[45]。

花費(5.4.4)
    `cost_usd_p90 = tokens_in × 每 token 輸入價 + out_p90 × 每 token 輸出價`,
    價格來自 LiteLLM 的本地價格表(`litellm.cost_per_token`;不連網,
    import litellm 前設定 `LITELLM_LOCAL_MODEL_COST_MAP=True`)。
    以 `model_string(dep)` 查價;`kind == "cli"` 查不到時再以裸 `model` 查一次
    (CLI provider 名稱如 `claude-cli` 不在價格表,但其模型名可能在)。
    以下情況為 None(不參與 4.2.5 的 precheck):`kind == "local"`、`provider == "codex-cli"`、
    價格表查不到該模型(cost_per_token 拋例外)、`model_string` 不合法(例如 `litellm` 直通的
    model 沒有 `/`)。
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from spillway.adapters.base import load_litellm, model_string
from spillway.domain import ChatRequest, Deployment

HISTORY_WINDOW = 50
MIN_SAMPLES = 5
DEFAULT_OUT_P50 = 500
DEFAULT_OUT_P90 = 2000
NO_PRICE_PROVIDERS = frozenset({"codex-cli"})

ConnSource = sqlite3.Connection | Callable[[], sqlite3.Connection]


@dataclass(frozen=True)
class Estimate:
    tokens_in: int
    out_p50: int
    out_p90: int
    cost_usd_p90: float | None


def percentile_nearest_rank(values: Sequence[int], p: float) -> int:
    """nearest-rank 百分位:排序後取第 ceil(p/100 × n) 個(1 起算)。`values` 不可為空。"""
    if not values:
        raise ValueError("values 不可為空")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _cli_overhead(dep: Deployment) -> int:
    v = (dep.params or {}).get("cli_overhead_tokens", 0)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


class Estimator:
    """依帳本與 LiteLLM 本地價格表計算預估。

    `conn` 可為 `sqlite3.Connection` 或無參數的連線工廠;工廠於每次 `estimate()` 呼叫一次,
    連線的生命週期由工廠的擁有者負責(本類別不關閉它)。
    """

    def __init__(self, conn: ConnSource) -> None:
        self._conn_source = conn

    def _conn(self) -> sqlite3.Connection:
        src = self._conn_source
        return src if isinstance(src, sqlite3.Connection) else src()

    # ------------------------------------------------------------------ 輸入
    def tokens_in(self, dep: Deployment, req: ChatRequest) -> int:
        n: int | None
        try:
            n = load_litellm().token_counter(model=model_string(dep), messages=req.messages)
        except Exception:  # noqa: BLE001 — 任何失敗都退回字元估算
            n = None
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            n = math.ceil(req.text_chars / 4)
        if dep.kind == "cli":
            n += _cli_overhead(dep)
        return n

    # ------------------------------------------------------------------ 輸出
    def recent_tokens_out(self, dep: Deployment, route_name: str | None) -> list[int]:
        """同 route 與成員最近 50 筆 ok 且 tokens_out 非 NULL 的 tokens_out(新到舊)。

        `repo.list_ledger` 的 `route_name=None` 代表「不篩選」,無法表達 `IS NULL`,
        也不能排除 tokens_out 為 NULL 的列,因此這裡直接以參數化 SQL 查詢(唯讀)。
        """
        sql = (
            "SELECT tokens_out FROM usage_ledger "
            "WHERE deployment_id = ? AND status = 'ok' AND tokens_out IS NOT NULL AND "
            + ("route_name IS NULL" if route_name is None else "route_name = ?")
            + " ORDER BY ts DESC, id DESC LIMIT ?"
        )
        args: list[Any] = [dep.id]
        if route_name is not None:
            args.append(route_name)
        args.append(HISTORY_WINDOW)
        return [int(r[0]) for r in self._conn().execute(sql, args)]

    def tokens_out(
        self, dep: Deployment, req: ChatRequest, route_name: str | None
    ) -> tuple[int, int]:
        if req.max_tokens is not None:
            return req.max_tokens, req.max_tokens
        samples = self.recent_tokens_out(dep, route_name)
        if len(samples) < MIN_SAMPLES:
            return DEFAULT_OUT_P50, DEFAULT_OUT_P90
        return percentile_nearest_rank(samples, 50), percentile_nearest_rank(samples, 90)

    # ------------------------------------------------------------------ 花費
    def cost_usd(self, dep: Deployment, tokens_in: int, tokens_out: int) -> float | None:
        if dep.kind == "local" or dep.provider in NO_PRICE_PROVIDERS:
            return None
        candidates: list[str] = []
        try:
            candidates.append(model_string(dep))
        except ValueError:
            pass
        if dep.kind == "cli" and dep.model:
            candidates.append(dep.model)
        litellm = load_litellm()
        for model in candidates:
            try:
                cin, cout = litellm.cost_per_token(
                    model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out
                )
            except Exception:  # noqa: BLE001 — 價格表查不到即視為無定價
                continue
            return float(cin) + float(cout)
        return None

    # ------------------------------------------------------------------ 合併
    def estimate(self, dep: Deployment, req: ChatRequest, route_name: str | None) -> Estimate:
        t_in = self.tokens_in(dep, req)
        p50, p90 = self.tokens_out(dep, req, route_name)
        return Estimate(
            tokens_in=t_in, out_p50=p50, out_p90=p90, cost_usd_p90=self.cost_usd(dep, t_in, p90)
        )
