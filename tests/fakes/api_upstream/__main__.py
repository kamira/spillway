"""以前景行程啟動假上游:`python -m tests.fakes.api_upstream [--port N]`(只綁 127.0.0.1)。"""
from __future__ import annotations

import argparse
import threading

from tests.fakes.api_upstream.server import FakeApiServer


def main() -> None:
    parser = argparse.ArgumentParser(description="spillway 測試用假 API 上游(只綁 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="埠號;0 為隨機")
    args = parser.parse_args()
    server = FakeApiServer(port=args.port).start()
    print(server.base_url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
