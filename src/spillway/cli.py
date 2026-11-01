"""命令列入口(CHG-20261030-02 T13;決策 3、4、21;design D16;CHG-20261101-03 T7 決策 5、14、15、17)。

`spillway serve [--host 127.0.0.1] [--port 8787]`、`spillway init [--rotate]`;`mcp-stdio` 於 M3b 加入。

serve:
- `--host` 只接受 `settings.ALLOWED_HOSTS`(字面 `127.0.0.1`;M3 仍只允許 127.0.0.1);其他值印錯誤到
  stderr 並回 2,不啟動。
- `--port` 必須為 1–65535。
- 啟動前置檢查 `_preflight(host, port, data_dir)`(決策 5 ①②):先以暫時 socket 試綁埠(隨即關閉),
  再以非阻塞方式取得資料目錄鎖 `spillway.lock` 並持有到行程結束;任一失敗印錯誤到 stderr、回
  `EXIT_STARTUP_FAILED`,不建 app、不開資料庫、不 migrate。
- 以 uvicorn 執行 `spillway.app.create_app(issue_token=True)`(`lifespan="on"`:lifespan 失敗即啟動
  失敗)。資料庫沒有存取 token 雜湊時,lifespan 產生 token 並以單行印到 stderr(只此一次)。
  退出碼:用法錯誤或 host 不允許回 2;啟動失敗(例如 7.6.6 模組衝突)回非 0——uvicorn 以
  `sys.exit(3)` 結束時沿用 3,未啟動而無退出碼時回 1(`EXIT_STARTUP_FAILED`)。
  Ctrl-C / SIGTERM:uvicorn 先完成正常關閉(lifespan shutdown、停止 RetentionJob 排程),再依 Unix
  慣例把收到的訊號重新送給自己,行程以該訊號結束(shell 看到 130 / 143,`subprocess` 看到 −2 / −15),
  不回 0;這讓上層的行程管理器分得出「被要求停止」與「自己結束」。本模組不改寫此行為。
- uvicorn access log 保留(只有方法、路徑、狀態碼,不含 body)。

init(決策 14、15):建立資料目錄與 `work/claude`、`work/codex`;在資料目錄鎖內開啟資料庫(執行
migration,含舊版庫的備份;不做埠檢查)並產生存取 token。token 明文只印到 stdout 一次(單獨一行),
資料庫只存 SHA-256。已有 token 時印「已存在,未變更」並回 0;`--rotate` 才重新產生。鎖被占用(serve
正在執行)或 migration 失敗時回 `EXIT_STARTUP_FAILED`。

`main(argv)` 回傳退出碼;`python -m spillway` 以 `SystemExit(main())` 結束。
"""
from __future__ import annotations

import argparse
import socket
import sqlite3
import sys
from pathlib import Path

from spillway import settings
from spillway.store.db import (
    DataDirLockedError,
    MigrationError,
    SchemaVersionError,
    acquire_data_dir_lock,
    connect,
    data_dir_lock,
)

__all__ = ["main"]

EXIT_USAGE = 2
EXIT_STARTUP_FAILED = 1
WORK_SUBDIRS = ("claude", "codex")   # 決策 10、11:CLI 模組未設定 cwd 時的工作目錄


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("埠號必須是整數") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("埠號必須在 1–65535")
    return port


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spillway", description="單人使用的 LLM 路由閘道")
    sub = p.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="啟動 HTTP 伺服器")
    serve.add_argument("--host", default=settings.DEFAULT_HOST,
                       help=f"綁定位址;M3 仍只允許 {settings.DEFAULT_HOST}")
    serve.add_argument("--port", type=_port, default=settings.DEFAULT_PORT,
                       help=f"埠號(預設 {settings.DEFAULT_PORT})")
    init = sub.add_parser("init", help="建立資料目錄、升級資料庫並產生存取 token")
    init.add_argument("--rotate", action="store_true", help="重新產生存取 token(舊 token 失效)")
    return p


def _err(message: str) -> None:
    print(f"spillway: {message}", file=sys.stderr)


def _preflight(host: str, port: int, data_dir: Path) -> int:
    """serve 開啟資料庫前的前置檢查(決策 5 ①②);通過回 0,否則印錯誤並回非 0。

    ① 以暫時 socket 試綁 `(host, port)` 後關閉(與 uvicorn 一樣設 `SO_REUSEADDR`);
    ② 建立資料目錄並取得 `spillway.lock`(非阻塞),持有到行程結束。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as exc:
        _err(f"無法綁定 {host}:{port}({exc.strerror or type(exc).__name__});"
             "未開啟資料庫、未執行 migration")
        return EXIT_STARTUP_FAILED
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        acquire_data_dir_lock(data_dir)
    except DataDirLockedError as exc:
        _err(f"{exc};未執行 migration")
        return EXIT_STARTUP_FAILED
    except OSError as exc:
        _err(f"無法建立或鎖定資料目錄 {data_dir}({exc.strerror or type(exc).__name__})")
        return EXIT_STARTUP_FAILED
    return 0


def _run_server(host: str, port: int) -> int:
    if rc := _preflight(host, port, settings.data_dir()):
        return rc

    import uvicorn

    from spillway import app as app_module

    config = uvicorn.Config(app_module.create_app(issue_token=True), host=host, port=port,
                            lifespan="on", log_level="info", access_log=True)
    server = uvicorn.Server(config)
    try:
        server.run()
    except SystemExit as exc:   # uvicorn 部分版本在啟動失敗時直接 sys.exit
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else EXIT_STARTUP_FAILED
    return 0 if server.started else EXIT_STARTUP_FAILED


def _init(rotate: bool) -> int:
    """`spillway init [--rotate]`(決策 14、15)。"""
    home = settings.data_dir()
    try:
        for d in (home, *(home / "work" / name for name in WORK_SUBDIRS)):
            d.mkdir(parents=True, exist_ok=True)
        with data_dir_lock(home):
            conn = connect(settings.db_path())
            try:
                if rotate:
                    token: str | None = settings.rotate_access_token(conn)
                else:
                    token = settings.ensure_access_token(conn)
            finally:
                conn.close()
    except DataDirLockedError as exc:
        _err(f"{exc};init 未執行(要重新產生 token 可改用 POST /api/settings/token/rotate)")
        return EXIT_STARTUP_FAILED
    except (MigrationError, SchemaVersionError, sqlite3.Error, OSError) as exc:
        _err(f"init 失敗:{type(exc).__name__}: {exc}")
        return EXIT_STARTUP_FAILED
    if token is None:
        print("spillway: 存取 token 已存在,未變更(要重新產生請執行 spillway init --rotate)")
        return 0
    _err(f"資料目錄 {home};已產生存取 token(印在 stdout,只顯示這一次;資料庫只存 SHA-256 雜湊)")
    print(token, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:   # argparse 錯誤或 --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "init":
        return _init(args.rotate)
    if args.command != "serve":
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if not settings.is_allowed_host(args.host):
        allowed = ", ".join(sorted(settings.ALLOWED_HOSTS))
        print(f"spillway: 拒絕綁定 {args.host!r}:M3 仍只允許 {allowed}", file=sys.stderr)
        return EXIT_USAGE
    return _run_server(args.host, args.port)
