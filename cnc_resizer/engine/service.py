"""High-level read-only CNC engine operations."""
from typing import Any

from .ir import to_dict
from .parametric import preview_resize
from .parser import ParseError, parse_program
from .recognition import recognize
from .validation import validate_program
from .postprocessor import execution_gate


def inspect_program(text: str, *, filename: str = "<memory>") -> dict[str, Any]:
    program = parse_program(text, filename=filename)
    recognition = recognize(program)
    validation = validate_program(program)
    return {
        "ok": True,
        "preview_only": True,
        "safe_for_execution": False,
        "program": to_dict(program),
        "analysis": recognition,
        "validation": validation,
        "execution_gate": execution_gate(program),
    }


def preview_program(text: str, request: dict[str, Any], *, filename: str = "<memory>") -> dict[str, Any]:
    program = parse_program(text, filename=filename)
    return preview_resize(program, request)
