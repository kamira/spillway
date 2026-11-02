"""`GET /api/mcp/config`(CHG-20261102-01 T5;決策 3、16、17〔AM-14〕;spec 8.2)。

`?group=<g>&tool=<t>`:`tool` ∈ `TOOL_FORMATS` 的鍵(`claude-code`、`codex`、`cursor`、`gemini`、`stdio`),
其他 → 422 `validation_error`;`group` 不存在且不是 `all` → 404 `not_found`。

回應 `{group, tool, variants: [{label, scope, format, path, filename, content}], env, notes}`
(CHG-20261102-02 決策 16:已移除頂層 `format`/`path`/`content`,請讀 `variants[0]`)。`scope` 為 `user`|`project`,
`format` 為 `command`|`json`|`toml`;命令類 variant 的 `path`、`filename` 為 null。`env` 固定
`["SPILLWAY_TOKEN"]`;token 一律以環境變數參照,輸出內容不含任何 token(決策 3)。

內容由 `src/spillway/templates/mcp/` 的 Jinja2 模板渲染(`StrictUndefined`、不自動跳脫),變數 `name`
(`spillway-<group>`)、`url`、`group`、`port`、`command`、`args_prefix`。跳脫由模板內過濾器負責:
JSON `tojson`;TOML `toml_str`/`toml_key`;命令 `shq`(`shlex.quote`);URL 片段 `quote(g, safe="")`。
新增工具只新增模板檔與 `TOOL_FORMATS` 的一行(`TOOL_NOTES` 為可選)。
"""
from __future__ import annotations

import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import jinja2
from fastapi import Request

from spillway.api.deps import request_conn
from spillway.settings import MCP_ALL_GROUP, mcp_url
from spillway.store import repo

from .common import _invalid, _not_found, router

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "mcp"
ENV_VARS = ["SPILLWAY_TOKEN"]


@dataclass(frozen=True)
class VariantSpec:
    label: str
    scope: Literal["user", "project"]
    format: Literal["command", "json", "toml"]
    template: str                 # 相對於模板目錄的檔名
    path: str | None = None       # 建議放置路徑(命令類為 None)
    filename: str | None = None


TOOL_FORMATS: dict[str, list[VariantSpec]] = {
    "claude-code": [
        VariantSpec("專案 .mcp.json", "project", "json", "claude-code.project.j2", ".mcp.json", ".mcp.json"),
        VariantSpec("claude mcp add 命令", "user", "command", "claude-code.command.j2"),
    ],
    "codex": [
        VariantSpec("~/.codex/config.toml", "user", "toml", "codex.toml.j2", "~/.codex/config.toml", "config.toml"),
        VariantSpec("codex mcp add 命令", "user", "command", "codex.command.j2"),
    ],
    "cursor": [
        VariantSpec("~/.cursor/mcp.json", "user", "json", "cursor.http.j2", "~/.cursor/mcp.json", "mcp.json"),
        VariantSpec("專案 .cursor/mcp.json", "project", "json", "cursor.http.j2", ".cursor/mcp.json", "mcp.json"),
    ],
    "gemini": [
        VariantSpec("~/.gemini/settings.json(HTTP)", "user", "json", "gemini.http.j2",
           "~/.gemini/settings.json", "settings.json"),
        VariantSpec("~/.gemini/settings.json(stdio)", "user", "json", "gemini.stdio.j2",
           "~/.gemini/settings.json", "settings.json"),
    ],
    "stdio": [
        VariantSpec("Claude Code(專案 .mcp.json)", "project", "json", "claude-code.stdio.j2", ".mcp.json", ".mcp.json"),
        VariantSpec("Cursor(~/.cursor/mcp.json)", "user", "json", "cursor.stdio.j2", "~/.cursor/mcp.json", "mcp.json"),
        VariantSpec("Codex(~/.codex/config.toml)", "user", "toml", "codex.stdio.j2", "~/.codex/config.toml",
           "config.toml"),
        VariantSpec("Gemini CLI(~/.gemini/settings.json)", "user", "json", "gemini.stdio.j2",
           "~/.gemini/settings.json", "settings.json"),
    ],
}

# (severity, text) 依序輸出;可選——沒有列出的工具沒有 tool 專屬 notes
_STDIO_NOTE = ("info", "stdio 的 command 是此台機器上的絕對路徑({command});換機器或重裝後須重新產生。")
TOOL_NOTES: dict[str, list[tuple[str, str]]] = {
    "claude-code": [
        ("warn", "命令中的 ${SPILLWAY_TOKEN} 以單引號傳入,local/user scope 是否在執行期展開官方文件未載明;"
                 "請以 `claude mcp get` 確認顯示的是字面 ${SPILLWAY_TOKEN}。"),
    ],
    "codex": [
        ("info", "請先在 shell 環境設定 SPILLWAY_TOKEN;tool_timeout_sec 預設 60 秒,此處設為 600。"),
    ],
    "cursor": [
        ("info", "請先在啟動 Cursor 的環境設定 SPILLWAY_TOKEN。"),
    ],
    "gemini": [
        ("warn", "若 401 或 `gemini mcp list` 顯示未連線,改用 variants[1](stdio)。"),
        _STDIO_NOTE,
    ],
    "stdio": [_STDIO_NOTE],
}


# ---------------------------------------------------------------- 跳脫過濾器

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_str(value: Any) -> str:
    """TOML 基本字串(含引號)。"""
    out = ['"']
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_key(value: Any) -> str:
    s = str(value)
    return s if _BARE_KEY.match(s) else toml_str(s)


def _env(template_dir: Path) -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)),
                             undefined=jinja2.StrictUndefined, autoescape=False,
                             keep_trailing_newline=False)
    env.filters["toml_str"] = toml_str
    env.filters["toml_key"] = toml_key
    env.filters["shq"] = lambda v: shlex.quote(str(v))
    return env


# ---------------------------------------------------------------- 渲染

def stdio_command() -> tuple[str, list[str]]:
    """`(command, args_prefix)`:`shutil.which("spillway")` 的絕對路徑,找不到時 `sys.executable -m spillway`。"""
    found = shutil.which("spillway")
    if found:
        return str(Path(found).absolute()), []
    return sys.executable, ["-m", "spillway"]


def build_context(group: str, port: int) -> dict[str, Any]:
    command, args_prefix = stdio_command()
    return {"name": f"spillway-{group}", "group": group, "port": port,
            "url": mcp_url(port, group),
            "command": command, "args_prefix": args_prefix}


def render_config(tool: str, ctx: dict[str, Any],
                  template_dir: Path = DEFAULT_TEMPLATE_DIR) -> list[dict[str, Any]]:
    """渲染 `TOOL_FORMATS[tool]` 的每個 variant(測試以 `template_dir` 注入模板目錄)。"""
    env = _env(Path(template_dir))
    out: list[dict[str, Any]] = []
    for spec in TOOL_FORMATS[tool]:
        content = env.get_template(spec.template).render(**ctx)
        out.append({"label": spec.label, "scope": spec.scope, "format": spec.format,
                    "path": spec.path, "filename": spec.filename, "content": content})
    return out


def _notes(tool: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    return [{"severity": sev, "text": text.replace("{command}", " ".join([ctx["command"], *ctx["args_prefix"]]))}
            for sev, text in TOOL_NOTES.get(tool, [])]


@router.get("/mcp/config", responses={404: {"description": "群組不存在"}})
async def mcp_config(request: Request, group: str, tool: str) -> dict[str, Any]:
    """見模組 docstring。"""
    if tool not in TOOL_FORMATS:
        raise _invalid("query.tool", "不支援的工具(可用:" + "、".join(TOOL_FORMATS) + ")")
    if group != MCP_ALL_GROUP:
        with request_conn(request) as conn:
            if repo.get_group(conn, group) is None:
                raise _not_found(f"群組 {group}")
    ctx = build_context(group, int(request.app.state.port))
    variants = render_config(tool, ctx)
    return {"group": group, "tool": tool, "variants": variants, "env": list(ENV_VARS),
            "notes": _notes(tool, ctx)}
