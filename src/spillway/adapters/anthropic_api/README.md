# anthropic_api

以 Anthropic API key 經 LiteLLM 呼叫 Anthropic Messages API(spec 7.2、7.6.9)。

| 項目 | 值 |
|------|----|
| provider | `anthropic` |
| kind / access | `api` / `api` |
| cardinality | `multiple`(可建多個成員,例如不同金鑰或不同模型) |
| 模型字串 | `anthropic/<model>`;`model` 已帶 `anthropic/` 前綴時不重複加 |
| requires | 無(金鑰不靠環境變數,見「憑證」) |

## 憑證

成員必須設定 `credential_ref`(`env:變數名` 或 `keyring:服務:帳號`)。呼叫當下經
`AdapterContext.credentials` 解析,明確以 `api_key` 傳給 LiteLLM;`credential_ref` 為空時
`call()` 直接回 `status="error"`,不發出請求,也不讓 LiteLLM 自行讀 `ANTHROPIC_API_KEY`。

## params

| 鍵 | 說明 | 預設 |
|----|------|------|
| `api_base` | 上游位址;不帶 `/v1`(LiteLLM 自行補 `/v1/messages`) | LiteLLM 預設(官方端點) |
| `timeout` | 逾時秒數(spec 7.2.3) | 120 |
| `signals` | 覆寫上游訊號判定規則(spec 5.3.1;格式見 `adapters/base/signals.py`) | manifest `default_signals` |

## 行為

- effort(spec 6.3):`none` 不傳;`low`/`medium`/`high`/`max` 以 `reasoning_effort` 交給
  LiteLLM,由它轉成 Anthropic 的 `thinking` / `output_config.effort`。
- 上游訊號(spec 5.3.1 api 列):HTTP 402 或錯誤碼 `insufficient_quota` → `quota_exhausted`;
  HTTP 429 → `rate_limited`;其他失敗 → `error`。Anthropic 錯誤 body 沒有 `code` 欄位,
  adapter 從訊息內嵌的 JSON 取 `error.type` 當錯誤碼。
- 花費:LiteLLM `completion_cost()`;無定價的模型為 None(spec 5.2.1)。
- `health()`:api 成員不探測,回 True;額度與限流由 `call()` 的 status 處理。

## 可用模型(models.yaml)

| id | ctx | tools | vision | efforts |
|----|-----|-------|--------|---------|
| `claude-opus-5-5` | 1,000,000 | ✓ | ✓ | low、medium、high、max |
| `claude-sonnet-5-5` | 1,000,000 | ✓ | ✓ | low、medium、high、max |
| `claude-haiku-5-5` | 1,000,000 | ✓ | ✓ | low、medium、high、max |
| `claude-sonnet-4-5` | 200,000 | ✓ | ✓ | low、medium、high、max |

清單外的模型可在 UI 以「其他(手動輸入)」填入 id。

## 已知限制

- 真實 Anthropic 的「餘額不足」回應不一定是 402 或 `insufficient_quota`;遇到時以成員
  `params.signals` 補規則(例如 `{"quota": [{"match": {"message": "credit balance"}}]}`)。
- `adapter.py` 與 openai_api、litellm 模組相同(模組之間不可互相 import)。
