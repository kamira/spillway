"""Python 版與黑箱契約的對照(CHG-20261107-03 T2;決策 9 ⑥、11;AM-9;契約清單「資料庫為跨語言契約」)。

黑箱測試(`tests/blackbox/`)不 import spillway,它們用的凍結檔與字面常數由本檔核對 Python 實作:
- `MIGRATIONS` 依序套用後的正規化 schema 等於黑箱黃金檔 `tests/blackbox/golden/schema_v2.json`;
  `MIGRATIONS` 的 v1 腳本即套件內的 `schema.sql`。
- `schema.sql` 與 `git show d89118c:src/spillway/store/schema.sql`(schema v1 的基線 commit)相同;黑箱的凍結
  v1 DDL `tests/blackbox/golden/schema_v1.sql` 與 `schema.sql` 相同。
- `tests.blackbox.harness.drain_expire_delay` 與 `cli.drain_expire_delay` 相同(CHG-20261102-01 決策 18)。
- 行程契約的字面常數(`TOKEN_NOTICE`、控制介面警告、已忽略旋鈕前綴、旋鈕名與順序、結束碼)、
  `access_token_sha256` 鍵名與資料庫檔名,與實作的定義相同。

本檔是 Python 專屬的合約測試(可以 import spillway);換實作時不跑。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from spillway import app as app_mod
from spillway import cli, settings
from spillway.store import db as store_db
from tests.blackbox import db as bb_db
from tests.blackbox import harness

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "blackbox" / "golden"
SCHEMA_V1_COMMIT = "d89118c"


def _golden() -> dict:
    return json.loads((GOLDEN_DIR / "schema_v2.json").read_text(encoding="utf-8"))


def _package_schema_sql() -> str:
    return resources.files("spillway.store").joinpath("schema.sql").read_text(encoding="utf-8")


def test_migrations_produce_golden_snapshot(tmp_path):
    conn = store_db.connect(tmp_path / "spillway.db")
    try:
        snap = json.loads(json.dumps(bb_db.schema_snapshot(conn)))
    finally:
        conn.close()
    assert snap == _golden()


def test_migration_scripts_alone_produce_golden_tables():
    """`MIGRATIONS` 的腳本本身(不經 `connect` 的其他步驟)依序執行,表結構即黃金檔。"""
    assert [v for v, _ in store_db.MIGRATIONS] == [1, 2]
    assert store_db.MIGRATIONS[0][1] == _package_schema_sql()
    conn = sqlite3.connect(":memory:")
    try:
        for _version, script in store_db.MIGRATIONS:
            conn.executescript(script)
        snap = json.loads(json.dumps(bb_db.schema_snapshot(conn)))
    finally:
        conn.close()
    golden = _golden()
    assert snap["tables"] == golden["tables"]
    assert snap["sqlite_master"] == golden["sqlite_master"]


def test_schema_v1_unchanged_since_baseline_commit():
    r = subprocess.run(["git", "show", f"{SCHEMA_V1_COMMIT}:src/spillway/store/schema.sql"], cwd=REPO_ROOT,
                       capture_output=True, text=True, check=True)
    assert r.stdout == _package_schema_sql()
    assert (GOLDEN_DIR / "schema_v1.sql").read_text(encoding="utf-8") == _package_schema_sql()


@pytest.mark.parametrize("grace", [0.5, 1.0, 2.0, 3.0, 10.0, 30.0])
def test_drain_expire_delay_constant(grace):
    assert harness.drain_expire_delay(grace) == cli.drain_expire_delay(grace)


def test_process_contract_literals_match_implementation():
    assert harness.TOKEN_NOTICE == app_mod.TOKEN_NOTICE
    assert harness.TEST_CONTROL_WARNING == cli.TEST_CONTROL_WARNING
    assert harness.KNOBS_IGNORED_PREFIX == cli.TEST_KNOBS_IGNORED
    assert harness.CONTROL_ENV == cli.TEST_CONTROL_ENV
    assert harness.KNOB_ENVS == tuple(cli.TEST_KNOB_ENVS)
    assert harness.GRACEFUL_ENV == cli.TEST_GRACEFUL_TIMEOUT_ENV
    assert harness.MCP_MAX_SESSIONS_ENV == cli.TEST_MCP_MAX_SESSIONS_ENV
    assert harness.LOG_PREFIX.strip() == cli.LOG_PREFIX and harness.LOG_CONT_PREFIX.strip() == cli.LOG_CONT_PREFIX
    assert (harness.EXIT_USAGE, harness.EXIT_PREFLIGHT, harness.EXIT_SIGINT) == (
        cli.EXIT_USAGE, cli.EXIT_STARTUP_FAILED, cli.EXIT_SIGINT)
    assert bb_db.ACCESS_TOKEN_KEY == settings.ACCESS_TOKEN_KEY
    assert bb_db.DB_NAME == settings.db_path().name
