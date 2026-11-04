"""Process-only selection of the supported direct vision services."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def _valid_url(value: str) -> bool:
    try:
        if any(char.isspace() for char in value):
            return False
        parsed = urlsplit(value)
        _ = parsed.port  # Reject non-numeric and out-of-range ports.
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def transport(value: str | None = None) -> str:
    raw = (os.environ.get("JY_VISION_TRANSPORT", "") if value is None else value).strip().lower()
    if raw in {"", "volc_glm", "glm", "volc"}:
        return "volc_glm"
    if raw in {"qwen", "qwen_compatible"}:
        return "qwen"
    raise ValueError("UNSUPPORTED_VISION_TRANSPORT:" + raw)


def model() -> str:
    if transport() == "qwen":
        return (os.environ.get("JY_QWEN_MODEL", "").strip()
                or os.environ.get("JY_VISION_TEXT_MODEL", "").strip())
    return os.environ.get("JY_VISION_MODEL", "").strip() or "glm-5.3-flash"


def facts() -> dict:
    """Diagnostics must survive invalid configuration so a failure can be saved."""
    try:
        return {"vision_transport": transport(), "vision_model": model() or None}
    except ValueError as exc:
        return {"vision_transport": os.environ.get("JY_VISION_TRANSPORT", "").strip(),
                "vision_model": None, "vision_configuration_error": str(exc)}


def missing_required(manifest: dict | None = None) -> list[str]:
    """Check presence only; never return credential values or contact a service."""
    route = transport()
    if route == "qwen":
        missing = []
        endpoint = os.environ.get("JY_QWEN_BASE_URL", "").strip() or os.environ.get("JY_VISION_TEXT_BASE_URL", "").strip()
        if not endpoint:
            missing.append("JY_QWEN_BASE_URL (or JY_VISION_TEXT_BASE_URL)")
        elif not _valid_url(endpoint):
            missing.append("JY_QWEN_BASE_URL_INVALID")
        if not (os.environ.get("JY_QWEN_MODEL", "").strip() or os.environ.get("JY_VISION_TEXT_MODEL", "").strip()):
            missing.append("JY_QWEN_MODEL (or JY_VISION_TEXT_MODEL)")
        if not (os.environ.get("JY_QWEN_API_KEY", "").strip() or os.environ.get("JY_VISION_TEXT_API_KEY", "").strip()):
            missing.append("JY_QWEN_API_KEY (or JY_VISION_TEXT_API_KEY)")
        return missing
    missing = []
    endpoint = os.environ.get("JY_VOLC_BASE_URL", "").strip()
    if endpoint and not _valid_url(endpoint):
        missing.append("JY_VOLC_BASE_URL_INVALID")
    if not (os.environ.get("ARK_API_KEY", "").strip() or os.environ.get("JY_VOLC_API_KEY", "").strip()):
        missing.append("ARK_API_KEY (or JY_VOLC_API_KEY)")
    from ..ai_editor_contract import selection_mode
    mode = selection_mode(manifest)
    if mode in {"ai_editor", "rule_then_ai"} and os.environ.get("JY_AI_EDITOR_QWEN", "").strip() == "1":
        for name in ("JY_VISION_TEXT_BASE_URL", "JY_VISION_TEXT_MODEL", "JY_VISION_TEXT_API_KEY"):
            if not os.environ.get(name, "").strip():
                missing.append(name)
        endpoint = os.environ.get("JY_VISION_TEXT_BASE_URL", "").strip()
        if endpoint and not _valid_url(endpoint):
            missing.append("JY_VISION_TEXT_BASE_URL_INVALID")
    return missing
