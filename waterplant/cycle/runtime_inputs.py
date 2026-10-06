"""Typed, validated inputs for one treatment cycle."""

from __future__ import annotations

from dataclasses import dataclass

from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import validate_flow
from waterplant.quota import validate_amount


@dataclass(frozen=True)
class CycleInputs:
    """Every reading an operator supplies when starting a cycle."""

    flow: float
    samples: tuple[float, ...]
    demand: float
    level: float
    bed_id: str
    zone: int
    amount: float

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "CycleInputs":
        raw_samples = payload.get("samples", [])
        if raw_samples is None:
            raw_samples = []
        if not isinstance(raw_samples, (list, tuple)):
            raise ValueError("field samples must be a list of numbers")
        return cls(
            flow=_as_number("flow", payload.get("flow", 0.0)),
            samples=tuple(_as_number("samples", item) for item in raw_samples),
            demand=_as_number("demand", payload.get("demand", 0.0)),
            level=_as_number("level", payload.get("level", 0.0)),
            bed_id=_as_bed(payload.get("bed_id", "")),
            zone=_as_zone(payload.get("zone", 0)),
            amount=_as_number("amount", payload.get("amount", 0.0)),
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "flow": self.flow,
            "samples": list(self.samples),
            "demand": self.demand,
            "level": self.level,
            "bed_id": self.bed_id,
            "zone": self.zone,
            "amount": self.amount,
        }

    def validate(self, rt) -> None:  # noqa: ANN001 - runtime wiring
        """Reject an unusable cycle before any step runs or anything doses."""

        validate_flow(self.flow)
        validate_factor(rt.calibration.current())
        validate_zone(self.zone)
        validate_amount(self.amount)
        validate_level(self.level)
        if self.bed_id and rt.bank.bed(self.bed_id) is None:
            raise ValueError(f"filter bed {self.bed_id} not found")


def _as_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"field {name} must be a number")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"field {name} must be a number") from exc


def _as_zone(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("field zone must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError("field zone must be an integer") from exc
    raise ValueError("field zone must be an integer")


def _as_bed(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
