"""設定檔格式與匯入匯出(spec 3;CHG-20261102-02 決策 5、8、18;應用層)。

依賴只有 `spillway.store`、`spillway.domain`、`spillway.clock`、`yaml` 與標準庫;**不可** import
`spillway.api*`(L1 測試:`import spillway.configio` 之後 `"spillway.api" not in sys.modules`)。
SQL 一律經 `store.repo`。HTTP 轉換、請求與檔案模型驗證屬介面層(`api/admin/config_http.py`、
`api/admin/config_file.py`)。

匯出(T3):`dump(conn) -> str`、格式常數與 `_Dumper`。匯入(T4):`load_yaml`(嚴格 loader)、
`ConfigPlan`、`apply`、`ImportResult`、`ImportConflict`、`ConfigSyntaxError`、`format_path`;
沿用下方的鍵名與預設值常數,省略規則與匯出共用同一份定義(往返的前提,決策 8)。

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

import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from spillway.clock import utc_strftime
from spillway.domain import Budget, Deployment
from spillway.store import repo
from spillway.store.db import transaction

__all__ = [
    "FORMAT_VERSION", "TOP_KEYS", "SETTING_KEYS", "SETTING_TYPES", "DEPLOYMENT_KEYS", "BUDGET_KEYS",
    "SELECTOR_KEYS", "ROUTE_KEYS", "TARGET_ENTRY_KEYS", "DEFAULT_MODE", "DEFAULT_WEIGHT",
    "DUMP_KWARGS", "dump", "render", "build_document", "utc_stamp_filename",
]
# 匯入(T4)的名稱(`load_yaml`、`ConfigSyntaxError`、`ConfigPlan`、`apply`、`ImportResult`、
# `ImportConflict`、`format_path`、`privileged_view`、`is_privileged_change`、`MAX_DEPTH`、
# `BACKUP_FILENAME_FORMAT`)以 `configio.<名稱>` 取用;
# `__all__` 維持匯出部分(`tests/unit/test_configio_dump.py::test_public_surface` 釘住)。

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


class _Dumper(yaml.SafeDumper):
    """匯出用 Dumper:
    - 不產生 anchor/alias(同一物件出現兩次也展開;匯入的嚴格 loader 拒絕 alias,決策 18);
    - list 在 mapping 下縮排(`key:\\n  - item`),只影響排版。
    - 含 U+0085、U+2028、U+2029 的字串一律以雙引號輸出並跳脫(`\\N`、`\\L`、`\\P`):單引號或
      plain 風格會把它們原樣寫出,讀回時被當成換行折疊(U+0085 實測讀回不同)。
    字串若會被 YAML 1.1 解析成 bool、數字、日期或 null,SafeDumper 自動加引號,匯入時仍讀回字串。"""

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


_LINE_BREAK_CHARS = ("\x85", "\u2028", "\u2029")


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if any(ch in data for ch in _LINE_BREAK_CHARS):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_str(data)


_Dumper.add_representer(str, _represent_str)


# `now` 轉 UTC 後套 `fmt`;匯出檔名(介面層)與匯入備份檔名(T4)共用。定義在 `spillway.clock`,
# 此處為別名(CHG-20261103-01 決策 21 ⑤;`__all__` 不變)
utc_stamp_filename = utc_strftime


# ---------------------------------------------------------------- 各實體 → 文件片段(純函式)


def _setting_value(key: str, text: str) -> bool | int:
    """settings 表的字串值 → 檔案中的型別(`"1"`/`"0"` → bool;其餘 int),與
    `repo.get_context_settings` 的讀法一致。"""
    if SETTING_TYPES[key] is bool:
        return text == "1"
    return int(text)


def _settings_doc(stored: Mapping[str, str]) -> dict[str, bool | int]:
    """`stored`:鍵 → settings 表字串值(缺鍵視為預設)。只回與預設不同的鍵,順序 `SETTING_KEYS`。"""
    out: dict[str, bool | int] = {}
    for key in SETTING_KEYS:
        default = _setting_value(key, repo.SETTING_DEFAULTS[key])
        raw = stored.get(key)
        value = default if raw is None else _setting_value(key, raw)
        if value != default:
            out[key] = value
    return out


def _budget_doc(b: Budget) -> dict[str, Any]:
    out: dict[str, Any] = {"type": b.type}
    if b.limit_amount is not None:
        out["limit_amount"] = b.limit_amount
    out["window"] = b.window
    if b.reset_anchor is not None:
        out["reset_anchor"] = b.reset_anchor
    return out


def _deployment_doc(dep: Deployment) -> dict[str, Any]:
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
        out["budget"] = _budget_doc(dep.budget)
    return out


def _selector_doc(selector: Mapping[str, Any]) -> dict[str, list[str]]:
    """`{all, any, none}`,空清單(或缺鍵)省略;清單保留存放順序。"""
    return {k: list(selector[k]) for k in SELECTOR_KEYS if selector.get(k)}


def _target_doc(target: Mapping[str, Any]) -> dict[str, Any]:
    if "group" in target:
        return {"group": target["group"]}
    if "tags" in target:
        return {"tags": _selector_doc(target["tags"] or {})}
    return dict(target)   # 管理 API 不會寫入其他形狀;照原樣輸出,匯入時由檔案模型拒絕


def _targets_doc(rows: Iterable[repo.RouteTarget]) -> list[list[dict[str, Any]]]:
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


def _route_doc(route: repo.Route, targets: Iterable[repo.RouteTarget] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"target": _target_doc(route.target)}
    if route.mode != DEFAULT_MODE:
        out["mode"] = route.mode
    if route.tags:
        out["tags"] = list(route.tags)
    if route.record_context is not None:
        out["record_context"] = bool(route.record_context)
    if route.budget_precheck:
        out["budget_precheck"] = True
    slots = _targets_doc(targets)
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
        "settings": _settings_doc(settings),
        "deployments": [_deployment_doc(d) for d in sorted(deployments, key=lambda d: d.id)],
        "groups": {g.name: _selector_doc(g.selector) for g in sorted(groups, key=lambda g: g.name)},
        "routes": {
            r.name: _route_doc(r, route_targets.get(r.name, ()))
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


# ================================================================ 匯入(T4;決策 6、7、18、20)

MAX_DEPTH = 32                      # 巢狀深度上限(mapping/sequence 層數,決策 18)
BACKUP_FILENAME_FORMAT = "config-%Y%m%dT%H%M%SZ.yaml"
_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
# 與群組名稱規則(`api/admin/models.py` 的 `_BAD_NAME_CATEGORIES`,決策 11)拒絕的類別相同;本模組不可
# import `spillway.api`,故另列一份。路徑中這些字元一律以 `\uXXXX` 顯示,CJK 等可見字元照原樣。
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


def _escape_invisible(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch) not in _ESCAPED_CATEGORIES:
            out.append(ch)
            continue
        cp = ord(ch)
        if cp > 0xFFFF:                 # 與 JSON 相同,以 UTF-16 代理對表示
            cp -= 0x10000
            out.append(f"\\u{0xD800 + (cp >> 10):04x}\\u{0xDC00 + (cp & 0x3FF):04x}")
        else:
            out.append(f"\\u{cp:04x}")
    return "".join(out)


def format_path(parts: Iterable[str | int]) -> str:
    """欄位路徑(決策 6):list 索引為 0 起算 `[i]`;鍵符合 `^[A-Za-z0-9_-]+$` 時為 `.key`(開頭不加點),
    否則為 `["..."]`(JSON 跳脫;另將 Unicode 類別 Cc、Cf、Cs、Co、Cn、Zl、Zp、Zs 的字元(含空白、
    零寬字元、方向控制字元)寫成 `\\uXXXX`,CJK 等可見字元照原樣)。例:`deployments[1].kind`、
    `routes["a.b"].targets[0][1].deployment`、`groups["a\\u200bb"]`。
    回顯的鍵名由介面層另經 `redact_echo` 遮罩(本模組不 import `spillway.api`)。"""
    out = ""
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            out += f"[{part}]"
        elif _PLAIN_KEY.fullmatch(str(part)):
            out += f".{part}" if out else str(part)
        else:
            out += "[" + _escape_invisible(json.dumps(str(part), ensure_ascii=False)) + "]"
    return out


class ConfigSyntaxError(ValueError):
    """YAML 層的錯誤(語法、anchor/alias、深度、明確 tag、重複鍵、非字串鍵、頂層形狀、version)。
    `path`:欄位路徑(`format_path`),語法類錯誤為 `yaml`;`reason`:說明,只含行列與問題描述,
    不含原始行片段(決策 18)。"""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _scrub_problem(problem: Any) -> str:
    """PyYAML 的 `problem` 文字會以引號帶出輸入片段(例:未定義的 tag handle `'!AIza…!'`、重複的
    `%TAG` handle、未知的跳脫字元);引號內一律換成 `'…'`,輸入永不回顯(決策 18)。"""
    return _QUOTED.sub("'…'", str(problem or "語法錯誤"))


def _mark_reason(mark: Any, problem: str) -> str:
    if mark is None:
        return problem
    return f"line {mark.line + 1} column {mark.column + 1}: {problem}"


class _StrictLoader(yaml.SafeLoader):
    """嚴格 loader(決策 18):拒絕 anchor/alias 與任何明確 tag;巢狀深度上限 `MAX_DEPTH`;
    不解析 timestamp(日期當字串)與 merge key(`<<` 當一般字串鍵);bool 只認 true/false(不分大小寫),
    `on`、`yes` 等保持字串。重複鍵與非字串鍵在 `_to_python` 檢查。"""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ConfigSyntaxError("yaml", _mark_reason(event.start_mark, "不支援 anchor/alias"))
        if getattr(event, "tag", None) is not None:
            raise ConfigSyntaxError("yaml", _mark_reason(event.start_mark, "不支援明確 tag"))
        nested = isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent))
        if nested:
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ConfigSyntaxError(
                    "yaml", _mark_reason(event.start_mark, f"巢狀深度超過 {MAX_DEPTH} 層"))
        try:
            return super().compose_node(parent, index)
        finally:
            if nested:
                self._depth -= 1


_BOOL_TAG = "tag:yaml.org,2002:bool"
_DROPPED_TAGS = {_BOOL_TAG, "tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:merge"}
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in _DROPPED_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:[Tt][Rr][Uu][Ee]|[Ff][Aa][Ll][Ss][Ee])$"), list("tTfF"))

_STR_TAG = "tag:yaml.org,2002:str"
_SURROGATE = re.compile("[\ud800-\udfff]")


def _to_python(loader: _StrictLoader, node: yaml.Node, path: tuple[str | int, ...]) -> Any:
    """節點樹 → Python 物件,沿途帶欄位路徑;深度已由 composer 限制。"""
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_object(node)
        if isinstance(value, str) and _SURROGATE.search(value):
            # 雙引號跳脫 `"\ud800"` 可產生孤立代理字元;無法以 UTF-8 寫入資料庫或匯出
            raise ConfigSyntaxError(format_path(path) or "yaml", "不可含孤立的代理字元")
        return value
    if isinstance(node, yaml.SequenceNode):
        return [_to_python(loader, child, (*path, i)) for i, child in enumerate(node.value)]
    if isinstance(node, yaml.MappingNode):
        out: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != _STR_TAG:
                raise ConfigSyntaxError(format_path(path) or "yaml",
                                        _mark_reason(key_node.start_mark, "鍵必須是字串"))
            key = loader.construct_object(key_node)
            if _SURROGATE.search(key):
                raise ConfigSyntaxError(format_path(path) or "yaml", "鍵不可含孤立的代理字元")
            if key in out:
                raise ConfigSyntaxError(format_path((*path, key)), "重複的鍵")
            out[key] = _to_python(loader, value_node, (*path, key))
        return out
    raise ConfigSyntaxError(format_path(path) or "yaml", "不支援的節點")


def load_yaml(text: str) -> Any:
    """嚴格解析設定檔(決策 18)。回傳頂層 mapping(已確認 `version == 1`);任何錯誤丟
    `ConfigSyntaxError`,訊息只含行列與問題描述(不用 `str(exc)`、不取原始行片段)。"""
    loader: _StrictLoader | None = None
    try:
        loader = _StrictLoader(text)    # Reader 在建構時就檢查不可列印字元(ReaderError)
        node = loader.get_single_node()
        data = None if node is None else _to_python(loader, node, ())
    except ConfigSyntaxError:
        raise
    except yaml.MarkedYAMLError as exc:
        raise ConfigSyntaxError(
            "yaml", _mark_reason(exc.problem_mark, _scrub_problem(exc.problem))) from None
    except yaml.reader.ReaderError as exc:
        raise ConfigSyntaxError("yaml", f"位置 {exc.position}: 不允許的字元") from None
    except yaml.YAMLError:
        raise ConfigSyntaxError("yaml", "語法錯誤") from None
    except RecursionError:
        raise ConfigSyntaxError("yaml", "巢狀過深") from None
    except (ValueError, OverflowError) as exc:
        # 純量建構失敗(例:超過 int 位數上限 4300 的整數)。ConfigSyntaxError 本身是 ValueError,
        # 已在上方原樣丟出;這裡不用 str(exc)(可能帶出輸入片段)
        raise ConfigSyntaxError("yaml", f"無法解析的值({type(exc).__name__})") from None
    finally:
        if loader is not None:
            loader.dispose()
    if not isinstance(data, dict):
        raise ConfigSyntaxError("yaml", "頂層必須是 mapping")
    version = data.get("version")
    if type(version) is not int or version != FORMAT_VERSION:
        raise ConfigSyntaxError("version", f"必須為 {FORMAT_VERSION}")
    return data


@dataclass(frozen=True)
class ConfigPlan:
    """已驗證的設定檔(介面層 `config_file.parse` 產生)。`routes` 每項為 `(route, targets)`,
    `targets` 為 None 表示檔案沒列 `targets`(回到預設順序;與空 tuple 效果相同)。
    `settings` 只含檔案中出現的鍵(已轉型)。"""

    deployments: tuple[Deployment, ...] = ()
    groups: tuple[repo.Group, ...] = ()
    routes: tuple[tuple[repo.Route, tuple[repo.RouteTarget, ...] | None], ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)


def _counts(deployments: int = 0, groups: int = 0, routes: int = 0) -> dict[str, int]:
    return {"deployments": deployments, "groups": groups, "routes": routes}


@dataclass
class ImportResult:
    """`created`/`updated`/`deleted` 各為 `{deployments, groups, routes}` 計數;`backup` 為備份檔名
    (merge 與 dry_run 為 None);`affected_ids` = 交易前 ∪ 交易後的成員 id(呼叫端據此逐一讓健康快取
    失效);`privileged` 見 `is_privileged_change`(依 id 排序,只含 id)。"""

    mode: str
    created: dict[str, int]
    updated: dict[str, int]
    deleted: dict[str, int]
    backup: str | None
    affected_ids: frozenset[str]
    privileged: tuple[str, ...]
    dry_run: bool


RESTORE_PRECHECK_PREFIX = "匯入前的設定不符現行規則,replace 的備份將無法還原;請先以管理 API 修正:"
_STATUS_OF_CODE = {"provider_single": 409, "validation_error": 422, "internal_error": 500}


class ImportConflict(Exception):
    """套用階段的失敗(交易已回滾):`validation_error`(422;targets 參照不存在的成員、route 目標群組
    不存在)、`provider_single`(409)、`internal_error`(500;備份失敗)。`path` 為欄位路徑(可為空),
    `message` 可能含使用者輸入的 id 或名稱,介面層回應前須經 `redact_echo`。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.code = code
        self.path = path
        self.message = message

    @property
    def status(self) -> int:
        return _STATUS_OF_CODE.get(self.code, 422)


# ---------------------------------------------------------------- privileged 欄位(決策 20)

_URL_KEYS = frozenset({"api_base", "base_url", "url", "endpoint"})
_URL_SUFFIXES = ("_url", "_base", "_endpoint")
_CLI_PRIVILEGED_PARAMS = ("extra_args", "effort_args", "cwd")


def _is_url_key(key: str) -> bool:
    k = key.lower()
    return k in _URL_KEYS or k.endswith(_URL_SUFFIXES)


def privileged_view(dep: Deployment | None) -> tuple[Any, ...]:
    """成員的 privileged 欄位投影(決策 20):`credential_ref`、URL 類 `params` 頂層鍵(`api_base`、
    `base_url`、`url`、`endpoint` 或以 `_url`/`_base`/`_endpoint` 結尾)、CLI 成員的 `params.extra_args`、
    `effort_args`、`cwd`。`params.timeout` 等其他鍵不列入。不存在的成員視同全部未設定。"""
    if dep is None:
        return (None, {}, {})
    params = dep.params or {}
    urls = {k: params[k] for k in params if _is_url_key(str(k))}
    cli = {k: params[k] for k in _CLI_PRIVILEGED_PARAMS if k in params} if dep.kind == "cli" else {}
    return (dep.credential_ref, urls, cli)


def is_privileged_change(before: Deployment | None, after: Deployment) -> bool:
    """匯入前後的成員是否變更了 privileged 欄位:`privileged_view` 不同,或**既有**成員的 `provider`
    改變(改變憑證送往何處;CHG-20261103-01 決策 10 ⑥,M4a 帶入)。新成員只看 `privileged_view`。"""
    if privileged_view(after) != privileged_view(before):
        return True
    return before is not None and before.provider != after.provider


# ---------------------------------------------------------------- 備份(決策 7)


def _write_backup(backup_dir: Path, now: datetime, text: str) -> str:
    """寫入 `backup_dir/config-<UTC>.yaml`(0600、O_EXCL|O_NOFOLLOW、fsync;同名已存在時加 `-<n>`,n 從 2
    遞增,一律不覆寫)。`backup_dir` 以 0700 建立;是 symlink 或不是目錄 → `ImportConflict(internal_error)`。
    回傳檔名(不含目錄)。"""
    try:
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        st = backup_dir.lstat()
    except OSError as exc:
        raise ImportConflict("internal_error", "", f"無法建立備份目錄({type(exc).__name__})") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ImportConflict("internal_error", "", "備份目錄不是一般目錄(可能是 symlink),未匯入")
    base = utc_stamp_filename(now, BACKUP_FILENAME_FORMAT)
    stem = base[: -len(".yaml")]
    data = text.encode("utf-8")
    n = 1
    while True:
        name = base if n == 1 else f"{stem}-{n}.yaml"
        path = backup_dir / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            n += 1
            continue
        except OSError as exc:
            raise ImportConflict("internal_error", "", f"無法寫入備份({type(exc).__name__})") from None
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            _unlink_quiet(path)
            raise ImportConflict("internal_error", "", f"無法寫入備份({type(exc).__name__})") from None
        os.close(fd)
        return name


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 套用(決策 7、20)


class _DryRunRollback(Exception):
    """dry_run:套用與檢查都做完後丟出,讓 `transaction` 回滾。"""

    def __init__(self, result: ImportResult) -> None:
        super().__init__("dry_run")
        self.result = result


def _route_path(name: str, *rest: str | int) -> str:
    return format_path(("routes", name, *rest))


def apply(
    conn: sqlite3.Connection,
    plan: ConfigPlan,
    mode: Literal["merge", "replace"],
    *,
    cardinality_of: Callable[[str, str], str | None],
    backup_dir: Path,
    now: datetime,
    dry_run: bool = False,
    check_restorable: Callable[[Sequence[Deployment], str], str | None] | None = None,
) -> ImportResult:
    """在單一 `transaction(conn)`(BEGIN IMMEDIATE)內套用設定檔(決策 7):

    1. targets 內每個成員必須存在於「檔案 ∪(merge 時)資料庫」,否則 `ImportConflict(validation_error)`
       (寫入 route_targets 之前檢查,不撞外鍵);
    2. 只限 replace(終審 U2):以 `check_restorable(匯入前的成員, 匯入前的 dump)` 檢查目前設定能否通過
       匯出與匯入的規則(由介面層注入:決策 5 的憑證鍵名掃描與 `config_file.parse`;本模組不 import
       `spillway.api`);回傳問題說明(只含位置與原因,不含值)→ `ImportConflict(validation_error)`,
       未刪除、未寫備份;`dry_run` 同樣檢查。之後只限非 dry_run:把同一份 dump 寫成備份(寫鎖內、刪除之前);
    3. UPSERT 檔案內的成員、群組、route(`save_*`),每條 route 以 `replace_route_targets` 取代排序;
       settings 只覆寫檔案中出現的鍵;
    4. 只限 replace:刪除檔案外的 routes → groups → deployments(其 tags、budget、冷卻、route_targets
       由外鍵串聯刪除),並刪除檔案中沒有的 `SETTING_DEFAULTS` 鍵(回到預設);其他 settings 鍵、帳本、
       payload 不動;
    5. 最終狀態檢查:route `target.group` 存在(422);`cardinality_of(provider, kind) == "single"` 的 provider
       至多 1 個成員(409 `provider_single`);不檢查成員是否屬於 route 目標(stale targets 由 Router 過濾)。
    任一步失敗 → ROLLBACK(本次寫的備份檔一併移除)並丟 `ImportConflict`。`dry_run=True` 時做完 1、2 的檢查、3–5
    後 ROLLBACK,回傳同樣的計數與 `privileged`(`backup` 為 None)。
    """
    if mode not in ("merge", "replace"):
        raise ValueError(f"mode 必須為 merge 或 replace:{mode!r}")
    replace = mode == "replace"
    written: list[Path] = []
    try:
        with transaction(conn):
            result = _apply_tx(conn, plan, replace, cardinality_of=cardinality_of,
                               backup_dir=backup_dir, now=now, dry_run=dry_run, written=written,
                               check_restorable=check_restorable)
            if dry_run:
                raise _DryRunRollback(result)
    except _DryRunRollback as rb:
        return rb.result
    except BaseException:
        for path in written:
            _unlink_quiet(path)
        raise
    return result


def _apply_tx(
    conn: sqlite3.Connection,
    plan: ConfigPlan,
    replace: bool,
    *,
    cardinality_of: Callable[[str, str], str | None],
    backup_dir: Path,
    now: datetime,
    dry_run: bool,
    written: list[Path],
    check_restorable: Callable[[Sequence[Deployment], str], str | None] | None,
) -> ImportResult:
    before_deps = {d.id: d for d in repo.list_deployments(conn)}
    before_groups = {g.name for g in repo.list_groups(conn)}
    before_routes = {r.name for r in repo.list_routes(conn)}
    file_deps = {d.id for d in plan.deployments}
    file_groups = {g.name for g in plan.groups}
    file_routes = {r.name for r, _ in plan.routes}

    # 1. targets 參照的成員必須存在(寫入 route_targets 之前)
    known = file_deps if replace else file_deps | set(before_deps)
    for route, targets in plan.routes:
        for t in targets or ():
            if t.deployment_id not in known:
                raise ImportConflict(
                    "validation_error",
                    _route_path(route.name, "targets", t.slot - 1, t.position, "deployment"),
                    f"成員 {t.deployment_id} 不存在")

    # 2. replace:匯入前設定須可還原(終審 U2;dry_run 同樣檢查),再寫備份(非 dry_run;寫鎖內、刪除之前)
    backup: str | None = None
    if replace:
        current = dump(conn)
        if check_restorable is not None:
            problem = check_restorable(tuple(before_deps.values()), current)
            if problem:
                raise ImportConflict("validation_error", "", RESTORE_PRECHECK_PREFIX + problem)
        if not dry_run:
            backup = _write_backup(backup_dir, now, current)
            written.append(backup_dir / backup)

    # 3. UPSERT
    for dep in plan.deployments:
        repo.save_deployment(conn, dep)
    for group in plan.groups:
        repo.save_group(conn, group)
    for route, targets in plan.routes:
        repo.save_route(conn, route)
        repo.replace_route_targets(conn, route.name, list(targets or ()))
    for key, value in plan.settings.items():
        repo.set_setting(conn, key, value)

    # 4. replace:刪除檔案外的實體與 context 設定鍵
    deleted = _counts()
    if replace:
        gone_routes = sorted(before_routes - file_routes)
        gone_groups = sorted(before_groups - file_groups)
        gone_deps = sorted(set(before_deps) - file_deps)
        for name in gone_routes:
            repo.delete_route(conn, name)
        for name in gone_groups:
            repo.delete_group(conn, name)
        for dep_id in gone_deps:
            repo.delete_deployment(conn, dep_id)
        repo.delete_settings(conn, [k for k in repo.SETTING_DEFAULTS if k not in plan.settings])
        deleted = _counts(len(gone_deps), len(gone_groups), len(gone_routes))

    # 5. 最終狀態檢查。single provider 的檢查看「交易內的最終狀態」(整批匯入後才算數);單筆管理 API
    #    則在 `api/admin/deployments._check_cardinality` 逐列檢查寫入前的狀態——兩者刻意分開(終審 I3)。
    for route in repo.list_routes(conn):
        group = route.target.get("group")
        if group is not None and repo.get_group(conn, group) is None:
            raise ImportConflict("validation_error", _route_path(route.name, "target", "group"),
                                 f"群組 {group} 不存在")
    single = sorted({p for k, p in repo.list_kinds_providers(conn)
                     if cardinality_of(p, k) == "single"})
    by_provider = repo.deployment_ids_by_provider(conn)
    for provider in single:
        ids = by_provider.get(provider, [])
        if len(ids) > 1:
            raise ImportConflict(
                "provider_single", "deployments",
                f"provider {provider} 的模組僅允許 1 個成員,匯入後共有 {len(ids)} 個:{', '.join(ids)}")

    before_ids = set(before_deps)
    privileged = tuple(sorted(
        d.id for d in plan.deployments if is_privileged_change(before_deps.get(d.id), d)))
    return ImportResult(
        mode="replace" if replace else "merge",
        created=_counts(len(file_deps - before_ids), len(file_groups - before_groups),
                        len(file_routes - before_routes)),
        updated=_counts(len(file_deps & before_ids), len(file_groups & before_groups),
                        len(file_routes & before_routes)),
        deleted=deleted,
        backup=backup,
        affected_ids=frozenset(before_ids | file_deps),
        privileged=privileged,
        dry_run=dry_run,
    )
