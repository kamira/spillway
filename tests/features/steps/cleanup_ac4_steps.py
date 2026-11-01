"""m2c_cleanup.feature 第 4 情境(AC-4 無 seed)的步驟(CHG-20261101-02 T10;決策 17)。

只新增「加權抽選使用未給 seed 的亂數來源」;成員、route、N 次 dry-run 與比例步驟沿用
`common_steps.py` 與 `routing_steps.py`。
"""
from __future__ import annotations

from behave import step, use_step_matcher

from spillway.clock import SystemRng

use_step_matcher("re")


@step(r"加權抽選使用未給 seed 的亂數來源")
def step_unseeded_rng(context):
    gw = context.gw
    # 換成未給 seed 的 SystemRng(),並以新的 rng 重建 app(資料庫與成員設定保留)
    gw.seed = None
    gw.rng = SystemRng()
    gw.start(gw.extra_paths)
    assert gw.app.state.rng is gw.rng
    assert isinstance(gw.rng, SystemRng)
    assert gw.seed is None
