"""黑箱測試的 harness 與共用零件(CHG-20261107-03 T2;決策 8、9;AM-15)。

本套件與其中的測試只經 HTTP、子行程(`SPILLWAY_CMD`)與唯讀資料庫操作 spillway,**不 import `spillway`**
(`tests/contract/test_blackbox_boundary.py` 靜態檢查遞移閉包;`guard.py` 在執行期檢查 `sys.modules`)。

模組
- `harness`:`Serve`(serve 子行程)、`run_cli`(`init` 等)、`child_env`、`spillway_cmd`、行程契約常數(spec 14)。
- `control`:`Control`,`/__test__/*` 的同步 client(spec 13)。
- `db`:`read_db`(`mode=ro`)、`snapshot`、`schema_snapshot`、`build_db`(serve 啟動前建測試庫)。
- `fakeapi`:`FakeApi`(行程內假上游,不 import spillway)、`shared_fake_api()`。
- `mcp`:MCP SDK 驅動(HTTP 與 `mcp-stdio`)。
- `ports`:`free_port`、`dead_base`、`lan_ip`。
- `mask`:測試端遮罩函式、`TEST_KEY`、`TEST_KEY_ENV`。
- `fakemods`:`write_module`(假 adapter 模組資料夾)。
- `prices`:litellm 價格 JSON 的檔案讀取(不 import litellm;AM-9)。
- `ui/`:畫面測試的頁面操作與種資料(由 `tests/helpers/ui*.py` 複製改寫)。
- `guard`:執行期守門;`plugin`:pytest fixture(`serve_factory`、`blackbox_guard`),由根 conftest 註冊。
- `impl_dependent.toml`:實作相依清單(AM-13);`converted_roots.txt`:已轉換目錄(守門範圍)。

wave 3(T3–T5)只 import 本套件、不修改;唯一例外是 `converted_roots.txt` 各加一行自己的目錄。
需要新零件時回報主 session,由 T2 擁有者補。
"""
