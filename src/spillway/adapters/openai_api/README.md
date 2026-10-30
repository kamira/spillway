# openai_api

以 OpenAI API key 經 LiteLLM 呼叫 OpenAI Chat Completions(spec 7.2、7.6.9)。

| 項目 | 值 |
|------|----|
| provider | `openai` |
| kind / access | `api` / `api` |
| cardinality | `multiple` |
| 模型字串 | 一律 `openai/<model>`(`adapters.base.model_string`,spec 7.2.1);`model` 本身以 `openai/` 開頭時也照補,例如 `openai/gpt-oss-20b` → `openai/openai/gpt-oss-20b`,上游收到 `openai/gpt-oss-20b` |
| requires | 無(金鑰不靠環境變數,見「憑證」) |

## 憑證

成員必須設定 `credential_ref`(`env:變數名` 或 `keyring:服務:帳號`)。呼叫當下經
`AdapterContext.credentials` 解析,明確以 `api_key` 傳給 LiteLLM;`credential_ref` 為空時
`call()` 直接回 `status="error"`,不發出請求,也不讓 LiteLLM 自行讀 `OPENAI_API_KEY`。

## params

| 鍵 | 說明 | 預設 |
|----|------|------|
| `api_base` | 上游位址,需帶 `/v1`(例:`https://api.openai.com/v1`) | LiteLLM 預設(官方端點) |
| `timeout` | 逾時秒數(spec 7.2.3) | 120 |
| `signals` | 覆寫上游訊號判定規則(spec 5.3.1) | manifest `default_signals` |

## 行為

- effort(spec 6.3):`none` 不傳;其餘以 `reasoning_effort=<等級>` 送出。OpenAI 不接受 `max`,
  所以清單中的模型 `efforts` 不含 `max`,由路由依 6.2 降級。非推理模型(如 `gpt-4o-mini`)
  收到 `reasoning_effort` 會被 LiteLLM 拒絕,`efforts` 為空,路由不會送。
- 上游訊號(spec 5.3.1 api 列):HTTP 402 或錯誤碼 `insufficient_quota` → `quota_exhausted`
  (OpenAI 額度用完回 HTTP 429 + `insufficient_quota`,錯誤碼先比對);HTTP 429 → `rate_limited`;
  其他失敗 → `error`。
- 花費:LiteLLM `completion_cost()`;無定價的模型為 None(spec 5.2.1)。
- `health()`:api 成員不探測,回 True。

## 可用模型(models.yaml)

| id | ctx | tools | vision | efforts |
|----|-----|-------|--------|---------|
| `gpt-5.5` | 1,050,000 | ✓ | ✓ | low、medium、high |
| `gpt-5.4-mini` | 272,000 | ✓ | ✓ | low、medium、high |
| `gpt-5.4-nano` | 272,000 | ✓ | ✓ | low、medium、high |
| `gpt-4o-mini` | 128,000 | ✓ | ✓ | (無) |

清單外的模型可在 UI 以「其他(手動輸入)」填入 id。

## 已知限制

- `adapter.py` 與 anthropic_api、litellm 模組相同(模組之間不可互相 import);`tests/unit/adapters/test_review_w3_fixes.py` 檢查三份逐字相同。
