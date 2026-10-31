"""命令列入口(CHG-20261030-02 T13;決策 3、4、21;design D16)。

M2 只有 `spillway serve [--host 127.0.0.1] [--port 8787]`;`mcp-stdio`、`init` 於 M3 加入。

- `--host` 只接受 `settings.ALLOWED_HOSTS`(字面 `127.0.0.1`);其他值印錯誤到 stderr 並回 2,不啟動。
- `--port` 必須為 1–65535。
- 以 uvicorn 執行 `spillway.app.create_app()`(`lifespan="on"`:lifespan 失敗即啟動失敗)。
  退出碼:用法錯誤或 host 不允許回 2;啟動失敗(例如 7.6.6 模組衝突)回非 0——uvicorn 以
  `sys.exit(3)` 結束時沿用 3,未啟動而無退出碼時回 1(`EXIT_STARTUP_FAILED`)。
  Ctrl-C / SIGTERM:uvicorn 先完成正常關閉(lifespan shutdown、停止 RetentionJob 排程),再依 Unix
  慣例把收到的訊號重新送給自己,行程以該訊號結束(shell 看到 130 / 143,`subprocess` 看到 −2 / −15),
  不回 0;這讓上層的行程管理器分得出「被要求停止」與「自己結束」。本模組不改寫此行為。
- `main(argv)` 回傳退出碼;`python -m spillway` 以 `SystemExit(main())` 結束。
- uvicorn access log 保留(只有方法、路徑、狀態碼,不含 body)。
"""
from __future__ import annotations

import argparse
import sys

from spillway import settings

__all__ = ["main"]

EXIT_USAGE = 2
EXIT_STARTUP_FAILED = 1


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
                       help=f"綁定位址;M2 只允許 {settings.DEFAULT_HOST}")
    serve.add_argument("--port", type=_port, default=settings.DEFAULT_PORT,
                       help=f"埠號(預設 {settings.DEFAULT_PORT})")
    return p


def _run_server(host: str, port: int) -> int:
    import uvicorn

    from spillway import app as app_module

    config = uvicorn.Config(app_module.create_app(), host=host, port=port, lifespan="on",
                            log_level="info", access_log=True)
    server = uvicorn.Server(config)
    try:
        server.run()
    except SystemExit as exc:   # uvicorn 部分版本在啟動失敗時直接 sys.exit
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else EXIT_STARTUP_FAILED
    return 0 if server.started else EXIT_STARTUP_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:   # argparse 錯誤或 --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command != "serve":
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if not settings.is_allowed_host(args.host):
        allowed = ", ".join(sorted(settings.ALLOWED_HOSTS))
        print(f"spillway: 拒絕綁定 {args.host!r}:M2 只允許 {allowed}(其他位址需要 M3 的存取 token)",
              file=sys.stderr)
        return EXIT_USAGE
    return _run_server(args.host, args.port)
