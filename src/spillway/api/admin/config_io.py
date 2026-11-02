"""`GET /api/config/export`(CHG-20261102-02 T3;決策 5、18、19;spec 3)。

匯出內容由 `spillway.configio.dump` 產生(格式見該模組 docstring);本模組只負責 HTTP 轉換:
- 回應 `application/yaml; charset=utf-8`、`Content-Disposition: attachment;
  filename="spillway-config-<UTC yyyymmddThhmmssZ>.yaml"`(時間取自 `request.app.state.clock`)、
  `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。
- 舊列掃描(決策 5,AM-9):匯出前在同一個讀交易內對每個成員的 `params`、`capabilities` 跑
  `secrets._secret_paths`;命中 → 422 `validation_error`,訊息只列 `deployments[<id>].params.<path>`
  位置(不含值),須先以管理 API 修正。匯入備份走 `configio.dump`,不經此掃描。
- Host/Origin 由 app 層 middleware 照常檢查(9.1.4)。

`POST /api/config/import` 由 T4 加在本模組。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from spillway import configio
from spillway.configio import utc_stamp_filename
from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.store import repo

from .common import router
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
