# anthropic_api

以 Anthropic API key 經 LiteLLM 呼叫 Anthropic Messages API(spec 7.2、7.6.9)。

| 項目 | 值 |
|------|----|
| provider | `anthropic` |
| kind / access | `api` / `api` |
| cardinality | `multiple`(可建多個成員,例如不同金鑰或不同模型) |
| 模型字串 | 一律 `anthropic/<model>`(`adapters.base.model_string`,spec 7.2.1);`model` 本身以 `anthropic/` 開頭時也照補 |
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
- 上游訊號(spec 5.3.1 api(anthropic)列,決策 27):
  - `quota_exhausted`:HTTP 402;錯誤碼 `insufficient_quota`;HTTP 400 且訊息符合 `credit balance`
    (真實 Anthropic 餘額不足回 400 `invalid_request_error`「Your credit balance is too low…」)。
  - `rate_limited`:HTTP 429;HTTP 529(overloaded)。LiteLLM 會把 529 包成 status 500 的
    `InternalServerError`,`litellm_call` 沿例外鏈取回原始的 529。
  - 其他失敗 → `error`。Anthropic 錯誤 body 沒有 `code` 欄位,adapter 從訊息內嵌的 JSON 取
    `error.type` 當錯誤碼。
- 只回工具呼叫時:`CallResult.tool_calls` 為 OpenAI 格式清單、`text` 為空字串、`finish_reason`
  照上游(spec 7.1,決策 28)。
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

- 開推理(effort `low`/`medium`/`high`/`max`)時,LiteLLM 把等級轉成 `thinking.budget_tokens`
  (low 1024、high 4096、max 16384…)。請求帶的 `max_tokens` ≤ 推理預算時(例如 `max_tokens=500`
  搭配 `high`),上游會回 HTTP 400,歸類為 `error`,由 fallback 接手;帳本可查到這次失敗。
  請求不帶 `max_tokens` 時 LiteLLM 自動補成預算 + 4096,不受影響。`temperature` ≠ 1 搭配推理也屬
  同類(決策 26,M2 已知限制)。
- 其他形式的「餘額不足」訊息仍可用成員 `params.signals` 補規則(格式見 `adapters/base/signals.py`)。
- `adapter.py` 與 openai_api、litellm 模組相同(模組之間不可互相 import);`tests/unit/adapters/test_review_w3_fixes.py` 檢查三份逐字相同。
