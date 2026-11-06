from __future__ import annotations

from typing import Any


def normalize_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none"}:
        return ""
    if normalized in {"low", "medium", "high"}:
        return normalized
    if normalized in {"xhigh", "extended"}:
        return "extended"
    return ""


def thinking_effort_from_body(body: dict[str, Any]) -> str:
    if "thinking_effort" in body:
        return normalize_thinking_effort(body.get("thinking_effort"))
    if "reasoning_effort" in body:
        return normalize_thinking_effort(body.get("reasoning_effort"))
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        return normalize_thinking_effort(reasoning.get("effort"))
    return ""


def thinking_control_from_body(body: dict[str, Any]) -> tuple[bool | None, str]:
    """Return (enabled, effort). ``None`` means follow the model default."""
    enabled: bool | None = None
    for key in ("thinking", "thinking_enabled", "reasoning_enabled"):
        if key in body and isinstance(body.get(key), bool):
            enabled = bool(body[key])
            break
    reasoning = body.get("reasoning")
    if enabled is None and isinstance(reasoning, dict) and isinstance(reasoning.get("enabled"), bool):
        enabled = bool(reasoning["enabled"])
    effort = thinking_effort_from_body(body)
    if enabled is False:
        return False, ""
    if enabled is True and not effort:
        effort = "medium"
    return enabled, effort
