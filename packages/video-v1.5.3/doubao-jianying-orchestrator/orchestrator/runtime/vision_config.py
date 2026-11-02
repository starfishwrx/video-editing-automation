"""Process-only selection of the supported direct vision services."""
from __future__ import annotations

import os


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
