"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str
    ref: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "time": self.time, "kind": self.kind, "detail": self.detail}
        if self.ref:
            payload["ref"] = self.ref
        return payload


class Auditor:
    """Records and queries dosing entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(self, kind: str, detail: str, ref: str = "") -> Entry:
        if ref:
            existing = self.find_by_ref(ref)
            if existing is not None:
                return existing
        entry = Entry(
            id=uuid.uuid4().hex,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            detail=detail,
            ref=ref,
        )
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def record_once(self, kind: str, detail: str, ref: str) -> tuple[Entry, bool]:
        """Record a dose keyed by ``ref`` and report whether it was new.

        ``True`` means the dose was just applied; ``False`` means an entry
        with the same ref already existed and no second dose was recorded.
        """

        existing = self.find_by_ref(ref)
        if existing is not None:
            return existing, False
        return self.record(kind, detail, ref), True

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    ref=str(payload.get("ref", "")),
                )
            )
        return entries

    def find_by_ref(self, ref: str) -> Entry | None:
        """Return the entry already recorded under ``ref`` for idempotency."""

        if not ref:
            return None
        for entry in self.entries():
            if entry.ref == ref:
                return entry
        return None

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            try:
                amount = float(entry.detail)
            except ValueError:
                continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"
