"""`GET /api/config/export`(CHG-20261102-02 T3;決策 5、18、19;spec 3)。

匯出內容由 `spillway.configio.dump` 產生(格式見該模組 docstring);本模組只負責 HTTP 轉換:
- 回應 `application/yaml; charset=utf-8`、`Content-Disposition: attachment;
  filename="spillway-config-<UTC yyyymmddThhmmssZ>.yaml"`(時間取自 `request.app.state.clock`)、
  `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。
- 舊列掃描(決策 5,AM-9):匯出前在同一個讀交易內對每個成員的 `params`、`capabilities` 跑
  `secrets._secret_paths`;命中 → 422 `validation_error`,訊息只列 `deployments[<id>].params.<path>`
  位置(不含值),須先以管理 API 修正。匯入備份走 `configio.dump`,不經此掃描。
- Host/Origin 由 app 層 middleware 照常檢查(9.1.4)。

`POST /api/config/import?mode=merge|replace[&dry_run=true]`(T4;決策 6、7、19、20):
- 請求邊界:Content-Type 只收 `application/yaml`、`application/x-yaml`、`text/yaml`(參數只可為
  `charset=utf-8`),其他或沒帶 → 422(yaml 型別不在 CORS safelist,強制 preflight;跨站表單送不出);
  `Content-Length` 超過 `MAX_IMPORT_BYTES` 不讀直接 422;body 以 `request.stream()` 累計,超過上限
  立刻 422;`utf-8-sig` 解碼,失敗 → 422。
- 解析與驗證:`config_file.parse`(全部錯誤,上限 20);套用:`configio.apply`(單一交易;
  `ImportConflict` → 422 / 409 `provider_single` / 500 `internal_error`,訊息經 `redact_echo`)。
- 備份:`Path(app.state.db_path).parent / "backups"`;時間 `app.state.clock.now()`。
- 成功(非 dry_run)後:對 `affected_ids` 逐一 `router.invalidate_health(id)`,再 `tools_changed` 一次;
  dry_run 兩者都不做、不寫備份。
- 回應 `{mode, dry_run, created, updated, deleted, backup, privileged}`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Query, Request
from fastapi.responses import Response

from spillway import configio
from spillway.configio import utc_stamp_filename
from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.store import repo

from . import config_file
from .common import router, tools_changed
from .secrets import _secret_paths

EXPORT_MEDIA_TYPE = "application/yaml; charset=utf-8"
EXPORT_FILENAME_FORMAT = "spillway-config-%Y%m%dT%H%M%SZ.yaml"
_SECRET_REASON = "疑似金鑰,請先以管理 API 改用 credential_ref 再匯出"


def _legacy_secret_hits(deployments: list) -> list[str]:
    hits: list[str] = []
    for dep in deployments:
        hits += _secret_paths(dep.params, f"deployments[{dep.id}].params")
        hits += _secret_paths(dep.capabilities, f"deployments[{dep.id}].capabilities")
    return hits


@router.get(
    "/config/export",
    response_class=Response,
    responses={
        200: {"description": "YAML 設定檔(spec 3)", "content": {"application/yaml": {}}},
        422: {"description": "既有成員的 params/capabilities 含疑似憑證鍵名或值"},
    },
)
async def export_config(request: Request) -> Response:
    """目前設定的 YAML(決策 5);不含憑證值、token 雜湊、冷卻、帳本、payload、埠號。"""
    with request_conn(request) as conn:
        with repo.read_snapshot(conn):
            hits = _legacy_secret_hits(repo.list_deployments(conn))
            if hits:
                raise ApiError(422, "validation_error",
                               redact_echo("; ".join(f"{h}: {_SECRET_REASON}" for h in hits)))
            text = configio.dump(conn)
    filename = utc_stamp_filename(request.app.state.clock.now(), EXPORT_FILENAME_FORMAT)
    return Response(
        content=text.encode("utf-8"),
        media_type=EXPORT_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ================================================================ POST /api/config/import(T4)

IMPORT_MEDIA_TYPES = frozenset({"application/yaml", "application/x-yaml", "text/yaml"})
MAX_IMPORT_BYTES = 1_048_576
BACKUP_DIRNAME = "backups"
_CONTENT_TYPE_REASON = "Content-Type 必須為 application/yaml"


def _bad_request(message: str) -> ApiError:
    return ApiError(422, "validation_error", message)


def _check_content_type(value: str | None) -> None:
    if not value:
        raise _bad_request(_CONTENT_TYPE_REASON)
    media, *params = (p.strip().lower() for p in value.split(";"))
    if media not in IMPORT_MEDIA_TYPES:
        raise _bad_request(_CONTENT_TYPE_REASON)
    for param in params:
        if param.replace(" ", "") not in ("charset=utf-8", "charset=\"utf-8\"", ""):
            raise _bad_request("Content-Type 的 charset 必須為 utf-8")


async def _read_body(request: Request) -> str:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise _bad_request("Content-Length 無效") from None
        if declared > MAX_IMPORT_BYTES:
            raise _bad_request(f"設定檔超過上限 {MAX_IMPORT_BYTES} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise _bad_request(f"設定檔超過上限 {MAX_IMPORT_BYTES} bytes")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _bad_request("設定檔必須是 UTF-8 文字") from None


@router.post(
    "/config/import",
    responses={
        403: {"description": "Host/Origin 不允許(9.1.4)"},
        409: {"description": "`single` 模組的 provider 匯入後超過 1 個成員(`provider_single`)"},
        422: {"description": "Content-Type、大小、YAML 或欄位驗證失敗(訊息含欄位路徑)"},
    },
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/yaml": {"schema": {"type": "string"}}}}},
)
async def import_config(
    request: Request,
    mode: Annotated[Literal["merge", "replace"], Query()],
    dry_run: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """匯入 YAML 設定檔(spec 3.1;決策 6、7、20)。回 `{mode, dry_run, created, updated, deleted,
    backup, privileged}`:計數各為 `{deployments, groups, routes}`;`backup` 為 `backups/` 下的檔名
    (merge 與 dry_run 為 null);`privileged` 為新增或變更了 privileged 欄位的成員 id(依 id 排序)。"""
    _check_content_type(request.headers.get("content-type"))
    plan = config_file.parse(await _read_body(request))
    state = request.app.state
    with request_conn(request) as conn:
        try:
            result = configio.apply(
                conn, plan, mode,
                cardinality_of=state.registry.cardinality_of,
                backup_dir=Path(state.db_path).parent / BACKUP_DIRNAME,
                now=state.clock.now(),
                dry_run=dry_run,
            )
        except configio.ImportConflict as exc:
            text = f"{exc.path}: {exc.message}" if exc.path else exc.message
            raise ApiError(exc.status, exc.code, redact_echo(text)) from None
    if not result.dry_run:
        for dep_id in sorted(result.affected_ids):
            state.router.invalidate_health(dep_id)
        tools_changed(request)
    return {
        "mode": result.mode,
        "dry_run": result.dry_run,
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "backup": result.backup,
        "privileged": list(result.privileged),
    }
