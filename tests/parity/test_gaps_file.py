"""差距機制的契約測試(CHG-20261108-01 T2;AM-9;AM-8 ⑤)。

- L1:`rust_gaps.toml`、`protected.toml`、`impl_dependent.toml` 的欄位驗證;受保護清單與差距清單交集為空;單向
  規則(已刪除 id 不得加回;`main` 上沒有檔案視為空清單);`on_rust` 三種值。清單 id 以**實際收集結果**比對
  (`pytest --collect-only`、`behave --dry-run`),不沿用 `test_impl_dependent_ids_exist` 的凍結基準。
- L2:以夾具差距清單跑內層 pytest:清單內失敗判 xfail、清單內通過判失敗(strict)、`raises` 以外的例外判失敗、
  serve 起不來判失敗;`SPILLWAY_IMPL=python` 套用 0 項;非預設 `SPILLWAY_CMD` 未設 `SPILLWAY_IMPL` 時失敗。
- L4:`behave_gate.py` 以**真實 behave 的 json 輸出**當正反樣本判定結束碼。
- T2 仲裁 A1、A2、B1–B6、C7、C8 的修正各有內層 pytest 或 behave_gate 測試(階段規則、replace 的 deselect、skip 的列出、
  feature hook、受保護清單、serve 崩潰掃描、readd 格式、退出條件文字、重名情境、清單互斥)。

內層 pytest 的夾具清單由內層 conftest 改寫 `tests.parity.gaps` 的模組常數,正式路徑沒有環境變數或命令列開關可以換掉清單。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from tests.blackbox.harness import (
    EndpointNotImplemented, ImplConfigError, Serve, child_env, require_implemented, spillway_impl,
)
from tests.parity import behave_gate, gaps

REPO = Path(__file__).resolve().parents[2]
FAKE_RUST_CMD = "/nonexistent/spillway-rs"
NEW_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
_GAP_FIELDS = 'reason = "r"\nbatch = "b"\nsince = "CHG-20261108-01"\n'


def _gap_toml(*ids: str, kind: str = "pytest", extra: str = "") -> str:
    return "".join(f'[[gap]]\nkind = "{kind}"\nid = {json.dumps(i, ensure_ascii=False)}\n{_GAP_FIELDS}{extra}\n'
                   for i in ids)


def _load_text(tmp_path: Path, text: str) -> list[gaps.Gap]:
    p = tmp_path / "g.toml"
    p.write_text(text, encoding="utf-8")
    return gaps.load_gaps(p)


@pytest.fixture(scope="session")
def collected() -> dict[str, set[str]]:
    return {"pytest": gaps.collect_pytest_ids(), "behave": gaps.collect_behave_ids()}


# ================================================================ L1:實際檔案


def test_rust_gaps_file_ships_empty_and_valid():
    assert gaps.load_gaps(gaps.RUST_GAPS_FILE) == []


def test_gap_ids_exist_in_real_collection(collected):
    for g in gaps.load_gaps(gaps.RUST_GAPS_FILE):
        assert g.id in collected[g.kind], g.id


def test_protected_names_all_expand_against_real_collection(collected):
    expanded, missing = gaps.expand_protected(gaps.load_protected(gaps.PROTECTED_FILE), collected["pytest"])
    assert not missing, missing
    # AM-9 的每個名稱都要展開到至少一個真實 node ID,兩個 fresh database 測試各一個
    assert all(expanded.values())
    names = {e.name for e in gaps.load_protected(gaps.PROTECTED_FILE)}
    assert sum(1 for n in names if n.startswith("test_fresh_database")) == 2
    assert {"test_clock_*", "test_token_*", "test_exit_1_*", "test_exit_2_*", "test_home_check_*"} <= names


def test_protected_and_gaps_are_disjoint(collected):
    expanded, _ = gaps.expand_protected(gaps.load_protected(gaps.PROTECTED_FILE), collected["pytest"])
    assert not gaps.protected_hits(gaps.load_gaps(gaps.RUST_GAPS_FILE), {i for v in expanded.values() for i in v})


def test_impl_dependent_every_entry_has_on_rust_and_exists(collected):
    entries = gaps.load_impl_dependent(gaps.IMPL_DEPENDENT_FILE)
    assert len(entries) >= 13
    for e in entries:
        if e.is_file:
            assert any(i.startswith(e.id + "::") for i in collected["pytest"]), e.id
        else:
            assert any(gaps.id_matches(e.id, i) for i in collected[e.kind]), e.id
    ids = {e.id for e in entries}
    assert "tests/ui/test_shell.py::test_async_tests_after_ui_module_pass" in ids
    assert "tests/blackbox/test_harness.py" in ids


def test_impl_dependent_deselectable_entries_are_not_protected(collected):
    expanded, _ = gaps.expand_protected(gaps.load_protected(gaps.PROTECTED_FILE), collected["pytest"])
    hits = {i for v in expanded.values() for i in v}
    bad = [e.id for e in gaps.load_impl_dependent(gaps.IMPL_DEPENDENT_FILE)
           if e.action in ("not_applicable", "replace") and any(e.covers(h) for h in hits)]
    assert not bad, bad


@pytest.mark.parametrize("entry_id", [
    "tests/blackbox/test_control_contract.py::test_cli_subprocess_does_not_inherit_knobs",
    "tests/acceptance/test_m4b_operation.py::test_m4b_operation",
])
def test_adapter_paths_gaps_state_their_exit_condition(entry_id):
    rows = tomllib.loads(gaps.IMPL_DEPENDENT_FILE.read_text(encoding="utf-8"))["entry"]
    row = next(r for r in rows if r["id"] == entry_id)
    assert row["on_rust"] == "gap" and "設定檔 adapter CHG" in row["on_rust_reason"]


def test_one_way_rule_on_real_repo():
    assert gaps.one_way_violations(gaps.load_gaps(gaps.RUST_GAPS_FILE), gaps.main_deleted_ids(REPO), REPO) == []


def test_real_key_vars_extended_in_every_local_copy():
    for rel in ("tests/blackbox/harness.py", "tests/conftest.py", "tests/integration/test_graceful_shutdown.py",
                "tests/integration/test_test_control.py", "tests/integration/test_init_token.py"):
        m = re.search(r"^REAL_KEY_VARS = \((.*?)\)", (REPO / rel).read_text(encoding="utf-8"), re.M | re.S)
        assert m, rel
        for var in (*NEW_KEY_VARS, "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            assert f'"{var}"' in m.group(1), (rel, var)


# ================================================================ L1:欄位驗證


@pytest.mark.parametrize("text", [
    '[[gap]]\nkind = "pytest"\nid = "a::b"\nreason = "r"\nbatch = "b"\n',                              # 缺 since
    '[[gap]]\nkind = "nope"\nid = "a::b"\n' + _GAP_FIELDS,                                             # kind 不合法
    '[[gap]]\nkind = "pytest"\nid = "a::b"\nreason = ""\nbatch = "b"\nsince = "CHG-20261108-01"\n',    # 空 reason
    '[[gap]]\nkind = "pytest"\nid = "a::b"\nreason = "r"\nbatch = "b"\nsince = "soon"\n',              # since 非 CHG
    '[[gap]]\nkind = "pytest"\nid = "a::b"\n' + _GAP_FIELDS + 'surprise = 1\n',                        # 未知欄位
    '[[gap]]\nkind = "pytest"\nid = "a::b"\n' + _GAP_FIELDS + 'expect = ""\n',                         # 空 expect
    _gap_toml("a::b") + _gap_toml("a::b"),                                                             # 重複
    'gap = 3\n',                                                                                       # 型別
    'extra = []\n',                                                                                    # 未知頂層鍵
])
def test_gap_file_rejects_invalid(tmp_path, text):
    with pytest.raises(gaps.GapFileError):
        _load_text(tmp_path, text)


def test_gap_file_accepts_full_entry(tmp_path):
    g = _load_text(tmp_path, _gap_toml("a::b", extra='expect = "404"\n'))[0]
    assert (g.kind, g.id, g.expect, g.since) == ("pytest", "a::b", "404", "CHG-20261108-01")


def test_empty_file_is_empty_list(tmp_path):
    assert _load_text(tmp_path, "# 註解\n") == []


def test_expand_protected_treats_only_star_as_wildcard():
    ids = {"t.py::test_a[x]", "t.py::test_a[y]", "t.py::test_b", "t.py::test_bb", "t.py::test_c[1]"}
    entries = [gaps.Protected("t.py", "test_a[*]"), gaps.Protected("t.py", "test_b"), gaps.Protected("t.py", "test_c"),
               gaps.Protected("t.py", "test_gone_*")]
    expanded, missing = gaps.expand_protected(entries, ids)
    assert expanded["t.py::test_a[*]"] == {"t.py::test_a[x]", "t.py::test_a[y]"}
    assert expanded["t.py::test_b"] == {"t.py::test_b"}       # 不是前綴比對
    assert expanded["t.py::test_c"] == {"t.py::test_c[1]"}    # 參數化的完整名稱也算
    assert missing == ["t.py::test_gone_*"]


def test_protected_intersection_is_detected(tmp_path):
    assert gaps.protected_hits(_load_text(tmp_path, _gap_toml("a::b", "a::c")), {"a::c"}) == ["a::c"]


@pytest.mark.parametrize("value, action, target", [
    ("gap", "gap", None), ("not_applicable", "not_applicable", None),
    ("replace:tests/x.py::test_y", "replace", "tests/x.py::test_y"),
])
def test_on_rust_three_values(value, action, target):
    assert gaps.parse_on_rust(value) == (action, target)


@pytest.mark.parametrize("value", ["", "skip", "xfail", "replace:", "replace:no-separator", "gap:x", None, 1])
def test_on_rust_rejects_others(value):
    with pytest.raises(gaps.GapFileError):
        gaps.parse_on_rust(value)


def _impl_toml(on_rust: str, extra: str = "") -> str:
    return f'[[entry]]\nkind = "pytest"\nid = "a::b"\nreason = "r"\non_rust = "{on_rust}"\n{extra}'


def test_impl_dependent_validation(tmp_path):
    p = tmp_path / "i.toml"
    for bad in ('[[entry]]\nkind = "pytest"\nid = "a::b"\nreason = "r"\n', _impl_toml("not_applicable"),
                _impl_toml("skip"), _impl_toml("gap", 'surprise = 1\n')):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(gaps.GapFileError):
            gaps.load_impl_dependent(p)
    p.write_text(_impl_toml("not_applicable", 'on_rust_reason = "x"\n'), encoding="utf-8")
    assert gaps.load_impl_dependent(p)[0].action == "not_applicable"


def test_replace_target_must_differ_from_own_id(tmp_path):
    p = tmp_path / "i.toml"
    p.write_text(_impl_toml("replace:a::b"), encoding="utf-8")
    with pytest.raises(gaps.GapFileError):
        gaps.load_impl_dependent(p)


def test_missing_replacements_defines_catch_up(tmp_path):
    p = tmp_path / "i.toml"
    p.write_text(_impl_toml("replace:t.py::test_new") + _impl_toml("gap").replace("a::b", "a::c"), encoding="utf-8")
    entries = gaps.load_impl_dependent(p)
    assert [e.id for e in gaps.missing_replacements(entries, {"a::b"})] == ["a::b"]
    assert gaps.missing_replacements(entries, {"t.py::test_new"}) == []


def test_render_gaps_validates_against_collection(tmp_path):
    text = gaps.render_gaps("pytest", ["a::b"], {"a::b"}, reason="r", batch="骨架", since="CHG-20261108-01")
    assert _load_text(tmp_path, text)[0].batch == "骨架"
    with pytest.raises(gaps.GapFileError):
        gaps.render_gaps("pytest", ["a::zzz"], {"a::b"}, reason="r", batch="b", since="CHG-20261108-01")


# ================================================================ L1:單向規則(暫存 git repo)


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit_gaps(repo: Path, text: str | None, msg: str) -> None:
    f = repo / gaps.RUST_GAPS_RELPATH
    f.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        f.unlink()
    else:
        f.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "x").write_text("x", encoding="utf-8")
    _git(repo, "add", "x")
    _git(repo, "commit", "-m", "init")
    return repo


def test_one_way_missing_file_on_main_is_empty(git_repo, tmp_path):
    assert gaps.main_deleted_ids(git_repo) == set()
    assert gaps.one_way_violations(_load_text(tmp_path, _gap_toml("a::b")), set(), git_repo) == []


def test_one_way_missing_main_ref_fails_closed(tmp_path):
    repo = tmp_path / "nomain"
    repo.mkdir()
    _git(repo, "init", "-b", "other")
    with pytest.raises(gaps.GapFileError):
        gaps.main_deleted_ids(repo)


def test_one_way_deleted_id_cannot_return(git_repo, tmp_path):
    _commit_gaps(git_repo, _gap_toml("a::b", "a::c"), "add")
    _commit_gaps(git_repo, _gap_toml("a::c"), "port a::b")
    deleted = gaps.main_deleted_ids(git_repo)
    assert deleted == {"a::b"}
    assert gaps.one_way_violations(_load_text(tmp_path, _gap_toml("a::b", "a::c")), deleted, git_repo) == ["a::b"]
    assert gaps.one_way_violations(_load_text(tmp_path, _gap_toml("a::c")), deleted, git_repo) == []   # 仍在清單不算


def test_one_way_new_addition_is_allowed(git_repo, tmp_path):
    _commit_gaps(git_repo, _gap_toml("a::c"), "add")
    assert gaps.one_way_violations(_load_text(tmp_path, _gap_toml("a::c", "a::new")),
                                   gaps.main_deleted_ids(git_repo), git_repo) == []


def test_one_way_file_removed_from_main_keeps_history(git_repo):
    _commit_gaps(git_repo, _gap_toml("a::b"), "add")
    _commit_gaps(git_repo, None, "cleared")
    assert gaps.main_deleted_ids(git_repo) == {"a::b"}


def test_one_way_readd_needs_chg_that_lists_the_id(git_repo, tmp_path):
    _commit_gaps(git_repo, _gap_toml("a::b", "a::c"), "add")
    _commit_gaps(git_repo, _gap_toml("a::c"), "port")
    deleted = gaps.main_deleted_ids(git_repo)
    current = _load_text(tmp_path, _gap_toml("a::b", extra='readd_chg = "CHG-20270101-01"\n'))
    assert gaps.one_way_violations(current, deleted, git_repo) == ["a::b"]        # CHG 檔不存在
    chg = git_repo / "sdlc_docs/changes/CHG-20270101-01.md"
    chg.parent.mkdir(parents=True)
    for text in ("決策 6 判定後加回其他項目\n",           # 沒列這個 id
                 "本筆刪除 a::b,移植完成\n",              # 只提到 id
                 "決策 6 判定:a::c:規格修訂\n",          # 別的 id
                 "決策 6 判定:a::b:   \n",               # 理由為空
                 "決策 6 判定:a::bb:規格修訂\n",         # id 必須逐字
                 "說明:決策 6 判定:a::b:規格修訂\n"):    # 必須在行首
        chg.write_text(text, encoding="utf-8")
        assert gaps.one_way_violations(current, deleted, git_repo) == ["a::b"], text
    for text in ("決策 6 判定:a::b:規格修訂,Rust 版行為待定\n", "  - 決策 6 判定:a::b:規格修訂\n"):
        chg.write_text(text, encoding="utf-8")
        assert gaps.one_way_violations(current, deleted, git_repo) == [], text


# ================================================================ harness:實作辨識、Serve(cmd=...)


@pytest.mark.parametrize("impl, cmd, expected", [
    (None, None, "python"), ("python", None, "python"), ("python", "/x/wrapper", "python"),
    ("rust", "/x/spillway-rs", "rust"), (None, "  ", "python"),
])
def test_spillway_impl_resolves(monkeypatch, impl, cmd, expected):
    for name, value in (("SPILLWAY_IMPL", impl), ("SPILLWAY_CMD", cmd)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    assert spillway_impl() == expected


@pytest.mark.parametrize("impl, cmd", [
    (None, "/x/spillway-rs"),    # AM-9:非預設 SPILLWAY_CMD 未設 SPILLWAY_IMPL
    ("java", None),              # 不合法的值
    ("rust", None),              # 宣稱 rust 卻跑預設的 Python 命令(標籤與實際不符)
])
def test_spillway_impl_rejects(monkeypatch, impl, cmd):
    for name, value in (("SPILLWAY_IMPL", impl), ("SPILLWAY_CMD", cmd)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    with pytest.raises(ImplConfigError):
        spillway_impl()


def test_serve_cmd_is_per_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_CMD", "/should/not/be/used")
    s = Serve(tmp_path / "h", cmd=[sys.executable, "-c", "import sys; sys.exit(7)"])
    with pytest.raises(RuntimeError, match="rc=7"):
        s.start()
    with pytest.raises(ValueError):
        Serve(tmp_path / "h2", cmd=[])


def test_relative_serve_cmd_resolves_like_spillway_cmd(tmp_path):
    from tests.blackbox.harness import INVOCATION_DIR
    assert Serve(tmp_path / "h", cmd=["./rel/spillway-rs", "--x"]).cmd == [str((INVOCATION_DIR / "rel/spillway-rs").resolve()), "--x"]
    assert Serve(tmp_path / "h", cmd=["spillway-rs"]).cmd == ["spillway-rs"]
    assert Serve(tmp_path / "h").cmd is None


def test_impl_env_is_not_passed_to_the_product(tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_IMPL", "python")
    for var in NEW_KEY_VARS:
        monkeypatch.setenv(var, "sk-fake-not-a-real-key")
    env = child_env(tmp_path / "h")
    assert "SPILLWAY_IMPL" not in env and not set(NEW_KEY_VARS) & set(env)


def test_require_implemented():
    req = httpx.Request("GET", "http://127.0.0.1:1/api/x?secret=1")
    ok = httpx.Response(200, request=req)
    assert require_implemented(ok) is ok
    assert require_implemented(httpx.Response(422, request=req)).status_code == 422
    for status in (404, 405, 501):
        with pytest.raises(EndpointNotImplemented) as ei:
            require_implemented(httpx.Response(status, request=req))
        assert "secret" not in str(ei.value)                       # 不把查詢字串寫進訊息


def test_xfail_raises_is_exactly_the_named_types():
    assert gaps.XFAIL_RAISES == (AssertionError, EndpointNotImplemented)


# ================================================================ L2:內層 pytest

_SAMPLE = """
import pytest
from tests.blackbox.harness import EndpointNotImplemented
def test_fails():
    assert 1 == 2, "boom-marker"
def test_passes():
    pass
def test_runtime_error():
    raise RuntimeError("not an assertion")
def test_endpoint_missing():
    raise EndpointNotImplemented("GET /x -> 404")
def test_skips():
    pytest.skip("nope")
@pytest.mark.parametrize("n", [1])
def test_param(n):
    assert n == 2
"""
_SERVE_SAMPLE = """
import sys
from tests.blackbox.harness import Serve
def test_serve_cannot_start(tmp_path):
    Serve(tmp_path / "h", cmd=[sys.executable, "-c", "import sys; sys.exit(3)"]).start()
def test_serve_binary_missing(tmp_path):
    Serve(tmp_path / "h", cmd=["/nonexistent/spillway-rs"]).start()
"""


def _inner(tmp_path: Path, gap_text: str, *, impl: str | None = "rust", cmd: str | None = FAKE_RUST_CMD,
           impl_dependent: str = "", sample: str = _SAMPLE) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "inner"
    root.mkdir()
    (root / "gaps.toml").write_text(gap_text, encoding="utf-8")
    (root / "impl.toml").write_text(impl_dependent, encoding="utf-8")
    (root / "protected.toml").write_text(
        '[[protected]]\nfile = "test_sample.py"\nnames = ["test_protected_*"]\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        'from pathlib import Path\n'
        'from tests.parity import gaps\n'
        'HERE = Path(__file__).parent\n'
        'gaps.RUST_GAPS_FILE = HERE / "gaps.toml"\n'
        'gaps.IMPL_DEPENDENT_FILE = HERE / "impl.toml"\n'
        'gaps.PROTECTED_FILE = HERE / "protected.toml"\n'
        'pytest_plugins = ["tests.blackbox.plugin", "tests.parity.gaps"]\n', encoding="utf-8")
    (root / "test_sample.py").write_text(sample, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("SPILLWAY_IMPL", "SPILLWAY_CMD", "SPILLWAY_BLACKBOX_GUARD")}
    env["PYTHONPATH"] = str(REPO)
    if impl is not None:
        env["SPILLWAY_IMPL"] = impl
    if cmd is not None:
        env["SPILLWAY_CMD"] = cmd
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", "--rootdir", str(root),
                           str(root)], cwd=root, env=env, capture_output=True, text=True, timeout=180)


def _outcome(proc: subprocess.CompletedProcess[str], name: str) -> str:
    m = re.search(rf"^(PASSED|FAILED|XFAIL|XPASS|SKIPPED|ERROR)\s.*test_sample\.py::{re.escape(name)}\b", proc.stdout, re.M)
    return m.group(1) if m else f"<not reported: {proc.stdout[-1500:]}{proc.stderr[-800:]}>"


def test_inner_listed_failure_is_xfail(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample="def test_fails():\n    assert 1 == 2\n")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _outcome(p, "test_fails") == "XFAIL"


def test_inner_expected_failure_makes_serve_factory_kill(tmp_path):
    sample = """
from pathlib import Path
import pytest
from tests.blackbox.plugin import _FAILED_KEY
@pytest.fixture
def report_flag(request):
    yield
    Path("flag").write_text(str(request.node.stash.get(_FAILED_KEY, False)))
def test_fails(report_flag):
    assert 1 == 2
"""
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample=sample)
    assert _outcome(p, "test_fails") == "XFAIL", p.stdout
    assert (tmp_path / "inner/flag").read_text() == "True"


def test_inner_listed_pass_is_failure_strict(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_passes"), sample="def test_passes():\n    pass\n")
    assert p.returncode == 1 and _outcome(p, "test_passes") == "FAILED"
    assert "XPASS(strict)" in p.stdout


def test_inner_exception_outside_raises_is_failure(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_runtime_error"),
               sample='def test_runtime_error():\n    raise RuntimeError("x")\n')
    assert p.returncode == 1 and _outcome(p, "test_runtime_error") == "FAILED"


def test_inner_named_endpoint_exception_is_xfail(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_endpoint_missing"),
               sample=_SAMPLE.split("def test_fails")[0] +
               'def test_endpoint_missing():\n    raise EndpointNotImplemented("GET /x")\n')
    assert p.returncode == 0 and _outcome(p, "test_endpoint_missing") == "XFAIL", p.stdout


def test_inner_serve_that_cannot_start_is_not_xfail(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_serve_cannot_start", "test_sample.py::test_serve_binary_missing"),
               sample=_SERVE_SAMPLE)
    assert p.returncode == 1, p.stdout
    assert _outcome(p, "test_serve_cannot_start") == "FAILED" and _outcome(p, "test_serve_binary_missing") == "FAILED"
    assert "xfailed" not in p.stdout


def test_inner_unlisted_failure_stays_failure(tmp_path):
    p = _inner(tmp_path, "", sample="def test_fails():\n    assert 1 == 2\n")
    assert p.returncode == 1 and _outcome(p, "test_fails") == "FAILED"


def test_inner_parametrized_gap_needs_exact_node_id(tmp_path):
    sample = 'import pytest\n@pytest.mark.parametrize("n", [1, 2])\ndef test_param(n):\n    assert n == 3\n'
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_param[1]"), sample=sample)
    assert p.returncode == 1
    assert re.search(r"^XFAIL .*test_param\[1\]", p.stdout, re.M) and re.search(r"^FAILED .*test_param\[2\]", p.stdout, re.M)


def test_inner_expect_must_match_failure_text(tmp_path):
    sample = 'def test_fails():\n    assert 1 == 2, "boom-marker"\n'
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ok = _inner(tmp_path / "a", _gap_toml("test_sample.py::test_fails", extra='expect = "boom-marker"\n'), sample=sample)
    assert ok.returncode == 0 and _outcome(ok, "test_fails") == "XFAIL", ok.stdout
    bad = _inner(tmp_path / "b", _gap_toml("test_sample.py::test_fails", extra='expect = "other-marker"\n'), sample=sample)
    assert bad.returncode == 1 and _outcome(bad, "test_fails") == "FAILED", bad.stdout


def test_inner_listed_skip_is_failure(tmp_path):
    sample = 'import pytest\ndef test_skips():\n    pytest.skip("nope")\n'
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_skips"), sample=sample)
    assert p.returncode == 1 and _outcome(p, "test_skips") == "FAILED", p.stdout


def test_inner_python_applies_zero_gaps(tmp_path):
    listed = _gap_toml("test_sample.py::test_fails", "test_sample.py::test_passes")
    sample = "def test_fails():\n    assert 1 == 2\ndef test_passes():\n    pass\n"
    p = _inner(tmp_path, listed, impl="python", cmd=None, sample=sample)
    assert p.returncode == 1 and _outcome(p, "test_fails") == "FAILED" and _outcome(p, "test_passes") == "PASSED"
    assert "xfailed" not in p.stdout and "XPASS" not in p.stdout


def test_inner_unset_impl_with_default_cmd_is_python(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), impl=None, cmd=None,
               sample="def test_fails():\n    assert 1 == 2\n")
    assert p.returncode == 1 and _outcome(p, "test_fails") == "FAILED"


def test_inner_non_default_cmd_without_impl_fails(tmp_path):
    p = _inner(tmp_path, "", impl=None, cmd=FAKE_RUST_CMD, sample="def test_passes():\n    pass\n")
    assert p.returncode == pytest.ExitCode.USAGE_ERROR, p.stdout + p.stderr
    assert "SPILLWAY_IMPL" in p.stdout + p.stderr and "passed" not in p.stdout


def test_inner_invalid_gap_file_fails_the_run(tmp_path):
    p = _inner(tmp_path, '[[gap]]\nkind = "pytest"\n', sample="def test_passes():\n    pass\n")
    assert p.returncode == pytest.ExitCode.USAGE_ERROR, p.stdout + p.stderr


def test_inner_protected_id_in_gaps_fails_the_run(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_protected_token"),
               sample="def test_protected_token():\n    assert 1 == 2\n")
    assert p.returncode == pytest.ExitCode.USAGE_ERROR, p.stdout + p.stderr
    assert "test_protected_token" in p.stdout + p.stderr


def _impl_entry(name: str, on_rust: str, extra: str = "") -> str:
    return f'[[entry]]\nkind = "pytest"\nid = "test_sample.py::{name}"\nreason = "r"\non_rust = "{on_rust}"\n{extra}\n'


_IMPL_SAMPLE = """
def test_na():
    assert 1 == 2
def test_replace():
    assert 1 == 2
def test_gap_choice():
    assert 1 == 2
def test_ok():
    pass
"""


def test_inner_replace_without_target_runs_only_not_applicable_is_deselected(tmp_path):
    entries = (_impl_entry("test_na", "not_applicable", 'on_rust_reason = "x"\n')
               + _impl_entry("test_replace", "replace:test_sample.py::test_future")
               + _impl_entry("test_gap_choice", "gap"))
    p = _inner(tmp_path, "", impl_dependent=entries, sample=_IMPL_SAMPLE)
    assert p.returncode == 1, p.stdout
    assert "test_na" not in re.sub(r"^.*not_applicable.*$", "", p.stdout, flags=re.M)
    # replace 與 gap 不會自動變成 skip 或 xfail:照常執行、照常失敗
    assert _outcome(p, "test_replace") == "FAILED" and _outcome(p, "test_gap_choice") == "FAILED"
    assert _outcome(p, "test_ok") == "PASSED"
    assert "1 deselected" in p.stdout and "not_applicable deselected=1" in p.stdout and "gaps applied=0" in p.stdout


def test_inner_not_applicable_is_kept_under_python(tmp_path):
    entries = _impl_entry("test_na", "not_applicable", 'on_rust_reason = "x"\n')
    p = _inner(tmp_path, "", impl="python", cmd=None, impl_dependent=entries, sample=_IMPL_SAMPLE)
    assert _outcome(p, "test_na") == "FAILED" and "deselected" not in p.stdout


def test_inner_not_applicable_file_entry_deselects_the_whole_file(tmp_path):
    entries = '[[file_entry]]\nid = "test_sample.py"\nreason = "r"\non_rust = "not_applicable"\non_rust_reason = "x"\n'
    p = _inner(tmp_path, "", impl_dependent=entries, sample=_IMPL_SAMPLE)
    assert p.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, p.stdout + p.stderr
    assert "4 deselected" in p.stdout


def test_inner_replace_with_existing_target_deselects_the_original(tmp_path):
    entries = _impl_entry("test_replace", "replace:test_sample.py::test_new")
    sample = "def test_replace():\n    assert 1 == 2\ndef test_new():\n    pass\n"
    p = _inner(tmp_path, "", impl_dependent=entries, sample=sample)
    assert p.returncode == 0, p.stdout + p.stderr
    assert _outcome(p, "test_new") == "PASSED" and "test_replace" not in p.stdout
    assert "replaced deselected=1" in p.stdout and "not_applicable deselected=0" in p.stdout


def test_inner_replace_is_kept_under_python(tmp_path):
    entries = _impl_entry("test_replace", "replace:test_sample.py::test_new")
    sample = "def test_replace():\n    assert 1 == 2\ndef test_new():\n    pass\n"
    p = _inner(tmp_path, "", impl="python", cmd=None, impl_dependent=entries, sample=sample)
    assert _outcome(p, "test_replace") == "FAILED" and "deselected" not in p.stdout


_NA_FILE = '[[file_entry]]\nid = "test_sample.py"\nreason = "r"\non_rust = "not_applicable"\non_rust_reason = "x"\n'


@pytest.mark.parametrize("entries", [
    _NA_FILE,
    _impl_entry("test_protected_x", "not_applicable", 'on_rust_reason = "x"\n'),
    _impl_entry("test_protected_x", "replace:test_sample.py::test_new"),
], ids=["file_entry", "not_applicable", "replace"])
def test_inner_protected_test_cannot_be_deselected(tmp_path, entries):
    sample = "def test_protected_x():\n    assert 1 == 2\ndef test_new():\n    pass\n"
    p = _inner(tmp_path, "", impl_dependent=entries, sample=sample)
    assert p.returncode == pytest.ExitCode.USAGE_ERROR, p.stdout + p.stderr
    assert "test_sample.py::test_protected_x" in p.stdout + p.stderr


@pytest.mark.parametrize("entries", [
    _impl_entry("test_listed", "not_applicable", 'on_rust_reason = "x"\n'),
    _impl_entry("test_listed", "replace:test_sample.py::test_new"),
], ids=["not_applicable", "replace"])
def test_inner_gap_and_deselect_lists_are_exclusive(tmp_path, entries):
    sample = "def test_listed():\n    assert 1 == 2\ndef test_new():\n    pass\n"
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_listed"), impl_dependent=entries, sample=sample)
    assert p.returncode == pytest.ExitCode.USAGE_ERROR, p.stdout + p.stderr
    assert "test_sample.py::test_listed" in p.stdout + p.stderr


# ---------------------------------------------------------------- 階段規則(T2 仲裁 A1)


def test_inner_teardown_assert_of_listed_test_is_not_xfail(tmp_path):
    sample = """
import pytest
@pytest.fixture(scope="module", autouse=True)
def module_scan():
    yield
    assert False, "secret-scan"
def test_fails():
    assert 1 == 2
"""
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample=sample)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "ERROR at teardown" in p.stdout


def test_inner_setup_assert_of_listed_test_is_xfail_and_sets_failed_key(tmp_path):
    sample = """
from pathlib import Path
import pytest
from tests.blackbox.plugin import _FAILED_KEY
@pytest.fixture
def report_flag(request):
    yield
    Path("flag").write_text(str(request.node.stash.get(_FAILED_KEY, False)))
@pytest.fixture
def seeded(report_flag):
    assert 1 == 2, "endpoint not ported"
def test_fails(seeded):
    pass
"""
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample=sample)
    assert p.returncode == 0 and _outcome(p, "test_fails") == "XFAIL", p.stdout + p.stderr
    assert (tmp_path / "inner/flag").read_text() == "True"


def test_inner_real_cli_guard_is_never_xfail(tmp_path):
    sample = """
import pytest
from tests.fakes.cli_upstream.harness import RealCliFound
@pytest.fixture
def guarded():
    raise RealCliFound("PATH 上找到真實的 claude:/x")
def test_body():
    raise RealCliFound("PATH 上找到真實的 claude:/x")
def test_fixture(guarded):
    pass
"""
    listed = _gap_toml("test_sample.py::test_body", "test_sample.py::test_fixture", extra='expect = "PATH"\n')
    p = _inner(tmp_path, listed, sample=sample)
    assert p.returncode == 1, p.stdout + p.stderr
    assert _outcome(p, "test_body") == "FAILED" and _outcome(p, "test_fixture") == "ERROR", p.stdout


# ---------------------------------------------------------------- serve 崩潰掃描(T2 仲裁 B4)

_FAKE_SERVE = r"""
import sys
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *a):
        pass
print(STDERR_LINE, file=sys.stderr, flush=True)
TCPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), H).serve_forever()
"""


def _crash_sample(stderr_line: str) -> str:
    code = _FAKE_SERVE.replace("STDERR_LINE", repr(stderr_line))
    return f"""
import sys
CODE = {code!r}
def test_fails(serve_factory):
    serve_factory(cmd=[sys.executable, "-c", CODE], control=False)
    assert 1 == 2
"""


def test_inner_expected_failure_with_serve_panic_is_error(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample=_crash_sample("panicked at src/x.rs:1:1"))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "ERROR at teardown" in p.stdout
    assert "src/x.rs" not in p.stdout     # 只報行數,不轉印內容


def test_inner_expected_failure_without_serve_panic_stays_xfail(tmp_path):
    p = _inner(tmp_path, _gap_toml("test_sample.py::test_fails"), sample=_crash_sample("listening"))
    assert p.returncode == 0 and _outcome(p, "test_fails") == "XFAIL", p.stdout + p.stderr


# ================================================================ L4:behave_gate(真實 behave 輸出)

_FEATURE = """Feature: 樣本
  Scenario: 通過
    Given ok
  Scenario: 斷言失敗
    Given 斷言失敗
  Scenario: 端點未實作
    Given 端點未實作
  Scenario: 其他例外
    Given 其他例外
  Scenario: serve 起不來
    Given serve 起不來
  Scenario: 未定義步驟
    Given 沒有這個步驟
  @hookboom
  Scenario: hook 失敗
    Given ok
"""
_STEPS = """
import sys
from behave import given
from tests.blackbox.harness import EndpointNotImplemented, Serve
import tempfile, pathlib
@given("ok")
def s1(context):
    pass
@given("斷言失敗")
def s2(context):
    assert 1 == 2, "boom-marker"
@given("端點未實作")
def s3(context):
    raise EndpointNotImplemented("GET /x -> 404")
@given("其他例外")
def s4(context):
    raise RuntimeError("AssertionError: 訊息裡有這個字也不算")
@given("serve 起不來")
def s5(context):
    d = pathlib.Path(tempfile.mkdtemp())
    Serve(d / "h", cmd=[sys.executable, "-c", "import sys; sys.exit(3)"]).start()
"""
_ENV = """
def before_scenario(context, scenario):
    if "hookboom" in scenario.tags:
        assert False, "hook"
"""
_PASS_ONLY_FEATURE = "Feature: 全過\n  Scenario: 通過\n    Given ok\n"
_FEATURE_FILE = "features/s.feature"
_S = _FEATURE_FILE + "::"
_ALL_BAD = ("斷言失敗", "端點未實作", "其他例外", "serve 起不來", "未定義步驟", "hook 失敗")


@pytest.fixture(scope="module")
def behave_sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("behave-sample")
    (root / "features/steps").mkdir(parents=True)
    (root / _FEATURE_FILE).write_text(_FEATURE, encoding="utf-8")
    (root / "features/steps/s.py").write_text(_STEPS, encoding="utf-8")
    (root / "features/environment.py").write_text(_ENV, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("SPILLWAY_CMD", None)
    subprocess.run([sys.executable, "-m", "behave", "-f", "json", "-o", "report.json", "features"], cwd=root, env=env,
                   capture_output=True, text=True, timeout=180)
    return root


def _gate(root: Path, monkeypatch: pytest.MonkeyPatch, gap_text: str, *, impl: str = "rust",
          report: str = "report.json", impl_text: str = "") -> int:
    monkeypatch.chdir(root)
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    if impl == "rust":
        monkeypatch.setenv("SPILLWAY_IMPL", "rust")
        monkeypatch.setenv("SPILLWAY_CMD", FAKE_RUST_CMD)
    else:
        monkeypatch.setenv("SPILLWAY_IMPL", "python")
        monkeypatch.delenv("SPILLWAY_CMD", raising=False)
    g = root / "fixture-gaps.toml"
    g.write_text(gap_text, encoding="utf-8")
    i = root / "fixture-impl.toml"
    i.write_text(impl_text, encoding="utf-8")
    return behave_gate.main([report, "--root", "features"], gaps_file=g, impl_file=i)


def _behave_gaps(*names: str, extra: str = "") -> str:
    return _gap_toml(*[_S + n for n in names], kind="behave", extra=extra)


def _only_report(root: Path, tmp_path: Path, keep: tuple[str, ...]) -> str:
    data = json.loads((root / "report.json").read_text(encoding="utf-8"))
    for feat in data:
        feat["elements"] = [e for e in feat["elements"] if e["name"] in keep]
    out = tmp_path / "only.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _dry_expected(monkeypatch: pytest.MonkeyPatch, ids: set[str]) -> None:
    monkeypatch.setattr(behave_gate, "expected_ids", lambda roots: ids)


def test_gate_real_output_statuses(behave_sample):
    out = behave_gate.scenario_outcomes(json.loads((behave_sample / "report.json").read_text(encoding="utf-8")))
    assert {k.split("::")[1]: v.status for k, v in out.items()} == {
        "通過": "passed", "斷言失敗": "failed", "端點未實作": "failed", "其他例外": "error", "serve 起不來": "error",
        "未定義步驟": "error", "hook 失敗": "hook_error"}


def test_gate_positive_listed_failures_are_expected(behave_sample, tmp_path, monkeypatch):
    keep = ("通過", "斷言失敗", "端點未實作")
    _dry_expected(monkeypatch, {_S + n for n in keep})
    assert _gate(behave_sample, monkeypatch, _behave_gaps("斷言失敗", "端點未實作"),
                 report=_only_report(behave_sample, tmp_path, keep)) == 0


@pytest.mark.parametrize("name", ["其他例外", "serve 起不來", "未定義步驟", "hook 失敗"])
def test_gate_listed_but_outside_raises_is_failure(behave_sample, tmp_path, monkeypatch, name):
    keep = ("通過", name)
    _dry_expected(monkeypatch, {_S + n for n in keep})
    assert _gate(behave_sample, monkeypatch, _behave_gaps(name), report=_only_report(behave_sample, tmp_path, keep)) == 1


def test_gate_listed_pass_is_failure_strict(behave_sample, tmp_path, monkeypatch):
    _dry_expected(monkeypatch, {_S + "通過"})
    assert _gate(behave_sample, monkeypatch, _behave_gaps("通過"),
                 report=_only_report(behave_sample, tmp_path, ("通過",))) == 1


def test_gate_unlisted_failure_is_failure(behave_sample, tmp_path, monkeypatch):
    keep = ("通過", "斷言失敗")
    _dry_expected(monkeypatch, {_S + n for n in keep})
    assert _gate(behave_sample, monkeypatch, "", report=_only_report(behave_sample, tmp_path, keep)) == 1


def test_gate_gap_without_scenario_is_failure(behave_sample, tmp_path, monkeypatch):
    _dry_expected(monkeypatch, {_S + "通過"})
    assert _gate(behave_sample, monkeypatch, _behave_gaps("早就不存在"),
                 report=_only_report(behave_sample, tmp_path, ("通過",))) == 1


def test_gate_expect_must_match(behave_sample, tmp_path, monkeypatch):
    keep = ("斷言失敗",)
    _dry_expected(monkeypatch, {_S + n for n in keep})
    report = _only_report(behave_sample, tmp_path, keep)
    assert _gate(behave_sample, monkeypatch, _behave_gaps(*keep, extra='expect = "boom-marker"\n'), report=report) == 0
    assert _gate(behave_sample, monkeypatch, _behave_gaps(*keep, extra='expect = "other"\n'), report=report) == 1


def test_gate_python_applies_zero_gaps(behave_sample, tmp_path, monkeypatch):
    keep = ("通過", "斷言失敗")
    _dry_expected(monkeypatch, {_S + n for n in keep})
    assert _gate(behave_sample, monkeypatch, _behave_gaps("斷言失敗"), impl="python",
                 report=_only_report(behave_sample, tmp_path, keep)) == 1


def test_gate_partial_report_fails_against_real_dry_run(behave_sample, tmp_path, monkeypatch):
    """沒有替換 expected_ids:實際跑 `behave --dry-run`,報告少了情境就失敗(behave 中途崩潰不能算過)。"""
    assert _gate(behave_sample, monkeypatch, "", report=_only_report(behave_sample, tmp_path, ("通過",))) == 1


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"a": 1}'])
def test_gate_rejects_unusable_report(behave_sample, tmp_path, monkeypatch, content):
    _dry_expected(monkeypatch, {_S + "通過"})
    p = tmp_path / "bad.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    assert _gate(behave_sample, monkeypatch, "", report=str(p)) == 2


def test_gate_refuses_cmd_without_impl(behave_sample, monkeypatch):
    monkeypatch.chdir(behave_sample)
    monkeypatch.delenv("SPILLWAY_IMPL", raising=False)
    monkeypatch.setenv("SPILLWAY_CMD", FAKE_RUST_CMD)
    assert behave_gate.main(["report.json", "--root", "features"]) == 2


def _gate_cli(cwd: Path, report: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SPILLWAY_IMPL", "SPILLWAY_CMD")}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, "-m", "tests.parity.behave_gate", report, "--root", "features"], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=120)


def test_gate_cli_exit_codes_and_no_error_text(behave_sample, tmp_path):
    bad = _gate_cli(behave_sample, "report.json")
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "boom-marker" not in bad.stdout + bad.stderr      # 只印情境與狀態,不轉印錯誤內容
    root = tmp_path / "pass-only"
    (root / "features/steps").mkdir(parents=True)
    (root / "features/p.feature").write_text(_PASS_ONLY_FEATURE, encoding="utf-8")
    (root / "features/steps/s.py").write_text(
        'from behave import given\n@given("ok")\ndef s(context):\n    pass\n', encoding="utf-8")
    subprocess.run([sys.executable, "-m", "behave", "-f", "json", "-o", "report.json", "features"], cwd=root,
                   capture_output=True, text=True, timeout=120, check=True)
    good = _gate_cli(root, "report.json")
    assert good.returncode == 0, good.stdout + good.stderr


# ---------------------------------------------------------------- T2 仲裁 A1、A2、B1、B2、C7、C8


@pytest.mark.parametrize("text", gaps.REAL_CLI_TEXT)
def test_evaluate_real_cli_guard_message_is_not_an_expected_failure(text):
    sid = "features/s.feature::x"
    gap = gaps.Gap("behave", sid, "r", "b", "CHG-20261108-01")
    problems = behave_gate.evaluate({sid: behave_gate.Outcome("failed", f"前綴 {text} /secret/path")}, [gap], {sid})
    assert len(problems) == 1 and sid in problems[0] and "/secret/path" not in problems[0]


def test_real_cli_text_matches_the_harness():
    source = (REPO / "tests/fakes/cli_upstream/harness.py").read_text(encoding="utf-8")
    assert len(gaps.REAL_CLI_TEXT) == 2 and all(t in source for t in gaps.REAL_CLI_TEXT)


def _behave_entry(name: str, on_rust: str, extra: str = "") -> str:
    return f'[[entry]]\nkind = "behave"\nid = "{_S}{name}"\nreason = "r"\non_rust = "{on_rust}"\n{extra}\n'


_KEEP_FAIL = ("通過", "斷言失敗")


def test_gate_replace_with_existing_target_is_removed(behave_sample, tmp_path, monkeypatch, capsys):
    _dry_expected(monkeypatch, {_S + n for n in _KEEP_FAIL})
    rc = _gate(behave_sample, monkeypatch, "", report=_only_report(behave_sample, tmp_path, _KEEP_FAIL),
               impl_text=_behave_entry("斷言失敗", f"replace:{_S}通過"))
    assert rc == 0
    assert "replaced deselected=1" in capsys.readouterr().out


def test_gate_replace_without_target_still_fails(behave_sample, tmp_path, monkeypatch):
    _dry_expected(monkeypatch, {_S + n for n in _KEEP_FAIL})
    assert _gate(behave_sample, monkeypatch, "", report=_only_report(behave_sample, tmp_path, _KEEP_FAIL),
                 impl_text=_behave_entry("斷言失敗", f"replace:{_S}還沒有的情境")) == 1


def test_gate_not_applicable_is_removed_from_outcomes_and_expected(behave_sample, tmp_path, monkeypatch, capsys):
    _dry_expected(monkeypatch, {_S + n for n in (*_KEEP_FAIL, "報告裡沒有的情境")})
    rc = _gate(behave_sample, monkeypatch, "", report=_only_report(behave_sample, tmp_path, _KEEP_FAIL),
               impl_text=_behave_entry("斷言失敗", "not_applicable", 'on_rust_reason = "x"\n')
               + _behave_entry("報告裡沒有的情境", "not_applicable", 'on_rust_reason = "x"\n'))
    assert rc == 0
    assert "not_applicable deselected=1" in capsys.readouterr().out


@pytest.mark.parametrize("on_rust, extra", [("not_applicable", 'on_rust_reason = "x"\n'), (f"replace:{_S}通過", "")])
def test_gate_python_ignores_impl_dependent(behave_sample, tmp_path, monkeypatch, on_rust, extra):
    _dry_expected(monkeypatch, {_S + n for n in _KEEP_FAIL})
    assert _gate(behave_sample, monkeypatch, "", impl="python", report=_only_report(behave_sample, tmp_path, _KEEP_FAIL),
                 impl_text=_behave_entry("斷言失敗", on_rust, extra)) == 1


@pytest.mark.parametrize("on_rust, extra", [("not_applicable", 'on_rust_reason = "x"\n'), (f"replace:{_S}通過", "")])
def test_gate_gap_and_deselect_lists_are_exclusive(behave_sample, tmp_path, monkeypatch, on_rust, extra):
    _dry_expected(monkeypatch, {_S + n for n in _KEEP_FAIL})
    assert _gate(behave_sample, monkeypatch, _behave_gaps("斷言失敗"),
                 report=_only_report(behave_sample, tmp_path, _KEEP_FAIL),
                 impl_text=_behave_entry("斷言失敗", on_rust, extra)) == 2


def _with_statuses(root: Path, tmp_path: Path, statuses: dict[str, str]) -> str:
    """只留 `statuses` 內的情境,並把狀態改成指定值。"""
    data = json.loads((root / "report.json").read_text(encoding="utf-8"))
    for feat in data:
        feat["elements"] = [{**e, "status": statuses[e["name"]]} for e in feat["elements"] if e["name"] in statuses]
    out = tmp_path / "statuses.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def test_gate_all_skipped_is_invalid(behave_sample, tmp_path, monkeypatch):
    statuses = {"通過": "skipped", "斷言失敗": "skipped"}
    _dry_expected(monkeypatch, {_S + n for n in statuses})
    assert _gate(behave_sample, monkeypatch, "", report=_with_statuses(behave_sample, tmp_path, statuses)) == 2


def test_gate_all_skipped_after_deselect_is_invalid(behave_sample, tmp_path, monkeypatch):
    statuses = {"通過": "skipped", "斷言失敗": "failed"}
    _dry_expected(monkeypatch, {_S + n for n in statuses})
    assert _gate(behave_sample, monkeypatch, "", report=_with_statuses(behave_sample, tmp_path, statuses),
                 impl_text=_behave_entry("斷言失敗", "not_applicable", 'on_rust_reason = "x"\n')) == 2


def test_gate_partial_skip_passes_and_lists_each_id(behave_sample, tmp_path, monkeypatch, capsys):
    statuses = {"通過": "passed", "斷言失敗": "skipped"}
    _dry_expected(monkeypatch, {_S + n for n in statuses})
    assert _gate(behave_sample, monkeypatch, "", report=_with_statuses(behave_sample, tmp_path, statuses)) == 0
    assert f"skipped:{_S}斷言失敗" in capsys.readouterr().out.splitlines()


@pytest.fixture(scope="module")
def behave_feature_hook_sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("behave-feature-hook")
    (root / "features/steps").mkdir(parents=True)
    (root / "features/p.feature").write_text(_PASS_ONLY_FEATURE, encoding="utf-8")
    (root / "features/steps/s.py").write_text('from behave import given\n@given("ok")\ndef s(context):\n    pass\n',
                                              encoding="utf-8")
    (root / "features/environment.py").write_text('def after_feature(context, feature):\n    assert False, "boom"\n',
                                                  encoding="utf-8")
    subprocess.run([sys.executable, "-m", "behave", "-f", "json", "-o", "report.json", "features"], cwd=root,
                   capture_output=True, text=True, timeout=120)
    return root


@pytest.mark.parametrize("impl", ["python", "rust"])
def test_gate_feature_hook_failure_fails(behave_feature_hook_sample, monkeypatch, capsys, impl):
    assert _gate(behave_feature_hook_sample, monkeypatch, "", impl=impl) == 1
    assert "feature hook 失敗:features/p.feature(hook_error)" in capsys.readouterr().err


@pytest.mark.parametrize("status, n", [("hook_error", 1), ("cleanup_error", 1), ("passed", 0), ("failed", 0)])
def test_feature_problems_statuses(status, n):
    assert len(behave_gate.feature_problems([{"location": "f.feature:1", "status": status, "elements": []}])) == n


_DUP_REPORT = [{"location": "features/s.feature:1", "elements": [
    {"type": "scenario", "name": "同名", "status": "failed", "steps": []},
    {"type": "scenario", "name": "同名", "status": "passed", "steps": []},
]}]


def test_duplicate_scenario_is_a_report_error():
    with pytest.raises(behave_gate.ReportError):
        behave_gate.scenario_outcomes(_DUP_REPORT)


def test_gate_duplicate_scenario_exits_2(behave_sample, tmp_path, monkeypatch):
    _dry_expected(monkeypatch, {"features/s.feature::同名"})
    p = tmp_path / "dup.json"
    p.write_text(json.dumps(_DUP_REPORT), encoding="utf-8")
    assert _gate(behave_sample, monkeypatch, "", report=str(p)) == 2


def test_real_behave_ids_are_feature_path_and_name(collected):
    assert "tests/features/m2_routing.feature::slot 內依優先權重分攤 (AC-4)" in collected["behave"]


def test_toml_files_parse():
    for path in (gaps.RUST_GAPS_FILE, gaps.PROTECTED_FILE, gaps.IMPL_DEPENDENT_FILE):
        tomllib.loads(path.read_text(encoding="utf-8"))
