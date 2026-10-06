"""Append only journal of cycle lifecycle events.

Every meaningful cycle action lands here: creation, each step's success or
failure, the breakpoint where the cycle stopped, and every operator resume or
abort. The console projects this journal so an operator can see exactly what
ran, what dosed, and what an interruption recovery did.

The journal builds the new list of serialised entries; the cycle store writes
that list together with the cycle checkpoint in one atomic document update,
so an audit event and the step result it describes can never disagree.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import load_commands
from waterplant.store.store import Store

CYCLE_EVENTS_KEY = "cycle:events"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class CycleEvent:
    """One immutable fact about a cycle."""

    id: str
    time: str
    cycle_id: str
    kind: str
    detail: str
    step: str = ""
    operator: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "time": self.time,
            "cycle_id": self.cycle_id,
            "kind": self.kind,
            "detail": self.detail,
            "step": self.step,
            "operator": self.operator,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CycleEvent":
        return cls(
            id=str(payload.get("id", "")),
            time=str(payload.get("time", "")),
            cycle_id=str(payload.get("cycle_id", "")),
            kind=str(payload.get("kind", "")),
            detail=str(payload.get("detail", "")),
            step=str(payload.get("step", "")),
            operator=str(payload.get("operator", "")),
        )


class CycleJournal:
    """Reads cycle events and composes new entries for an atomic checkpoint."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def build(
        self,
        cycle_id: str,
        kind: str,
        detail: str,
        step: str = "",
        operator: str = "",
    ) -> tuple[CycleEvent, dict[str, str]]:
        """Return an event and the store change that appends it atomically."""

        event = CycleEvent(
            id=uuid.uuid4().hex,
            time=utc_now(),
            cycle_id=cycle_id,
            kind=kind,
            detail=detail,
            step=step,
            operator=operator,
        )
        raw = list(load_commands(self._store, CYCLE_EVENTS_KEY))
        raw.append(json.dumps(event.as_dict(), ensure_ascii=False))
        return event, {CYCLE_EVENTS_KEY: json.dumps(raw, ensure_ascii=False)}

    def events(self, cycle_id: str | None = None) -> list[CycleEvent]:
        events: list[CycleEvent] = []
        for item in load_commands(self._store, CYCLE_EVENTS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(CycleEvent.from_dict(payload))
        if cycle_id is not None:
            events = [event for event in events if event.cycle_id == cycle_id]
        return events
