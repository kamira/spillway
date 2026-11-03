# UI layout

7 個畫面的設計稿，畫面上的數值皆為示意資料。

| 檔案 | 畫面 |
| --- | --- |
| `Main.dc.html` | UI-1 總覽 |
| `Members.dc.html` | UI-2 成員 |
| `Groups.dc.html` | UI-3 群組與 Tag |
| `Routes.dc.html` | UI-4 Route（拖曳排序） |
| `Mcp.dc.html` | UI-5 MCP 設定 |
| `Ledger.dc.html` | UI-6 帳本 |
| `Settings.dc.html` | UI-7 設定 |
| （無設計稿） | UI-8 API 參考：頂端群組選單、左側可折疊索引（連線、工具、通知、錯誤碼）、右側顯示所選項目（CHG-20261103-01 決策 4） |

`.dc.html` 是 Claude 設計畫布（Design Component）的格式，需在畫布中開啟才能完整渲染；`canvas.json` 記錄各畫面在畫布上的位置。直接用瀏覽器開啟時，版面與樣式仍可閱讀，但互動元件不會運作。

實作位於 `src/spillway/web/static/`（靜態 HTML、CSS 與 JavaScript，經 `/api` 取資料；網址 `http://127.0.0.1:<port>/ui/`）。與設計稿的差異（系統字型、UI-1 不做期間切換、首次設定精靈為獨立頁 `wizard.html` 等）見 `sdlc_docs/structure/design.md`「設計差異」。
