"""驗收用子行程:`SPILLWAY_CMD serve` 與 `python -m tests.fakes.api_upstream`(CHG-20261030-02 T15)。

黑箱(CHG-20261107-03 T3):serve 一律經 `tests.blackbox.harness.Serve`(`SPILLWAY_CMD`、行程群組、就緒條件
`GET /api/settings/context` 200 + 存活、`stop()` 斷言結束碼與 stderr;spec 14);本模組不 import spillway。

- 子行程的環境:`tests.blackbox.harness.child_env`(`SPILLWAY_HOME` 為呼叫端給的暫存目錄、移除真實 API key 與
  測試旋鈕、PATH 只找得到假 CLI)另加 `SPILLWAY_TEST_KEY=sk-cred-serve-7777`(與 prompt 字串 `sk-test-0000` 不同)。
- `start_serve(home, port, *, log_path=None, wait=True, **opts) -> Serve`:未開啟測試控制介面的一般 serve
  (`control=False`,與正式使用相同);`opts` 原樣交給 `Serve`。`wait=False` 時只啟動不等就緒。
- `listening(port)`:在該埠以 TCP LISTEN 的位址(`lsof -nP -iTCP:<port> -sTCP:LISTEN` 的 NAME 欄;以埠而非 pid
  觀察,`SPILLWAY_CMD` 為包裝程式時同樣成立)。`wait_listening(serve)` 等到就緒且有監聽位址。
- `stop(p)`:`Serve` 以 `Serve.stop()`(SIGTERM,斷言結束碼與 stderr;冪等);一般 `Popen` 先 SIGTERM、逾時再
  SIGKILL。呼叫端以 try/finally 確保結束所有子行程。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tests.blackbox import harness, ports
from tests.blackbox.harness import Serve

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_KEY_VARS = harness.REAL_KEY_VARS
# 子行程的憑證值;與 L4 prompt 裡的金鑰字串(`PROMPT_KEY`)不同(末端審查 M28)
TEST_KEY = "sk-cred-serve-7777"
PROMPT_KEY = "sk-test-0000"
HAS_LSOF = shutil.which("lsof") is not None


def free_port() -> int:
    return ports.free_port()


def child_env(home: Path) -> dict[str, str]:
    return harness.child_env(home, extra={"SPILLWAY_TEST_KEY": TEST_KEY})


def listening(port: int) -> list[str]:
    """在 `port` 以 TCP LISTEN 的位址(`lsof -nP -iTCP:<port> -sTCP:LISTEN` 的 NAME 欄,去重後排序)。"""
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return sorted({ln.split()[-2] for ln in r.stdout.splitlines()[1:] if "(LISTEN)" in ln})


def stop(p: Any) -> None:
    if isinstance(p, Serve):
        p.stop()
        return
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


def start_serve(home: Path, port: int, *, log_path: Path | None = None, wait: bool = True, **opts: Any) -> Serve:
    """啟動一般 serve(未開啟測試控制介面);stderr 的遮罩副本寫入 `log_path`(預設 home 旁)。"""
    extra = {"SPILLWAY_TEST_KEY": TEST_KEY, **opts.pop("extra_env", {})}
    opts.setdefault("control", False)
    s = Serve(home, port=port, log_path=log_path or Path(home).parent / f"serve-{port}.log", extra_env=extra,
              **opts)
    return s.start(wait_ready=wait)


def wait_listening(p: Serve, timeout: float = 60.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.returncode is not None:
            raise RuntimeError(f"serve 提前結束 rc={p.returncode}")
        addrs = listening(p.port)
        if addrs:
            return addrs
        time.sleep(0.2)
    raise RuntimeError("serve 未在時限內開始監聽")


def start_fake_upstream(home: Path) -> tuple[subprocess.Popen, str]:
    """`python -m tests.fakes.api_upstream --port 0`(不 import spillway);回傳 (行程, base_url)。"""
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
    return ports.lan_ip()
