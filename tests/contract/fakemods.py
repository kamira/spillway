"""在暫存目錄寫出假 adapter 模組資料夾(決策 15:測試模組不寫入 src/)。

T4 擁有;T6、T14、T15 可重用 `write_module()` 產生測試用模組。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

DEFAULT_EFFORT_MAP = '{"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"}'

DEFAULT_MODELS_YAML = """\
- id: fake-small
  label: Fake Small
  capabilities: {ctx: 8000, tools: true, vision: false, efforts: [low, medium]}
- id: fake-large
  label: Fake Large
  provider: acme
  note: 測試用
  capabilities: {ctx: 200000, tools: true, vision: true, efforts: [none, low, medium, high, max]}
"""

DEFAULT_ADAPTER_PY = '''\
from spillway.adapters.base import CallResult


class FakeAdapter:
    def __init__(self, context):
        self.context = context

    async def call(self, dep, req, effort):
        return CallResult(ok=True, text="fake", tokens_in=1, tokens_out=1,
                          cost_usd=0.0, status="ok", error=None)

    async def health(self, dep):
        return True
'''


def manifest_source(
    *,
    providers: tuple[str, ...] = ("acme",),
    kinds: tuple[str, ...] = ("api",),
    access: tuple[str, ...] = ("api",),
    cardinality: str = "multiple",
    contract_version: int = 1,
    fallback_for_kinds: tuple[str, ...] | None = None,
    effort_map: str = DEFAULT_EFFORT_MAP,
) -> str:
    fb = f"\n    fallback_for_kinds={fallback_for_kinds!r}," if fallback_for_kinds is not None else ""
    return (
        "MANIFEST = AdapterManifest(\n"
        f"    providers={providers!r},\n"
        f"    kinds={kinds!r},\n"
        f"    access={access!r},\n"
        f"    cardinality={cardinality!r},\n"
        f"    effort_map={effort_map},\n"
        "    default_signals={'quota': [{'http_status': 402}, {'error_code': 'insufficient_quota'}],\n"
        "                     'rate_limit': [{'http_status': 429}]},\n"
        "    default_capabilities={'ctx': 8000, 'tools': True, 'vision': False, 'efforts': ['low', 'medium']},\n"
        "    default_params={'timeout': 120},\n"
        "    requires=(),\n"
        f"    contract_version={contract_version!r},{fb}\n"
        ")\n"
    )


def write_module(
    root: Path,
    name: str,
    *,
    init_source: str | None = None,
    pre_import: str = "",
    adapter_source: str = DEFAULT_ADAPTER_PY,
    models_yaml: str | None = DEFAULT_MODELS_YAML,
    readme: bool = True,
    adapter_file: bool = True,
    extra_files: dict[str, str] | None = None,
    **manifest_kw,
) -> Path:
    """在 `root/<name>/` 寫出一個模組資料夾,回傳該資料夾。

    `init_source` 給了就整個取代 `__init__.py`;`pre_import` 會放在 `__init__.py` 最前面
    (例如 `import 不存在的套件` 模擬無法 import)。`models_yaml=None` 表示不建立該檔。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if init_source is None:
        init_source = (
            pre_import
            + "from spillway.adapters.base import AdapterManifest\n"
            + "from .adapter import FakeAdapter\n\n"
            + manifest_source(**manifest_kw)
            + "\n\ndef create(context):\n    return FakeAdapter(context)\n"
        )
    (d / "__init__.py").write_text(textwrap.dedent(init_source), encoding="utf-8")
    if adapter_file:
        (d / "adapter.py").write_text(adapter_source, encoding="utf-8")
    if models_yaml is not None:
        (d / "models.yaml").write_text(models_yaml, encoding="utf-8")
    if readme:
        (d / "README.md").write_text(f"# {name}\n\n測試用假模組。\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d
