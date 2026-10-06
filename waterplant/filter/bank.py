"""The set of filter beds under one control console.

The bank mirrors its beds into the key/value store on every mutation so a
restarted console still knows the beds, their closed state and who is on duty.
That persistence is what lets an interrupted backwash step recover after the
process restarts.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from .bed import Bed
from .report import BankState, BedState

BANK_KEY = "filter:bank"


class Bank:
    """Thread safe registry of filter beds, optionally persisted."""

    def __init__(self, store: Optional[object] = None) -> None:
        self._beds: dict[str, Bed] = {}
        self._store = store
        self._lock = threading.RLock()
        if store is not None:
            self._load()

    # ---------------------------------------------------------- persistence

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
            if not isinstance(item, dict):
                continue
            try:
                bed = Bed(
                    id=str(item["id"]),
                    zone=int(item["zone"]),
                    load=float(item.get("load", 0.0)),
                    closed=bool(item.get("closed", False)),
                    duty=bool(item.get("duty", False)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._beds[bed.id] = bed

    def _persist_locked(self) -> None:
        if self._store is None:
            return
        self._store.put(
            BANK_KEY,
            json.dumps(
                [bed.snapshot() for bed in (self._beds[key] for key in sorted(self._beds))],
                ensure_ascii=False,
            ),
        )

    def add_bed(self, bed_id: str, zone: int, load: float = 0.0) -> Bed:
        with self._lock:
            bed = Bed(id=bed_id, zone=zone, load=load)
            self._beds[bed_id] = bed
            self._persist_locked()
            return bed

    def bed(self, bed_id: str) -> Bed | None:
        with self._lock:
            return self._beds.get(bed_id)

    def beds(self) -> list[Bed]:
        with self._lock:
            return [self._beds[key] for key in sorted(self._beds)]

    def remove_bed(self, bed_id: str) -> None:
        with self._lock:
            if bed_id not in self._beds:
                raise ValueError(f"filter bed {bed_id} not found")
            del self._beds[bed_id]
            self._persist_locked()

    def close(self, bed_id: str) -> None:
        with self._lock:
            self._require(bed_id).closed = True
            self._persist_locked()

    def open(self, bed_id: str) -> None:
        with self._lock:
            self._require(bed_id).closed = False
            self._persist_locked()

    def is_closed(self, bed_id: str) -> bool:
        with self._lock:
            return self._require(bed_id).closed

    def reset_closed(self) -> None:
        with self._lock:
            for bed in self._beds.values():
                bed.closed = False
            self._persist_locked()

    def set_load(self, bed_id: str, load: float) -> None:
        with self._lock:
            self._require(bed_id).load = load
            self._persist_locked()

    def renumber(self, bed_id: str, zone: int) -> None:
        with self._lock:
            self._require(bed_id).zone = zone
            self._persist_locked()

    def mapping(self) -> dict[int, str]:
        """Zone to bed identifier, rebuilt from the live bed registry."""

        with self._lock:
            return {bed.zone: bed_id for bed_id, bed in self._beds.items()}

    def bed_zone(self, bed_id: str) -> tuple[int, bool]:
        with self._lock:
            bed = self._beds.get(bed_id)
            if bed is None:
                return 0, False
            return bed.zone, True

    def rotate(self, bed_id: str) -> None:
        """Give the duty flag to one bed and clear it everywhere else."""

        with self._lock:
            self._require(bed_id)
            for bed in self._beds.values():
                bed.duty = False
            self._beds[bed_id].duty = True
            self._persist_locked()

    def on_duty(self) -> str | None:
        with self._lock:
            for bed_id, bed in self._beds.items():
                if bed.duty:
                    return bed_id
            return None

    def dirtiest(self) -> str:
        beds = self.beds()
        if not beds:
            return ""
        return max(beds, key=lambda bed: (bed.load, bed.id)).id

    def cleanest(self) -> str:
        beds = self.beds()
        if not beds:
            return ""
        return min(beds, key=lambda bed: (bed.load, bed.id)).id

    def count(self) -> int:
        with self._lock:
            return len(self._beds)

    def active_count(self) -> int:
        return sum(1 for bed in self.beds() if not bed.closed)

    def total_load(self) -> float:
        return sum(bed.load for bed in self.beds())

    def bed_ids(self) -> list[str]:
        return [bed.id for bed in self.beds()]

    def state(self) -> BankState:
        beds = self.beds()
        return BankState(
            beds=[
                BedState(
                    id=bed.id,
                    zone=bed.zone,
                    load=bed.load,
                    closed=bed.closed,
                    duty=bed.duty,
                )
                for bed in beds
            ],
            on_duty=self.on_duty(),
            count=len(beds),
        )

    def describe(self) -> str:
        state = self.state()
        return f"filter beds={state.count} on_duty={state.on_duty or ''}"

    def _require(self, bed_id: str) -> Bed:
        bed = self._beds.get(bed_id)
        if bed is None:
            raise ValueError(f"filter bed {bed_id} not found")
        return bed
