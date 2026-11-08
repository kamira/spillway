"""m2c_cleanup.feature 第 4 情境(AC-4 無 seed)的步驟(CHG-20261101-02 T10;決策 17)。

只新增「加權抽選使用未給 seed 的亂數來源」;成員、route、N 次 dry-run 與比例步驟沿用
`common_steps.py` 與 `routing_steps.py`。
(CHG-20261107-03 T4)「未給 seed」= 不設 `SPILLWAY_TEST_RNG_SEED` 重新啟動 serve(spec 13.4.2),
以 `GET /__test__/rng` 確認。
"""
from __future__ import annotations

from behave import step, use_step_matcher

use_step_matcher("re")


@step(r"加權抽選使用未給 seed 的亂數來源")
def step_unseeded_rng(context):
    gw = context.gw
    # 不設 seed 重新啟動 serve(資料庫與成員設定保留)
    gw.seed = None
    gw.start(gw.extra_paths)
    # 新 serve 的路由抽選使用未給 seed 的亂數來源
    rng = gw.control.rng()
    assert rng["seeded"] is False, rng
    assert rng["seed"] is None, rng
