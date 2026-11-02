"""設定檔模型與解析(CHG-20261102-02 T4;決策 6、11、18;spec 3;介面層)。

`parse(text) -> configio.ConfigPlan`:
1. `spillway.configio.load_yaml`(嚴格 loader;語法、anchor/alias、深度、明確 tag、重複鍵、非字串鍵、
   `version`)。錯誤 → 422 `validation_error`,訊息「路徑: 原因」經 `redact_echo`(configio 不可 import
   `spillway.api`,遮罩在本層套用)。
2. 檔案模型 `ConfigFileIn`(`extra="forbid"`),組合管理 API 的請求模型而不另寫一份:成員
   `DeploymentIn`(憑證鍵名與值樣式、`params.signals`、`credential_ref` 格式;檔案中 `id` 必填)、
   `BudgetIn`、群組 `SelectorIn`、`RouteIn`(檔案以 mapping 鍵為 route 名,另加 `targets`)、
   `OrderEntryIn` 與 `OrderIn._no_duplicates`(targets 重複成員)、settings `ContextSettingsIn`。
   另檢查:群組名稱 `group_name_problem`(決策 11;與 `POST /api/groups` 相同,另加 `_ID_PATTERN`)、
   route 名稱(`RouteIn.name` 的規則)、重複的成員 id。
   解析與模型驗證的錯誤一次回全部(上限 `MAX_ERRORS` 筆,以「; 」串接),不回顯輸入值;
   路徑格式見 `configio.format_path`(例 `deployments[1].kind`、`routes["a.b"].targets[0][1].deployment`)。

套用階段(`configio.apply`)的檢查(targets 參照、目標群組、single)回第一筆,由 `config_io.py` 轉換。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spillway import configio
from spillway.api import ApiError, redact_echo
from spillway.store import repo

from .models import (
    _ID_PATTERN,
    _ROUTE_PATTERN,
    DeploymentIn,
    OrderEntryIn,
    OrderIn,
    RouteIn,
    SelectorIn,
    group_name_problem,
)
from .settings import ContextSettingsIn

__all__ = ["MAX_ERRORS", "ConfigFileIn", "DeploymentFileIn", "RouteFileIn", "parse"]

MAX_ERRORS = 20
_NAME_MAX = 200
_ID_RE = re.compile(_ID_PATTERN)
_ROUTE_RE = re.compile(_ROUTE_PATTERN)

Slot = Annotated[list[OrderEntryIn], Field(min_length=1)]


class DeploymentFileIn(DeploymentIn):
    """檔案中的成員:同 `DeploymentIn`(全部 validator 沿用),`id` 必填。"""

    id: str = Field(min_length=1, max_length=_NAME_MAX, pattern=_ID_PATTERN)


class RouteFileIn(RouteIn):
    """檔案中的 route:同 `RouteIn`(名稱為 mapping 的鍵,值內不可再寫 `name`),另加 `targets`
    (list(slot)of list(position),格式同 `PUT /api/routes/{name}/order` 的 `slots`;未列 = 預設順序)。"""

    targets: list[Slot] | None = None

    @field_validator("targets")
    @classmethod
    def _targets_no_duplicates(cls, v: list[list[OrderEntryIn]] | None) -> list[list[OrderEntryIn]] | None:
        return v if v is None else OrderIn._no_duplicates(v)


class ConfigFileIn(BaseModel):
    """設定檔頂層(spec 3;決策 5 的格式;`version` 已由 loader 確認為 1)。`settings`、`deployments`、
    `groups`、`routes` 可省略(視同空)。"""

    model_config = ConfigDict(extra="forbid")

    version: int
    settings: ContextSettingsIn = Field(default_factory=ContextSettingsIn)
    deployments: list[DeploymentFileIn] = Field(default_factory=list)
    groups: dict[str, SelectorIn] = Field(default_factory=dict)
    routes: dict[str, RouteFileIn] = Field(default_factory=dict)


def _msg(path: str, reason: str) -> str:
    return f"{path}: {reason}" if path else reason


def _pydantic_errors(exc: ValidationError) -> list[str]:
    """Pydantic 錯誤 → 「路徑: 說明」;只用 loc 與 msg,不含 input。"""
    return [_msg(configio.format_path(e.get("loc", ())), str(e.get("msg", "invalid")))
            for e in exc.errors()]


def _name_errors(data: Mapping[str, Any]) -> list[str]:
    """群組名、route 名、route 值內的 `name`、重複的成員 id(Pydantic 模型之外的檢查)。"""
    errors: list[str] = []
    groups = data.get("groups")
    if isinstance(groups, Mapping):
        for name in groups:
            problem = group_name_problem(name)
            if problem is None and not _ID_RE.match(name):
                problem = "不可含空白與 /"
            if problem is not None:
                errors.append(_msg(configio.format_path(("groups", name)), problem))
    routes = data.get("routes")
    if isinstance(routes, Mapping):
        for name, value in routes.items():
            if not 1 <= len(name) <= _NAME_MAX:
                errors.append(_msg(configio.format_path(("routes", name)),
                                   f"名稱長度必須為 1–{_NAME_MAX} 個字元"))
            elif not _ROUTE_RE.match(name):
                errors.append(_msg(configio.format_path(("routes", name)), "名稱不可含空白、/ 與 :"))
            if isinstance(value, Mapping) and "name" in value:
                errors.append(_msg(configio.format_path(("routes", name, "name")),
                                   "Extra inputs are not permitted"))
    deployments = data.get("deployments")
    if isinstance(deployments, Sequence) and not isinstance(deployments, str):
        seen: set[str] = set()
        for i, dep in enumerate(deployments):
            dep_id = dep.get("id") if isinstance(dep, Mapping) else None
            if isinstance(dep_id, str):
                if dep_id in seen:
                    errors.append(_msg(configio.format_path(("deployments", i, "id")),
                                       f"重複的成員 id:{dep_id}"))
                seen.add(dep_id)
    return errors


def _invalid(errors: list[str]) -> ApiError:
    return ApiError(422, "validation_error", redact_echo("; ".join(errors[:MAX_ERRORS])))


def _plan(model: ConfigFileIn) -> configio.ConfigPlan:
    routes = []
    for name, r in model.routes.items():
        targets = None
        if r.targets is not None:
            targets = tuple(
                repo.RouteTarget(deployment_id=e.deployment, slot=i + 1, position=j,
                                 effort=e.effort, weight=e.weight)
                for i, slot in enumerate(r.targets) for j, e in enumerate(slot))
        routes.append((r.to_route(name), targets))
    return configio.ConfigPlan(
        deployments=tuple(d.to_deployment(d.id) for d in model.deployments),
        groups=tuple(repo.Group(name, sel.model_dump()) for name, sel in model.groups.items()),
        routes=tuple(routes),
        settings=model.settings.model_dump(exclude_none=True),
    )


def parse(text: str) -> configio.ConfigPlan:
    """設定檔文字 → `ConfigPlan`;任何解析或驗證錯誤丟 `ApiError(422, "validation_error")`。"""
    try:
        data = configio.load_yaml(text)
    except configio.ConfigSyntaxError as exc:
        raise _invalid([_msg(exc.path, exc.reason)]) from None
    errors = _name_errors(data)
    model: ConfigFileIn | None = None
    try:
        model = ConfigFileIn.model_validate(data)
    except ValidationError as exc:
        errors = _pydantic_errors(exc) + errors
    if errors:
        raise _invalid(errors)
    assert model is not None
    return _plan(model)
