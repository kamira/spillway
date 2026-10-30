"""AdapterManifest、manifest 驗證與 models.yaml 解析(spec 7.6.2、7.6.4;design D11、D12)。

模組在 `__init__.py` 匯出:

- `MANIFEST`:`AdapterManifest(...)`(或同欄位的 dict);`providers` 請寫成字面值,
  模組無法 import 時 registry 以 ast 讀它推得 provider(決策 14)。
- `create(context) -> Adapter`。
- 可選 `list_models(context)`(async 或同步):執行期向上游查詢模型,回傳 dict 清單
  (每筆至少含 `id`),由 `available_models()` 與 `models.yaml` 合併。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from spillway.adapters.base.signals import SignalsError, validate_signals
from spillway.adapters.base.types import EFFORTS, KINDS

ACCESS_VALUES = ("subscription-cli", "api", "local")
CARDINALITIES = ("single", "multiple")
SUPPORTED_CONTRACT_VERSIONS = (1,)
REQUIRED_MANIFEST_FIELDS = (
    "providers", "kinds", "access", "cardinality", "effort_map", "default_signals",
    "default_capabilities", "default_params", "requires", "contract_version",
)
OPTIONAL_MANIFEST_FIELDS = ("fallback_for_kinds",)


class ManifestError(ValueError):
    """manifest 不合法;`field` 為出錯的欄位路徑(例如 `effort_map.max`)。"""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


class ModelsYamlError(ValueError):
    """models.yaml 不合法;`field` 為出錯位置(例如 `[0].capabilities.efforts`)。"""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        where = f"models.yaml{field}" if not field or field.startswith("[") else field
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class AdapterManifest:
    providers: tuple[str, ...]
    kinds: tuple[str, ...]
    access: tuple[str, ...]
    cardinality: str
    effort_map: Mapping[str, str | None]
    default_signals: Mapping[str, Any]
    default_capabilities: Mapping[str, Any]
    default_params: Mapping[str, Any]
    requires: tuple[str, ...]
    contract_version: int
    fallback_for_kinds: tuple[str, ...] = ()   # 只有通用 litellm 模組宣告 ("api", "local")


def _str_tuple(v: Any, field: str, *, allowed: tuple[str, ...] | None = None,
               allow_str: bool = False, allow_empty: bool = False) -> tuple[str, ...]:
    if allow_str and isinstance(v, str):
        v = [v]
    if not isinstance(v, list | tuple):
        raise ManifestError(field, "必須是字串清單")
    if not v and not allow_empty:
        raise ManifestError(field, "不可為空")
    for i, item in enumerate(v):
        if not isinstance(item, str) or not item:
            raise ManifestError(f"{field}[{i}]", "必須是非空字串")
        if allowed is not None and item not in allowed:
            raise ManifestError(f"{field}[{i}]", f"{item!r} 不合法,可用:{', '.join(allowed)}")
    if len(set(v)) != len(v):
        raise ManifestError(field, "有重複值")
    return tuple(v)


def _validate_efforts(v: Any, field: str, err: type[ValueError]) -> tuple[str, ...]:
    if not isinstance(v, list | tuple):
        raise err(field, "必須是清單")
    bad = [e for e in v if e not in EFFORTS]
    if bad:
        raise err(field, f"只可含 {', '.join(EFFORTS)},不可有 {', '.join(map(str, bad))}")
    return tuple(v)


def validate_manifest(obj: Any) -> AdapterManifest:
    """驗證 `MANIFEST`(AdapterManifest 或 dict)並回傳正規化後的 AdapterManifest。

    不合法時丟 `ManifestError`,`field` 指出欄位。
    """
    if isinstance(obj, AdapterManifest):
        data = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    elif isinstance(obj, Mapping):
        data = dict(obj)
    else:
        raise ManifestError("MANIFEST", "必須是 AdapterManifest 或 dict")

    for name in REQUIRED_MANIFEST_FIELDS:
        if name not in data:
            raise ManifestError(name, "缺少必要欄位")
    for name in data:
        if name not in REQUIRED_MANIFEST_FIELDS + OPTIONAL_MANIFEST_FIELDS:
            raise ManifestError(name, "未知欄位")

    providers = _str_tuple(data["providers"], "providers")
    kinds = _str_tuple(data["kinds"], "kinds", allowed=KINDS)
    access = _str_tuple(data["access"], "access", allowed=ACCESS_VALUES, allow_str=True)

    cardinality = data["cardinality"]
    if cardinality not in CARDINALITIES:
        raise ManifestError("cardinality", f"必須是 single 或 multiple,收到 {cardinality!r}")

    effort_map = data["effort_map"]
    if not isinstance(effort_map, Mapping):
        raise ManifestError("effort_map", "必須是 {等級: 上游值或 null}")
    for level in EFFORTS:
        if level not in effort_map:
            raise ManifestError(f"effort_map.{level}", "未定義(五個等級皆須定義,不傳時為 null)")
    for level, value in effort_map.items():
        if level not in EFFORTS:
            raise ManifestError(f"effort_map.{level}", f"不是內部等級,只可為 {', '.join(EFFORTS)}")
        if value is not None and not (isinstance(value, str) and value):
            raise ManifestError(f"effort_map.{level}", "必須是非空字串或 null")

    try:
        validate_signals(data["default_signals"], "default_signals")
    except SignalsError as e:
        raise ManifestError(e.field, str(e).split(": ", 1)[-1]) from None

    caps = data["default_capabilities"]
    if not isinstance(caps, Mapping):
        raise ManifestError("default_capabilities", "必須是 dict")
    if "efforts" in caps:
        _validate_efforts(caps["efforts"], "default_capabilities.efforts", ManifestError)

    params = data["default_params"]
    if not isinstance(params, Mapping):
        raise ManifestError("default_params", "必須是 dict")

    requires = _str_tuple(data["requires"], "requires", allow_empty=True)

    version = data["contract_version"]
    if not (isinstance(version, int) and not isinstance(version, bool)) or version not in SUPPORTED_CONTRACT_VERSIONS:
        raise ManifestError(
            "contract_version",
            f"不支援的版本 {version!r},支援:{', '.join(map(str, SUPPORTED_CONTRACT_VERSIONS))}",
        )

    fallback = _str_tuple(data.get("fallback_for_kinds", ()), "fallback_for_kinds",
                          allowed=KINDS, allow_empty=True)
    for i, k in enumerate(fallback):
        if k not in kinds:
            raise ManifestError(f"fallback_for_kinds[{i}]", f"{k!r} 不在 kinds 內")

    return AdapterManifest(
        providers=providers, kinds=kinds, access=access, cardinality=cardinality,
        effort_map=dict(effort_map), default_signals=dict(data["default_signals"]),
        default_capabilities=dict(caps), default_params=dict(params), requires=requires,
        contract_version=version, fallback_for_kinds=fallback,
    )


# ---- models.yaml -----------------------------------------------------------------------

@dataclass(frozen=True)
class ModelInfo:
    """可用模型清單的一筆(spec 7.6.4)。`capabilities.efforts` 正規化為 tuple。"""

    id: str
    label: str
    capabilities: Mapping[str, Any]
    provider: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        caps = dict(self.capabilities)
        if "efforts" in caps:
            caps["efforts"] = list(caps["efforts"])
        d: dict[str, Any] = {"id": self.id, "label": self.label, "capabilities": caps}
        if self.provider is not None:
            d["provider"] = self.provider
        if self.note is not None:
            d["note"] = self.note
        return d


def _parse_caps(caps: Any, f: str) -> dict[str, Any]:
    if not isinstance(caps, Mapping):
        raise ModelsYamlError(f, "必須是 {ctx, tools, vision, efforts}")
    for key in ("ctx", "tools", "vision", "efforts"):
        if key not in caps:
            raise ModelsYamlError(f"{f}.{key}", "缺少必要欄位")
    ctx = caps["ctx"]
    if not (isinstance(ctx, int) and not isinstance(ctx, bool)) or ctx <= 0:
        raise ModelsYamlError(f"{f}.ctx", "必須是正整數")
    for key in ("tools", "vision"):
        if not isinstance(caps[key], bool):
            raise ModelsYamlError(f"{f}.{key}", "必須是 true 或 false")
    out = dict(caps)
    out["efforts"] = _validate_efforts(caps["efforts"], f"{f}.efforts", ModelsYamlError)
    return out


def parse_model_entry(entry: Any, f: str) -> ModelInfo:
    if not isinstance(entry, Mapping):
        raise ModelsYamlError(f, "每筆必須是 dict")
    for key in ("id", "label"):
        if not isinstance(entry.get(key), str) or not entry.get(key):
            raise ModelsYamlError(f"{f}.{key}", "缺少或不是非空字串")
    if "capabilities" not in entry:
        raise ModelsYamlError(f"{f}.capabilities", "缺少必要欄位")
    caps = _parse_caps(entry["capabilities"], f"{f}.capabilities")
    for key in ("provider", "note"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            raise ModelsYamlError(f"{f}.{key}", "必須是字串")
    return ModelInfo(id=entry["id"], label=entry["label"], capabilities=caps,
                     provider=entry.get("provider"), note=entry.get("note"))


def parse_models(data: Any) -> list[ModelInfo]:
    """解析 models.yaml 的內容(頂層為清單;空檔視為空清單)。"""
    if data is None:
        return []
    if not isinstance(data, list):
        raise ModelsYamlError("", "頂層必須是清單")
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        m = parse_model_entry(entry, f"[{i}]")
        if m.id in seen:
            raise ModelsYamlError(f"[{i}].id", f"重複的 id {m.id!r}")
        seen.add(m.id)
        out.append(m)
    return out


def load_models_yaml(path: Path | str) -> list[ModelInfo]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ModelsYamlError(p.name, f"無法讀取:{e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise ModelsYamlError(p.name, f"不是 UTF-8 編碼:{e.reason}(位置 {e.start})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ModelsYamlError(p.name, f"YAML 無法解析:{e}") from None
    return parse_models(data)
