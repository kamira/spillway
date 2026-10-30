"""假 API 上游(CHG-20261030-02 T5;決策 9、10;design D5)。

在 127.0.0.1 隨機埠以背景執行緒跑 uvicorn + Starlette。只綁 127.0.0.1,不連外部網路。

端點
----
- `GET /`                     健康檢查,200 `{"status":"ok"}`
- `POST /v1/chat/completions` OpenAI 相容;成功回 `choices[0].message.content` 與
                              `usage.prompt_tokens`/`completion_tokens`(11/7)
- `GET /v1/models`            OpenAI 形狀模型清單(預設三個 + 已設定模式的 model)
- `POST /v1/messages`         Anthropic Messages;成功回 `content[0].text` 與
                              `usage.input_tokens`/`output_tokens`(11/7)
- `POST /_fake/mode`、`POST /_fake/reset`、`GET /_fake/requests`
                              控制端點,供子行程(驗收操作)從外部設定;pytest 內用 fixture。
- `python -m tests.fakes.api_upstream [--port N]` 以前景行程啟動(仍只綁 127.0.0.1)。

模式(依 model 名稱設定,未設定者用 `"*"` 的設定,再無則 ok)
--------------------------------------------------------------
| 模式               | HTTP | OpenAI error.type / code                   | Anthropic error.type   |
|--------------------|------|--------------------------------------------|------------------------|
| ok                 | 200  | —                                          | —                      |
| http_402           | 402  | billing_error / payment_required           | billing_error          |
| insufficient_quota | 429  | insufficient_quota / insufficient_quota    | insufficient_quota     |
| http_429           | 429  | rate_limit_error / rate_limit_exceeded     | rate_limit_error       |
| http_500           | 500  | server_error / null                        | api_error              |
| delay(seconds=)    | 200  | 延遲 seconds 秒後回成功                    | 同左                   |
| echo_auth(status=) | 400* | invalid_request_error / invalid_api_key,  | authentication_error,  |
|                    |      | message 含 Authorization 值                | message 含 x-api-key 值|

insufficient_quota 選 HTTP 429:與 OpenAI 實際行為一致(額度用完回 429,
`error.type` 與 `error.code` 皆為 `insufficient_quota`)。所以 T6 必須先看錯誤碼、
再看 429,否則會誤判為限流。echo_auth 預設 400,可用 `status=` 改。
錯誤 body:OpenAI 形狀 `{"error":{"message","type","param","code"}}`;
Anthropic 形狀 `{"type":"error","error":{"type","message"}}`。

LiteLLM 對接實測(litellm 1.x,本 venv,2026-10-30;`max_retries=0`)
-------------------------------------------------------------------
api_base 格式:
- `openai/<m>`、`hosted_vllm/<m>`:`api_base=f"{base_url}/v1"`。不帶 `/v1` 會打到
  `/chat/completions` → 404 `NotFoundError`。
- `anthropic/<m>`:`api_base=base_url`(不帶 `/v1`;LiteLLM 自行補 `/v1/messages`)。
  帶 `/v1` → 404 `NotFoundError`;完整 `.../v1/messages` 也可用。
- auth:openai/hosted_vllm 送 `Authorization: Bearer <key>`;anthropic 送 `x-api-key`。

成功:三種前綴皆可解析 content 與 usage(11/7)。`completion_cost()`:
`openai/gpt-4o-mini` = 5.85e-06、`anthropic/claude-sonnet-4-5` = 0.000138;
`hosted_vllm/local-model` 拋 `ModelNotMappedError`(local 成員 cost 應直接記 0)。

例外(類別皆在 `litellm.exceptions`;`status_code`):
| 模式               | openai/                    | hosted_vllm/               | anthropic/                    |
|--------------------|----------------------------|----------------------------|-------------------------------|
| http_402           | APIError 402               | APIError 402               | PaymentRequiredError 402      |
| insufficient_quota | RateLimitError 429         | RateLimitError 429         | RateLimitError 429            |
| http_429           | RateLimitError 429         | RateLimitError 429         | RateLimitError 429            |
| http_500           | InternalServerError 500    | InternalServerError 500    | InternalServerError 500       |
| echo_auth          | BadRequestError 400        | BadRequestError 400        | BadRequestError 400           |
| delay > timeout    | Timeout 408                | Timeout 408                | Timeout 408                   |

區分 insufficient_quota 與一般 429(兩者例外類別與 status 相同):
- `openai/`:`str(e)` **不含** `insufficient_quota` 字樣(只有 message 文字),
  但 `e.body == {"message":..., "type":"insufficient_quota", "code":"insufficient_quota", ...}`。
- `hosted_vllm/`、`anthropic/`:`e.body` 為 None,但 `str(e)` 含原始 JSON body,
  故字串中出現 `insufficient_quota`。
=> T6 歸類時應同時檢查 `e.body.get("code")` 與 `str(e)`。

秘密:echo_auth 模式下 `str(e)` 會含 `sk-test-0000`(openai 形式為 `Bearer sk-test-0000`),
T6 寫入 `CallResult.error` 前必須遮罩。請求紀錄只存 `has_auth` 與 header 名稱,不存值。
"""
from tests.fakes.api_upstream.server import FakeApiServer, ModeSpec, RecordedRequest

__all__ = ["FakeApiServer", "ModeSpec", "RecordedRequest"]
