"""驗收用子行程:`python -m spillway serve` 與 `python -m tests.fakes.api_upstream`(CHG-20261030-02 T15)。

- 子行程的環境:`SPILLWAY_HOME` 為呼叫端給的暫存目錄、`PYTHONPATH` 為 `src` 與 repo 根目錄、
  `LITELLM_LOCAL_MODEL_COST_MAP=True`、`SPILLWAY_TEST_KEY=sk-cred-serve-7777`(與 prompt 字串 `sk-test-0000` 不同),並移除真實 API key 變數。
- `stop()` 先 SIGTERM、逾時再 SIGKILL;呼叫端以 try/finally 確保結束所有子行程。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# 子行程的憑證值;與 L4 prompt 裡的金鑰字串(`PROMPT_KEY`)不同(末端審查 M28)
TEST_KEY = "sk-cred-serve-7777"
PROMPT_KEY = "sk-test-0000"
HAS_LSOF = shutil.which("lsof") is not None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in REAL_KEY_VARS}
    env["SPILLWAY_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["SPILLWAY_TEST_KEY"] = TEST_KEY
    return env


def listening(pid: int) -> list[str]:
    """該行程以 TCP LISTEN 的位址(`lsof -nP -iTCP -sTCP:LISTEN -a -p <pid>` 的 NAME 欄)。"""
    r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                       capture_output=True, text=True)
    return [ln.split()[-2] for ln in r.stdout.splitlines()[1:] if "(LISTEN)" in ln]


def stop(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=15)
    for stream in (p.stdout, p.stderr):
        if stream is not None:
            stream.close()


def start_serve(home: Path, port: int, *extra: str, log_path: Path | None = None) -> subprocess.Popen:
    """啟動 `python -m spillway serve --port <port>`;stdout/stderr 寫入 `log_path`(預設 home 旁)。"""
    log = open(log_path or home.parent / f"serve-{port}.log", "w", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", "spillway", "serve", "--port", str(port), *extra],
                            env=child_env(home), cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
                            text=True)


def wait_listening(p: subprocess.Popen, timeout: float = 60.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.poll() is not None:
            raise RuntimeError(f"serve 提前結束 rc={p.returncode}")
        addrs = listening(p.pid)
        if addrs:
            return addrs
        time.sleep(0.2)
    raise RuntimeError("serve 未在時限內開始監聽")


def start_fake_upstream(home: Path) -> tuple[subprocess.Popen, str]:
    """`python -m tests.fakes.api_upstream --port 0`;回傳 (行程, base_url)。"""
    p = subprocess.Popen([sys.executable, "-m", "tests.fakes.api_upstream", "--port", "0"],
                         env=child_env(home), cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    assert p.stdout is not None
    line = p.stdout.readline().strip()
    if not line.startswith("http://127.0.0.1:"):
        stop(p)
        raise RuntimeError(f"假上游啟動失敗:{line!r}")
    return p, line


def lan_ip() -> str | None:
    """本機的非 loopback IPv4(不送出封包:UDP connect 只做路由選擇);沒有時回 None。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
