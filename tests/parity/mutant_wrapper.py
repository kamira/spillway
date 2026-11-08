#!/usr/bin/env python3
"""差分比對的第二實作:包裝實作並改動線上內容的突變 wrapper(CHG-20261108-01 T3;AM-1、AM-10;spec 15.2)。

    python tests/parity/mutant_wrapper.py --mutation json_body -- <實作命令...> serve --port 1234

以標準庫寫成、不 import spillway 或 tests(可當 `SPILLWAY_PARITY_CMD_B`,也經 `Serve(cmd=...)` 啟動)。把實作放在內部埠上,自己綁
`--port` 的埠當反向代理,依 `--mutation` 改動 `POST /v1/chat/completions` 的回應或資料庫:
- `status`:狀態碼 200 → 201。
- `header`:`X-Spillway-Served-By` 換值。
- `json_body`:`$.choices[0].message.content` 換值。
- `key_in_response`:JSON body 多一個帶語料金鑰的欄位(`SPILLWAY_PARITY_KEY_OPENAI` 的值)。
- `db_row`:回應送出前把最新一列 `usage_ledger.tokens_in` 加 1。
- `log_key`:向 stderr 寫一行帶語料金鑰的 `SPILLWAY-LOG` 行。
- `none`:不改動(驗證 wrapper 本身不造成差異)。

內部埠一律在 JSON 回應中換回外部埠(`$.port` 與 MCP URL 因此仍等於本次埠)。訊號處理為 harness 的 ② 型:實作在另一個行程群組,
SIGINT、SIGTERM 轉送給它,實作結束後以相同狀態結束(訊號結束時重設預設處置後對自己重送,不改成 exit 128+n)。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MUTATIONS = ("status", "header", "json_body", "key_in_response", "db_row", "log_key", "none")
CHAT_PATH = "/v1/chat/completions"
KEY_ENV = "SPILLWAY_PARITY_KEY_OPENAI"
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _db_row_mutation() -> None:
    conn = sqlite3.connect(os.path.join(os.environ["SPILLWAY_HOME"], "spillway.db"), timeout=10)
    try:
        conn.execute("UPDATE usage_ledger SET tokens_in = tokens_in + 1 WHERE id = (SELECT max(id) FROM usage_ledger)")
        conn.commit()
    finally:
        conn.close()


def _mutate_body(mutation: str, body: bytes) -> bytes:
    data = json.loads(body)
    if mutation == "json_body":
        data["choices"][0]["message"]["content"] += " (mutated)"
    elif mutation == "key_in_response":
        data["leaked"] = os.environ[KEY_ENV]
    return json.dumps(data).encode("utf-8")


def make_handler(mutation: str, inner_port: int, outer_port: int) -> type[BaseHTTPRequestHandler]:
    port_re = re.compile(rb"(?<![0-9a-zA-Z])" + str(inner_port).encode() + rb"(?![0-9a-zA-Z])")

    class Handler(BaseHTTPRequestHandler):
        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
            conn = http.client.HTTPConnection("127.0.0.1", inner_port, timeout=120)
            try:
                conn.request(self.command, self.path, body=data, headers=headers)
                resp = conn.getresponse()
            except OSError:   # 實作尚未就緒(harness 輪詢)或已結束:回 502,不寫 traceback
                self.send_error(502)
                return
            body, status, out_headers = resp.read(), resp.status, resp.getheaders()
            conn.close()
            content_type = next((v for k, v in out_headers if k.lower() == "content-type"), "")
            if "json" in content_type:
                body = port_re.sub(str(outer_port).encode(), body)
            chat_ok = self.path.split("?", 1)[0] == CHAT_PATH and status == 200
            if chat_ok and mutation == "status":
                status = 201
            if chat_ok and mutation in ("json_body", "key_in_response"):
                body = _mutate_body(mutation, body)
            if chat_ok and mutation == "db_row":
                _db_row_mutation()
            if chat_ok and mutation == "log_key":
                sys.stderr.write(f"SPILLWAY-LOG INFO mutant {os.environ[KEY_ENV]}\n")
                sys.stderr.flush()
            self.send_response(status)
            for name, value in out_headers:
                if name.lower() in HOP_BY_HOP:
                    continue
                if chat_ok and mutation == "header" and name.lower() == "x-spillway-served-by":
                    value = value + "-mutated"
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _forward

        def log_message(self, format: str, *args: object) -> None:   # 不寫 stderr(harness 掃描 stderr)
            pass

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:   # 不寫 stderr(harness 掃描 stderr)
        pass


def _parse(argv: list[str]) -> tuple[str, list[str], int]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutation", required=True, choices=MUTATIONS)
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    idx = len(rest) - 1 - rest[::-1].index("--port")
    return args.mutation, rest[:idx + 1] + ["<inner>"] + rest[idx + 2:], int(rest[idx + 1])


def main(argv: list[str]) -> int:
    mutation, cmd, outer_port = _parse(argv)
    inner_port = _free_port()
    server = _QuietServer(("127.0.0.1", outer_port), make_handler(mutation, inner_port, outer_port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = subprocess.Popen([str(inner_port) if a == "<inner>" else a for a in cmd], start_new_session=True)

    def forward(signum: int, _frame: object) -> None:
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    rc = proc.wait()
    server.server_close()
    if rc < 0:
        signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
        signal.pause()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
