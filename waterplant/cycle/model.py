"""Durable treatment cycle: steps, states and the fixed safety policy.

A cycle walks the treatment line one step at a time. The policy here is fixed
deliberately and not configurable per request:

* Each step's result is persisted before the next step starts.
* A failed step is a *partial* failure: the cycle stops at the current safe
  point, every later step stays ``pending`` and the completed steps (and the
  chemicals they already dosed) are retained.
* A cycle never resumes itself. A crash leaves the active step ``running``;
  it is reconciled to an ``interrupted`` state and only an explicit operator
  confirmation (resume) continues from the breakpoint, re-running the failed
  step.
* Chemical dosing steps are idempotent: they carry a cycle-unique dedupe ref
  so a retry or a resume can never dose twice.
* The chlorine step is gated on a stable pH; it does not start while pH is
  outside the band.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from waterplant.ns import Stage


class CycleStatus(str, Enum):
    """Lifecycle of one treatment cycle."""

    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    ABORTED = "aborted"


class StepStatus(str, Enum):
    """Lifecycle of one step inside a cycle."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


# Reasons a cycle can be stopped at a safe point.
REASON_STEP_FAILED = "step_failed"
REASON_PH_NOT_STABLE = "ph_not_stable"
REASON_PROCESS_CRASH = "process_interrupted"
REASON_OPERATOR_ABORT = "operator_abort"


@dataclass(frozen=True)
class StepDef:
    """One ordered step of the cycle and the safety gate in front of it."""

    name: str
    stage: Stage | None
    action: str
    requires_ph_stable: bool = False


@dataclass
class StepRecord:
    """The persisted result of running (or trying to run) one step."""

    name: str
    stage: str
    action: str
    status: str = StepStatus.PENDING.value
    result: dict[str, object] = field(default_factory=dict)
    error: str = ""
    attempts: int = 0
    time: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "stage": self.stage,
            "action": self.action,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "time": self.time,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "StepRecord":
        result = payload.get("result", {})
        return cls(
            name=str(payload.get("name", "")),
            stage=str(payload.get("stage", "")),
            action=str(payload.get("action", "")),
            status=str(payload.get("status", StepStatus.PENDING.value)),
            result=dict(result) if isinstance(result, dict) else {},
            error=str(payload.get("error", "")),
            attempts=int(payload.get("attempts", 0) or 0),
            time=str(payload.get("time", "")),
        )


#: The canonical, fixed cycle order. The CHLOR step carries the pH gate.
STEP_DEFS: tuple[StepDef, ...] = (
    StepDef("intake", Stage.INTAKE, "collect flow and turbidity"),
    StepDef("coag", Stage.COAG, "dose coagulant from flow"),
    StepDef("turbidity", Stage.TURBIDITY, "judge turbidity and dose coagulant"),
    StepDef("ph", None, "read and stabilise ph"),
    StepDef("residual", Stage.CLEARWELL, "update residual demand target"),
    StepDef("chlor", Stage.CHLOR, "dose chlorine", requires_ph_stable=True),
    StepDef("clearwell", Stage.CLEARWELL, "adjust clear well level"),
    StepDef("rotation", Stage.FILTER, "order filter duty rotation"),
    StepDef("backwash", Stage.BACKWASH, "start requested backwash"),
    StepDef("quota", Stage.QUOTA, "accumulate chemical consumption"),
    StepDef("audit", Stage.AUDIT, "close and record the cycle"),
)


def build_steps() -> list[StepRecord]:
    """Fresh, all-pending step records in cycle order."""

    return [
        StepRecord(name=step.name, stage="" if step.stage is None else step.stage.value, action=step.action)
        for step in STEP_DEFS
    ]


def step_def(name: str) -> StepDef:
    for candidate in STEP_DEFS:
        if candidate.name == name:
            return candidate
    raise KeyError(name)
