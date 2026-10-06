"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing or cycle decision."""

    id: str
    time: str
    kind: str
    detail: str
    key: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "time": self.time,
            "key": self.key,
            "kind": self.kind,
            "detail": self.detail,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Entry":
        return cls(
            id=str(payload.get("id", "")),
            time=str(payload.get("time", "")),
            kind=str(payload.get("kind", "")),
            detail=str(payload.get("detail", "")),
            key=str(payload.get("key", "")),
        )


class Auditor:
    """Records and queries dosing entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _make(self, kind: str, detail: str, key: str = "") -> Entry:
        return Entry(
            id=uuid.uuid4().hex,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            detail=detail,
            key=key,
        )

    def record(self, kind: str, detail: str) -> Entry:
        entry = self._make(kind, detail)
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def entry_for_key(self, key: str) -> Entry | None:
        if not key:
            return None
        for entry in reversed(self.entries()):
            if entry.key == key:
                return entry
        return None

    def has_key(self, key: str) -> bool:
        return self.entry_for_key(key) is not None

    def record_idempotent(self, kind: str, detail: str, key: str) -> Entry | None:
        """Record ``kind`` once for ``key``; return None when the key exists."""

        existing = self.entry_for_key(key)
        if existing is not None:
            return None
        entry = self._make(kind, detail, key)
        persisted = self.append_entries_atomically([entry])
        return entry if persisted else None

    def make_entry(self, kind: str, detail: str, key: str = "") -> Entry:
        """Create an entry without persisting it, used in atomic batches."""

        existing = self.entry_for_key(key) if key else None
        if existing is not None:
            return existing
        return self._make(kind, detail, key)

    def append_entry(self, entry: Entry) -> Entry:
        """Append a prebuilt entry, treating its key as an idempotency key."""

        if entry.key and self.entry_for_key(entry.key) is not None:
            return entry
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def append_entries_atomically(self, entries: list[Entry], updates: dict[str, str] | None = None) -> list[Entry]:
        """Persist audit entries and caller state in one store transaction."""

        with self._store.transaction() as store:
            existing_items = self._raw_entries_locked(store)
            keys = {entry.key for entry in existing_items if entry.key}
            pending = [entry for entry in entries if entry.key not in keys]
            if not pending and not updates:
                return []
            if updates:
                for key, value in updates.items():
                    store.set_locked(key, value)
            if pending:
                items = [entry.as_dict() for entry in existing_items]
                items.extend(entry.as_dict() for entry in pending)
                store.set_locked(AUDIT_KEY, json.dumps(items, ensure_ascii=False))
        return pending

    def _decode_entries(self, raw: str) -> list[dict[str, object]]:
        try:
            items = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(items, list):
            return []
        payloads: list[dict[str, object]] = []
        for item in items:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            if isinstance(item, dict):
                payloads.append(item)
        return payloads

    def _raw_entries_locked(self, store: Store) -> list[Entry]:
        return [
            Entry.from_payload(payload)
            for payload in self._decode_entries(store.raw_locked(AUDIT_KEY, "[]"))
        ]

    def _raw_entries(self) -> list[dict[str, object]]:
        raw, present = self._store.get(AUDIT_KEY)
        if not present:
            return []
        return self._decode_entries(raw)

    def entries(self) -> list[Entry]:
        return [Entry.from_payload(payload) for payload in self._raw_entries()]

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
