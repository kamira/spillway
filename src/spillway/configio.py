"""設定檔格式與匯入匯出(spec 3;CHG-20261102-02 決策 5、8、18;應用層)。

依賴只有 `spillway.store`、`spillway.domain`、`spillway.clock`、`yaml` 與標準庫;**不可** import
`spillway.api*`(L1 測試:`import spillway.configio` 之後 `"spillway.api" not in sys.modules`)。
SQL 一律經 `store.repo`。HTTP 轉換、請求與檔案模型驗證屬介面層(`api/admin/config_io.py`、
`api/admin/config_file.py`)。

本檔目前提供匯出(T3):`dump(conn) -> str`、格式常數與 `_Dumper`。匯入(T4:`load_yaml`、`ConfigPlan`、
`apply`、`ImportResult`、`ImportConflict`、`ConfigSyntaxError`)沿用下方的鍵名與預設值常數,
省略規則與匯出共用同一份定義(往返的前提,決策 8)。

匯出格式(決策 5,逐欄定死;改動須經修改治理,黃金檔 `tests/unit/data/config_golden.yaml` 釘住):
- 頂層 `TOP_KEYS`:`version`(= `FORMAT_VERSION`)、`settings`、`deployments`、`groups`、`routes`
  一律輸出,空值為 `{}`/`[]`。
- `settings`:mapping;只輸出值與 `SETTING_DEFAULTS` 不同的鍵,鍵順序 `SETTING_KEYS`;
  型別 `SETTING_TYPES`(`record_context_default` 為 bool,其餘 int)。存取 token 雜湊等其他鍵不輸出。
- `deployments`:list,依 id 排序;欄位順序 `DEPLOYMENT_KEYS`;省略 `params == {}`、
  `credential_ref is None`、`capabilities == {}`、`enabled is True`、`tags == []`、沒有 budget 列。
  `params`、`capabilities` 為不透明 JSON,照資料庫原樣輸出(不遞迴剝除)。tags 依存放排序。
  `budget` 子鍵 `BUDGET_KEYS`;`limit_amount`、`reset_anchor` 為 None 時省略;`type: none`、
  `window: none` 不省略。
- `groups`:mapping(名稱 → selector),依名稱排序;selector 子鍵 `SELECTOR_KEYS`,空清單省略。
- `routes`:mapping(名稱 → route),依名稱排序;欄位順序 `ROUTE_KEYS`;省略 `mode == DEFAULT_MODE`、
  `tags == []`、`record_context is None`、`budget_precheck is False`、沒有手動排序;`record_context: false`
  不省略。`target` 為 `{group: <name>}` 或 `{tags: <selector>}`(selector 同 groups;全空時 `{tags: {}}`)。
  `targets` 為 list(slot)of list(position),項目鍵 `TARGET_ENTRY_KEYS`;`weight == DEFAULT_WEIGHT` 省略、
  `effort is None` 省略(`effort: none` 不省略);slot 依資料庫的 slot 遞增輸出,空洞重新編號。
  route tags 保留原順序與重複值。
- 不含:存取 token 雜湊、冷卻、帳本、payload、埠號。
- 序列化 `DUMP_KWARGS`(`sort_keys=False, allow_unicode=True, default_flow_style=False,
  width=inf`)經 `_Dumper`;行尾 `\\n`。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

import yaml

from spillway.domain import Budget, Deployment
from spillway.store import repo

__all__ = [
    "FORMAT_VERSION", "TOP_KEYS", "SETTING_KEYS", "SETTING_TYPES", "DEPLOYMENT_KEYS", "BUDGET_KEYS",
    "SELECTOR_KEYS", "ROUTE_KEYS", "TARGET_ENTRY_KEYS", "DEFAULT_MODE", "DEFAULT_WEIGHT",
    "DUMP_KWARGS", "dump", "render", "build_document", "setting_value",
    "deployment_doc", "budget_doc", "selector_doc", "target_doc", "route_doc", "targets_doc",
    "settings_doc",
]

FORMAT_VERSION = 1

TOP_KEYS = ("version", "settings", "deployments", "groups", "routes")
SETTING_KEYS: tuple[str, ...] = tuple(repo.SETTING_DEFAULTS)
SETTING_TYPES: dict[str, type] = {
    "record_context_default": bool,
    "context_retention_days": int,
    "context_max_bytes": int,
}
DEPLOYMENT_KEYS = ("id", "model_name", "kind", "provider", "model", "params", "credential_ref",
                   "capabilities", "enabled", "tags", "budget")
BUDGET_KEYS = ("type", "limit_amount", "window", "reset_anchor")
SELECTOR_KEYS = ("all", "any", "none")
ROUTE_KEYS = ("target", "mode", "tags", "record_context", "budget_precheck", "targets")
TARGET_ENTRY_KEYS = ("deployment", "effort", "weight")
DEFAULT_MODE = "chat"
DEFAULT_WEIGHT = "medium"

DUMP_KWARGS: dict[str, Any] = {
    "sort_keys": False,
    "allow_unicode": True,
    "default_flow_style": False,
    "width": float("inf"),
}

assert set(SETTING_TYPES) == set(SETTING_KEYS), "SETTING_TYPES 必須涵蓋 SETTING_DEFAULTS 的每個鍵"


class _Dumper(yaml.SafeDumper):
    """匯出用 Dumper:
    - 不產生 anchor/alias(同一物件出現兩次也展開;匯入的嚴格 loader 拒絕 alias,決策 18);
    - list 在 mapping 下縮排(`key:\\n  - item`),只影響排版。
    字串若會被 YAML 1.1 解析成 bool、數字、日期或 null,SafeDumper 自動加引號,匯入時仍讀回字串。"""

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


# ---------------------------------------------------------------- 各實體 → 文件片段(純函式)


def setting_value(key: str, text: str) -> bool | int:
    """settings 表的字串值 → 檔案中的型別(`"1"`/`"0"` → bool;其餘 int),與
    `repo.get_context_settings` 的讀法一致。"""
    if SETTING_TYPES[key] is bool:
        return text == "1"
    return int(text)


def settings_doc(stored: Mapping[str, str]) -> dict[str, bool | int]:
    """`stored`:鍵 → settings 表字串值(缺鍵視為預設)。只回與預設不同的鍵,順序 `SETTING_KEYS`。"""
    out: dict[str, bool | int] = {}
    for key in SETTING_KEYS:
        default = setting_value(key, repo.SETTING_DEFAULTS[key])
        raw = stored.get(key)
        value = default if raw is None else setting_value(key, raw)
        if value != default:
            out[key] = value
    return out


def budget_doc(b: Budget) -> dict[str, Any]:
    out: dict[str, Any] = {"type": b.type}
    if b.limit_amount is not None:
        out["limit_amount"] = b.limit_amount
    out["window"] = b.window
    if b.reset_anchor is not None:
        out["reset_anchor"] = b.reset_anchor
    return out


def deployment_doc(dep: Deployment) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": dep.id,
        "model_name": dep.model_name,
        "kind": dep.kind,
        "provider": dep.provider,
        "model": dep.model,
    }
    if dep.params:
        out["params"] = dep.params
    if dep.credential_ref is not None:
        out["credential_ref"] = dep.credential_ref
    if dep.capabilities:
        out["capabilities"] = dep.capabilities
    if dep.enabled is not True:
        out["enabled"] = bool(dep.enabled)
    if dep.tags:
        out["tags"] = list(dep.tags)
    if dep.budget is not None:
        out["budget"] = budget_doc(dep.budget)
    return out


def selector_doc(selector: Mapping[str, Any]) -> dict[str, list[str]]:
    """`{all, any, none}`,空清單(或缺鍵)省略;清單保留存放順序。"""
    return {k: list(selector[k]) for k in SELECTOR_KEYS if selector.get(k)}


def target_doc(target: Mapping[str, Any]) -> dict[str, Any]:
    if "group" in target:
        return {"group": target["group"]}
    if "tags" in target:
        return {"tags": selector_doc(target["tags"] or {})}
    return dict(target)   # 管理 API 不會寫入其他形狀;照原樣輸出,匯入時由檔案模型拒絕


def targets_doc(rows: Iterable[repo.RouteTarget]) -> list[list[dict[str, Any]]]:
    """route_targets → list(slot)of list(position)。`rows` 須已依 (slot, position, deployment_id)
    排序;slot 依遞增分組,空洞(例如 1、3)輸出時自然重新編號。"""
    slots: list[list[dict[str, Any]]] = []
    current: int | None = None
    for t in rows:
        if current is None or t.slot != current:
            slots.append([])
            current = t.slot
        entry: dict[str, Any] = {"deployment": t.deployment_id}
        if t.effort is not None:
            entry["effort"] = t.effort
        if t.weight != DEFAULT_WEIGHT:
            entry["weight"] = t.weight
        slots[-1].append(entry)
    return slots


def route_doc(route: repo.Route, targets: Iterable[repo.RouteTarget] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"target": target_doc(route.target)}
    if route.mode != DEFAULT_MODE:
        out["mode"] = route.mode
    if route.tags:
        out["tags"] = list(route.tags)
    if route.record_context is not None:
        out["record_context"] = bool(route.record_context)
    if route.budget_precheck:
        out["budget_precheck"] = True
    slots = targets_doc(targets)
    if slots:
        out["targets"] = slots
    return out


def build_document(
    *,
    settings: Mapping[str, str],
    deployments: Iterable[Deployment],
    groups: Iterable[repo.Group],
    routes: Iterable[repo.Route],
    route_targets: Mapping[str, Iterable[repo.RouteTarget]],
) -> dict[str, Any]:
    """組出匯出文件(Python 物件,鍵順序即輸出順序)。排序在此處決定,與輸入順序無關。"""
    return {
        "version": FORMAT_VERSION,
        "settings": settings_doc(settings),
        "deployments": [deployment_doc(d) for d in sorted(deployments, key=lambda d: d.id)],
        "groups": {g.name: selector_doc(g.selector) for g in sorted(groups, key=lambda g: g.name)},
        "routes": {
            r.name: route_doc(r, route_targets.get(r.name, ()))
            for r in sorted(routes, key=lambda r: r.name)
        },
    }


def render(document: Mapping[str, Any]) -> str:
    """文件 → YAML 文字(`_Dumper` + `DUMP_KWARGS`),行尾 `\\n`。"""
    text = yaml.dump(dict(document), Dumper=_Dumper, **DUMP_KWARGS)
    return text if text.endswith("\n") else text + "\n"


def dump(conn: sqlite3.Connection) -> str:
    """目前設定 → YAML 文字(決策 5)。呼叫端已在交易內(例如匯入時的寫鎖)時沿用該交易;
    否則自行在單一讀交易內讀取(`repo.read_snapshot`),各表看到同一份快照。不掃描憑證鍵名
    (匯出端點另行掃描;備份不掃描,決策 5)。"""
    with repo.read_snapshot(conn):
        stored = {}
        for key in SETTING_KEYS:
            row = repo.get_setting(conn, key)
            if row is not None:
                stored[key] = row
        document = build_document(
            settings=stored,
            deployments=repo.list_deployments(conn),
            groups=repo.list_groups(conn),
            routes=repo.list_routes(conn),
            route_targets=repo.list_all_route_targets(conn),
        )
    return render(document)
