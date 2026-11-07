"""目前 venv 與 `requirements-dev.lock` 的漂移偵測(CHG-20261107-01 T1;決策 6 ④,決策審議修訂 AM-7)。

這是漂移偵測,不是完整性保證;venv 若已被入侵,這個測試在 pytest 啟動時就已失去意義,完整性由
`scripts/check_venv_integrity.py` 負責。規則:(a) 每個已安裝套件都在 lock ∪ {spillway};(b) 已安裝版本屬於
lock 中該名的版本候選之一(字串相等,不求值 marker);(c) lock 中沒有 marker 的套件必須已安裝,有 marker
的未安裝時略過(平台與 Python 版本差異);另報告重複的 dist-info 名稱(施工審議修訂 F11)。
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path

from tests.helpers.lockfile import DUMMY_HASH as H, canon, parse_lock

ROOT = Path(__file__).resolve().parents[2]
REINSTALL = ("uv pip sync --python .venv/bin/python --require-hashes --no-build "
             "--build-constraint build-constraints.lock requirements-dev.lock\n"
             "uv pip install --python .venv/bin/python --no-deps --build-constraint build-constraints.lock -e .")


def _drift(installed: dict[str, str], lock: dict[str, set[tuple[str, str]]],
           duplicates: frozenset[str] = frozenset()) -> list[str]:
    problems: list[str] = [f"重複的 dist-info:{n}" for n in sorted(duplicates)]
    for name, version in sorted(installed.items()):
        if name == "spillway":
            continue
        if name not in lock:
            problems.append(f"lock 外的套件:{name}=={version}")
        elif version not in {v for v, _ in lock[name]}:
            problems.append(f"版本不符:{name}=={version}(lock:{sorted(v for v, _ in lock[name])})")
    for name, entries in sorted(lock.items()):
        if name not in installed and any(not marker for _, marker in entries):
            problems.append(f"未安裝:{name}")
    return problems


def _installed() -> tuple[dict[str, str], frozenset[str]]:
    seen: dict[str, str] = {}
    dup: set[str] = set()
    for d in importlib.metadata.distributions():
        name = canon(d.metadata["Name"])
        if name in seen and name != "spillway":   # PYTHONPATH=src 時 src/spillway.egg-info 也會被列出
            dup.add(name)
        seen[name] = d.version
    return seen, frozenset(dup)


def test_drift_rules_on_fake_lists():
    lock = parse_lock(f"a==1.0 {H}\nb==2.0 ; sys_platform == 'win32' {H}\nc==3.0 {H}\nc==3.1 ; python_version < '3.12' {H}\n")
    assert _drift({"a": "1.0", "c": "3.1", "spillway": "0.1.0"}, lock) == []          # b 有 marker 未裝略過
    assert _drift({"a": "1.0", "c": "3.0", "x": "9"}, lock) == ["lock 外的套件:x==9"]   # (a)
    assert _drift({"a": "1.1", "c": "3.0"}, lock) == ["版本不符:a==1.1(lock:['1.0'])"]  # (b)
    assert _drift({"c": "3.0"}, lock) == ["未安裝:a"]                                    # (c)
    assert _drift({"a": "1.0", "c": "3.0"}, lock, frozenset({"a"})) == ["重複的 dist-info:a"]


def test_venv_matches_dev_lock():
    installed, duplicates = _installed()
    problems = _drift(installed, parse_lock((ROOT / "requirements-dev.lock").read_text(encoding="utf-8")), duplicates)
    assert not problems, "目前 venv 與 requirements-dev.lock 不一致:\n" + "\n".join(problems) + \
        "\n請依 README 重裝:\n" + REINSTALL
