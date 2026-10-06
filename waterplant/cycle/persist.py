"""Persistence for the active cycle document and finished cycle history."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from waterplant.store.store import Store

from .journal import utc_now
from .model import CycleStatus, StepRecord, StepStatus, build_steps

ACTIVE_CYCLE_KEY = "cycle:active"
CYCLE_HISTORY_KEY = "cycle:history"


@dataclass
class CycleDoc:
    """The full, persistable state of one treatment cycle."""

    cycle_id: str
    status: str
    steps: list[StepRecord]
    inputs: dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    breakpoint: str = ""
    interrupt_reason: str = ""
    interrupt_detail: str = ""
    resumed_count: int = 0
    finished_at: str = ""

    def step(self, name: str) -> StepRecord:
        for record in self.steps:
            if record.name == name:
                return record
        raise KeyError(name)

    def pending_from(self, name: str) -> list[StepRecord]:
        names = [record.name for record in self.steps]
        index = names.index(name)
        return self.steps[index:]

    def first_unfinished(self) -> StepRecord | None:
        for record in self.steps:
            if record.status != StepStatus.SUCCEEDED.value:
                return record
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "steps": [record.as_dict() for record in self.steps],
            "inputs": self.inputs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "breakpoint": self.breakpoint,
            "interrupt_reason": self.interrupt_reason,
            "interrupt_detail": self.interrupt_detail,
            "resumed_count": self.resumed_count,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CycleDoc":
        raw_steps = payload.get("steps", [])
        steps = [StepRecord.from_dict(item) for item in raw_steps] if isinstance(raw_steps, list) else []
        inputs = payload.get("inputs", {})
        return cls(
            cycle_id=str(payload.get("cycle_id", "")),
            status=str(payload.get("status", CycleStatus.RUNNING.value)),
            steps=steps,
            inputs=dict(inputs) if isinstance(inputs, dict) else {},
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            breakpoint=str(payload.get("breakpoint", "")),
            interrupt_reason=str(payload.get("interrupt_reason", "")),
            interrupt_detail=str(payload.get("interrupt_detail", "")),
            resumed_count=int(payload.get("resumed_count", 0) or 0),
            finished_at=str(payload.get("finished_at", "")),
        )


def StepStatus_SUCCEEDED_value() -> str:
    # Indirection keeps the hot import list of this module small.
    from .model import StepStatus

    return StepStatus.SUCCEEDED.value


class CycleStore:
    """Loads and saves the active cycle and appends finished cycles."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load_active(self) -> CycleDoc | None:
        raw, present = self._store.get(ACTIVE_CYCLE_KEY)
        if not present:
            return None
        return self._decode(raw)

    def new(self, cycle_id: str, inputs: dict[str, object]) -> CycleDoc:
        now = utc_now()
        return CycleDoc(
            cycle_id=cycle_id,
            status=CycleStatus.RUNNING.value,
            steps=build_steps(),
            inputs=dict(inputs),
            created_at=now,
            updated_at=now,
        )

    def save(self, doc: CycleDoc, extra: dict[str, str] | None = None) -> None:
        """Persist the active document together with any journal writes.

        The cycle document and supplied key/value changes land in one atomic
        document replacement, keeping the checkpoint consistent.
        """

        doc.updated_at = utc_now()
        changes = {ACTIVE_CYCLE_KEY: json.dumps(doc.as_dict(), ensure_ascii=False)}
        if extra:
            changes.update(extra)
        self._store.put_many(changes)

    def finish(self, doc: CycleDoc, status: str, extra: dict[str, str] | None = None) -> None:
        """Move a terminal cycle to history and clear the active slot."""

        doc.status = status
        doc.finished_at = utc_now()
        doc.updated_at = doc.finished_at
        changes: dict[str, str | None] = {
            ACTIVE_CYCLE_KEY: None,
        }
        history = self.history_docs()
        history.append(doc.as_dict())
        changes[CYCLE_HISTORY_KEY] = json.dumps(history, ensure_ascii=False)
        if extra:
            changes.update(extra)
        self._store.put_many(changes)

    def history_docs(self) -> list[dict[str, object]]:
        raw, present = self._store.get(CYCLE_HISTORY_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    def history(self) -> list[CycleDoc]:
        return [CycleDoc.from_dict(item) for item in self.history_docs()]

    @staticmethod
    def _decode(raw: str) -> CycleDoc | None:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        return CycleDoc.from_dict(parsed)
