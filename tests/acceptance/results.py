"""驗收量測的共用函式(CHG-20261030-02 T15)。"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO_ROOT / "artifacts" / "m2"
RESULTS_FILE = ARTIFACTS / "acceptance-results.json"


def record_result(key: str, value: Any) -> None:
    """量測數字(不含任何秘密)合併寫入 `acceptance-results.json`。"""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    data[key] = value
    RESULTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")


def p95(samples_ms: list[float]) -> float:
    """nearest-rank P95(與 spec 5.4.3 同一百分位定義)。"""
    ordered = sorted(samples_ms)
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]
