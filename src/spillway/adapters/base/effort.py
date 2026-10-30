"""內部 effort 等級 → 上游值(spec 6.3;決策 12)。降級(6.2)由應用層 EffortMapper 處理。"""
from __future__ import annotations

from spillway.adapters.base.manifest import AdapterManifest
from spillway.adapters.base.types import EFFORTS


def effort_to_upstream(manifest: AdapterManifest, level: str | None) -> str | None:
    """回傳 `manifest.effort_map[level]`;`level` 為 None(不傳)或對應值為 null 時回 None。"""
    if level is None:
        return None
    if level not in EFFORTS:
        raise ValueError(f"未知的 effort 等級 {level!r},只可為 {', '.join(EFFORTS)}")
    return manifest.effort_map[level]
