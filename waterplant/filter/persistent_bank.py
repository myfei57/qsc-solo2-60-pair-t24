"""Filter bank whose bed registry survives a console restart."""

from __future__ import annotations

import json

from waterplant.store.store import Store

from .bank import Bank
from .bed import Bed
from .report import BANK_KEY


class PersistentBank(Bank):
    """In-memory bank operations with a durable JSON snapshot."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self._store = store
        self._load()

    def add_bed(self, bed_id: str, zone: int, load: float = 0.0) -> Bed:
        with self._lock:
            bed = super().add_bed(bed_id, zone, load)
            self._persist_locked()
            return bed

    def remove_bed(self, bed_id: str) -> None:
        with self._lock:
            super().remove_bed(bed_id)
            self._persist_locked()

    def close(self, bed_id: str) -> None:
        with self._lock:
            super().close(bed_id)
            self._persist_locked()

    def open(self, bed_id: str) -> None:
        with self._lock:
            super().open(bed_id)
            self._persist_locked()

    def reset_closed(self) -> None:
        with self._lock:
            super().reset_closed()
            self._persist_locked()

    def set_load(self, bed_id: str, load: float) -> None:
        with self._lock:
            super().set_load(bed_id, load)
            self._persist_locked()

    def renumber(self, bed_id: str, zone: int) -> None:
        with self._lock:
            super().renumber(bed_id, zone)
            self._persist_locked()

    def rotate(self, bed_id: str) -> None:
        with self._lock:
            super().rotate(bed_id)
            self._persist_locked()

    def restore_beds(self, beds: list[dict[str, object]]) -> None:
        """Replace the registry with a saved snapshot, used by cycle recovery."""

        with self._lock:
            self._beds = {}
            for item in beds:
                bed = Bed(
                    id=str(item["id"]),
                    zone=int(item["zone"]),
                    load=float(item.get("load", 0.0)),
                    closed=bool(item.get("closed", False)),
                    duty=bool(item.get("duty", False)),
                )
                self._beds[bed.id] = bed
            self._persist_locked()

    def _load(self) -> None:
        raw, present = self._store.get(BANK_KEY)
        if not present:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if not isinstance(payload, list):
            return
        for item in payload:
            if not isinstance(item, dict) or "id" not in item or "zone" not in item:
                continue
            self._beds[str(item["id"])] = Bed(
                id=str(item["id"]),
                zone=int(item["zone"]),
                load=float(item.get("load", 0.0)),
                closed=bool(item.get("closed", False)),
                duty=bool(item.get("duty", False)),
            )

    def _persist_locked(self) -> None:
        self._store.put(BANK_KEY, json.dumps([bed.snapshot() for bed in self.beds()], ensure_ascii=False))
