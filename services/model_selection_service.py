"""Model availability and explicit fallback-chain policy.

This module owns the ordered fallback decision only. Account selection, HTTP
transport, and response projection remain in their existing owners.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import TypeVar

from services.config import config

T = TypeVar("T")

DEFAULT_CHAT_CHAIN = ("gpt-6", "gpt-5.6", "auto")
DEFAULT_IMAGE_CHAIN = (
    "gpt-image-2.6",
    "gpt-image-2.5",
    "gpt-image-2",
)
MODEL_UNAVAILABLE_CODES = frozenset(
    {
        "image_model_not_found",
        "model_not_found",
        "unsupported_model",
    }
)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _configured_chain(kind: str, fallback: tuple[str, ...]) -> list[str]:
    catalog = config.get().get("model_catalog")
    catalog = catalog if isinstance(catalog, dict) else {}
    raw = catalog.get(f"{kind}_fallback_models")
    values = [str(item or "").strip() for item in raw] if isinstance(raw, list) else []
    return _unique(values) or list(fallback)


def chat_fallback_chain() -> list[str]:
    return _configured_chain("chat", DEFAULT_CHAT_CHAIN)


def image_fallback_chain() -> list[str]:
    return _configured_chain("image", DEFAULT_IMAGE_CHAIN)


def model_chain(model: str, *, image: bool = False) -> list[str]:
    canonical = chat_fallback_chain() if not image else image_fallback_chain()
    requested = str(model or "").strip()
    if requested and requested != "auto":
        start = requested
        if requested in canonical:
            start_index = canonical.index(requested)
            return _unique([requested, *canonical[start_index + 1:]])
        return _unique([requested, *canonical])
    return canonical


def is_model_unavailable_failure(code: object) -> bool:
    return str(code or "").strip() in MODEL_UNAVAILABLE_CODES


def model_unavailable_text(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "model not found",
            "model_not_found",
            "unsupported model",
            "unsupported_model",
            "model is not available",
        )
    )


def model_unavailable_from_error(error: Exception) -> bool:
    if getattr(error, "conversation_id", ""):
        return False
    for attr in ("failure", "code", "stage"):
        value = getattr(error, attr, None)
        if attr == "failure" and value is not None:
            value = getattr(value, "code", None)
        if is_model_unavailable_failure(value):
            return True
    return model_unavailable_text(error)


def with_model_fallback(
    model: str,
    factory,
    *,
    image: bool = False,
) -> Iterator[T]:
    """Yield one successful attempt; retry only when no candidate was emitted."""
    last_error: Exception | None = None
    for selected_model in model_chain(model, image=image):
        emitted = False
        try:
            for item in factory(selected_model):
                emitted = True
                yield item
            if emitted:
                return
            raise RuntimeError("model attempt produced no output")
        except Exception as exc:
            last_error = exc
            if not emitted and model_unavailable_from_error(exc):
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("no fallback model available")
