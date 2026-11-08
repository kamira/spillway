"""資料庫為跨語言契約的黑箱測試(CHG-20261107-03 T2;決策 3;AM-2;契約清單「資料庫為跨語言契約」)。

契約對象是 serve 在空資料目錄建出的資料庫(AM-2):正規化後的 `sqlite_master`、各表 `PRAGMA table_info`、
`index_list`/`index_info`、`foreign_key_list`、`user_version`、`journal_mode`(`tests.blackbox.db.schema_snapshot`)。

- 快照等於凍結黃金檔 `tests/blackbox/golden/schema_v2.json`。
- 表與欄和結構文件一致:表名集合 = `sdlc_docs/structure/data.md`「實體 / 資料表」= spec 2.2;每表的欄、索引、外鍵
  = 在記憶體 SQLite 執行 spec 2.3 DDL 的結果(獨立 oracle,不取自產品程式)。
- v1 → v2 黑箱遷移(data.md「Schema 版本與 migration」):serve 啟動前以 sqlite3 從凍結的 v1 DDL
  `tests/blackbox/golden/schema_v1.sql` 建出舊庫並種入資料,啟動後:`user_version` 2、快照等於黃金檔、
  冷卻搬到 `cooldowns`(reason 取帳本最近一筆,找不到為 `quota_exhausted`)、不刪 `budgets` 列、備份
  `spillway.db.v1.bak` 為完整的 v1 庫;再次啟動不再備份。

黃金檔的更新(schema 變更必須走 CHG):以本檔 `_fresh_snapshot` 的結果覆寫 `schema_v2.json`,並同步更新
`tests/contract/test_schema_python.py` 對照的 `MIGRATIONS`。不 import spillway。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from tests.blackbox.db import DB_NAME, build_db, read_db, schema_snapshot, snapshot
from tests.blackbox.harness import Serve

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).with_name("golden")
GOLDEN_V2 = GOLDEN_DIR / "schema_v2.json"
GOLDEN_V1_SQL = GOLDEN_DIR / "schema_v1.sql"


def _golden() -> dict:
    return json.loads(GOLDEN_V2.read_text(encoding="utf-8"))


def _jsonable(snap: dict) -> dict:
    return json.loads(json.dumps(snap))


def _fresh_snapshot(tmp_path: Path) -> dict:
    home = tmp_path / "fresh"
    with Serve(home):
        pass
    with read_db(home) as conn:
        return _jsonable(schema_snapshot(conn))


def _spec_ddl_snapshot() -> dict:
    spec = (REPO_ROOT / "docs" / "spec.md").read_text(encoding="utf-8")
    sec = spec.split("### 2.3 DDL", 1)[1].split("### 2.4", 1)[0]
    blocks = re.findall(r"```sql\n(.*?)```", sec, re.S)
    assert blocks, "spec 2.3 沒有 DDL 區塊"
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript("\n".join(blocks))
        return _jsonable(schema_snapshot(conn))
    finally:
        conn.close()


def _data_md_tables() -> set[str]:
    text = (REPO_ROOT / "sdlc_docs" / "structure" / "data.md").read_text(encoding="utf-8")
    sec = text.split("## 實體 / 資料表", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", sec, re.M))


def _spec_tables() -> set[str]:
    spec = (REPO_ROOT / "docs" / "spec.md").read_text(encoding="utf-8")
    sec = spec.split("### 2.2 表關係", 1)[1].split("### 2.3", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", sec, re.M))


# ================================================================ 空資料目錄建出的資料庫


def test_fresh_database_matches_golden_snapshot(tmp_path):
    snap = _fresh_snapshot(tmp_path)
    assert snap == _golden()
    assert snap["user_version"] == 2 and snap["journal_mode"] == "wal"


def test_fresh_database_has_no_rows_except_settings_token(tmp_path):
    """spec 2:初始化只建表、不寫入資料列(serve 首次啟動另寫存取 token 雜湊,data.md「settings 鍵」)。"""
    home = tmp_path / "fresh"
    with Serve(home):
        pass
    with read_db(home) as conn:
        rows = snapshot(conn)
    assert {t: r for t, r in rows.items() if r and t != "settings"} == {}
    assert [r[0] for r in rows["settings"]] == ["access_token_sha256"]


def test_tables_and_columns_match_structure_docs():
    golden = _golden()
    tables = set(golden["tables"])
    assert tables == _data_md_tables() == _spec_tables()
    ddl = _spec_ddl_snapshot()
    assert set(ddl["tables"]) == tables
    for t in sorted(tables):
        for key in ("table_info", "index_list", "index_info", "foreign_key_list"):
            assert golden["tables"][t][key] == ddl["tables"][t][key], (t, key)


# ================================================================ v1 → v2 黑箱遷移


_V1_SEED = """
INSERT INTO deployments (id, model_name, kind, provider, model) VALUES
  ('a', 'a', 'api', 'openai', 'gpt-4o-mini'),
  ('b', 'b', 'api', 'openai', 'gpt-4o-mini'),
  ('c', 'c', 'api', 'openai', 'gpt-4o-mini');
INSERT INTO budgets (deployment_id, type, limit_amount, window, exhausted_until) VALUES
  ('a', 'usd', 10.0, 'monthly', '2026-12-01T00:00:00Z'),
  ('b', 'none', NULL, 'none', '2026-12-02T00:00:00Z'),
  ('c', 'usd', 5.0, 'weekly', NULL);
INSERT INTO usage_ledger (id, ts, deployment_id, status) VALUES
  (1, '2026-10-01T00:00:00Z', 'a', 'quota_exhausted'),
  (2, '2026-10-02T00:00:00Z', 'a', 'rate_limited'),
  (3, '2026-10-03T00:00:00Z', 'a', 'ok');
INSERT INTO settings (key, value) VALUES ('context_retention_days', '9');
"""


def _v1_home(tmp_path: Path) -> Path:
    home = tmp_path / "v1home"
    build_db(home / DB_NAME, GOLDEN_V1_SQL.read_text(encoding="utf-8"), _V1_SEED, user_version=1)
    return home


def test_v1_to_v2_migration_with_backup(tmp_path):
    home = _v1_home(tmp_path)
    with read_db(home) as conn:
        v1_rows = snapshot(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    with Serve(home) as s:
        assert s.request("GET", "/api/settings/context").json()["context_retention_days"] == 9
    with read_db(home) as conn:
        snap = _jsonable(schema_snapshot(conn))
        rows = snapshot(conn)
    assert snap == _golden()   # 遷移後的 schema 與新建的庫相同
    assert rows["cooldowns"] == [("a", "2026-12-01T00:00:00Z", "rate_limited"),     # 帳本最近一筆
                                 ("b", "2026-12-02T00:00:00Z", "quota_exhausted")]  # 找不到時
    assert [r[0] for r in rows["budgets"]] == ["a", "b", "c"]                       # 不刪 budgets 列
    assert rows["usage_ledger"] == v1_rows["usage_ledger"] and rows["deployments"] == v1_rows["deployments"]
    bak = home / f"{DB_NAME}.v1.bak"
    assert bak.is_file()
    with read_db(bak) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # 單一自足檔案
        assert "exhausted_until" in [r[1] for r in conn.execute("PRAGMA table_info(budgets)")]
        assert snapshot(conn) == v1_rows
    with Serve(home):   # 已是 v2:不再備份
        pass
    assert sorted(p.name for p in home.glob(f"{DB_NAME}.v1.bak*")) == [f"{DB_NAME}.v1.bak"]
