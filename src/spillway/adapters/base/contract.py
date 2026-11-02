"""共用合約檢查(spec 7.6.1、7.6.3、7.6.8;design 規則 2、3)。

`tests/contract/` 與 T15 的 AC-19 步驟都呼叫這裡,讓「合約測試對該模組通過」可以對
任意路徑的模組執行。`call` 對四種上游輸出的 status 由 `tests/contract/test_call_status.py` 驗證。
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

from spillway.adapters.base.protocol import AdapterContext
from spillway.adapters.base.registry import ModuleInfo
from spillway.adapters.base.types import EFFORTS

REQUIRED_FILES = ("__init__.py", "adapter.py", "models.yaml", "README.md")
ALLOWED_SPILLWAY_PREFIX = "spillway.adapters.base"
# Global Constraints「分層」:第三方套件只放行 pyyaml;其餘(含 litellm、httpx)一律不可
ALLOWED_THIRD_PARTY = ("yaml",)
# CHG-20261101-03 Global Constraints「分層」:模組不可自行起子行程,只能經 `AdapterContext.cli_runner`。
# `import asyncio`、`os`、`tempfile` 本身仍允許;禁的是下列模組與函式。
SUBPROCESS_MODULES = ("subprocess", "pty", "asyncio.subprocess", "multiprocessing")
# `os` 與 `posix`(os 的底層實作)上的起行程函式
_OS_MODULES = ("os", "posix")
_OS_SPAWN = re.compile(r"^(system|popen|spawn\w*|exec\w*|posix_spawn\w*|fork|forkpty)$")
# 不論掛在哪個物件上都禁的屬性/名稱:`asyncio.create_subprocess_*`、事件迴圈的
# `loop.subprocess_exec`/`subprocess_shell`、`concurrent.futures.ProcessPoolExecutor`
_ANY_ATTR = re.compile(r"^(create_subprocess_\w+|subprocess_exec|subprocess_shell|ProcessPoolExecutor)$")
_SUBPROCESS_HINT = "(模組不可自行起子行程,只能經 AdapterContext.cli_runner)"

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


def _dynamic_import_literal(node: ast.AST) -> str | None:
    """`importlib.import_module("…")`、`__import__("…")` 的字面值;不是這種呼叫時回 None。"""
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
    if fname in ("import_module", "__import__") and node.args \
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _subprocess_violations(tree: ast.AST, where: str) -> list[str]:
    """子行程禁用規則。

    - import 即違規:`subprocess`、`pty`、`asyncio.subprocess`、`multiprocessing`(含子模組、
      `from … import`、`importlib.import_module`/`__import__` 字面值)。
    - `os`/`posix` 上的 `system`、`popen`、`spawn*`、`exec*`、`posix_spawn*`、`fork`、`forkpty`:
      `from os import …` 與經別名(`import os as x`;`import os.path` 也會綁定 `os`)的屬性呼叫。
    - 任何物件上的 `create_subprocess_*`、`subprocess_exec`、`subprocess_shell`、
      `ProcessPoolExecutor`(含 `from … import` 這些名稱)。
    """
    out: list[str] = []
    os_aliases: set[str] = set()

    def bad(node: ast.AST, what: str) -> None:
        out.append(f"{where}:{getattr(node, 'lineno', 0)}: 不可使用 {what}{_SUBPROCESS_HINT}")

    def banned_module(name: str) -> bool:
        return any(_under(name, m) for m in SUBPROCESS_MODULES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".", 1)[0]
                if banned_module(a.name):
                    bad(node, a.name)
                elif top in _OS_MODULES:
                    # `import os` / `import os as x` / `import os.path`(綁定 os)/ `import os.path as p`
                    # (保守起見同樣把 os 視為已綁定)
                    os_aliases.add(top)
                    if a.asname and a.name in _OS_MODULES:
                        os_aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mod = node.module or ""
            if banned_module(mod):
                bad(node, mod)
                continue
            for a in node.names:
                full = f"{mod}.{a.name}" if mod else a.name
                if banned_module(full):
                    bad(node, full)
                elif mod in _OS_MODULES and _OS_SPAWN.match(a.name):
                    bad(node, full)
                elif _ANY_ATTR.match(a.name):
                    bad(node, full)
        else:
            lit = _dynamic_import_literal(node)
            if lit is not None and banned_module(lit):
                bad(node, lit)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if _ANY_ATTR.match(node.attr):
                bad(node, node.attr)
            elif isinstance(node.value, ast.Name) and node.value.id in os_aliases \
                    and _OS_SPAWN.match(node.attr):
                bad(node, f"{node.value.id}.{node.attr}")
    return out

def layering_violations(module_dir: Path, package_name: str) -> list[str]:
    """以 AST 掃描模組資料夾內所有 .py 的 import,回傳違規描述(`檔案:行: 說明`)。

    白名單(Global Constraints「分層」):`spillway.adapters.base`(含子模組)、自己模組內的
    檔案、標準庫(`sys.stdlib_module_names`)與 `yaml`。其餘一律報錯:其他 `spillway.*`、
    `litellm`、`httpx` 等任何第三方套件(需要 HTTP 探測請用 `adapters.base.probe_http`)。
    `importlib.import_module("…")`、`__import__("…")` 的字面值同樣檢查(`.` 開頭視為相對)。
    另檢查子行程禁用規則(`_subprocess_violations`)。
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
        out.extend(_subprocess_violations(tree, where))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = (_resolve_relative(package, node.level, node.module) if node.level
                       else node.module or "")
                if _forbidden(mod, package_name):
                    targets = [f"{mod}.{a.name}" if mod else a.name for a in node.names]
            elif (lit := _dynamic_import_literal(node)) is not None:
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
