"""Adapter 共用契約(spec 7.6.3:模組只可引用這裡)。

匯出:值物件(types)、`Adapter` Protocol 與 `AdapterContext`、`AdapterManifest` 與驗證、
`models.yaml` 解析、`classify` 與 signals 規則、`effort_to_upstream`、`litellm_call`、
`AdapterRegistry` 與合約檢查。
"""
from spillway.adapters.base.contract import contract_problems, layering_violations
from spillway.adapters.base.effort import effort_to_upstream
from spillway.adapters.base.litellm_call import DEFAULT_TIMEOUT, LiteLLMResult, litellm_call
from spillway.adapters.base.manifest import (
    ACCESS_VALUES,
    CARDINALITIES,
    REQUIRED_MANIFEST_FIELDS,
    AdapterManifest,
    ManifestError,
    ModelInfo,
    ModelsYamlError,
    load_models_yaml,
    parse_models,
    validate_manifest,
)
from spillway.adapters.base.protocol import Adapter, AdapterContext, CredentialResolver
from spillway.adapters.base.registry import (
    AdapterConflictError,
    AdapterRegistry,
    AdapterUnavailableError,
    ModelsListing,
    ModuleInfo,
    available_models,
)
from spillway.adapters.base.signals import (
    API_DEFAULT_SIGNALS,
    SignalsError,
    classify,
    resolve_signals,
    validate_signals,
)
from spillway.adapters.base.types import (
    CALL_STATUSES,
    EFFORTS,
    KINDS,
    Budget,
    CallResult,
    ChatRequest,
    Deployment,
)

__all__ = [
    # types(契約先行,欄位不可改)
    "Budget", "CallResult", "ChatRequest", "Deployment", "CALL_STATUSES", "EFFORTS", "KINDS",
    # protocol
    "Adapter", "AdapterContext", "CredentialResolver",
    # manifest / models.yaml
    "AdapterManifest", "ManifestError", "ModelInfo", "ModelsYamlError", "ACCESS_VALUES",
    "CARDINALITIES", "REQUIRED_MANIFEST_FIELDS", "load_models_yaml", "parse_models",
    "validate_manifest",
    # signals / effort / litellm
    "API_DEFAULT_SIGNALS", "SignalsError", "classify", "resolve_signals", "validate_signals",
    "effort_to_upstream", "DEFAULT_TIMEOUT", "LiteLLMResult", "litellm_call",
    # registry / contract
    "AdapterConflictError", "AdapterRegistry", "AdapterUnavailableError", "ModelsListing",
    "ModuleInfo", "available_models", "contract_problems", "layering_violations",
]
