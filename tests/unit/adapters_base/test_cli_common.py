"""adapters.base.cli_common(CHG-20261102-01 決策 20)。"""
import pytest

from spillway.adapters.base import AdapterContext
from spillway.adapters.base.cli_common import CLI_WORK_SUBDIRS, cli_cwd, head, nonneg_int, tail
from spillway.clock import FakeClock


def _ctx(data_dir):
    return AdapterContext(credentials=lambda ref: "unused", clock=FakeClock(), data_dir=data_dir)


@pytest.mark.parametrize("sub", ["claude", "codex"])
def test_cli_cwd_default_is_data_dir_work_sub(tmp_path, sub):
    cwd = cli_cwd({}, _ctx(tmp_path), sub)
    assert cwd == tmp_path / "work" / sub and cwd.is_dir()


def test_cli_cwd_params_overrides_and_expands(tmp_path, monkeypatch):
    assert cli_cwd({"cwd": str(tmp_path)}, _ctx(None), "claude") == tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli_cwd({"cwd": "~/x"}, _ctx(None), "codex") == tmp_path / "x"
    assert not (tmp_path / "work").exists()


def test_cli_cwd_no_data_dir_raises_value_error():
    with pytest.raises(ValueError) as ei:
        cli_cwd({}, _ctx(None), "codex")
    assert "cwd" in str(ei.value) and "data_dir" in str(ei.value) and "codex" in str(ei.value)


def test_cli_cwd_unusable_dir_raises_value_error(tmp_path):
    (tmp_path / "work").write_text("x")
    with pytest.raises(ValueError):
        cli_cwd({}, _ctx(tmp_path), "claude")


def test_head_tail():
    assert head("  abc  ", 5) == "abc" and tail("  abc  ", 5) == "abc"
    assert head("abcdef", 3) == "abc…"
    assert tail("abcdef", 3) == "…def"


@pytest.mark.parametrize("v,expected", [(0, 0), (5, 5), (-1, None), (True, None), (1.5, None),
                                         ("3", None), (None, None)])
def test_nonneg_int(v, expected):
    assert nonneg_int(v) == expected


def test_work_subdirs():
    assert CLI_WORK_SUBDIRS == ("claude", "codex")
