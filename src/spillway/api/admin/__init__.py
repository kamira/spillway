"""管理 API `/api/*`(spec 9.2 本筆範圍、9.2.1、9.2.2、9.2.3、9.3;CHG-20261030-02 T14;
design D13、D15、D22;決策 6、16、17、22、24、25、32)。

掛載:`app.include_router(admin.router)`(T15)。本模組只匯出 `router`,不修改 `app.py`。

套件配置(CHG-20261101-02 決策 9;依原 `admin.py` 的段落純搬移,行為不變):
- `common`:錯誤工具、`_AdminRoute`、`router` 本體、共用 helper
- `models`:成員、群組、route 的請求 body 模型(含呼叫憑證掃描的 validator)
- `secrets`:憑證鍵名與值樣式掃描
- `deployments`(含 test-call)、`groups`、`routes`(含 order)、`planning`(dry-run、estimate)、
  `ledger`、`settings`、`dashboard`、`adapters`:各端點群
各子模組以 `common.router` 註冊端點,本檔依原檔順序匯入(端點註冊順序不變);logger 名稱一律
`spillway.api.admin`。另轉匯出既有測試讀取的屬性(`ApiError`、`QuotaService`、`test_deployment`、
`TEST_TEXT_PREVIEW`、`ChatBody`、`DeploymentIn`、`StatusLit`),皆為子模組內的同一物件。

依賴(一律從 `request.app.state` 取得,與 T13 `create_app` 放的屬性名一致):
- `registry`:`AdapterRegistry`(已 `discover()`,帶 `AdapterContext`)
- `router`:`routing.Router`(dry-run 與 `/api/estimate` 用)
- `conn_factory`:零參數函式,每次呼叫回傳**新開**的 `sqlite3.Connection`;本模組每個請求經
  `api.deps.request_conn(request)` 呼叫一次、請求結束時關閉(與 `Router(..., close_conn=True)` 的假設相同)
- `clock`:`spillway.clock.Clock`

錯誤(spec 9.1.2;決策 16、17、22):一律 `{"error": {"code", "message"}}`,格式工具與
`/v1/*` 共用 `spillway.api` 的 `ApiError`、`error_response`、`validation_message`。
- 422 `validation_error`:body/query/path 驗證失敗(含 FastAPI 的 `RequestValidationError`,
  由本 router 的 `_AdminRoute` 轉換,不依賴 app 層 handler);message 為「欄位路徑: 說明」,
  多筆以「; 」連接,路徑以 `.` 連接(例:`body.capabilities.efforts.1`)。**不回顯輸入值**。
- 404 `not_found`:資源不存在;payload 從未記錄或帳本列不存在。
- 409 `conflict`:id/name 重複;刪除仍被 route 參照的群組。
- 409 `provider_single`:`single` 模組的 provider 已有成員(POST,或 PUT 變更 provider / kind)。
錯誤訊息中回顯的使用者輸入(id、名稱、model)一律先經 Redactor 遮罩。
- 422 `member_not_in_target`:`PUT /api/routes/{name}/order` 的成員不屬 route 目標(9.3.1)。
- 410 `payload_expired`:payload 已過保存期限(決策 17)。

寫入驗證(決策 22):`credential_ref` 只接受 `env:…`、`keyring:…:…`(`credentials.validate_ref`);
`params` 與 `capabilities` 的額外鍵內任何字串(含巢狀與鍵名)命中 `Redactor.matches`,或任一層
鍵名不分大小寫完全等於 `CREDENTIAL_KEY_NAMES`(api_key、apikey、key、token、access_token、secret、
password、authorization;U9)即 422,不回顯值;`reset_anchor` 1–28;
`context_retention_days` 0–36500;
`kind`、`effort`、`weight`、`mode`、`window`、budget `type` 只接受 DDL 的合法值;
`capabilities.efforts` 只可含五級。成員 id、群組名不可含空白與 `/`;route 名另不可含 `:`
(避免與 `local/`、`route:` 前綴混淆)。各 body 不接受未知欄位(`/api/estimate`、dry-run 的請求 body
除外:其他鍵只轉交 OpenAI 標準參數白名單,名單外靜默丟棄)。

各端點回應格式見各函式 docstring。時間一律 UTC ISO 8601 到秒(`2026-10-30T12:00:00Z`)。
"""
from __future__ import annotations

# 匯入順序 = 端點註冊順序(與原 admin.py 相同)
from . import deployments, groups, routes, planning, ledger, settings, dashboard, adapters  # noqa: F401
from .common import ApiError, router
# 以下轉匯出供既有測試與向後相容使用(與子模組內為同一物件);公開 API 仍只有 router
from .deployments import TEST_TEXT_PREVIEW, QuotaService, test_deployment
from .ledger import StatusLit
from .models import DeploymentIn
from .planning import ChatBody

__all__ = ["router"]
