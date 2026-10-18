"""Fail-closed controller postprocessor registry.

A generic G-code writer is intentionally not provided: the repository has no
verified controller profile for the supplied programs. A controller adapter
must be registered only after controller documentation, fixture hashes and
simulation/physical validation are supplied.
"""
from __future__ import annotations

from typing import Any, Callable


class OutputBlocked(RuntimeError):
    pass


ADAPTERS: dict[str, Callable[..., str]] = {}


def register_adapter(key: str, adapter: Callable[..., str]) -> None:
    if not key or not callable(adapter):
        raise ValueError("A named callable adapter is required")
    if key in ADAPTERS:
        raise ValueError(f"Postprocessor adapter {key!r} is already registered")
    ADAPTERS[key] = adapter


def execution_gate(program: Any, controller_profile: dict[str, Any] | None = None,
                   source_sha256: str | None = None) -> dict[str, Any]:
    reasons = []
    if controller_profile is not None and not isinstance(controller_profile, dict):
        reasons.append("Controller profile data is malformed")
        controller_profile = None

    def declared_strings(field: str) -> set[str]:
        value = (controller_profile or {}).get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            reasons.append(f"Controller profile field {field} must be a list of strings")
            return set()
        return set(value)

    if not controller_profile:
        reasons.append("No controller profile is assigned to this CNC source")
    else:
        if controller_profile.get("approved") is not True:
            reasons.append("Controller profile has not been approved")
        key = controller_profile.get("adapter_key")
        if not isinstance(key, str) or not key or key not in ADAPTERS:
            reasons.append("No verified controller-specific postprocessor adapter is registered")
        validated_hashes = declared_strings("validated_source_sha256")
        source_hash = source_sha256 or getattr(program, "source_sha256", None)
        if not source_hash or source_hash not in validated_hashes:
            reasons.append("This exact source hash has not passed the controller regression/simulation suite")
    approved_g = declared_strings("supported_g_codes")
    approved_m = declared_strings("supported_m_codes")
    approved_compact = declared_strings("supported_compact_tokens")
    unknown_g = {item["code"] for item in (getattr(program, "unknown_g_codes", None) or [])}
    unknown_m = {item["code"] for item in (getattr(program, "unknown_m_codes", None) or [])}
    compact = {item["token"] for item in (getattr(program, "compact_controller_tokens", None) or [])}
    observed_g = set(getattr(program, "observed_g_codes", []) or [])
    observed_m = set(getattr(program, "observed_m_codes", []) or [])
    if observed_g - approved_g:
        reasons.append("Controller profile does not declare observed G codes: " + ", ".join(sorted(observed_g - approved_g)))
    if observed_m - approved_m:
        reasons.append("Controller profile does not declare observed M codes: " + ", ".join(sorted(observed_m - approved_m)))
    if unknown_g - approved_g:
        reasons.append("Unapproved G codes are present: " + ", ".join(sorted(unknown_g - approved_g)))
    if unknown_m - approved_m:
        reasons.append("Unapproved M codes are present: " + ", ".join(sorted(unknown_m - approved_m)))
    if compact - approved_compact:
        reasons.append("Unapproved compact controller commands are present: " + ", ".join(sorted(compact - approved_compact)))
    if unknown_g or unknown_m or compact:
        reasons.append("Parser cannot transform all unknown/compact command semantics; a whitelist alone is insufficient")
    if any(getattr(move.arc, "plane_assumed", False) for move in (getattr(program, "moves", []) or []) if move.arc):
        reasons.append("At least one arc plane is assumed from the G17 default rather than explicitly selected")
    if getattr(program, "unsupported_constructs", None):
        reasons.append("Macro/expression/control-flow constructs are not expanded by the parser")
    if getattr(program, "units", None) is None:
        reasons.append("Source units are undeclared")
    if getattr(program, "coordinate_system", None):
        reasons.append("Work-coordinate offsets are not resolved to physical machine coordinates")
    if any(code == "G43" or code.startswith("G43.") for code in (getattr(program, "observed_g_codes", []) or [])):
        reasons.append("Tool-length compensation is recorded but not applied")
    if any(getattr(move, "machine_coordinates", False) for move in (getattr(program, "moves", []) or [])):
        reasons.append("G53 machine-coordinate moves are not transformed or verified")
    return {"allowed": not reasons, "reasons": reasons, "output_written": False}


def generate_gcode(program: Any, transformed_ir: Any, controller_profile: dict[str, Any] | None = None) -> str:
    gate = execution_gate(program, controller_profile)
    if not gate["allowed"]:
        raise OutputBlocked("Executable CNC output blocked: " + "; ".join(gate["reasons"]))
    adapter = ADAPTERS[controller_profile["adapter_key"]]
    output = adapter(program, transformed_ir, controller_profile)
    if not isinstance(output, str) or not output.strip():
        raise OutputBlocked("Controller adapter returned no output")
    return output
