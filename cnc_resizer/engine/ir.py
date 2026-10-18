"""Small, framework-independent intermediate representation for CNC toolpaths."""
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Point3:
    x: float | None
    y: float | None
    z: float | None


@dataclass
class ArcGeometry:
    plane: str | None
    direction: str
    center: dict[str, float] | None
    radius: float | None
    sweep_radians: float | None
    valid: bool | None
    source_offsets: dict[str, float]
    plane_assumed: bool = False


@dataclass
class Motion:
    id: str
    source_line: int
    motion: str
    start: Point3 | None
    end: Point3 | None
    feed: float | None = None
    spindle: float | None = None
    tool: int | None = None
    h_offset: float | None = None
    coordinate_mode: str | None = None
    coordinate_system: str | None = None
    source_block: str | None = None
    machine_coordinates: bool = False
    arc: ArcGeometry | None = None
    raw_words: dict[str, float] = field(default_factory=dict)
    raw_line: str = ""


@dataclass
class PathBlock:
    id: str
    started_by_line: int
    ended_at_line: int
    start: Point3 | None
    motions: list[Motion] = field(default_factory=list)
    machine_coordinates: bool = False


@dataclass
class Program:
    filename: str
    source_sha256: str
    source_bytes: int
    line_count: int
    units: str | None
    coordinate_mode: str | None
    plane: str | None
    coordinate_system: str | None
    tools: list[int]
    h_registers: list[dict[str, Any]]
    observed_g_codes: list[str]
    observed_m_codes: list[str]
    feed_values: list[float]
    spindle_values: list[float]
    moves: list[Motion]
    blocks: list[PathBlock]
    unknown_g_codes: list[dict[str, Any]]
    unknown_m_codes: list[dict[str, Any]]
    compact_controller_tokens: list[dict[str, Any]]
    unsupported_constructs: list[dict[str, Any]]
    warnings: list[str]


def to_dict(value: Any) -> Any:
    """Convert the typed IR to JSON-compatible dictionaries/lists."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    return value
