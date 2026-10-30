# litellm

通用後備模組:沒有任何模組宣告的 `api`、`local` provider(例如 `deepseek`、`gemini`、`ollama`、
`hosted_vllm`)都由它經 LiteLLM 呼叫(spec 7.1.1、7.6.9)。

| 項目 | 值 |
|------|----|
| providers | `litellm`(見下) |
| fallback_for_kinds | `api`、`local` |
| kind / access | `api`、`local` / `api`、`local` |
| cardinality | `multiple` |
| 模型字串 | `<provider>/<model>`;`model` 已帶該前綴時不重複加 |
| requires | 無 |

## provider

manifest 的 `providers` 不可為空,所以只宣告 `litellm`;其他 provider 一律不宣告,經
`fallback_for_kinds` 落到本模組。這樣日後新增專屬模組(例如 `ollama`)不會與本模組衝突。

provider 填 `litellm` 的成員,`model` 欄直接寫完整 LiteLLM 模型字串(例如 `ollama/llama3`),
原樣傳入;沒有 `/` 時 `call()` 回 `error`。

## 憑證

- `api` 成員:必須設定 `credential_ref`,呼叫當下經 `AdapterContext.credentials` 解析並以
  `api_key` 明確傳入;為空時 `call()` 回 `error`,不發請求。
- `local` 成員:可不設 `credential_ref`,此時以 `api_key=None` 呼叫;有設定時照常解析並傳入。

## params

| 鍵 | 說明 | 預設 |
|----|------|------|
| `api_base` | 上游位址。OpenAI 相容協定(`hosted_vllm`、`deepseek`…)需帶 `/v1`。local 成員必填(健康檢查用) | LiteLLM 依 provider 的預設 |
| `timeout` | 逾時秒數(spec 7.2.3) | 120 |
| `signals` | 覆寫上游訊號判定規則(spec 5.3.1) | manifest `default_signals` |

## 行為

- effort(spec 6.3):`none` 不傳;其餘以 `reasoning_effort=<等級>` 交給 LiteLLM,由它依
  provider 轉換(OpenAI 相容端點原樣送出,部分 provider 轉成 `thinking`)。
- 上游訊號(spec 5.3.1 api 列):HTTP 402 或錯誤碼 `insufficient_quota` → `quota_exhausted`;
  HTTP 429 → `rate_limited`;其他失敗 → `error`。
- 花費:`api` 成員用 LiteLLM `completion_cost()`(無定價為 None);`local` 成員一律 0(spec 5.2.4)。
- `health()`:`local` 成員對 `params.api_base` 發一次 `GET`,2 秒內收到任何 HTTP 回應即健康
  (決策 10;經 `adapters.base.probe_http`);沒有 `api_base` 回 False。`api` 成員回 True。

## list_models(context, api_base=None)

執行期查詢 local 端點的 OpenAI 相容 `/v1/models`,回傳 `[{"id": ...}]`,由
`adapters.base.available_models()` 與 `models.yaml` 合併(同 id 以 `models.yaml` 為準;
執行期項目的能力取 manifest `default_capabilities`)。查詢失敗時只回 `models.yaml` 並附原因。

端點依序取:參數 `api_base` → 環境變數 `SPILLWAY_LOCAL_API_BASE` → Ollama 預設
`http://127.0.0.1:11434/v1`。結尾有無 `/v1` 皆可。逾時 2 秒,不讀 proxy 環境變數、不跟隨轉址。

## 可用模型(models.yaml)

| id | provider | ctx | tools | vision | efforts |
|----|----------|-----|-------|--------|---------|
| `deepseek-chat` | deepseek | 131,072 | ✓ | — | (無) |
| `deepseek-reasoner` | deepseek | 131,072 | — | — | (無) |
| `gemini-2.5-flash` | gemini | 1,048,576 | ✓ | ✓ | low、medium、high |

local 端點的模型不列在這裡,由 `list_models()` 查詢。

## 已知限制

- `adapter.py` 與 anthropic_api、openai_api 模組相同(模組之間不可互相 import)。
- `list_models(context)` 只拿得到 `AdapterContext`,不知道個別成員的 `api_base`;多個 local
  端點時只查詢上述單一端點。
