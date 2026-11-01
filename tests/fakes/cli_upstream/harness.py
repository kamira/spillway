"""假 CLI 執行檔與真實 CLI 護欄的實作(CHG-20261101-03 T3);協定說明見 `tests/fakes/cli_upstream/__init__.py`。"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CLI_NAMES", "SCENARIOS", "PRESENCE_VARS", "FAKE_MARKER", "FAKE_SCRIPT", "RealCliFound",
    "install", "is_fake", "real_cli_dirs", "sanitize_path", "assert_only_fakes",
    "safe_subprocess_env", "FakeCli",
]

CLI_NAMES = ("claude", "codex")
SCENARIOS = ("ok", "quota", "rate_limit", "error", "timeout", "non_json",
             "hold_until_release", "grandchild_holds_stdout")
PRESENCE_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
FAKE_MARKER = "# spillway-fake-cli (tests/fakes/cli_upstream)"
FAKE_SCRIPT = Path(__file__).resolve().parent / "fake.py"
_MARKER_BYTES = FAKE_MARKER.encode()


class RealCliFound(AssertionError):
    """PATH 上找得到非假的 `claude` 或 `codex`。"""


# ---- 假執行檔 ----

def install(bin_dir: Path) -> Path:
    """在 `bin_dir` 產生假 `claude`、`codex`;回傳 `bin_dir`。"""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in CLI_NAMES:
        target = bin_dir / name
        target.write_text(
            "#!/bin/sh\n"
            f"{FAKE_MARKER}\n"
            f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(FAKE_SCRIPT))} {name} \"$@\"\n",
            encoding="utf-8",
        )
        target.chmod(0o755)
    return bin_dir


def is_fake(path: str | os.PathLike[str]) -> bool:
    """`path` 是 `install()` 產生的假執行檔(只讀檔頭,不執行)。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return _MARKER_BYTES in head


# ---- 護欄 ----

def _unsafe_entry(entry: str) -> bool:
    """空、相對或以 `~` 開頭的 PATH 項目:子行程以自己的 cwd 解析相對項目,macOS `/bin/sh` 查找時會展開 `~`,
    兩者都可能在子行程裡解析到真實 CLI,一律視為不安全。"""
    return not entry or entry.startswith("~") or not os.path.isabs(entry)


def _candidates(entry: str) -> list[Path]:
    found = []
    for name in CLI_NAMES:
        p = Path(entry) / name
        if p.is_file() and os.access(p, os.X_OK):
            found.append(p)
    return found


def real_cli_dirs(path: str | None = None) -> list[str]:
    """PATH 中不安全(空、相對、`~` 開頭)或含有非假 `claude`/`codex` 的項目。"""
    path = os.environ.get("PATH", "") if path is None else path
    return [e for e in path.split(os.pathsep)
            if _unsafe_entry(e) or any(not is_fake(c) for c in _candidates(e))]


def sanitize_path(path: str | None = None) -> str:
    """移除 PATH 中不安全(空、相對、`~` 開頭)或含有非假 `claude`/`codex` 的項目。"""
    path = os.environ.get("PATH", "") if path is None else path
    bad = set(real_cli_dirs(path))
    return os.pathsep.join(e for e in path.split(os.pathsep) if e not in bad)


def assert_only_fakes(path: str | None = None) -> dict[str, str | None]:
    """斷言 `shutil.which` 在 `path` 上找到的 `claude`、`codex` 只會是假執行檔(或找不到)。

    回傳 `{name: 解析到的路徑或 None}`;找到真實執行檔時丟 `RealCliFound`。
    """
    path = os.environ.get("PATH", "") if path is None else path
    resolved: dict[str, str | None] = {}
    for name in CLI_NAMES:
        hit = shutil.which(name, path=path)
        if hit is not None and not is_fake(hit):
            raise RealCliFound(f"PATH 上找到真實的 {name}:{hit}(測試不可執行真實 CLI)")
        resolved[name] = hit
    bad = real_cli_dirs(path)
    if bad:
        raise RealCliFound(f"PATH 中仍有不安全或含真實 claude/codex 的項目:{bad}")
    return resolved


def safe_subprocess_env(base: Mapping[str, str] | None = None, *,
                        fake_bin: Path | None = None) -> dict[str, str]:
    """測試啟動的子行程(serve 等)用的環境:複製 `base`(預設 `os.environ`),PATH 清理後
    (可選)把 `fake_bin` 放到最前面,並斷言只會找到假執行檔。"""
    env = dict(os.environ if base is None else base)
    path = sanitize_path(env.get("PATH", ""))
    if fake_bin is not None:
        path = os.pathsep.join([str(fake_bin), path]) if path else str(fake_bin)
    env["PATH"] = path
    assert_only_fakes(path)
    return env


# ---- 控制檔與記錄檔 ----

class FakeCli:
    """控制假 CLI 的輔助:`home` 為 `SPILLWAY_HOME`,`bin_dir` 為假執行檔目錄。"""

    def __init__(self, home: Path, bin_dir: Path) -> None:
        self.home = Path(home)
        self.bin_dir = Path(bin_dir)

    @property
    def control_dir(self) -> Path:
        return self.home / "fake_cli"

    @property
    def records_dir(self) -> Path:
        return self.control_dir / "records"

    def path(self, name: str) -> Path:
        return self.bin_dir / name

    def _write_control(self, name: str, data: dict[str, Any]) -> None:
        if name not in CLI_NAMES:
            raise ValueError(f"未知的 CLI:{name}")
        self.control_dir.mkdir(parents=True, exist_ok=True)
        target = self.control_dir / f"{name}.json"
        tmp = target.with_name(target.name + f".tmp-{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, target)

    def control(self, name: str) -> dict[str, Any]:
        try:
            return json.loads((self.control_dir / f"{name}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def set(self, name: str, scenario: str = "ok", *, delay_seconds: float = 0, **extra: Any) -> None:
        """設定情境(會清掉先前的 `released`)。"""
        if scenario not in SCENARIOS:
            raise ValueError(f"未知的情境:{scenario}")
        self._write_control(name, {"scenario": scenario, "delay_seconds": delay_seconds, **extra})

    def release(self, name: str) -> None:
        """放行 `hold_until_release`。"""
        self._write_control(name, {**self.control(name), "released": True})

    def records(self, name: str | None = None) -> list[dict[str, Any]]:
        """記錄檔(依開始時間排序);`name` 為 None 時兩種都列。"""
        if not self.records_dir.is_dir():
            return []
        out = []
        for p in self.records_dir.glob("*.json"):
            if name is not None and not p.name.startswith(f"{name}-"):
                continue
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda r: r.get("started_at", 0))

    def wait_for_records(self, name: str, count: int = 1, *, timeout: float = 10.0,
                         finished: bool = False) -> list[dict[str, Any]]:
        """等到 `name` 至少有 `count` 個記錄檔(`finished=True` 時要已結束)。逾時丟 `TimeoutError`。"""
        deadline = time.monotonic() + timeout
        while True:
            recs = [r for r in self.records(name) if r.get("finished") or not finished]
            if len(recs) >= count:
                return recs
            if time.monotonic() > deadline:
                raise TimeoutError(f"等不到 {name} 的 {count} 個記錄檔(目前 {len(recs)})")
            time.sleep(0.02)

    def clear_records(self) -> None:
        if self.records_dir.is_dir():
            shutil.rmtree(self.records_dir)
