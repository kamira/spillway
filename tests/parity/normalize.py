"""跨實作比對的擷取型別、遮罩、正規化與比較(CHG-20261108-01 T3;spec 15;AM-6、AM-7、AM-8)。不 import spillway。

處理順序(spec 15.10.1):① 原始擷取留在記憶體(`Capture`);② sentinel 掃描與「遮罩前」形狀驗證(`scan_capture`);③ 遮罩
(`mask_capture`);④ 15.9 正規化(`normalize_capture`);⑤ 比較(`compare_views`)。`compare_captures` 依序跑完五步,回傳 `Report`。

規則只依欄位路徑(`PATH_RULES`),不依值的樣式。本檔的表格常數由 `tests/parity/test_spec_table.py` 與 `docs/spec.md` 第 15 節比對:
`SURFACES`(15.2)、`REQUIRED_UPSTREAM_HEADERS` 與 `IGNORED_UPSTREAM_HEADERS`(15.3)、`CONTRACT_HEADERS`(15.4)、`DB_TABLES`
與 `DB_COLUMN_RULES`(15.8)、`PATH_RULES`(15.9.5)。shape 與 treatment 欄位是 spec 表格的原文。

`Report` 只含比對面、位置與路徑,不含任何值(15.10.4);路徑本身也再遮罩一次(JSON 鍵可能帶秘密)。
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from tests.blackbox.mask import MASK, mask_secrets

__all__ = [
    "CONTRACT_HEADERS", "DB_COLUMN_RULES", "DB_TABLES", "IGNORED_UPSTREAM_HEADERS", "PATH_RULES",
    "REQUIRED_UPSTREAM_HEADERS", "SURFACES", "UNDEFINED_SURFACES", "Capture", "Difference", "Exchange", "ParsedJson",
    "PathRule", "Report", "compare_captures", "compare_views", "diff_values", "mask_capture", "normalize_capture",
    "parse_strict", "route_of", "scan_capture", "sentinels_of",
]

# ---------------------------------------------------------------- 15.2 比對面

STATUS, HEADERS, JSON_BODY, ERRORS, DB_ROWS, UPSTREAM = "status", "headers", "json_body", "errors", "db_rows", "upstream_chat"
LOG = "log"   # 不是比對面(15.2):只檢查等級格式與 sentinel
# spec 表格的比對面名稱 → 情境名稱(同時是本檔的比對面代號)
SURFACES = {"HTTP 狀態碼": STATUS, "契約 header": HEADERS, "JSON body": JSON_BODY, "錯誤物件": ERRORS,
            "DB 資料列": DB_ROWS, "API 上游請求": UPSTREAM}
UNDEFINED_SURFACES = ("CLI 呼叫", "MCP 訊息序列與 `mcp-stdio`", "CSV 與 YAML 匯出", "花費與 token 估算")

# ---------------------------------------------------------------- 15.3 上游請求

REQUIRED_UPSTREAM_HEADERS = {
    ("POST /v1/chat/completions", True): ("authorization", "content-type"),
    ("POST /v1/messages", True): ("x-api-key", "content-type"),
    ("POST", False): ("content-type",),
    ("GET", None): (),
}
IGNORED_UPSTREAM_HEADERS = ("user-agent", "x-stainless-*", "accept", "accept-encoding", "connection", "content-length", "host")
IGNORED_WITHOUT_CREDENTIAL = ("authorization", "x-api-key")

# ---------------------------------------------------------------- 15.4 契約 header

CHAT_PATH = "/v1/chat/completions"
EST_COST_RE = re.compile(r"^([0-9]+\.[0-9]{8})?$")
# (spec 表格的 header 名稱, 適用路徑(None=全部), 比對方式)
CONTRACT_HEADERS = (
    ("X-Spillway-Served-By", CHAT_PATH, "verbatim"),
    ("X-Spillway-Slot", CHAT_PATH, "verbatim"),
    ("X-Spillway-Est-Cost", CHAT_PATH, "est_cost"),
    ("cache-control", None, "verbatim_except"),
    ("x-content-type-options", None, "verbatim_except"),
)
HOST_ORIGIN_CODES = ("host_not_allowed", "origin_not_allowed")

# ---------------------------------------------------------------- 15.8 資料庫

DB_TABLES = ("deployments", "deployment_tags", "budgets", "cooldowns", "groups", "routes", "route_targets", "usage_ledger",
             "usage_payloads", "settings")
# (表.欄 或 settings 的鍵) → 方式;鍵與 spec 15.8.3 表格的欄位一一對應
DB_COLUMN_RULES = {
    "deployments.params": "api_base_literal",
    "deployments.capabilities": "literal",
    "groups.selector": "literal",
    "routes.target": "literal",
    "routes.tags": "literal",
    "usage_payloads.request_json": "payload_request",
    "usage_payloads.response_text": "literal",
    "usage_payloads.ledger_id": "integer",
    "usage_payloads.bytes": "integer",
    "usage_payloads.truncated": "integer",
    "usage_payloads.redacted": "integer",
    "usage_payloads.ts": "literal",
    "settings.access_token_sha256": "sha256",
    "settings.*": "literal",
    "usage_ledger.latency_ms": "latency",
    "usage_ledger.error": "presence",
    "usage_ledger.cost_usd": "type_only",
    "usage_ledger.est_tokens_in": "type_only",
    "usage_ledger.est_tokens_out_p50": "type_only",
    "usage_ledger.est_tokens_out_p90": "type_only",
    "usage_ledger.est_cost_usd_p90": "type_only",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------- 15.7 錯誤

FIELD_PATH_RE = re.compile(r"^(body|query|path|header|cookie)(\.[^\s.:;]+)*$")
MODE_MALFORMED = "malformed"     # 語料標記:畸形 JSON,只比 15.7.1(15.7.3)
MODE_UNKNOWN_PATH = "unknown_path"   # 語料標記:13.1.4 不存在路徑的 404,訊息逐字(15.7.2)

# ---------------------------------------------------------------- 15.9.5 路徑


@dataclass(frozen=True)
class PathRule:
    surface: str     # 例如 `POST /v1/chat/completions`;資料庫列為 `DB`
    path: str
    kind: str
    shape: str       # spec 表格「形狀或型別」欄原文
    treatment: str   # spec 表格「處理」欄原文


INT, NO_REPLACE = "整數", "不替換"
INT_OR_NULL = "整數或 null"
PORT_SHAPE, HOME_SHAPE = "整數,等於本次埠", "等於本次 `SPILLWAY_HOME`"
DB_SEE, DB_SAME = "見 DB 列", "同 DB 列"
LATENCY_SHAPE, PRESENCE_SHAPE = "15.6.2", "15.7.4"
NUMBER_ONLY = "待定義期間只比 null 或數值"
INT_ONLY = "待定義期間只比 null 或整數"
PAYLOAD_SHAPE = "同 15.8.3 同名欄位"
URL_SHAPE = r"`^http://127\.0\.0\.1:<本次埠>/mcp/`"
URL_TREATMENT = "只把埠換成 `<port>`"
STDIO_PORT_SHAPE = "等於本次埠"

CHAT = "POST /v1/chat/completions"
MODELS = "GET /v1/models"
ROTATE = "POST /api/settings/token/rotate"
SETTINGS = "GET /api/settings"
DEP_LIST = "GET /api/deployments"
DEP_GET, DEP_PUT, DEP_POST = "GET /api/deployments/{id}", "PUT /api/deployments/{id}", "POST /api/deployments"
DEP_TEST = "POST /api/deployments/{id}/test"
LEDGER = "GET /api/ledger"
PAYLOAD = "GET /api/ledger/{id}/payload"
CATALOG = "GET /api/mcp/catalog"
DB_SURFACE = "DB"


def _rules(surface: str, paths: Iterable[str], kind: str, shape: str, treatment: str) -> list[PathRule]:
    return [PathRule(surface, p, kind, shape, treatment) for p in paths]


PATH_RULES: tuple[PathRule, ...] = (
    PathRule(CHAT, "$.id", "chat_id", "`^chatcmpl-[0-9a-f]{32}$`", "`<id-n>`"),
    *_rules(CHAT, ["$.created", "$.choices[*].index", "$.usage.prompt_tokens", "$.usage.completion_tokens",
                   "$.usage.total_tokens"], "int", INT, NO_REPLACE),
    PathRule(MODELS, "$.data[*].created", "int", INT, NO_REPLACE),
    PathRule(ROTATE, "$.token", "token", "遮罩前 `^[A-Za-z0-9_-]{43}$`;遮罩後必須為 `[REDACTED]`", "不另替換"),
    PathRule(SETTINGS, "$.port", "port", PORT_SHAPE, "`<port>`"),
    PathRule(SETTINGS, "$.data_dir", "home", HOME_SHAPE, "`<home>`"),
    PathRule(SETTINGS, "$.version", "version", r"`^[0-9]+\.[0-9]+\.[0-9]+`", "`<version>`"),
    *_rules(SETTINGS, ["$.counts.*", "$.context.context_retention_days", "$.context.context_max_bytes",
                       "$.context.payload_count", "$.context.payload_bytes"], "int", INT, NO_REPLACE),
    PathRule(DEP_LIST, "$.deployments[*].params.api_base", "api_base", DB_SEE, DB_SAME),
    *[PathRule(s, "$.params.api_base", "api_base", DB_SEE, DB_SAME) for s in (DEP_GET, DEP_PUT, DEP_POST)],
    PathRule(DEP_TEST, "$.latency_ms", "latency", LATENCY_SHAPE, NO_REPLACE),
    PathRule(DEP_TEST, "$.error", "presence", PRESENCE_SHAPE, NO_REPLACE),
    PathRule(DEP_TEST, "$.cost_usd", "number_only", NUMBER_ONLY, NO_REPLACE),
    *_rules(DEP_TEST, ["$.ledger_id", "$.tokens_in", "$.tokens_out"], "int_or_null", INT_OR_NULL, NO_REPLACE),
    PathRule(LEDGER, "$.items[*].latency_ms", "latency", LATENCY_SHAPE, NO_REPLACE),
    PathRule(LEDGER, "$.items[*].error", "presence", PRESENCE_SHAPE, NO_REPLACE),
    *_rules(LEDGER, ["$.items[*].cost_usd", "$.items[*].est_cost_usd_p90"], "number_only", NUMBER_ONLY, NO_REPLACE),
    *_rules(LEDGER, ["$.items[*].est_tokens_in", "$.items[*].est_tokens_out_p50", "$.items[*].est_tokens_out_p90"],
            "int_only", INT_ONLY, NO_REPLACE),
    *_rules(LEDGER, ["$.items[*].id", "$.items[*].tokens_in", "$.items[*].tokens_out", "$.items[*].slot",
                     "$.items[*].fallback_depth", "$.page", "$.page_size", "$.total", "$.pages"],
            "int_or_null", INT_OR_NULL, NO_REPLACE),
    PathRule(PAYLOAD, "$.request_json", "payload_request", PAYLOAD_SHAPE, NO_REPLACE),
    PathRule(PAYLOAD, "$.response_text", "verbatim", PAYLOAD_SHAPE, NO_REPLACE),
    PathRule(CATALOG, "$.connection.url", "mcp_url", URL_SHAPE, URL_TREATMENT),
    PathRule(CATALOG, "$.connection.groups[*].url", "mcp_url", URL_SHAPE, URL_TREATMENT),
    PathRule(CATALOG, "$.connection.stdio.args[*]", "stdio_port", STDIO_PORT_SHAPE, "`<port>`"),
    PathRule(CATALOG, "$.connection.stdio.command", "stdio_command", "`^spillway(-rs)?$`", "`<cmd>`(15.11)"),
    PathRule(DB_SURFACE, "deployments.params$.api_base", "api_base",
             "值以本次 fake `base_url` 開頭時替換;其他值不替換,依 literal 比對", "把該字首換成 `<fake-base-url>`"),
)

CHAT_ID_RE = re.compile(r"^chatcmpl-[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+")
STDIO_COMMAND_RE = re.compile(r"^spillway(-rs)?$")
LOG_LINE_RE = re.compile(r"^SPILLWAY-LOG (DEBUG|INFO|WARNING|ERROR|CRITICAL) \S+( |$)")
LOG_PREFIX, LOG_CONT_PREFIX = "SPILLWAY-LOG ", "SPILLWAY-LOG-CONT "

_ROUTE_TABLE: list[tuple[str, re.Pattern[str]]] = []
for _surface in sorted({r.surface for r in PATH_RULES if r.surface != DB_SURFACE}):
    _m, _p = _surface.split(" ", 1)
    _ROUTE_TABLE.append((_surface, re.compile("^" + _m + " " + re.sub(r"\\\{id\\\}", "[^/]+", re.escape(_p)) + "$")))


def route_of(method: str, path: str) -> str | None:
    """`(方法, 路徑)` 對應的 15.9.5 面;沒有對應列時回 None。查詢字串不算路徑。"""
    target = f"{method.upper()} {path.split('?', 1)[0]}"
    return next((s for s, rx in _ROUTE_TABLE if rx.match(target)), None)


# ---------------------------------------------------------------- 型別


class StrictJsonError(ValueError):
    """JSON 不合法(含 NaN、Infinity)。"""


def _reject_constant(name: str) -> None:
    raise StrictJsonError(f"非標準 JSON 數值:{name}")


def parse_strict(data: bytes | str) -> Any:
    """解析 JSON;`NaN`、`Infinity`、`-Infinity` 視為不合法(`StrictJsonError`)。"""
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except StrictJsonError:
        raise
    except ValueError as exc:   # JSONDecodeError、UnicodeDecodeError
        raise StrictJsonError(type(exc).__name__) from None


@dataclass(frozen=True)
class Exchange:
    """一個 HTTP 回應的原始擷取。`error_mode` 由語料標記(15.7.2、15.7.3)。"""

    method: str
    path: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    error_mode: str = ""

    @property
    def location(self) -> str:
        return f"{self.method} {self.path.split('?', 1)[0]}"


@dataclass
class Capture:
    """單側單一情境的原始擷取(只留在記憶體;15.10.1 ①)。"""

    scenario: str
    port: int
    home: str
    fake_base_url: str
    corpus_keys: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    exchanges: list[Exchange] = field(default_factory=list)
    upstream: list[Any] = field(default_factory=list)   # tests.fakes.api_upstream.RecordedRequest
    db: dict[str, list[dict[str, tuple[str, Any]]]] = field(default_factory=dict)   # 表 → 列(欄 → (typeof, 值))
    user_version: int | None = None
    log_lines: list[str] = field(default_factory=list)
    model_credentials: dict[str, bool] = field(default_factory=dict)   # 上游 model → 成員是否有 credential_ref
    seeded_param_ids: frozenset[str] = frozenset()   # 以 13.4.5 種入 params 的成員


@dataclass(frozen=True, order=True)
class Difference:
    surface: str
    location: str
    path: str
    kind: str
    side: str = ""

    def __str__(self) -> str:
        side = f" [{self.side}]" if self.side else ""
        return f"{self.surface} {self.location} {self.path} ({self.kind}){side}"


@dataclass
class Report:
    scenario: str
    differences: list[Difference]

    @property
    def ok(self) -> bool:
        return not self.differences

    def text(self) -> str:
        head = f"{self.scenario}: {len(self.differences)} 項差異"
        return "\n".join([head, *(f"  {d}" for d in sorted(self.differences))])


class ParsedJson:
    """payload `request_json` 解析後的值(15.8.3):與未解析的字串型別不同,所以兩側一邊能解析、一邊不能時判不一致。"""

    def __init__(self, value: Any) -> None:
        self.value = value


# ---------------------------------------------------------------- 15.5 相等


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):      # bool 先於數值排除(bool 是 int 的子類別)
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, ParsedJson):
        return "parsed-json"
    raise TypeError(type(v).__name__)


def diff_values(a: Any, b: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """15.5:回傳 `(路徑, 種類)`;不含值。"""
    ta, tb = _json_type(a), _json_type(b)
    if ta != tb:
        yield path, "type"
    elif ta == "object":
        for key in sorted({*a, *b}):
            child = f"{path}.{key}"
            if key not in a or key not in b:
                yield child, "missing_key"
            else:
                yield from diff_values(a[key], b[key], child)
    elif ta == "array":
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff_values(x, y, f"{path}[{i}]")
        for i in range(min(len(a), len(b)), max(len(a), len(b))):
            yield f"{path}[{i}]", "missing_item"
    elif ta == "parsed-json":
        yield from diff_values(a.value, b.value, path)
    elif a != b:
        yield path, "value"


# ---------------------------------------------------------------- 路徑走訪


def _tokens(path: str) -> list[str]:
    assert path.startswith("$"), path
    return re.findall(r"\[\*\]|[^.\[\]$]+|\*", path)


def _visit(node: Any, tokens: Sequence[str], prefix: str, parent: Any, key: Any) -> Iterator[tuple[Any, Any, str]]:
    """依路徑走訪,產生 `(父節點, 鍵或索引, 實際路徑)`;路徑不存在時不產生(缺鍵由 15.5 比對)。`*` 依鍵排序。"""
    if not tokens:
        yield parent, key, prefix
        return
    head, rest = tokens[0], tokens[1:]
    if head == "[*]":
        if isinstance(node, list):
            for i, item in enumerate(node):
                yield from _visit(item, rest, f"{prefix}[{i}]", node, i)
    elif head == "*":
        if isinstance(node, dict):
            for k in sorted(node):
                yield from _visit(node[k], rest, f"{prefix}.{k}", node, k)
    elif isinstance(node, dict) and head in node:
        yield from _visit(node[head], rest, f"{prefix}.{head}", node, head)


# ---------------------------------------------------------------- ② 掃描


def sentinels_of(capture: Capture) -> tuple[str, ...]:
    """15.10.3:語料金鑰值與本次 token 明文。"""
    return tuple(s for s in (*capture.corpus_keys, *capture.tokens) if s)


def _contains(text: str, sentinels: Sequence[str]) -> bool:
    return any(s in text for s in sentinels)


def _lower_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for name, value in headers:
        out.setdefault(name.lower(), []).append(value)
    return {k: ", ".join(v) for k, v in out.items()}


def _is_error_shape(body: Any) -> bool:
    """9.1.2:`{"error": {"code": str, "message": str}}`。"""
    err = body.get("error") if isinstance(body, dict) else None
    return isinstance(err, dict) and isinstance(err.get("code"), str) and isinstance(err.get("message"), str)


def _content_type(ex: Exchange) -> str:
    return _lower_headers(ex.headers).get("content-type", "")


def _parse_body(ex: Exchange) -> tuple[str, Any]:
    """`("json", 值)`、`("invalid", None)`(宣告 JSON 但不合法)、`("other", None)`(非 JSON,15.7.6)。"""
    if not ex.body or "json" not in _content_type(ex).lower():
        return "other", None
    try:
        return "json", parse_strict(ex.body)
    except StrictJsonError:
        return "invalid", None


def _exempt_paths(ex: Exchange, body: Any, capture: Capture) -> tuple[str, ...]:
    """15.10.3 不掃描的位置(路徑前綴)。"""
    surface = route_of(ex.method, ex.path)
    if surface == ROTATE:
        return ("$.token",)
    exempt: list[str] = []
    if surface == DEP_LIST and isinstance(body, dict) and isinstance(body.get("deployments"), list):
        for i, item in enumerate(body["deployments"]):
            if isinstance(item, dict) and item.get("id") in capture.seeded_param_ids:
                exempt.append(f"$.deployments[{i}].params")
    elif surface in (DEP_GET, DEP_PUT, DEP_POST) and isinstance(body, dict) and body.get("id") in capture.seeded_param_ids:
        exempt.append("$.params")
    return tuple(exempt)


def _is_exempt(path: str, exempt: Sequence[str]) -> bool:
    return any(path == p or path.startswith(p + ".") or path.startswith(p + "[") for p in exempt)


def _scan_json(value: Any, path: str, sentinels: Sequence[str], exempt: Sequence[str]) -> Iterator[str]:
    if _is_exempt(path, exempt):
        return
    if isinstance(value, str):
        if _contains(value, sentinels):
            yield path
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _scan_json(item, f"{path}[{i}]", sentinels, exempt)
    elif isinstance(value, dict):
        for k in sorted(value):
            child = f"{path}.{k}"
            if _contains(k, sentinels):
                yield child
            else:
                yield from _scan_json(value[k], child, sentinels, exempt)


def _body_surface(body: Any) -> str:
    return ERRORS if _is_error_shape(body) else JSON_BODY


def scan_capture(capture: Capture, side: str) -> list[Difference]:
    """15.10.1 ②:sentinel 掃描(HTTP 回應的 header 與 body、DB 全部 TEXT 欄、log 行)與標「遮罩前」的形狀。"""
    sentinels = sentinels_of(capture)
    out: list[Difference] = []
    raw_sentinels = [s.encode("utf-8") for s in sentinels]
    for i, ex in enumerate(capture.exchanges):
        loc = f"response[{i}] {ex.location}"
        for name, value in ex.headers:
            if _contains(name, sentinels) or _contains(value, sentinels):
                out.append(Difference(HEADERS, loc, name.lower(), "sentinel", side))
        kind, body = _parse_body(ex)
        if kind == "json":
            for path in _scan_json(body, "$", sentinels, _exempt_paths(ex, body, capture)):
                out.append(Difference(_body_surface(body), loc, path, "sentinel", side))
            for rule in (r for r in PATH_RULES if r.kind == "token" and r.surface == route_of(ex.method, ex.path)):
                for parent, key, actual in _visit(body, _tokens(rule.path), "$", None, None):
                    if not (isinstance(parent[key], str) and TOKEN_RE.match(parent[key])):
                        out.append(Difference(JSON_BODY, loc, actual, "shape", side))
        elif any(s in ex.body for s in raw_sentinels):
            out.append(Difference(JSON_BODY, loc, "$", "sentinel", side))
    for table, rows in capture.db.items():
        for r, row in enumerate(rows):
            for col, (typeof, value) in row.items():
                if typeof == "text" and isinstance(value, str) and _contains(value, sentinels):
                    if table == "deployments" and col == "params" and row.get("id", ("", None))[1] in capture.seeded_param_ids:
                        continue
                    out.append(Difference(DB_ROWS, f"{table}[{r}]", col, "sentinel", side))
    for i, line in enumerate(capture.log_lines):
        if line.startswith(LOG_CONT_PREFIX):
            pass
        elif line.startswith(LOG_PREFIX):
            if not LOG_LINE_RE.match(line):
                out.append(Difference(LOG, f"stderr[{i}]", "$", "log_format", side))
        else:
            continue
        if _contains(line, sentinels):
            out.append(Difference(LOG, f"stderr[{i}]", "$", "sentinel", side))
    return out


# ---------------------------------------------------------------- ③ 遮罩


def _mask_json(value: Any, extra: Sequence[str]) -> Any:
    if isinstance(value, str):
        return mask_secrets(value, extra)
    if isinstance(value, list):
        return [_mask_json(v, extra) for v in value]
    if isinstance(value, dict):
        return {mask_secrets(k, extra): _mask_json(v, extra) for k, v in value.items()}
    return value


@dataclass
class MaskedExchange:
    index: int
    location: str
    surface: str | None
    status: int
    headers: dict[str, str]
    kind: str     # json | invalid | other
    body: Any
    error_mode: str
    method: str = ""
    path: str = ""
    is_error: bool = False     # 正規化前判定的 9.1.2 形狀(`error.message` 正規化後不再是字串)


@dataclass
class MaskedCapture:
    source: Capture
    exchanges: list[MaskedExchange]
    upstream: list[dict[str, Any]]
    db: dict[str, list[dict[str, tuple[str, Any]]]]
    user_version: int | None


def mask_capture(capture: Capture) -> MaskedCapture:
    """15.10.1 ③:所有擷取面過 `mask_secrets`;`extra` 為語料金鑰值與本次 token 明文。JSON 先解析,逐字串遮罩(不破壞跳脫)。"""
    extra = sentinels_of(capture)
    exchanges = []
    for i, ex in enumerate(capture.exchanges):
        kind, body = _parse_body(ex)
        exchanges.append(MaskedExchange(
            i, f"response[{i}] {ex.location}", route_of(ex.method, ex.path), ex.status,
            {k: mask_secrets(v, extra) for k, v in _lower_headers(ex.headers).items()},
            kind, _mask_json(body, extra) if kind == "json" else None, ex.error_mode, ex.method, ex.path))
    upstream = []
    for rec in capture.upstream:
        upstream.append({
            "method": rec.method, "path": rec.path, "query_names": tuple(rec.query_names),
            "header_names": tuple(rec.header_names), "has_json": rec.has_json,
            "json_body": _mask_json(rec.json_body, extra) if rec.has_json else None,
            "body_length": rec.body_length, "model": rec.body.get("model") if isinstance(rec.body, dict) else None,
        })
    db = {t: [{c: (ty, mask_secrets(v, extra) if isinstance(v, str) else v) for c, (ty, v) in row.items()} for row in rows]
          for t, rows in capture.db.items()}
    return MaskedCapture(capture, exchanges, upstream, db, capture.user_version)


# ---------------------------------------------------------------- ④ 正規化


@dataclass
class View:
    """正規化後、可比較的一側。"""

    responses: list[MaskedExchange]
    upstream: list[dict[str, Any]]
    db: dict[str, list[dict[str, tuple[str, Any]]]]
    user_version: int | None


class _Ctx:
    def __init__(self, capture: Capture, side: str) -> None:
        self.port = capture.port
        self.home = capture.home
        self.fake_base_url = capture.fake_base_url
        self.side = side
        self.numbers: dict[str, str] = {}
        self.violations: list[Difference] = []

    def number(self, value: str) -> str:
        return self.numbers.setdefault(value, f"<id-{len(self.numbers) + 1}>")

    def shape(self, surface: str, location: str, path: str) -> None:
        self.violations.append(Difference(surface, location, path, "shape", self.side))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _replace_prefix(value: str, prefix: str, replacement: str) -> str:
    return replacement + value[len(prefix):] if value.startswith(prefix) else value


def _normalize_payload_request(value: Any, truncated: Any) -> Any:
    """15.8.3 `request_json`:`truncated = 0` 且能解析時解析後依 15.5 比對,其餘逐字。"""
    if not isinstance(value, str) or truncated not in (0, False):
        return value
    try:
        return ParsedJson(parse_strict(value))
    except StrictJsonError:
        return value


_MCP_URL_TAIL = "/mcp/"


def _apply_kind(kind: str, value: Any, parent: Any, key: Any, ctx: _Ctx) -> tuple[bool, Any]:
    """回傳 `(形狀是否符合, 新值)`;不符合時新值就是原值(15.9.2:形狀不符不替換)。"""
    if kind == "chat_id":
        return (True, ctx.number(value)) if isinstance(value, str) and CHAT_ID_RE.match(value) else (False, value)
    if kind == "int":
        return _is_int(value), value
    if kind == "int_or_null":
        return value is None or _is_int(value), value
    if kind == "token":
        return value == MASK, value
    if kind == "port":
        return (True, "<port>") if _is_int(value) and value == ctx.port else (False, value)
    if kind == "home":
        return (True, "<home>") if value == ctx.home else (False, value)
    if kind == "version":
        return (True, "<version>") if isinstance(value, str) and VERSION_RE.match(value) else (False, value)
    if kind == "api_base":
        return True, _replace_prefix(value, ctx.fake_base_url, "<fake-base-url>") if isinstance(value, str) else value
    if kind == "latency":   # 15.6.2
        if value is None:
            return True, None
        return (True, "<latency>") if _is_int(value) and value >= 0 else (False, value)
    if kind == "presence":   # 15.7.4
        if value is None:
            return True, None
        return (True, "<text>") if isinstance(value, str) and value else (False, value)
    if kind == "number_only":
        if value is None:
            return True, None
        return (True, "<number>") if _is_number(value) else (False, value)
    if kind == "int_only":
        if value is None:
            return True, None
        return (True, "<integer>") if _is_int(value) else (False, value)
    if kind == "payload_request":
        return True, _normalize_payload_request(value, parent.get("truncated"))
    if kind == "verbatim":
        return True, value
    if kind == "mcp_url":
        prefix = f"http://127.0.0.1:{ctx.port}{_MCP_URL_TAIL}"
        if isinstance(value, str) and value.startswith(prefix):
            return True, f"http://127.0.0.1:<port>{_MCP_URL_TAIL}" + value[len(prefix):]
        return False, value
    if kind == "stdio_port":
        if isinstance(key, int) and key > 0 and parent[key - 1] == "--port":
            return (True, "<port>") if value == str(ctx.port) else (False, value)
        return True, value
    if kind == "stdio_command":
        return (True, "<cmd>") if isinstance(value, str) and STDIO_COMMAND_RE.match(value) else (False, value)
    raise AssertionError(f"未知的 kind:{kind}")


def _normalize_error(body: dict[str, Any], status: int, mode: str, surface: str, location: str, ctx: _Ctx) -> None:
    """15.7:改寫 `$.error.message`。其他鍵依 15.5。"""
    err = body["error"]
    message, code = err["message"], err["code"]
    if mode == MODE_MALFORMED:
        err["message"] = "<any>"
    elif code == "no_candidate" or mode == MODE_UNKNOWN_PATH:
        pass   # 15.7.2 逐字
    elif status == 422:
        err["message"] = sorted({seg.split(": ", 1)[0] for seg in message.split("; ")
                                 if ": " in seg and FIELD_PATH_RE.fullmatch(seg.split(": ", 1)[0])})
    elif message:
        err["message"] = "<text>"   # 15.7.4、15.7.5:只比非空字串
    else:
        ctx.shape(ERRORS, location, "$.error.message")


def _normalize_headers(ex: MaskedExchange, ctx: _Ctx) -> dict[str, str]:
    out: dict[str, str] = {}
    code = ex.body["error"]["code"] if _is_error_shape(ex.body) else None
    route_path = ex.path.split("?", 1)[0]
    excepted = ex.status >= 500 or (ex.status == 403 and code in HOST_ORIGIN_CODES)
    for name, scope, method in CONTRACT_HEADERS:
        if scope is not None and route_path != scope:
            continue
        if method == "verbatim_except" and excepted:
            continue
        low = name.lower()
        if low not in ex.headers:
            continue
        value = ex.headers[low]
        if method == "est_cost":
            if EST_COST_RE.match(value):
                value = "<est-cost>"
            else:
                ctx.shape(HEADERS, ex.location, low)
        out[low] = value
    return out


def _normalize_exchange(ex: MaskedExchange, ctx: _Ctx) -> MaskedExchange:
    body = json.loads(json.dumps(ex.body)) if ex.kind == "json" else None
    headers = _normalize_headers(ex, ctx)
    if ex.kind == "invalid":
        ctx.shape(JSON_BODY, ex.location, "$")
    is_error = _is_error_shape(body)
    if is_error:
        _normalize_error(body, ex.status, ex.error_mode, ex.surface or "", ex.location, ctx)
    elif ex.surface is not None:
        for rule in PATH_RULES:
            if rule.surface != ex.surface:
                continue
            for parent, key, actual in list(_visit(body, _tokens(rule.path), "$", None, None)):
                ok, new = _apply_kind(rule.kind, parent[key], parent, key, ctx)
                if ok:
                    parent[key] = new
                else:
                    ctx.shape(JSON_BODY, ex.location, actual)
    return MaskedExchange(ex.index, ex.location, ex.surface, ex.status, headers, ex.kind, body, ex.error_mode, ex.method, ex.path,
                          is_error)


def _api_base_literal(value: Any, ctx: _Ctx, location: str, col: str) -> Any:
    """15.8.3 `deployments.params`:先替換 `$.api_base` 的 fake 字首,其餘逐位元組(literal)。"""
    if not isinstance(value, str):
        return value
    try:
        data = parse_strict(value)
    except StrictJsonError:
        return value
    base = data.get("api_base") if isinstance(data, dict) else None
    if not isinstance(base, str) or not base.startswith(ctx.fake_base_url):
        return value
    needle = '"api_base": ' + json.dumps(base, ensure_ascii=False)
    if value.count(needle) != 1:
        ctx.shape(DB_ROWS, location, f"{col}.$.api_base")
        return value
    return value.replace(needle, '"api_base": ' + json.dumps(_replace_prefix(base, ctx.fake_base_url, "<fake-base-url>"),
                                                             ensure_ascii=False))


def _normalize_cell(table: str, col: str, row: dict[str, tuple[str, Any]], location: str, ctx: _Ctx) -> tuple[str, Any]:
    typeof, value = row[col]
    rule = DB_COLUMN_RULES.get(f"{table}.{col}")
    if table == "settings" and col == "value":
        key = row["key"][1]
        if f"settings.{key}" in DB_COLUMN_RULES:
            if not (isinstance(value, str) and SHA256_RE.match(value)):
                ctx.shape(DB_ROWS, location, col)
                return typeof, value
            return typeof, "<sha256>"
        return typeof, value    # literal
    if rule == "api_base_literal":
        return typeof, _api_base_literal(value, ctx, location, col)
    if rule == "payload_request":
        return typeof, _normalize_payload_request(value, row.get("truncated", ("", None))[1])
    if rule == "integer":
        if typeof != "integer":
            ctx.shape(DB_ROWS, location, col)
        return typeof, value
    if rule == "latency":
        if value is None:
            return typeof, None
        if _is_int(value) and value >= 0:
            return typeof, "<latency>"
        ctx.shape(DB_ROWS, location, col)
        return typeof, value
    if rule == "presence":
        if value is None:
            return typeof, None
        if isinstance(value, str) and value:
            return typeof, "<text>"
        ctx.shape(DB_ROWS, location, col)
        return typeof, value
    if rule == "type_only":
        return typeof, "<type-only>"
    return typeof, value    # literal 或未列欄位:typeof 相同且值相等


def normalize_capture(masked: MaskedCapture, side: str) -> tuple[View, list[Difference]]:
    """15.10.1 ④:只改列在 15.9.5 的路徑。編號範圍為單一情境、依回應順序(15.9.3)。"""
    ctx = _Ctx(masked.source, side)
    responses = [_normalize_exchange(ex, ctx) for ex in masked.exchanges]
    db: dict[str, list[dict[str, tuple[str, Any]]]] = {}
    for table, rows in masked.db.items():
        db[table] = [{col: _normalize_cell(table, col, row, f"{table}[{r}]", ctx) for col in row} for r, row in enumerate(rows)]
    return View(responses, masked.upstream, db, masked.user_version), ctx.violations


# ---------------------------------------------------------------- ⑤ 比較


def _credential_of(rec: dict[str, Any], credentials: dict[str, bool]) -> bool | None:
    if rec["method"] == "GET":
        return None
    return credentials.get(rec["model"])


def _required_headers(rec: dict[str, Any], credential: bool | None) -> tuple[str, ...]:
    if rec["method"] == "GET":
        return REQUIRED_UPSTREAM_HEADERS[("GET", None)]
    if credential:
        return REQUIRED_UPSTREAM_HEADERS.get((f"{rec['method']} {rec['path']}", True), ())
    return REQUIRED_UPSTREAM_HEADERS[("POST", False)]


def _compared_headers(rec: dict[str, Any], credential: bool | None) -> set[str]:
    names = set(rec["header_names"])
    ignored = set()
    for pattern in IGNORED_UPSTREAM_HEADERS:
        ignored |= {n for n in names if (n.startswith(pattern[:-1]) if pattern.endswith("*") else n == pattern)}
    if credential is False:
        ignored |= set(IGNORED_WITHOUT_CREDENTIAL)
    return names - ignored


def _compare_upstream(a: View, b: View, ca: dict[str, bool], cb: dict[str, bool], out: list[Difference]) -> None:
    if len(a.upstream) != len(b.upstream):
        out.append(Difference(UPSTREAM, "upstream", "count", "count"))
    for i, (x, y) in enumerate(zip(a.upstream, b.upstream)):
        loc = f"upstream[{i}]"
        for f in ("method", "path"):
            if x[f] != y[f]:
                out.append(Difference(UPSTREAM, loc, f, "value"))
        if set(x["query_names"]) != set(y["query_names"]):
            out.append(Difference(UPSTREAM, loc, "query_names", "value"))
        for rec, credentials, side in ((x, ca, "a"), (y, cb, "b")):
            if rec["method"] != "GET" and rec["model"] not in credentials:
                out.append(Difference(UPSTREAM, loc, "$.model", "unknown_member", side))
        cred_x, cred_y = _credential_of(x, ca), _credential_of(y, cb)
        for rec, credential, side in ((x, cred_x, "a"), (y, cred_y, "b")):
            for name in _required_headers(rec, credential):
                if name not in rec["header_names"]:
                    out.append(Difference(UPSTREAM, loc, f"header:{name}", "required_missing", side))
        hx, hy = _compared_headers(x, cred_x), _compared_headers(y, cred_y)
        out.extend(Difference(UPSTREAM, loc, f"header:{n}", "one_side_only") for n in sorted(hx ^ hy))
        if x["has_json"] != y["has_json"]:
            out.append(Difference(UPSTREAM, loc, "$", "type"))
        elif x["has_json"]:
            out.extend(Difference(UPSTREAM, loc, p, k) for p, k in diff_values(x["json_body"], y["json_body"]))
        elif x["body_length"] != y["body_length"]:
            out.append(Difference(UPSTREAM, loc, "length", "value"))


def _compare_db(a: View, b: View, out: list[Difference]) -> None:
    if a.user_version != b.user_version:
        out.append(Difference(DB_ROWS, "pragma", "user_version", "value"))
    for table in sorted({*a.db, *b.db}):
        ra, rb = a.db.get(table), b.db.get(table)
        if ra is None or rb is None:
            out.append(Difference(DB_ROWS, table, "table", "missing_key"))
            continue
        if len(ra) != len(rb):
            out.append(Difference(DB_ROWS, table, "row_count", "count"))
        for r, (x, y) in enumerate(zip(ra, rb)):
            loc = f"{table}[{r}]"
            for col in sorted({*x, *y}):
                if col not in x or col not in y:
                    out.append(Difference(DB_ROWS, loc, col, "missing_key"))
                    continue
                (tx, vx), (ty, vy) = x[col], y[col]
                if tx != ty:
                    out.append(Difference(DB_ROWS, loc, col, "typeof"))
                elif isinstance(vx, ParsedJson) or isinstance(vy, ParsedJson):
                    out.extend(Difference(DB_ROWS, loc, f"{col}{p[1:]}", k) for p, k in diff_values(vx, vy))
                elif vx != vy:
                    out.append(Difference(DB_ROWS, loc, col, "value"))


def compare_views(a: View, b: View, ca: dict[str, bool], cb: dict[str, bool]) -> list[Difference]:
    """15.10.1 ⑤。`ca`、`cb` 為兩側的 上游 model → 是否有 `credential_ref`。"""
    out: list[Difference] = []
    if len(a.responses) != len(b.responses):
        out.append(Difference(STATUS, "responses", "count", "count"))
    for x, y in zip(a.responses, b.responses):
        loc = x.location
        if x.status != y.status:
            out.append(Difference(STATUS, loc, "status", "value"))
        for name in sorted({*x.headers, *y.headers}):
            if name not in x.headers or name not in y.headers:
                out.append(Difference(HEADERS, loc, name, "missing_key"))
            elif x.headers[name] != y.headers[name]:
                out.append(Difference(HEADERS, loc, name, "value"))
        if x.kind != y.kind:
            out.append(Difference(JSON_BODY, loc, "$", "type"))
        elif x.kind == "json":
            surface = ERRORS if x.is_error or y.is_error else JSON_BODY
            out.extend(Difference(surface, loc, p, k) for p, k in diff_values(x.body, y.body))
    _compare_upstream(a, b, ca, cb, out)
    _compare_db(a, b, out)
    return out


def _dedupe(diffs: Iterable[Difference]) -> list[Difference]:
    """同一位置同一路徑同時有形狀與值差異時只留形狀(`shape`、`sentinel` 先於 `value`)。"""
    first: dict[tuple[str, str, str], Difference] = {}
    rank = {"sentinel": 0, "shape": 1}
    for d in sorted(diffs, key=lambda d: rank.get(d.kind, 2)):
        first.setdefault((d.surface, d.location, d.path), d)
    return list(first.values())


def compare_captures(a: Capture, b: Capture) -> Report:
    """15.10.1 依序五步。報告中的位置與路徑再遮罩一次(`mask_secrets` + 兩側 sentinel)。"""
    extra = (*sentinels_of(a), *sentinels_of(b))
    diffs = scan_capture(a, "a") + scan_capture(b, "b")
    view_a, shape_a = normalize_capture(mask_capture(a), "a")
    view_b, shape_b = normalize_capture(mask_capture(b), "b")
    diffs += shape_a + shape_b + compare_views(view_a, view_b, a.model_credentials, b.model_credentials)
    safe = [Difference(d.surface, mask_secrets(d.location, extra), mask_secrets(d.path, extra), d.kind, d.side)
            for d in _dedupe(diffs)]
    return Report(a.scenario, sorted(safe))
