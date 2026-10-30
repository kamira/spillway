# 目錄結構 — Spillway

- 專案:spillway · 分支:main · 版本:v1.1(2026-10-30,CHG-20261030-02:M2 施工檔案同步,SignalMatcher 移至 adapters/base,標註里程碑;使用者於 CHG 確認閘確認,2026-10-30) · 前版 v1.0 已確認(2026-10-29,使用者) · 日期:2026-10-30(UTC+0)
- 依據:sdlc_docs/structure/logical.md、design.md

## 樹狀

```
spillway/
├── AGENTS.md                    # AI 入口
├── README.md
├── .gitignore                   # .venv/、__pycache__/、*.egg-info/、.pytest_cache/、artifacts/
├── pyproject.toml               # 套件、相依、console script `spillway`
├── docs/                        # 企劃書、規格書、UI 設計稿(需求來源,不放帳本)
├── sdlc_docs/                   # 治理帳本
├── artifacts/                   # 驗收與量測產物(不進版控,M2 起)
├── src/spillway/
│   ├── __main__.py / cli.py     # spillway serve(M2) | mcp-stdio、init(M3)
│   ├── app.py                   # FastAPI 組裝、lifespan(背景清理工作)
│   ├── settings.py              # 資料目錄、埠號、綁定常數(M2 不依賴 store);token(M3)
│   ├── clock.py                 # Clock、Rng
│   ├── store/
│   │   ├── db.py                # 連線、PRAGMA、migration(user_version)
│   │   ├── schema.sql           # 與 spec 2.3 一致
│   │   └── repo.py              # 各表存取
│   ├── credentials.py
│   ├── redact.py
│   ├── routing/
│   │   ├── resolver.py
│   │   ├── filter.py
│   │   ├── planner.py
│   │   ├── selector.py
│   │   └── router.py
│   ├── quota.py                 # QuotaService(上游訊號判定 `classify` 在 adapters/base/)
│   ├── estimate.py
│   ├── effort.py
│   ├── ledger.py                # LedgerWriter、RetentionJob
│   ├── adapters/
│   │   ├── base/                # 共用契約:Adapter、CallResult、AdapterContext、AdapterManifest、registry(自動發現)、classify(上游訊號歸類)、effort_to_upstream、litellm_call;cli_runner(M3)
│   │   ├── litellm/             # 通用後備(api/local);每個模組:__init__.py(MANIFEST、create)、adapter.py、models.yaml、README.md
│   │   ├── claude_cli/          # 訂閱 CLI · single(M3)
│   │   ├── codex_cli/           # 訂閱 CLI · single(M3)
│   │   ├── anthropic_api/       # API · multiple
│   │   └── openai_api/          # API · multiple
│   ├── configio.py              # M4
│   ├── api/
│   │   ├── openai.py
│   │   └── admin.py
│   ├── mcp/                     # M3
│   │   ├── server.py
│   │   ├── bridge.py
│   │   └── config_gen.py
│   ├── templates/               # M3(mcp/)、M4(web/)
│   │   ├── mcp/<tool>.j2        # claude-code、codex、cursor、gemini-cli、stdio
│   │   └── web/*.html           # UI-1–UI-7
│   └── static/                  # 少量 JS 與 CSS(M4)
└── tests/
    ├── fakes/
    │   ├── api_upstream/        # 假 API 上游(OpenAI 相容與 Anthropic 協定;三個內建 API 模組共用,M2)
    │   └── <module>/            # 其他 adapter 模組自己的假上游(假 claude、codex 腳本,M3)
    ├── contract/                # 對所有被發現的 adapter 模組參數化執行的合約測試(test_adapter_contract.py、test_call_status.py)、分層檢查(test_layering.py)
    ├── unit/                    # 對應 src 模組
    ├── integration/             # HTTP、MCP、CLI 子行程(假上游)
    │   └── admin/               # 管理 API 整合測試(M2)
    ├── features/                # behave 行為情境(m2_*.feature)
    │   ├── steps/               # 步驟檔(T15 撰寫)
    │   └── environment.py
    └── acceptance/              # AC-1–21、NFR 量測
```

## 各目錄職責

| 路徑 | 職責 | 對應邏輯模組 |
|------|------|--------------|
| `src/spillway/api/` | HTTP 介面 | `api.openai`、`api.admin` |
| `src/spillway/mcp/` | MCP server、stdio 橋接、設定產生 | `mcp.server`、`mcp.bridge`、`mcpconf` |
| `src/spillway/templates/web`、`static/` | Web UI | `web` |
| `src/spillway/routing/` | 路由管線 | `routing` |
| `src/spillway/adapters/base/` | adapter 共用契約、自動發現、訊號歸類與 effort 轉換 | `adapters.base` |
| `src/spillway/adapters/<module>/` | 一種 AI 一個模組 | `adapters.<module>` |
| `tests/contract/` | adapter 合約測試、分層 import 檢查 | — |
| `src/spillway/store/` | SQLite | `store` |
| `tests/fakes/` | 假上游(Guideline Q1);API 共用 `api_upstream/` | — |
| `tests/features/` | behave 行為情境與步驟 | — |
| `artifacts/` | 驗收與量測產物,不進版控 | — |
| `tests/acceptance/` | 驗收項,檔名帶 AC 編號 | — |

## 命名與放置規則

- 套件採 `src/` layout;模組名小寫底線。
- 新增一種 AI:只新增 `adapters/<module>/` 子套件(含 `MANIFEST`、`create()`、`models.yaml`、`README.md`)與 `tests/fakes/<module>/`,不修改任何既有檔案;須通過 `tests/contract/`(design D11)。模組名小寫底線,`provider` 值用連字號(例:模組 `gemini_cli` 處理 provider `gemini-cli`)。
- 新增或下架某個 AI 的模型:只改該模組的 `models.yaml`(design D12)。
- 新增 MCP 工具格式:只加 `templates/mcp/<tool>.j2`(spec 8.2.3)。
- 驗收測試檔名 `test_ac<編號>_<簡述>.py`(例:`test_ac04_weighted_split.py`),NFR 量測檔名 `test_nfr<n>_<簡述>.py`,讓 ACC 可直接指到。
- 不在 repo 存任何憑證、資料庫檔或 payload;資料目錄在 repo 外(design D8)。
