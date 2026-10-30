"""命令列入口。M2 只有 `serve`(CHG-20261030-02 決策 3);`mcp-stdio`、`init` 於 M3 加入。"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print("spillway: serve 尚未實作(CHG-20261030-02 T13)", file=sys.stderr)
    return 2
