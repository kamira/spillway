"""差分 runner(CHG-20261108-01 T3;spec 15;AM-7、AM-8、AM-10)。不 import spillway。

- `run_scenario(scenario, cmd=None) -> Capture`:以一個 `Serve` 與一個專屬 fakeapi 跑一個情境的一側(暫存 `SPILLWAY_HOME`、
  `SPILLWAY_TEST_CONTROL=1`、`SPILLWAY_TEST_START_TIME`、`SPILLWAY_TEST_LOG_LEVEL=INFO`)。原始擷取只留在記憶體,暫存目錄用完即刪。
- `run_pair(scenario, cmd_a=None, cmd_b=None) -> PairResult`:兩側並行(各自的 HOME、埠、fakeapi),再依 15.10.1 比對。
- `run_corpus(scenarios=SCENARIOS, ...) -> list[PairResult]`:整份語料。
- `empty_surfaces(capture, scenario)`:情境宣告的比對面中沒有內容者(AM-10)。
- `second_impl_cmd()`:`SPILLWAY_PARITY_CMD_B`(第二實作的命令;未設時 None,即與第一實作相同)。
- `mutant_cmd(mutation)`:以 `tests/parity/mutant_wrapper.py` 包裝第一實作的命令。

錯誤訊息不轉印 serve 的輸出原文(可能含秘密;15.10.4)。
"""
from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.blackbox.harness import Serve, spillway_cmd
from tests.blackbox.mask import mask_secrets
from tests.fakes.api_upstream.server import FakeApiServer
from tests.parity.corpus import CORPUS_KEY_ENVS, CORPUS_KEYS, FAKE_BASE, SCENARIOS, START_TIME, FakeMode, Scenario, Step
from tests.parity.normalize import (
    DB_ROWS, DB_TABLES, ERRORS, HEADERS, JSON_BODY, LOG_BARE, LOG_CONT_PREFIX, LOG_PREFIX, STATUS, UPSTREAM, Capture, Exchange, Report,
    _is_error_shape, _parse_body, compare_captures, sentinels_of,
)

__all__ = ["CMD_B_ENV", "PairResult", "RunnerError", "empty_surfaces", "mutant_cmd", "run_corpus", "run_pair", "run_scenario",
           "second_impl_cmd"]

CMD_B_ENV = "SPILLWAY_PARITY_CMD_B"
WRAPPER = Path(__file__).resolve().parent / "mutant_wrapper.py"
ROTATE_PATH = "/api/settings/token/rotate"
RAW_PARAMS_PATH = "/__test__/raw/deployment-params"
# 15.2:log 內容不比對;ERROR 等級的 SPILLWAY-LOG 行與其 traceback 續行屬於實作相依,不算停機失敗
ALLOW_STDERR = (r"^SPILLWAY-LOG",)


class RunnerError(RuntimeError):
    """runner 無法完成情境(serve 起不來、停機失敗等);訊息已遮罩。"""


@dataclass
class PairResult:
    scenario: Scenario = field(repr=False)
    report: Report
    empty_a: list[str]
    empty_b: list[str]
    capture_a: Capture = field(repr=False)
    capture_b: Capture = field(repr=False)

    @property
    def ok(self) -> bool:
        return self.report.ok and not self.empty_a and not self.empty_b


def second_impl_cmd() -> list[str] | None:
    raw = os.environ.get(CMD_B_ENV, "")
    return shlex.split(raw) if raw.strip() else None


def mutant_cmd(mutation: str, base: Sequence[str] | None = None) -> list[str]:
    return [sys.executable, str(WRAPPER), "--mutation", mutation, "--", *(base if base is not None else spillway_cmd())]


def _substitute(value: Any, fake_base: str) -> Any:
    if isinstance(value, str):
        return value.replace(FAKE_BASE, fake_base)
    if isinstance(value, list):
        return [_substitute(v, fake_base) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, fake_base) for k, v in value.items()}
    return value


def _snapshot_db(conn: sqlite3.Connection) -> tuple[dict[str, list[dict[str, tuple[str, Any]]]], int]:
    """15.8.1:10 張表的全部資料列,依主鍵排序;每欄 `(typeof, 值)`。BLOB 以十六進位表示(比對用,不解碼)。"""
    out: dict[str, list[dict[str, tuple[str, Any]]]] = {}
    for table in DB_TABLES:
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [r[1] for r in info]
        keys = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])] or ["rowid"]
        select = ", ".join(f'typeof("{c}"), "{c}"' for c in columns)
        order = ", ".join(f'"{k}"' for k in keys)
        rows = []
        for row in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}'):
            cells = {}
            for i, col in enumerate(columns):
                typeof, value = row[2 * i], row[2 * i + 1]
                cells[col] = (typeof, value.hex() if isinstance(value, bytes) else value)
            rows.append(cells)
        out[table] = rows
    return out, conn.execute("PRAGMA user_version").fetchone()[0]


def _log_lines(stderr: bytes) -> list[str]:
    text = stderr.decode("utf-8", errors="replace")
    return [ln for ln in text.split("\n") if ln.startswith((LOG_PREFIX, LOG_CONT_PREFIX)) or ln == LOG_BARE]


def _model_credentials(scenario: Scenario) -> dict[str, bool]:
    """語料建立成員的請求決定「成員是否有 `credential_ref`」(15.3.3)。"""
    out: dict[str, bool] = {}
    for step in scenario.steps:
        if isinstance(step, Step) and step.method == "POST" and step.path == "/api/deployments" and isinstance(step.json, dict):
            out[step.json["model"]] = bool(step.json.get("credential_ref"))
    return out


def _seeded_ids(scenario: Scenario) -> frozenset[str]:
    return frozenset(s.json["id"] for s in scenario.steps if isinstance(s, Step) and s.path == RAW_PARAMS_PATH)


def _send(step: Step, client: Any, fake_base: str) -> Any:
    kw: dict[str, Any] = {}
    if step.json is not None:
        kw["json"] = _substitute(step.json, fake_base)
    if step.content is not None:
        kw["content"] = step.content
    if step.headers:
        kw["headers"] = dict(step.headers)
    return client.request(step.method, step.path, **kw)


def _do_step(step: Step, client: Any, fake_base: str, capture: Capture) -> None:
    try:
        r = _send(step, client, fake_base)
    except Exception as exc:    # `from None`:--tb=long 會逐層印出 httpx 函式的 `json=` 參數,其中可能有語料金鑰(15.10.4)
        raise RunnerError(_scrub(f"{step.method} {step.path}: 請求失敗:{type(exc).__name__}", capture)) from None
    ex = Exchange(step.method, step.path, r.status_code, tuple(r.headers.multi_items()), r.content, step.error_mode)
    capture.exchanges.append(ex)
    if step.path == ROTATE_PATH and r.status_code == 200:
        kind, body = _parse_body(ex)
        if kind == "json" and isinstance(body, dict) and isinstance(body.get("token"), str):
            capture.tokens += (body["token"],)


def _scrub(message: str, capture: Capture) -> str:
    return mask_secrets(message, sentinels_of(capture))


def run_scenario(scenario: Scenario, cmd: Sequence[str] | None = None, *, keep_root: Path | None = None) -> Capture:
    """跑一側;回傳原始擷取(只在記憶體)。暫存 HOME 用完即刪,serve 的記錄檔也在其中。

    `keep_root` 給了就改用該目錄且不刪除(test_no_secrets 掃描記錄檔位元組用;必須在暫存目錄下)。
    """
    root = keep_root.resolve() if keep_root is not None else Path(tempfile.mkdtemp(prefix="spillway-parity-")).resolve()
    fake = FakeApiServer().start()
    try:
        serve = Serve(root / "home", cmd=cmd, start_time=START_TIME, log_level="INFO", extra_env=dict(CORPUS_KEY_ENVS),
                      log_path=root / "serve.log", allow_stderr=ALLOW_STDERR, log_secrets=CORPUS_KEYS)
        capture = Capture(scenario=scenario.name, port=serve.port, home=str(serve.home), fake_base_url=fake.base_url,
                          corpus_keys=CORPUS_KEYS, model_credentials=_model_credentials(scenario),
                          seeded_param_ids=_seeded_ids(scenario))
        try:
            serve.start()
        except BaseException as exc:
            raise RunnerError(_scrub(f"{scenario.name}: serve 無法啟動:{type(exc).__name__}", capture)) from None
        try:
            if serve.token:
                capture.tokens += (serve.token,)
            with serve.http(timeout=60.0) as client:
                for step in scenario.steps:
                    if isinstance(step, FakeMode):
                        fake.set_mode(step.model, step.mode, **dict(step.options))
                    else:
                        _do_step(step, client, fake.base_url, capture)
            with serve.db() as conn:
                capture.db, capture.user_version = _snapshot_db(conn)
            capture.upstream = list(fake.requests)
        except BaseException:
            serve.kill()
            raise
        try:
            serve.stop()
        except AssertionError as exc:
            raise RunnerError(f"{scenario.name}: serve 未正常結束:{type(exc).__name__}") from None
        capture.log_lines = _log_lines(serve.stderr_bytes())
        return capture
    finally:
        fake.stop()
        if keep_root is None:
            shutil.rmtree(root, ignore_errors=True)


def empty_surfaces(capture: Capture, scenario: Scenario) -> list[str]:
    """情境宣告的比對面中沒有內容者(AM-10)。"""
    parsed = [_parse_body(ex) for ex in capture.exchanges]
    jsons = [b for kind, b in parsed if kind == "json"]
    present = {
        STATUS: bool(capture.exchanges),
        HEADERS: any({n.lower() for n, _ in ex.headers} & {"x-spillway-served-by", "x-spillway-slot", "cache-control"}
                     for ex in capture.exchanges),
        JSON_BODY: any(not _is_error_shape(b) for b in jsons),
        ERRORS: any(_is_error_shape(b) for b in jsons),
        DB_ROWS: any(rows for table, rows in capture.db.items() if table != "settings"),
        UPSTREAM: bool(capture.upstream),
    }
    return [s for s in scenario.surfaces if not present[s]]


def run_pair(scenario: Scenario, cmd_a: Sequence[str] | None = None, cmd_b: Sequence[str] | None = None) -> PairResult:
    """兩側並行(各自的 HOME、埠與 fakeapi);`cmd_b` 省略時取 `SPILLWAY_PARITY_CMD_B`,再省略時與第一實作相同。"""
    second = cmd_b if cmd_b is not None else second_impl_cmd()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(run_scenario, scenario, cmd_a)
        fb = pool.submit(run_scenario, scenario, second)
        a, b = fa.result(), fb.result()
    return PairResult(scenario, compare_captures(a, b), empty_surfaces(a, scenario), empty_surfaces(b, scenario), a, b)


def run_corpus(scenarios: Sequence[Scenario] = SCENARIOS, cmd_a: Sequence[str] | None = None,
               cmd_b: Sequence[str] | None = None, workers: int = 4) -> list[PairResult]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda s: run_pair(s, cmd_a, cmd_b), scenarios))
