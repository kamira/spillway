"""`spillway.api.admin` 套件的公開匯入路徑與 OpenAPI 契約(CHG-20261101-02 T4;決策 9)。

- L1:`__all__ == ["router"]`;決策 9 列出的轉匯出屬性與實作所在子模組的物件相同;
  每個子模組 ≤ 400 行;子模組的 logger 名稱一律 `spillway.api.admin`。
- L2:`create_app()` 的 OpenAPI 路徑 → 方法 → 回應狀態碼與拆檔前完全相同
  (`EXPECTED_OPENAPI` 為拆檔前由 `create_app().openapi()` 擷取的常數)。
"""
from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path

import pytest

SUBMODULES = (
    "common", "models", "secrets", "deployments", "groups", "routes", "planning",
    "ledger", "settings", "dashboard", "adapters",
)
MAX_LINES = 400
LOGGER_NAME = "spillway.api.admin"

# 拆檔前(commit d744265,api/admin.py 單檔)擷取
EXPECTED_OPENAPI: dict[str, dict[str, tuple[str, ...]]] = {
    "/api/adapters": {"get": ("200",)},
    "/api/dashboard": {"get": ("200",)},
    "/api/deployments": {"get": ("200",), "post": ("201", "422")},
    "/api/deployments/{dep_id}": {"delete": ("204", "422"), "get": ("200", "422"),
                                  "put": ("200", "422")},
    "/api/deployments/{dep_id}/clear-cooldown": {"post": ("200", "422")},
    "/api/deployments/{dep_id}/test": {"post": ("200", "422")},
    "/api/estimate": {"post": ("200", "422")},
    "/api/groups": {"get": ("200",), "post": ("201", "422")},
    "/api/groups/{name}": {"delete": ("204", "422"), "get": ("200", "422"),
                           "put": ("200", "422")},
    "/api/ledger": {"get": ("200", "422")},
    "/api/ledger/{ledger_id}/payload": {"get": ("200", "422")},
    "/api/routes": {"get": ("200",), "post": ("201", "422")},
    "/api/routes/{name}": {"delete": ("204", "422"), "get": ("200", "422"),
                           "put": ("200", "422")},
    "/api/routes/{name}/dry-run": {"post": ("200", "422")},
    "/api/routes/{name}/order": {"delete": ("204", "422"), "put": ("200", "422")},
    "/api/settings/context": {"get": ("200",), "put": ("200", "422")},
    "/v1/chat/completions": {"post": ("200", "422")},
    "/v1/models": {"get": ("200",)},
}

# 轉匯出名稱 → 實作所在子模組
REEXPORTS = {
    "router": "common",
    "ApiError": "common",
    "QuotaService": "deployments",
    "test_deployment": "deployments",
    "TEST_TEXT_PREVIEW": "deployments",
    "DeploymentIn": "models",
    "ChatBody": "planning",
    "StatusLit": "ledger",
}


def _sub(name: str):
    return importlib.import_module(f"spillway.api.admin.{name}")


def test_all_is_router_only():
    from spillway.api import admin

    assert admin.__all__ == ["router"]


def test_admin_is_package():
    from spillway.api import admin

    assert Path(admin.__file__).name == "__init__.py"


@pytest.mark.parametrize("name", sorted(REEXPORTS))
def test_reexports_are_same_object(name):
    from spillway.api import admin

    assert getattr(admin, name) is getattr(_sub(REEXPORTS[name]), name)


def test_shared_objects_are_upstream():
    import spillway.api as api
    from spillway.api import admin
    from spillway.quota import QuotaService

    assert admin.ApiError is api.ApiError
    assert admin.QuotaService is QuotaService


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_line_count(name):
    path = Path(_sub(name).__file__)
    n = len(path.read_text(encoding="utf-8").splitlines())
    assert n <= MAX_LINES, f"{path.name}: {n} 行"


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_logger_name(name):
    mod = _sub(name)
    for attr, value in vars(mod).items():
        if isinstance(value, logging.Logger):
            assert value.name == LOGGER_NAME, f"{name}.{attr}"
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        callee = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if callee != "getLogger":
            continue
        args = node.args
        assert (len(args) == 1 and not node.keywords and isinstance(args[0], ast.Constant)
                and args[0].value == LOGGER_NAME), f"{name}.py:{node.lineno} getLogger 參數"


def test_deployments_logger_is_admin_logger():
    assert _sub("deployments")._log is logging.getLogger(LOGGER_NAME)


def test_all_routes_registered_on_single_router():
    from spillway.api import admin

    paths = {r.path for r in admin.router.routes}
    assert paths == {p for p in EXPECTED_OPENAPI if p.startswith("/api/")}


def test_openapi_paths_methods_status_codes_unchanged(tmp_path):
    from spillway.app import create_app

    spec = create_app(db_path=tmp_path / "openapi.db").openapi()
    got = {p: {m: tuple(sorted(op["responses"])) for m, op in ops.items()}
           for p, ops in spec["paths"].items()}
    assert got == EXPECTED_OPENAPI
