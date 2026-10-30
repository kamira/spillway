"""共用合約檢查(spec 7.6.1、7.6.3、7.6.8;design 規則 2、3)。

`tests/contract/` 與 T15 的 AC-19 步驟都呼叫這裡,讓「合約測試對該模組通過」可以對
任意路徑的模組執行。`call` 對四種上游輸出的 status 由 `tests/contract/test_call_status.py` 驗證。
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

from spillway.adapters.base.protocol import AdapterContext
from spillway.adapters.base.registry import ModuleInfo
from spillway.adapters.base.types import EFFORTS

REQUIRED_FILES = ("__init__.py", "adapter.py", "models.yaml", "README.md")
ALLOWED_SPILLWAY_PREFIX = "spillway.adapters.base"
# Global Constraints「分層」:第三方套件只放行 pyyaml;其餘(含 litellm、httpx)一律不可
ALLOWED_THIRD_PARTY = ("yaml",)


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _allowed(name: str, own_package: str) -> bool:
    """白名單:自己的套件、`spillway.adapters.base`、標準庫、`yaml`。"""
    if _under(name, own_package) or _under(name, ALLOWED_SPILLWAY_PREFIX):
        return True
    top = name.split(".", 1)[0]
    if not top or top == "spillway":
        return False
    return top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY


def _forbidden(name: str, own_package: str) -> bool:
    return not _allowed(name, own_package)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    parts = package.split(".")
    if level - 1 >= len(parts):
        base = ""
    else:
        base = ".".join(parts[: len(parts) - (level - 1)])
    if module:
        return f"{base}.{module}" if base else module
    return base


def layering_violations(module_dir: Path, package_name: str) -> list[str]:
    """以 AST 掃描模組資料夾內所有 .py 的 import,回傳違規描述(`檔案:行: 說明`)。

    白名單(Global Constraints「分層」):`spillway.adapters.base`(含子模組)、自己模組內的
    檔案、標準庫(`sys.stdlib_module_names`)與 `yaml`。其餘一律報錯:其他 `spillway.*`、
    `litellm`、`httpx` 等任何第三方套件(需要 HTTP 探測請用 `adapters.base.probe_http`)。
    `importlib.import_module("…")`、`__import__("…")` 的字面值同樣檢查(`.` 開頭視為相對)。
    """
    module_dir = Path(module_dir)
    out: list[str] = []
    for py in sorted(module_dir.rglob("*.py")):
        rel = py.relative_to(module_dir)
        sub = rel.parent.parts
        package = ".".join((package_name, *sub)) if sub else package_name
        where = f"{module_dir.name}/{rel.as_posix()}"
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            out.append(f"{where}:{exc.lineno}: 語法錯誤,無法檢查 import")
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = (_resolve_relative(package, node.level, node.module) if node.level
                       else node.module or "")
                if _forbidden(mod, package_name):
                    targets = [f"{mod}.{a.name}" if mod else a.name for a in node.names]
            elif isinstance(node, ast.Call):
                fn = node.func
                fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
                if fname in ("import_module", "__import__") and node.args \
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    lit = node.args[0].value
                    level = len(lit) - len(lit.lstrip("."))
                    targets = [_resolve_relative(package, level, lit[level:] or None) if level else lit]
            for t in targets:
                if _forbidden(t, package_name):
                    out.append(f"{where}:{node.lineno}: 不可 import {t}"
                               f"(只可 import spillway.adapters.base、標準庫與 yaml)")
    return out


def contract_problems(info: ModuleInfo, context: AdapterContext | None = None) -> list[str]:
    """回傳模組違反共用合約之處;空清單表示通過。

    檢查:7.6.1 四個檔案、模組可載入且 manifest 合法(含 `contract_version`、`effort_map`
    五級)、`models.yaml` 可解析且 `efforts` 只含五級、`create(context)` 的實例 `call`/`health`
    為 async、import 分層。
    """
    problems: list[str] = []
    for f in REQUIRED_FILES:
        if not (info.path / f).is_file():
            problems.append(f"缺少檔案 {f}(spec 7.6.1)")
    if not info.available or info.manifest is None:
        problems.append(info.reason or f"模組 {info.name} 無法載入")
    else:
        if set(info.manifest.effort_map) != set(EFFORTS):
            problems.append("effort_map 必須定義五個等級")
        for m in info.models:
            bad = set(m.capabilities.get("efforts", ())) - set(EFFORTS)
            if bad:
                problems.append(f"models.yaml {m.id} 的 efforts 含 {sorted(bad)}")
        instance = info.instance
        if instance is None and context is not None and info.module is not None:
            try:
                instance = info.module.create(context)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"create(context) 失敗:{type(exc).__name__}: {exc}")
        if instance is not None:
            for meth in ("call", "health"):
                if not inspect.iscoroutinefunction(getattr(instance, meth, None)):
                    problems.append(f"{meth}() 必須是 async def(spec 7.1)")
    package = info.import_name if info.builtin else f"spillway.adapters.{info.name}"
    problems.extend(layering_violations(info.path, package))
    return problems
