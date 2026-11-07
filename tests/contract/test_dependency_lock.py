"""雜湊 lock 檔契約(CHG-20261107-01 T1;決策 3、5、6 ③、8、9,決策審議修訂 AM-6)。

只讀檔,與目前環境無關:三個 lock 的每個套件為 `==` 確切版本並附 sha256;檔頭無指令註解;`pyproject.toml`
的 runtime 相依都在兩份 lock、dev 相依都在 dev lock;兩份 lock 的共同套件版本相同;litellm 不在被植入
的版本,`pyproject.toml` 的 litellm 規格排除它們;`build-constraints.lock` 只鎖 setuptools 且與 `.in` 相同。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.helpers.lockfile import BLOCKED_LITELLM, LockError, canon, parse_lock

ROOT = Path(__file__).resolve().parents[2]
LOCKS = ("requirements.lock", "requirements-dev.lock", "build-constraints.lock")
H = "--hash=sha256:" + "a" * 64


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _req_name(spec: str) -> str:
    return canon(re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec).group(1))


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(_read("pyproject.toml"))


# ---- 解析器自測(字串樣本) ----------------------------------------------------------------------

def test_parser_accepts_marker_and_multiple_hashes():
    text = (f"foo-Bar==1.2 ; sys_platform == 'win32' \\\n    {H} \\\n    {H}\n"
            f"# comment\n\nbaz==3.0 \\\n    {H}\n")
    assert parse_lock(text) == {"foo-bar": {("1.2", "sys_platform == 'win32'")}, "baz": {("3.0", "")}}


@pytest.mark.parametrize("bad", [f"foo>=1.0 {H}", "foo==1.0", f"foo @ https://x/y.whl {H}",
                                 "-e file:///tmp/x", "--index-url https://example.invalid/simple"])
def test_parser_rejects(bad: str):
    with pytest.raises(LockError):
        parse_lock(bad + "\n")


# ---- 三個 lock 檔 -------------------------------------------------------------------------------

@pytest.mark.parametrize("name", LOCKS)
def test_lock_pins_exact_versions_with_sha256(name: str):
    pkgs = parse_lock(_read(name))           # 格式錯即 LockError
    assert pkgs, f"{name} 是空的"


@pytest.mark.parametrize("name", LOCKS)
def test_lock_has_no_header_comment(name: str):
    assert "#" not in _read(name), f"{name} 含註解(以 --no-header --no-annotate 重產)"


def test_runtime_deps_in_both_locks(pyproject: dict):
    runtime = {_req_name(s) for s in pyproject["project"]["dependencies"]}
    for name in ("requirements.lock", "requirements-dev.lock"):
        missing = runtime - set(parse_lock(_read(name)))
        assert not missing, f"{name} 缺 runtime 相依:{sorted(missing)}"


def test_dev_deps_in_dev_lock(pyproject: dict):
    dev = {_req_name(s) for s in pyproject["project"]["optional-dependencies"]["dev"]}
    missing = dev - set(parse_lock(_read("requirements-dev.lock")))
    assert not missing, f"requirements-dev.lock 缺 dev 相依:{sorted(missing)}"


def test_shared_packages_have_same_versions():
    rt, dev = parse_lock(_read("requirements.lock")), parse_lock(_read("requirements-dev.lock"))
    diff = {n: (sorted(v for v, _ in rt[n]), sorted(v for v, _ in dev[n]))
            for n in set(rt) & set(dev) if {v for v, _ in rt[n]} != {v for v, _ in dev[n]}}
    assert not diff, f"兩份 lock 的共同套件版本不同:{diff}"


def test_litellm_not_a_blocked_version(pyproject: dict):
    for name in ("requirements.lock", "requirements-dev.lock"):
        versions = {v for v, _ in parse_lock(_read(name))["litellm"]}
        assert not versions & BLOCKED_LITELLM, f"{name} 的 litellm 為已知被植入的版本"
    spec = next(s for s in pyproject["project"]["dependencies"] if _req_name(s) == "litellm")
    excluded = {p.strip()[2:].strip() for p in spec[len("litellm"):].split(",") if p.strip().startswith("!=")}
    assert excluded >= BLOCKED_LITELLM, f"pyproject 的 litellm 規格未排除:{sorted(BLOCKED_LITELLM - excluded)}"


def test_build_constraints_only_setuptools_matching_in_file():
    pkgs = parse_lock(_read("build-constraints.lock"))
    assert set(pkgs) == {"setuptools"}
    pinned = re.fullmatch(r"setuptools==(\S+)", _read("build-constraints.in").strip()).group(1)
    assert {v for v, _ in pkgs["setuptools"]} == {pinned}
