"""Route 目標比對(spec 1.2 選擇器、2.3 `routes.target`/`groups.selector`;CHG-20261030-02 T11)。

`members_of(target, deployments, groups=())` 回傳 `deployments` 中符合 `target` 的成員,保留輸入順序。

target 格式:
- `{"group": name}`:以 `groups` 中同名群組的選擇器比對;群組不存在時沒有任何成員
  (刪除仍被參照的群組由 api.admin 以 409 擋下,這裡只做防禦)。
  `groups` 可以是 `repo.Group` 的可迭代物件,或 `{name: selector}` 對照表。
- `{"tags": {"all": [...], "any": [...], "none": [...]}}`:直接以選擇器比對。

選擇器規則(缺鍵視為空清單):
- `all`:成員必須含有清單中每一個 tag(空 = 不限制)。
- `any`:成員至少含有清單中一個 tag;**空清單視為不限制**。
- `none`:成員不可含有清單中任何 tag。

不看 `enabled`、kind 等其他欄位——篩選是 `CandidateFilter` 的事。其他 target 形狀丟 ValueError。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from spillway.domain import Deployment
from spillway.store.repo import Group

GroupsArg = Mapping[str, Mapping[str, Any]] | Iterable[Group]


def _selector_map(groups: GroupsArg) -> dict[str, Mapping[str, Any]]:
    if isinstance(groups, Mapping):
        return dict(groups)
    return {g.name: g.selector for g in groups}


def matches(selector: Mapping[str, Any], tags: Iterable[str]) -> bool:
    """單一成員的 tags 是否符合選擇器 `{all, any, none}`。"""
    have = set(tags)
    all_ = set(selector.get("all") or ())
    any_ = set(selector.get("any") or ())
    none_ = set(selector.get("none") or ())
    if not all_ <= have:
        return False
    if any_ and not (any_ & have):
        return False
    return not (none_ & have)


def selector_of(target: Mapping[str, Any], groups: GroupsArg = ()) -> Mapping[str, Any] | None:
    """target → 選擇器;參照的群組不存在回 None。"""
    if "group" in target:
        return _selector_map(groups).get(target["group"])
    if "tags" in target:
        return target["tags"] or {}
    raise ValueError(f"無法辨識的 route target:{sorted(target)}")


def members_of(
    target: Mapping[str, Any],
    deployments: Sequence[Deployment],
    groups: GroupsArg = (),
) -> list[Deployment]:
    selector = selector_of(target, groups)
    if selector is None:
        return []
    return [d for d in deployments if matches(selector, d.tags)]
