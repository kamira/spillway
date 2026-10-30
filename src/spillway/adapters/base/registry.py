"""AdapterRegistry:自動發現 adapter 模組並以 provider 註冊(spec 7.1.1、7.6.6、7.6.7;決策 14、15)。

掃描範圍:`spillway.adapters` 套件下、以及 `discover(extra_paths=...)` 每個路徑下,
含 `__init__.py` 的子資料夾(略過 `base` 與 `_`、`.` 開頭者)。每個資料夾是一個模組。

載入失敗(無法 import、沒有 `MANIFEST`/`create`、manifest 不合法、`contract_version` 不支援、
`models.yaml` 無法解析、`create(context)` 失敗或回傳的實例 `call`/`health` 不是 async)
→ 該模組不註冊;其 provider 依決策 14 推得((1) ast 讀 `__init__.py` 中 `MANIFEST` 的
`providers` 字面值,(2) 資料夾名 `_` 轉 `-`),列入 `unavailable()`,且**不**落到後備模組。

同一 provider 被兩個模組宣告(含載入失敗模組推得的 provider),或同一 kind 被兩個模組宣告為
`fallback_for_kinds` → `AdapterConflictError`(訊息列出兩個模組),呼叫端應拒絕啟動。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from spillway.adapters.base.manifest import (
    AdapterManifest,
    ManifestError,
    ModelInfo,
    ModelsYamlError,
    load_models_yaml,
    parse_model_entry,
    validate_manifest,
)
from spillway.adapters.base.protocol import Adapter, AdapterContext
from spillway.adapters.base.types import Deployment

BUILTIN_PACKAGE = "spillway.adapters"
_EXT_PREFIX = "_spillway_ext_adapter"


class AdapterConflictError(RuntimeError):
    """spec 7.6.6:同一 provider(或後備 kind)被兩個模組宣告。"""

    def __init__(self, provider: str, modules: Iterable[str], *, what: str = "provider") -> None:
        self.provider = provider
        self.modules = tuple(modules)
        super().__init__(
            f"{what} {provider!r} 同時被模組 {' 與 '.join(self.modules)} 宣告,拒絕啟動"
        )


class AdapterUnavailableError(LookupError):
    """成員的 provider 沒有可用模組(載入失敗或沒有任何模組處理)。"""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"provider {provider!r} 不可用:{reason}")


@dataclass
class ModuleInfo:
    name: str                               # 資料夾名
    path: Path
    import_name: str
    builtin: bool
    providers: tuple[str, ...]              # 載入失敗時為推得值
    available: bool = False
    reason: str | None = None
    manifest: AdapterManifest | None = None
    models: tuple[ModelInfo, ...] = ()
    module: ModuleType | None = field(default=None, repr=False)
    instance: Any = field(default=None, repr=False)

    @property
    def has_list_models(self) -> bool:
        return self.module is not None and callable(getattr(self.module, "list_models", None))


@dataclass(frozen=True)
class ModelsListing:
    models: list[ModelInfo]
    error: str | None = None   # list_models() 失敗原因;None 表示成功或模組沒有提供


# ---- provider 推得(決策 14)--------------------------------------------------------------

def _literal_providers(node: ast.AST) -> tuple[str, ...] | None:
    value: ast.AST | None = None
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == "providers":
                value = kw.value
    elif isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "providers":
                value = v
    if value is None:
        return None
    try:
        lit = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return None
    if isinstance(lit, str):
        lit = (lit,)
    if isinstance(lit, list | tuple) and lit and all(isinstance(p, str) and p for p in lit):
        return tuple(lit)
    return None


def infer_providers(module_dir: Path) -> tuple[str, ...]:
    """無法 import 的模組:先讀 `MANIFEST` 的 `providers` 字面值,再退回資料夾名。"""
    try:
        tree = ast.parse((module_dir / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for stmt in tree.body:
            targets: list[ast.AST] = []
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == "MANIFEST" for t in targets):
                found = _literal_providers(value)
                if found:
                    return found
    return (module_dir.name.replace("_", "-"),)


def _declared_providers(manifest_obj: Any) -> tuple[str, ...] | None:
    raw = manifest_obj.get("providers") if isinstance(manifest_obj, dict) else getattr(manifest_obj, "providers", None)
    if isinstance(raw, str):
        raw = (raw,)
    if isinstance(raw, list | tuple) and raw and all(isinstance(p, str) and p for p in raw):
        return tuple(raw)
    return None


# ---- 匯入 ---------------------------------------------------------------------------------

def _module_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and d.name != "base" and not d.name.startswith(("_", "."))
        and (d / "__init__.py").is_file()
    )


def _purge(prefix: str) -> None:
    for key in [k for k in sys.modules if k == prefix or k.startswith(prefix + ".")]:
        del sys.modules[key]


def _import_external(import_name: str, module_dir: Path) -> ModuleType:
    _purge(import_name)
    spec = importlib.util.spec_from_file_location(
        import_name, module_dir / "__init__.py", submodule_search_locations=[str(module_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"無法建立 {module_dir} 的匯入規格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[import_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        _purge(import_name)
        raise
    return module


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


class AdapterRegistry:
    """`discover()` 後以 `get(dep)` 取得 adapter 實例。

    `context` 給了才會在 `discover()` 時以 `create(context)` 建立實例;沒有 context 時
    只做發現與驗證(例如列出模組),`get()` 會丟 RuntimeError。
    """

    def __init__(self, context: AdapterContext | None = None) -> None:
        self._context = context
        self._modules: list[ModuleInfo] = []
        self._by_provider: dict[str, ModuleInfo] = {}
        self._fallback: dict[str, ModuleInfo] = {}
        self._unavailable: dict[str, str] = {}

    # -- 發現 --------------------------------------------------------------------------------

    def discover(self, extra_paths: Iterable[Path | str] = ()) -> list[AdapterManifest]:
        """重新掃描並註冊;回傳成功註冊的模組 manifest。衝突時丟 AdapterConflictError。"""
        import spillway.adapters as pkg

        self._modules, self._by_provider, self._fallback, self._unavailable = [], {}, {}, {}
        _purge(_EXT_PREFIX)

        found: list[ModuleInfo] = []
        for d in _module_dirs(Path(pkg.__file__).parent):
            found.append(self._load(d, f"{BUILTIN_PACKAGE}.{d.name}", builtin=True))
        for i, root in enumerate(extra_paths):
            for d in _module_dirs(Path(root)):
                safe = re.sub(r"\W", "_", d.name)
                found.append(self._load(d, f"{_EXT_PREFIX}.p{i}_{safe}", builtin=False))

        self._check_conflicts(found)
        if self._context is not None:
            for info in found:
                if info.available:
                    self._instantiate(info)

        self._modules = found
        for info in found:
            if info.available:
                for p in info.providers:
                    self._by_provider[p] = info
                for k in info.manifest.fallback_for_kinds:  # type: ignore[union-attr]
                    self._fallback[k] = info
            else:
                for p in info.providers:
                    self._unavailable[p] = info.reason or "模組載入失敗"
        return [m.manifest for m in found if m.available and m.manifest is not None]

    def _load(self, module_dir: Path, import_name: str, *, builtin: bool) -> ModuleInfo:
        info = ModuleInfo(name=module_dir.name, path=module_dir, import_name=import_name,
                          builtin=builtin, providers=())

        def fail(reason: str, providers: tuple[str, ...] | None = None) -> ModuleInfo:
            info.available = False
            info.reason = f"模組 {module_dir.name} 載入失敗:{reason}"
            info.providers = providers or infer_providers(module_dir)
            info.manifest = None
            return info

        if not builtin:
            _ensure_ext_parent()
        try:
            module = importlib.import_module(import_name) if builtin else _import_external(import_name, module_dir)
        except Exception as exc:  # noqa: BLE001 — 任何 import 錯誤都是「無法 import」
            return fail(f"無法 import({_short(exc)})")
        info.module = module

        raw_manifest = getattr(module, "MANIFEST", None)
        if raw_manifest is None:
            return fail("沒有匯出 MANIFEST")
        try:
            manifest = validate_manifest(raw_manifest)
        except ManifestError as exc:
            return fail(f"manifest 不合法({exc})", _declared_providers(raw_manifest))
        info.providers = manifest.providers

        if not callable(getattr(module, "create", None)):
            return fail("沒有匯出 create(context)", manifest.providers)
        try:
            models = load_models_yaml(module_dir / "models.yaml")
        except ModelsYamlError as exc:
            return fail(f"models.yaml 不合法({exc})", manifest.providers)

        info.manifest = manifest
        info.models = tuple(models)
        info.available = True
        return info

    def _check_conflicts(self, found: list[ModuleInfo]) -> None:
        owner: dict[str, ModuleInfo] = {}
        for info in found:
            for p in info.providers:
                if p in owner:
                    raise AdapterConflictError(p, (owner[p].name, info.name))
                owner[p] = info
        fb: dict[str, ModuleInfo] = {}
        for info in found:
            if info.available and info.manifest is not None:
                for k in info.manifest.fallback_for_kinds:
                    if k in fb:
                        raise AdapterConflictError(k, (fb[k].name, info.name), what="後備 kind")
                    fb[k] = info

    def _instantiate(self, info: ModuleInfo) -> None:
        try:
            instance = info.module.create(self._context)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            info.available, info.reason = False, f"模組 {info.name} 載入失敗:create() 失敗({_short(exc)})"
            return
        for meth in ("call", "health"):
            if not inspect.iscoroutinefunction(getattr(instance, meth, None)):
                info.available = False
                info.reason = f"模組 {info.name} 載入失敗:{meth}() 必須是 async def"
                return
        info.instance = instance

    # -- 查詢 --------------------------------------------------------------------------------

    def modules(self) -> list[ModuleInfo]:
        """所有被發現的模組(含載入失敗者,`available=False` 並附 `reason`)。"""
        return list(self._modules)

    def unavailable(self) -> dict[str, str]:
        """provider → 不可用原因(spec 7.6.7;路由篩選原因 `adapter_unavailable`)。"""
        return dict(self._unavailable)

    def module_for(self, dep: Deployment) -> ModuleInfo:
        """處理此成員的模組;沒有可用模組時丟 AdapterUnavailableError。"""
        if dep.provider in self._unavailable:
            raise AdapterUnavailableError(dep.provider, self._unavailable[dep.provider])
        info = self._by_provider.get(dep.provider) or self._fallback.get(dep.kind)
        if info is None:
            raise AdapterUnavailableError(
                dep.provider, f"沒有模組宣告 provider {dep.provider!r},kind {dep.kind!r} 也沒有後備模組"
            )
        if dep.kind not in info.manifest.kinds:  # type: ignore[union-attr]
            raise AdapterUnavailableError(
                dep.provider, f"模組 {info.name} 不支援 kind {dep.kind!r}"
            )
        return info

    def get(self, dep: Deployment) -> Adapter:
        info = self.module_for(dep)
        if info.instance is None:
            raise RuntimeError("AdapterRegistry 沒有 AdapterContext,無法建立 adapter 實例")
        return info.instance

    def cardinality_of(self, provider: str, kind: str | None = None) -> str | None:
        """`single`/`multiple`;provider 不可用或無模組處理時回 None。

        未宣告的 provider 由後備模組決定:給了 `kind` 就看該 kind 的後備模組;
        沒給 `kind` 時,所有後備 kind 都屬同一模組才以它為準。
        """
        if provider in self._unavailable:
            return None
        info = self._by_provider.get(provider)
        if info is None:
            if kind is not None:
                info = self._fallback.get(kind)
            else:
                owners = {id(m): m for m in self._fallback.values()}
                info = next(iter(owners.values())) if len(owners) == 1 else None
        return info.manifest.cardinality if info is not None and info.manifest is not None else None


def _ensure_ext_parent() -> None:
    """額外路徑模組掛在虛擬套件 `_spillway_ext_adapter` 底下,讓相對 import 能解析。"""
    if _EXT_PREFIX not in sys.modules:
        parent = ModuleType(_EXT_PREFIX)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[_EXT_PREFIX] = parent


async def available_models(info: ModuleInfo, context: AdapterContext) -> ModelsListing:
    """`models.yaml` 加上可選 `list_models(context)` 的結果(spec 7.6.4)。

    同 `id` 以 `models.yaml` 為準;執行期項目缺 `label` 時用 `id`、缺 `capabilities` 時
    用 manifest 的 `default_capabilities`。查詢失敗只回 `models.yaml` 並附原因。
    """
    static = list(info.models)
    if not info.has_list_models:
        return ModelsListing(static)
    try:
        result = info.module.list_models(context)  # type: ignore[union-attr]
        if inspect.isawaitable(result):
            result = await result
        known = {m.id for m in static}
        merged = list(static)
        defaults = dict(info.manifest.default_capabilities) if info.manifest else {}
        for i, item in enumerate(result or []):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ModelsYamlError(f"[{i}]", "list_models() 每筆必須是含 id 的 dict")
            if item["id"] in known:
                continue
            entry = {"label": item["id"], "capabilities": defaults, **item}
            merged.append(parse_model_entry(entry, f"list_models[{i}]"))
            known.add(item["id"])
        return ModelsListing(merged)
    except Exception as exc:  # noqa: BLE001
        return ModelsListing(static, error=_short(exc))
