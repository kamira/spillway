"""litellm 價格表的檔案讀取(CHG-20261107-03 T2;決策 9 ③、11;AM-9;DIR-5 ②)。

預期花費不取自產品程式:直接讀 litellm 套件內附的價格 JSON(`model_prices_and_context_window_backup.json`,
`LITELLM_LOCAL_MODEL_COST_MAP=True` 時 litellm 用的就是這份)當資料。只以 `importlib.util.find_spec`
找到套件目錄,**不 import litellm**(邊界守門的允許清單,見 `tests/contract/test_blackbox_boundary.py`)。

- `PRICES_FILE`:價格 JSON 的檔名。
- `prices_path() -> Path`;`model_prices() -> dict`(整份表,快取)。
- `cost_per_token(model) -> (input_cost_per_token, output_cost_per_token)`(`model` 可帶 provider 前綴,
  例如 `openai/gpt-4o-mini`;先找完整名稱,再找去掉前綴的名稱)。
- `expected_cost(model, tokens_in, tokens_out) -> float`。
"""
from __future__ import annotations

import importlib.util
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["PRICES_FILE", "cost_per_token", "expected_cost", "model_prices", "prices_path"]

PRICES_FILE = "model_prices_and_context_window_backup.json"


def prices_path() -> Path:
    spec = importlib.util.find_spec("litellm")   # 只找位置,不執行套件
    if spec is None or not spec.submodule_search_locations:
        raise FileNotFoundError("找不到 litellm 套件目錄")
    return Path(list(spec.submodule_search_locations)[0]) / PRICES_FILE


@lru_cache(maxsize=1)
def model_prices() -> dict[str, Any]:
    return json.loads(prices_path().read_text(encoding="utf-8"))


def cost_per_token(model: str) -> tuple[float, float]:
    table = model_prices()
    entry = table.get(model) or table.get(model.split("/", 1)[-1])
    if entry is None:
        raise KeyError(f"價格表沒有 {model}")
    return float(entry["input_cost_per_token"]), float(entry["output_cost_per_token"])


def expected_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    cin, cout = cost_per_token(model)
    return cin * tokens_in + cout * tokens_out
